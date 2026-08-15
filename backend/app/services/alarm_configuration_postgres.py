"""PostgreSQL persistence for reviewable, atomic alarm configuration applies."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

from psycopg2.extras import Json, register_uuid

from app.services.alarm_configuration import (
    AlarmConfigurationError,
    AlarmConfigurationPlan,
    AlarmConfigurationPlanItem,
    AlarmRule,
    AlarmRuleSetRevision,
    AppliedAlarmConfiguration,
    EntitySelection,
    ResolvedAlarmEntity,
)
from app.services.alarm_definitions import (
    AlarmDefinitionPlan,
    InstalledAlarmDefinition,
)
from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog


ConnectionFactory = Callable[[], Any]


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


def _plan_from_json(value: dict[str, Any]) -> AlarmConfigurationPlan:
    return AlarmConfigurationPlan(
        id=UUID(value["id"]),
        installation_id=UUID(value["installation_id"]),
        base_site_configuration_version=int(value["base_site_configuration_version"]),
        rule_set_revision=_revision_from_json(value["rule_set_revision"]),
        status=value["status"],
        items=tuple(
            AlarmConfigurationPlanItem(
                definition_key=item["definition_key"],
                entity_instance_id=UUID(item["entity_instance_id"]),
                rule_id=item["rule_id"],
                action=item["action"],
                before=item.get("before"),
                after=item.get("after"),
                blockers=tuple(item["blockers"]),
            )
            for item in value["items"]
        ),
        blockers=tuple(value["blockers"]),
        digest=value["digest"],
    )


def _result_json(result: AppliedAlarmConfiguration) -> dict[str, Any]:
    return _json_value(result)


def _result_from_json(value: dict[str, Any]) -> AppliedAlarmConfiguration:
    return AppliedAlarmConfiguration(
        id=UUID(value["id"]),
        plan_id=UUID(value["plan_id"]),
        installation_id=UUID(value["installation_id"]),
        site_configuration_version=int(value["site_configuration_version"]),
        definition_ids=tuple(UUID(item) for item in value["definition_ids"]),
        audit_event_id=UUID(value["audit_event_id"]),
        applied_at=datetime.fromisoformat(value["applied_at"]),
    )


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
        if self._connection_factory is None:
            from app.services.telemetry_store import get_connection

            with get_connection() as connection:
                try:
                    yield connection
                except Exception:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            return
        connection = self._connection_factory()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def save_rule_set_revision(
        self,
        *,
        key: str,
        name: str,
        rules: tuple[AlarmRule, ...],
        actor: str,
    ) -> AlarmRuleSetRevision:
        normalized_rules = tuple(sorted(rules, key=lambda rule: rule.id))
        digest = _digest(
            {
                "key": key,
                "name": name,
                "rules": [_json_value(rule) for rule in normalized_rules],
            }
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, name FROM t_alarm_rule_sets
                    WHERE rule_set_key = %s FOR UPDATE
                    """,
                    (key,),
                )
                row = cursor.fetchone()
                if row is None:
                    rule_set_id = uuid4()
                    cursor.execute(
                        """
                        INSERT INTO t_alarm_rule_sets
                          (id, rule_set_key, name, created_by)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (rule_set_id, key, name, actor),
                    )
                else:
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
                      (rule_set_id, revision, rules, digest, actor)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        rule_set_id,
                        revision_number,
                        Json(_json_value(normalized_rules)),
                        digest,
                        actor,
                    ),
                )
        return AlarmRuleSetRevision(
            rule_set_id=rule_set_id,
            key=key,
            name=name,
            revision=revision_number,
            rules=normalized_rules,
            digest=digest,
        )

    def get_rule_set_revision(
        self,
        rule_set_id: UUID,
        revision: int,
    ) -> AlarmRuleSetRevision | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT rule_set.rule_set_key, rule_set.name, revision.rules,
                           revision.digest
                    FROM t_alarm_rule_set_revisions revision
                    JOIN t_alarm_rule_sets rule_set ON rule_set.id = revision.rule_set_id
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

    def current_site_version(self) -> int:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_version FROM t_site_configuration_state WHERE singleton = TRUE"
                )
                row = cursor.fetchone()
        return int(row[0]) if row else 0

    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan:
        canonical_plan = _json_value(plan)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_alarm_configuration_plans
                      (id, source_installation_id,
                       base_site_configuration_version, rule_set_id,
                       rule_set_revision, canonical_plan, digest, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (digest) DO NOTHING
                    RETURNING canonical_plan
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
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "SELECT canonical_plan FROM t_alarm_configuration_plans WHERE digest = %s",
                        (plan.digest,),
                    )
                    row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Alarm configuration plan conflict row disappeared")
        return _plan_from_json(row[0])

    def get_plan(self, plan_id: UUID) -> AlarmConfigurationPlan | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT canonical_plan FROM t_alarm_configuration_plans WHERE id = %s",
                    (plan_id,),
                )
                row = cursor.fetchone()
        return _plan_from_json(row[0]) if row else None

    def current_site_context(self) -> dict[str, Any]:
        definitions: dict[str, dict[str, Any]] = {}
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT definition.asset_id, definition.id,
                           definition.entity_instance_id,
                           definition.trigger_condition,
                           definition.trigger_duration_seconds,
                           definition.recovery_condition,
                           definition.recovery_duration_seconds,
                           definition.severity,
                           definition.notification_throttle_seconds,
                           entity.unit, origin.details
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
            origin_rule = (row[10] or {}).get("rule")
            rule = origin_rule or {
                "id": row[0].rsplit(".", 1)[-1],
                "name": row[0],
                "severity": row[7],
                "trigger": row[3],
                "trigger_duration_seconds": row[4],
                "recovery": row[5],
                "recovery_duration_seconds": row[6],
                "notification_throttle_seconds": row[8],
                "unit": row[9],
                "fault_map_id": None,
            }
            definitions[row[0]] = {
                "id": row[1],
                "payload": {
                    "rule": rule,
                    "entity_instance_id": str(row[2]),
                },
            }
        return {
            "site_configuration_version": site_version,
            "definitions": definitions,
        }

    def find_idempotency(
        self,
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
                    WHERE idempotency.idempotency_key = %s
                    ORDER BY idempotency.created_at, idempotency.actor LIMIT 1
                    """,
                    (idempotency_key,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return row[0], row[1].strip(), row[2], _result_from_json(row[3])

    def apply_plan(
        self,
        plan: AlarmConfigurationPlan,
        *,
        idempotency_key: str,
        actor: str,
    ) -> AppliedAlarmConfiguration:
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
                    SELECT canonical_plan, digest, status
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
                stored_plan = _plan_from_json(stored[0])
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
                    SET status = 'applied', actor = %s,
                        applied_result = %s, applied_at = %s
                    WHERE id = %s
                    """,
                    (actor, Json(result_value), result.applied_at, stored_plan.id),
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
                    trigger=dict(rule["trigger"]),
                    trigger_duration_seconds=rule["trigger_duration_seconds"],
                    recovery=dict(rule["recovery"]),
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
        )
