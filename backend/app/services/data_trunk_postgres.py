"""L0、L2、latest、source 与 outbox 的单事务 PostgreSQL adapter。"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg2.extras import Json, execute_values

from app.services.data_trunk import DataTrunk
from app.services.data_trunk_contracts import (
    BlackboardRecovery,
    BudgetTerminalizationClaim,
    BooleanCodeInput,
    BooleanSetTransform,
    ClaimedFrame,
    DataTrunkError,
    EnumTransform,
    FaultCodeTransform,
    FrameStatus,
    FrameFailure,
    FramedRawObservation,
    FrozenFrameCandidate,
    FormulaSource,
    FormulaTransform,
    CompiledFormula,
    InputReference,
    InstalledPointProcessing,
    L2Observation,
    PendingFrame,
    ProcessingSnapshot,
    RawObservation,
    SourceOrder,
    SourceOrderMode,
    TrunkQuality,
    TypedValue,
    TerminalFrame,
    ValueKind,
)
from app.services.point_processing_dag import validate_processing_dag
from app.services.point_processing import _formula_input_names
from app.services.runtime_identity import RUNTIME_INSTANCE_ID
from app.services.data_trunk_outbox import (
    build_frame_outbox_event,
    capture_previous_l0_state,
)
ConnectionFactory = Callable[[], AbstractContextManager[Any]]
FaultHook = Callable[[str], None]


def data_frame_release_readiness_blockers(
    *,
    committed_frame_consumer: bool,
    retention_policy_resolved: bool,
) -> frozenset[str]:
    blockers = set()
    if not committed_frame_consumer:
        blockers.add("COMMITTED_FRAME_CONSUMER_MISSING")
    if not retention_policy_resolved:
        blockers.add("DATA_FRAME_RETENTION_POLICY_UNRESOLVED")
    return frozenset(blockers)


def verify_data_trunk_contract_gate(
    connection_factory: ConnectionFactory | None = None,
) -> int:
    """Fail startup when an L2 entity no longer has exactly one source."""
    if connection_factory is None:
        from app.services.telemetry_store import get_connection

        connection_factory = get_connection
    with connection_factory() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DO $$
                BEGIN
                  IF to_regclass('public.t_configuration_state') IS NULL
                     OR to_regclass('public.t_configuration_revisions') IS NULL
                     OR to_regclass('public.t_configuration_audit') IS NULL
                     OR to_regclass('public.t_point_processing_expressions') IS NULL
                     OR to_regclass('public.t_point_processing_selectors') IS NULL
                     OR to_regclass('public.t_point_processing_selector_members') IS NULL
                     OR to_regclass('public.t_point_processing_dependencies') IS NULL
                     OR to_regclass('public.t_point_processing_formula_runs') IS NULL
                     OR to_regclass('public.t_data_frames') IS NULL
                     OR to_regclass('public.t_data_frame_outbox') IS NULL
                     OR to_regclass('public.t_committed_frame_consumers') IS NULL
                     OR to_regclass('public.l2_agg_1h') IS NULL
                     OR to_regclass('public.l2_agg_1d') IS NULL
                     OR to_regclass('zizu_internal.retention_guard') IS NULL
                     OR to_regclass('public.t_l2_stream_outbox') IS NOT NULL THEN
                    RAISE EXCEPTION 'schema 048 data frame contract is incomplete'
                      USING ERRCODE = '55000';
                  END IF;
                  IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 't_nodes'
                      AND column_name = 'parent_id'
                  ) OR NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 't_entity_instances'
                      AND column_name = 'node_id'
                  ) THEN
                    RAISE EXCEPTION 'schema 044 node ownership contract is incomplete'
                      USING ERRCODE = '55000';
                  END IF;
                  IF EXISTS (
                    SELECT required.table_name, required.column_name
                    FROM (VALUES
                      ('t_data_frames','frame_sequence'),
                      ('t_data_frames','processing_token'),
                      ('t_data_frame_outbox','claim_token'),
                      ('t_data_frame_outbox','payload_version'),
                      ('t_data_frame_outbox','payload'),
                      ('t_telemetry','frame_id'),
                      ('t_telemetry','frame_sequence'),
                      ('t_telemetry','accepted_beat'),
                      ('t_telemetry','source_order_mode'),
                      ('t_telemetry','source_receive_ordinal'),
                      ('t_telemetry_latest','frame_sequence'),
                      ('t_l2_observations','frame_id'),
                      ('t_l2_observations','commit_sequence'),
                      ('t_l2_latest','frame_sequence')
                    ) AS required(table_name,column_name)
                    LEFT JOIN information_schema.columns AS actual
                      ON actual.table_schema='public'
                     AND actual.table_name=required.table_name
                     AND actual.column_name=required.column_name
                    WHERE actual.column_name IS NULL
                  ) THEN
                    RAISE EXCEPTION 'schema 046 frame columns are incomplete'
                      USING ERRCODE = '55000';
                  END IF;
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname='public' AND indexname='ix_data_frames_claim'
                  ) OR NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname='public'
                      AND indexname='ix_data_frame_outbox_pending'
                  ) OR NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname='public'
                      AND indexname='ix_data_frame_outbox_replay'
                  ) OR NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname='public'
                      AND indexname='ix_telemetry_tag_frame_sequence'
                  ) OR NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname='public'
                      AND indexname='uq_committed_frame_consumer_sequence'
                  ) THEN
                    RAISE EXCEPTION 'schema 049 frame indexes are incomplete'
                      USING ERRCODE = '55000';
                  END IF;
                  IF to_regprocedure('public.guard_data_frame_transition()') IS NULL
                     OR to_regprocedure(
                       'public.prune_committed_frame_history(integer,jsonb)'
                     ) IS NULL
                     OR NOT EXISTS (
                       SELECT 1 FROM pg_trigger
                       WHERE tgname='trg_guard_data_frame_transition'
                         AND NOT tgisinternal
                     ) OR NOT EXISTS (
                       SELECT 1 FROM pg_trigger
                       WHERE tgname='trg_guard_data_frame_outbox_payload'
                         AND NOT tgisinternal
                     ) OR NOT EXISTS (
                       SELECT 1 FROM pg_constraint
                       WHERE conname='chk_data_frame_outbox_claim'
                     ) OR (
                       SELECT count(*) FROM pg_constraint
                       WHERE conrelid='public.t_committed_frame_consumers'::regclass
                         AND conname IN (
                           't_committed_frame_consumers_pkey',
                           'chk_committed_frame_consumer_key',
                           'chk_committed_frame_consumer_sequence',
                           'chk_committed_frame_consumer_revision',
                           'fk_committed_frame_consumer_frame'
                         )
                     ) <> 5
                     ) THEN
                    RAISE EXCEPTION 'schema 049 frame fencing is incomplete'
                      USING ERRCODE = '55000';
                  END IF;
                  IF (
                    SELECT count(*) FROM timescaledb_information.jobs
                    WHERE proc_schema='public'
                      AND proc_name='prune_committed_frame_history'
                  ) <> 1 THEN
                    RAISE EXCEPTION 'schema 048 retention job is incomplete'
                      USING ERRCODE = '55000';
                  END IF;
                END;
                $$
                """
            )
            cursor.execute(
                """
                SELECT assert_entity_instance_single_source(id)
                FROM t_entity_instances
                ORDER BY id
                """
            )
            return len(cursor.fetchall())


@dataclass(frozen=True)
class _ConversionSnapshot:
    installed: tuple[InstalledPointProcessing, ...]
    configuration_revision: int
    current_inputs: Mapping[InputReference, RawObservation]


class FrameWriterLease:
    """A process-lifetime PostgreSQL advisory lock on its dedicated session."""

    def __init__(
        self,
        connection,
        release_connection: Callable[[Any], None],
    ) -> None:
        self._connection = connection
        self._release_connection = release_connection
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock("
                    "hashtextextended('zizu:data-frame-writer', 0))"
                )
        finally:
            self._closed = True
            self._release_connection(self._connection)


