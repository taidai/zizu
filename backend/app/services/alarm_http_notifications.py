"""Deep module for safe alarm HTTP notification requests.

Alarm state transitions create delivery intent elsewhere.  This module owns
request validation, template rendering, secret encryption and HTTP result
classification; it never changes alarm state.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import re
import time
from collections.abc import Sequence
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
import httpx


ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
ALLOWED_VARIABLES = frozenset(
    {
        "notification.id",
        "event.id",
        "event.type",
        "event.time",
        "alarm.name",
        "alarm.severity",
        "alarm.state",
        "alarm.definition_id",
        "alarm.rule_key",
        "node.id",
        "node.name",
        "node.path",
        "entity.id",
        "entity.key",
        "entity.name",
        "entity.value",
        "entity.unit",
        "entity.quality",
        "entity.observed_at",
    }
)
SYSTEM_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "host",
        "idempotency-key",
        "x-zizu-notification-id",
    }
)
_TEMPLATE_VARIABLE = re.compile(r"{{\s*([a-z][a-z0-9_.]*)\s*}}")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.azure.internal",
        "instance-data.ec2.internal",
    }
)


class HttpNotificationError(ValueError):
    """Stable, machine-readable HTTP notification failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RequestField:
    key: str
    value: str = ""
    sensitive: bool = False
    clear: bool = False


@dataclass(frozen=True)
class HttpNotificationDraft:
    name: str
    description: str | None
    method: str
    url: str
    query_params: Sequence[RequestField]
    headers: Sequence[RequestField]
    content_type: str
    body_template: str
    timeout_seconds: int = 5


@dataclass(frozen=True)
class NotificationContext:
    values: dict[str, object]


@dataclass(frozen=True)
class RenderedHttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    timeout_seconds: int
    target_display: str


@dataclass(frozen=True)
class HttpSendResult:
    delivered: bool
    outcome: str
    http_status: int | None
    duration_ms: int
    error_code: str | None
    error_detail: str | None
    response_excerpt: str | None
    method: str | None = None
    target_display: str | None = None


@dataclass(frozen=True)
class StoredHttpNotificationConfig:
    id: UUID
    name: str
    description: str | None
    method: str
    url_display: str
    public_query_params: Sequence[RequestField]
    secret_query_param_names: Sequence[str]
    public_headers: Sequence[RequestField]
    secret_header_names: Sequence[str]
    content_type: str
    body_template: str
    timeout_seconds: int
    current_digest: str
    tested_digest: str | None
    tested_at: Any | None
    last_test_status: dict[str, object] | None
    enabled: bool


@dataclass(frozen=True)
class ResolvedHttpNotificationConfig:
    id: UUID
    draft: HttpNotificationDraft
    current_digest: str
    tested_digest: str | None
    enabled: bool


@dataclass(frozen=True)
class DeliveryClaim:
    id: UUID
    transition_id: UUID
    transition_code: str
    event_id: UUID
    configuration_id: UUID
    context: NotificationContext
    attempt_count: int
    cycle_attempt_count: int
    lease_owner: str


class SecretCodec:
    """Encrypt request targets and sensitive fields without plaintext fallback."""

    def __init__(self, key: str | None) -> None:
        self._fernet: Fernet | None = None
        if key and key.strip():
            try:
                self._fernet = Fernet(key.strip().encode("ascii"))
            except (ValueError, UnicodeEncodeError) as error:
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_SECRET_KEY_NOT_CONFIGURED",
                    "HTTP notification encryption key is invalid",
                ) from error

    def encrypt(self, value: str) -> str:
        fernet = self._required()
        return fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        fernet = self._required()
        try:
            return fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError) as error:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_SECRET_KEY_NOT_CONFIGURED",
                "HTTP notification secret cannot be decrypted",
            ) from error

    def _required(self) -> Fernet:
        if self._fernet is None:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_SECRET_KEY_NOT_CONFIGURED",
                "HTTP notification encryption key is not configured",
            )
        return self._fernet


