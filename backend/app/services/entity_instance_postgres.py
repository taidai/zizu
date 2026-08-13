"""实体实例 Registry 的 Postgres 来源、持久化与遥测 Adapter。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import psycopg2
from typing import Any
from uuid import UUID

from app.services.entity_instance_registry import (
    ApplyOutcome,
    EntityInstanceError,
    EntityInstancePlan,
    ResolvedEntitySource,
    SourceDescriptor,
    entity_instance_plan_from_dict,
)
from app.services.entity_instance_runtime import SourceObservation


@contextmanager
def _connection(transaction: Any | None = None):
    if transaction is not None:
        yield transaction
        return
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


class PostgresSourceCatalog:
    """将点位/节点表规范化为只读来源目录。"""

    @staticmethod
    def _rows(transaction: Any | None = None) -> list[tuple[Any, ...]]:
        with _connection(transaction) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.id, COALESCE(n.source_catalog_key, n.name),
                           n.name, t.name, t.data_type,
                           COALESCE(t.unit_to, t.unit), t.read_write, t.enabled
                    FROM t_tags t
                    JOIN t_nodes n ON n.id = t.node_id
                    WHERE n.enabled = TRUE
                      AND (
                        n.source_catalog_key IS NOT NULL
                        OR NOT EXISTS (
                            SELECT 1 FROM t_nodes duplicate
                            WHERE duplicate.name = n.name
                              AND duplicate.id <> n.id
                        )
                      )
                    ORDER BY t.id
                    """
                )
                return list(cur.fetchall())

    def version(self, transaction: Any | None = None) -> str:
        content = [
            [str(row[0]), *row[1:]]
            for row in self._rows(transaction)
        ]
        return hashlib.sha256(
            json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    def list_sources(
        self,
        transaction: Any | None = None,
    ) -> tuple[SourceDescriptor, ...]:
        return tuple(
            SourceDescriptor(
                tag_id=row[0],
                device_key=row[1],
                device_name=row[2],
                tag_name=row[3],
                data_type=row[4],
                unit=row[5],
                direction=row[6],
                enabled=bool(row[7]),
            )
            for row in self._rows(transaction)
        )


class PostgresEntityInstanceRepository:
    """候选证据在安装计划；此 Adapter 只持久运行对象和确认事实。"""

    def save_plan(self, plan: EntityInstancePlan) -> EntityInstancePlan:
        # Approval is persisted by the enclosing installation-plan repository.
        # Keeping a second in-process copy here would create a bypass after the
        # package or persisted plan changed.
        return plan

    def get_approved_plan(
        self,
        plan_id: UUID,
        transaction: Any | None = None,
    ) -> EntityInstancePlan | None:
        """Load only a persisted ready plan whose validated package still matches."""
        with _connection(transaction) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ip.entity_plan
                    FROM t_solution_install_plans ip
                    JOIN t_solution_packages package
                      ON package.id = ip.package_record_id
                     AND package.digest = ip.package_digest
                    WHERE ip.entity_plan->>'id' = %s
                      AND ip.entity_plan->>'package_digest' = ip.package_digest
                      AND ip.status = 'ready'
                      AND jsonb_array_length(ip.blockers) = 0
                    """,
                    (str(plan_id),),
                )
                row = cur.fetchone()
        if row is None or not isinstance(row[0], dict):
            return None
        return entity_instance_plan_from_dict(row[0])

    def apply_plan(
        self,
        plan: EntityInstancePlan,
        actor: str,
        transaction: Any | None = None,
    ) -> ApplyOutcome:
        device_ids: list[UUID] = []
        entity_ids: list[UUID] = []
        binding_ids: list[UUID] = []
        with _connection(transaction) as conn:
            with conn.cursor() as cur:
                for item in plan.items:
                    device_id = UUID(item["device_instance_id"])
                    entity_id = UUID(item["entity_instance_id"])
                    binding_id = UUID(item["binding_id"])
                    audit_id = UUID(item["confirmation_audit_id"])
                    tag_id = UUID(item["selected_tag_id"])
                    cur.execute(
                        """
                        INSERT INTO t_device_instances
                          (id, identity_installation_id, slot_id, instance_key,
                           device_category, display_name)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE
                        SET display_name = EXCLUDED.display_name,
                            device_category = EXCLUDED.device_category,
                            updated_at = now()
                        """,
                        (
                            device_id,
                            plan.installation_id,
                            item["slot_id"],
                            item["instance_key"],
                            item["device_category"],
                            item["device_display_name"],
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO t_entity_instances
                          (id, device_instance_id, definition_id, display_name,
                           data_type, unit, direction, freshness_seconds)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE
                        SET display_name = EXCLUDED.display_name,
                            data_type = EXCLUDED.data_type,
                            unit = EXCLUDED.unit,
                            direction = EXCLUDED.direction,
                            freshness_seconds = EXCLUDED.freshness_seconds,
                            updated_at = now()
                        """,
                        (
                            entity_id,
                            device_id,
                            item["definition_id"],
                            item["definition_display_name"],
                            item["data_type"],
                            item.get("unit"),
                            item["direction"],
                            item["freshness_seconds"],
                        ),
                    )
                    try:
                        cur.execute(
                            """
                            INSERT INTO t_entity_instance_bindings
                              (id, entity_instance_id, tag_id, matcher_id,
                               confirmation_audit_id)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (entity_instance_id) WHERE active = TRUE
                            DO UPDATE SET tag_id = EXCLUDED.tag_id,
                                          matcher_id = EXCLUDED.matcher_id,
                                          confirmation_audit_id = EXCLUDED.confirmation_audit_id
                            """,
                            (binding_id, entity_id, tag_id, item["matcher_id"], audit_id),
                        )
                    except psycopg2.errors.UniqueViolation as exc:
                        if getattr(exc.diag, "constraint_name", None) != (
                            "uq_entity_tag_active_primary"
                        ):
                            raise
                        raise EntityInstanceError(
                            "ENTITY_BINDING_SOURCE_IN_USE",
                            "Physical source already has an active primary binding",
                        ) from exc
                    cur.execute(
                        """
                        INSERT INTO t_entity_binding_confirmations
                          (id, entity_instance_id, binding_id, actor, matcher_id,
                           reason, plan_digest, selected_tag_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            audit_id,
                            entity_id,
                            binding_id,
                            actor,
                            item["matcher_id"],
                            next(
                                candidate["reason"]
                                for candidate in item["candidates"]
                                if candidate["tag_id"] == item["selected_tag_id"]
                            ),
                            plan.digest,
                            tag_id,
                        ),
                    )
                    device_ids.append(device_id)
                    entity_ids.append(entity_id)
                    binding_ids.append(binding_id)
        return ApplyOutcome(
            plan.id,
            tuple(dict.fromkeys(device_ids)),
            tuple(entity_ids),
            tuple(binding_ids),
        )

    def resolve(self, entity_instance_id: UUID) -> ResolvedEntitySource | None:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ei.id, ei.definition_id, di.instance_key, di.id,
                           b.id, b.tag_id, b.matcher_id, b.confirmation_audit_id,
                           ei.data_type, ei.unit, ei.direction, ei.freshness_seconds
                    FROM t_entity_instances ei
                    JOIN t_device_instances di ON di.id = ei.device_instance_id
                    JOIN t_entity_instance_bindings b
                      ON b.entity_instance_id = ei.id AND b.active = TRUE
                    WHERE ei.id = %s AND ei.active = TRUE AND di.active = TRUE
                    """,
                    (entity_instance_id,),
                )
                row = cur.fetchone()
        return ResolvedEntitySource(*row) if row else None


class PostgresObservationCatalog:
    def latest(self, tag_id: UUID) -> SourceObservation | None:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT tag_id, ts, value_float, value_int, value_bool,
                           value_str, quality
                    FROM t_telemetry_latest WHERE tag_id = %s
                    """,
                    (tag_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        value = next((candidate for candidate in row[2:6] if candidate is not None), None)
        return SourceObservation(row[0], row[1], value, row[6])
