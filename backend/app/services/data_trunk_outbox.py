"""Deliver committed L2 observations without creating another write seam."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Callable, Protocol
from uuid import UUID, uuid4
import secrets

from app.services.data_trunk_contracts import DataTrunkError
from app.services.runtime_identity import RUNTIME_INSTANCE_ID


@dataclass(frozen=True)
class OutboxEvent:
    event_id: UUID
    entity_instance_id: UUID
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def public_dict(self) -> dict[str, Any]:
        return {
            "type": "entity_observation",
            "event_id": str(self.event_id),
            "entity_instance_id": str(self.entity_instance_id),
            **dict(self.payload),
        }


class AcceptanceReceiptRecorder(Protocol):
    def record_acknowledgement(
        self,
        binding: Any,
        event: OutboxEvent,
        runtime_instance_id: UUID,
    ) -> None: ...


@dataclass(frozen=True)
class _Subscription:
    entity_instance_ids: frozenset[UUID]
    acceptance_binding: Any | None = None


@dataclass(frozen=True)
class _PendingAcceptanceEvent:
    event: OutboxEvent
    nonce: str
    sent: bool


class EntityObservationBroadcaster:
    """Keep per-socket L2 entity subscriptions and publish commit evidence."""

    def __init__(
        self,
        *,
        receipt_recorder: AcceptanceReceiptRecorder | None = None,
        runtime_instance_id: UUID = RUNTIME_INSTANCE_ID,
    ) -> None:
        self._subscriptions: dict[Any, _Subscription | None] = {}
        self._pending_acceptance_events: dict[
            Any, dict[UUID, _PendingAcceptanceEvent]
        ] = {}
        self._lock = asyncio.Lock()
        self._receipt_recorder = receipt_recorder
        self._runtime_instance_id = runtime_instance_id

    async def connect(self, websocket: Any) -> None:
        async with self._lock:
            self._subscriptions[websocket] = None
            self._pending_acceptance_events[websocket] = {}

    async def disconnect(self, websocket: Any) -> None:
        async with self._lock:
            self._subscriptions.pop(websocket, None)
            self._pending_acceptance_events.pop(websocket, None)

    async def subscribe(
        self,
        websocket: Any,
        entity_instance_ids: tuple[UUID, ...],
        *,
        acceptance_binding: Any | None = None,
    ) -> None:
        async with self._lock:
            if websocket in self._subscriptions:
                self._subscriptions[websocket] = _Subscription(
                    frozenset(entity_instance_ids),
                    acceptance_binding,
                )
                self._pending_acceptance_events[websocket] = {}

    async def acknowledge(
        self,
        websocket: Any,
        event_id: UUID,
        nonce: str,
        application_id: UUID | None = None,
    ) -> None:
        """Persist evidence only after this authenticated socket ACKs a sent event."""
        async with self._lock:
            subscription = self._subscriptions.get(websocket)
            pending = self._pending_acceptance_events.get(websocket, {})
            pending_event = pending.get(event_id)
        if (
            subscription is None
            or subscription.acceptance_binding is None
            or pending_event is None
            or not pending_event.sent
            or not secrets.compare_digest(pending_event.nonce, nonce)
            or self._receipt_recorder is None
            or (
                application_id is not None
                and getattr(
                    subscription.acceptance_binding,
                    "application_id",
                    application_id,
                ) != application_id
            )
        ):
            raise ValueError("ACK_EVENT_NOT_PENDING")
        await asyncio.to_thread(
            self._receipt_recorder.record_acknowledgement,
            subscription.acceptance_binding,
            pending_event.event,
            self._runtime_instance_id,
        )
        async with self._lock:
            self._pending_acceptance_events.get(websocket, {}).pop(event_id, None)

    async def publish(self, event: OutboxEvent) -> None:
        async with self._lock:
            subscribers = tuple(
                (websocket, subscription)
                for websocket, subscription in self._subscriptions.items()
                if subscription is not None
                and event.entity_instance_id in subscription.entity_instance_ids
            )
        payload = event.public_dict()
        failed = []
        for websocket, subscription in subscribers:
            socket_payload = payload
            nonce = None
            if subscription.acceptance_binding is not None:
                nonce = secrets.token_urlsafe(32)
                socket_payload = {**payload, "acceptance_ack_nonce": nonce}
                async with self._lock:
                    pending = self._pending_acceptance_events.get(websocket)
                    if pending is not None:
                        pending[event.event_id] = _PendingAcceptanceEvent(
                            event,
                            nonce,
                            False,
                        )
                        while len(pending) > 1000:
                            pending.pop(next(iter(pending)))
            try:
                await websocket.send_json(socket_payload)
            except Exception:
                failed.append(websocket)
                continue
            if nonce is not None:
                async with self._lock:
                    pending = self._pending_acceptance_events.get(websocket)
                    current = pending.get(event.event_id) if pending else None
                    if current is not None and current.nonce == nonce:
                        pending[event.event_id] = _PendingAcceptanceEvent(
                            event,
                            nonce,
                            True,
                        )
        if failed:
            async with self._lock:
                for websocket in failed:
                    self._subscriptions.pop(websocket, None)
                    self._pending_acceptance_events.pop(websocket, None)


class OutboxRepository(Protocol):
    def claim_unpublished(self, limit: int) -> tuple[OutboxEvent, ...]: ...

    def record_attempt(self, event_id: UUID) -> None: ...

    def mark_published(self, event_id: UUID) -> None: ...


class OutboxPublisher(Protocol):
    async def publish(self, event: OutboxEvent) -> None: ...


class OutboxDispatcher:
    def __init__(
        self,
        repository: OutboxRepository,
        broadcaster: OutboxPublisher,
    ) -> None:
        self._repository = repository
        self._broadcaster = broadcaster

    async def run_once(self, limit: int = 200) -> int:
        claimed = await asyncio.to_thread(
            self._repository.claim_unpublished,
            limit,
        )
        published = 0
        for event in claimed:
            try:
                await self._broadcaster.publish(event)
            except Exception:
                await asyncio.to_thread(
                    self._repository.record_attempt,
                    event.event_id,
                )
            else:
                await asyncio.to_thread(
                    self._repository.mark_published,
                    event.event_id,
                )
                published += 1
        return published


ConnectionFactory = Callable[[], AbstractContextManager[Any]]


class PostgresOutboxRepository:
    """Lease committed outbox rows and acknowledge them after publication."""

    def __init__(
        self,
        *,
        worker_id: UUID | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._worker_id = worker_id or uuid4()
        self._connection = connection_factory

    def claim_unpublished(self, limit: int) -> tuple[OutboxEvent, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("outbox claim limit must be between 1 and 1000")
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            WITH candidates AS (
                              SELECT event_id
                              FROM t_l2_stream_outbox
                              WHERE published_at IS NULL
                                AND next_attempt_at <= now()
                                AND (
                                  claimed_until IS NULL
                                  OR claimed_until <= now()
                                )
                              ORDER BY created_at, event_id
                              FOR UPDATE SKIP LOCKED
                              LIMIT %s
                            )
                            UPDATE t_l2_stream_outbox AS outbox
                            SET claimed_by = %s,
                                claimed_until = now() + INTERVAL '30 seconds'
                            FROM candidates
                            WHERE outbox.event_id = candidates.event_id
                            RETURNING outbox.event_id,
                                      outbox.entity_instance_id,
                                      outbox.payload
                            """,
                            (limit, str(self._worker_id)),
                        )
                        rows = cursor.fetchall()
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_TRUNK_OUTBOX_UNAVAILABLE",
                "Committed observation outbox is unavailable",
            ) from exc
        return tuple(
            OutboxEvent(
                event_id=UUID(str(event_id)),
                entity_instance_id=UUID(str(entity_instance_id)),
                payload=_public_payload(payload),
            )
            for event_id, entity_instance_id, payload in rows
        )

    def mark_published(self, event_id: UUID) -> None:
        self._finish_claim(
            event_id,
            """
            UPDATE t_l2_stream_outbox
            SET published_at = now(), claimed_by = NULL, claimed_until = NULL
            WHERE event_id = %s AND claimed_by = %s AND published_at IS NULL
            RETURNING event_id
            """,
        )

    def record_attempt(self, event_id: UUID) -> None:
        self._finish_claim(
            event_id,
            """
            UPDATE t_l2_stream_outbox
            SET attempts = attempts + 1,
                next_attempt_at = now() + make_interval(
                  secs => LEAST(60, power(2, attempts + 1)::INTEGER)
                ),
                claimed_by = NULL,
                claimed_until = NULL
            WHERE event_id = %s AND claimed_by = %s AND published_at IS NULL
            RETURNING event_id
            """,
        )

    def _finish_claim(self, event_id: UUID, statement: str) -> None:
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            statement,
                            (str(event_id), str(self._worker_id)),
                        )
                        if cursor.fetchone() is None:
                            raise DataTrunkError(
                                "DATA_TRUNK_OUTBOX_CLAIM_LOST",
                                "Committed observation outbox claim was lost",
                            )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_TRUNK_OUTBOX_UNAVAILABLE",
                "Committed observation outbox is unavailable",
            ) from exc


def _public_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    observed_at = payload.get("observed_at")
    age_ms = None
    if isinstance(observed_at, str):
        try:
            parsed = datetime.fromisoformat(observed_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            age_ms = max(
                0,
                round(
                    (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()
                    * 1000
                ),
            )
        except ValueError:
            age_ms = None
    return MappingProxyType(
        {
            "definition_id": payload.get("definition_id"),
            "value": payload.get("value"),
            "data_type": payload.get("value_kind"),
            "unit": payload.get("unit"),
            "quality": payload.get("quality"),
            "reason": payload.get("reason"),
            "observed_at": observed_at,
            "received_at": payload.get("received_at"),
            "calculated_at": payload.get("calculated_at"),
            "age_ms": age_ms,
            "processing_revision_id": payload.get("processing_revision_id"),
            "site_configuration_version": payload.get(
                "site_configuration_version"
            ),
            "source_summary": {
                "digest": payload.get("source_digest"),
            },
        }
    )
