"""Deliver committed L2 observations without creating another write seam."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Callable, Protocol
from uuid import UUID, uuid4

from app.services.data_trunk_contracts import (
    DataTrunkError,
    FrameStatus,
    TrunkQuality,
    TypedValue,
    ValueKind,
)


@dataclass(frozen=True)
class CommittedL0Change:
    tag_id: UUID
    observation_id: UUID
    value: TypedValue
    source_quality: TrunkQuality
    effective_quality: TrunkQuality
    source_timestamp: datetime
    received_at: datetime
    accepted_beat: int


@dataclass(frozen=True)
class CommittedL2Change:
    entity_instance_id: UUID
    event_id: UUID
    value: TypedValue
    quality: TrunkQuality
    reason: str | None


@dataclass(frozen=True)
class FrameOutboxEvent:
    frame_id: UUID
    frame_sequence: int
    status: FrameStatus
    configuration_revision: int
    l0_changes: tuple[CommittedL0Change, ...]
    l2_changes: tuple[CommittedL2Change, ...]
    failure_id: UUID | None
    failure_code: str | None


@dataclass(frozen=True)
class FrameOutboxClaim:
    event: FrameOutboxEvent
    claim_token: UUID


class CommittedFramePublisher(Protocol):
    async def publish(self, event: FrameOutboxEvent) -> None: ...


class FrameOutboxRepository(Protocol):
    def claim_unpublished(
        self, now: datetime | None = None
    ) -> FrameOutboxClaim | None: ...

    def mark_published(self, frame_id: UUID, claim_token: UUID) -> None: ...

    def record_attempt(
        self, frame_id: UUID, claim_token: UUID, now: datetime | None = None
    ) -> None: ...


class FrameOutboxDispatcher:
    """Publish one whole terminal frame while preserving frame order."""

    def __init__(
        self,
        repository: FrameOutboxRepository,
        publisher: CommittedFramePublisher,
    ) -> None:
        self._repository = repository
        self._publisher = publisher

    async def run_once(self, *, now: datetime | None = None) -> int:
        claim = await asyncio.to_thread(self._repository.claim_unpublished, now)
        if claim is None:
            return 0
        try:
            await self._publisher.publish(claim.event)
        except Exception:
            await asyncio.to_thread(
                self._repository.record_attempt,
                claim.event.frame_id,
                claim.claim_token,
                now,
            )
            return 0
        await asyncio.to_thread(
            self._repository.mark_published,
            claim.event.frame_id,
            claim.claim_token,
        )
        return 1


class InMemoryFrameOutboxRepository:
    """Deterministic ordered repository used by contract tests."""

    def __init__(
        self,
        events: tuple[FrameOutboxEvent, ...] = (),
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._events = {
            event.frame_id: event
            for event in sorted(events, key=lambda item: item.frame_sequence)
        }
        self._clock = clock or (lambda: datetime.now(UTC))
        self._claims: dict[UUID, UUID] = {}
        self._published: list[UUID] = []
        self.attempts: dict[UUID, int] = {
            event.frame_id: 0 for event in events
        }
        self.next_attempt_at: dict[UUID, datetime] = {
            event.frame_id: datetime.min.replace(tzinfo=UTC) for event in events
        }

    @property
    def published_ids(self) -> tuple[UUID, ...]:
        return tuple(self._published)

    def claim_unpublished(
        self, now: datetime | None = None
    ) -> FrameOutboxClaim | None:
        current = now or self._clock()
        remaining = (
            event
            for event in self._events.values()
            if event.frame_id not in self._published
        )
        head = next(remaining, None)
        if head is None or self.next_attempt_at[head.frame_id] > current:
            return None
        if head.frame_id in self._claims:
            return None
        token = uuid4()
        self._claims[head.frame_id] = token
        return FrameOutboxClaim(head, token)

    def mark_published(self, frame_id: UUID, claim_token: UUID) -> None:
        self._verify_claim(frame_id, claim_token)
        self._claims.pop(frame_id)
        self._published.append(frame_id)

    def record_attempt(
        self,
        frame_id: UUID,
        claim_token: UUID,
        now: datetime | None = None,
    ) -> None:
        self._verify_claim(frame_id, claim_token)
        self._claims.pop(frame_id)
        self.attempts[frame_id] += 1
        delay = min(60, 2 ** self.attempts[frame_id])
        self.next_attempt_at[frame_id] = (now or self._clock()) + timedelta(
            seconds=delay
        )

    def _verify_claim(self, frame_id: UUID, claim_token: UUID) -> None:
        if self._claims.get(frame_id) != claim_token:
            raise DataTrunkError(
                "DATA_FRAME_OUTBOX_CLAIM_LOST",
                "DATA_FRAME_OUTBOX_CLAIM_LOST",
            )


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


@dataclass(frozen=True)
class _Subscription:
    entity_instance_ids: frozenset[UUID]


class EntityObservationBroadcaster:
    """Keep per-socket L2 entity subscriptions and publish commit evidence."""

    def __init__(self) -> None:
        self._subscriptions: dict[Any, _Subscription | None] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: Any) -> None:
        async with self._lock:
            self._subscriptions[websocket] = None

    async def disconnect(self, websocket: Any) -> None:
        async with self._lock:
            self._subscriptions.pop(websocket, None)

    async def subscribe(
        self,
        websocket: Any,
        entity_instance_ids: tuple[UUID, ...],
    ) -> None:
        async with self._lock:
            if websocket in self._subscriptions:
                self._subscriptions[websocket] = _Subscription(
                    frozenset(entity_instance_ids)
                )

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
        for websocket, _subscription in subscribers:
            try:
                await websocket.send_json(payload)
            except Exception:
                failed.append(websocket)
                continue
        if failed:
            async with self._lock:
                for websocket in failed:
                    self._subscriptions.pop(websocket, None)


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


class PostgresFrameOutboxRepository:
    """Claim and reconstruct one terminal frame without skipping its head."""

    def __init__(
        self,
        *,
        worker_id: UUID | None = None,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._worker_id = worker_id or uuid4()
        self._connection = connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def claim_unpublished(
        self, now: datetime | None = None
    ) -> FrameOutboxClaim | None:
        current = now or self._clock()
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended('zizu:data-frame-outbox',0))"
                        )
                        cursor.execute(
                            """
                            SELECT outbox.frame_id,outbox.frame_sequence,
                                   outbox.terminal_status,
                                   frame.configuration_revision,
                                   frame.capture_beat,outbox.next_attempt_at,
                                   outbox.claimed_until
                            FROM t_data_frame_outbox AS outbox
                            JOIN t_data_frames AS frame
                              ON frame.frame_id=outbox.frame_id
                            WHERE outbox.published_at IS NULL
                            ORDER BY outbox.frame_sequence
                            LIMIT 1
                            FOR UPDATE OF outbox
                            """
                        )
                        row = cursor.fetchone()
                        if row is None:
                            connection.commit()
                            return None
                        if row[5] > current or (
                            row[6] is not None and row[6] > current
                        ):
                            connection.commit()
                            return None
                        token = uuid4()
                        cursor.execute(
                            """
                            UPDATE t_data_frame_outbox
                            SET claimed_by=%s,claim_token=%s,
                                claimed_until=%s + interval '30 seconds'
                            WHERE frame_id=%s AND published_at IS NULL
                            """,
                            (
                                str(self._worker_id),
                                str(token),
                                current,
                                str(row[0]),
                            ),
                        )
                        event = self._load_event(cursor, row)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_OUTBOX_UNAVAILABLE",
                "DATA_FRAME_OUTBOX_UNAVAILABLE",
            ) from exc
        return FrameOutboxClaim(event=event, claim_token=token)

    def mark_published(self, frame_id: UUID, claim_token: UUID) -> None:
        self._finish(
            frame_id,
            claim_token,
            """
            UPDATE t_data_frame_outbox
            SET published_at=clock_timestamp(),claimed_by=NULL,
                claim_token=NULL,claimed_until=NULL
            WHERE frame_id=%s AND claimed_by=%s AND claim_token=%s
              AND published_at IS NULL
            RETURNING frame_id
            """,
            (str(frame_id), str(self._worker_id), str(claim_token)),
        )

    def record_attempt(
        self,
        frame_id: UUID,
        claim_token: UUID,
        now: datetime | None = None,
    ) -> None:
        current = now or self._clock()
        self._finish(
            frame_id,
            claim_token,
            """
            UPDATE t_data_frame_outbox
            SET attempts=attempts+1,
                next_attempt_at=%s + make_interval(
                  secs=>LEAST(60,power(2,attempts+1)::integer)
                ),
                claimed_by=NULL,claim_token=NULL,claimed_until=NULL
            WHERE frame_id=%s AND claimed_by=%s AND claim_token=%s
              AND published_at IS NULL
            RETURNING frame_id
            """,
            (
                current,
                str(frame_id),
                str(self._worker_id),
                str(claim_token),
            ),
        )

    def _finish(
        self,
        frame_id: UUID,
        claim_token: UUID,
        statement: str,
        parameters: tuple[object, ...],
    ) -> None:
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(statement, parameters)
                        if cursor.fetchone() is None:
                            raise DataTrunkError(
                                "DATA_FRAME_OUTBOX_CLAIM_LOST",
                                "DATA_FRAME_OUTBOX_CLAIM_LOST",
                            )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_OUTBOX_UNAVAILABLE",
                "DATA_FRAME_OUTBOX_UNAVAILABLE",
            ) from exc

    def _load_event(self, cursor, frame_row) -> FrameOutboxEvent:
        frame_id = UUID(str(frame_row[0]))
        frame_sequence = int(frame_row[1])
        status = FrameStatus(str(frame_row[2]))
        configuration_revision = int(frame_row[3])
        capture_beat = int(frame_row[4])
        current_l0 = self._load_l0_state(cursor, frame_sequence, capture_beat)
        cursor.execute(
            """
            SELECT max(frame_sequence)
            FROM t_data_frames
            WHERE frame_sequence < %s AND status IN ('COMPLETE','FAILED')
            """,
            (frame_sequence,),
        )
        previous_row = cursor.fetchone()
        previous_l0 = {}
        if previous_row is not None and previous_row[0] is not None:
            previous_sequence = int(previous_row[0])
            cursor.execute(
                "SELECT capture_beat FROM t_data_frames WHERE frame_sequence=%s",
                (previous_sequence,),
            )
            previous_capture = int(cursor.fetchone()[0])
            previous_l0 = self._load_l0_state(
                cursor, previous_sequence, previous_capture
            )
        l0_changes = tuple(
            change
            for tag_id, change in sorted(current_l0.items(), key=lambda item: str(item[0]))
            if previous_l0.get(tag_id) != change
        )
        cursor.execute(
            """
            SELECT observation.entity_instance_id,observation.event_id,
                   entity.data_type,observation.value_float,
                   observation.value_int,observation.value_numeric,
                   observation.value_bool,observation.value_text,
                   observation.value_codes,observation.quality,
                   observation.reason
            FROM t_l2_observations AS observation
            JOIN t_entity_instances AS entity
              ON entity.id=observation.entity_instance_id
            WHERE observation.frame_id=%s
            ORDER BY observation.entity_instance_id
            """,
            (str(frame_id),),
        )
        l2_changes = tuple(
            CommittedL2Change(
                entity_instance_id=UUID(str(row[0])),
                event_id=UUID(str(row[1])),
                value=_typed_value_from_columns(str(row[2]), *row[3:9]),
                quality=TrunkQuality(int(row[9])),
                reason=row[10],
            )
            for row in cursor.fetchall()
        )
        cursor.execute(
            "SELECT id FROM t_ingestion_failures WHERE frame_id=%s",
            (str(frame_id),),
        )
        failure_row = cursor.fetchone()
        return FrameOutboxEvent(
            frame_id=frame_id,
            frame_sequence=frame_sequence,
            status=status,
            configuration_revision=configuration_revision,
            l0_changes=l0_changes,
            l2_changes=l2_changes,
            failure_id=(
                None if failure_row is None else UUID(str(failure_row[0]))
            ),
            failure_code=(None if status is FrameStatus.COMPLETE else self._failure_code(cursor, frame_id)),
        )

    @staticmethod
    def _failure_code(cursor, frame_id: UUID) -> str | None:
        cursor.execute(
            "SELECT failure_code FROM t_data_frames WHERE frame_id=%s",
            (str(frame_id),),
        )
        row = cursor.fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _load_l0_state(cursor, frame_sequence: int, capture_beat: int):
        cursor.execute(
            """
            SELECT DISTINCT ON (telemetry.tag_id)
                   telemetry.tag_id,telemetry.observation_id,tag.data_type,
                   telemetry.raw_value_float,telemetry.raw_value_int,
                   telemetry.raw_value_bool,telemetry.raw_value_text,
                   telemetry.quality,telemetry.ts,
                   telemetry.event_received_at,telemetry.accepted_beat
            FROM t_telemetry AS telemetry
            JOIN t_tags AS tag ON tag.id=telemetry.tag_id
            WHERE telemetry.frame_sequence IS NOT NULL
              AND telemetry.frame_sequence <= %s
            ORDER BY telemetry.tag_id,telemetry.frame_sequence DESC,
                     telemetry.ts DESC
            """,
            (frame_sequence,),
        )
        state = {}
        for row in cursor.fetchall():
            source_quality = TrunkQuality(int(row[7]))
            accepted_beat = int(row[10])
            effective_quality = (
                TrunkQuality.STALE
                if capture_beat - accepted_beat >= 3
                else source_quality
            )
            change = CommittedL0Change(
                tag_id=UUID(str(row[0])),
                observation_id=UUID(str(row[1])),
                value=_typed_value_from_columns(
                    str(row[2]), row[3], row[4], None, row[5], row[6], None
                ),
                source_quality=source_quality,
                effective_quality=effective_quality,
                source_timestamp=row[8],
                received_at=row[9],
                accepted_beat=accepted_beat,
            )
            state[change.tag_id] = change
        return state


def _typed_value_from_columns(
    data_type: str,
    value_float,
    value_int,
    value_numeric,
    value_bool,
    value_text,
    value_codes,
) -> TypedValue:
    kind = ValueKind(data_type.upper())
    if kind is ValueKind.FLOAT:
        value = value_numeric if value_numeric is not None else value_float
    elif kind is ValueKind.INT:
        value = value_numeric if value_numeric is not None else value_int
    elif kind is ValueKind.BOOL:
        value = value_bool
    elif kind in {ValueKind.STRING, ValueKind.ENUM}:
        value = value_text
    else:
        value = None if value_codes is None else tuple(value_codes)
    return TypedValue(kind, value)


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
            "configuration_revision": payload.get(
                "configuration_revision"
            ),
            "source_summary": {
                "digest": payload.get("source_digest"),
            },
        }
    )