class AlarmHttpNotificationRepository(Protocol):
    def list_configs(self) -> Sequence[StoredHttpNotificationConfig]: ...

    def get_config(
        self,
        config_id: UUID,
    ) -> StoredHttpNotificationConfig | None: ...

    def resolve_config(
        self,
        config_id: UUID,
    ) -> ResolvedHttpNotificationConfig | None: ...

    def create_config(
        self,
        draft: HttpNotificationDraft,
        actor: str,
    ) -> StoredHttpNotificationConfig: ...

    def update_config(
        self,
        config_id: UUID,
        draft: HttpNotificationDraft,
        actor: str,
    ) -> StoredHttpNotificationConfig: ...

    def record_test(
        self,
        config_id: UUID,
        digest: str,
        result: HttpSendResult,
        actor: str,
    ) -> StoredHttpNotificationConfig: ...

    def set_enabled(
        self,
        config_id: UUID,
        enabled: bool,
        actor: str,
    ) -> StoredHttpNotificationConfig: ...

    def delete_config(self, config_id: UUID, actor: str) -> None: ...

    def list_deliveries(
        self,
        *,
        page: int,
        page_size: int,
    ) -> dict[str, object]: ...

    def retry_delivery(
        self,
        notification_id: UUID,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, object]: ...


class AlarmDeliveryRepository(Protocol):
    def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> DeliveryClaim | None: ...

    def current_config(
        self,
        config_id: UUID,
    ) -> ResolvedHttpNotificationConfig | None: ...

    def complete_attempt(
        self,
        claim: DeliveryClaim,
        result: HttpSendResult,
        now: datetime,
    ) -> None: ...

    def release_lease(self, notification_id: UUID, worker_id: str) -> None: ...

    def cancel_missing_config(
        self,
        claim: DeliveryClaim,
        now: datetime,
    ) -> None: ...


Sender = Callable[[RenderedHttpRequest], Awaitable[HttpSendResult]]


