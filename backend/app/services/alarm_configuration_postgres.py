"""PostgreSQL persistence for L2-only alarm configuration."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from typing import Any, Callable, Iterator
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg2
from psycopg2.extras import Json, register_uuid

from app.services.alarm_configuration import (
    AlarmConfiguration,
    AlarmConfigurationError,
    AlarmConfigurationPlan,
    AlarmConfigurationPlanItem,
    AlarmRule,
    AlarmRuleGroup,
    AlarmRuleSetRevision,
    AppliedAlarmConfiguration,
    EntitySelection,
    ResolvedAlarmEntity,
    canonical_digest,
)
from app.services.configuration_revision_postgres import PostgresConfigurationRevisions


ConnectionFactory = Callable[[], Any]


def _json_value(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(value))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _rule_from_json(value: dict[str, Any]) -> AlarmRule:
    fault_map_id = value.get("fault_map_id")
    notification_config_id = value.get("http_notification_config_id")
    return AlarmRule(
        id=value["id"], name=value["name"], severity=value["severity"],
        trigger=dict(value["trigger"]),
        trigger_duration_seconds=float(value["trigger_duration_seconds"]),
        recovery=dict(value["recovery"]),
        recovery_duration_seconds=float(value["recovery_duration_seconds"]),
        notification_throttle_seconds=float(value["notification_throttle_seconds"]),
        unit=value.get("unit"),
        fault_map_id=UUID(fault_map_id) if fault_map_id else None,
        http_notification_config_id=(
            UUID(notification_config_id) if notification_config_id else None
        ),
    )


def _revision_from_json(value: dict[str, Any]) -> AlarmRuleSetRevision:
    return AlarmRuleSetRevision(
        rule_set_id=UUID(value["rule_set_id"]), key=value["key"], name=value["name"],
        revision=int(value["revision"]),
        rules=tuple(_rule_from_json(rule) for rule in value["rules"]),
        digest=value["digest"],
    )


def _item_from_json(value: dict[str, Any]) -> AlarmConfigurationPlanItem:
    return AlarmConfigurationPlanItem(
        definition_key=value["definition_key"],
        entity_instance_id=UUID(value["entity_instance_id"]),
        rule_id=value["rule_id"], action=value["action"],
        before=value.get("before"), after=value.get("after"),
        blockers=tuple(value.get("blockers", ())),
        before_definition_id=UUID(value["before_definition_id"]) if value.get("before_definition_id") else None,
    )


def _result_from_json(value: dict[str, Any], items: tuple[AlarmConfigurationPlanItem, ...] = ()) -> AppliedAlarmConfiguration:
    return AppliedAlarmConfiguration(
        id=UUID(value["id"]), plan_id=UUID(value["plan_id"]),
        configuration_revision=int(value["configuration_revision"]),
        definition_ids=tuple(UUID(item) for item in value["definition_ids"]),
        audit_event_id=UUID(value["audit_event_id"]),
        applied_at=datetime.fromisoformat(value["applied_at"]),
        items=items,
    )


def _plan_from_json(value: dict[str, Any], *, status: str | None = None, applied_result: dict[str, Any] | None = None) -> AlarmConfigurationPlan:
    items = tuple(_item_from_json(item) for item in value["items"])
    return AlarmConfigurationPlan(
        id=UUID(value["id"]),
        base_configuration_revision=int(value["base_configuration_revision"]),
        rule_set_revision=_revision_from_json(value["rule_set_revision"]),
        status=status or value["status"], items=items,
        blockers=tuple(value["blockers"]), digest=value["digest"],
        planned_by=value["planned_by"],
        applied_result=_result_from_json(applied_result, items) if applied_result else None,
    )


def _rule_groups(
    revisions: tuple[AlarmRuleSetRevision, ...],
    bindings: tuple[tuple[str, UUID, UUID, bool], ...],
    publications: tuple[tuple[UUID, UUID, int], ...] = (),
) -> tuple[AlarmRuleGroup, ...]:
    severity_rank = {"INFO": 0, "WARNING": 1, "MAJOR": 2, "CRITICAL": 3}
    grouped: dict[UUID, list[AlarmRuleSetRevision]] = {}
    for revision in revisions:
        grouped.setdefault(revision.rule_set_id, []).append(revision)
    result: list[AlarmRuleGroup] = []
    for rule_set_id, candidates in grouped.items():
        latest = max(candidates, key=lambda item: item.revision)
        non_empty = [item for item in candidates if item.rules]
        last_non_empty = max(non_empty, key=lambda item: item.revision) if non_empty else None
        entity_ids: set[UUID] = set()
        enabled_ids: set[UUID] = set()
        node_ids: set[UUID] = set()
        prefix = f"alarm.{latest.key}."
        for asset_id, entity_id, node_id, enabled in bindings:
            if not asset_id.startswith(prefix):
                continue
            entity_ids.add(entity_id)
            node_ids.add(node_id)
            if enabled:
                enabled_ids.add(entity_id)
        published_by_entity = {
            entity_id: revision
            for group_id, entity_id, revision in publications
            if group_id == rule_set_id and entity_id in entity_ids
        }
        published_versions = set(published_by_entity.values())
        published = None
        if entity_ids and set(published_by_entity) == entity_ids and len(published_versions) == 1:
            published = next((item for item in candidates if item.revision in published_versions and item.rules), None)
        # A draft may describe editing work, but must never become a restart target.
        displayed = published or last_non_empty
        rules = () if displayed is None else displayed.rules
        highest = max(
            (rule.severity for rule in rules),
            key=severity_rank.__getitem__,
            default=None,
        )
        result.append(
            AlarmRuleGroup(
                rule_set_id,
                latest.key,
                latest.name,
                latest.revision,
                None if last_non_empty is None else last_non_empty.revision,
                tuple(sorted(entity_ids, key=str)),
                tuple(sorted(enabled_ids, key=str)),
                len(node_ids),
                len(rules),
                highest,
                None if published is None else published.revision,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.name, item.key)))


class PostgresAlarmConfigurationRepository:
    def __init__(self, connection_factory: ConnectionFactory | None = None) -> None:
        register_uuid()
        self._connection_factory = connection_factory
        self._revisions = PostgresConfigurationRevisions()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            if self._connection_factory is None:
                from app.services.telemetry_store import get_connection
                with get_connection() as connection:
                    try:
                        yield connection
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                return
            connection = self._connection_factory()
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except AlarmConfigurationError:
            raise
        except psycopg2.IntegrityError as error:
            raise AlarmConfigurationError("ALARM_CONFIGURATION_PERSISTENCE_FAILED") from error
        except (psycopg2.Error, OSError) as error:
            raise AlarmConfigurationError("ALARM_CONFIGURATION_PERSISTENCE_UNAVAILABLE") from error

    def save_rule_set_revision(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        if not key.strip() or not name.strip() or not actor.strip():
            raise AlarmConfigurationError("ALARM_RULE_SET_COMMAND_INVALID")
        normalized = tuple(sorted(rules, key=lambda rule: rule.id))
        digest = canonical_digest({"key": key, "name": name, "rules": normalized})
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO t_alarm_rule_sets(id, rule_set_key, name, created_by) VALUES (%s,%s,%s,%s) ON CONFLICT(rule_set_key) DO NOTHING", (uuid4(), key, name, actor))
                cursor.execute("SELECT id, name FROM t_alarm_rule_sets WHERE rule_set_key=%s FOR UPDATE", (key,))
                row = cursor.fetchone()
                if row is None or row[1] != name:
                    raise AlarmConfigurationError("ALARM_RULE_SET_CONFLICT")
                rule_set_id = row[0]
                cursor.execute("SELECT COALESCE(max(revision),0)+1 FROM t_alarm_rule_set_revisions WHERE rule_set_id=%s", (rule_set_id,))
                revision = int(cursor.fetchone()[0])
                cursor.execute("INSERT INTO t_alarm_rule_set_revisions(rule_set_id,revision,rule_set_key,rule_set_name,rules,digest,actor) VALUES (%s,%s,%s,%s,%s,%s,%s)", (rule_set_id, revision, key, name, Json(_json_value(normalized)), digest, actor))
        return AlarmRuleSetRevision(rule_set_id, key, name, revision, normalized, digest)

    def get_rule_set_revision(self, rule_set_id: UUID, revision: int) -> AlarmRuleSetRevision | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT rule_set_key,rule_set_name,rules,digest FROM t_alarm_rule_set_revisions WHERE rule_set_id=%s AND revision=%s", (rule_set_id, revision))
                row = cursor.fetchone()
        return None if row is None else AlarmRuleSetRevision(rule_set_id, row[0], row[1], revision, tuple(_rule_from_json(item) for item in row[2]), row[3].strip())

    def list_rule_set_revisions(self) -> tuple[AlarmRuleSetRevision, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT rule_set_id,rule_set_key,rule_set_name,revision,rules,digest FROM t_alarm_rule_set_revisions ORDER BY rule_set_key,revision")
                rows = cursor.fetchall()
        return tuple(AlarmRuleSetRevision(row[0], row[1], row[2], int(row[3]), tuple(_rule_from_json(item) for item in row[4]), row[5].strip()) for row in rows)

    def list_rule_groups(self) -> tuple[AlarmRuleGroup, ...]:
        revisions = self.list_rule_set_revisions()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT definition.asset_id, definition.entity_instance_id,
                           entity.node_id,
                           (current.definition_id IS NOT NULL) AS enabled
                    FROM t_alarm_definitions definition
                    JOIN t_entity_instances entity
                      ON entity.id = definition.entity_instance_id
                    LEFT JOIN t_alarm_definition_current current
                      ON current.definition_id = definition.id
                    ORDER BY definition.asset_id, definition.entity_instance_id
                    """
                )
                rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT DISTINCT ON (plan.rule_set_id, item->>'entity_instance_id')
                           plan.rule_set_id, (item->>'entity_instance_id')::uuid,
                           plan.rule_set_revision
                    FROM t_alarm_configuration_plans plan
                    CROSS JOIN LATERAL jsonb_array_elements(plan.canonical_plan->'items') item
                    WHERE plan.status='applied'
                      AND item->>'action' IN ('add','update','preserve')
                      AND item->'after' IS NOT NULL AND item->'after' <> 'null'::jsonb
                    ORDER BY plan.rule_set_id, item->>'entity_instance_id',
                             (plan.applied_result->>'configuration_revision')::bigint DESC NULLS LAST,
                             plan.applied_at DESC, plan.id DESC
                    """
                )
                publications = tuple(cursor.fetchall())
        bindings = tuple(
            (str(row[0]), row[1], row[2], bool(row[3]))
            for row in rows
        )
        return _rule_groups(revisions, bindings, publications)

    def resolve_entities(self, selection: EntitySelection) -> tuple[ResolvedAlarmEntity, ...]:
        clauses = ["entity.active=TRUE"]
        parameters: list[Any] = []
        if selection.entity_instance_ids:
            clauses.append("entity.id=ANY(%s::uuid[])")
            parameters.append([str(value) for value in selection.entity_instance_ids])
        if selection.node_ids:
            clauses.append("entity.node_id=ANY(%s::uuid[])")
            parameters.append([str(value) for value in selection.node_ids])
        if selection.entity_definition_ids:
            clauses.append("entity.definition_id=ANY(%s::text[])")
            parameters.append(list(selection.entity_definition_ids))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT entity.id,entity.node_id,entity.definition_id,entity.display_name,entity.data_type,entity.unit FROM t_entity_instances entity WHERE {' AND '.join(clauses)} ORDER BY entity.id", tuple(parameters))
                rows = cursor.fetchall()
        return tuple(ResolvedAlarmEntity(*row) for row in rows)

    def current_configuration_revision(self) -> int:
        with self._connection() as connection:
            return self._revisions.current(connection)

    def http_notification_status(
        self,
        config_id: UUID,
    ) -> tuple[bool, bool] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT enabled,tested_digest=current_digest
                FROM t_alarm_http_notification_configs
                WHERE id=%s
                """,
                (config_id,),
            )
            row = cursor.fetchone()
        return None if row is None else (bool(row[0]), bool(row[1]))

    def current_configuration(self) -> dict[str, Any]:
        with self._connection() as connection:
            revision = self._revisions.current(connection)
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT definition.asset_id,definition.id,definition.entity_instance_id,
                           entity.display_name,definition.entity_definition_id,
                           definition.trigger_condition,definition.trigger_duration_seconds,
                           definition.recovery_condition,definition.recovery_duration_seconds,
                           definition.severity,definition.notification_throttle_seconds,
                           notification.configuration_id
                    FROM t_alarm_definition_current current
                    JOIN t_alarm_definitions definition ON definition.id=current.definition_id
                    JOIN t_entity_instances entity ON entity.id=definition.entity_instance_id
                    LEFT JOIN t_alarm_http_notification_bindings notification
                      ON notification.definition_id=definition.id
                    ORDER BY definition.asset_id
                """)
                rows = cursor.fetchall()
        definitions: dict[str, Any] = {}
        for row in rows:
            rule = {
                "id": row[0].rsplit(".", 1)[-1], "name": row[0], "severity": row[9],
                "trigger": {"operator": row[5]["op"], "value": row[5].get("value")},
                "trigger_duration_seconds": row[6],
                "recovery": {"operator": row[7]["op"], "value": row[7].get("value")},
                "recovery_duration_seconds": row[8],
                "notification_throttle_seconds": row[10], "unit": None, "fault_map_id": None,
                "http_notification_config_id": str(row[11]) if row[11] else None,
            }
            definitions[row[0]] = {
                "id": row[1], "payload": {"entity_instance_id": str(row[2]), "rule": rule},
                "entity_display_name": row[3], "rule_name": rule["name"],
                "severity": row[9], "trigger": rule["trigger"], "recovery": rule["recovery"],
                "source": "L2", "version_description": f"配置版本 {revision}",
                "enabled": True, "status": "active",
            }
        return {"configuration_revision": revision, "definitions": definitions}

    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan:
        payload = _json_value(plan)
        payload.pop("applied_result", None)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO t_alarm_configuration_plans
                      (id,base_configuration_revision,rule_set_id,rule_set_revision,
                       canonical_plan,digest,status,planned_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(digest) DO NOTHING
                """, (plan.id, plan.base_configuration_revision, plan.rule_set_revision.rule_set_id, plan.rule_set_revision.revision, Json(payload), plan.digest, plan.status, plan.planned_by))
                cursor.execute("SELECT canonical_plan,status,applied_result FROM t_alarm_configuration_plans WHERE digest=%s", (plan.digest,))
                row = cursor.fetchone()
        return _plan_from_json(row[0], status=row[1], applied_result=row[2])

    def get_plan(self, plan_id: UUID) -> AlarmConfigurationPlan | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT canonical_plan,status,applied_result FROM t_alarm_configuration_plans WHERE id=%s", (plan_id,))
                row = cursor.fetchone()
        return None if row is None else _plan_from_json(row[0], status=row[1], applied_result=row[2])

    def apply_plan(self, plan: AlarmConfigurationPlan, *, idempotency_key: str, actor: str) -> AppliedAlarmConfiguration:
        request_digest = canonical_digest({"plan_id": plan.id, "plan_digest": plan.digest, "actor": actor})
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT request_digest,plan_id,applied_result FROM t_alarm_configuration_idempotency WHERE actor=%s AND idempotency_key=%s", (actor, idempotency_key))
                prior = cursor.fetchone()
                if prior:
                    if prior[0].strip() != request_digest or prior[1] != plan.id:
                        raise AlarmConfigurationError("IDEMPOTENCY_KEY_REUSED")
                    return _result_from_json(prior[2], plan.items)
                cursor.execute("SELECT status,base_configuration_revision,applied_result FROM t_alarm_configuration_plans WHERE id=%s FOR UPDATE", (plan.id,))
                row = cursor.fetchone()
                if row is None:
                    raise AlarmConfigurationError("ALARM_PLAN_NOT_FOUND")
                if row[0] == "applied":
                    return _result_from_json(row[2], plan.items)
                if row[0] != "ready":
                    raise AlarmConfigurationError("ALARM_PLAN_BLOCKED")
                if self._revisions.current(connection) != int(row[1]):
                    raise AlarmConfigurationError("ALARM_PLAN_STALE")
                revision = self._revisions.publish(
                    transaction=connection, base_revision=plan.base_configuration_revision,
                    actor=actor, action="alarm.configuration.apply", resource_kind="alarm_configuration",
                    resource_id=str(plan.id), before_digest=None, after_digest=plan.digest,
                    details={"plan_id": str(plan.id), "item_count": len(plan.items)},
                )
                definition_ids: list[UUID] = []
                for item in plan.items:
                    if item.action == "delete_candidate":
                        cursor.execute("DELETE FROM t_alarm_definition_current WHERE asset_id=%s AND entity_instance_id=%s", (item.definition_key, item.entity_instance_id))
                        continue
                    if item.action == "preserve" and item.before_definition_id:
                        definition_ids.append(item.before_definition_id)
                        continue
                    if item.action not in {"add", "update"} or item.after is None:
                        continue
                    rule = item.after["rule"]
                    cursor.execute("SELECT definition_id,data_type,unit FROM t_entity_instances WHERE id=%s AND active=TRUE", (item.entity_instance_id,))
                    entity = cursor.fetchone()
                    if entity is None:
                        raise AlarmConfigurationError("ALARM_ENTITY_UNRESOLVED")
                    definition_id = uuid4()
                    digest = canonical_digest(item.after)
                    trigger = {"op": rule["trigger"]["operator"], "value": rule["trigger"]["value"]}
                    recovery = {"op": rule["recovery"]["operator"], "value": rule["recovery"]["value"]}
                    cursor.execute("""
                        INSERT INTO t_alarm_definitions
                          (id,asset_id,definition_version,configuration_revision,
                           entity_instance_id,entity_definition_id,trigger_condition,
                           trigger_duration_seconds,recovery_condition,recovery_duration_seconds,
                           severity,notification_throttle_seconds,content_digest,content_digest_algorithm)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'sha256-v2-content')
                        ON CONFLICT(asset_id,entity_instance_id,content_digest_algorithm,content_digest)
                        DO NOTHING
                        RETURNING id
                    """, (definition_id, item.definition_key, str(revision), revision, item.entity_instance_id, entity[0], Json(trigger), rule["trigger_duration_seconds"], Json(recovery), rule["recovery_duration_seconds"], rule["severity"], rule["notification_throttle_seconds"], digest))
                    inserted = cursor.fetchone()
                    if inserted is None:
                        cursor.execute("""
                            SELECT id FROM t_alarm_definitions
                            WHERE asset_id=%s AND entity_instance_id=%s
                              AND content_digest_algorithm='sha256-v2-content'
                              AND content_digest=%s
                        """, (item.definition_key, item.entity_instance_id, digest))
                        definition_id = cursor.fetchone()[0]
                    else:
                        definition_id = inserted[0]
                    cursor.execute("""
                        INSERT INTO t_alarm_definition_current(asset_id,entity_instance_id,definition_id,configuration_revision)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT(asset_id,entity_instance_id) DO UPDATE
                        SET definition_id=EXCLUDED.definition_id,configuration_revision=EXCLUDED.configuration_revision
                    """, (item.definition_key, item.entity_instance_id, definition_id, revision))
                    notification_config_id = rule.get(
                        "http_notification_config_id"
                    )
                    if notification_config_id:
                        cursor.execute(
                            """
                            SELECT enabled,tested_digest=current_digest
                            FROM t_alarm_http_notification_configs
                            WHERE id=%s FOR SHARE
                            """,
                            (UUID(notification_config_id),),
                        )
                        notification_state = cursor.fetchone()
                        if notification_state is None:
                            raise AlarmConfigurationError(
                                "HTTP_NOTIFICATION_NOT_FOUND"
                            )
                        if not notification_state[0]:
                            raise AlarmConfigurationError(
                                "HTTP_NOTIFICATION_DISABLED"
                            )
                        if not notification_state[1]:
                            raise AlarmConfigurationError(
                                "HTTP_NOTIFICATION_TEST_STALE"
                            )
                        cursor.execute(
                            """
                            INSERT INTO t_alarm_http_notification_bindings
                              (definition_id,configuration_id,created_by)
                            VALUES (%s,%s,%s)
                            ON CONFLICT(definition_id) DO UPDATE
                            SET configuration_id=EXCLUDED.configuration_id,
                                created_by=EXCLUDED.created_by,
                                created_at=clock_timestamp()
                            """,
                            (
                                definition_id,
                                UUID(notification_config_id),
                                actor,
                            ),
                        )
                    definition_ids.append(definition_id)
                application_id = uuid4()
                applied_at = datetime.now(timezone.utc)
                audit_id = uuid5(NAMESPACE_URL, f"zizu/configuration-audit/{revision}/alarm_configuration/{plan.id}/{plan.digest}")
                result = AppliedAlarmConfiguration(application_id, plan.id, revision, tuple(definition_ids), audit_id, applied_at, plan.items)
                result_json = _json_value(result)
                cursor.execute("UPDATE t_alarm_configuration_plans SET status='applied',applied_by=%s,applied_result=%s,applied_at=%s,application_id=%s WHERE id=%s", (actor, Json(result_json), applied_at, application_id, plan.id))
                cursor.execute("INSERT INTO t_alarm_configuration_idempotency(actor,idempotency_key,request_digest,plan_id,applied_result) VALUES (%s,%s,%s,%s,%s)", (actor, idempotency_key, request_digest, plan.id, Json(result_json)))
                return result


