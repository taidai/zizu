"""Ticket #3 的非控制业务 REST 权限声明。

路由只声明业务能力，不感知角色；角色与能力的对应关系由 Identity 统一执行。
这份小接口也把能力写进 OpenAPI，便于覆盖测试发现遗漏的新端点。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from fastapi import Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.api.security import _client_ip, get_identity, require_capability
from app.services.identity import AuditEvent, Identity, Principal


RUNTIME_READ = "runtime.read"
CONFIGURATION_READ = "configuration.read"
CONFIGURATION_WRITE = "configuration.write"
ALARM_ACKNOWLEDGE = "alarm.acknowledge"
LEGACY_ALARM_WRITE = "legacy_alarm.write"


_authorize_configuration_write = require_capability(CONFIGURATION_WRITE)


def _operation_target(request: Request) -> str:
    """Return a stable route template without query, body, or credential data."""
    # FastAPI's included-router object exposes the child path (for example
    # ``/categories``), not its externally visible ``/api/v1`` prefix.  Start
    # from the actual URL path, then replace every parsed path value with its
    # parameter name so resource IDs/names never enter the audit record.
    route_path = request.url.path
    for name, value in request.path_params.items():
        route_path = route_path.replace(f"/{value}", f"/{{{name}}}")
    return f"{request.method.upper()} {route_path}"


async def _configuration_change_audit(
    request: Request,
    principal: Principal = Depends(_authorize_configuration_write),
    identity: Identity = Depends(get_identity),
) -> AsyncIterator[Principal]:
    """Guard every configuration write with a minimal append-only audit pair.

    The requested event is persisted before the endpoint can touch business
    state, so an unavailable audit store fails closed.  The success event is
    appended only after the endpoint returns normally.  Existing endpoints
    own and commit their business transactions, so the post-success event is
    deliberately *not* atomic with those commits; a later UnitOfWork migration
    is required to close that remaining boundary.
    """
    event_context = {
        "event": "configuration.change",
        "actor": principal.actor,
        "target": _operation_target(request),
        "request_id": request.headers.get("X-Request-ID"),
        "client_ip": _client_ip(request),
    }
    try:
        await run_in_threadpool(
            identity.audit,
            AuditEvent(outcome="requested", **event_context),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "AUDIT_UNAVAILABLE",
                "message": "Configuration audit service is unavailable",
            },
        ) from exc

    try:
        yield principal
    except Exception:
        # The endpoint failed: preserve the requested attempt but never claim
        # that the configuration change succeeded.
        raise
    else:
        try:
            await run_in_threadpool(
                identity.audit,
                AuditEvent(outcome="success", **event_context),
            )
        except Exception as exc:
            # The business endpoint may already have committed.  Returning a
            # stable failure is safer than silently losing the required audit,
            # but callers must not blindly retry until the UnitOfWork boundary
            # described above exists.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "AUDIT_UNAVAILABLE",
                    "message": "Configuration audit service is unavailable",
                },
            ) from exc


def protected(
    capability: str,
    **operation_options: object,
) -> dict[str, object]:
    """Return the two pieces every protected operation must publish."""
    extra = dict(operation_options.pop("openapi_extra", {}) or {})
    extra["x-zizu-capability"] = capability
    dependency = (
        Depends(_configuration_change_audit, scope="function")
        if capability == CONFIGURATION_WRITE
        else Depends(require_capability(capability))
    )
    return {
        **operation_options,
        "dependencies": [dependency],
        "openapi_extra": extra,
    }


def capability_metadata(capability: str) -> dict[str, str]:
    """Declare an actor-sensitive operation without adding a second dependency."""
    return {"x-zizu-capability": capability}


def principal_for(capability: str) -> Callable[..., Principal]:
    """Expose a Principal only when an operation needs the server-side actor."""
    if capability == CONFIGURATION_WRITE:
        return _configuration_change_audit
    return require_capability(capability)