class AlarmHttpNotifications:
    """Small public facade over the notification persistence and sender seams."""

    def __init__(
        self,
        repository: AlarmHttpNotificationRepository,
        sender: Sender | None = None,
    ) -> None:
        self._repository = repository
        self._sender = sender or send_http_request

    @property
    def repository(self) -> AlarmHttpNotificationRepository:
        return self._repository

    def list(self) -> Sequence[dict[str, object]]:
        return tuple(public_config(item) for item in self._repository.list_configs())

    def list_options(self) -> Sequence[dict[str, object]]:
        """Read-only rule choices; never expose request contents or credentials."""
        return tuple(
            {
                "id": str(item.id),
                "name": item.name,
                "status": (
                    "needs_test"
                    if not item.tested_digest or item.tested_digest != item.current_digest
                    else "available" if item.enabled else "disabled"
                ),
            }
            for item in self._repository.list_configs()
        )

    def create(
        self,
        draft: HttpNotificationDraft,
        actor: str,
    ) -> dict[str, object]:
        return public_config(
            self._repository.create_config(normalize_draft(draft), actor)
        )

    def update(
        self,
        config_id: UUID,
        draft: HttpNotificationDraft,
        actor: str,
    ) -> dict[str, object]:
        return public_config(
            self._repository.update_config(config_id, draft, actor)
        )

    async def test(self, config_id: UUID, actor: str) -> dict[str, object]:
        resolved = self._repository.resolve_config(config_id)
        if resolved is None:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_NOT_FOUND",
                "HTTP notification configuration was not found",
            )
        notification_id = str(UUID(int=config_id.int ^ 1))
        now = datetime.now(timezone.utc).isoformat()
        context = NotificationContext(
            {
                "notification.id": notification_id,
                "event.id": str(UUID(int=config_id.int ^ 2)),
                "event.type": "TEST",
                "event.time": now,
                "alarm.name": "测试告警",
                "alarm.severity": "WARNING",
                "alarm.state": "test",
                "alarm.definition_id": "test-definition",
                "alarm.rule_key": "test.rule",
                "node.id": "test-node",
                "node.name": "测试节点",
                "node.path": "测试场站/测试节点",
                "entity.id": "test-entity",
                "entity.key": "test.value",
                "entity.name": "测试实体",
                "entity.value": 1,
                "entity.unit": None,
                "entity.quality": 192,
                "entity.observed_at": now,
            }
        )
        result = await self._sender(render_request(resolved.draft, context))
        return public_config(
            self._repository.record_test(
                config_id,
                resolved.current_digest,
                result,
                actor,
            )
        )

    def enable(self, config_id: UUID, actor: str) -> dict[str, object]:
        return public_config(
            self._repository.set_enabled(config_id, True, actor)
        )

    def disable(self, config_id: UUID, actor: str) -> dict[str, object]:
        return public_config(
            self._repository.set_enabled(config_id, False, actor)
        )

    def delete(self, config_id: UUID, actor: str) -> None:
        self._repository.delete_config(config_id, actor)

    def list_deliveries(
        self,
        *,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        return self._repository.list_deliveries(page=page, page_size=page_size)

    def retry(
        self,
        notification_id: UUID,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return self._repository.retry_delivery(
            notification_id,
            actor,
            idempotency_key,
        )


class AlarmHttpNotificationDispatcher:
    """Claims committed intents and sends one bounded HTTP attempt per tick."""

    def __init__(
        self,
        repository: AlarmDeliveryRepository,
        *,
        sender: Sender | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._sender = sender or send_http_request
        self._worker_id = worker_id or f"alarm-http-{uuid4()}"

    async def run_once(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        claim = self._repository.claim_due(
            worker_id=self._worker_id,
            now=current,
        )
        if claim is None:
            return 0
        try:
            config = self._repository.current_config(claim.configuration_id)
            if config is None:
                self._repository.cancel_missing_config(claim, current)
                return 1
            if (
                not config.enabled
                or config.tested_digest != config.current_digest
            ):
                self._repository.release_lease(claim.id, self._worker_id)
                return 0
            try:
                request = render_request(config.draft, claim.context)
            except HttpNotificationError as error:
                result = HttpSendResult(
                    False,
                    "render_error",
                    None,
                    0,
                    error.code,
                    str(error),
                    None,
                    config.draft.method,
                    mask_url(config.draft.url),
                )
            else:
                sent = await self._sender(request)
                result = replace(
                    sent,
                    method=request.method,
                    target_display=request.target_display,
                )
            self._repository.complete_attempt(claim, result, current)
            return 1
        except Exception:
            self._repository.release_lease(claim.id, self._worker_id)
            raise


def normalize_draft(draft: HttpNotificationDraft) -> HttpNotificationDraft:
    name = draft.name.strip()
    if not name:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_TEMPLATE",
            "HTTP notification name is required",
        )
    method = draft.method.strip().upper()
    if method not in ALLOWED_METHODS:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_TEMPLATE",
            "HTTP method is unsupported",
        )
    url = _validated_url(draft.url)
    if not isinstance(draft.timeout_seconds, int) or not 1 <= draft.timeout_seconds <= 30:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_TEMPLATE",
            "HTTP timeout must be between 1 and 30 seconds",
        )
    content_type = draft.content_type.strip()
    if not content_type or "\r" in content_type or "\n" in content_type:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_TEMPLATE",
            "Content-Type is invalid",
        )
    query_params = _normalize_fields(draft.query_params, header=False)
    headers = _normalize_fields(draft.headers, header=True)
    body_template = str(draft.body_template)
    _validate_template(body_template, content_type)
    description = draft.description.strip() if draft.description else None
    return replace(
        draft,
        name=name,
        description=description or None,
        method=method,
        url=url,
        query_params=query_params,
        headers=headers,
        content_type=content_type,
        body_template=body_template,
    )


