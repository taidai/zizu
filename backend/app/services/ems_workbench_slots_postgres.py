"""PostgreSQL adapter for auditable fixed EMS workbench slot bindings."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from psycopg2.extras import Json

from app.services.alarm_configuration import canonical_digest
from app.services.configuration_revision_postgres import PostgresConfigurationRevisions
from app.services.ems_workbench_slots import (
    EmsWorkbenchSlotError,
    WORKBENCH_SLOT_SPEC_BY_KEY,
    WorkbenchSlotWriteReceipt,
)


class PostgresWorkbenchSlotRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection_factory = connection_factory
        self._revisions = PostgresConfigurationRevisions()

    def list_manual_bindings(self) -> Mapping[str, UUID]:
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT slot_key,entity_instance_id "
                    "FROM t_ems_workbench_slot_bindings ORDER BY slot_key"
                )
                return {str(row[0]): UUID(str(row[1])) for row in cursor.fetchall()}

    def find_replay(
        self,
        *,
        slot_key: str,
        entity_instance_id: UUID | None,
        base_configuration_revision: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkbenchSlotWriteReceipt | None:
        if slot_key not in WORKBENCH_SLOT_SPEC_BY_KEY:
            raise EmsWorkbenchSlotError(
                "WORKBENCH_SLOT_NOT_FOUND",
                "EMS workbench slot is not defined",
            )
        normalized_actor, normalized_key, request_digest = _request_identity(
            slot_key=slot_key,
            entity_instance_id=entity_instance_id,
            base_configuration_revision=base_configuration_revision,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT request_digest,response "
                    "FROM t_ems_workbench_slot_idempotency "
                    "WHERE actor=%s AND idempotency_key=%s",
                    (normalized_actor, normalized_key),
                )
                replay = cursor.fetchone()
        return _replay_receipt(replay, request_digest)

    def set_binding(
        self,
        *,
        slot_key: str,
        entity_instance_id: UUID | None,
        base_configuration_revision: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkbenchSlotWriteReceipt:
        spec = WORKBENCH_SLOT_SPEC_BY_KEY.get(slot_key)
        if spec is None:
            raise EmsWorkbenchSlotError(
                "WORKBENCH_SLOT_NOT_FOUND",
                "EMS workbench slot is not defined",
            )
        normalized_actor, normalized_key, request_digest = _request_identity(
            slot_key=slot_key,
            entity_instance_id=entity_instance_id,
            base_configuration_revision=base_configuration_revision,
            actor=actor,
            idempotency_key=idempotency_key,
        )

        with self._connection_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"{normalized_actor}:{normalized_key}",),
                    )
                    cursor.execute(
                        "SELECT request_digest,response "
                        "FROM t_ems_workbench_slot_idempotency "
                        "WHERE actor=%s AND idempotency_key=%s",
                        (normalized_actor, normalized_key),
                    )
                    replay = cursor.fetchone()
                    replay_receipt = _replay_receipt(replay, request_digest)
                    if replay_receipt is not None:
                        connection.commit()
                        return replay_receipt

                    if entity_instance_id is not None:
                        self._lock_and_validate_target(
                            cursor,
                            entity_instance_id=entity_instance_id,
                            required_unit=spec.unit,
                        )

                    cursor.execute(
                        "SELECT entity_instance_id "
                        "FROM t_ems_workbench_slot_bindings "
                        "WHERE slot_key=%s FOR UPDATE",
                        (slot_key,),
                    )
                    before = cursor.fetchone()
                    before_entity_id = str(before[0]) if before is not None else None
                    after_entity_id = (
                        str(entity_instance_id)
                        if entity_instance_id is not None
                        else None
                    )
                    revision = self._revisions.publish(
                        transaction=connection,
                        base_revision=base_configuration_revision,
                        actor=normalized_actor,
                        action=(
                            "ems_workbench_slot.bind"
                            if entity_instance_id is not None
                            else "ems_workbench_slot.clear"
                        ),
                        resource_kind="ems_workbench_slot",
                        resource_id=slot_key,
                        before_digest=canonical_digest(
                            {
                                "slot_key": slot_key,
                                "entity_instance_id": before_entity_id,
                            }
                        ),
                        after_digest=canonical_digest(
                            {
                                "slot_key": slot_key,
                                "entity_instance_id": after_entity_id,
                            }
                        ),
                        details={
                            "slot_key": slot_key,
                            "previous_entity_instance_id": before_entity_id,
                            "entity_instance_id": after_entity_id,
                        },
                    )
                    if entity_instance_id is None:
                        cursor.execute(
                            "DELETE FROM t_ems_workbench_slot_bindings "
                            "WHERE slot_key=%s",
                            (slot_key,),
                        )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO t_ems_workbench_slot_bindings
                              (slot_key,entity_instance_id,configuration_revision,
                               created_by,updated_by)
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT(slot_key) DO UPDATE SET
                              entity_instance_id=EXCLUDED.entity_instance_id,
                              configuration_revision=EXCLUDED.configuration_revision,
                              updated_by=EXCLUDED.updated_by,
                              updated_at=clock_timestamp()
                            """,
                            (
                                slot_key,
                                entity_instance_id,
                                revision,
                                normalized_actor,
                                normalized_actor,
                            ),
                        )
                    response = {
                        "slot_key": slot_key,
                        "entity_instance_id": after_entity_id,
                        "configuration_revision": revision,
                    }
                    cursor.execute(
                        """
                        INSERT INTO t_ems_workbench_slot_idempotency
                          (actor,idempotency_key,request_digest,
                           configuration_revision,response)
                        VALUES (%s,%s,%s,%s,%s)
                        """,
                        (
                            normalized_actor,
                            normalized_key,
                            request_digest,
                            revision,
                            Json(response),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return WorkbenchSlotWriteReceipt(
            slot_key=slot_key,
            entity_instance_id=entity_instance_id,
            configuration_revision=revision,
            replayed=False,
        )

    @staticmethod
    def _lock_and_validate_target(
        cursor: Any,
        *,
        entity_instance_id: UUID,
        required_unit: str,
    ) -> None:
        cursor.execute(
            """
            SELECT entity.data_type,entity.unit
            FROM t_entity_instances AS entity
            JOIN t_nodes AS node ON node.id=entity.node_id
            WHERE entity.id=%s
              AND entity.active=TRUE
              AND node.enabled=TRUE
              AND EXISTS (
                SELECT 1
                FROM t_point_processing_output_bindings AS output
                JOIN t_installed_point_processings AS installed
                  ON installed.id=output.installed_processing_id
                 AND installed.current=TRUE
                WHERE output.entity_instance_id=entity.id
              )
            FOR SHARE OF entity,node
            """,
            (entity_instance_id,),
        )
        target = cursor.fetchone()
        if target is None:
            raise EmsWorkbenchSlotError(
                "WORKBENCH_SLOT_ENTITY_NOT_AVAILABLE",
                "EMS workbench slot target is missing, inactive, or unconfirmed",
            )
        if str(target[0]).upper() not in {"FLOAT", "INT"}:
            raise EmsWorkbenchSlotError(
                "WORKBENCH_SLOT_ENTITY_TYPE_INVALID",
                "EMS workbench slot target must be numeric",
            )
        if target[1] != required_unit:
            raise EmsWorkbenchSlotError(
                "WORKBENCH_SLOT_ENTITY_UNIT_INVALID",
                f"EMS workbench slot target unit must be {required_unit}",
            )


def _request_identity(
    *,
    slot_key: str,
    entity_instance_id: UUID | None,
    base_configuration_revision: int,
    actor: str,
    idempotency_key: str,
) -> tuple[str, str, str]:
    normalized_actor = actor.strip()
    normalized_key = idempotency_key.strip()
    if not normalized_actor or not normalized_key or len(normalized_key) > 200:
        raise EmsWorkbenchSlotError(
            "WORKBENCH_SLOT_REQUEST_INVALID",
            "Actor and idempotency key are required",
        )
    return (
        normalized_actor,
        normalized_key,
        canonical_digest(
            {
                "slot_key": slot_key,
                "entity_instance_id": (
                    str(entity_instance_id)
                    if entity_instance_id is not None
                    else None
                ),
                "base_configuration_revision": base_configuration_revision,
            }
        ),
    )


def _replay_receipt(
    replay: tuple | None,
    request_digest: str,
) -> WorkbenchSlotWriteReceipt | None:
    if replay is None:
        return None
    if str(replay[0]) != request_digest:
        raise EmsWorkbenchSlotError(
            "WORKBENCH_SLOT_IDEMPOTENCY_CONFLICT",
            "Idempotency key was already used for another request",
        )
    response = replay[1]
    return WorkbenchSlotWriteReceipt(
        slot_key=str(response["slot_key"]),
        entity_instance_id=(
            UUID(response["entity_instance_id"])
            if response.get("entity_instance_id") is not None
            else None
        ),
        configuration_revision=int(response["configuration_revision"]),
        replayed=True,
    )


__all__ = ["PostgresWorkbenchSlotRepository"]
