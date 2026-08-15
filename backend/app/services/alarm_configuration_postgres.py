"""PostgreSQL persistence for reviewable, atomic alarm configuration applies."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from functools import wraps
from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json, register_uuid

from app.services.alarm_configuration import (
    AlarmConfigurationError,
    AlarmConfigurationPlan,
    AlarmConfigurationPlanItem,
    AlarmRule,
    AlarmRuleSetRevision,
    AppliedAlarmConfiguration,
    EntitySelection,
    LegacyAlarmDefinitionSpec,
    LegacyAlarmMigrationPlan,
    LegacyAlarmSource,
    ResolvedAlarmEntity,
    compile_legacy_migration_plan,
    legacy_migration_plan_digest,
    _raise_legacy_blockers,
)
from app.services.alarm_definitions import (
    AlarmDefinitionPlan,
    InstalledAlarmDefinition,
)
from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog


ConnectionFactory = Callable[[], Any]


def _persistence_operation(method):
    """Never leak driver exceptions past the repository boundary."""

    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except AlarmConfigurationError:
            raise
        except psycopg2.IntegrityError as error:
            raise AlarmConfigurationError(
                "ALARM_CONFIGURATION_PERSISTENCE_FAILED"
            ) from error
        except (psycopg2.Error, OSError) as error:
            raise AlarmConfigurationError(
                "ALARM_CONFIGURATION_PERSISTENCE_UNAVAILABLE"
            ) from error

    return wrapped


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _rule_from_json(value: dict[str, Any]) -> AlarmRule:
    fault_map_id = value.get("fault_map_id")
    return AlarmRule(
        id=value["id"],
        name=value["name"],
        severity=value["severity"],
        trigger=dict(value["trigger"]),
        trigger_duration_seconds=value["trigger_duration_seconds"],
        recovery=dict(value["recovery"]),
        recovery_duration_seconds=value["recovery_duration_seconds"],
        notification_throttle_seconds=value["notification_throttle_seconds"],
        unit=value.get("unit"),
        fault_map_id=UUID(fault_map_id) if fault_map_id else None,
    )


def _revision_from_json(value: dict[str, Any]) -> AlarmRuleSetRevision:
    return AlarmRuleSetRevision(
        rule_set_id=UUID(value["rule_set_id"]),
        key=value["key"],
        name=value["name"],
        revision=int(value["revision"]),
        rules=tuple(_rule_from_json(rule) for rule in value["rules"]),
        digest=value["digest"],
    )


def _plan_from_json(
    value: dict[str, Any],
    *,
    lifecycle_status: str | None = None,
    applied_result: dict[str, Any] | None = None,
    planned_by: str | None = None,
) -> AlarmConfigurationPlan:
    items = tuple(_plan_item_from_json(item) for item in value["items"])
    return AlarmConfigurationPlan(
        id=UUID(value["id"]),
        installation_id=UUID(value["installation_id"]),
        base_site_configuration_version=int(value["base_site_configuration_version"]),
        rule_set_revision=_revision_from_json(value["rule_set_revision"]),
        status=lifecycle_status or value["status"],
        items=items,
        blockers=tuple(value["blockers"]),
        digest=value["digest"],
        planned_by=planned_by or value["planned_by"],
        applied_result=(
            _result_from_json(
                applied_result,
                items=tuple(
                    item for item in items
                    if item.action in {"add", "update", "preserve"}
                ),
            )
            if applied_result is not None
            else None
        ),
    )


def _result_json(result: AppliedAlarmConfiguration) -> dict[str, Any]:
    return _json_value(result)


def _plan_item_from_json(value: dict[str, Any]) -> AlarmConfigurationPlanItem:
    return AlarmConfigurationPlanItem(
        definition_key=value["definition_key"],
        entity_instance_id=UUID(value["entity_instance_id"]),
        rule_id=value["rule_id"],
        action=value["action"],
        before=value.get("before"),
        after=value.get("after"),
        blockers=tuple(value["blockers"]),
    )


def _result_from_json(
    value: dict[str, Any],
    *,
    items: tuple[AlarmConfigurationPlanItem, ...] = (),
) -> AppliedAlarmConfiguration:
    return AppliedAlarmConfiguration(
        id=UUID(value["id"]),
        plan_id=UUID(value["plan_id"]),
        installation_id=UUID(value["installation_id"]),
        site_configuration_version=int(value["site_configuration_version"]),
        definition_ids=tuple(UUID(item) for item in value["definition_ids"]),
        audit_event_id=UUID(value["audit_event_id"]),
        applied_at=datetime.fromisoformat(value["applied_at"]),
        items=tuple(
            _plan_item_from_json(item) for item in value.get("items", ())
        ) or items,
    )


def load_applied_alarm_configuration(
    connection: Any,
    application_id: UUID,
) -> AppliedAlarmConfiguration | None:
    """Load one immutable application and its exact ordered plan evidence."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT plan.canonical_plan, plan.applied_result, plan.planned_by
            FROM t_alarm_configuration_plans plan
            WHERE plan.application_id = %s AND plan.status = 'applied'
            """,
            (application_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    plan = _plan_from_json(row[0], lifecycle_status="applied", planned_by=row[2])
    items = tuple(
        item for item in plan.items
        if item.action in {"add", "update", "preserve"}
    )
    return _result_from_json(row[1], items=items)


def load_latest_applied_alarm_configuration(
    connection: Any,
) -> AppliedAlarmConfiguration | None:
    """Load the most recently applied immutable alarm configuration."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT application_id
            FROM t_alarm_configuration_plans
            WHERE status = 'applied' AND application_id IS NOT NULL
            ORDER BY applied_at DESC, id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    return None if row is None else load_applied_alarm_configuration(connection, row[0])


