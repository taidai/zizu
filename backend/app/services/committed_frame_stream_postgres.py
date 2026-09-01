"""PostgreSQL adapter for one-cut snapshots and bounded frame replay."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.services.committed_frame_stream import (
    FrameDelta,
    FrameScope,
    FrameSnapshot,
    FrameStreamError,
    ReplayWindow,
)
from app.services.data_trunk_contracts import TrunkQuality, ValueKind
from app.services.data_trunk_freshness import effective_l0_quality
from app.services.data_trunk_outbox import FrameOutboxEvent


ConnectionFactory = Callable[[], AbstractContextManager[Any]]


@contextmanager
def _snapshot_cursor(connection):
    """Keep repeatable-read/read-only settings inside one transaction only."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise


class PostgresCommittedFrameStreamRepository:
    """Read a node projection without exposing its consistency machinery."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        on_snapshot_head_read: Callable[[], None] | None = None,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory
        self._on_snapshot_head_read = on_snapshot_head_read

    def read_snapshot(self, scope: FrameScope) -> FrameSnapshot:
        try:
            with self._connection() as connection, _snapshot_cursor(connection) as cursor:
                self._require_active_node(cursor, scope.node_id)
                cursor.execute(
                    """
                    SELECT frame_sequence,COALESCE(finished_at,shot_at),
                           configuration_revision,capture_beat,CURRENT_TIMESTAMP,
                           status,failure_code
                    FROM t_data_frames
                    WHERE status IN ('COMPLETE','FAILED')
                    ORDER BY frame_sequence DESC
                    LIMIT 1
                    """
                )
                head = cursor.fetchone()
                if head is None:
                    cursor.execute(
                        "SELECT current_revision FROM t_configuration_state "
                        "WHERE singleton=TRUE"
                    )
                    revision_row = cursor.fetchone()
                    frame_sequence = 0
                    frame_time = None
                    capture_beat = 0
                    snapshot_at = datetime.now(UTC)
                    frame_status = None
                    failure = None
                    configuration_revision = (
                        0 if revision_row is None else int(revision_row[0])
                    )
                else:
                    frame_sequence = int(head[0])
                    frame_time = _optional_iso(head[1])
                    configuration_revision = int(head[2])
                    capture_beat = int(head[3])
                    snapshot_at = head[4]
                    frame_status = str(head[5])
                    failure = (
                        None if head[6] is None else {"code": str(head[6])}
                    )
                cursor.execute(
                    "SELECT count(*) FROM t_data_frames "
                    "WHERE status IN ('PENDING','PROCESSING')"
                )
                backlog_row = cursor.fetchone()
                backlog_frames = 0 if backlog_row is None else int(backlog_row[0])
                if self._on_snapshot_head_read is not None:
                    self._on_snapshot_head_read()
                l0 = self._read_l0(
                    cursor,
                    scope.node_id,
                    frame_sequence,
                    capture_beat,
                    snapshot_at,
                )
                l2 = self._read_l2(
                    cursor,
                    scope.node_id,
                    frame_sequence,
                    snapshot_at,
                )
        except FrameStreamError:
            raise
        except Exception as exc:
            raise FrameStreamError("FRAME_SNAPSHOT_UNAVAILABLE") from exc
        return FrameSnapshot(
            node_id=scope.node_id,
            cursor="",
            frame_sequence=frame_sequence,
            frame_time=frame_time,
            configuration_revision=configuration_revision,
            l0=l0,
            l2=l2,
            frame_status=frame_status,
            failure=failure,
            backlog_frames=backlog_frames,
        )

    def replay_window(self) -> ReplayWindow:
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT min(frame_sequence),max(frame_sequence) "
                    "FROM t_data_frame_outbox WHERE published_at IS NOT NULL"
                )
                oldest, latest = cursor.fetchone()
                connection.commit()
        except Exception as exc:
            raise FrameStreamError("FRAME_REPLAY_UNAVAILABLE") from exc
        return ReplayWindow(
            oldest_sequence=None if oldest is None else int(oldest),
            latest_sequence=0 if latest is None else int(latest),
        )

    def replay_after(
        self,
        sequence: int,
        high_watermark: int,
        scope: FrameScope,
    ) -> tuple[FrameDelta, ...]:
        if (
            sequence < 0
            or high_watermark < sequence
            or high_watermark - sequence > 5000
        ):
            raise FrameStreamError("FRAME_CURSOR_TOO_OLD")
        try:
            with self._connection() as connection, _snapshot_cursor(connection) as cursor:
                self._require_active_node(cursor, scope.node_id)
                tag_ids, entity_ids = self._scope_ids(cursor, scope.node_id)
                cursor.execute(
                    """
                    SELECT payload
                    FROM t_data_frame_outbox
                    WHERE published_at IS NOT NULL
                      AND frame_sequence > %s
                      AND frame_sequence <= %s
                    ORDER BY frame_sequence
                    """,
                    (sequence, high_watermark),
                )
                payloads = tuple(row[0] for row in cursor.fetchall())
        except FrameStreamError:
            raise
        except Exception as exc:
            raise FrameStreamError("FRAME_REPLAY_UNAVAILABLE") from exc
        deltas = []
        for payload in payloads:
            try:
                event = FrameOutboxEvent.from_payload(payload)
            except (KeyError, TypeError, ValueError) as exc:
                raise FrameStreamError("FRAME_PAYLOAD_INVALID") from exc
            deltas.append(
                self._project_event(
                    event,
                    scope,
                    tag_ids=tag_ids,
                    entity_ids=entity_ids,
                )
            )
        return tuple(deltas)

    def project_event(self, event: FrameOutboxEvent, scope: FrameScope) -> FrameDelta:
        return self._project_event(event, scope)

    @staticmethod
    def _project_event(
        event: FrameOutboxEvent,
        scope: FrameScope,
        *,
        tag_ids: frozenset[UUID] | None = None,
        entity_ids: frozenset[UUID] | None = None,
    ) -> FrameDelta:
        l0 = tuple(
            item.public_dict()
            for item in event.l0_changes
            if item.node_id == scope.node_id
            and (tag_ids is None or item.tag_id in tag_ids)
        )
        l2 = tuple(
            item.public_dict()
            for item in event.l2_changes
            if item.node_id == scope.node_id
            and (entity_ids is None or item.entity_instance_id in entity_ids)
        )
        if event.frame_time is None:
            raise FrameStreamError("FRAME_PAYLOAD_INVALID")
        failure = (
            None
            if event.failure_id is None and event.failure_code is None
            else {
                "failure_id": (
                    None if event.failure_id is None else str(event.failure_id)
                ),
                "code": event.failure_code,
            }
        )
        return FrameDelta(
            node_id=scope.node_id,
            cursor="",
            frame_id=event.frame_id,
            frame_sequence=event.frame_sequence,
            status=event.status.value,
            frame_time=_iso(event.frame_time),
            configuration_revision=event.configuration_revision,
            l0_changes=l0,
            l2_changes=l2,
            failure=failure,
        )

    @staticmethod
    def _require_active_node(cursor, node_id: UUID) -> None:
        cursor.execute(
            "SELECT 1 FROM t_nodes WHERE id=%s AND enabled=TRUE",
            (str(node_id),),
        )
        if cursor.fetchone() is None:
            raise FrameStreamError("FRAME_SCOPE_NOT_FOUND")

    @staticmethod
    def _scope_ids(cursor, node_id: UUID):
        cursor.execute(
            "SELECT id FROM t_tags WHERE node_id=%s AND enabled=TRUE",
            (str(node_id),),
        )
        tag_ids = frozenset(UUID(str(row[0])) for row in cursor.fetchall())
        cursor.execute(
            "SELECT id FROM t_entity_instances WHERE node_id=%s AND active=TRUE",
            (str(node_id),),
        )
        entity_ids = frozenset(UUID(str(row[0])) for row in cursor.fetchall())
        return tag_ids, entity_ids

    @staticmethod
    def _read_l0(
        cursor,
        node_id: UUID,
        frame_sequence: int,
        capture_beat: int,
        snapshot_at: datetime,
    ):
        cursor.execute(
            """
            SELECT tag.id,tag.name,tag.display_name,tag.data_type,tag.unit,
                   tag.source_path,tag.source_type,
                   latest.observation_id,latest.raw_value_float,
                   latest.raw_value_int,latest.raw_value_bool,
                   latest.raw_value_text,latest.quality,latest.ts,
                   latest.event_received_at,latest.source_digest,
                   latest.frame_sequence,history.quality,history.accepted_beat,
                   latest.value_float,latest.value_int,
                   latest.value_bool,latest.value_str
            FROM t_tags AS tag
            LEFT JOIN t_telemetry_latest AS latest
              ON latest.tag_id=tag.id AND latest.node_id=tag.node_id
             AND latest.frame_sequence <= %s
            LEFT JOIN LATERAL (
              SELECT telemetry.quality,telemetry.accepted_beat
              FROM t_telemetry AS telemetry
              WHERE telemetry.observation_id=latest.observation_id
              ORDER BY telemetry.ts DESC LIMIT 1
            ) AS history ON TRUE
            WHERE tag.node_id=%s AND tag.enabled=TRUE
            ORDER BY tag.name,tag.id
            """,
            (frame_sequence, str(node_id)),
        )
        items = []
        for row in cursor.fetchall():
            frame_sequence_value = 0 if row[16] is None else int(row[16])
            legacy_has_value = frame_sequence_value == 0 and any(
                value is not None for value in row[19:23]
            )
            has_value = row[7] is not None or legacy_has_value
            effective_quality = effective_l0_quality(
                frame_sequence_value,
                has_value=has_value,
                stored_quality=row[12],
                capture_beat=capture_beat,
                accepted_beat=row[18],
                received_at=row[14],
                evaluated_at=snapshot_at,
            )
            source_quality = (
                int(TrunkQuality.STALE)
                if not has_value
                else int(row[17] if row[17] is not None else row[12])
            )
            value = (
                None
                if not has_value
                else _l0_snapshot_value(
                    str(row[3]),
                    frame_sequence_value,
                    raw_float=row[8],
                    raw_int=row[9],
                    raw_bool=row[10],
                    raw_text=row[11],
                    legacy_float=row[19],
                    legacy_int=row[20],
                    legacy_bool=row[21],
                    legacy_text=row[22],
                )
            )
            if not has_value:
                reason = "WAITING_DATA"
            elif effective_quality == int(TrunkQuality.STALE):
                reason = "STALE"
            elif effective_quality not in {
                int(TrunkQuality.GOOD),
                int(TrunkQuality.UNCERTAIN),
            }:
                reason = "SOURCE_QUALITY_BAD"
            else:
                reason = None
            items.append(
                {
                    "tag_id": str(row[0]),
                    "node_id": str(node_id),
                    "name": row[1],
                    "display_name": row[2] or row[1],
                    "data_type": str(row[3]),
                    "value": value,
                    "unit": row[4],
                    "source_quality": source_quality,
                    "effective_quality": effective_quality,
                    "reason": reason,
                    "source_timestamp": _optional_iso(row[13]),
                    "received_at": _optional_iso(row[14]),
                    "accepted_beat": row[18],
                    "source_path": row[5],
                    "source_type": row[6],
                    "source_digest": (
                        None if row[15] is None else str(row[15]).strip()
                    ),
                    "frame_sequence": frame_sequence_value,
                }
            )
        return tuple(items)

    @staticmethod
    def _read_l2(
        cursor,
        node_id: UUID,
        frame_sequence: int,
        snapshot_at: datetime,
    ):
        cursor.execute(
            """
            SELECT entity.id,entity.definition_id,entity.display_name,
                   entity.data_type,entity.unit,latest.event_id,
                   latest.value_float,latest.value_int,latest.value_numeric,
                   latest.value_bool,latest.value_text,latest.value_codes,
                   latest.quality,latest.reason,latest.observed_at,
                   latest.received_at,latest.calculated_at,
                   latest.processing_revision_id,latest.configuration_revision,
                   latest.source_digest,latest.frame_sequence,
                   entity.freshness_seconds
            FROM t_entity_instances AS entity
            LEFT JOIN t_l2_latest AS latest
              ON latest.entity_instance_id=entity.id
             AND latest.frame_sequence <= %s
            WHERE entity.node_id=%s AND entity.active=TRUE
            ORDER BY entity.display_name,entity.id
            """,
            (frame_sequence, str(node_id)),
        )
        items = []
        for row in cursor.fetchall():
            has_value = row[5] is not None
            effective_quality = _l2_snapshot_quality(
                has_value=has_value,
                stored_quality=row[12],
                observed_at=row[14],
                freshness_seconds=row[21],
                snapshot_at=snapshot_at,
            )
            if not has_value:
                effective_reason = "WAITING_DATA"
            elif effective_quality == int(TrunkQuality.STALE) and not row[13]:
                effective_reason = "STALE"
            else:
                effective_reason = row[13]
            items.append(
                {
                    "entity_instance_id": str(row[0]),
                    "node_id": str(node_id),
                    "definition_id": row[1],
                    "display_name": row[2],
                    "data_type": str(row[3]),
                    "value": (
                        None
                        if not has_value
                        else _typed_value(
                            str(row[3]),
                            row[6],
                            row[7],
                            row[8],
                            row[9],
                            row[10],
                            row[11],
                        )
                    ),
                    "unit": row[4],
                    "quality": (
                        effective_quality
                    ),
                    "reason": effective_reason,
                    "observed_at": _optional_iso(row[14]),
                    "received_at": _optional_iso(row[15]),
                    "calculated_at": _optional_iso(row[16]),
                    "processing_revision_id": (
                        None if row[17] is None else str(row[17])
                    ),
                    "configuration_revision": (
                        None if row[18] is None else int(row[18])
                    ),
                    "source_digest": (
                        None if row[19] is None else str(row[19]).strip()
                    ),
                    "frame_sequence": 0 if row[20] is None else int(row[20]),
                }
            )
        return tuple(items)


def _typed_value(
    data_type: str,
    value_float: Any,
    value_int: Any,
    value_numeric: Any,
    value_bool: Any,
    value_text: Any,
    value_codes: Any,
) -> Any:
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
        value = None if value_codes is None else list(value_codes)
    return float(value) if isinstance(value, Decimal) else value


def _l2_snapshot_quality(
    *,
    has_value: bool,
    stored_quality: Any,
    observed_at: datetime | None,
    freshness_seconds: Any,
    snapshot_at: datetime,
) -> int:
    if not has_value or observed_at is None or freshness_seconds is None:
        return int(TrunkQuality.STALE)
    effective = int(stored_quality)
    age_seconds = (snapshot_at - observed_at).total_seconds()
    if age_seconds > float(freshness_seconds):
        return min(effective, int(TrunkQuality.STALE))
    return effective


def _l0_snapshot_value(
    data_type: str,
    frame_sequence: int,
    *,
    raw_float: Any,
    raw_int: Any,
    raw_bool: Any,
    raw_text: Any,
    legacy_float: Any,
    legacy_int: Any,
    legacy_bool: Any,
    legacy_text: Any,
) -> Any:
    if frame_sequence > 0:
        return _typed_value(
            data_type,
            raw_float,
            raw_int,
            None,
            raw_bool,
            raw_text,
            None,
        )
    # Before committed frames, imported Neuron register types and engineering
    # values were occasionally stored in different legacy columns. Preserve
    # the last observable value for diagnostics without relaxing new frames.
    values = tuple(
        value
        for value in (legacy_float, legacy_int, legacy_bool, legacy_text)
        if value is not None
    )
    if len(values) != 1:
        return None
    value = values[0]
    return float(value) if isinstance(value, Decimal) else value


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("frame timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)
