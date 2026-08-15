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
from app.services.entity_instance_failover import EntityFailoverState
from app.services.entity_instance_catalog import (
    EntityInstanceDescriptor,
    LegacyEntityMigrationItem,
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
                    standby_tag_id = (
                        UUID(item["standby_tag_id"])
                        if item.get("standby_tag_id")
                        else None
                    )
                    proposed_failover = (
                        item.get("failover_policy") == "manual"
                        and standby_tag_id is not None
                    )
                    cur.execute(
                        """
                        SELECT active_source_role, primary_tag_id, standby_tag_id
                        FROM t_entity_failover_policies
                        WHERE entity_instance_id = %s
                        FOR UPDATE
                        """,
                        (entity_id,),
                    )
                    previous_failover = cur.fetchone()
                    policy_changes_source = previous_failover is not None and (
                        not proposed_failover
                        or previous_failover[1] != tag_id
                        or previous_failover[2] != standby_tag_id
                    )
                    if (
                        previous_failover is not None
                        and previous_failover[0] == "standby"
                        and policy_changes_source
                    ):
                        raise EntityInstanceError(
                            "ENTITY_FAILOVER_POLICY_CHANGE_REQUIRES_PRIMARY",
                            "Switch the entity instance back to primary before changing or removing its failover policy",
                        )
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
                           data_type, unit, direction, freshness_seconds, control_policy)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE
                        SET display_name = EXCLUDED.display_name,
                            data_type = EXCLUDED.data_type,
                            unit = EXCLUDED.unit,
                            direction = EXCLUDED.direction,
                            freshness_seconds = EXCLUDED.freshness_seconds,
                            control_policy = EXCLUDED.control_policy,
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
                            json.dumps(item.get("control")) if item.get("control") else None,
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
                            item.get("selection_reason")
                            or next(
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
                    try:
                        cur.execute(
                            "DELETE FROM t_entity_source_reservations "
                            "WHERE entity_instance_id = %s",
                            (entity_id,),
                        )
                        cur.execute(
                            """
                            INSERT INTO t_entity_source_reservations
                              (tag_id, entity_instance_id, source_role)
                            VALUES (%s, %s, 'primary')
                            """,
                            (tag_id, entity_id),
                        )
                        if proposed_failover:
                            cur.execute(
                                """
                                INSERT INTO t_entity_source_reservations
                                  (tag_id, entity_instance_id, source_role)
                                VALUES (%s, %s, 'standby')
                                """,
                                (standby_tag_id, entity_id),
                            )
                    except psycopg2.errors.UniqueViolation as exc:
                        raise EntityInstanceError(
                            "ENTITY_BINDING_SOURCE_IN_USE",
                            "Physical source is reserved by another entity instance",
                        ) from exc
                    if proposed_failover:
                        cur.execute(
                            """
                            INSERT INTO t_entity_failover_policies
                              (entity_instance_id, primary_tag_id, standby_tag_id)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (entity_instance_id) DO UPDATE
                            SET primary_tag_id = EXCLUDED.primary_tag_id,
                                standby_tag_id = EXCLUDED.standby_tag_id,
                                updated_at = now()
                            """,
                            (entity_id, tag_id, standby_tag_id),
                        )
                        cur.execute(
                            """
                            UPDATE t_entity_instance_bindings binding
                            SET tag_id = CASE policy.active_source_role
                              WHEN 'primary' THEN policy.primary_tag_id
                              ELSE policy.standby_tag_id
                            END
                            FROM t_entity_failover_policies policy
                            WHERE binding.entity_instance_id = policy.entity_instance_id
                              AND binding.entity_instance_id = %s
                              AND binding.active = TRUE
                            """,
                            (entity_id,),
                        )
                    else:
                        cur.execute(
                            "DELETE FROM t_entity_failover_policies "
                            "WHERE entity_instance_id = %s",
                            (entity_id,),
                        )
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

    def control_policy(self, entity_instance_id: UUID) -> dict[str, Any] | None:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT control_policy FROM t_entity_instances WHERE id = %s AND active = TRUE",
                    (entity_instance_id,),
                )
                row = cur.fetchone()
        return row[0] if row and isinstance(row[0], dict) else None

    def entity_instance_for_definition(
        self,
        device_instance_id: UUID,
        definition_id: str,
    ) -> UUID | None:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ei.id
                    FROM t_entity_instances ei
                    JOIN t_entity_instance_bindings binding
                      ON binding.entity_instance_id = ei.id AND binding.active = TRUE
                    WHERE ei.device_instance_id = %s
                      AND ei.definition_id = %s
                      AND ei.active = TRUE
                    """,
                    (device_instance_id, definition_id),
                )
                rows = cur.fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def list_instances(self) -> tuple[EntityInstanceDescriptor, ...]:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ei.id, di.id, di.slot_id, di.instance_key,
                           di.device_category, di.display_name, ei.definition_id,
                           ei.display_name, ei.data_type, ei.unit, ei.direction,
                           ei.freshness_seconds, TRUE
                    FROM t_entity_instances ei
                    JOIN t_device_instances di ON di.id = ei.device_instance_id
                    JOIN t_site_configuration_state state ON state.singleton = TRUE
                    JOIN t_site_configuration_versions site
                      ON site.version = state.current_version
                     AND di.identity_installation_id = site.entity_identity_installation_id
                    JOIN t_entity_instance_bindings binding
                      ON binding.entity_instance_id = ei.id AND binding.active = TRUE
                    JOIN t_entity_binding_confirmations confirmation
                      ON confirmation.id = binding.confirmation_audit_id
                     AND confirmation.entity_instance_id = binding.entity_instance_id
                     AND confirmation.binding_id = binding.id
                     AND confirmation.selected_tag_id = binding.tag_id
                    WHERE ei.active = TRUE AND di.active = TRUE
                    ORDER BY di.instance_key, ei.definition_id
                    """
                )
                rows = cur.fetchall()
        return tuple(EntityInstanceDescriptor(*row) for row in rows)

    def preview_legacy(self) -> tuple[LegacyEntityMigrationItem, ...]:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT legacy.id, legacy.name,
                           COALESCE(
                             array_agg(DISTINCT reservation.entity_instance_id)
                               FILTER (WHERE reservation.entity_instance_id IS NOT NULL),
                             '{}'::uuid[]
                           )
                    FROM t_entities legacy
                    LEFT JOIN t_entity_bindings old_binding
                      ON old_binding.entity_id = legacy.id
                     AND old_binding.enabled = TRUE
                    LEFT JOIN t_entity_source_reservations reservation
                      ON reservation.tag_id = old_binding.tag_id
                    WHERE legacy.enabled = TRUE
                    GROUP BY legacy.id, legacy.name
                    ORDER BY legacy.name, legacy.id
                    """
                )
                rows = cur.fetchall()
        return tuple(
            LegacyEntityMigrationItem(
                row[0],
                row[1],
                "unique" if len(row[2]) == 1 else "ambiguous" if len(row[2]) > 1 else "missing",
                tuple(sorted(row[2], key=str)),
            )
            for row in rows
        )

    def failover_state(self, entity_instance_id: UUID) -> EntityFailoverState | None:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT active_source_role, switch_count, updated_at
                    FROM t_entity_failover_policies
                    WHERE entity_instance_id = %s
                    """,
                    (entity_instance_id,),
                )
                state = cur.fetchone()
                if state is None:
                    return None
                cur.execute(
                    """
                    SELECT from_role, to_role, actor, reason, changed_at
                    FROM t_entity_failover_audit
                    WHERE entity_instance_id = %s
                    ORDER BY changed_at, id
                    """,
                    (entity_instance_id,),
                )
                audit = tuple(
                    {
                        "from_role": row[0],
                        "to_role": row[1],
                        "actor": row[2],
                        "reason": row[3],
                        "changed_at": row[4].isoformat(),
                    }
                    for row in cur.fetchall()
                )
        latest = audit[-1] if audit else {}
        return EntityFailoverState(
            entity_instance_id,
            state[0],
            state[1],
            latest.get("actor"),
            latest.get("reason"),
            state[2] if state[1] else None,
            audit,
        )

    def switch_failover(
        self,
        entity_instance_id: UUID,
        expected_current_role: str,
        target_role: str,
        actor: str,
        reason: str,
    ) -> EntityFailoverState:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT active_source_role, primary_tag_id, standby_tag_id
                    FROM t_entity_failover_policies
                    WHERE entity_instance_id = %s
                    FOR UPDATE
                    """,
                    (entity_instance_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise EntityInstanceError(
                        "ENTITY_FAILOVER_NOT_CONFIGURED",
                        "Entity instance has no manual failover policy",
                    )
                if row[0] != expected_current_role:
                    raise EntityInstanceError(
                        "ENTITY_FAILOVER_STATE_CHANGED",
                        "Entity source role changed after it was read",
                    )
                if target_role == expected_current_role or target_role not in {"primary", "standby"}:
                    raise EntityInstanceError(
                        "ENTITY_FAILOVER_TARGET_INVALID",
                        "Failover target must be the other configured role",
                    )
                target_tag_id = row[1] if target_role == "primary" else row[2]
                try:
                    cur.execute(
                        """
                        UPDATE t_entity_instance_bindings
                        SET tag_id = %s
                        WHERE entity_instance_id = %s AND active = TRUE
                        """,
                        (target_tag_id, entity_instance_id),
                    )
                except psycopg2.errors.UniqueViolation as exc:
                    if getattr(exc.diag, "constraint_name", None) != (
                        "uq_entity_tag_active_primary"
                    ):
                        raise
                    raise EntityInstanceError(
                        "ENTITY_FAILOVER_SOURCE_IN_USE",
                        "Failover target is active for another entity instance",
                    ) from exc
                cur.execute(
                    """
                    UPDATE t_entity_failover_policies
                    SET active_source_role = %s, switch_count = switch_count + 1,
                        updated_at = now()
                    WHERE entity_instance_id = %s
                    """,
                    (target_role, entity_instance_id),
                )
                cur.execute(
                    """
                    INSERT INTO t_entity_failover_audit
                      (entity_instance_id, from_role, to_role, actor, reason)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (entity_instance_id, expected_current_role, target_role, actor, reason),
                )
        return self.failover_state(entity_instance_id)  # type: ignore[return-value]


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

    def history(self, tag_id: UUID, range_key: str) -> list[SourceObservation]:
        interval = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}.get(range_key)
        if interval is None:
            raise ValueError("Unsupported history range")
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT tag_id, ts, value_float, value_int, value_bool,
                           value_str, quality
                    FROM t_telemetry
                    WHERE tag_id = %s AND ts > NOW() - %s::interval
                    ORDER BY ts ASC
                    LIMIT 2000
                    """,
                    (tag_id, interval),
                )
                rows = cur.fetchall()
        return [
            SourceObservation(
                row[0], row[1], next((candidate for candidate in row[2:6] if candidate is not None), None), row[6]
            )
            for row in rows
        ]
