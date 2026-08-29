"""PostgreSQL transaction adapter for committed-L2 JDM execution."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from typing import Any
from uuid import UUID

from psycopg2.extras import Json

from app.services.jdm_runtime import (
    JdmExecution,
    JdmModel,
    JdmRuntimeError,
)


ConnectionFactory = Callable[[], AbstractContextManager[Any]]


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


__all__ = ["PostgresJdmRepository"]
