"""Deliver committed L2 observations without creating another write seam."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    node_id: UUID | None = None
    unit: str | None = None
    source_path: str | None = None
    source_type: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "tag_id": str(self.tag_id),
            "observation_id": str(self.observation_id),
            "node_id": None if self.node_id is None else str(self.node_id),
            "data_type": self.value.kind.value,
            "value": _json_value(self.value.value),
            "unit": self.unit,
            "source_quality": int(self.source_quality),
            "effective_quality": int(self.effective_quality),
            "source_timestamp": _utc_iso(self.source_timestamp),
            "received_at": _utc_iso(self.received_at),
            "accepted_beat": self.accepted_beat,
            "source_path": self.source_path,
            "source_type": self.source_type,
        }

    @classmethod
    def from_public_dict(cls, payload: Mapping[str, Any]) -> "CommittedL0Change":
        return cls(
            tag_id=UUID(str(payload["tag_id"])),
            observation_id=UUID(str(payload["observation_id"])),
            value=_typed_value_from_payload(payload),
            source_quality=TrunkQuality(int(payload["source_quality"])),
            effective_quality=TrunkQuality(int(payload["effective_quality"])),
            source_timestamp=_datetime_from_payload(payload["source_timestamp"]),
            received_at=_datetime_from_payload(payload["received_at"]),
            accepted_beat=int(payload["accepted_beat"]),
            node_id=_optional_uuid(payload.get("node_id")),
            unit=_optional_string(payload.get("unit")),
            source_path=_optional_string(payload.get("source_path")),
            source_type=_optional_string(payload.get("source_type")),
        )


@dataclass(frozen=True)
class CommittedL2Change:
    entity_instance_id: UUID
    event_id: UUID
    value: TypedValue
    quality: TrunkQuality
    reason: str | None
    node_id: UUID | None = None
    unit: str | None = None
    observed_at: datetime | None = None
    received_at: datetime | None = None
    calculated_at: datetime | None = None
    processing_revision_id: UUID | None = None
    source_digest: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "entity_instance_id": str(self.entity_instance_id),
            "event_id": str(self.event_id),
            "node_id": None if self.node_id is None else str(self.node_id),
            "data_type": self.value.kind.value,
            "value": _json_value(self.value.value),
            "unit": self.unit,
            "quality": int(self.quality),
            "reason": self.reason,
            "observed_at": _optional_utc_iso(self.observed_at),
            "received_at": _optional_utc_iso(self.received_at),
            "calculated_at": _optional_utc_iso(self.calculated_at),
            "processing_revision_id": (
                None
                if self.processing_revision_id is None
                else str(self.processing_revision_id)
            ),
            "source_digest": self.source_digest,
        }

    @classmethod
    def from_public_dict(cls, payload: Mapping[str, Any]) -> "CommittedL2Change":
        return cls(
            entity_instance_id=UUID(str(payload["entity_instance_id"])),
            event_id=UUID(str(payload["event_id"])),
            value=_typed_value_from_payload(payload),
            quality=TrunkQuality(int(payload["quality"])),
            reason=_optional_string(payload.get("reason")),
            node_id=_optional_uuid(payload.get("node_id")),
            unit=_optional_string(payload.get("unit")),
            observed_at=_optional_datetime(payload.get("observed_at")),
            received_at=_optional_datetime(payload.get("received_at")),
            calculated_at=_optional_datetime(payload.get("calculated_at")),
            processing_revision_id=_optional_uuid(
                payload.get("processing_revision_id")
            ),
            source_digest=_optional_string(payload.get("source_digest")),
        )


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
    frame_time: datetime | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "type": "frame_delta",
            "frame_id": str(self.frame_id),
            "frame_sequence": self.frame_sequence,
            "status": self.status.value,
            "frame_time": _optional_utc_iso(self.frame_time),
            "configuration_revision": self.configuration_revision,
            "l0_changes": [item.public_dict() for item in self.l0_changes],
            "l2_changes": [item.public_dict() for item in self.l2_changes],
            "failure": (
                None
                if self.failure_id is None and self.failure_code is None
                else {
                    "failure_id": (
                        None if self.failure_id is None else str(self.failure_id)
                    ),
                    "code": self.failure_code,
                }
            ),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FrameOutboxEvent":
        if payload.get("type") != "frame_delta":
            raise ValueError("frame payload type is invalid")
        failure = payload.get("failure")
        if failure is not None and not isinstance(failure, Mapping):
            raise ValueError("frame failure payload is invalid")
        return cls(
            frame_id=UUID(str(payload["frame_id"])),
            frame_sequence=int(payload["frame_sequence"]),
            status=FrameStatus(str(payload["status"])),
            configuration_revision=int(payload["configuration_revision"]),
            l0_changes=tuple(
                CommittedL0Change.from_public_dict(item)
                for item in payload["l0_changes"]
            ),
            l2_changes=tuple(
                CommittedL2Change.from_public_dict(item)
                for item in payload["l2_changes"]
            ),
            failure_id=(
                None
                if failure is None
                else _optional_uuid(failure.get("failure_id"))
            ),
            failure_code=(
                None if failure is None else _optional_string(failure.get("code"))
            ),
            frame_time=_optional_datetime(payload.get("frame_time")),
        )


@dataclass(frozen=True)
class FrameOutboxClaim:
    event: FrameOutboxEvent
    claim_token: UUID


class CommittedFramePublisher(Protocol):
    async def publish(self, event: FrameOutboxEvent) -> None: ...


class CommittedFrameFanout:
    """Deliver one frame to every registered consumer in a fixed order."""

    def __init__(self, consumers: tuple[CommittedFramePublisher, ...]) -> None:
        if not consumers:
            raise ValueError("committed frame fanout requires a consumer")
        self._consumers = consumers

    async def publish(self, event: FrameOutboxEvent) -> None:
        for consumer in self._consumers:
            await consumer.publish(event)


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


ConnectionFactory = Callable[[], AbstractContextManager[Any]]


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
                        # Outbox delivery is at-least-once.  A claim lost with the
                        # process is safe to repeat after its lease expires.
                        cursor.execute("SET LOCAL synchronous_commit TO OFF")
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended('zizu:data-frame-outbox',0))"
                        )
                        cursor.execute(
                            """
                            SELECT outbox.frame_id,outbox.frame_sequence,
                                   outbox.terminal_status,
                                   frame.configuration_revision,
                                   frame.capture_beat,outbox.payload,
                                   outbox.next_attempt_at,outbox.claimed_until
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
                        if row[6] > current or (
                            row[7] is not None and row[7] > current
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
                        # Losing an acknowledgement can only cause a duplicate
                        # delivery (or an earlier retry), which is already part of
                        # the outbox contract.  The frame payload remains durable.
                        cursor.execute("SET LOCAL synchronous_commit TO OFF")
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
        del cursor
        try:
            event = FrameOutboxEvent.from_payload(frame_row[5])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataTrunkError(
                "DATA_FRAME_OUTBOX_PAYLOAD_INVALID",
                "DATA_FRAME_OUTBOX_PAYLOAD_INVALID",
            ) from exc
        expected = (
            UUID(str(frame_row[0])),
            int(frame_row[1]),
            FrameStatus(str(frame_row[2])),
            int(frame_row[3]),
        )
        actual = (
            event.frame_id,
            event.frame_sequence,
            event.status,
            event.configuration_revision,
        )
        if actual != expected:
            raise DataTrunkError(
                "DATA_FRAME_OUTBOX_PAYLOAD_MISMATCH",
                "DATA_FRAME_OUTBOX_PAYLOAD_MISMATCH",
            )
        return event

    @classmethod
    def build_event_from_history(
        cls,
        cursor,
        *,
        frame_id: UUID,
        frame_sequence: int,
        status: FrameStatus,
        configuration_revision: int,
        capture_beat: int,
        frame_time: datetime,
        failure_id: UUID | None = None,
        failure_code: str | None = None,
        previous_l0: Mapping[UUID, CommittedL0Change] | None = None,
    ) -> FrameOutboxEvent:
        if previous_l0 is None:
            current_l0 = cls._load_l0_state(cursor, frame_sequence, capture_beat)
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
                previous_l0 = cls._load_l0_state(
                    cursor, previous_sequence, previous_capture
                )
        else:
            current_l0 = cls._load_l0_latest_state(cursor, capture_beat)
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
                   observation.reason,entity.node_id,entity.unit,
                   observation.observed_at,observation.received_at,
                   observation.calculated_at,
                   observation.processing_revision_id,
                   observation.source_digest
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
                node_id=UUID(str(row[11])),
                unit=row[12],
                observed_at=row[13],
                received_at=row[14],
                calculated_at=row[15],
                processing_revision_id=UUID(str(row[16])),
                source_digest=str(row[17]).strip(),
            )
            for row in cursor.fetchall()
        )
        return FrameOutboxEvent(
            frame_id=frame_id,
            frame_sequence=frame_sequence,
            status=status,
            configuration_revision=configuration_revision,
            l0_changes=l0_changes,
            l2_changes=l2_changes,
            failure_id=failure_id,
            failure_code=failure_code,
            frame_time=frame_time,
        )

    @staticmethod
    def _load_l0_latest_state(cursor, capture_beat: int):
        cursor.execute(
            """
            SELECT latest.tag_id,latest.observation_id,tag.data_type,
                   latest.raw_value_float,latest.raw_value_int,
                   latest.raw_value_bool,latest.raw_value_text,
                   latest.quality,latest.ts,latest.event_received_at,
                   latest.accepted_beat,tag.node_id,tag.unit,
                   tag.source_path,tag.source_type
            FROM t_telemetry_latest AS latest
            JOIN t_tags AS tag ON tag.id=latest.tag_id
            WHERE latest.frame_sequence > 0
            ORDER BY latest.tag_id
            """
        )
        return PostgresFrameOutboxRepository._l0_state_from_rows(
            cursor.fetchall(), capture_beat
        )

    @staticmethod
    def _load_l0_state(cursor, frame_sequence: int, capture_beat: int):
        cursor.execute(
            """
            SELECT DISTINCT ON (telemetry.tag_id)
                   telemetry.tag_id,telemetry.observation_id,tag.data_type,
                   telemetry.raw_value_float,telemetry.raw_value_int,
                   telemetry.raw_value_bool,telemetry.raw_value_text,
                   telemetry.quality,telemetry.ts,
                   telemetry.event_received_at,telemetry.accepted_beat,
                   tag.node_id,tag.unit,tag.source_path,tag.source_type
            FROM t_telemetry AS telemetry
            JOIN t_tags AS tag ON tag.id=telemetry.tag_id
            WHERE telemetry.frame_sequence IS NOT NULL
              AND telemetry.frame_sequence <= %s
            ORDER BY telemetry.tag_id,telemetry.frame_sequence DESC,
                     telemetry.ts DESC
            """,
            (frame_sequence,),
        )
        return PostgresFrameOutboxRepository._l0_state_from_rows(
            cursor.fetchall(), capture_beat
        )

    @staticmethod
    def _l0_state_from_rows(rows, capture_beat: int):
        state = {}
        for row in rows:
            source_quality = TrunkQuality(int(row[7]))
            accepted_beat = int(row[10])
            effective_quality = (
                TrunkQuality.STALE
                if accepted_beat <= 0 or capture_beat - accepted_beat >= 3
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
                node_id=UUID(str(row[11])),
                unit=row[12],
                source_path=row[13],
                source_type=row[14],
            )
            state[change.tag_id] = change
        return state