def draft_digest(draft: HttpNotificationDraft) -> str:
    """Hash only request material; labels do not invalidate a successful test."""
    normalized = normalize_draft(draft)
    material = {
        "method": normalized.method,
        "url": normalized.url,
        "query_params": [
            {"key": field.key, "value": field.value, "sensitive": field.sensitive}
            for field in normalized.query_params
        ],
        "headers": [
            {"key": field.key, "value": field.value, "sensitive": field.sensitive}
            for field in normalized.headers
        ],
        "content_type": normalized.content_type,
        "body_template": normalized.body_template,
        "timeout_seconds": normalized.timeout_seconds,
    }
    canonical = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mask_url(url: str) -> str:
    parsed = urlsplit(url)
    masked_query = urlencode([(key, "***") for key, _value in parse_qsl(parsed.query, keep_blank_values=True)])
    masked_path = "/***" if parsed.path and parsed.path != "/" else "/"
    return urlunsplit((parsed.scheme, parsed.netloc, masked_path, masked_query, ""))


def render_request(
    draft: HttpNotificationDraft,
    context: NotificationContext,
) -> RenderedHttpRequest:
    normalized = normalize_draft(draft)
    notification_id = context.values.get("notification.id")
    if not isinstance(notification_id, str) or not notification_id.strip():
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_TEMPLATE",
            "notification.id is required",
        )
    missing = [
        name
        for name in _TEMPLATE_VARIABLE.findall(normalized.body_template)
        if name not in context.values
    ]
    if missing:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_TEMPLATE",
            "Template context is incomplete",
        )
    if _is_json(normalized.content_type):
        rendered_body = _TEMPLATE_VARIABLE.sub(
            lambda match: json.dumps(
                context.values[match.group(1)],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            normalized.body_template,
        )
        try:
            json.loads(rendered_body)
        except json.JSONDecodeError as error:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_TEMPLATE",
                "Rendered JSON body is invalid",
            ) from error
    else:
        rendered_body = _TEMPLATE_VARIABLE.sub(
            lambda match: _text_value(context.values[match.group(1)]),
            normalized.body_template,
        )

    parsed = urlsplit(normalized.url)
    query = list(parse_qsl(parsed.query, keep_blank_values=True))
    query.extend((field.key, field.value) for field in normalized.query_params)
    url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
    )
    headers = {field.key: field.value for field in normalized.headers}
    headers.update(
        {
            "Content-Type": normalized.content_type,
            "Idempotency-Key": notification_id,
            "X-ZiZu-Notification-Id": notification_id,
        }
    )
    return RenderedHttpRequest(
        method=normalized.method,
        url=url,
        headers=headers,
        body=rendered_body.encode("utf-8"),
        timeout_seconds=normalized.timeout_seconds,
        target_display=mask_url(url),
    )


async def send_http_request(
    request: RenderedHttpRequest,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HttpSendResult:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=request.timeout_seconds,
            transport=transport,
        ) as client:
            response = await client.request(
                request.method,
                request.url,
                headers=request.headers,
                content=request.body,
            )
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        excerpt = _response_excerpt(response.text)
        if 200 <= response.status_code < 300:
            return HttpSendResult(
                True,
                "delivered",
                response.status_code,
                duration_ms,
                None,
                None,
                excerpt,
            )
        return HttpSendResult(
            False,
            "rejected",
            response.status_code,
            duration_ms,
            "HTTP_NOTIFICATION_DELIVERY_REJECTED",
            "Remote endpoint returned a non-2xx response",
            excerpt,
        )
    except httpx.TimeoutException:
        return HttpSendResult(
            False,
            "timeout",
            None,
            max(0, round((time.monotonic() - started) * 1000)),
            "HTTP_NOTIFICATION_DELIVERY_TIMEOUT",
            "Remote endpoint timed out",
            None,
        )
    except httpx.HTTPError:
        return HttpSendResult(
            False,
            "network_error",
            None,
            max(0, round((time.monotonic() - started) * 1000)),
            "HTTP_NOTIFICATION_DELIVERY_REJECTED",
            "Remote endpoint could not be reached",
            None,
        )


