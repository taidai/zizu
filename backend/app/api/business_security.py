"""REST 能力声明。

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
SYSTEM_MANAGE = "system.manage"
GATEWAY_MANAGE = "gateway.manage"
CONTROL_WRITE = "control.write"


_AUDITED_OPERATION_POLICIES = {
    CONFIGURATION_WRITE: (
        "configuration.change",
        "Configuration audit service is unavailable",
    ),
    ALARM_ACKNOWLEDGE: (
        "alarm.acknowledge",
        "Alarm acknowledgement audit service is unavailable",
    ),
    SYSTEM_MANAGE: (
        "system.operation",
        "Privileged operation audit is unavailable",
    ),
    GATEWAY_MANAGE: (
        "gateway.operation",
        "Privileged operation audit is unavailable",
    ),
    CONTROL_WRITE: (
        "control.command",
        "Privileged operation audit is unavailable",
    ),
}


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


def _audited_operation(capability: str) -> Callable[..., AsyncIterator[Principal]]:
    """Build a fail-closed authorization and append-only audit dependency.

    The requested event is persisted before the endpoint can touch business
    state.  The success event is appended only after the endpoint returns.
    Existing endpoints own their transactions, so the latter is not yet
    atomic with every business write; callers must not blindly retry a 503.
    """
    authorize = require_capability(capability)
    event_name, unavailable_message = _AUDITED_OPERATION_POLICIES[capability]

    async def dependency(
        request: Request,
        principal: Principal = Depends(authorize),
        identity: Identity = Depends(get_identity),
    ) -> AsyncIterator[Principal]:
        event_context = {
            "event": event_name,
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
                    "message": unavailable_message,
                },
            ) from exc
        try:
            yield principal
        except Exception:
            raise
        else:
            try:
                await run_in_threadpool(
                    identity.audit,
                    AuditEvent(outcome="success", **event_context),
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "AUDIT_UNAVAILABLE",
                        "message": unavailable_message,
                    },
                ) from exc

    return dependency


_AUDITED_OPERATION_DEPENDENCIES = {
    capability: _audited_operation(capability)
    for capability in _AUDITED_OPERATION_POLICIES
}


def protected(
    capability: str,
    **operation_options: object,
) -> dict[str, object]:
    """Return the two pieces every protected operation must publish."""
    extra = dict(operation_options.pop("openapi_extra", {}) or {})
    extra["x-zizu-capability"] = capability
    audited_dependency = _AUDITED_OPERATION_DEPENDENCIES.get(capability)
    dependency = (
        Depends(audited_dependency, scope="function")
        if audited_dependency is not None
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
    audited_dependency = _AUDITED_OPERATION_DEPENDENCIES.get(capability)
    if audited_dependency is not None:
        return audited_dependency
    return require_capability(capability)
