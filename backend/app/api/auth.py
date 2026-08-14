"""登录、当前身份和注销公开 HTTP 适配器。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.security import (
    _client_ip,
    current_principal,
    get_identity,
    identity_http_error,
    require_secure_auth_transport,
)
from app.services.identity import Identity, IdentityError, Principal


router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    # Secret fields intentionally avoid Pydantic length constraints: FastAPI's
    # default 422 body includes rejected input. Enforce the byte limit below
    # with a stable response that never reflects the password.
    password: str


async def _read_login_request(request: Request) -> LoginRequest:
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16 * 1024:
                raise ValueError("login request is too large")
            raw.extend(chunk)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("login request must be an object")
        for field in ("username", "password"):
            value = payload.get(field)
            if not isinstance(value, str):
                raise ValueError("login fields must be strings")
            value.encode("utf-8")
        return LoginRequest.model_validate(payload)
    except (UnicodeError, ValueError, TypeError, ValidationError):
        # Never return Pydantic's default error body for a credential payload:
        # it includes the rejected input and can therefore reflect a password.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "AUTH_REQUEST_INVALID",
                "message": "Login request is invalid",
            },
        ) from None


def _user_dict(principal: Principal) -> dict[str, str]:
    return {
        "id": str(principal.user_id),
        "username": principal.username,
        "role": principal.role,
    }


@router.post(
    "/auth/login",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["username", "password"],
                        "properties": {
                            "username": {"type": "string", "maxLength": 128},
                            "password": {"type": "string", "writeOnly": True},
                        },
                    }
                }
            },
        }
    },
)
async def login(
    request: Request,
    response: Response,
    identity: Identity = Depends(get_identity),
) -> dict:
    await require_secure_auth_transport(request, identity)
    try:
        body = await _read_login_request(request)
    except HTTPException as request_error:
        try:
            await run_in_threadpool(
                identity.reject_login_request,
                request_id=request.headers.get("X-Request-ID"),
                client_ip=_client_ip(request),
            )
        except IdentityError as exc:
            raise identity_http_error(exc) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "AUTH_UNAVAILABLE",
                    "message": "Authentication service is unavailable",
                },
            ) from exc
        raise request_error
    try:
        session = await run_in_threadpool(
            identity.authenticate,
            body.username,
            body.password,
            request_id=request.headers.get("X-Request-ID"),
            client_ip=_client_ip(request),
        )
    except IdentityError as exc:
        raise identity_http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "AUTH_UNAVAILABLE",
                "message": "Authentication service is unavailable",
            },
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {
        "access_token": session.access_token,
        "token_type": "bearer",
        "expires_at": session.expires_at.isoformat(),
        "user": _user_dict(session.principal),
    }


@router.get("/auth/me")
async def me(principal: Principal = Depends(current_principal)) -> dict:
    return {"user": _user_dict(principal)}


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    principal: Principal = Depends(current_principal),
    identity: Identity = Depends(get_identity),
) -> None:
    try:
        await run_in_threadpool(identity.revoke, principal)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "AUTH_UNAVAILABLE",
                "message": "Authentication service is unavailable",
            },
        ) from exc


@router.post("/auth/ws-ticket", status_code=status.HTTP_201_CREATED)
async def issue_websocket_ticket(
    request: Request,
    response: Response,
    principal: Principal = Depends(current_principal),
    identity: Identity = Depends(get_identity),
) -> dict:
    """Issue a 30-second, single-use telemetry WebSocket ticket."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if principal.username == "insecure-development-anonymous":
        response_expires = datetime.now(timezone.utc) + timedelta(seconds=30)
        return {
            "ticket": "insecure-development",
            "expires_at": response_expires.isoformat(),
        }
    try:
        issued = await run_in_threadpool(
            identity.issue_ws_ticket,
            principal,
            request_id=request.headers.get("X-Request-ID"),
            client_ip=_client_ip(request),
        )
    except IdentityError as exc:
        raise identity_http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "AUTH_UNAVAILABLE",
                "message": "Authentication service is unavailable",
            },
        ) from exc
    return {"ticket": issued.ticket, "expires_at": issued.expires_at.isoformat()}
