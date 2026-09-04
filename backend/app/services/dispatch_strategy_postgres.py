"""PostgreSQL authority for dispatch-strategy lifecycle and ownership."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from decimal import Decimal
import json
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg2.extras import Json

from app.services.dispatch_strategies import (
    EntityBindingContract,
    StrategyBindingDraft,
    StrategyDraft,
    StrategyRevision,
    StrategyView,
    static_jdm_targets,
    strategy_draft_digest,
    validate_publish_bindings,
)
from app.services.gorules_adapter import compile_standard_jdm


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
