"""PostgreSQL authority for dispatch-strategy lifecycle and ownership."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from decimal import Decimal
import json
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg2.extras import Json

from app.services.dispatch_strategies import (
    ControlIntentDraft,
    EntityBindingContract,
    StrategyEvaluationMutation,
    StrategyBindingDraft,
    StrategyDraft,
    StrategyInput,
    StrategyRevision,
    StrategyRuntimeState,
    StrategySnapshot,
    StrategyView,
    static_jdm_targets,
    strategy_draft_digest,
    validate_publish_bindings,
)
from app.services.gorules_adapter import compile_standard_jdm
from app.services.dispatch_strategy_workers import ControlIntent


ConnectionFactory = Callable[[], AbstractContextManager[Any]]
Compiler = Callable[[dict[str, object]], str]


class StrategyRepositoryError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(f"{code}: {message or code}")
        self.code = code


class PostgresStrategyRepository:
    """Own drafts, immutable revisions, activation and output ownership."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        compiler: Compiler = compile_standard_jdm,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory
        self._compiler = compiler

    def create_strategy(self, draft: StrategyDraft, actor: str) -> StrategyView:
        _validate_draft(draft)
        strategy_id = uuid4()
        revision_id = uuid4()
        digest = strategy_draft_digest(draft)
        with self._write() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_dispatch_strategies
                  (id,name,description,created_by,updated_by)
                VALUES(%s,%s,%s,%s,%s)
                """,
                (strategy_id, draft.name.strip(), draft.description, actor, actor),
            )
            self._insert_revision(
                cursor,
                revision_id=revision_id,
                strategy_id=strategy_id,
                revision=1,
                draft=draft,
                digest=digest,
                actor=actor,
            )
            self._replace_bindings(cursor, revision_id, draft.bindings)
            return self._get_view(connection, strategy_id)

    def save_draft(
        self,
        strategy_id: UUID,
        draft: StrategyDraft,
        expected_digest: str,
        actor: str,
    ) -> StrategyView:
        _validate_draft(draft)
        digest = strategy_draft_digest(draft)
        with self._write() as connection, connection.cursor() as cursor:
            self._lock_strategy(cursor, strategy_id)
            cursor.execute(
                f"{_REVISION_SELECT} WHERE strategy_id=%s AND lifecycle='DRAFT' FOR UPDATE",
                (strategy_id,),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"{_REVISION_SELECT} WHERE strategy_id=%s "
                    "ORDER BY revision DESC LIMIT 1 FOR UPDATE",
                    (strategy_id,),
                )
                source = cursor.fetchone()
                if source is None or str(source[7]) != expected_digest:
                    raise StrategyRepositoryError("STRATEGY_DRAFT_STALE")
                revision_id = uuid4()
                revision = int(source[2]) + 1
                self._insert_revision(
                    cursor,
                    revision_id=revision_id,
                    strategy_id=strategy_id,
                    revision=revision,
                    draft=draft,
                    digest=digest,
                    actor=actor,
                )
            else:
                if str(row[7]) != expected_digest:
                    raise StrategyRepositoryError("STRATEGY_DRAFT_STALE")
                revision_id = UUID(str(row[0]))
                cursor.execute(
                    """
                    UPDATE t_dispatch_strategy_revisions
                    SET trigger_kind=%s,site_timezone=%s,jdm_content=%s,
                        content_digest=%s,base_configuration_revision=%s,
                        created_by=%s,created_at=clock_timestamp()
                    WHERE id=%s AND lifecycle='DRAFT'
                    """,
                    (
                        draft.trigger_kind,
                        draft.site_timezone,
                        Json(dict(draft.jdm_content)),
                        digest,
                        draft.base_configuration_revision,
                        actor,
                        revision_id,
                    ),
                )
            self._replace_bindings(cursor, revision_id, draft.bindings)
            cursor.execute(
                "UPDATE t_dispatch_strategies "
                "SET name=%s,description=%s,updated_by=%s,updated_at=clock_timestamp() "
                "WHERE id=%s",
                (draft.name.strip(), draft.description, actor, strategy_id),
            )
            return self._get_view(connection, strategy_id)

    def publish(
        self,
        strategy_id: UUID,
        expected_digest: str,
        expected_configuration_revision: int,
        actor: str,
    ) -> StrategyRevision:
        with self._write() as connection, connection.cursor() as cursor:
            self._lock_strategy(cursor, strategy_id)
            cursor.execute(
                f"{_REVISION_SELECT} WHERE strategy_id=%s AND lifecycle='DRAFT' FOR UPDATE",
                (strategy_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise StrategyRepositoryError("STRATEGY_DRAFT_NOT_FOUND")
            draft = self._revision_from_row(connection, row)
            if draft.content_digest != expected_digest:
                raise StrategyRepositoryError("STRATEGY_DRAFT_STALE")
            cursor.execute(
                "SELECT current_revision FROM t_configuration_state "
                "WHERE singleton=TRUE FOR SHARE"
            )
            current = cursor.fetchone()
            if current is None or int(current[0]) != expected_configuration_revision:
                raise StrategyRepositoryError("DATA_FRAME_CONFIGURATION_STALE")
            if draft.base_configuration_revision != expected_configuration_revision:
                raise StrategyRepositoryError("DATA_FRAME_CONFIGURATION_STALE")
            self._compiler(dict(draft.jdm_content))
            contracts = self._load_entity_contracts(cursor, draft.bindings)
            validate_publish_bindings(
                draft.bindings,
                contracts,
                static_targets=static_jdm_targets(draft.jdm_content),
            )
            cursor.execute(
                """
                UPDATE t_dispatch_strategy_revisions
                SET lifecycle='PUBLISHED',published_by=%s,
                    published_at=clock_timestamp()
                WHERE id=%s AND lifecycle='DRAFT'
                """,
                (actor, draft.id),
            )
            cursor.execute(
                "UPDATE t_dispatch_strategies "
                "SET updated_by=%s,updated_at=clock_timestamp() WHERE id=%s",
                (actor, strategy_id),
            )
            cursor.execute(f"{_REVISION_SELECT} WHERE id=%s", (draft.id,))
            return self._revision_from_row(connection, cursor.fetchone())

    def enable(
        self,
        strategy_id: UUID,
        revision_id: UUID,
        actor: str,
    ) -> StrategyView:
        with self._write() as connection, connection.cursor() as cursor:
            strategy = self._lock_strategy(cursor, strategy_id)
            cursor.execute(
                "SELECT id FROM t_dispatch_strategy_revisions "
                "WHERE id=%s AND strategy_id=%s AND lifecycle='PUBLISHED'",
                (revision_id, strategy_id),
            )
            if cursor.fetchone() is None:
                raise StrategyRepositoryError("STRATEGY_PUBLISHED_REVISION_NOT_FOUND")
            active_revision_id = strategy[3]
            if active_revision_id is not None and UUID(str(active_revision_id)) != revision_id:
                cursor.execute(
                    "SELECT count(*) FROM t_dispatch_control_intents "
                    "WHERE strategy_id=%s AND revision_id=%s "
                    "AND status IN ('PENDING','IN_FLIGHT')",
                    (strategy_id, active_revision_id),
                )
                if int(cursor.fetchone()[0]) > 0:
                    raise StrategyRepositoryError("STRATEGY_REVISION_IN_FLIGHT")
            output_ids = self._output_ids(cursor, revision_id)
            if not output_ids:
                raise StrategyRepositoryError("STRATEGY_OUTPUT_REQUIRED")
            cursor.execute(
                "SELECT id FROM t_entity_instances WHERE id=ANY(%s::uuid[]) "
                "ORDER BY id FOR UPDATE",
                ([str(item) for item in output_ids],),
            )
            if len(cursor.fetchall()) != len(output_ids):
                raise StrategyRepositoryError("L2_BINDING_UNAVAILABLE")
            cursor.execute(
                "SELECT entity_instance_id,strategy_id "
                "FROM t_dispatch_strategy_owners "
                "WHERE entity_instance_id=ANY(%s::uuid[]) FOR UPDATE",
                ([str(item) for item in output_ids],),
            )
            conflicts = [row for row in cursor.fetchall() if UUID(str(row[1])) != strategy_id]
            if conflicts:
                raise StrategyRepositoryError("OUTPUT_ALREADY_OWNED")
            cursor.execute(
                "DELETE FROM t_dispatch_strategy_owners WHERE strategy_id=%s",
                (strategy_id,),
            )
            for entity_id in output_ids:
                cursor.execute(
                    "INSERT INTO t_dispatch_strategy_owners"
                    "(entity_instance_id,strategy_id,revision_id) VALUES(%s,%s,%s)",
                    (entity_id, strategy_id, revision_id),
                )
            cursor.execute(
                """
                UPDATE t_dispatch_strategies
                SET active_revision_id=%s,enabled=TRUE,runtime_health='READY',
                    failure_code=NULL,updated_by=%s,updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (revision_id, actor, strategy_id),
            )
            return self._get_view(connection, strategy_id)

    def disable(self, strategy_id: UUID, actor: str) -> StrategyView:
        with self._write() as connection, connection.cursor() as cursor:
            self._lock_strategy(cursor, strategy_id)
            cursor.execute(
                "UPDATE t_dispatch_strategies "
                "SET enabled=FALSE,updated_by=%s,updated_at=clock_timestamp() "
                "WHERE id=%s",
                (actor, strategy_id),
            )
            cursor.execute(
                "DELETE FROM t_dispatch_strategy_owners WHERE strategy_id=%s",
                (strategy_id,),
            )
            cursor.execute(
                "UPDATE t_dispatch_control_intents "
                "SET status='CANCELLED',updated_at=clock_timestamp(),"
                "last_error_code='STRATEGY_DISABLED' "
                "WHERE strategy_id=%s AND status='PENDING'",
                (strategy_id,),
            )
            return self._get_view(connection, strategy_id)

    def clear_failure(self, strategy_id: UUID, actor: str) -> StrategyView:
        with self._write() as connection, connection.cursor() as cursor:
            strategy = self._lock_strategy(cursor, strategy_id)
            if strategy[3] is None:
                raise StrategyRepositoryError("STRATEGY_ACTIVE_REVISION_REQUIRED")
            cursor.execute(
                "UPDATE t_dispatch_strategies "
                "SET runtime_health='READY',failure_code=NULL,updated_by=%s,"
                "updated_at=clock_timestamp() WHERE id=%s",
                (actor, strategy_id),
            )
            return self._get_view(connection, strategy_id)

    def get_strategy(self, strategy_id: UUID) -> StrategyView:
        with self._connection() as connection:
            return self._get_view(connection, strategy_id)

    def list_strategies(self) -> tuple[StrategyView, ...]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"{_STRATEGY_SELECT} ORDER BY updated_at DESC,id")
            return tuple(self._view_from_row(connection, row) for row in cursor.fetchall())

    def current_configuration_revision(self) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_revision FROM t_configuration_state WHERE singleton=TRUE"
            )
            row = cursor.fetchone()
        if row is None:
            raise StrategyRepositoryError("CONFIGURATION_REVISION_UNAVAILABLE")
        return int(row[0])

    def list_events(
        self,
        strategy_id: UUID,
        before_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> tuple[tuple[dict[str, object], ...], bool]:
        bounded = min(max(int(limit), 1), 100)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM t_dispatch_strategies WHERE id=%s",
                (strategy_id,),
            )
            if cursor.fetchone() is None:
                raise StrategyRepositoryError("STRATEGY_NOT_FOUND")
            cursor.execute(
                """
                SELECT event.id,event.occurred_at,event.event_kind,
                       event.trigger_kind,event.trigger_key,event.frame_sequence,
                       event.configuration_revision,event.snapshot_evidence,
                       event.decision,event.intent_summary,event.control_command_id,
                       command.status,event.reason_code
                FROM t_dispatch_strategy_events AS event
                LEFT JOIN t_control_commands AS command
                  ON command.id=event.control_command_id
                WHERE event.strategy_id=%s
                  AND (
                    %s::timestamptz IS NULL
                    OR (event.occurred_at,event.id)<(%s,%s)
                  )
                ORDER BY event.occurred_at DESC,event.id DESC
                LIMIT %s
                """,
                (strategy_id, before_at, before_at, before_id, bounded + 1),
            )
            rows = cursor.fetchall()
        has_more = len(rows) > bounded
        items = tuple(
            {
                "id": UUID(str(row[0])),
                "occurred_at": row[1],
                "event_kind": str(row[2]),
                "trigger_kind": str(row[3]),
                "trigger_key": str(row[4]),
                "frame_sequence": None if row[5] is None else int(row[5]),
                "configuration_revision": int(row[6]),
                "snapshot_evidence": dict(row[7]),
                "decision": None if row[8] is None else dict(row[8]),
                "intent_summary": list(row[9]),
                "control_command_id": None if row[10] is None else UUID(str(row[10])),
                "control_status": row[11],
                "reason_code": row[12],
            }
            for row in rows[:bounded]
        )
        return items, has_more

    def affected_strategy_ids(
        self,
        entity_ids: tuple[UUID, ...] | list[UUID],
        trigger_kind: str,
    ) -> tuple[UUID, ...]:
        if not entity_ids:
            return ()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT strategy.id
                FROM t_dispatch_strategies AS strategy
                JOIN t_dispatch_strategy_revisions AS revision
                  ON revision.id=strategy.active_revision_id
                JOIN t_dispatch_strategy_bindings AS binding
                  ON binding.revision_id=revision.id
                 AND binding.direction='INPUT'
                WHERE strategy.enabled=TRUE
                  AND revision.lifecycle='PUBLISHED'
                  AND revision.trigger_kind=%s
                  AND binding.entity_instance_id=ANY(%s::uuid[])
                ORDER BY strategy.id
                """,
                (trigger_kind, [str(item) for item in entity_ids]),
            )
            return tuple(UUID(str(row[0])) for row in cursor.fetchall())

    def active_revision(self, strategy_id: UUID) -> StrategyRevision | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""{_REVISION_SELECT}
                    WHERE id=(
                      SELECT active_revision_id FROM t_dispatch_strategies
                      WHERE id=%s AND enabled=TRUE
                    ) AND lifecycle='PUBLISHED'""",
                (strategy_id,),
            )
            row = cursor.fetchone()
            return None if row is None else self._revision_from_row(connection, row)

    def get_revision(self, revision_id: UUID) -> StrategyRevision | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"{_REVISION_SELECT} WHERE id=%s", (revision_id,))
            row = cursor.fetchone()
            return None if row is None else self._revision_from_row(connection, row)

    def runtime_state(self, strategy_id: UUID) -> StrategyRuntimeState:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT runtime_health,last_trigger_key,last_desired,last_actual,
                       last_evidence,failure_code
                FROM t_dispatch_strategies WHERE id=%s
                """,
                (strategy_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise StrategyRepositoryError("STRATEGY_NOT_FOUND")
        evidence = row[4] if isinstance(row[4], dict) else {}
        return StrategyRuntimeState(
            runtime_health=str(row[0]),
            last_trigger_key=row[1],
            last_desired=None if row[2] is None else dict(row[2]),
            last_actual=None if row[3] is None else dict(row[3]),
            block_reason=evidence.get("reason_code"),
            failure_code=row[5],
        )

    def load_snapshot(
        self,
        revision: StrategyRevision,
        frame_sequence: int | None,
        evaluated_at: datetime,
    ) -> StrategySnapshot:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute(
                """
                SELECT frame_sequence,configuration_revision
                FROM t_data_frames
                WHERE status='COMPLETE' AND (%s::bigint IS NULL OR frame_sequence<=%s)
                ORDER BY frame_sequence DESC LIMIT 1
                """,
                (frame_sequence, frame_sequence),
            )
            head = cursor.fetchone()
            if head is None:
                raise StrategyRepositoryError("COMMITTED_L2_SNAPSHOT_UNAVAILABLE")
            head_sequence = int(head[0])
            configuration_revision = int(head[1])
            cursor.execute(
                """
                SELECT binding.binding_key,binding.entity_instance_id,
                       entity.data_type,entity.unit,binding.freshness_seconds,
                       sample.value_float,sample.value_int,sample.value_numeric,
                       sample.value_bool,sample.value_text,sample.value_codes,
                       sample.quality,sample.value_observed_at,
                       sample.configuration_revision,sample.frame_sequence
                FROM t_dispatch_strategy_bindings AS binding
                JOIN t_entity_instances AS entity
                  ON entity.id=binding.entity_instance_id AND entity.active=TRUE
                LEFT JOIN LATERAL (
                  SELECT candidate.* FROM (
                    SELECT latest.value_float,latest.value_int,latest.value_numeric,
                           latest.value_bool,latest.value_text,latest.value_codes,
                           latest.quality,
                           COALESCE(latest.value_observed_at,latest.observed_at)
                             AS value_observed_at,
                           latest.configuration_revision,latest.frame_sequence
                    FROM t_l2_latest AS latest
                    WHERE latest.entity_instance_id=binding.entity_instance_id
                      AND latest.frame_sequence<=%s
                    UNION ALL
                    SELECT history.value_float,history.value_int,history.value_numeric,
                           history.value_bool,history.value_text,history.value_codes,
                           history.quality,history.observed_at AS value_observed_at,
                           history.configuration_revision,
                           history.commit_sequence AS frame_sequence
                    FROM t_l2_observations AS history
                    WHERE history.entity_instance_id=binding.entity_instance_id
                      AND history.commit_sequence<=%s
                  ) AS candidate
                  ORDER BY candidate.frame_sequence DESC LIMIT 1
                ) AS sample ON TRUE
                WHERE binding.revision_id=%s
                ORDER BY binding.direction,binding.ordinal
                """,
                (head_sequence, head_sequence, revision.id),
            )
            inputs: list[StrategyInput] = []
            for row in cursor.fetchall():
                if row[11] is None:
                    continue
                observed_at = row[12]
                quality = _quality_name(int(row[11]))
                if (
                    observed_at is None
                    or (evaluated_at - observed_at).total_seconds() > float(row[4])
                ):
                    quality = "STALE"
                inputs.append(
                    StrategyInput(
                        field_key=str(row[0]),
                        entity_instance_id=UUID(str(row[1])),
                        value=_l2_value(str(row[2]), *row[5:11]),
                        data_type=str(row[2]),
                        unit=row[3],
                        quality=quality,
                        observed_at=observed_at,
                        frame_sequence=int(row[14]),
                        configuration_revision=int(row[13]),
                    )
                )
        return StrategySnapshot(
            frame_sequence=head_sequence,
            configuration_revision=configuration_revision,
            evaluated_at=evaluated_at,
            inputs=tuple(inputs),
        )

    def has_open_intent(self, strategy_id: UUID, entity_id: UUID) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS(
                  SELECT 1 FROM t_dispatch_control_intents
                  WHERE strategy_id=%s AND entity_instance_id=%s
                    AND status IN ('PENDING','IN_FLIGHT')
                )
                """,
                (strategy_id, entity_id),
            )
            return bool(cursor.fetchone()[0])

    def commit_evaluation(self, mutation: StrategyEvaluationMutation) -> bool:
        evidence = _snapshot_evidence(mutation)
        with self._write() as connection, connection.cursor() as cursor:
            strategy = self._lock_strategy(cursor, mutation.strategy_id)
            if (
                not bool(strategy[4])
                or strategy[3] is None
                or UUID(str(strategy[3])) != mutation.revision_id
            ):
                raise StrategyRepositoryError("STRATEGY_ACTIVE_REVISION_CHANGED")
            if strategy[6] == mutation.trigger.trigger_key:
                return False
            for event in mutation.events:
                cursor.execute(
                    """
                    INSERT INTO t_dispatch_strategy_events
                      (occurred_at,id,strategy_id,revision_id,event_kind,
                       trigger_kind,trigger_key,frame_sequence,
                       configuration_revision,snapshot_evidence,decision,
                       intent_summary,reason_code)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        mutation.trigger.evaluated_at,
                        uuid4(),
                        mutation.strategy_id,
                        mutation.revision_id,
                        event.kind,
                        mutation.trigger.kind,
                        mutation.trigger.trigger_key,
                        mutation.snapshot.frame_sequence,
                        mutation.snapshot.configuration_revision,
                        Json(evidence),
                        None if mutation.decision is None else Json(_json_safe(dict(mutation.decision))),
                        Json(_intent_summary(mutation.intents)),
                        event.reason_code,
                    ),
                )
            for intent in mutation.intents:
                cursor.execute(
                    """
                    SELECT EXISTS(
                      SELECT 1 FROM t_dispatch_control_intents
                      WHERE strategy_id=%s AND entity_instance_id=%s
                        AND status IN ('PENDING','IN_FLIGHT')
                    )
                    """,
                    (mutation.strategy_id, intent.entity_instance_id),
                )
                if bool(cursor.fetchone()[0]):
                    continue
                cursor.execute(
                    """
                    INSERT INTO t_dispatch_control_intents
                      (id,strategy_id,revision_id,evaluation_key,action_id,ordinal,
                       entity_instance_id,expected_value,snapshot_evidence)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (revision_id,evaluation_key,action_id) DO NOTHING
                    """,
                    (
                        uuid4(),
                        mutation.strategy_id,
                        mutation.revision_id,
                        mutation.trigger.trigger_key,
                        intent.action_id,
                        intent.ordinal,
                        intent.entity_instance_id,
                        Json(_json_safe(intent.value)),
                        Json(evidence),
                    ),
                )
            cursor.execute(
                """
                UPDATE t_dispatch_strategies
                SET runtime_health=%s,last_trigger_key=%s,last_evaluated_at=%s,
                    last_desired=%s,last_actual=%s,last_evidence=%s,
                    failure_code=%s,updated_by='strategy-runtime',
                    updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (
                    mutation.runtime_health,
                    mutation.trigger.trigger_key,
                    mutation.trigger.evaluated_at,
                    None if mutation.desired is None else Json(_json_safe(dict(mutation.desired))),
                    None if mutation.actual is None else Json(_json_safe(dict(mutation.actual))),
                    Json(evidence),
                    mutation.failure_code,
                    mutation.strategy_id,
                ),
            )
        return True

    def fixed_tick_strategy_ids(self) -> tuple[UUID, ...]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT strategy.id
                FROM t_dispatch_strategies AS strategy
                JOIN t_dispatch_strategy_revisions AS revision
                  ON revision.id=strategy.active_revision_id
                WHERE strategy.enabled=TRUE
                  AND strategy.runtime_health<>'FAILED'
                  AND revision.lifecycle='PUBLISHED'
                  AND revision.trigger_kind='FIXED_TICK'
                ORDER BY strategy.id
                """
            )
            return tuple(UUID(str(row[0])) for row in cursor.fetchall())

    def claim_next(self, now: datetime) -> ControlIntent | None:
        with self._write() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT intent.id,intent.strategy_id,intent.revision_id,
                       revision.revision,intent.evaluation_key,intent.action_id,
                       intent.ordinal,intent.entity_instance_id,
                       intent.expected_value,intent.status,intent.attempt_count,
                       intent.control_command_id,intent.snapshot_evidence,
                       intent.next_attempt_at,strategy.enabled,
                       strategy.active_revision_id,strategy.runtime_health,
                       EXISTS(
                         SELECT 1 FROM t_dispatch_strategy_owners AS owner
                         WHERE owner.entity_instance_id=intent.entity_instance_id
                           AND owner.strategy_id=intent.strategy_id
                           AND owner.revision_id=intent.revision_id
                       ) AS owns_target,
                       configuration.current_revision
                FROM t_dispatch_control_intents AS intent
                JOIN t_dispatch_strategy_revisions AS revision
                  ON revision.id=intent.revision_id
                JOIN t_dispatch_strategies AS strategy
                  ON strategy.id=intent.strategy_id
                CROSS JOIN t_configuration_state AS configuration
                WHERE configuration.singleton=TRUE
                  AND intent.status IN ('PENDING','IN_FLIGHT')
                  AND intent.next_attempt_at<=%s
                  AND NOT EXISTS(
                    SELECT 1 FROM t_dispatch_control_intents AS previous
                    WHERE previous.revision_id=intent.revision_id
                      AND previous.evaluation_key=intent.evaluation_key
                      AND previous.ordinal<intent.ordinal
                      AND previous.status<>'CONFIRMED'
                  )
                ORDER BY intent.created_at,intent.ordinal,intent.id
                FOR UPDATE OF intent SKIP LOCKED
                LIMIT 1
                """,
                (now,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            evidence = dict(row[12])
            configuration_revision = evidence.get("configuration_revision")
            eligible = (
                bool(row[14])
                and row[15] is not None
                and UUID(str(row[15])) == UUID(str(row[2]))
                and str(row[16]) != "FAILED"
                and bool(row[17])
                and configuration_revision is not None
                and int(configuration_revision) == int(row[18])
            )
            if not eligible:
                cursor.execute(
                    """
                    UPDATE t_dispatch_control_intents
                    SET status='CANCELLED',last_error_code='STRATEGY_FENCE_CHANGED',
                        updated_at=clock_timestamp()
                    WHERE id=%s
                    """,
                    (row[0],),
                )
                return None
            if str(row[9]) == "PENDING":
                cursor.execute(
                    """
                    UPDATE t_dispatch_control_intents
                    SET status='IN_FLIGHT',attempt_count=attempt_count+1,
                        next_attempt_at=%s + interval '250 milliseconds',
                        updated_at=clock_timestamp()
                    WHERE id=%s RETURNING attempt_count
                    """,
                    (now, row[0]),
                )
                attempt_count = int(cursor.fetchone()[0])
                control_command_id = None
                status = "IN_FLIGHT"
            else:
                cursor.execute(
                    """
                    UPDATE t_dispatch_control_intents
                    SET next_attempt_at=%s + interval '250 milliseconds',
                        updated_at=clock_timestamp()
                    WHERE id=%s AND status='IN_FLIGHT'
                    """,
                    (now, row[0]),
                )
                attempt_count = int(row[10])
                control_command_id = (
                    None if row[11] is None else UUID(str(row[11]))
                )
                status = str(row[9])
            return ControlIntent(
                id=UUID(str(row[0])),
                strategy_id=UUID(str(row[1])),
                revision_id=UUID(str(row[2])),
                revision_number=int(row[3]),
                evaluation_key=str(row[4]),
                action_id=str(row[5]),
                ordinal=int(row[6]),
                entity_instance_id=UUID(str(row[7])),
                expected_value=row[8],
                status=status,
                attempt_count=attempt_count,
                control_command_id=control_command_id,
                snapshot_evidence=evidence,
                next_attempt_at=row[13],
            )

    def attach_command(
        self,
        intent_id: UUID,
        attempt_number: int,
        command_id: UUID,
    ) -> None:
        with self._write() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_dispatch_control_intents
                SET control_command_id=%s,updated_at=clock_timestamp()
                WHERE id=%s AND status='IN_FLIGHT' AND attempt_count=%s
                  AND (control_command_id IS NULL OR control_command_id=%s)
                """,
                (command_id, intent_id, attempt_number, command_id),
            )
            if cursor.rowcount != 1:
                raise StrategyRepositoryError("CONTROL_INTENT_ATTEMPT_CHANGED")

    def mark_confirmed(
        self,
        intent_id: UUID,
        command_id: UUID,
        now: datetime,
    ) -> None:
        with self._write() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_dispatch_control_intents
                SET status='CONFIRMED',confirmed_at=%s,updated_at=clock_timestamp(),
                    last_error_code=NULL
                WHERE id=%s AND status='IN_FLIGHT' AND control_command_id=%s
                """,
                (now, intent_id, command_id),
            )
            if cursor.rowcount != 1:
                raise StrategyRepositoryError("CONTROL_INTENT_STATE_CHANGED")

    def schedule_retry(
        self,
        intent_id: UUID,
        command_id: UUID,
        code: str,
        next_attempt_at: datetime,
    ) -> None:
        with self._write() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_dispatch_control_intents
                SET status='PENDING',control_command_id=NULL,next_attempt_at=%s,
                    last_error_code=%s,updated_at=clock_timestamp()
                WHERE id=%s AND status='IN_FLIGHT' AND control_command_id=%s
                  AND attempt_count<3
                """,
                (next_attempt_at, code, intent_id, command_id),
            )
            if cursor.rowcount != 1:
                raise StrategyRepositoryError("CONTROL_INTENT_STATE_CHANGED")

    def mark_failed(
        self,
        intent: ControlIntent,
        command_id: UUID,
        code: str,
        now: datetime,
    ) -> None:
        with self._write() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_dispatch_control_intents
                SET status='FAILED',last_error_code=%s,updated_at=clock_timestamp()
                WHERE id=%s AND status='IN_FLIGHT' AND control_command_id=%s
                  AND attempt_count=3
                """,
                (code, intent.id, command_id),
            )
            if cursor.rowcount != 1:
                raise StrategyRepositoryError("CONTROL_INTENT_STATE_CHANGED")
            cursor.execute(
                """
                UPDATE t_dispatch_control_intents
                SET status='CANCELLED',last_error_code='STRATEGY_FAILED',
                    updated_at=clock_timestamp()
                WHERE strategy_id=%s AND status='PENDING'
                """,
                (intent.strategy_id,),
            )
            cursor.execute(
                """
                UPDATE t_dispatch_strategies
                SET runtime_health='FAILED',failure_code=%s,
                    updated_by='strategy-runtime',updated_at=clock_timestamp()
                WHERE id=%s AND active_revision_id=%s
                """,
                (code, intent.strategy_id, intent.revision_id),
            )
            if cursor.rowcount != 1:
                raise StrategyRepositoryError("STRATEGY_ACTIVE_REVISION_CHANGED")
            evidence = dict(intent.snapshot_evidence)
            cursor.execute(
                """
                INSERT INTO t_dispatch_strategy_events
                  (occurred_at,id,strategy_id,revision_id,event_kind,trigger_kind,
                   trigger_key,frame_sequence,configuration_revision,
                   snapshot_evidence,intent_summary,control_command_id,reason_code)
                VALUES(%s,%s,%s,%s,'FAILED','CONTROL_RESULT',%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    now,
                    uuid4(),
                    intent.strategy_id,
                    intent.revision_id,
                    f"intent:{intent.id}:failed",
                    evidence.get("frame_sequence"),
                    int(evidence["configuration_revision"]),
                    Json(_json_safe(evidence)),
                    Json(
                        {
                            "intent_id": str(intent.id),
                            "attempt_count": intent.attempt_count,
                        }
                    ),
                    command_id,
                    code,
                ),
            )

    def recoverable_count(self) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_dispatch_control_intents "
                "WHERE status='IN_FLIGHT'"
            )
            return int(cursor.fetchone()[0])

    @contextmanager
    def _write(self):
        with self._connection() as connection:
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _insert_revision(
        self,
        cursor,
        *,
        revision_id: UUID,
        strategy_id: UUID,
        revision: int,
        draft: StrategyDraft,
        digest: str,
        actor: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO t_dispatch_strategy_revisions
              (id,strategy_id,revision,lifecycle,trigger_kind,site_timezone,
               jdm_content,content_digest,base_configuration_revision,created_by)
            VALUES(%s,%s,%s,'DRAFT',%s,%s,%s,%s,%s,%s)
            """,
            (
                revision_id,
                strategy_id,
                revision,
                draft.trigger_kind,
                draft.site_timezone,
                Json(dict(draft.jdm_content)),
                digest,
                draft.base_configuration_revision,
                actor,
            ),
        )

    def _replace_bindings(
        self,
        cursor,
        revision_id: UUID,
        bindings: tuple[StrategyBindingDraft, ...],
    ) -> None:
        cursor.execute(
            "DELETE FROM t_dispatch_strategy_bindings WHERE revision_id=%s",
            (revision_id,),
        )
        for binding in sorted(bindings, key=lambda item: (item.direction, item.ordinal)):
            cursor.execute(
                """
                INSERT INTO t_dispatch_strategy_bindings
                  (revision_id,direction,binding_key,ordinal,entity_instance_id,
                   expected_data_type,unit,freshness_seconds)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    revision_id,
                    binding.direction,
                    binding.binding_key,
                    binding.ordinal,
                    binding.entity_instance_id,
                    binding.expected_data_type,
                    binding.unit,
                    binding.freshness_seconds,
                ),
            )

    def _lock_strategy(self, cursor, strategy_id: UUID):
        cursor.execute(f"{_STRATEGY_SELECT} WHERE id=%s FOR UPDATE", (strategy_id,))
        row = cursor.fetchone()
        if row is None:
            raise StrategyRepositoryError("STRATEGY_NOT_FOUND")
        return row

    def _get_view(self, connection, strategy_id: UUID) -> StrategyView:
        with connection.cursor() as cursor:
            cursor.execute(f"{_STRATEGY_SELECT} WHERE id=%s", (strategy_id,))
            row = cursor.fetchone()
        if row is None:
            raise StrategyRepositoryError("STRATEGY_NOT_FOUND")
        return self._view_from_row(connection, row)

    def _view_from_row(self, connection, row) -> StrategyView:
        strategy_id = UUID(str(row[0]))
        with connection.cursor() as cursor:
            cursor.execute(
                f"{_REVISION_SELECT} WHERE strategy_id=%s AND lifecycle='DRAFT'",
                (strategy_id,),
            )
            draft_row = cursor.fetchone()
            active_row = None
            if row[3] is not None:
                cursor.execute(f"{_REVISION_SELECT} WHERE id=%s", (row[3],))
                active_row = cursor.fetchone()
            cursor.execute(
                f"{_REVISION_SELECT} WHERE strategy_id=%s AND lifecycle='PUBLISHED' "
                "ORDER BY revision DESC LIMIT 1",
                (strategy_id,),
            )
            published_row = cursor.fetchone()
        return StrategyView(
            id=strategy_id,
            name=str(row[1]),
            description=row[2],
            active_revision_id=None if row[3] is None else UUID(str(row[3])),
            enabled=bool(row[4]),
            runtime_health=str(row[5]),
            last_trigger_key=row[6],
            last_evaluated_at=row[7],
            last_desired=row[8],
            last_actual=row[9],
            last_evidence=None if row[10] is None else dict(row[10]),
            failure_code=row[11],
            created_at=row[14],
            updated_at=row[15],
            draft=None if draft_row is None else self._revision_from_row(connection, draft_row),
            active_revision=None if active_row is None else self._revision_from_row(connection, active_row),
            published_revision=None if published_row is None else self._revision_from_row(connection, published_row),
        )

    def _revision_from_row(self, connection, row) -> StrategyRevision:
        revision_id = UUID(str(row[0]))
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT direction,binding_key,ordinal,entity_instance_id,
                       expected_data_type,unit,freshness_seconds
                FROM t_dispatch_strategy_bindings
                WHERE revision_id=%s ORDER BY direction,ordinal
                """,
                (revision_id,),
            )
            bindings = tuple(
                StrategyBindingDraft(
                    direction=str(item[0]),
                    binding_key=str(item[1]),
                    ordinal=int(item[2]),
                    entity_instance_id=UUID(str(item[3])),
                    expected_data_type=str(item[4]),
                    unit=item[5],
                    freshness_seconds=float(item[6]),
                )
                for item in cursor.fetchall()
            )
        return StrategyRevision(
            id=revision_id,
            strategy_id=UUID(str(row[1])),
            revision=int(row[2]),
            lifecycle=str(row[3]),
            trigger_kind=str(row[4]),
            site_timezone=str(row[5]),
            jdm_content=dict(row[6]),
            content_digest=str(row[7]),
            base_configuration_revision=int(row[8]),
            bindings=bindings,
            created_by=str(row[9]),
            created_at=row[10],
            published_by=row[11],
            published_at=row[12],
        )

    def _load_entity_contracts(
        self,
        cursor,
        bindings: tuple[StrategyBindingDraft, ...],
    ) -> dict[UUID, EntityBindingContract]:
        entity_ids = sorted({item.entity_instance_id for item in bindings}, key=str)
        cursor.execute(
            """
            SELECT entity.id,entity.active,entity.data_type,entity.unit,
                   entity.direction,
                   (SELECT count(*) FROM t_l2_control_bindings AS control
                    WHERE control.entity_instance_id=entity.id),
                   entity.control_policy->>'minimum',
                   entity.control_policy->>'maximum'
            FROM t_entity_instances AS entity
            WHERE entity.id=ANY(%s::uuid[])
            """,
            ([str(item) for item in entity_ids],),
        )
        contracts: dict[UUID, EntityBindingContract] = {}
        for row in cursor.fetchall():
            contracts[UUID(str(row[0]))] = EntityBindingContract(
                active=bool(row[1]),
                data_type=str(row[2]),
                unit=row[3],
                direction=str(row[4]),
                confirmed_write_points=int(row[5]),
                minimum=None if row[6] is None else float(Decimal(str(row[6]))),
                maximum=None if row[7] is None else float(Decimal(str(row[7]))),
            )
        return contracts

    def _output_ids(self, cursor, revision_id: UUID) -> tuple[UUID, ...]:
        cursor.execute(
            "SELECT entity_instance_id FROM t_dispatch_strategy_bindings "
            "WHERE revision_id=%s AND direction='OUTPUT' ORDER BY entity_instance_id",
            (revision_id,),
        )
        return tuple(UUID(str(row[0])) for row in cursor.fetchall())


