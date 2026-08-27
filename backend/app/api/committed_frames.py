"""One public read seam for committed L0/L2 frame snapshots and deltas."""
from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.api.business_security import RUNTIME_READ, protected
from app.api.security import (
    _INSECURE_DEVELOPMENT_PRINCIPAL,
    _client_ip,
    _request_scheme,
    get_identity,
)
from app.core.config import settings
from app.services.committed_frame_stream import (
    CommittedFrameStream,
    FrameScope,
    FrameStreamError,
)
from app.services.committed_frame_stream_postgres import (
    PostgresCommittedFrameStreamRepository,
)
from app.services.identity import Identity, IdentityError


router = APIRouter()
_stream: CommittedFrameStream | None = None


def get_committed_frame_stream() -> CommittedFrameStream:
    global _stream
    if _stream is None:
        _stream = CommittedFrameStream(PostgresCommittedFrameStreamRepository())
    return _stream


def set_committed_frame_stream(stream: CommittedFrameStream | None) -> None:
    global _stream
    _stream = stream


@router.get("/runtime/frame-snapshot", **protected(RUNTIME_READ))
async def read_frame_snapshot(
    node_id: UUID,
    stream: CommittedFrameStream = Depends(get_committed_frame_stream),
) -> dict:
    try:
        snapshot = await run_in_threadpool(
            stream.read_snapshot,
            FrameScope.for_node(node_id),
        )
    except FrameStreamError as exc:
        raise _frame_http_error(exc) from exc
    return snapshot.public_dict()


@router.websocket("/ws/data-frames")
async def data_frames_ws(
    ws: WebSocket,
    identity: Identity = Depends(get_identity),
    stream: CommittedFrameStream = Depends(get_committed_frame_stream),
) -> None:
    await ws.accept()
    subscription = None
    if (
        settings.auth_require_https
        and _request_scheme(ws) != "https"
        and not (
            settings.deployment_mode == "development"
            and settings.allow_insecure_anonymous_access
        )
    ):
        await ws.close(code=4406, reason="secure WebSocket required")
        return
    try:
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
            authenticate = first.get("authenticate")
            ticket = (
                authenticate.get("ticket")
                if isinstance(authenticate, dict)
                else None
            )
            if not isinstance(ticket, str):
                raise ValueError("ticket required")
            if (
                settings.deployment_mode == "development"
                and settings.allow_insecure_anonymous_access
                and ticket == "insecure-development"
            ):
                principal = _INSECURE_DEVELOPMENT_PRINCIPAL
            else:
                principal = await asyncio.to_thread(
                    identity.consume_ws_ticket,
                    ticket,
                    client_ip=_client_ip(ws),
                )
            await asyncio.to_thread(identity.authorize, principal, RUNTIME_READ)
        except (IdentityError, ValueError, AttributeError, TimeoutError):
            await ws.close(code=4401, reason="authentication required")
            return

        await ws.send_json({"type": "authenticated"})
        try:
            command = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
            requested = command.get("subscribe")
            if not isinstance(requested, dict):
                raise ValueError("subscription required")
            node_value = requested.get("node_id")
            cursor = requested.get("after")
            if not isinstance(node_value, str) or not isinstance(cursor, str):
                raise ValueError("node and cursor required")
            node_id = UUID(node_value)
            if principal != _INSECURE_DEVELOPMENT_PRINCIPAL:
                principal = await asyncio.to_thread(
                    identity.revalidate_session,
                    principal,
                    client_ip=_client_ip(ws),
                )
            await asyncio.to_thread(identity.authorize, principal, RUNTIME_READ)
            subscription = await stream.subscribe_after(
                FrameScope.for_node(node_id),
                cursor,
            )
        except IdentityError as exc:
            await ws.close(
                code=4401 if exc.status_code == 401 else 4403,
                reason=(
                    "authentication required"
                    if exc.status_code == 401
                    else "permission denied"
                ),
            )
            return
        except (ValueError, AttributeError, TimeoutError):
            await ws.send_json({"type": "error", "code": "SUBSCRIPTION_INVALID"})
            await ws.close(code=4400, reason="invalid subscription")
            return
        except FrameStreamError as exc:
            if exc.code == "FRAME_CURSOR_TOO_OLD":
                await ws.send_json(
                    {"type": "resnapshot_required", "code": exc.code}
                )
                await ws.close(code=4409, reason="snapshot required")
            else:
                await ws.send_json({"type": "error", "code": exc.code})
                await ws.close(
                    code=4404 if exc.code == "FRAME_SCOPE_NOT_FOUND" else 4400,
                    reason="subscription unavailable",
                )
            return

        await ws.send_json(
            {
                "type": "subscribed",
                "node_id": str(node_id),
            }
        )
        while True:
            try:
                delta = await subscription.receive()
            except FrameStreamError as exc:
                await ws.send_json(
                    {"type": "resnapshot_required", "code": exc.code}
                )
                await ws.close(code=4409, reason="snapshot required")
                return
            await ws.send_json(delta.public_dict())
    except WebSocketDisconnect:
        pass
    finally:
        if subscription is not None:
            await stream.unsubscribe(subscription)


def _frame_http_error(exc: FrameStreamError) -> HTTPException:
    if exc.code == "FRAME_SCOPE_NOT_FOUND":
        status_code = 404
    elif exc.code in {"FRAME_CURSOR_TOO_OLD", "FRAME_CURSOR_SCOPE_MISMATCH"}:
        status_code = 409
    elif exc.code.endswith("_UNAVAILABLE"):
        status_code = 503
    else:
        status_code = 400
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc)},
    )
