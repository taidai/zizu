"""Deep module for safe alarm HTTP notification requests.

Alarm state transitions create delivery intent elsewhere.  This module owns
request validation, template rendering, secret encryption and HTTP result
classification; it never changes alarm state.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import ipaddress
import json
import re
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

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
    value: str
    sensitive: bool = False


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


def mask_url(url: str) -> str:
    parsed = urlsplit(url)
    masked_query = urlencode([(key, "***") for key, _value in parse_qsl(parsed.query, keep_blank_values=True)])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, masked_query, ""))


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
    "mask_url",
    "normalize_draft",
    "public_config",
    "public_fields",
    "render_request",
    "send_http_request",
]