def load_applied_alarm_configuration(connection: Any, application_id: UUID) -> AppliedAlarmConfiguration | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT canonical_plan,applied_result FROM t_alarm_configuration_plans WHERE application_id=%s AND status='applied'", (application_id,))
        row = cursor.fetchone()
    if row is None:
        return None
    plan = _plan_from_json(row[0], status="applied", applied_result=row[1])
    return plan.applied_result


def load_latest_applied_alarm_configuration(connection: Any) -> AppliedAlarmConfiguration | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT application_id FROM t_alarm_configuration_plans WHERE status='applied' ORDER BY applied_at DESC,id DESC LIMIT 1")
        row = cursor.fetchone()
    return None if row is None else load_applied_alarm_configuration(connection, row[0])


def build_postgres_alarm_configuration() -> AlarmConfiguration:
    runtime_gate = None
    try:
        from app.main import get_pipeline

        pipeline = get_pipeline()
        if pipeline is not None:
            runtime_gate = pipeline.data_trunk.configuration_gate
    except (ImportError, RuntimeError):
        runtime_gate = None
    return AlarmConfiguration(
        PostgresAlarmConfigurationRepository(),
        runtime_gate=runtime_gate,
    )


__all__ = [
    "PostgresAlarmConfigurationRepository",
    "build_postgres_alarm_configuration",
    "load_applied_alarm_configuration",
    "load_latest_applied_alarm_configuration",
]
