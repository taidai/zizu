"""FastAPI 身份适配器：只负责 Bearer、请求上下文和能力依赖。"""
from __future__ import annotations

from collections.abc import Callable
import ipaddress
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.services.identity import (
    AuditEvent,
    Identity,
    IdentityError,
    PostgresIdentityRepository,
    Principal,
)


_bearer = HTTPBearer(auto_error=False)
_identity = Identity(
    PostgresIdentityRepository(),
    session_minutes=settings.auth_session_minutes,
)

_INSECURE_DEVELOPMENT_PRINCIPAL = Principal(
    user_id=UUID("00000000-0000-0000-0000-000000000000"),
    username="insecure-development-anonymous",
    role="admin",
    session_id=UUID("00000000-0000-0000-0000-000000000000"),
)


def get_identity() -> Identity:
    return _identity


def _trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(
        ipaddress.ip_network(cidr, strict=False)
        for cidr in settings.auth_trusted_proxy_cidrs
    )


def _peer_ip(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if request.client is None:
        return None
    try:
        return ipaddress.ip_address(request.client.host)
    except ValueError:
        return None


def _peer_is_trusted_proxy(request: Request) -> bool:
    if not settings.auth_trust_proxy_headers:
        return False
    peer = _peer_ip(request)
    return peer is not None and any(peer in network for network in _trusted_proxy_networks())


def _client_ip(request: Request) -> str | None:
    peer = _peer_ip(request)
    if peer is None:
        return None
    if not _peer_is_trusted_proxy(request):
        return str(peer)

    raw_chain = request.headers.get("X-Forwarded-For", "")
    try:
        forwarded = [
            ipaddress.ip_address(item.strip())
            for item in raw_chain.split(",")
            if item.strip()
        ]
    except ValueError:
        return str(peer)
    networks = _trusted_proxy_networks()
    for candidate in reversed([*forwarded, peer]):
        if not any(candidate in network for network in networks):
            return str(candidate)
    return str(forwarded[0]) if forwarded else str(peer)


def _request_scheme(request: Request) -> str:
    if _peer_is_trusted_proxy(request):
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        forwarded_values = [
            value.strip().lower()
            for value in forwarded_proto.split(",")
            if value.strip()
        ]
        # A trusted edge proxy must overwrite this header. Multiple values are
        # ambiguous and may contain an attacker-supplied left-most value, so
        # never use them to upgrade an HTTP peer to HTTPS.
        if len(forwarded_values) == 1 and forwarded_values[0] in {"http", "https"}:
            return forwarded_values[0]
    return {"ws": "http", "wss": "https"}.get(
        request.url.scheme.lower(),
        request.url.scheme.lower(),
    )


async def require_secure_auth_transport(
    request: Request,
    identity: Identity,
) -> None:
    if settings.auth_require_https and _request_scheme(request) != "https":
        try:
            await run_in_threadpool(
                identity.audit,
                AuditEvent(
                    event="authentication.transport",
                    outcome="denied",
                    reason="https_required",
                    target=request.url.path,
                    request_id=request.headers.get("X-Request-ID"),
                    client_ip=_client_ip(request),
                ),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "AUTH_UNAVAILABLE",
                    "message": "Authentication service is unavailable",
                },
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            detail={
                "code": "HTTPS_REQUIRED",
                "message": "Authentication requires an HTTPS connection",
            },
        )


def identity_http_error(exc: IdentityError) -> HTTPException:
    headers: dict[str, str] = {}
    if exc.status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
        headers=headers or None,
    )


async def current_principal(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    identity: Identity = Depends(get_identity),
) -> Principal:
    request_id = request.headers.get("X-Request-ID")
    client_ip = _client_ip(request)
    if (
        settings.deployment_mode == "development"
        and settings.allow_insecure_anonymous_access
        and credentials is None
    ):
        response.headers["X-ZiZu-Security-Mode"] = "insecure-development"
        return _INSECURE_DEVELOPMENT_PRINCIPAL
    await require_secure_auth_transport(request, identity)
    if credentials is None or credentials.scheme.lower() != "bearer":
        try:
            await run_in_threadpool(
                identity.reject_anonymous,
                request.url.path,
                request_id=request_id,
                client_ip=client_ip,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "AUTH_UNAVAILABLE",
                    "message": "Authentication service is unavailable",
                },
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Bearer authentication is required",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await run_in_threadpool(
            identity.resolve,
            credentials.credentials,
            request_id=request_id,
            client_ip=client_ip,
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


def require_capability(capability: str) -> Callable[..., Principal]:
    async def dependency(
        request: Request,
        principal: Principal = Depends(current_principal),
        identity: Identity = Depends(get_identity),
    ) -> Principal:
        try:
            return await run_in_threadpool(
                identity.authorize,
                principal,
                capability,
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

    return dependency