def _validate_draft(draft: StrategyDraft) -> None:
    if not draft.name.strip():
        raise StrategyRepositoryError("STRATEGY_NAME_REQUIRED")
    if draft.trigger_kind not in {"DATA_CHANGE", "FIXED_TICK"}:
        raise StrategyRepositoryError("STRATEGY_TRIGGER_INVALID")
    if not draft.site_timezone.strip():
        raise StrategyRepositoryError("STRATEGY_TIMEZONE_REQUIRED")
    try:
        ZoneInfo(draft.site_timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise StrategyRepositoryError("STRATEGY_TIMEZONE_INVALID") from error
    if draft.base_configuration_revision < 0:
        raise StrategyRepositoryError("CONFIGURATION_REVISION_INVALID")


def _quality_name(value: int) -> str:
    return {0: "BAD", 1: "STALE", 64: "UNCERTAIN", 192: "GOOD"}.get(
        value, "BAD"
    )


def _l2_value(
    data_type: str,
    value_float: object,
    value_int: object,
    value_numeric: object,
    value_bool: object,
    value_text: object,
    value_codes: object,
) -> object:
    if data_type == "FLOAT":
        value = value_numeric if value_numeric is not None else value_float
        return None if value is None else float(value)
    if data_type == "INT":
        value = value_numeric if value_numeric is not None else value_int
        return None if value is None else int(value)
    if data_type == "BOOL":
        return value_bool
    if data_type in {"STRING", "ENUM"}:
        return value_text
    if data_type == "CODE_SET":
        return None if value_codes is None else list(value_codes)
    raise StrategyRepositoryError("L2_DATA_TYPE_UNSUPPORTED", data_type)


def _snapshot_evidence(mutation: StrategyEvaluationMutation) -> dict[str, object]:
    return _json_safe(
        {
            "frame_sequence": mutation.snapshot.frame_sequence,
            "configuration_revision": mutation.snapshot.configuration_revision,
            "evaluated_at": mutation.snapshot.evaluated_at,
            "reason_code": mutation.reason_code,
            "inputs": [
                {
                    "binding_key": item.field_key,
                    "entity_instance_id": item.entity_instance_id,
                    "value": item.value,
                    "data_type": item.data_type,
                    "unit": item.unit,
                    "quality": item.quality,
                    "observed_at": item.observed_at,
                    "frame_sequence": item.frame_sequence,
                    "configuration_revision": item.configuration_revision,
                }
                for item in mutation.snapshot.inputs
            ],
        }
    )


def _intent_summary(intents: tuple[ControlIntentDraft, ...]) -> list[dict[str, object]]:
    return [
        {
            "action_id": item.action_id,
            "entity_instance_id": str(item.entity_instance_id),
            "value": _json_safe(item.value),
            "ordinal": item.ordinal,
        }
        for item in intents
    ]


def _json_safe(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


_STRATEGY_SELECT = """
SELECT id,name,description,active_revision_id,enabled,runtime_health,
       last_trigger_key,last_evaluated_at,last_desired,last_actual,last_evidence,
       failure_code,created_by,updated_by,created_at,updated_at
FROM t_dispatch_strategies
"""

_REVISION_SELECT = """
SELECT id,strategy_id,revision,lifecycle,trigger_kind,site_timezone,jdm_content,
       content_digest,base_configuration_revision,created_by,created_at,
       published_by,published_at
FROM t_dispatch_strategy_revisions
"""


__all__ = ["PostgresStrategyRepository", "StrategyRepositoryError"]
