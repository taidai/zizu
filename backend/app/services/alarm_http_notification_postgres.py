"""PostgreSQL persistence for alarm HTTP notification configurations."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from typing import Any, Callable, Iterator, Sequence
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json, register_uuid

from app.services.alarm_http_notifications import (
    AlarmHttpNotificationDispatcher,
    AlarmHttpNotifications,
    DeliveryClaim,
    HttpNotificationDraft,
    HttpNotificationError,
    HttpSendResult,
    NotificationContext,
    RequestField,
    ResolvedHttpNotificationConfig,
    SecretCodec,
    StoredHttpNotificationConfig,
    draft_digest,
    mask_url,
    normalize_draft,
)


ConnectionFactory = Callable[[], Any]

_PUBLIC_COLUMNS = """
id,name,description,method,url_display,public_query_params,
encrypted_secret_query_params,public_headers,encrypted_secret_headers,
content_type,body_template,timeout_seconds,current_digest,tested_digest,
tested_at,last_test_status,enabled
"""

_RESOLVED_COLUMNS = """
id,name,description,method,encrypted_url,public_query_params,
encrypted_secret_query_params,public_headers,encrypted_secret_headers,
content_type,body_template,timeout_seconds,current_digest,tested_digest,enabled
"""


class PostgresAlarmHttpNotificationRepository:
    def __init__(
        self,
        connection_factory: ConnectionFactory | None = None,
        secret_codec: SecretCodec | None = None,
    ) -> None:
        register_uuid()
        self._connection_factory = connection_factory
        if secret_codec is None:
            from app.core.config import settings

            secret_codec = SecretCodec(settings.http_notification_encryption_key)
        self._secrets = secret_codec

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            if self._connection_factory is None:
                from app.services.telemetry_store import get_connection

                with get_connection() as connection:
                    try:
                        yield connection
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                return
            connection = self._connection_factory()
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except HttpNotificationError:
            raise
        except psycopg2.IntegrityError as error:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_TEMPLATE",
                "HTTP notification name or request fields conflict",
            ) from error
        except (psycopg2.Error, OSError) as error:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_PERSISTENCE_UNAVAILABLE",
                "HTTP notification storage is unavailable",
            ) from error

    def list_configs(self) -> Sequence[StoredHttpNotificationConfig]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_PUBLIC_COLUMNS} FROM t_alarm_http_notification_configs ORDER BY name,id"
            )
            rows = cursor.fetchall()
        return tuple(self._stored(row) for row in rows)

    def get_config(
        self,
        config_id: UUID,
    ) -> StoredHttpNotificationConfig | None:
        with self._connection() as connection, connection.cursor() as cursor:
            row = self._fetch_public(cursor, config_id)
        return None if row is None else self._stored(row)

    def resolve_config(
        self,
        config_id: UUID,
    ) -> ResolvedHttpNotificationConfig | None:
        with self._connection() as connection, connection.cursor() as cursor:
            row = self._fetch_resolved(cursor, config_id)
        return None if row is None else self._resolved(row)

    def create_config(
        self,
        draft: HttpNotificationDraft,
        actor: str,
    ) -> StoredHttpNotificationConfig:
        self._require_actor(actor)
        self._require_new_secrets(draft)
        normalized = normalize_draft(draft)
        digest = draft_digest(normalized)
        config_id = uuid4()
        query_public, query_secret = self._encode_fields(normalized.query_params)
        header_public, header_secret = self._encode_fields(normalized.headers)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_alarm_http_notification_configs
                  (id,name,description,method,encrypted_url,url_display,
                   public_query_params,encrypted_secret_query_params,
                   public_headers,encrypted_secret_headers,content_type,
                   body_template,timeout_seconds,current_digest,created_by,updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    config_id,
                    normalized.name,
                    normalized.description,
                    normalized.method,
                    self._secrets.encrypt(normalized.url),
                    mask_url(normalized.url),
                    Json(query_public),
                    query_secret,
                    Json(header_public),
                    header_secret,
                    normalized.content_type,
                    normalized.body_template,
                    normalized.timeout_seconds,
                    digest,
                    actor,
                    actor,
                ),
            )
            row = self._fetch_public(cursor, config_id)
        assert row is not None
        return self._stored(row)

    def update_config(
        self,
        config_id: UUID,
        draft: HttpNotificationDraft,
        actor: str,
    ) -> StoredHttpNotificationConfig:
        self._require_actor(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            current_row = self._fetch_resolved(cursor, config_id, lock=True)
            if current_row is None:
                self._not_found()
            assert current_row is not None
            current = self._resolved(current_row)
            merged = normalize_draft(self._merge_update(current.draft, draft))
            digest = draft_digest(merged)
            material_changed = digest != current.current_digest
            query_public, query_secret = self._encode_fields(merged.query_params)
            header_public, header_secret = self._encode_fields(merged.headers)
            cursor.execute(
                """
                UPDATE t_alarm_http_notification_configs
                SET name=%s,description=%s,method=%s,encrypted_url=%s,url_display=%s,
                    public_query_params=%s,encrypted_secret_query_params=%s,
                    public_headers=%s,encrypted_secret_headers=%s,content_type=%s,
                    body_template=%s,timeout_seconds=%s,current_digest=%s,
                    tested_digest=CASE WHEN %s THEN NULL ELSE tested_digest END,
                    tested_at=CASE WHEN %s THEN NULL ELSE tested_at END,
                    last_test_status=CASE WHEN %s THEN NULL ELSE last_test_status END,
                    enabled=CASE WHEN %s THEN FALSE ELSE enabled END,
                    updated_by=%s,updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (
                    merged.name,
                    merged.description,
                    merged.method,
                    self._secrets.encrypt(merged.url),
                    mask_url(merged.url),
                    Json(query_public),
                    query_secret,
                    Json(header_public),
                    header_secret,
                    merged.content_type,
                    merged.body_template,
                    merged.timeout_seconds,
                    digest,
                    material_changed,
                    material_changed,
                    material_changed,
                    material_changed,
                    actor,
                    config_id,
                ),
            )
            row = self._fetch_public(cursor, config_id)
        assert row is not None
        return self._stored(row)

    def record_test(
        self,
        config_id: UUID,
        digest: str,
        result: HttpSendResult,
        actor: str,
    ) -> StoredHttpNotificationConfig:
        self._require_actor(actor)
        status = {
            "delivered": result.delivered,
            "outcome": result.outcome,
            "http_status": result.http_status,
            "duration_ms": result.duration_ms,
            "error_code": result.error_code,
            "error_detail": result.error_detail,
            "response_excerpt": result.response_excerpt,
        }
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_digest FROM t_alarm_http_notification_configs WHERE id=%s FOR UPDATE",
                (config_id,),
            )
            row = cursor.fetchone()
            if row is None:
                self._not_found()
            if row[0].strip() != digest:
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_TEST_STALE",
                    "HTTP notification changed while its test was running",
                )
            cursor.execute(
                """
                UPDATE t_alarm_http_notification_configs
                SET tested_digest=CASE WHEN %s THEN current_digest ELSE NULL END,
                    tested_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                    last_test_status=%s,
                    enabled=CASE WHEN %s THEN enabled ELSE FALSE END,
                    updated_by=%s,updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (
                    result.delivered,
                    result.delivered,
                    Json(status),
                    result.delivered,
                    actor,
                    config_id,
                ),
            )
            updated = self._fetch_public(cursor, config_id)
        assert updated is not None
        return self._stored(updated)

    def set_enabled(
        self,
        config_id: UUID,
        enabled: bool,
        actor: str,
    ) -> StoredHttpNotificationConfig:
        self._require_actor(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT current_digest,tested_digest
                FROM t_alarm_http_notification_configs
                WHERE id=%s FOR UPDATE
                """,
                (config_id,),
            )
            row = cursor.fetchone()
            if row is None:
                self._not_found()
            current_digest = row[0].strip()
            tested_digest = row[1].strip() if row[1] else None
            if enabled and tested_digest is None:
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_NOT_TESTED",
                    "Send a successful test before enabling this configuration",
                )
            if enabled and tested_digest != current_digest:
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_TEST_STALE",
                    "Test this configuration again before enabling it",
                )
            cursor.execute(
                """
                UPDATE t_alarm_http_notification_configs
                SET enabled=%s,updated_by=%s,updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (enabled, actor, config_id),
            )
            updated = self._fetch_public(cursor, config_id)
        assert updated is not None
        return self._stored(updated)

    def delete_config(self, config_id: UUID, actor: str) -> None:
        self._require_actor(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM t_alarm_http_notification_configs WHERE id=%s FOR UPDATE",
                (config_id,),
            )
            row = cursor.fetchone()
            if row is None:
                self._not_found()
            cursor.execute(
                "DELETE FROM t_alarm_http_notification_bindings WHERE configuration_id=%s",
                (config_id,),
            )
            cursor.execute(
                """
                UPDATE t_alarm_notification_outbox
                SET status='cancelled',configuration_id=NULL,
                    configuration_name_snapshot=COALESCE(configuration_name_snapshot,%s),
                    last_error_code='HTTP_NOTIFICATION_DELIVERY_CANCELLED',
                    last_error_detail='Notification configuration was deleted',
                    lease_owner=NULL,lease_expires_at=NULL,
                    cancelled_at=clock_timestamp(),updated_at=clock_timestamp()
                WHERE configuration_id=%s
                  AND status IN ('pending','retry_wait','failed')
                """,
                (row[0], config_id),
            )
            cursor.execute(
                "DELETE FROM t_alarm_http_notification_configs WHERE id=%s",
                (config_id,),
            )

    def list_deliveries(
        self,
        *,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        if page < 1 or page_size < 1 or page_size > 200:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_PAGE",
                "Notification delivery page is invalid",
            )
        offset = (page - 1) * page_size
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_alarm_notification_outbox")
            total = int(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT delivery.id,delivery.event_id,delivery.transition_code,
                       delivery.configuration_name_snapshot,
                       delivery.context_snapshot,delivery.status,
                       delivery.attempt_count,
                       COALESCE(delivery.last_target_display,config.url_display),
                       delivery.last_http_status,delivery.last_error_code,
                       delivery.last_error_detail,delivery.last_response_excerpt,
                       delivery.created_at,delivery.delivered_at,
                       delivery.cancelled_at,(config.id IS NOT NULL)
                FROM t_alarm_notification_outbox delivery
                LEFT JOIN t_alarm_http_notification_configs config
                  ON config.id=delivery.configuration_id
                ORDER BY delivery.created_at DESC,delivery.id DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            rows = cursor.fetchall()
            notification_ids = [row[0] for row in rows]
            attempts_by_notification: dict[UUID, list[dict[str, object]]] = {
                notification_id: [] for notification_id in notification_ids
            }
            if notification_ids:
                cursor.execute(
                    """
                    SELECT notification_id,attempt_no,attempted_at,method,
                           target_display,duration_ms,outcome,http_status,
                           error_code,error_detail,response_excerpt
                    FROM t_alarm_notification_attempts
                    WHERE notification_id=ANY(%s)
                    ORDER BY notification_id,attempt_no
                    """,
                    (notification_ids,),
                )
                for attempt in cursor.fetchall():
                    attempts_by_notification[attempt[0]].append(
                        self._attempt_public(attempt)
                    )
        items = [
            self._delivery_public(row, attempts_by_notification[row[0]])
            for row in rows
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }

    def retry_delivery(
        self,
        notification_id: UUID,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self._require_actor(actor)
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_IDEMPOTENCY_KEY",
                "Manual retry needs a valid idempotency key",
            )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"alarm-http-retry:{actor}:{key}",),
            )
            cursor.execute(
                """
                SELECT notification_id,response
                FROM t_alarm_notification_retry_idempotency
                WHERE actor=%s AND idempotency_key=%s
                """,
                (actor, key),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if replay[0] != notification_id:
                    raise HttpNotificationError(
                        "HTTP_NOTIFICATION_IDEMPOTENCY_KEY_REUSED",
                        "Idempotency key was already used for another notification",
                    )
                return dict(replay[1])

            cursor.execute(
                """
                SELECT delivery.id,delivery.event_id,delivery.transition_code,
                       delivery.configuration_name_snapshot,
                       delivery.context_snapshot,delivery.status,
                       delivery.attempt_count,
                       COALESCE(delivery.last_target_display,config.url_display),
                       delivery.last_http_status,delivery.last_error_code,
                       delivery.last_error_detail,delivery.last_response_excerpt,
                       delivery.created_at,delivery.delivered_at,
                       delivery.cancelled_at,(config.id IS NOT NULL)
                FROM t_alarm_notification_outbox delivery
                LEFT JOIN t_alarm_http_notification_configs config
                  ON config.id=delivery.configuration_id
                WHERE delivery.id=%s
                FOR UPDATE OF delivery
                """,
                (notification_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_DELIVERY_NOT_FOUND",
                    "HTTP notification delivery was not found",
                )
            if not row[15]:
                self._not_found()
            if row[5] != "failed":
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_RETRY_NOT_ALLOWED",
                    "Only failed HTTP notification deliveries can be retried",
                )
            cursor.execute(
                """
                UPDATE t_alarm_notification_outbox
                SET status='pending',cycle_attempt_count=0,
                    next_attempt_at=clock_timestamp(),lease_owner=NULL,
                    lease_expires_at=NULL,delivered_at=NULL,cancelled_at=NULL,
                    updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (notification_id,),
            )
            cursor.execute(
                """
                SELECT delivery.id,delivery.event_id,delivery.transition_code,
                       delivery.configuration_name_snapshot,
                       delivery.context_snapshot,delivery.status,
                       delivery.attempt_count,
                       COALESCE(delivery.last_target_display,config.url_display),
                       delivery.last_http_status,delivery.last_error_code,
                       delivery.last_error_detail,delivery.last_response_excerpt,
                       delivery.created_at,delivery.delivered_at,
                       delivery.cancelled_at,(config.id IS NOT NULL)
                FROM t_alarm_notification_outbox delivery
                LEFT JOIN t_alarm_http_notification_configs config
                  ON config.id=delivery.configuration_id
                WHERE delivery.id=%s
                """,
                (notification_id,),
            )
            updated = cursor.fetchone()
            cursor.execute(
                """
                SELECT notification_id,attempt_no,attempted_at,method,
                       target_display,duration_ms,outcome,http_status,
                       error_code,error_detail,response_excerpt
                FROM t_alarm_notification_attempts
                WHERE notification_id=%s
                ORDER BY attempt_no
                """,
                (notification_id,),
            )
            attempts = [self._attempt_public(item) for item in cursor.fetchall()]
            response = self._delivery_public(updated, attempts)
            cursor.execute(
                """
                INSERT INTO t_alarm_notification_retry_idempotency
                  (actor,idempotency_key,notification_id,response)
                VALUES (%s,%s,%s,%s)
                """,
                (actor, key, notification_id, Json(response)),
            )
        return response

    def delete_deliveries(
        self,
        notification_ids: tuple[UUID, ...],
        actor: str,
    ) -> int:
        self._require_actor(actor)
        if (
            not 1 <= len(notification_ids) <= 200
            or len(set(notification_ids)) != len(notification_ids)
        ):
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_DELIVERY_SELECTION_INVALID",
                "Select between 1 and 200 distinct notification deliveries",
            )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,status
                FROM t_alarm_notification_outbox
                WHERE id=ANY(%s)
                FOR UPDATE
                """,
                (list(notification_ids),),
            )
            rows = cursor.fetchall()
            if len(rows) != len(notification_ids):
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_DELIVERY_NOT_FOUND",
                    "HTTP notification delivery was not found",
                )
            if any(
                row[1] not in {"delivered", "failed", "cancelled"}
                for row in rows
            ):
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_DELIVERY_NOT_TERMINAL",
                    "Only completed notification deliveries can be deleted",
                )
            cursor.execute(
                "DELETE FROM t_alarm_notification_outbox WHERE id=ANY(%s)",
                (list(notification_ids),),
            )
            return cursor.rowcount

    def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> DeliveryClaim | None:
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT delivery.id
                  FROM t_alarm_notification_outbox delivery
                  JOIN t_alarm_http_notification_configs config
                    ON config.id=delivery.configuration_id
                  WHERE delivery.status IN ('pending','retry_wait')
                    AND delivery.next_attempt_at <= %s
                    AND (
                      delivery.lease_expires_at IS NULL
                      OR delivery.lease_expires_at <= %s
                    )
                    AND config.enabled=TRUE
                    AND config.tested_digest=config.current_digest
                  ORDER BY delivery.next_attempt_at,delivery.created_at,delivery.id
                  FOR UPDATE OF delivery SKIP LOCKED
                  LIMIT 1
                )
                UPDATE t_alarm_notification_outbox delivery
                SET lease_owner=%s,lease_expires_at=%s,updated_at=%s
                FROM candidate
                WHERE delivery.id=candidate.id
                RETURNING delivery.id,delivery.transition_id,
                          delivery.transition_code,delivery.event_id,
                          delivery.configuration_id,delivery.context_snapshot,
                          delivery.attempt_count,delivery.cycle_attempt_count,
                          delivery.lease_owner
                """,
                (now, now, worker_id, lease_until, now),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return DeliveryClaim(
            id=row[0],
            transition_id=row[1],
            transition_code=row[2],
            event_id=row[3],
            configuration_id=row[4],
            context=NotificationContext(dict(row[5])),
            attempt_count=int(row[6]),
            cycle_attempt_count=int(row[7]),
            lease_owner=row[8],
        )

    def current_config(
        self,
        config_id: UUID,
    ) -> ResolvedHttpNotificationConfig | None:
        return self.resolve_config(config_id)

    def complete_attempt(
        self,
        claim: DeliveryClaim,
        result: HttpSendResult,
        now: datetime,
    ) -> None:
        method = result.method or "POST"
        target_display = result.target_display or "unavailable"
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_count,cycle_attempt_count
                FROM t_alarm_notification_outbox
                WHERE id=%s AND lease_owner=%s
                  AND status IN ('pending','retry_wait')
                FOR UPDATE
                """,
                (claim.id, claim.lease_owner),
            )
            row = cursor.fetchone()
            if row is None:
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_DELIVERY_CLAIM_LOST",
                    "HTTP notification delivery lease is no longer owned",
                )
            attempt_count = int(row[0]) + 1
            cycle_attempt_count = int(row[1]) + 1
            cursor.execute(
                """
                INSERT INTO t_alarm_notification_attempts
                  (id,notification_id,attempt_no,attempted_at,method,
                   target_display,duration_ms,outcome,http_status,error_code,
                   error_detail,response_excerpt)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    uuid4(),
                    claim.id,
                    attempt_count,
                    now,
                    method,
                    target_display,
                    result.duration_ms,
                    result.outcome,
                    result.http_status,
                    result.error_code,
                    result.error_detail,
                    result.response_excerpt,
                ),
            )
            if result.delivered:
                status = "delivered"
                next_attempt_at = now
                delivered_at = now
            elif cycle_attempt_count <= 3:
                status = "retry_wait"
                delays = (5, 30, 300)
                next_attempt_at = now + timedelta(
                    seconds=delays[cycle_attempt_count - 1]
                )
                delivered_at = None
            else:
                status = "failed"
                next_attempt_at = now
                delivered_at = None
            cursor.execute(
                """
                UPDATE t_alarm_notification_outbox
                SET status=%s,attempt_count=%s,cycle_attempt_count=%s,
                    next_attempt_at=%s,lease_owner=NULL,lease_expires_at=NULL,
                    last_target_display=%s,last_http_status=%s,
                    last_error_code=%s,last_error_detail=%s,
                    last_response_excerpt=%s,delivered_at=%s,updated_at=%s
                WHERE id=%s AND lease_owner=%s
                """,
                (
                    status,
                    attempt_count,
                    cycle_attempt_count,
                    next_attempt_at,
                    target_display,
                    result.http_status,
                    result.error_code,
                    result.error_detail,
                    result.response_excerpt,
                    delivered_at,
                    now,
                    claim.id,
                    claim.lease_owner,
                ),
            )
            if cursor.rowcount != 1:
                raise HttpNotificationError(
                    "HTTP_NOTIFICATION_DELIVERY_CLAIM_LOST",
                    "HTTP notification delivery lease is no longer owned",
                )

    def release_lease(self, notification_id: UUID, worker_id: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_alarm_notification_outbox
                SET lease_owner=NULL,lease_expires_at=NULL,
                    updated_at=clock_timestamp()
                WHERE id=%s AND lease_owner=%s
                """,
                (notification_id, worker_id),
            )

    def cancel_missing_config(
        self,
        claim: DeliveryClaim,
        now: datetime,
    ) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_alarm_notification_outbox
                SET status='cancelled',configuration_id=NULL,
                    last_error_code='HTTP_NOTIFICATION_DELIVERY_CANCELLED',
                    last_error_detail='Notification configuration is missing',
                    lease_owner=NULL,lease_expires_at=NULL,cancelled_at=%s,
                    updated_at=%s
                WHERE id=%s AND lease_owner=%s
                  AND status IN ('pending','retry_wait')
                """,
                (now, now, claim.id, claim.lease_owner),
            )

    @staticmethod
    def _iso(value: Any) -> str | None:
        return value.isoformat() if value is not None else None

    @classmethod
    def _attempt_public(cls, row: tuple[Any, ...]) -> dict[str, object]:
        return {
            "attempt_no": int(row[1]),
            "attempted_at": cls._iso(row[2]),
            "method": row[3],
            "target_display": row[4],
            "duration_ms": int(row[5]),
            "outcome": row[6],
            "http_status": row[7],
            "error_code": row[8],
            "error_detail": row[9],
            "response_excerpt": row[10],
        }

    @classmethod
    def _delivery_public(
        cls,
        row: tuple[Any, ...],
        attempts: Sequence[dict[str, object]],
    ) -> dict[str, object]:
        context = dict(row[4] or {})
        return {
            "id": str(row[0]),
            "event_id": str(row[1]),
            "event_type": row[2],
            "alarm_name": context.get("alarm.name"),
            "severity": context.get("alarm.severity"),
            "node_name": context.get("node.name"),
            "entity_name": context.get("entity.name"),
            "configuration_name": row[3],
            "configuration_exists": bool(row[15]),
            "target_display": row[7],
            "status": row[5],
            "attempt_count": int(row[6]),
            "last_http_status": row[8],
            "last_error_code": row[9],
            "last_error_detail": row[10],
            "last_response_excerpt": row[11],
            "created_at": cls._iso(row[12]),
            "delivered_at": cls._iso(row[13]),
            "cancelled_at": cls._iso(row[14]),
            "attempts": list(attempts),
        }

    @staticmethod
    def _fetch_public(cursor: Any, config_id: UUID) -> tuple[Any, ...] | None:
        cursor.execute(
            f"SELECT {_PUBLIC_COLUMNS} FROM t_alarm_http_notification_configs WHERE id=%s",
            (config_id,),
        )
        return cursor.fetchone()

    @staticmethod
    def _fetch_resolved(
        cursor: Any,
        config_id: UUID,
        *,
        lock: bool = False,
    ) -> tuple[Any, ...] | None:
        suffix = " FOR UPDATE" if lock else ""
        cursor.execute(
            f"SELECT {_RESOLVED_COLUMNS} FROM t_alarm_http_notification_configs WHERE id=%s{suffix}",
            (config_id,),
        )
        return cursor.fetchone()

    @staticmethod
    def _marker_fields(value: Any) -> tuple[tuple[RequestField, ...], tuple[str, ...]]:
        records = value or []
        public: list[RequestField] = []
        secret_names: list[str] = []
        for item in records:
            if item.get("sensitive"):
                secret_names.append(str(item["key"]))
            else:
                public.append(RequestField(str(item["key"]), str(item.get("value", ""))))
        return tuple(public), tuple(secret_names)

    def _stored(self, row: tuple[Any, ...]) -> StoredHttpNotificationConfig:
        public_query, query_names = self._marker_fields(row[5])
        public_headers, header_names = self._marker_fields(row[7])
        return StoredHttpNotificationConfig(
            id=row[0],
            name=row[1],
            description=row[2],
            method=row[3],
            url_display=row[4],
            public_query_params=public_query,
            secret_query_param_names=query_names,
            public_headers=public_headers,
            secret_header_names=header_names,
            content_type=row[9],
            body_template=row[10],
            timeout_seconds=int(row[11]),
            current_digest=row[12].strip(),
            tested_digest=row[13].strip() if row[13] else None,
            tested_at=row[14],
            last_test_status=row[15],
            enabled=bool(row[16]),
        )

    def _resolved(self, row: tuple[Any, ...]) -> ResolvedHttpNotificationConfig:
        draft = HttpNotificationDraft(
            name=row[1],
            description=row[2],
            method=row[3],
            url=self._secrets.decrypt(row[4]),
            query_params=self._decode_fields(row[5], row[6]),
            headers=self._decode_fields(row[7], row[8]),
            content_type=row[9],
            body_template=row[10],
            timeout_seconds=int(row[11]),
        )
        return ResolvedHttpNotificationConfig(
            id=row[0],
            draft=draft,
            current_digest=row[12].strip(),
            tested_digest=row[13].strip() if row[13] else None,
            enabled=bool(row[14]),
        )

    def _encode_fields(
        self,
        fields: Sequence[RequestField],
    ) -> tuple[list[dict[str, object]], str | None]:
        markers: list[dict[str, object]] = []
        secrets: dict[str, str] = {}
        for field in fields:
            if field.sensitive:
                markers.append(
                    {"key": field.key, "sensitive": True, "configured": True}
                )
                secrets[field.key] = field.value
            else:
                markers.append(
                    {"key": field.key, "value": field.value, "sensitive": False}
                )
        encrypted = None
        if secrets:
            encrypted = self._secrets.encrypt(
                json.dumps(secrets, ensure_ascii=False, separators=(",", ":"))
            )
        return markers, encrypted

    def _decode_fields(
        self,
        markers: Any,
        encrypted: str | None,
    ) -> tuple[RequestField, ...]:
        secrets: dict[str, str] = {}
        if encrypted:
            secrets = json.loads(self._secrets.decrypt(encrypted))
        result: list[RequestField] = []
        for item in markers or []:
            sensitive = bool(item.get("sensitive"))
            key = str(item["key"])
            value = secrets[key] if sensitive else str(item.get("value", ""))
            result.append(RequestField(key, value, sensitive))
        return tuple(result)

    @staticmethod
    def _merge_update(
        current: HttpNotificationDraft,
        update: HttpNotificationDraft,
    ) -> HttpNotificationDraft:
        def merge_fields(
            old_fields: Sequence[RequestField],
            new_fields: Sequence[RequestField],
        ) -> tuple[RequestField, ...]:
            old_secrets = {
                field.key.lower(): field.value
                for field in old_fields
                if field.sensitive
            }
            merged: list[RequestField] = []
            for field in new_fields:
                if field.clear:
                    continue
                value = field.value
                if field.sensitive and value == "":
                    value = old_secrets.get(field.key.lower(), "")
                    if value == "":
                        raise HttpNotificationError(
                            "HTTP_NOTIFICATION_INVALID_TEMPLATE",
                            "A new sensitive request field needs a value",
                        )
                merged.append(RequestField(field.key, value, field.sensitive))
            return tuple(merged)

        return HttpNotificationDraft(
            name=update.name,
            description=update.description,
            method=update.method,
            url=update.url.strip() or current.url,
            query_params=merge_fields(current.query_params, update.query_params),
            headers=merge_fields(current.headers, update.headers),
            content_type=update.content_type,
            body_template=update.body_template,
            timeout_seconds=update.timeout_seconds,
        )

    @staticmethod
    def _require_new_secrets(draft: HttpNotificationDraft) -> None:
        if any(
            field.clear or (field.sensitive and field.value == "")
            for field in (*draft.query_params, *draft.headers)
        ):
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_TEMPLATE",
                "Sensitive request fields need a value when created",
            )

    @staticmethod
    def _require_actor(actor: str) -> None:
        if not actor.strip():
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_INVALID_TEMPLATE",
                "HTTP notification change needs an actor",
            )

    @staticmethod
    def _not_found() -> None:
        raise HttpNotificationError(
            "HTTP_NOTIFICATION_NOT_FOUND",
            "HTTP notification configuration was not found",
        )


def build_postgres_alarm_http_notifications() -> AlarmHttpNotifications:
    return AlarmHttpNotifications(PostgresAlarmHttpNotificationRepository())


def build_postgres_alarm_http_notification_dispatcher(
) -> AlarmHttpNotificationDispatcher:
    return AlarmHttpNotificationDispatcher(
        PostgresAlarmHttpNotificationRepository()
    )


__all__ = [
    "PostgresAlarmHttpNotificationRepository",
    "build_postgres_alarm_http_notification_dispatcher",
    "build_postgres_alarm_http_notifications",
]