def capture_previous_l0_state(
    cursor,
    frame_sequence: int,
) -> Mapping[UUID, CommittedL0Change]:
    """Capture the last terminal L0 state before latest advances in transaction B."""
    cursor.execute(
        """
        SELECT capture_beat
        FROM t_data_frames
        WHERE frame_sequence < %s AND status IN ('COMPLETE','FAILED')
        ORDER BY frame_sequence DESC
        LIMIT 1
        """,
        (frame_sequence,),
    )
    row = cursor.fetchone()
    if row is None:
        return {}
    return PostgresFrameOutboxRepository._load_l0_latest_state(
        cursor, int(row[0])
    )


def build_frame_outbox_event(
    cursor,
    *,
    frame_id: UUID,
    frame_sequence: int,
    status: FrameStatus,
    configuration_revision: int,
    capture_beat: int,
    frame_time: datetime,
    failure_id: UUID | None = None,
    failure_code: str | None = None,
    previous_l0: Mapping[UUID, CommittedL0Change] | None = None,
) -> FrameOutboxEvent:
    """Build the immutable delta once, inside the terminal-frame transaction."""
    return PostgresFrameOutboxRepository.build_event_from_history(
        cursor,
        frame_id=frame_id,
        frame_sequence=frame_sequence,
        status=status,
        configuration_revision=configuration_revision,
        capture_beat=capture_beat,
        frame_time=frame_time,
        failure_id=failure_id,
        failure_code=failure_code,
        previous_l0=previous_l0,
    )


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("frame payload datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_utc_iso(value: datetime | None) -> str | None:
    return None if value is None else _utc_iso(value)


def _datetime_from_payload(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("frame payload datetime is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("frame payload datetime must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _datetime_from_payload(value)


def _optional_uuid(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("frame payload string is invalid")
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def _typed_value_from_payload(payload: Mapping[str, Any]) -> TypedValue:
    kind = ValueKind(str(payload["data_type"]).upper())
    value = payload.get("value")
    if kind is ValueKind.CODE_SET and value is not None:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError("frame code-set payload is invalid")
        value = tuple(value)
    return TypedValue(kind, value)

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