class PostgresAlarmConfigurationRepository:
    """The single atomic write seam for applying an alarm configuration plan."""

    def __init__(
        self,
        connection_factory: ConnectionFactory | None = None,
        definition_catalog: PostgresAlarmDefinitionCatalog | None = None,
    ) -> None:
        register_uuid()
        self._connection_factory = connection_factory
        self._definition_catalog = definition_catalog or PostgresAlarmDefinitionCatalog()

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
        except psycopg2.IntegrityError:
            raise
        except (psycopg2.Error, OSError) as error:
            raise AlarmConfigurationError(
                "ALARM_CONFIGURATION_PERSISTENCE_UNAVAILABLE"
            ) from error

    @_persistence_operation
    def save_rule_set_revision(
        self,
        *,
        key: str,
        name: str,
        rules: tuple[AlarmRule, ...],
        actor: str,
    ) -> AlarmRuleSetRevision:
        if not key.strip() or not name.strip() or not actor.strip():
            raise AlarmConfigurationError("ALARM_RULE_SET_COMMAND_INVALID")
        normalized_rules = tuple(sorted(rules, key=lambda rule: rule.id))
        digest = _digest(
            {
                "key": key,
                "name": name,
                "rules": [_json_value(rule) for rule in normalized_rules],
            }
        )
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    candidate_rule_set_id = uuid4()
                    cursor.execute(
                        """
                        INSERT INTO t_alarm_rule_sets
                          (id, rule_set_key, name, created_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (rule_set_key) DO NOTHING
                        """,
                        (candidate_rule_set_id, key, name, actor),
                    )
                    cursor.execute(
                        """
                        SELECT id, name FROM t_alarm_rule_sets
                        WHERE rule_set_key = %s FOR UPDATE
                        """,
                        (key,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise AlarmConfigurationError("ALARM_RULE_SET_CONFLICT")
                    rule_set_id = row[0]
                    if row[1] != name:
                        raise AlarmConfigurationError("ALARM_RULE_SET_NAME_MISMATCH")
                    cursor.execute(
                        """
                        SELECT COALESCE(max(revision), 0) + 1
                        FROM t_alarm_rule_set_revisions
                        WHERE rule_set_id = %s
                        """,
                        (rule_set_id,),
                    )
                    revision_number = int(cursor.fetchone()[0])
                    cursor.execute(
                        """
                        INSERT INTO t_alarm_rule_set_revisions
                          (rule_set_id, revision, rule_set_key, rule_set_name,
                           rules, digest, actor)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            rule_set_id,
                            revision_number,
                            key,
                            name,
                            Json(_json_value(normalized_rules)),
                            digest,
                            actor,
                        ),
                    )
        except psycopg2.IntegrityError as error:
            code = {
                "23503": "ALARM_RULE_SET_REFERENCE_INVALID",
                "23505": "ALARM_RULE_SET_CONFLICT",
                "23514": "ALARM_RULE_SET_COMMAND_INVALID",
            }.get(error.pgcode, "ALARM_RULE_SET_PERSISTENCE_FAILED")
            raise AlarmConfigurationError(code) from error
        return AlarmRuleSetRevision(
            rule_set_id=rule_set_id,
            key=key,
            name=name,
            revision=revision_number,
            rules=normalized_rules,
            digest=digest,
        )

    @_persistence_operation
    def get_rule_set_revision(
        self,
        rule_set_id: UUID,
        revision: int,
    ) -> AlarmRuleSetRevision | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT revision.rule_set_key, revision.rule_set_name,
                           revision.rules, revision.digest
                    FROM t_alarm_rule_set_revisions revision
                    WHERE revision.rule_set_id = %s AND revision.revision = %s
                    """,
                    (rule_set_id, revision),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return AlarmRuleSetRevision(
            rule_set_id=rule_set_id,
            key=row[0],
            name=row[1],
            revision=revision,
            rules=tuple(_rule_from_json(value) for value in row[2]),
            digest=row[3].strip(),
        )

    @_persistence_operation
    def list_rule_set_revisions(self) -> tuple[AlarmRuleSetRevision, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT revision.rule_set_id, revision.rule_set_key,
                           revision.rule_set_name, revision.revision,
                           revision.rules, revision.digest
                    FROM t_alarm_rule_set_revisions revision
                    ORDER BY revision.rule_set_key, revision.revision
                    """
                )
                rows = cursor.fetchall()
        return tuple(
            AlarmRuleSetRevision(
                rule_set_id=row[0],
                key=row[1],
                name=row[2],
                revision=int(row[3]),
                rules=tuple(_rule_from_json(value) for value in row[4]),
                digest=row[5].strip(),
            )
            for row in rows
        )

    @_persistence_operation
    def resolve_entities(
        self,
        installation_id: UUID,
        selection: EntitySelection,
    ) -> tuple[ResolvedAlarmEntity, ...]:
        clauses: list[str] = []
        parameters: list[Any] = [installation_id]
        if selection.entity_instance_ids:
            clauses.append("entity.id = ANY(%s::uuid[])")
            parameters.append([str(value) for value in selection.entity_instance_ids])
        if selection.device_instance_ids:
            clauses.append("device.id = ANY(%s::uuid[])")
            parameters.append([str(value) for value in selection.device_instance_ids])
        if selection.entity_definition_ids:
            clauses.append("entity.definition_id = ANY(%s::text[])")
            parameters.append(list(selection.entity_definition_ids))
        selected = "" if not clauses else " AND " + " AND ".join(clauses)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT entity.id, entity.device_instance_id,
                           entity.definition_id, entity.display_name,
                           entity.data_type, entity.unit,
                           confirmation.id
                    FROM t_site_configuration_state state
                    JOIN t_site_configuration_versions site
                      ON site.version = state.current_version
                    JOIN t_device_instances device
                      ON device.identity_installation_id = site.entity_identity_installation_id
                     AND device.active = TRUE
                    JOIN t_entity_instances entity
                      ON entity.device_instance_id = device.id AND entity.active = TRUE
                    LEFT JOIN t_entity_instance_bindings binding
                      ON binding.entity_instance_id = entity.id AND binding.active = TRUE
                    LEFT JOIN t_entity_binding_confirmations confirmation
                      ON confirmation.id = binding.confirmation_audit_id
                    WHERE state.singleton = TRUE AND site.installation_id = %s
                    {selected}
                    ORDER BY entity.id
                    """,
                    tuple(parameters),
                )
                rows = cursor.fetchall()
        return tuple(ResolvedAlarmEntity(*row) for row in rows)

    @_persistence_operation
    def current_site_version(self) -> int:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_version FROM t_site_configuration_state WHERE singleton = TRUE"
                )
                row = cursor.fetchone()
        return int(row[0]) if row else 0

    @_persistence_operation
    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan:
        canonical_plan = _json_value(plan)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_alarm_configuration_plans
                      (id, source_installation_id,
                       base_site_configuration_version, rule_set_id,
                       rule_set_revision, canonical_plan, digest, status,
                       planned_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (digest) DO NOTHING
                    RETURNING canonical_plan, planned_by
                    """,
                    (
                        plan.id,
                        plan.installation_id,
                        plan.base_site_configuration_version,
                        plan.rule_set_revision.rule_set_id,
                        plan.rule_set_revision.revision,
                        Json(canonical_plan),
                        plan.digest,
                        plan.status,
                        plan.planned_by,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT canonical_plan, planned_by
                        FROM t_alarm_configuration_plans WHERE digest = %s
                        """,
                        (plan.digest,),
                    )
                    row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Alarm configuration plan conflict row disappeared")
        return _plan_from_json(row[0], planned_by=row[1])

    @_persistence_operation
    def get_plan(self, plan_id: UUID) -> AlarmConfigurationPlan | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT canonical_plan, status, applied_result, planned_by
                    FROM t_alarm_configuration_plans WHERE id = %s
                    """,
                    (plan_id,),
                )
                row = cursor.fetchone()
        return (
            _plan_from_json(
                row[0],
                lifecycle_status=row[1],
                applied_result=row[2],
                planned_by=row[3],
            )
            if row
            else None
        )

    @_persistence_operation
    def get_application(
        self,
        application_id: UUID,
    ) -> AppliedAlarmConfiguration | None:
        with self._connection() as connection:
            return load_applied_alarm_configuration(connection, application_id)

    @_persistence_operation
    def current_site_context(self) -> dict[str, Any]:
        definitions: dict[str, dict[str, Any]] = {}
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT definition.asset_id, definition.id, definition.definition_version,
                           definition.entity_instance_id,
                           definition.trigger_condition,
                           definition.trigger_duration_seconds,
                           definition.recovery_condition,
                           definition.recovery_duration_seconds,
                           definition.severity,
                           definition.notification_throttle_seconds,
                           entity.unit, entity.display_name, origin.origin_type,
                           origin.details, origin.rule_set_revision
                    FROM t_alarm_definition_current current_definition
                    JOIN t_alarm_definitions definition
                      ON definition.id = current_definition.definition_id
                    JOIN t_entity_instances entity
                      ON entity.id = definition.entity_instance_id
                    LEFT JOIN t_alarm_definition_origins origin
                      ON origin.definition_id = definition.id
                    ORDER BY definition.asset_id
                    """
                )
                rows = cursor.fetchall()
                cursor.execute(
                    "SELECT current_version FROM t_site_configuration_state WHERE singleton = TRUE"
                )
                site_version = int(cursor.fetchone()[0])
        for row in rows:
            origin_details = row[13] or {}
            origin_rule = origin_details.get("rule")
            severity_name = {
                "CRITICAL": "严重",
                "MAJOR": "重要",
                "WARNING": "警告",
                "INFO": "提示",
            }.get(row[8], "告警")
            readable_name = origin_details.get("rule_name")
            if not isinstance(readable_name, str) or not readable_name.strip():
                readable_name = f"{row[11]} · {severity_name}告警"
            rule = origin_rule or {
                "id": row[0].rsplit(".", 1)[-1],
                "name": readable_name,
                "severity": row[8],
                "trigger": row[4],
                "trigger_duration_seconds": row[5],
                "recovery": row[6],
                "recovery_duration_seconds": row[7],
                "notification_throttle_seconds": row[9],
                "unit": row[10],
                "fault_map_id": None,
            }
            origin_type = row[12] or "package"
            if origin_type == "rule_set" and row[14] is not None:
                version_description = f"规则集第 {row[14]} 版"
            elif origin_type == "legacy_migration":
                version_description = "旧配置迁移版"
            else:
                version_description = "配置资产当前版本"
            definitions[row[0]] = {
                "id": row[1],
                "payload": {
                    "rule": rule,
                    "entity_instance_id": str(row[3]),
                },
                "entity_display_name": row[11],
                "rule_name": rule["name"],
                "severity": rule["severity"],
                "trigger": rule["trigger"],
                "recovery": rule["recovery"],
                "source": origin_type,
                "version_description": version_description,
                "enabled": True,
                "status": "current",
            }
        return {
            "site_configuration_version": site_version,
            "definitions": definitions,
        }

    @_persistence_operation
    def list_legacy_alarm_sources(
        self,
    ) -> tuple[UUID, tuple[LegacyAlarmSource, ...]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self._legacy_alarm_sources(cursor)

    @staticmethod
    def _legacy_alarm_sources(
        cursor: Any,
    ) -> tuple[UUID, tuple[LegacyAlarmSource, ...]]:
        cursor.execute(
            """
            SELECT site.installation_id, site.entity_identity_installation_id
            FROM t_site_configuration_state state
            JOIN t_site_configuration_versions site
              ON site.version = state.current_version
            WHERE state.singleton = TRUE
            """
        )
        site = cursor.fetchone()
        if site is None:
            raise AlarmConfigurationError("ALARM_SITE_CONFIGURATION_MISSING")
        installation_id, identity_installation_id = site
        cursor.execute(
            """
            SELECT migration.source_kind, migration.source_key,
                   array_agg(target.definition_id ORDER BY target.definition_id)
            FROM t_legacy_alarm_migrations migration
            JOIN t_legacy_alarm_migration_targets target
              ON target.migration_id = migration.id
            WHERE migration.state = 'migrated'
            GROUP BY migration.source_kind, migration.source_key
            """
        )
        migrated = {
            (row[0], row[1]): tuple(row[2]) for row in cursor.fetchall()
        }
        cursor.execute(
            """
            SELECT tag.id, tag.name, tag.alarm_level, tag.alarm_type,
                   tag.alarm_threshold, tag.fault_map_id,
                   (tag.fault_map_id IS NULL OR fault_map.id IS NOT NULL),
                   entity.id, entity.device_instance_id,
                   entity.definition_id, entity.display_name,
                   entity.data_type, entity.unit, confirmation.id, device.id
            FROM t_tags tag
            LEFT JOIN t_fault_maps fault_map ON fault_map.id = tag.fault_map_id
            LEFT JOIN t_entity_instance_bindings binding
              ON binding.tag_id = tag.id AND binding.active = TRUE
            LEFT JOIN t_entity_binding_confirmations confirmation
              ON confirmation.id = binding.confirmation_audit_id
             AND confirmation.entity_instance_id = binding.entity_instance_id
             AND confirmation.binding_id = binding.id
             AND confirmation.selected_tag_id = binding.tag_id
            LEFT JOIN t_entity_instances entity
              ON entity.id = binding.entity_instance_id AND entity.active = TRUE
            LEFT JOIN t_device_instances device
              ON device.id = entity.device_instance_id
             AND device.active = TRUE
             AND device.identity_installation_id = %s
            WHERE tag.enabled = TRUE
              AND (tag.alarm_level IS NOT NULL
                   OR tag.alarm_type IS NOT NULL
                   OR tag.alarm_threshold IS NOT NULL
                   OR tag.fault_map_id IS NOT NULL)
            ORDER BY tag.id, entity.id
            """,
            (identity_installation_id,),
        )
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in cursor.fetchall():
            source_key = str(row[0])
            record = grouped.setdefault(
                ("tag_alarm", source_key),
                {
                    "display_name": row[1],
                    "level_code": row[2] or "",
                    "stored_severity": None,
                    "trigger_rules": (
                        ({
                            "op": "fault",
                            "alarm_type": row[3],
                            "fault_map_id": (
                                str(row[5]) if row[5] is not None else None
                            ),
                        },)
                        if row[3] is not None or row[5] is not None
                        else (
                            ({"op": "gte", "threshold": row[4]},)
                            if row[4] is not None
                            else ({"op": "active"},)
                        )
                    ),
                    "fault_map_id": row[5],
                    "fault_map_exists": bool(row[6]),
                    "entities": {},
                },
            )
            if (
                row[7] is not None
                and row[13] is not None
                and row[8] is not None
                and row[14] is not None
            ):
                record["entities"][row[7]] = ResolvedAlarmEntity(
                    id=row[7],
                    device_instance_id=row[8],
                    definition_id=row[9],
                    display_name=row[10],
                    data_type=row[11],
                    unit=row[12],
                    confirmation_id=row[13],
                )
        cursor.execute(
            """
            SELECT alarm_binding.id, legacy.name, level.code, level.severity,
                   COALESCE(alarm_binding.trigger_rules, level.trigger_rules),
                   alarm_binding.fault_map_id,
                   (alarm_binding.fault_map_id IS NULL OR fault_map.id IS NOT NULL),
                   entity.id, entity.device_instance_id,
                   entity.definition_id, entity.display_name,
                   entity.data_type, entity.unit, confirmation.id, device.id
            FROM t_entity_alarm_bindings alarm_binding
            JOIN t_alarm_levels level
              ON level.id = alarm_binding.alarm_level_id AND level.enabled = TRUE
            JOIN t_entities legacy
              ON legacy.id = alarm_binding.entity_id AND legacy.enabled = TRUE
            LEFT JOIN t_fault_maps fault_map
              ON fault_map.id = alarm_binding.fault_map_id
            LEFT JOIN t_entity_bindings old_binding
              ON old_binding.entity_id = legacy.id AND old_binding.enabled = TRUE
            LEFT JOIN t_entity_instance_bindings binding
              ON binding.tag_id = old_binding.tag_id AND binding.active = TRUE
            LEFT JOIN t_entity_binding_confirmations confirmation
              ON confirmation.id = binding.confirmation_audit_id
             AND confirmation.entity_instance_id = binding.entity_instance_id
             AND confirmation.binding_id = binding.id
             AND confirmation.selected_tag_id = binding.tag_id
            LEFT JOIN t_entity_instances entity
              ON entity.id = binding.entity_instance_id AND entity.active = TRUE
            LEFT JOIN t_device_instances device
              ON device.id = entity.device_instance_id
             AND device.active = TRUE
             AND device.identity_installation_id = %s
            WHERE alarm_binding.enabled = TRUE
            ORDER BY alarm_binding.id, entity.id
            """,
            (identity_installation_id,),
        )
        for row in cursor.fetchall():
            source_key = str(row[0])
            trigger_rules = row[4]
            if not isinstance(trigger_rules, list):
                trigger_rules = []
            fault_map_id = row[5]
            fault_map_exists = bool(row[6])
            for rule in trigger_rules:
                reference = rule.get("fault_map_id") if isinstance(rule, dict) else None
                if reference:
                    try:
                        referenced_fault_map_id = UUID(str(reference))
                    except ValueError:
                        fault_map_exists = False
                        continue
                    cursor.execute(
                        "SELECT 1 FROM t_fault_maps WHERE id = %s",
                        (referenced_fault_map_id,),
                    )
                    fault_map_exists = (
                        fault_map_exists and cursor.fetchone() is not None
                    )
            record = grouped.setdefault(
                ("entity_alarm_binding", source_key),
                {
                    "display_name": row[1],
                    "level_code": row[2],
                    "stored_severity": row[3],
                    "trigger_rules": tuple(trigger_rules),
                    "fault_map_id": fault_map_id,
                    "fault_map_exists": fault_map_exists,
                    "entities": {},
                },
            )
            if (
                row[7] is not None
                and row[13] is not None
                and row[8] is not None
                and row[14] is not None
            ):
                record["entities"][row[7]] = ResolvedAlarmEntity(
                    id=row[7],
                    device_instance_id=row[8],
                    definition_id=row[9],
                    display_name=row[10],
                    data_type=row[11],
                    unit=row[12],
                    confirmation_id=row[13],
                )
        sources = tuple(
            LegacyAlarmSource(
                source_kind=source_kind,
                source_key=source_key,
                display_name=record["display_name"],
                entity_candidates=tuple(record["entities"].values()),
                level_code=record["level_code"],
                stored_severity=record["stored_severity"],
                trigger_rules=tuple(record["trigger_rules"]),
                fault_map_id=record["fault_map_id"],
                fault_map_exists=record["fault_map_exists"],
                target_definition_ids=migrated.get((source_kind, source_key), ()),
            )
            for (source_kind, source_key), record in sorted(grouped.items())
        )
        return installation_id, sources

    @_persistence_operation
    def apply_legacy_alarm_migration(
        self,
        plan: LegacyAlarmMigrationPlan,
        *,
        actor: str,
    ) -> LegacyAlarmMigrationPlan:
        if not actor.strip():
            raise AlarmConfigurationError("ALARM_MIGRATION_ACTOR_INVALID")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT site.installation_id, site.version,
                           site.package_digest,
                           site.entity_identity_installation_id
                    FROM t_site_configuration_state state
                    JOIN t_site_configuration_versions site
                      ON site.version = state.current_version
                    WHERE state.singleton = TRUE
                    FOR UPDATE OF state
                    """
                )
                site = cursor.fetchone()
                if site is None or site[0] != plan.installation_id:
                    raise AlarmConfigurationError("ALARM_MIGRATION_INSTALLATION_STALE")
                installation_id, site_version, package_digest, identity_installation_id = site
                _snapshot_installation_id, snapshot_sources = self._legacy_alarm_sources(
                    cursor
                )
                plan_keys = {
                    (item.source_kind, item.source_key) for item in plan.items
                }
                snapshot_keys = {
                    (source.source_kind, source.source_key)
                    for source in snapshot_sources
                }
                for source_kind, source_key in sorted(plan_keys | snapshot_keys):
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"{source_kind}:{source_key}",),
                    )
                cursor.execute(
                    """
                    LOCK TABLE t_tags,
                               t_entities,
                               t_entity_bindings,
                               t_entity_instance_bindings,
                               t_entity_binding_confirmations,
                               t_entity_instances,
                               t_device_instances
                    IN SHARE MODE
                    """
                )
                locked_installation_id, locked_sources = self._legacy_alarm_sources(
                    cursor
                )
                if locked_installation_id != installation_id:
                    raise AlarmConfigurationError("ALARM_MIGRATION_PLAN_STALE")
                locked_keys = {
                    (source.source_kind, source.source_key)
                    for source in locked_sources
                }
                if plan_keys != locked_keys:
                    raise AlarmConfigurationError("ALARM_MIGRATION_PLAN_STALE")
                if plan.status != "ready" or plan.blockers:
                    raise AlarmConfigurationError("ALARM_MIGRATION_PLAN_BLOCKED")
                if plan.digest != legacy_migration_plan_digest(
                    plan.installation_id, plan.items, actor
                ):
                    raise AlarmConfigurationError("ALARM_MIGRATION_PLAN_STALE")
                selections = {
                    (item.source_kind, item.source_key): item.entity_instance_id
                    for item in plan.items
                    if len(item.entity_instance_candidates) > 1
                    and item.entity_instance_id is not None
                }
                trusted_plan = compile_legacy_migration_plan(
                    installation_id=installation_id,
                    sources=locked_sources,
                    selections=selections,
                    actor=actor,
                )
                _raise_legacy_blockers(trusted_plan.blockers)
                supplied_by_source = {
                    (item.source_kind, item.source_key): item for item in plan.items
                }
                for trusted_item in trusted_plan.items:
                    if trusted_item.status == "migrated":
                        continue
                    supplied = supplied_by_source[
                        (trusted_item.source_kind, trusted_item.source_key)
                    ]
                    if supplied != trusted_item:
                        raise AlarmConfigurationError("ALARM_MIGRATION_PLAN_STALE")
                plan = trusted_plan
                current_items = []
                pending_specs: list[LegacyAlarmDefinitionSpec] = []
                target_ids: list[UUID] = []
                for item in sorted(plan.items, key=lambda value: (value.source_kind, value.source_key)):
                    cursor.execute(
                        """
                        SELECT target.definition_id
                        FROM t_legacy_alarm_migrations migration
                        JOIN t_legacy_alarm_migration_targets target
                          ON target.migration_id = migration.id
                        WHERE migration.source_kind = %s
                          AND migration.source_key = %s
                          AND migration.state = 'migrated'
                        ORDER BY target.definition_id
                        """,
                        (item.source_kind, item.source_key),
                    )
                    existing = tuple(row[0] for row in cursor.fetchall())
                    if existing:
                        current_items.append(
                            replace(item, status="migrated", target_definition_ids=existing)
                        )
                        target_ids.extend(existing)
                        continue
                    if item.status != "ready" or item.blockers or not item.definitions:
                        raise AlarmConfigurationError("ALARM_MIGRATION_PLAN_BLOCKED")
                    for definition in item.definitions:
                        if not self._legacy_target_is_current(
                            cursor,
                            definition,
                            identity_installation_id=identity_installation_id,
                        ):
                            raise AlarmConfigurationError("ALARM_MIGRATION_PLAN_STALE")
                    pending_specs.extend(item.definitions)
                    current_items.append(item)

                installed_ids: tuple[UUID, ...] = ()
                if pending_specs:
                    definitions = tuple(
                        InstalledAlarmDefinition(
                            id=uuid4(),
                            asset_id=spec.definition_key,
                            version="legacy-migration:1",
                            installation_id=installation_id,
                            site_configuration_version=int(site_version),
                            entity_instance_id=spec.entity.id,
                            entity_definition_id=spec.entity.definition_id,
                            trigger=dict(spec.trigger),
                            trigger_duration_seconds=0,
                            recovery=dict(spec.recovery),
                            recovery_duration_seconds=0,
                            severity=spec.severity,
                            notification_throttle_seconds=0,
                        )
                        for spec in pending_specs
                    )
                    definition_plan = AlarmDefinitionPlan(
                        installation_id=installation_id,
                        site_configuration_version=int(site_version),
                        package_digest=package_digest.strip(),
                        definitions=definitions,
                        digest=_digest(
                            [definition.public_dict() for definition in definitions]
                        ),
                    )
                    installed_ids = self._definition_catalog.install_definitions(
                        definition_plan,
                        transaction=connection,
                    )
                    installed_by_source: dict[tuple[str, str], list[UUID]] = {}
                    for spec, definition_id in zip(pending_specs, installed_ids, strict=True):
                        source = (spec.source_kind, spec.source_key)
                        installed_by_source.setdefault(source, []).append(definition_id)
                        cursor.execute(
                            """
                            INSERT INTO t_alarm_definition_origins
                              (definition_id, origin_type, source_kind,
                               source_key, details, actor)
                            VALUES (%s, 'legacy_migration', %s, %s, %s, %s)
                            """,
                            (
                                definition_id,
                                spec.source_kind,
                                spec.source_key,
                                Json(
                                    {
                                        "source_kind": spec.source_kind,
                                        "source_key": spec.source_key,
                                        "rule_name": spec.name,
                                        "legacy_rule": _json_value(spec.legacy_rule),
                                        "fault_map_id": (
                                            str(spec.fault_map_id)
                                            if spec.fault_map_id is not None
                                            else None
                                        ),
                                        "entity_instance_id": str(spec.entity.id),
                                        "confirmation_id": (
                                            str(spec.entity.confirmation_id)
                                            if spec.entity.confirmation_id is not None
                                            else None
                                        ),
                                    }
                                ),
                                actor,
                            ),
                        )
                    for source, source_definition_ids in installed_by_source.items():
                        candidate = next(
                            item
                            for item in current_items
                            if (item.source_kind, item.source_key) == source
                        )
                        migration_id = uuid4()
                        cursor.execute(
                            """
                            INSERT INTO t_legacy_alarm_migrations
                              (id, source_kind, source_key, state, actor, details)
                            VALUES (%s, %s, %s, 'migrated', %s, %s)
                            """,
                            (
                                migration_id,
                                source[0],
                                source[1],
                                actor,
                                Json(
                                    {
                                        "plan_digest": plan.digest,
                                        "installation_id": str(installation_id),
                                        "selected_entity_instance_id": str(
                                            candidate.entity_instance_id
                                        ),
                                        "entity_instance_candidates": [
                                            str(value)
                                            for value in candidate.entity_instance_candidates
                                        ],
                                        "confirmation_id": str(
                                            candidate.definitions[0].entity.confirmation_id
                                        ),
                                        "selection_reason": (
                                            "explicit_selection"
                                            if len(candidate.entity_instance_candidates) > 1
                                            else "unique_confirmed_binding"
                                        ),
                                    }
                                ),
                            ),
                        )
                        for definition_id in source_definition_ids:
                            cursor.execute(
                                """
                                INSERT INTO t_legacy_alarm_migration_targets
                                  (migration_id, definition_id, source_kind,
                                   source_key, origin_type)
                                VALUES (%s, %s, %s, %s, 'legacy_migration')
                                """,
                                (
                                    migration_id,
                                    definition_id,
                                    source[0],
                                    source[1],
                                ),
                            )
                    rebuilt_items = []
                    for item in current_items:
                        source = (item.source_kind, item.source_key)
                        created = tuple(installed_by_source.get(source, ()))
                        if created:
                            rebuilt_items.append(
                                replace(
                                    item,
                                    status="migrated",
                                    target_definition_ids=created,
                                )
                            )
                            target_ids.extend(created)
                        else:
                            rebuilt_items.append(item)
                    current_items = rebuilt_items
                return replace(
                    plan,
                    status="migrated",
                    items=tuple(current_items),
                    target_definition_ids=tuple(target_ids),
                )

    @staticmethod
    def _legacy_target_is_current(
        cursor: Any,
        definition: LegacyAlarmDefinitionSpec,
        *,
        identity_installation_id: UUID,
    ) -> bool:
        if definition.source_kind == "tag_alarm":
            cursor.execute(
                """
                SELECT 1
                FROM t_entity_instance_bindings binding
                JOIN t_entity_binding_confirmations confirmation
                  ON confirmation.id = binding.confirmation_audit_id
                 AND confirmation.entity_instance_id = binding.entity_instance_id
                 AND confirmation.binding_id = binding.id
                 AND confirmation.selected_tag_id = binding.tag_id
                JOIN t_entity_instances entity
                  ON entity.id = binding.entity_instance_id AND entity.active = TRUE
                JOIN t_device_instances device
                  ON device.id = entity.device_instance_id AND device.active = TRUE
                WHERE binding.active = TRUE
                  AND binding.tag_id = %s
                  AND entity.id = %s
                  AND device.identity_installation_id = %s
                """,
                (
                    UUID(definition.source_key),
                    definition.entity.id,
                    identity_installation_id,
                ),
            )
        elif definition.source_kind == "entity_alarm_binding":
            cursor.execute(
                """
                SELECT 1
                FROM t_entity_alarm_bindings alarm_binding
                JOIN t_entity_bindings old_binding
                  ON old_binding.entity_id = alarm_binding.entity_id
                 AND old_binding.enabled = TRUE
                JOIN t_entity_instance_bindings binding
                  ON binding.tag_id = old_binding.tag_id AND binding.active = TRUE
                JOIN t_entity_binding_confirmations confirmation
                  ON confirmation.id = binding.confirmation_audit_id
                 AND confirmation.entity_instance_id = binding.entity_instance_id
                 AND confirmation.binding_id = binding.id
                 AND confirmation.selected_tag_id = binding.tag_id
                JOIN t_entity_instances entity
                  ON entity.id = binding.entity_instance_id AND entity.active = TRUE
                JOIN t_device_instances device
                  ON device.id = entity.device_instance_id AND device.active = TRUE
                WHERE alarm_binding.id = %s
                  AND alarm_binding.enabled = TRUE
                  AND entity.id = %s
                  AND device.identity_installation_id = %s
                """,
                (
                    UUID(definition.source_key),
                    definition.entity.id,
                    identity_installation_id,
                ),
            )
        else:
            return False
        return cursor.fetchone() is not None

    @_persistence_operation
    def find_idempotency(
        self,
        actor: str,
        idempotency_key: str,
    ) -> tuple[UUID, str, str, AppliedAlarmConfiguration] | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT idempotency.plan_id, plan.digest,
                           idempotency.actor, idempotency.applied_result
                    FROM t_alarm_configuration_idempotency idempotency
                    JOIN t_alarm_configuration_plans plan
                      ON plan.id = idempotency.plan_id
                    WHERE idempotency.actor = %s
                      AND idempotency.idempotency_key = %s
                    """,
                    (actor, idempotency_key),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return row[0], row[1].strip(), row[2], _result_from_json(row[3])

    @_persistence_operation
    def apply_plan(
        self,
        plan: AlarmConfigurationPlan,
        *,
        idempotency_key: str,
        actor: str,
    ) -> AppliedAlarmConfiguration:
        if not idempotency_key.strip() or not actor.strip():
            raise AlarmConfigurationError("ALARM_APPLY_COMMAND_INVALID")
        request_digest = _digest({"plan_id": plan.id, "plan_digest": plan.digest})
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT current_version FROM t_site_configuration_state
                    WHERE singleton = TRUE FOR UPDATE
                    """
                )
                current_version = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT request_digest, plan_id, applied_result
                    FROM t_alarm_configuration_idempotency
                    WHERE actor = %s AND idempotency_key = %s
                    """,
                    (actor, idempotency_key),
                )
                replay = cursor.fetchone()
                if replay is not None:
                    if replay[0].strip() != request_digest or replay[1] != plan.id:
                        raise AlarmConfigurationError("IDEMPOTENCY_KEY_REUSED")
                    return _result_from_json(replay[2])

                cursor.execute(
                    """
                    SELECT canonical_plan, digest, status, planned_by
                    FROM t_alarm_configuration_plans
                    WHERE id = %s FOR UPDATE
                    """,
                    (plan.id,),
                )
                stored = cursor.fetchone()
                if stored is None:
                    raise AlarmConfigurationError("ALARM_PLAN_NOT_FOUND")
                if stored[1].strip() != plan.digest:
                    raise AlarmConfigurationError("ALARM_PLAN_DIGEST_MISMATCH")
                stored_plan = _plan_from_json(stored[0], planned_by=stored[3])
                if stored[2] != "ready" or stored_plan.status != "ready":
                    raise AlarmConfigurationError("ALARM_PLAN_BLOCKED")
                if stored_plan.base_site_configuration_version != current_version:
                    raise AlarmConfigurationError("ALARM_PLAN_STALE")

                current = self._current_site_installation(cursor, current_version)
                if current[0] != stored_plan.installation_id:
                    raise AlarmConfigurationError("ALARM_PLAN_STALE")
                result = self._apply_stored_plan(
                    cursor,
                    connection,
                    stored_plan,
                    current,
                    actor=actor,
                )
                result_value = _result_json(result)
                cursor.execute(
                    """
                    UPDATE t_alarm_configuration_plans
                    SET status = 'applied', application_id = %s, applied_by = %s,
                        applied_result = %s, applied_at = %s
                    WHERE id = %s
                    """,
                    (
                        result.id,
                        actor,
                        Json(result_value),
                        result.applied_at,
                        stored_plan.id,
                    ),
                )
                cursor.execute(
                    "UPDATE t_site_configuration_state SET current_version = %s WHERE singleton = TRUE",
                    (result.site_configuration_version,),
                )
                cursor.execute(
                    """
                    INSERT INTO t_alarm_configuration_idempotency
                      (actor, idempotency_key, request_digest, plan_id,
                       applied_installation_id, applied_result)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        actor,
                        idempotency_key,
                        request_digest,
                        stored_plan.id,
                        result.installation_id,
                        Json(result_value),
                    ),
                )
                return result

    @staticmethod
    def _current_site_installation(cursor: Any, current_version: int) -> tuple[Any, ...]:
        cursor.execute(
            """
            SELECT installation.id, installation.entity_instance_ids,
                   site.package_record_id, site.package_digest,
                   site.parameters, site.secret_references,
                   site.parameter_metadata, site.configuration_digest,
                   site.entity_identity_installation_id,
                   install_plan.parameter_contracts,
                   install_plan.parameter_sources,
                   install_plan.entity_plan
            FROM t_site_configuration_versions site
            JOIN t_solution_installations installation
              ON installation.id = site.installation_id
            JOIN t_solution_install_plans install_plan
              ON install_plan.id = installation.plan_id
            WHERE site.version = %s
            """,
            (current_version,),
        )
        row = cursor.fetchone()
        if row is None:
            raise AlarmConfigurationError("ALARM_SITE_CONFIGURATION_MISSING")
        return row

    def _apply_stored_plan(
        self,
        cursor: Any,
        connection: Any,
        plan: AlarmConfigurationPlan,
        current: tuple[Any, ...],
        *,
        actor: str,
    ) -> AppliedAlarmConfiguration:
        (
            _source_installation_id,
            entity_instance_ids,
            package_record_id,
            package_digest,
            parameters,
            secret_references,
            parameter_metadata,
            configuration_digest,
            entity_identity_installation_id,
            parameter_contracts,
            parameter_sources,
            entity_plan,
        ) = current
        next_version = plan.base_site_configuration_version + 1
        derived_plan_id = uuid4()
        derived_installation_id = uuid4()
        derived_plan_digest = _digest(
            {
                "kind": "alarm_configuration",
                "alarm_plan_digest": plan.digest,
                "base_site_configuration_version": plan.base_site_configuration_version,
                "site_configuration_version": next_version,
            }
        )
        cursor.execute(
            """
            INSERT INTO t_solution_install_plans
              (id, package_record_id, package_digest,
               base_site_configuration_version, status, items, blockers,
               parameter_contracts, parameters, secret_references,
               parameter_sources, parameter_metadata, configuration_digest,
               target_installation_id, entity_identity_installation_id,
               entity_plan, alarm_plan, digest)
            VALUES (%s, %s, %s, %s, 'ready', %s, '[]', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                derived_plan_id,
                package_record_id,
                package_digest,
                plan.base_site_configuration_version,
                Json(_json_value(plan.items)),
                Json(parameter_contracts),
                Json(parameters),
                Json(secret_references),
                Json(parameter_sources),
                Json(parameter_metadata),
                configuration_digest,
                derived_installation_id,
                entity_identity_installation_id,
                Json(entity_plan) if entity_plan is not None else None,
                Json(_json_value(plan)),
                derived_plan_digest,
            ),
        )
        cursor.execute(
            """
            INSERT INTO t_solution_installations
              (id, plan_id, package_record_id, package_digest,
               site_configuration_version, status, entity_instance_ids)
            VALUES (%s, %s, %s, %s, %s, 'installed', %s)
            """,
            (
                derived_installation_id,
                derived_plan_id,
                package_record_id,
                package_digest,
                next_version,
                list(entity_instance_ids),
            ),
        )
        cursor.execute(
            """
            INSERT INTO t_site_configuration_versions
              (version, previous_version, installation_id,
               package_record_id, package_digest, parameters,
               secret_references, parameter_metadata, configuration_digest,
               actor, entity_identity_installation_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                next_version,
                plan.base_site_configuration_version,
                derived_installation_id,
                package_record_id,
                package_digest,
                Json(parameters),
                Json(secret_references),
                Json(parameter_metadata),
                configuration_digest,
                actor,
                entity_identity_installation_id,
            ),
        )

        installable = tuple(
            item
            for item in plan.items
            if item.action in {"add", "update", "preserve"}
        )
        entity_ids = [str(item.entity_instance_id) for item in installable]
        cursor.execute(
            """
            SELECT id, definition_id FROM t_entity_instances
            WHERE id = ANY(%s::uuid[])
            """,
            (entity_ids,),
        )
        entity_definitions = {row[0]: row[1] for row in cursor.fetchall()}
        installed_definitions: list[InstalledAlarmDefinition] = []
        for item in installable:
            if item.after is None or item.entity_instance_id not in entity_definitions:
                raise AlarmConfigurationError("ALARM_PLAN_ENTITY_MISSING")
            rule = item.after["rule"]
            installed_definitions.append(
                InstalledAlarmDefinition(
                    id=uuid4(),
                    asset_id=item.definition_key,
                    version=(
                        f"rule-set:{plan.rule_set_revision.rule_set_id}:"
                        f"{plan.rule_set_revision.revision}"
                    ),
                    installation_id=derived_installation_id,
                    site_configuration_version=next_version,
                    entity_instance_id=item.entity_instance_id,
                    entity_definition_id=entity_definitions[item.entity_instance_id],
                    trigger={
                        "op": rule["trigger"]["operator"],
                        "value": rule["trigger"]["value"],
                    },
                    trigger_duration_seconds=rule["trigger_duration_seconds"],
                    recovery={
                        "op": rule["recovery"]["operator"],
                        "value": rule["recovery"]["value"],
                    },
                    recovery_duration_seconds=rule["recovery_duration_seconds"],
                    severity=rule["severity"],
                    notification_throttle_seconds=rule[
                        "notification_throttle_seconds"
                    ],
                )
            )
        definition_plan = AlarmDefinitionPlan(
            installation_id=derived_installation_id,
            site_configuration_version=next_version,
            package_digest=package_digest.strip(),
            definitions=tuple(installed_definitions),
            digest=_digest([definition.public_dict() for definition in installed_definitions]),
        )
        definition_ids = self._definition_catalog.install_definitions(
            definition_plan,
            transaction=connection,
        )
        for item, definition_id in zip(installable, definition_ids, strict=True):
            cursor.execute(
                """
                INSERT INTO t_alarm_definition_origins
                  (definition_id, origin_type, rule_set_id,
                   rule_set_revision, plan_id, details, actor)
                VALUES (%s, 'rule_set', %s, %s, %s, %s, %s)
                """,
                (
                    definition_id,
                    plan.rule_set_revision.rule_set_id,
                    plan.rule_set_revision.revision,
                    plan.id,
                    Json(
                        {
                            "definition_key": item.definition_key,
                            "rule": _json_value(item.after["rule"]),
                        }
                    ),
                    actor,
                ),
            )

        installation_audit_details = {
            "plan_id": str(derived_plan_id),
            "alarm_configuration_plan_id": str(plan.id),
            "alarm_configuration_plan_digest": plan.digest,
            "configuration_digest": configuration_digest.strip(),
        }
        cursor.execute(
            """
            INSERT INTO t_solution_delivery_audit
              (id, actor, action, installation_id, package_record_id,
               package_digest, site_configuration_version, details)
            VALUES (%s, %s, 'solution.install', %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                actor,
                derived_installation_id,
                package_record_id,
                package_digest,
                next_version,
                Json(installation_audit_details),
            ),
        )
        cursor.execute(
            """
            INSERT INTO t_audit_events
              (id, event, outcome, actor, target, details)
            VALUES (%s, 'solution.install', 'allowed', %s, %s, %s)
            """,
            (
                uuid4(),
                actor,
                f"installation:{derived_installation_id}",
                Json(
                    {
                        **installation_audit_details,
                        "package_record_id": str(package_record_id),
                        "package_digest": package_digest.strip(),
                        "site_configuration_version": next_version,
                    }
                ),
            ),
        )

        audit_event_id = uuid4()
        cursor.execute(
            """
            INSERT INTO t_audit_events
              (id, event, outcome, reason, actor, target, details)
            VALUES (%s, 'alarm.configuration.apply', 'applied',
                    'reviewed alarm configuration plan', %s, %s, %s)
            """,
            (
                audit_event_id,
                actor,
                f"alarm-configuration-plan:{plan.id}",
                Json(
                    {
                        "plan_digest": plan.digest,
                        "source_installation_id": str(plan.installation_id),
                        "derived_installation_id": str(derived_installation_id),
                        "site_configuration_version": next_version,
                        "definition_ids": [str(value) for value in definition_ids],
                    }
                ),
            ),
        )
        return AppliedAlarmConfiguration(
            id=uuid4(),
            plan_id=plan.id,
            installation_id=derived_installation_id,
            site_configuration_version=next_version,
            definition_ids=definition_ids,
            audit_event_id=audit_event_id,
            applied_at=datetime.now(timezone.utc),
            items=installable,
        )
