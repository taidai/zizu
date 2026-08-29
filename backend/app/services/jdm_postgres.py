"""PostgreSQL transaction adapter for committed-L2 JDM execution."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from psycopg2.extras import Json

from app.services.jdm_runtime import (
    JdmExecution,
    JdmModel,
    JdmRuntimeError,
)
from app.services.configuration_revision_postgres import (
    PostgresConfigurationRevisions,
)


ConnectionFactory = Callable[[], AbstractContextManager[Any]]


class JdmRuleError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PostgresJdmRules:
    """Own JDM model CRUD and its global configuration revision transaction."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory
        self._revisions = PostgresConfigurationRevisions()

    def current_revision(self) -> int:
        with self._connection() as connection:
            return self._revisions.current(transaction=connection)

    def list(self, enabled: bool | None = None) -> tuple[dict[str, Any], ...]:
        where = "" if enabled is None else " WHERE enabled=%s"
        parameters = () if enabled is None else (enabled,)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,name,rule_type,jdm_content,version,enabled,"
                "configuration_revision,created_at,updated_at "
                f"FROM t_rules{where} ORDER BY updated_at DESC,id",
                parameters,
            )
            return _rule_rows(cursor)

    def get(self, rule_id: UUID) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,name,rule_type,jdm_content,version,enabled,"
                "configuration_revision,created_at,updated_at "
                "FROM t_rules WHERE id=%s",
                (str(rule_id),),
            )
            rows = _rule_rows(cursor)
        return rows[0] if rows else None

    def create(
        self,
        *,
        name: str,
        rule_type: str,
        jdm_content: dict[str, Any],
        enabled: bool,
        references: tuple[tuple[str, str, UUID], ...],
        actor: str,
        base_revision: int,
    ) -> dict[str, Any]:
        _require_runtime_rule_type(rule_type)
        rule_id = uuid4()
        content = {
            "name": name,
            "rule_type": rule_type,
            "jdm_content": jdm_content,
            "enabled": enabled,
            "version": 1,
        }
        with self._write_transaction() as connection:
            revision = self._revisions.publish(
                transaction=connection,
                base_revision=base_revision,
                actor=actor,
                action="jdm_rule.create",
                resource_kind="jdm_rule",
                resource_id=str(rule_id),
                before_digest=None,
                after_digest=_digest(content),
                details={},
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_rules
                      (id,name,rule_type,jdm_content,version,enabled,
                       configuration_revision,created_at,updated_at)
                    VALUES(%s,%s,%s,%s,1,%s,%s,clock_timestamp(),clock_timestamp())
                    RETURNING id,name,rule_type,jdm_content,version,enabled,
                              configuration_revision,created_at,updated_at
                    """,
                    (
                        str(rule_id),
                        name,
                        rule_type,
                        Json(jdm_content),
                        enabled,
                        revision,
                    ),
                )
                row = _rule_rows(cursor)[0]
                _replace_references(cursor, rule_id, references)
        return row

    def update(
        self,
        *,
        rule_id: UUID,
        changes: dict[str, Any],
        references: tuple[tuple[str, str, UUID], ...] | None,
        actor: str,
        base_revision: int,
    ) -> dict[str, Any]:
        with self._write_transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id,name,rule_type,jdm_content,version,enabled,"
                    "configuration_revision,created_at,updated_at "
                    "FROM t_rules WHERE id=%s FOR UPDATE",
                    (str(rule_id),),
                )
                rows = _rule_rows(cursor)
                if not rows:
                    raise JdmRuleError("JDM_RULE_NOT_FOUND")
                before = rows[0]
            if before["rule_type"] not in {"control", "linkage"}:
                raise JdmRuleError("JDM_RULE_LEGACY_READ_ONLY")
            rule_type = str(changes.get("rule_type", before["rule_type"]))
            _require_runtime_rule_type(rule_type)
            after = {
                **before,
                **changes,
                "rule_type": rule_type,
                "version": int(before["version"]) + 1,
            }
            revision = self._revisions.publish(
                transaction=connection,
                base_revision=base_revision,
                actor=actor,
                action="jdm_rule.update",
                resource_kind="jdm_rule",
                resource_id=str(rule_id),
                before_digest=_digest(before),
                after_digest=_digest(after),
                details={},
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE t_rules
                    SET name=%s,rule_type=%s,jdm_content=%s,enabled=%s,
                        version=version+1,configuration_revision=%s,
                        updated_at=clock_timestamp()
                    WHERE id=%s
                    RETURNING id,name,rule_type,jdm_content,version,enabled,
                              configuration_revision,created_at,updated_at
                    """,
                    (
                        after["name"],
                        after["rule_type"],
                        Json(after["jdm_content"]),
                        after["enabled"],
                        revision,
                        str(rule_id),
                    ),
                )
                row = _rule_rows(cursor)[0]
                if references is not None:
                    _replace_references(cursor, rule_id, references)
        return row

    def delete(
        self,
        *,
        rule_id: UUID,
        actor: str,
        base_revision: int,
    ) -> dict[str, Any]:
        with self._write_transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id,name,rule_type,jdm_content,version,enabled,"
                    "configuration_revision,created_at,updated_at "
                    "FROM t_rules WHERE id=%s FOR UPDATE",
                    (str(rule_id),),
                )
                rows = _rule_rows(cursor)
                if not rows:
                    raise JdmRuleError("JDM_RULE_NOT_FOUND")
                before = rows[0]
                if before["rule_type"] not in {"control", "linkage"}:
                    raise JdmRuleError("JDM_RULE_LEGACY_READ_ONLY")
            tombstone = {"deleted": str(rule_id), "version": before["version"]}
            revision = self._revisions.publish(
                transaction=connection,
                base_revision=base_revision,
                actor=actor,
                action="jdm_rule.delete",
                resource_kind="jdm_rule",
                resource_id=str(rule_id),
                before_digest=_digest(before),
                after_digest=_digest(tombstone),
                details=tombstone,
            )
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM t_rules WHERE id=%s", (str(rule_id),))
        return {
            "status": "deleted",
            "id": str(rule_id),
            "configuration_revision": revision,
        }

    def executions(
        self,
        rule_id: UUID,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,rule_id,rule_version,frame_id,frame_sequence,
                       configuration_revision,model_digest,status,reason_code,
                       inputs,outputs,actions,executed_at
                FROM t_jdm_executions
                WHERE rule_id=%s
                ORDER BY frame_sequence DESC,id DESC
                LIMIT %s
                """,
                (str(rule_id), max(1, min(limit, 100))),
            )
            columns = [item[0] for item in cursor.description]
            return tuple(dict(zip(columns, row)) for row in cursor.fetchall())

    @contextmanager
    def _write_transaction(self):
        with self._connection() as connection:
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()


class PostgresJdmRepository:
    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory

    @contextmanager
    def transaction(self):
        with self._connection() as connection:
            try:
                yield _PostgresJdmTransaction(connection)
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()


class _PostgresJdmTransaction:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def begin_committed_frame(
        self,
        consumer_key: str,
        frame_id: UUID,
        frame_sequence: int,
        configuration_revision: int,
    ) -> bool:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_revision FROM t_configuration_state "
                "WHERE singleton=TRUE FOR SHARE"
            )
            row = cursor.fetchone()
            current_revision = None if row is None else int(row[0])
            if current_revision != configuration_revision:
                raise JdmRuntimeError("JDM_FRAME_CONFIGURATION_MISMATCH")
            cursor.execute(
                """
                INSERT INTO t_committed_frame_consumers
                  (consumer_key,frame_id,frame_sequence,configuration_revision)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT (consumer_key,frame_id) DO NOTHING
                RETURNING frame_id
                """,
                (
                    consumer_key,
                    str(frame_id),
                    frame_sequence,
                    configuration_revision,
                ),
            )
            return cursor.fetchone() is not None

    def active_models(
        self,
        configuration_revision: int,
    ) -> tuple[JdmModel, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,version,configuration_revision,jdm_content
                FROM t_rules
                WHERE enabled=TRUE
                  AND rule_type IN ('control','linkage')
                  AND configuration_revision <= %s
                ORDER BY id
                """,
                (configuration_revision,),
            )
            rows = cursor.fetchall()
        return tuple(
            JdmModel(
                id=UUID(str(row[0])),
                version=int(row[1]),
                configuration_revision=int(row[2]),
                content=dict(row[3]),
            )
            for row in rows
        )

    def save_execution(self, execution: JdmExecution) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_jdm_executions
                  (id,rule_id,rule_version,frame_id,frame_sequence,
                   configuration_revision,model_digest,status,reason_code,
                   inputs,outputs,actions)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(execution.id),
                    str(execution.rule_id),
                    execution.rule_version,
                    str(execution.frame_id),
                    execution.frame_sequence,
                    execution.configuration_revision,
                    execution.model_digest,
                    execution.status,
                    execution.reason_code,
                    Json(execution.inputs),
                    Json(execution.outputs),
                    Json(execution.actions),
                ),
            )


def _require_runtime_rule_type(rule_type: str) -> None:
    if rule_type not in {"control", "linkage"}:
        raise JdmRuleError("JDM_RULE_TYPE_UNSUPPORTED")


def _replace_references(
    cursor,
    rule_id: UUID,
    references: tuple[tuple[str, str, UUID], ...],
) -> None:
    cursor.execute(
        "DELETE FROM t_rule_entity_instance_refs WHERE rule_id=%s",
        (str(rule_id),),
    )
    for reference_kind, reference_key, entity_instance_id in references:
        cursor.execute(
            """
            INSERT INTO t_rule_entity_instance_refs
              (rule_id,reference_kind,reference_key,entity_instance_id)
            VALUES(%s,%s,%s,%s)
            """,
            (
                str(rule_id),
                reference_kind,
                reference_key,
                str(entity_instance_id),
            ),
        )


def _rule_rows(cursor) -> tuple[dict[str, Any], ...]:
    columns = [item[0] for item in cursor.description]
    return tuple(dict(zip(columns, row)) for row in cursor.fetchall())


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "JdmRuleError",
    "PostgresJdmRepository",
    "PostgresJdmRules",
]