class PostgresFrameRepository:
    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        fault_hook: FaultHook | None = None,
        state_heartbeat_seconds: float = 60.0,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault_hook = fault_hook or (lambda _stage: None)
        if state_heartbeat_seconds <= 0:
            raise ValueError("state heartbeat must be positive")
        self._state_heartbeat_seconds = state_heartbeat_seconds
        self._processing_owner = uuid4()

    def current_configuration_revision(self) -> int:
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_revision FROM t_configuration_state "
                    "WHERE singleton=TRUE"
                )
                row = cursor.fetchone()
                connection.commit()
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_CONFIGURATION_UNAVAILABLE",
                "DATA_FRAME_CONFIGURATION_UNAVAILABLE",
            ) from exc
        if row is None:
            raise DataTrunkError(
                "DATA_FRAME_CONFIGURATION_MISSING",
                "DATA_FRAME_CONFIGURATION_MISSING",
            )
        return int(row[0])

    def unfinished_frame_count(self) -> int:
        return self._count(
            "SELECT count(*) FROM t_data_frames "
            "WHERE status IN ('PENDING','PROCESSING')"
        )

    def unpublished_frame_outbox_count(self) -> int:
        return self._count(
            "SELECT count(*) FROM t_data_frame_outbox WHERE published_at IS NULL"
        )

    def _count(self, statement: str) -> int:
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(statement)
                value = int(cursor.fetchone()[0])
                connection.commit()
                return value
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_RUNTIME_STATE_UNAVAILABLE",
                "DATA_FRAME_RUNTIME_STATE_UNAVAILABLE",
            ) from exc

    def acquire_writer(self) -> FrameWriterLease:
        manager = self._connection()
        connection = manager.__enter__()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock("
                    "hashtextextended('zizu:data-frame-writer', 0))"
                )
                acquired = bool(cursor.fetchone()[0])
            if not acquired:
                raise DataTrunkError(
                    "DATA_FRAME_WRITER_ALREADY_ACTIVE",
                    "DATA_FRAME_WRITER_ALREADY_ACTIVE",
                )
        except Exception:
            manager.__exit__(None, None, None)
            raise

        def release(_connection) -> None:
            manager.__exit__(None, None, None)

        return FrameWriterLease(connection, release)

    def restore_blackboard(self) -> BlackboardRecovery:
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_revision FROM t_configuration_state "
                    "WHERE singleton=TRUE"
                )
                revision_row = cursor.fetchone()
                if revision_row is None:
                    raise DataTrunkError(
                        "DATA_FRAME_CONFIGURATION_MISSING",
                        "DATA_FRAME_CONFIGURATION_MISSING",
                    )
                cursor.execute(
                    "SELECT COALESCE(max(capture_beat),0) FROM t_data_frames"
                )
                capture_beat = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT tag.id,
                           EXISTS (
                             SELECT 1
                             FROM t_point_processing_input_bindings AS binding
                             JOIN t_installed_point_processings AS installed
                               ON installed.id = binding.installed_processing_id
                              AND installed.current = TRUE
                             JOIN t_point_processing_inputs AS input
                               ON input.id = binding.input_id
                             WHERE binding.source_kind = 'l0'
                               AND binding.l0_tag_id = tag.id
                               AND input.required = TRUE
                           ) AS required,
                           COALESCE(NULLIF(tag.source_path, ''), tag.name),
                           COALESCE(tag.value_data_type, tag.data_type),
                           tag.unit,
                           CASE
                             WHEN tag.source_sequence_trusted THEN 'sequence'
                             WHEN tag.timestamp_trusted THEN 'observed_at'
                             ELSE 'received_at'
                           END AS source_order_mode
                    FROM t_tags AS tag
                    JOIN t_nodes AS node ON node.id = tag.node_id
                    WHERE tag.enabled = TRUE
                      AND node.enabled = TRUE
                    ORDER BY tag.id
                    """
                )
                contracts = cursor.fetchall()
                contract_by_tag = {
                    UUID(str(row[0])): row for row in contracts
                }
                observations: list[FramedRawObservation] = []
                if contract_by_tag:
                    cursor.execute(
                        """
                        SELECT DISTINCT ON (telemetry.tag_id)
                          telemetry.observation_id, telemetry.node_id,
                          telemetry.tag_id, telemetry.ts,
                          telemetry.source_digest,
                          telemetry.source_message_id,
                          telemetry.source_sequence,
                          telemetry.raw_unit, telemetry.raw_value_float,
                          telemetry.raw_value_int, telemetry.raw_value_bool,
                          telemetry.raw_value_text, telemetry.quality,
                          telemetry.event_time_basis,
                          telemetry.event_received_at,
                          telemetry.accepted_beat,
                          telemetry.source_order_mode,
                          telemetry.source_receive_ordinal
                        FROM t_telemetry AS telemetry
                        WHERE telemetry.tag_id = ANY(%s::uuid[])
                          AND telemetry.frame_sequence IS NOT NULL
                        ORDER BY telemetry.tag_id,
                                 telemetry.frame_sequence DESC, telemetry.ts DESC
                        """,
                        ([str(tag_id) for tag_id in contract_by_tag],),
                    )
                    for row in cursor.fetchall():
                        tag_id = UUID(str(row[2]))
                        contract = contract_by_tag[tag_id]
                        mode = SourceOrderMode(str(row[16]))
                        ordinal = int(row[17] or 0)
                        if mode is SourceOrderMode.SEQUENCE:
                            if row[6] is None:
                                raise DataTrunkError(
                                    "DATA_FRAME_RECOVERY_EVIDENCE_INVALID",
                                    "DATA_FRAME_RECOVERY_EVIDENCE_INVALID",
                                )
                            order = SourceOrder.sequence(int(row[6]))
                        elif mode is SourceOrderMode.OBSERVED_AT:
                            order = SourceOrder.observed_at(
                                row[3], ordinal, str(row[4]).strip()
                            )
                        else:
                            order = SourceOrder.received_at(
                                row[14], ordinal, str(row[4]).strip()
                            )
                        value = _raw_value_from_columns(
                            str(contract[3]), row[8], row[9], row[10], row[11]
                        )
                        raw = RawObservation(
                            observation_id=UUID(str(row[0])),
                            node_id=UUID(str(row[1])),
                            tag_id=tag_id,
                            source_key=str(contract[2]),
                            value=value,
                            raw_unit=row[7],
                            quality=TrunkQuality(int(row[12])),
                            source_timestamp=row[3],
                            received_at=row[14],
                            source_message_id=row[5],
                            source_sequence=row[6],
                            source_digest=str(row[4]).strip(),
                            event_time_basis=str(row[13]),
                            source_order=order,
                        )
                        observations.append(
                            FramedRawObservation(
                                observation=raw,
                                accepted_beat=int(row[15]),
                                effective_quality=TrunkQuality(int(row[12])),
                            )
                        )
                connection.commit()
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_RECOVERY_FAILED",
                "DATA_FRAME_RECOVERY_FAILED",
            ) from exc
        return BlackboardRecovery(
            capture_beat=capture_beat,
            configuration_revision=int(revision_row[0]),
            active_input_contracts=MappingProxyType(
                {
                    tag_id: SourceOrderMode(str(row[5]))
                    for tag_id, row in contract_by_tag.items()
                }
            ),
            required_tag_ids=frozenset(
                tag_id for tag_id, row in contract_by_tag.items() if bool(row[1])
            ),
            observations=tuple(
                sorted(observations, key=lambda item: str(item.observation.tag_id))
            ),
        )

    def commit_pending(self, candidate: FrozenFrameCandidate) -> PendingFrame:
        pending: PendingFrame | None = None
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended('zizu:data-frame-capture', 0))"
                        )
                        cursor.execute(
                            "SELECT current_revision FROM t_configuration_state "
                            "WHERE singleton=TRUE FOR SHARE"
                        )
                        row = cursor.fetchone()
                        current_revision = None if row is None else int(row[0])
                        if current_revision != candidate.configuration_revision:
                            raise DataTrunkError(
                                "DATA_FRAME_CONFIGURATION_STALE",
                                "DATA_FRAME_CONFIGURATION_STALE",
                            )
                        cursor.execute(
                            """
                            SELECT frame_id, frame_sequence, candidate_digest,
                                   capture_beat, shot_at,
                                   configuration_revision, status
                            FROM t_data_frames
                            WHERE frame_id=%s OR capture_beat=%s
                            ORDER BY frame_sequence
                            FOR UPDATE
                            """,
                            (str(candidate.frame_id), candidate.capture_beat),
                        )
                        existing = cursor.fetchall()
                        if existing:
                            if len(existing) != 1 or not self._same_candidate(
                                existing[0], candidate
                            ):
                                raise DataTrunkError(
                                    "DATA_FRAME_CANDIDATE_CONFLICT",
                                    "DATA_FRAME_CANDIDATE_CONFLICT",
                                )
                            pending = self._pending_from_row(existing[0])
                        else:
                            cursor.execute(
                                """
                                INSERT INTO t_data_frames
                                  (frame_id, candidate_digest, capture_beat,
                                   shot_at, configuration_revision, status)
                                VALUES (%s,%s,%s,%s,%s,'PENDING')
                                RETURNING frame_id, frame_sequence,
                                          candidate_digest, capture_beat,
                                          shot_at, configuration_revision, status
                                """,
                                (
                                    str(candidate.frame_id),
                                    candidate.candidate_digest,
                                    candidate.capture_beat,
                                    candidate.shot_at,
                                    candidate.configuration_revision,
                                ),
                            )
                            inserted = cursor.fetchone()
                            pending = self._pending_from_row(inserted)
                            self._insert_frame_l0(
                                cursor,
                                pending=pending,
                                observations=candidate.changed_l0,
                            )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_COMMIT_FAILED",
                "DATA_FRAME_COMMIT_FAILED",
            ) from exc

        try:
            self._fault_hook("frame_commit")
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_COMMIT_RESULT_UNKNOWN",
                "DATA_FRAME_COMMIT_RESULT_UNKNOWN",
            ) from exc
        if pending is None:  # pragma: no cover - database contract guard
            raise DataTrunkError(
                "DATA_FRAME_COMMIT_FAILED",
                "DATA_FRAME_COMMIT_FAILED",
            )
        return pending

    def claim_next(
        self,
        now: datetime,
    ) -> ClaimedFrame | BudgetTerminalizationClaim | None:
        token = uuid4()
        lease_until = now + timedelta(seconds=30)
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock("
                            "hashtextextended('zizu:data-frame-processor',0))"
                        )
                        cursor.execute(
                            """
                            SELECT frame_id,frame_sequence,capture_beat,shot_at,
                                   configuration_revision,attempt_count,
                                   processing_owner,processing_token,lease_until,
                                   created_at,status
                            FROM t_data_frames
                            WHERE status IN ('PENDING','PROCESSING')
                            ORDER BY frame_sequence
                            LIMIT 1
                            FOR UPDATE
                            """,
                        )
                        head = cursor.fetchone()
                        row = None
                        terminalization = False
                        if head is not None:
                            if head[10] == "PROCESSING" and head[8] > now:
                                connection.commit()
                                return None
                            terminalization = (
                                int(head[5]) >= 3
                                or (now - head[9]).total_seconds() >= 60
                            )
                            # Migration 048 treats an expired PROCESSING lease as
                            # ownership recovery, not another business attempt.
                            # Only a PENDING -> PROCESSING transition spends the
                            # next retry from the three-attempt budget.
                            next_attempt = (
                                int(head[5])
                                if terminalization or head[10] == "PROCESSING"
                                else int(head[5]) + 1
                            )
                            claim_owner = (
                                uuid4()
                                if head[10] == "PROCESSING"
                                else self._processing_owner
                            )
                            cursor.execute(
                                """
                                UPDATE t_data_frames
                                SET status='PROCESSING',attempt_count=%s,
                                    processing_owner=%s,processing_token=%s,
                                    lease_until=%s
                                WHERE frame_id=%s
                                RETURNING frame_id,frame_sequence,capture_beat,
                                          shot_at,configuration_revision,
                                          attempt_count,processing_owner,
                                          processing_token,lease_until,created_at
                                """,
                                (
                                    next_attempt,
                                    str(claim_owner),
                                    str(token),
                                    lease_until,
                                    str(head[0]),
                                ),
                            )
                            row = cursor.fetchone()
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_CLAIM_FAILED",
                "DATA_FRAME_CLAIM_FAILED",
            ) from exc
        if row is None:
            return None
        claim_fields = dict(
            frame_id=UUID(str(row[0])),
            frame_sequence=int(row[1]),
            capture_beat=int(row[2]),
            shot_at=row[3],
            configuration_revision=int(row[4]),
            attempt_count=int(row[5]),
            processing_owner=UUID(str(row[6])),
            processing_token=UUID(str(row[7])),
            lease_until=row[8],
            created_at=row[9],
        )
        if terminalization:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT output_binding.entity_instance_id
                    FROM t_installed_point_processings AS installed
                    JOIN t_point_processing_output_bindings AS output_binding
                      ON output_binding.installed_processing_id=installed.id
                    WHERE installed.current=TRUE
                      AND installed.configuration_revision <= %s
                    ORDER BY output_binding.entity_instance_id
                    """,
                    (claim_fields["configuration_revision"],),
                )
                affected = frozenset(UUID(str(item[0])) for item in cursor.fetchall())
                connection.commit()
            return BudgetTerminalizationClaim(
                **claim_fields,
                affected_l2=affected,
            )
        return ClaimedFrame(**claim_fields)

    def load_processing_snapshot(self, claimed: ClaimedFrame) -> ProcessingSnapshot:
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH current_frame AS MATERIALIZED (
                      SELECT telemetry.observation_id,telemetry.node_id,
                             telemetry.tag_id,telemetry.raw_unit,
                             telemetry.raw_value_float,telemetry.raw_value_int,
                             telemetry.raw_value_bool,telemetry.raw_value_text,
                             telemetry.quality,telemetry.ts,
                             telemetry.event_received_at,
                             telemetry.source_message_id,
                             telemetry.source_sequence,telemetry.source_digest,
                             telemetry.event_time_basis,
                             telemetry.accepted_beat,
                             telemetry.source_order_mode,
                             telemetry.source_receive_ordinal
                      FROM t_l0_observation_dedup AS dedup
                      JOIN LATERAL (
                        SELECT item.observation_id,item.node_id,item.tag_id,
                               item.raw_unit,item.raw_value_float,
                               item.raw_value_int,item.raw_value_bool,
                               item.raw_value_text,item.quality,item.ts,
                               item.event_received_at,item.source_message_id,
                               item.source_sequence,item.source_digest,
                               item.event_time_basis,item.accepted_beat,
                               item.source_order_mode,
                               item.source_receive_ordinal
                        FROM t_telemetry AS item
                        WHERE item.observation_id=dedup.observation_id
                          AND item.ts=dedup.observed_at
                          AND item.frame_id=%s
                          AND item.ts >= %s - interval '5 minutes'
                          AND item.ts <= %s + interval '5 minutes'
                        LIMIT 1
                      ) AS telemetry ON TRUE
                      WHERE dedup.created_at=%s
                    ), relevant_tags AS (
                      SELECT DISTINCT binding.l0_tag_id AS tag_id
                      FROM t_point_processing_input_bindings AS binding
                      JOIN t_installed_point_processings AS installed
                        ON installed.id=binding.installed_processing_id
                      WHERE binding.source_kind='l0'
                        AND installed.current=TRUE
                        AND installed.configuration_revision <= %s
                      UNION
                      SELECT tag_id FROM current_frame
                    ), frame_state AS (
                      SELECT * FROM current_frame
                      UNION ALL
                      SELECT latest.observation_id,latest.node_id,latest.tag_id,
                             latest.raw_unit,latest.raw_value_float,
                             latest.raw_value_int,latest.raw_value_bool,
                             latest.raw_value_text,latest.quality,latest.ts,
                             latest.event_received_at,latest.source_message_id,
                             latest.source_sequence,latest.source_digest,
                             latest.event_time_basis,latest_frame.capture_beat,
                             latest.source_order_mode,
                             latest.source_receive_ordinal
                      FROM t_telemetry_latest AS latest
                      JOIN relevant_tags USING(tag_id)
                      JOIN t_data_frames AS latest_frame
                        ON latest_frame.frame_sequence=latest.frame_sequence
                      WHERE NOT EXISTS (
                        SELECT 1 FROM current_frame
                        WHERE current_frame.tag_id=latest.tag_id
                      )
                    )
                    SELECT
                      telemetry.observation_id,telemetry.node_id,
                      telemetry.tag_id,tag.name,
                      COALESCE(tag.value_data_type,tag.data_type),
                      telemetry.raw_unit,telemetry.raw_value_float,
                      telemetry.raw_value_int,telemetry.raw_value_bool,
                      telemetry.raw_value_text,telemetry.quality,telemetry.ts,
                      telemetry.event_received_at,telemetry.source_message_id,
                      telemetry.source_sequence,telemetry.source_digest,
                      telemetry.event_time_basis,telemetry.accepted_beat,
                      telemetry.source_order_mode,
                      telemetry.source_receive_ordinal
                    FROM frame_state AS telemetry
                    JOIN t_tags AS tag ON tag.id=telemetry.tag_id
                    ORDER BY telemetry.tag_id
                    """,
                    (
                        str(claimed.frame_id),
                        claimed.shot_at,
                        claimed.shot_at,
                        claimed.created_at,
                        claimed.configuration_revision,
                    ),
                )
                cells: dict[UUID, FramedRawObservation] = {}
                for row in cursor.fetchall():
                    tag_id = UUID(str(row[2]))
                    mode = SourceOrderMode(str(row[18]))
                    ordinal = int(row[19] or 0)
                    if mode is SourceOrderMode.SEQUENCE:
                        order = SourceOrder.sequence(int(row[14]))
                    elif mode is SourceOrderMode.OBSERVED_AT:
                        order = SourceOrder.observed_at(
                            row[11], ordinal, str(row[15]).strip()
                        )
                    else:
                        order = SourceOrder.received_at(
                            row[12], ordinal, str(row[15]).strip()
                        )
                    raw = RawObservation(
                        observation_id=UUID(str(row[0])),
                        node_id=UUID(str(row[1])),
                        tag_id=tag_id,
                        source_key=str(row[3]),
                        value=_raw_value_from_columns(
                            str(row[4]), row[6], row[7], row[8], row[9]
                        ),
                        raw_unit=row[5],
                        quality=TrunkQuality(int(row[10])),
                        source_timestamp=row[11],
                        received_at=row[12],
                        source_message_id=row[13],
                        source_sequence=row[14],
                        source_digest=str(row[15]).strip(),
                        event_time_basis=str(row[16]),
                        source_order=order,
                    )
                    effective_quality = TrunkQuality(int(row[10]))
                    if claimed.capture_beat - int(row[17]) >= 3:
                        effective_quality = min(
                            effective_quality,
                            TrunkQuality.STALE,
                        )
                    cells[tag_id] = FramedRawObservation(
                        observation=raw,
                        accepted_beat=int(row[17]),
                        effective_quality=effective_quality,
                    )
                legacy_snapshot = self._load_conversion_snapshot(
                    cursor,
                    tuple(cell.observation for cell in cells.values()),
                    calculated_at=claimed.shot_at,
                )
                installed = {
                    item.entity_instance_id: item
                    for item in legacy_snapshot.installed
                }
                installed.update(
                    {
                        item.entity_instance_id: item
                        for item in self._load_frame_formula_processings(
                            cursor, claimed.configuration_revision
                        )
                    }
                )
                cursor.execute(
                    """
                    SELECT source_entity_instance_id,target_entity_instance_id
                    FROM t_point_processing_dependencies AS dependency
                    JOIN t_installed_point_processings AS installed
                      ON installed.id=dependency.installed_processing_id
                    WHERE installed.current=TRUE
                      AND installed.configuration_revision <= %s
                    ORDER BY source_entity_instance_id,target_entity_instance_id
                    """,
                    (claimed.configuration_revision,),
                )
                edges = tuple(
                    (UUID(str(source)), UUID(str(target)))
                    for source, target in cursor.fetchall()
                    if UUID(str(source)) in installed
                    and UUID(str(target)) in installed
                )
                dag_order = validate_processing_dag(
                    existing_edges=edges,
                    planned_edges=(),
                ).order
                ordered = dag_order + tuple(
                    sorted(
                        set(installed) - set(dag_order),
                        key=str,
                    )
                )
                connection.commit()
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_SNAPSHOT_FAILED",
                "DATA_FRAME_SNAPSHOT_FAILED",
            ) from exc
        return ProcessingSnapshot(
            l0_by_tag=MappingProxyType(cells),
            installed_by_entity_id=MappingProxyType(installed),
            topological_output_ids=ordered,
            dependency_edges=edges,
        )

    @staticmethod
    def _load_frame_formula_processings(
        cursor,
        configuration_revision: int,
    ) -> tuple[InstalledPointProcessing, ...]:
        cursor.execute(
            """
            SELECT installed.id,installed.revision_id,output.id,
                   output_binding.entity_instance_id,
                   output.entity_definition_id,output.data_type,output.unit,
                   output.freshness_seconds,expression.dsl_text,
                   expression.canonical_ast,expression.ast_digest,
                   expression.schedule_seconds,expression.control_eligible
            FROM t_installed_point_processings AS installed
            JOIN t_point_processing_outputs AS output
              ON output.revision_id=installed.revision_id
            JOIN t_point_processing_output_bindings AS output_binding
              ON output_binding.installed_processing_id=installed.id
             AND output_binding.output_id=output.id
            JOIN t_point_processing_expressions AS expression
              ON expression.output_id=output.id
            WHERE installed.current=TRUE
              AND installed.configuration_revision <= %s
            ORDER BY output_binding.entity_instance_id
            """,
            (configuration_revision,),
        )
        items: list[InstalledPointProcessing] = []
        for row in cursor.fetchall():
            (
                installation_id,
                revision_id,
                output_id,
                target_entity_id,
                definition_id,
                output_type,
                output_unit,
                freshness_seconds,
                dsl_text,
                canonical_ast,
                ast_digest,
                schedule_seconds,
                control_eligible,
            ) = row
            cursor.execute(
                """
                SELECT input.id,input.input_key,input.source_kind,
                       input.data_type,input.unit,
                       input.required,COALESCE(selector.cardinality,'one'),
                       selector.default_value
                FROM t_point_processing_inputs AS input
                LEFT JOIN t_point_processing_selectors AS selector
                  ON selector.input_id=input.id
                WHERE input.revision_id=%s
                ORDER BY input.input_key
                """,
                (revision_id,),
            )
            input_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT input_id,source_kind,l0_tag_id,l2_entity_instance_id
                FROM t_point_processing_input_bindings
                WHERE installed_processing_id=%s
                ORDER BY input_id
                """,
                (installation_id,),
            )
            bound_sources: dict[UUID, list[InputReference]] = {}
            for input_id, source_kind, l0_tag_id, l2_entity_id in cursor.fetchall():
                source_id = l0_tag_id if source_kind == "l0" else l2_entity_id
                if source_id is None:
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "formula input binding has no source",
                    )
                bound_sources.setdefault(UUID(str(input_id)), []).append(
                    InputReference(str(source_kind), UUID(str(source_id)))
                )
            cursor.execute(
                """
                SELECT input_id,entity_instance_id
                FROM t_point_processing_selector_members
                WHERE installed_processing_id=%s
                ORDER BY input_id,ordinal
                """,
                (installation_id,),
            )
            for input_id, entity_id in cursor.fetchall():
                bound_sources.setdefault(UUID(str(input_id)), []).append(
                    InputReference.l2(UUID(str(entity_id)))
                )
            referenced_inputs = set(_formula_input_names(canonical_ast))
            source_contracts: dict[str, FormulaSource] = {}
            sources: dict[str, tuple[InputReference, ...]] = {}
            for (
                input_id,
                input_key,
                source_kind,
                data_type,
                unit,
                required,
                cardinality,
                default_value,
            ) in input_rows:
                if input_key not in referenced_inputs:
                    continue
                references = tuple(
                    bound_sources.get(UUID(str(input_id)), ())
                )
                if any(reference.source_kind != source_kind for reference in references):
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "formula input binding kind does not match its contract",
                    )
                if not references and required:
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "required formula input has no frozen source",
                    )
                source_contracts[input_key] = FormulaSource(
                    input_key,
                    ValueKind(data_type),
                    unit,
                    cardinality,
                    bool(required),
                    default_value,
                )
                sources[input_key] = references
            items.append(
                InstalledPointProcessing(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    entity_instance_id=UUID(str(target_entity_id)),
                    entity_definition_id=definition_id,
                    output_kind=ValueKind(output_type),
                    output_unit=output_unit,
                    freshness_seconds=float(freshness_seconds),
                    transform=FormulaTransform(
                        sources=sources,
                        source_contracts=source_contracts,
                        compiled=CompiledFormula(
                            text=dsl_text,
                            ast=canonical_ast,
                            digest=str(ast_digest).strip(),
                            result_kind=ValueKind(output_type),
                            result_unit=output_unit,
                        ),
                        schedule_seconds=int(schedule_seconds),
                        control_eligible=bool(control_eligible),
                    ),
                )
            )
        return tuple(items)

    def complete(
        self,
        claimed: ClaimedFrame,
        snapshot: ProcessingSnapshot,
        outputs: tuple[L2Observation, ...],
    ) -> TerminalFrame:
        finished_at = self._clock()
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT 1 FROM t_data_frames
                            WHERE frame_id=%s AND status='PROCESSING'
                              AND processing_owner=%s AND processing_token=%s
                              AND attempt_count=%s
                              AND lease_until > clock_timestamp()
                            FOR UPDATE
                            """,
                            (
                                str(claimed.frame_id),
                                str(claimed.processing_owner),
                                str(claimed.processing_token),
                                claimed.attempt_count,
                            ),
                        )
                        if cursor.fetchone() is None:
                            raise DataTrunkError(
                                "DATA_FRAME_CLAIM_LOST",
                                "DATA_FRAME_CLAIM_LOST",
                            )
                        previous_l0 = capture_previous_l0_state(
                            cursor, claimed.frame_sequence
                        )
                        self._advance_frame_l0_latest(
                            cursor,
                            claimed.frame_sequence,
                            tuple(snapshot.l0_by_tag.values()),
                        )
                        self._ensure_runtime(cursor)
                        self._insert_frame_l2(cursor, outputs)
                        self._advance_frame_l2_latest(cursor, outputs)
                        self._insert_sources(cursor, outputs)
                        self._fault_hook("source")
                        event = build_frame_outbox_event(
                            cursor,
                            frame_id=claimed.frame_id,
                            frame_sequence=claimed.frame_sequence,
                            status=FrameStatus.COMPLETE,
                            configuration_revision=(
                                claimed.configuration_revision
                            ),
                            capture_beat=claimed.capture_beat,
                            frame_time=finished_at,
                            previous_l0=previous_l0,
                        )
                        cursor.execute(
                            """
                            INSERT INTO t_data_frame_outbox
                              (frame_id,frame_sequence,terminal_status,
                               payload_version,payload)
                            VALUES(%s,%s,'COMPLETE',1,%s)
                            """,
                            (
                                str(claimed.frame_id),
                                claimed.frame_sequence,
                                Json(event.public_dict()),
                            ),
                        )
                        self._fault_hook("outbox")
                        cursor.execute(
                            """
                            UPDATE t_data_frames
                            SET status='COMPLETE',processing_owner=NULL,
                                processing_token=NULL,lease_until=NULL,
                                finished_at=%s
                            WHERE frame_id=%s AND processing_owner=%s
                              AND processing_token=%s AND attempt_count=%s
                            """,
                            (
                                finished_at,
                                str(claimed.frame_id),
                                str(claimed.processing_owner),
                                str(claimed.processing_token),
                                claimed.attempt_count,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise DataTrunkError(
                                "DATA_FRAME_CLAIM_LOST",
                                "DATA_FRAME_CLAIM_LOST",
                            )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_COMPLETE_FAILED",
                "DATA_FRAME_COMPLETE_FAILED",
            ) from exc
        return TerminalFrame(
            frame_id=claimed.frame_id,
            frame_sequence=claimed.frame_sequence,
            configuration_revision=claimed.configuration_revision,
            status=FrameStatus.COMPLETE,
            finished_at=finished_at,
        )

    @staticmethod
    def _insert_frame_l2(
        cursor,
        observations: tuple[L2Observation, ...],
    ) -> None:
        if not observations:
            return
        rows = []
        for observation in observations:
            if observation.frame_id is None or observation.frame_sequence < 1:
                raise DataTrunkError(
                    "DATA_FRAME_L2_IDENTITY_REQUIRED",
                    "DATA_FRAME_L2_IDENTITY_REQUIRED",
                )
            rows.append(
                (
                    observation.observed_at,
                    str(observation.event_id),
                    str(observation.entity_instance_id),
                    observation.received_at,
                    observation.calculated_at,
                    *_l2_columns(observation.value),
                    int(observation.quality),
                    observation.reason,
                    str(observation.processing_revision_id),
                    observation.configuration_revision,
                    observation.source_digest,
                    observation.source_order_key,
                    str(RUNTIME_INSTANCE_ID),
                    observation.event_time_basis,
                    str(observation.frame_id),
                    observation.frame_sequence,
                )
            )
        execute_values(
            cursor,
            """
            INSERT INTO t_l2_observations
              (observed_at,event_id,entity_instance_id,received_at,
               calculated_at,value_float,value_int,value_numeric,value_bool,
               value_text,value_codes,quality,reason,processing_revision_id,
               configuration_revision,source_digest,source_order_key,
               producing_runtime_instance_id,event_time_basis,frame_id,
               commit_sequence)
            VALUES %s
            ON CONFLICT (event_id,observed_at) DO NOTHING
            """,
            rows,
            template=(
                "(%s::timestamptz,%s::uuid,%s::uuid,%s::timestamptz,"
                "%s::timestamptz,%s::double precision,%s::bigint,%s::numeric,"
                "%s::boolean,%s::text,%s::text[],%s::smallint,%s::text,"
                "%s::uuid,%s::bigint,%s::char(64),%s::text,%s::uuid,"
                "%s::text,%s::uuid,%s::bigint)"
            ),
            page_size=len(rows),
        )

    @staticmethod
    def _advance_frame_l2_latest(
        cursor,
        observations: tuple[L2Observation, ...],
    ) -> None:
        if not observations:
            return
        rows = [
            (
                str(observation.entity_instance_id),
                str(observation.event_id),
                observation.observed_at,
                observation.received_at,
                observation.calculated_at,
                *_l2_columns(observation.value),
                int(observation.quality),
                observation.reason,
                str(observation.processing_revision_id),
                observation.configuration_revision,
                observation.source_digest,
                observation.source_order_key,
                str(RUNTIME_INSTANCE_ID),
                observation.event_time_basis,
                observation.frame_sequence,
            )
            for observation in observations
        ]
        execute_values(
            cursor,
            """
            INSERT INTO t_l2_latest
              (entity_instance_id,event_id,observed_at,received_at,
               calculated_at,value_float,value_int,value_numeric,value_bool,
               value_text,value_codes,quality,reason,processing_revision_id,
               configuration_revision,source_digest,source_order_key,
               producing_runtime_instance_id,event_time_basis,frame_sequence)
            VALUES %s
            ON CONFLICT (entity_instance_id) DO UPDATE SET
              event_id=EXCLUDED.event_id,observed_at=EXCLUDED.observed_at,
              received_at=EXCLUDED.received_at,
              calculated_at=EXCLUDED.calculated_at,
              value_float=EXCLUDED.value_float,value_int=EXCLUDED.value_int,
              value_numeric=EXCLUDED.value_numeric,
              value_bool=EXCLUDED.value_bool,value_text=EXCLUDED.value_text,
              value_codes=EXCLUDED.value_codes,quality=EXCLUDED.quality,
              reason=EXCLUDED.reason,
              processing_revision_id=EXCLUDED.processing_revision_id,
              configuration_revision=EXCLUDED.configuration_revision,
              source_digest=EXCLUDED.source_digest,
              source_order_key=EXCLUDED.source_order_key,
              producing_runtime_instance_id=EXCLUDED.producing_runtime_instance_id,
              event_time_basis=EXCLUDED.event_time_basis,
              frame_sequence=EXCLUDED.frame_sequence
            WHERE EXCLUDED.frame_sequence > t_l2_latest.frame_sequence
            """,
            rows,
            template=(
                "(%s::uuid,%s::uuid,%s::timestamptz,%s::timestamptz,"
                "%s::timestamptz,%s::double precision,%s::bigint,%s::numeric,"
                "%s::boolean,%s::text,%s::text[],%s::smallint,%s::text,"
                "%s::uuid,%s::bigint,%s::char(64),%s::text,%s::uuid,"
                "%s::text,%s::bigint)"
            ),
            page_size=len(rows),
        )

    def retry_or_fail(
        self,
        claimed: ClaimedFrame,
        failure: FrameFailure,
        now: datetime,
    ) -> TerminalFrame | None:
        if claimed.attempt_count < 3 and (now - claimed.created_at).total_seconds() < 60:
            try:
                with self._connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE t_data_frames
                            SET status='PENDING',processing_owner=NULL,
                                processing_token=NULL,lease_until=NULL,
                                finished_at=NULL,failure_code=NULL
                            WHERE frame_id=%s AND status='PROCESSING'
                              AND processing_owner=%s AND processing_token=%s
                              AND attempt_count=%s AND lease_until > %s
                            """,
                            (
                                str(claimed.frame_id),
                                str(claimed.processing_owner),
                                str(claimed.processing_token),
                                claimed.attempt_count,
                                now,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise DataTrunkError(
                                "DATA_FRAME_CLAIM_LOST",
                                "DATA_FRAME_CLAIM_LOST",
                            )
                    connection.commit()
            except DataTrunkError:
                raise
            except Exception as exc:
                raise DataTrunkError(
                    "DATA_FRAME_RETRY_FAILED",
                    "DATA_FRAME_RETRY_FAILED",
                ) from exc
            return None
        return self._fail_claim(claimed, failure, now)

    def fail_budget(
        self,
        claimed: BudgetTerminalizationClaim,
        now: datetime,
    ) -> TerminalFrame:
        return self._fail_claim(
            claimed,
            FrameFailure(
                "FRAME_PROCESSING_FAILED",
                claimed.affected_l2,
            ),
            now,
        )

    def _fail_claim(
        self,
        claimed: ClaimedFrame | BudgetTerminalizationClaim,
        failure: FrameFailure,
        now: datetime,
    ) -> TerminalFrame:
        drop_expired_l0 = (
            isinstance(claimed, BudgetTerminalizationClaim)
            and now - claimed.created_at >= timedelta(seconds=60)
        )
        snapshot = (
            None if drop_expired_l0 else self.load_processing_snapshot(claimed)
        )
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT candidate_digest FROM t_data_frames
                            WHERE frame_id=%s AND status='PROCESSING'
                              AND processing_owner=%s AND processing_token=%s
                              AND attempt_count=%s AND lease_until > %s
                            FOR UPDATE
                            """,
                            (
                                str(claimed.frame_id),
                                str(claimed.processing_owner),
                                str(claimed.processing_token),
                                claimed.attempt_count,
                                now,
                            ),
                        )
                        frame_row = cursor.fetchone()
                        if frame_row is None:
                            raise DataTrunkError(
                                "DATA_FRAME_CLAIM_LOST",
                                "DATA_FRAME_CLAIM_LOST",
                            )
                        candidate_digest = str(frame_row[0]).strip()
                        previous_l0 = capture_previous_l0_state(
                            cursor, claimed.frame_sequence
                        )
                        if snapshot is not None:
                            self._advance_frame_l0_latest(
                                cursor,
                                claimed.frame_sequence,
                                tuple(snapshot.l0_by_tag.values()),
                            )
                        stale_outputs = self._failure_stale_outputs(
                            cursor,
                            claimed,
                            failure.failed_entity_ids,
                            now,
                        )
                        self._ensure_runtime(cursor)
                        self._insert_frame_l2(cursor, stale_outputs)
                        self._advance_frame_l2_latest(cursor, stale_outputs)
                        self._insert_sources(cursor, stale_outputs)
                        failure_id = uuid5(
                            NAMESPACE_URL,
                            f"zizu:data-frame-failure:{claimed.frame_id}",
                        )
                        cursor.execute(
                            """
                            INSERT INTO t_ingestion_failures
                              (id,source_digest,stage,safe_summary,attempts,frame_id)
                            VALUES(%s,%s,'frame',%s,%s,%s)
                            ON CONFLICT (frame_id) WHERE frame_id IS NOT NULL
                            DO NOTHING
                            """,
                            (
                                str(failure_id),
                                candidate_digest,
                                Json(
                                    {
                                        "code": failure.code,
                                        "configurationRevision": (
                                            claimed.configuration_revision
                                        ),
                                        "frameSequence": claimed.frame_sequence,
                                        "affectedEntityIds": sorted(
                                            map(str, failure.failed_entity_ids)
                                        ),
                                    }
                                ),
                                max(1, claimed.attempt_count),
                                str(claimed.frame_id),
                            ),
                        )
                        cursor.execute(
                            """
                            INSERT INTO t_data_frame_outbox
                              (frame_id,frame_sequence,terminal_status,
                               payload_version,payload)
                            VALUES(%s,%s,'FAILED',1,%s)
                            """,
                            (
                                str(claimed.frame_id),
                                claimed.frame_sequence,
                                Json(
                                    build_frame_outbox_event(
                                        cursor,
                                        frame_id=claimed.frame_id,
                                        frame_sequence=claimed.frame_sequence,
                                        status=FrameStatus.FAILED,
                                        configuration_revision=(
                                            claimed.configuration_revision
                                        ),
                                        capture_beat=claimed.capture_beat,
                                        frame_time=now,
                                        failure_id=failure_id,
                                        failure_code=failure.code,
                                        previous_l0=previous_l0,
                                    ).public_dict()
                                ),
                            ),
                        )
                        cursor.execute(
                            """
                            UPDATE t_data_frames
                            SET status='FAILED',failure_code=%s,
                                processing_owner=NULL,processing_token=NULL,
                                lease_until=NULL,finished_at=%s
                            WHERE frame_id=%s AND processing_owner=%s
                              AND processing_token=%s AND attempt_count=%s
                            """,
                            (
                                failure.code,
                                now,
                                str(claimed.frame_id),
                                str(claimed.processing_owner),
                                str(claimed.processing_token),
                                claimed.attempt_count,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise DataTrunkError(
                                "DATA_FRAME_CLAIM_LOST",
                                "DATA_FRAME_CLAIM_LOST",
                            )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_FRAME_FAIL_FAILED",
                "DATA_FRAME_FAIL_FAILED",
            ) from exc
        return TerminalFrame(
            frame_id=claimed.frame_id,
            frame_sequence=claimed.frame_sequence,
            configuration_revision=claimed.configuration_revision,
            status=FrameStatus.FAILED,
            finished_at=now,
        )

    @staticmethod
    def _failure_stale_outputs(
        cursor,
        claimed: ClaimedFrame | BudgetTerminalizationClaim,
        affected_ids: frozenset[UUID],
        now: datetime,
    ) -> tuple[L2Observation, ...]:
        if not affected_ids:
            return ()
        cursor.execute(
            """
            SELECT output_binding.entity_instance_id,
                   output.entity_definition_id,output.data_type,output.unit,
                   installed.revision_id,
                   latest.event_id,latest.observed_at,latest.received_at,
                   latest.calculated_at,latest.value_float,latest.value_int,
                   latest.value_numeric,latest.value_bool,latest.value_text,
                   latest.value_codes,latest.source_digest,
                   latest.quality,latest.reason
            FROM t_point_processing_output_bindings AS output_binding
            JOIN t_installed_point_processings AS installed
              ON installed.id=output_binding.installed_processing_id
            JOIN t_point_processing_outputs AS output
              ON output.id=output_binding.output_id
            LEFT JOIN t_l2_latest AS latest
              ON latest.entity_instance_id=output_binding.entity_instance_id
            WHERE installed.current=TRUE
              AND installed.configuration_revision <= %s
              AND output_binding.entity_instance_id=ANY(%s::uuid[])
            ORDER BY output_binding.entity_instance_id
            """,
            (
                claimed.configuration_revision,
                [str(item) for item in sorted(affected_ids, key=str)],
            ),
        )
        outputs: list[L2Observation] = []
        for row in cursor.fetchall():
            entity_id = UUID(str(row[0]))
            has_baseline = row[5] is not None
            if (
                row[16] == int(TrunkQuality.STALE)
                and row[17]
                in {
                    "FRAME_PROCESSING_FAILED",
                    "FRAME_PROCESSING_FAILED_NO_BASELINE",
                }
            ):
                continue
            value = (
                _l2_value_from_columns(
                    str(row[2]), row[9], row[10], row[11], row[12], row[13], row[14]
                )
                if has_baseline
                else TypedValue(ValueKind(str(row[2])), None)
            )
            source_ids = (UUID(str(row[5])),) if has_baseline else ()
            digest = hashlib.sha256(
                f"{claimed.frame_id}:{entity_id}:{row[15] or ''}".encode()
            ).hexdigest()
            outputs.append(
                L2Observation(
                    event_id=uuid5(
                        NAMESPACE_URL,
                        f"zizu:failed-frame-l2:{claimed.frame_id}:{entity_id}",
                    ),
                    entity_instance_id=entity_id,
                    definition_id=str(row[1]),
                    value=value,
                    unit=row[3],
                    quality=TrunkQuality.STALE,
                    reason=(
                        "FRAME_PROCESSING_FAILED"
                        if has_baseline
                        else "FRAME_PROCESSING_FAILED_NO_BASELINE"
                    ),
                    observed_at=now,
                    received_at=now,
                    calculated_at=now,
                    processing_revision_id=UUID(str(row[4])),
                    configuration_revision=claimed.configuration_revision,
                    source_observation_ids=source_ids,
                    source_digest=digest,
                    source_order_key=f"FRAME:{claimed.frame_sequence}:{digest}",
                    event_time_basis="calculated_at",
                    frame_id=claimed.frame_id,
                    frame_sequence=claimed.frame_sequence,
                )
            )
        return tuple(outputs)

    @staticmethod
    def _same_candidate(row, candidate: FrozenFrameCandidate) -> bool:
        return (
            UUID(str(row[0])) == candidate.frame_id
            and str(row[2]).strip() == candidate.candidate_digest
            and int(row[3]) == candidate.capture_beat
            and int(row[5]) == candidate.configuration_revision
        )

    @staticmethod
    def _pending_from_row(row) -> PendingFrame:
        status = FrameStatus(str(row[6]))
        if status is not FrameStatus.PENDING:
            raise DataTrunkError(
                "DATA_FRAME_CANDIDATE_CONFLICT",
                "DATA_FRAME_CANDIDATE_CONFLICT",
            )
        return PendingFrame(
            frame_id=UUID(str(row[0])),
            frame_sequence=int(row[1]),
            capture_beat=int(row[3]),
            shot_at=row[4],
            configuration_revision=int(row[5]),
            status=status,
        )

    @staticmethod
    def _insert_frame_l0(
        cursor,
        *,
        pending: PendingFrame,
        observations: tuple[FramedRawObservation, ...],
    ) -> None:
        if not observations:
            return
        rows = []
        for framed in observations:
            observation = framed.observation
            order = observation.source_order
            if order is None:
                raise DataTrunkError(
                    "DATA_FRAME_SOURCE_ORDER_REQUIRED",
                    "DATA_FRAME_SOURCE_ORDER_REQUIRED",
                )
            compatibility, raw = _raw_columns(observation.value)
            receive_ordinal = (
                None if order.mode is SourceOrderMode.SEQUENCE else order.secondary
            )
            rows.append(
                (
                    str(observation.observation_id),
                    str(observation.node_id),
                    str(observation.tag_id),
                    observation.source_timestamp,
                    observation.source_digest,
                    observation.source_message_id,
                    observation.source_sequence,
                    *compatibility,
                    int(framed.effective_quality),
                    observation.raw_unit,
                    *raw,
                    observation.event_time_basis,
                    observation.received_at,
                    str(pending.frame_id),
                    pending.frame_sequence,
                    framed.accepted_beat,
                    order.mode.value,
                    receive_ordinal,
                )
            )
        execute_values(
            cursor,
            """
            WITH input (
              observation_id,node_id,tag_id,observed_at,source_digest,
              source_message_id,source_sequence,value_float,value_int,
              value_bool,value_str,quality,raw_unit,raw_value_float,
              raw_value_int,raw_value_bool,raw_value_text,event_time_basis,
              event_received_at,frame_id,frame_sequence,accepted_beat,
              source_order_mode,source_receive_ordinal
            ) AS (VALUES %s), accepted AS (
              INSERT INTO t_l0_observation_dedup
                (observation_id,tag_id,observed_at,source_digest,
                 source_message_id,source_sequence)
              SELECT observation_id,tag_id,observed_at,source_digest,
                     source_message_id,source_sequence FROM input
              ON CONFLICT (source_digest) DO NOTHING
              RETURNING observation_id
            )
            INSERT INTO t_telemetry
              (ts,node_id,tag_id,value_float,value_int,value_bool,value_str,
               is_virtual,quality,observation_id,source_message_id,
               source_sequence,source_digest,raw_unit,raw_value_float,
               raw_value_int,raw_value_bool,raw_value_text,event_time_basis,
               event_received_at,frame_id,frame_sequence,accepted_beat,
               source_order_mode,source_receive_ordinal)
            SELECT observed_at,node_id,tag_id,value_float,value_int,value_bool,
                   value_str,FALSE,quality,input.observation_id,
                   source_message_id,source_sequence,source_digest,raw_unit,
                   raw_value_float,raw_value_int,raw_value_bool,raw_value_text,
                   event_time_basis,event_received_at,frame_id,frame_sequence,
                   accepted_beat,source_order_mode,source_receive_ordinal
            FROM input JOIN accepted USING(observation_id)
            """,
            rows,
            template=(
                "(%s::uuid,%s::uuid,%s::uuid,%s::timestamptz,%s::char(64),"
                "%s::text,%s::bigint,%s::double precision,%s::bigint,"
                "%s::boolean,%s::text,%s::smallint,%s::text,"
                "%s::double precision,%s::bigint,%s::boolean,%s::text,"
                "%s::text,%s::timestamptz,%s::uuid,%s::bigint,%s::bigint,"
                "%s::text,%s::bigint)"
            ),
            page_size=len(rows),
        )

    @staticmethod
    def _advance_frame_l0_latest(
        cursor,
        frame_sequence: int,
        observations: tuple[FramedRawObservation, ...],
    ) -> None:
        if not observations:
            return
        rows = []
        for framed in observations:
            observation = framed.observation
            order = observation.source_order
            if order is None:
                raise DataTrunkError(
                    "DATA_FRAME_SOURCE_ORDER_REQUIRED",
                    "DATA_FRAME_SOURCE_ORDER_REQUIRED",
                )
            compatibility, raw = _raw_columns(observation.value)
            rows.append(
                (
                    str(observation.node_id),
                    str(observation.tag_id),
                    observation.source_timestamp,
                    *compatibility,
                    int(framed.effective_quality),
                    str(observation.observation_id),
                    observation.source_message_id,
                    observation.source_sequence,
                    observation.source_digest,
                    observation.raw_unit,
                    *raw,
                    observation.event_time_basis,
                    observation.received_at,
                    _raw_order_key(observation),
                    frame_sequence,
                    order.mode.value,
                    None
                    if order.mode is SourceOrderMode.SEQUENCE
                    else order.secondary,
                )
            )
        execute_values(
            cursor,
            """
            INSERT INTO t_telemetry_latest
              (node_id,tag_id,ts,value_float,value_int,value_bool,value_str,
               quality,observation_id,source_message_id,source_sequence,
               source_digest,raw_unit,raw_value_float,raw_value_int,
               raw_value_bool,raw_value_text,event_time_basis,
               event_received_at,source_order_key,frame_sequence,
               source_order_mode,source_receive_ordinal)
            VALUES %s
            ON CONFLICT (node_id,tag_id) DO UPDATE SET
              ts=EXCLUDED.ts,value_float=EXCLUDED.value_float,
              value_int=EXCLUDED.value_int,value_bool=EXCLUDED.value_bool,
              value_str=EXCLUDED.value_str,quality=EXCLUDED.quality,
              observation_id=EXCLUDED.observation_id,
              source_message_id=EXCLUDED.source_message_id,
              source_sequence=EXCLUDED.source_sequence,
              source_digest=EXCLUDED.source_digest,raw_unit=EXCLUDED.raw_unit,
              raw_value_float=EXCLUDED.raw_value_float,
              raw_value_int=EXCLUDED.raw_value_int,
              raw_value_bool=EXCLUDED.raw_value_bool,
              raw_value_text=EXCLUDED.raw_value_text,
              event_time_basis=EXCLUDED.event_time_basis,
              event_received_at=EXCLUDED.event_received_at,
              source_order_key=EXCLUDED.source_order_key,
              frame_sequence=EXCLUDED.frame_sequence,
              source_order_mode=EXCLUDED.source_order_mode,
              source_receive_ordinal=EXCLUDED.source_receive_ordinal
            WHERE EXCLUDED.frame_sequence > t_telemetry_latest.frame_sequence
            """,
            rows,
            template=(
                "(%s::uuid,%s::uuid,%s::timestamptz,%s::double precision,"
                "%s::bigint,%s::boolean,%s::text,%s::smallint,%s::uuid,"
                "%s::text,%s::bigint,%s::char(64),%s::text,"
                "%s::double precision,%s::bigint,%s::boolean,%s::text,"
                "%s::text,%s::timestamptz,%s::text,%s::bigint,%s::text,"
                "%s::bigint)"
            ),
            page_size=len(rows),
        )

    @staticmethod
    def _load_conversion_snapshot(
        cursor,
        observations: tuple[RawObservation, ...],
        *,
        calculated_at: datetime,
    ) -> _ConversionSnapshot:
        tag_ids = tuple(sorted({item.tag_id for item in observations}, key=str))

        cursor.execute(
            """
            SELECT
              installed.id,
              installed.revision_id,
              input_binding.l0_tag_id,
              output_binding.entity_instance_id,
              output.id,
              output.entity_definition_id,
              output.data_type,
              output.unit,
              output.freshness_seconds,
              input.unit,
              numeric.scale,
              numeric."offset",
              numeric.minimum,
              numeric.maximum,
              enum_rule.output_id IS NOT NULL,
              fault_rule.delimiter
            FROM t_installed_point_processings AS installed
            JOIN t_point_processing_outputs AS output
              ON output.revision_id = installed.revision_id
            JOIN t_point_processing_output_bindings AS output_binding
              ON output_binding.installed_processing_id = installed.id
             AND output_binding.output_id = output.id
            LEFT JOIN t_numeric_transform_rules AS numeric
              ON numeric.output_id = output.id
            LEFT JOIN t_enum_transform_rules AS enum_rule
              ON enum_rule.output_id = output.id
            LEFT JOIN t_fault_code_transform_rules AS fault_rule
              ON fault_rule.output_id = output.id
            JOIN t_point_processing_inputs AS input
              ON input.id = COALESCE(
                numeric.input_id,
                enum_rule.input_id,
                fault_rule.input_id
              )
            JOIN t_point_processing_input_bindings AS input_binding
              ON input_binding.installed_processing_id = installed.id
             AND input_binding.input_id = input.id
             AND input_binding.source_kind = 'l0'
            WHERE installed.current = TRUE
              AND input_binding.l0_tag_id = ANY(%s::uuid[])
              AND num_nonnulls(
                numeric.output_id,
                enum_rule.output_id,
                fault_rule.output_id
              ) = 1
            ORDER BY output_binding.entity_instance_id, installed.id
            """,
            ([str(tag_id) for tag_id in tag_ids],),
        )
        installed_items: list[InstalledPointProcessing] = []
        rows = cursor.fetchall()
        for row in rows:
            (
                installation_id,
                revision_id,
                input_tag_id,
                entity_instance_id,
                output_id,
                definition_id,
                output_type,
                output_unit,
                freshness_seconds,
                input_unit,
                scale,
                offset,
                minimum,
                maximum,
                has_enum_rule,
                fault_delimiter,
            ) = row
            input_reference = InputReference.l0(UUID(str(input_tag_id)))
            if scale is not None:
                if output_type != ValueKind.FLOAT.value:
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "numeric transform output must be FLOAT",
                    )
                item = InstalledPointProcessing.numeric(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    input_tag_id=UUID(str(input_tag_id)),
                    output_entity_instance_id=UUID(str(entity_instance_id)),
                    output_definition_id=definition_id,
                    scale=scale,
                    offset=offset,
                    input_unit=input_unit,
                    output_unit=output_unit,
                    minimum=minimum,
                    maximum=maximum,
                )
            elif has_enum_rule:
                if output_type != ValueKind.ENUM.value:
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "enum transform output must be ENUM",
                    )
                cursor.execute(
                    """
                    SELECT raw_value, canonical_value
                    FROM t_enum_mapping_entries
                    WHERE output_id = %s
                    ORDER BY raw_value
                    """,
                    (str(output_id),),
                )
                item = InstalledPointProcessing(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    entity_instance_id=UUID(str(entity_instance_id)),
                    entity_definition_id=definition_id,
                    output_kind=ValueKind.ENUM,
                    output_unit=output_unit,
                    freshness_seconds=freshness_seconds,
                    transform=EnumTransform(
                        input=input_reference,
                        entries=dict(cursor.fetchall()),
                    ),
                )
            else:
                if (
                    output_type != ValueKind.CODE_SET.value
                    or fault_delimiter is None
                ):
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "fault-code transform output must be CODE_SET",
                    )
                cursor.execute(
                    """
                    SELECT raw_code, canonical_code
                    FROM t_fault_code_mapping_entries
                    WHERE output_id = %s
                    ORDER BY raw_code
                    """,
                    (str(output_id),),
                )
                item = InstalledPointProcessing(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    entity_instance_id=UUID(str(entity_instance_id)),
                    entity_definition_id=definition_id,
                    output_kind=ValueKind.CODE_SET,
                    output_unit=output_unit,
                    freshness_seconds=freshness_seconds,
                    transform=FaultCodeTransform(
                        input=input_reference,
                        delimiter=fault_delimiter,
                        entries=dict(cursor.fetchall()),
                    ),
                )
            installed_items.append(
                replace(item, freshness_seconds=freshness_seconds)
            )

        cursor.execute(
            """
            SELECT installed.id, installed.revision_id,
                   output_binding.entity_instance_id, output.id,
                   output.entity_definition_id, output.data_type,
                   output.unit, output.freshness_seconds
            FROM t_installed_point_processings AS installed
            JOIN t_point_processing_outputs AS output
              ON output.revision_id = installed.revision_id
            JOIN t_point_processing_output_bindings AS output_binding
              ON output_binding.installed_processing_id = installed.id
             AND output_binding.output_id = output.id
            JOIN t_boolean_set_transform_rules AS boolean_rule
              ON boolean_rule.output_id = output.id
            WHERE installed.current = TRUE
              AND EXISTS (
                SELECT 1
                FROM t_boolean_set_mapping_entries AS entry
                JOIN t_point_processing_input_bindings AS input_binding
                  ON input_binding.installed_processing_id = installed.id
                 AND input_binding.input_id = entry.input_id
                 AND input_binding.source_kind = 'l0'
                WHERE entry.output_id = output.id
                  AND input_binding.l0_tag_id = ANY(%s::uuid[])
              )
            ORDER BY output_binding.entity_instance_id, installed.id
            """,
            ([str(tag_id) for tag_id in tag_ids],),
        )
        boolean_tag_ids: set[UUID] = set()
        for (
            installation_id,
            revision_id,
            entity_instance_id,
            output_id,
            definition_id,
            output_type,
            output_unit,
            freshness_seconds,
        ) in cursor.fetchall():
            if output_type != ValueKind.CODE_SET.value:
                raise DataTrunkError(
                    "POINT_PROCESSING_CONFIGURATION_INVALID",
                    "boolean-set transform output must be CODE_SET",
                )
            cursor.execute(
                """
                SELECT input_binding.l0_tag_id, entry.canonical_code
                FROM t_boolean_set_mapping_entries AS entry
                JOIN t_point_processing_input_bindings AS input_binding
                  ON input_binding.installed_processing_id = %s
                 AND input_binding.input_id = entry.input_id
                 AND input_binding.source_kind = 'l0'
                WHERE entry.output_id = %s
                ORDER BY entry.canonical_code
                """,
                (str(installation_id), str(output_id)),
            )
            boolean_inputs = tuple(
                BooleanCodeInput(
                    input=InputReference.l0(UUID(str(input_tag_id))),
                    code=canonical_code,
                )
                for input_tag_id, canonical_code in cursor.fetchall()
            )
            boolean_tag_ids.update(item.input.source_id for item in boolean_inputs)
            installed_items.append(
                InstalledPointProcessing(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    entity_instance_id=UUID(str(entity_instance_id)),
                    entity_definition_id=definition_id,
                    output_kind=ValueKind.CODE_SET,
                    output_unit=output_unit,
                    freshness_seconds=freshness_seconds,
                    transform=BooleanSetTransform(inputs=boolean_inputs),
                )
            )

        current_inputs: dict[InputReference, RawObservation] = {}
        if boolean_tag_ids:
            cursor.execute(
                """
                SELECT latest.node_id, latest.tag_id, tag.name,
                       latest.raw_value_bool, latest.raw_unit, latest.quality,
                       latest.ts, latest.event_received_at, latest.observation_id,
                       latest.source_message_id, latest.source_sequence,
                       latest.source_digest, tag.freshness_seconds,
                       latest.event_time_basis
                FROM t_telemetry_latest AS latest
                JOIN t_tags AS tag ON tag.id = latest.tag_id
                WHERE latest.tag_id = ANY(%s::uuid[])
                ORDER BY latest.tag_id
                """,
                ([str(tag_id) for tag_id in sorted(boolean_tag_ids, key=str)],),
            )
            for row in cursor.fetchall():
                (
                    node_id,
                    input_tag_id,
                    source_key,
                    raw_value_bool,
                    raw_unit,
                    quality,
                    source_timestamp,
                    received_at,
                    observation_id,
                    source_message_id,
                    source_sequence,
                    source_digest,
                    input_freshness_seconds,
                    event_time_basis,
                ) = row
                effective_quality = TrunkQuality(quality)
                if (
                    input_freshness_seconds is not None
                    and source_timestamp
                    + timedelta(seconds=float(input_freshness_seconds))
                    <= calculated_at
                ):
                    effective_quality = min(effective_quality, TrunkQuality.STALE)
                current_inputs[InputReference.l0(UUID(str(input_tag_id)))] = RawObservation(
                    observation_id=UUID(str(observation_id)),
                    node_id=UUID(str(node_id)),
                    tag_id=UUID(str(input_tag_id)),
                    source_key=source_key,
                    value=TypedValue(ValueKind.BOOL, raw_value_bool),
                    raw_unit=raw_unit,
                    quality=effective_quality,
                    source_timestamp=source_timestamp,
                    received_at=received_at,
                    source_message_id=source_message_id,
                    source_sequence=source_sequence,
                    source_digest=source_digest.strip(),
                    event_time_basis=event_time_basis,
                )

        cursor.execute(
            """
            SELECT current_revision
            FROM t_configuration_state
            WHERE singleton = TRUE
            """
        )
        site_row = cursor.fetchone()
        if site_row is None:
            raise DataTrunkError(
                "POINT_PROCESSING_CONFIGURATION_INVALID",
                "site configuration state is unavailable",
            )
        return _ConversionSnapshot(
            installed=tuple(installed_items),
            configuration_revision=site_row[0],
            current_inputs=current_inputs,
        )

    @staticmethod
    def _ensure_runtime(cursor) -> None:
        from app.api.health import _VERSION as platform_version

        cursor.execute(
            """
            INSERT INTO t_runtime_instances (id, started_at, platform_version)
            VALUES (%s, now(), %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (str(RUNTIME_INSTANCE_ID), platform_version),
        )

    @staticmethod
    def _insert_sources(cursor, observations: tuple[L2Observation, ...]) -> None:
        for observation in observations:
            if not observation.source_observation_ids:
                continue
            cursor.execute(
                """
                SELECT DISTINCT ON (dedup.observation_id)
                       dedup.observation_id, dedup.source_digest,
                       telemetry.event_time_basis
                FROM t_l0_observation_dedup AS dedup
                JOIN t_telemetry_latest AS telemetry
                  ON telemetry.observation_id = dedup.observation_id
                WHERE dedup.observation_id = ANY(%s::uuid[])
                ORDER BY dedup.observation_id, telemetry.ts DESC
                """,
                ([str(item) for item in observation.source_observation_ids],),
            )
            l0_sources = {
                UUID(str(source_id)): (source_digest.strip(), event_time_basis)
                for source_id, source_digest, event_time_basis in cursor.fetchall()
            }
            unresolved = tuple(
                source_id for source_id in observation.source_observation_ids
                if source_id not in l0_sources
            )
            l2_sources: dict[UUID, tuple[datetime, str, str]] = {}
            if unresolved:
                cursor.execute(
                    """
                    SELECT event_id, observed_at, source_digest, event_time_basis
                    FROM t_l2_observations
                    WHERE event_id = ANY(%s::uuid[])
                    ORDER BY event_id, observed_at DESC
                    """,
                    ([str(item) for item in unresolved],),
                )
                for (
                    source_id,
                    observed_at,
                    source_digest,
                    event_time_basis,
                ) in cursor.fetchall():
                    l2_sources.setdefault(
                        UUID(str(source_id)),
                        (observed_at, source_digest.strip(), event_time_basis),
                    )
            if len(l0_sources) + len(l2_sources) != len(
                observation.source_observation_ids
            ):
                raise DataTrunkError(
                    "POINT_PROCESSING_SOURCE_MISSING",
                    "L2 source observation is unavailable",
                )
            for source_id, (source_digest, source_event_time_basis) in l0_sources.items():
                cursor.execute(
                    """
                INSERT INTO t_l2_observation_sources
                  (l2_event_id, l2_observed_at, source_kind,
                       l0_observation_id, source_digest,
                       source_event_time_basis)
                    VALUES (%s, %s, 'l0', %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(observation.event_id),
                        observation.observed_at,
                        str(source_id),
                        source_digest,
                        source_event_time_basis,
                    ),
                )
            for source_id, (
                source_observed_at,
                source_digest,
                source_event_time_basis,
            ) in l2_sources.items():
                cursor.execute(
                    """
                    INSERT INTO t_l2_observation_sources
                      (l2_event_id, l2_observed_at, source_kind,
                       source_l2_event_id, source_l2_observed_at,
                       source_digest, source_event_time_basis)
                    VALUES (%s, %s, 'l2', %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(observation.event_id),
                        observation.observed_at,
                        str(source_id),
                        source_observed_at,
                        source_digest,
                        source_event_time_basis,
                    ),
                )


def _raw_columns(
    value: TypedValue,
) -> tuple[
    tuple[float | None, int | None, bool | None, str | None],
    tuple[float | None, int | None, bool | None, str | None],
]:
    raw_float: float | None = None
    raw_int: int | None = None
    raw_bool: bool | None = None
    raw_text: str | None = None
    if value.kind is ValueKind.FLOAT and isinstance(value.value, (int, float)):
        raw_float = float(value.value)
    elif value.kind is ValueKind.INT and isinstance(value.value, int):
        raw_int = value.value
    elif value.kind is ValueKind.BOOL and isinstance(value.value, bool):
        raw_bool = value.value
    elif value.kind in {ValueKind.STRING, ValueKind.ENUM} and isinstance(
        value.value,
        str,
    ):
        raw_text = value.value
    else:
        raise DataTrunkError(
            "RAW_OBSERVATION_INVALID",
            "Raw observation has no supported typed value",
        )
    columns = (raw_float, raw_int, raw_bool, raw_text)
    return columns, columns


def _raw_order_key(observation: RawObservation) -> str:
    if observation.source_sequence is None:
        return f"D:{observation.source_digest}"
    return (
        f"S:{observation.source_sequence:020d}:"
        f"{observation.source_digest}"
    )


def _raw_value_from_columns(
    data_type: str,
    raw_float: float | None,
    raw_int: int | None,
    raw_bool: bool | None,
    raw_text: str | None,
) -> TypedValue:
    kind = ValueKind(data_type.upper())
    if kind is ValueKind.FLOAT:
        value = raw_float
    elif kind is ValueKind.INT:
        value = raw_int
    elif kind is ValueKind.BOOL:
        value = raw_bool
    elif kind in {ValueKind.STRING, ValueKind.ENUM}:
        value = raw_text
    else:
        raise DataTrunkError(
            "DATA_FRAME_RECOVERY_EVIDENCE_INVALID",
            "DATA_FRAME_RECOVERY_EVIDENCE_INVALID",
        )
    if value is None:
        raise DataTrunkError(
            "DATA_FRAME_RECOVERY_EVIDENCE_INVALID",
            "DATA_FRAME_RECOVERY_EVIDENCE_INVALID",
        )
    return TypedValue(kind, value)


def _l2_columns(
    value: TypedValue,
) -> tuple[
    float | None,
    int | None,
    Decimal | None,
    bool | None,
    str | None,
    list[str] | None,
]:
    if value.value is None:
        return None, None, None, None, None, None
    if value.kind is ValueKind.FLOAT:
        if isinstance(value.value, Decimal):
            return None, None, value.value, None, None, None
        return float(value.value), None, None, None, None, None
    if value.kind is ValueKind.INT:
        integer = int(value.value)
        if isinstance(value.value, Decimal) or not -(1 << 63) <= integer <= (1 << 63) - 1:
            return None, None, Decimal(value.value), None, None, None
        return None, integer, None, None, None, None
    if value.kind is ValueKind.BOOL:
        return None, None, None, bool(value.value), None, None
    if value.kind in {ValueKind.STRING, ValueKind.ENUM}:
        return None, None, None, None, str(value.value), None
    if value.kind is ValueKind.CODE_SET:
        return None, None, None, None, None, list(value.value)
    raise DataTrunkError(
        "POINT_PROCESSING_VALUE_INVALID",
        "Unsupported L2 typed value",
    )


def _l2_value_from_columns(
    data_type: str,
    value_float,
    value_int,
    value_numeric,
    value_bool,
    value_text,
    value_codes,
) -> TypedValue:
    kind = ValueKind(data_type)
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
    if value is None:
        raise DataTrunkError(
            "DATA_FRAME_FAILURE_BASELINE_INVALID",
            "DATA_FRAME_FAILURE_BASELINE_INVALID",
        )
    return TypedValue(kind, value)


def _decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def build_postgres_data_trunk() -> DataTrunk:
    from app.services.data_trunk_conversion import evaluate_processing
    from app.services.frame_processor import FrameProcessor
    from app.services.realtime_blackboard import RealtimeBlackboard

    repository = PostgresFrameRepository()
    writer_lease = repository.acquire_writer()
    try:
        recovery = repository.restore_blackboard()
        blackboard = RealtimeBlackboard(
            active_input_contracts=recovery.active_input_contracts,
            required_tag_ids=recovery.required_tag_ids,
            capture_beat=recovery.capture_beat,
        )
        blackboard.restore(
            recovery.observations,
            configuration_revision=recovery.configuration_revision,
        )
        processor = FrameProcessor(
            repository,
            evaluator=evaluate_processing,
            clock=lambda: datetime.now(UTC),
        )
        return DataTrunk(
            repository,
            blackboard=blackboard,
            processor=processor,
            writer_lease=writer_lease,
        )
    except Exception:
        writer_lease.close()
        raise