def public_config(config: StoredHttpNotificationConfig) -> dict[str, object]:
    tested_at = config.tested_at
    return {
        "id": str(config.id),
        "name": config.name,
        "description": config.description,
        "method": config.method,
        "url_display": config.url_display,
        "query_params": public_fields(
            config.public_query_params,
            config.secret_query_param_names,
        ),
        "headers": public_fields(config.public_headers, config.secret_header_names),
        "content_type": config.content_type,
        "body_template": config.body_template,
        "timeout_seconds": config.timeout_seconds,
        "current_digest": config.current_digest,
        "tested_digest": config.tested_digest,
        "tested_at": tested_at.isoformat() if tested_at else None,
        "last_test_status": config.last_test_status,
        "enabled": config.enabled,
    }


def public_fields(
    values: Sequence[RequestField],
    secret_names: Sequence[str],
) -> list[dict[str, object]]:
    visible = [
        {"key": item.key, "value": item.value, "sensitive": False}
        for item in values
    ]
    hidden = [
        {"key": key, "sensitive": True, "configured": True}
        for key in secret_names
    ]
    return visible + hidden


def _validated_url(value: str) -> str:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_URL",
            "HTTP notification URL is invalid",
        ) from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_URL",
            "HTTP notification URL is invalid",
        )
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in _METADATA_HOSTS:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_URL",
            "Metadata service targets are forbidden",
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and address.is_link_local:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_URL",
            "Link-local targets are forbidden",
        )
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
    )


def _normalize_fields(
    fields: Sequence[RequestField],
    *,
    header: bool,
) -> tuple[RequestField, ...]:
    result: list[RequestField] = []
    names: set[str] = set()
    for field in fields:
        key = field.key.strip()
        value = str(field.value)
        canonical = key.lower() if header else key
        if not key or canonical in names or "\r" in value or "\n" in value:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_TEMPLATE",
                "HTTP request field is invalid",
            )
        if header and (_HEADER_NAME.fullmatch(key) is None or canonical in SYSTEM_HEADERS):
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_TEMPLATE",
                "HTTP request header is invalid or reserved",
            )
        names.add(canonical)
        result.append(RequestField(key, value, bool(field.sensitive)))
    return tuple(result)


def _validate_template(template: str, content_type: str) -> None:
    variables = _TEMPLATE_VARIABLE.findall(template)
    unknown = sorted(set(variables) - ALLOWED_VARIABLES)
    if unknown or "{{" in _TEMPLATE_VARIABLE.sub("", template) or "}}" in _TEMPLATE_VARIABLE.sub("", template):
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_INVALID_TEMPLATE",
            "HTTP notification template contains an unsupported variable",
        )
    if _is_json(content_type):
        probe = _TEMPLATE_VARIABLE.sub("null", template)
        try:
            json.loads(probe)
        except json.JSONDecodeError as error:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_TEMPLATE",
                "HTTP notification JSON template is invalid",
            ) from error


def _is_json(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _text_value(value: object) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _response_excerpt(value: str) -> str:
    cleaned = _CONTROL_CHARACTER.sub("", value)
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= 4096:
        return cleaned
    return encoded[:4096].decode("utf-8", errors="ignore")


__all__ = [
    "ALLOWED_VARIABLES",
    "AlarmDeliveryRepository",
    "AlarmHttpNotificationRepository",
    "AlarmHttpNotificationDispatcher",
    "AlarmHttpNotifications",
    "DeliveryClaim",
    "HttpNotificationDraft",
    "HttpNotificationError",
    "HttpSendResult",
    "NotificationContext",
    "RenderedHttpRequest",
    "RequestField",
    "ResolvedHttpNotificationConfig",
    "SecretCodec",
    "StoredHttpNotificationConfig",
    "draft_digest",
    "mask_url",
    "normalize_draft",
    "public_config",
    "public_fields",
    "render_request",
    "send_http_request",
]
