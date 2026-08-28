"""L2-only alarm configuration domain.

Alarm rules bind stable L2 entity UUIDs. Planning is read-only; apply is the
single configuration publication boundary.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from app.services.data_trunk_contracts import DataTrunkError
from app.services.alarm_runtime import GOOD_QUALITY, match_alarm_condition


Severity = Literal["CRITICAL", "MAJOR", "WARNING", "INFO"]


class AlarmConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class AlarmRule:
    id: str
    name: str
    severity: Severity
    trigger: dict[str, Any]
    trigger_duration_seconds: float
    recovery: dict[str, Any]
    recovery_duration_seconds: float
    notification_throttle_seconds: float
    unit: str | None = None
    fault_map_id: UUID | None = None


@dataclass(frozen=True)
class AlarmRuleSetRevision:
    rule_set_id: UUID
    key: str
    name: str
    revision: int
    rules: tuple[AlarmRule, ...]
    digest: str


@dataclass(frozen=True)
class AlarmRuleGroup:
    rule_set_id: UUID
    key: str
    name: str
    latest_revision: int
    last_non_empty_revision: int | None
    entity_instance_ids: tuple[UUID, ...]
    enabled_entity_instance_ids: tuple[UUID, ...]
    device_count: int
    rule_count: int
    highest_severity: Severity | None


@dataclass(frozen=True)
class ResolvedAlarmEntity:
    id: UUID
    node_id: UUID
    definition_id: str
    display_name: str
    data_type: str
    unit: str | None


@dataclass(frozen=True)
class AlarmRuleTrial:
    entity_instance_id: UUID
    trigger_matches: bool
    recovery_matches: bool
    description: str


@dataclass(frozen=True)
class EntitySelection:
    entity_instance_ids: tuple[UUID, ...] = ()
    node_ids: tuple[UUID, ...] = ()
    entity_definition_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanAlarmConfiguration:
    selection: EntitySelection
    rule_set_id: UUID
    rule_set_revision: int
    planned_by: str


@dataclass(frozen=True)
class AlarmConfigurationPlanItem:
    definition_key: str
    entity_instance_id: UUID
    rule_id: str
    action: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    blockers: tuple[dict[str, Any], ...] = ()
    before_definition_id: UUID | None = None


@dataclass(frozen=True)
class AlarmConfigurationPlan:
    id: UUID
    base_configuration_revision: int
    rule_set_revision: AlarmRuleSetRevision
    status: str
    items: tuple[AlarmConfigurationPlanItem, ...]
    blockers: tuple[dict[str, Any], ...]
    digest: str
    planned_by: str
    applied_result: AppliedAlarmConfiguration | None = None


@dataclass(frozen=True)
class ApplyAlarmConfigurationPlan:
    plan_id: UUID
    plan_digest: str
    idempotency_key: str
    actor: str


@dataclass(frozen=True)
class AppliedAlarmConfiguration:
    id: UUID
    plan_id: UUID
    configuration_revision: int
    definition_ids: tuple[UUID, ...]
    audit_event_id: UUID
    applied_at: Any
    items: tuple[AlarmConfigurationPlanItem, ...] = ()


class AlarmConfigurationRepository(Protocol):
    def save_rule_set_revision(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision: ...
    def list_rule_set_revisions(self) -> tuple[AlarmRuleSetRevision, ...]: ...
    def list_rule_groups(self) -> tuple[AlarmRuleGroup, ...]: ...
    def get_rule_set_revision(self, rule_set_id: UUID, revision: int) -> AlarmRuleSetRevision | None: ...
    def resolve_entities(self, selection: EntitySelection) -> tuple[ResolvedAlarmEntity, ...]: ...
    def current_configuration_revision(self) -> int: ...
    def current_configuration(self) -> dict[str, Any]: ...
    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan: ...
    def get_plan(self, plan_id: UUID) -> AlarmConfigurationPlan | None: ...
    def apply_plan(self, plan: AlarmConfigurationPlan, *, idempotency_key: str, actor: str) -> AppliedAlarmConfiguration: ...


def _json_value(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(value))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def canonical_digest(value: Any) -> str:
    payload = json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains"}
_ORDERED = {"gt", "gte", "lt", "lte"}
_MEMBERSHIP = {"contains", "not_contains"}
_NUMERIC = {"FLOAT", "INT", "NUMBER", "NUMERIC", "DOUBLE", "DECIMAL"}


def _blocker(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def _validate_rule(rule: AlarmRule) -> tuple[dict[str, Any], ...]:
    issues: list[dict[str, Any]] = []
    for side, condition in (("trigger", rule.trigger), ("recovery", rule.recovery)):
        if condition.get("operator") not in _OPERATORS:
            issues.append(_blocker("ALARM_OPERATOR_UNSUPPORTED", f"{side} operator is unsupported"))
        value = condition.get("value")
        if isinstance(value, float) and not math.isfinite(value):
            issues.append(_blocker("ALARM_THRESHOLD_INVALID", f"{side} threshold must be finite"))
    durations = (rule.trigger_duration_seconds, rule.recovery_duration_seconds, rule.notification_throttle_seconds)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in durations):
        issues.append(_blocker("ALARM_DURATION_INVALID", "alarm durations must be finite and non-negative"))
    return tuple(issues)


def _binding_issues(entity: ResolvedAlarmEntity, rule: AlarmRule) -> tuple[dict[str, Any], ...]:
    issues: list[dict[str, Any]] = []
    if (rule.trigger.get("operator") in _ORDERED or rule.recovery.get("operator") in _ORDERED) and entity.data_type.upper() not in _NUMERIC:
        issues.append(_blocker("ALARM_DATA_TYPE_UNSUPPORTED", "ordered alarm comparisons require a numeric L2 entity"))
    if (rule.trigger.get("operator") in _MEMBERSHIP or rule.recovery.get("operator") in _MEMBERSHIP) and entity.data_type.upper() != "CODE_SET":
        issues.append(_blocker("ALARM_DATA_TYPE_UNSUPPORTED", "membership alarm comparisons require a CODE_SET L2 entity"))
    if rule.unit is not None and (entity.unit or "").strip() != rule.unit.strip():
        issues.append(_blocker("ALARM_UNIT_MISMATCH", "alarm rule and L2 entity units must match"))
    return tuple(issues)


def validate_rules(rules: tuple[AlarmRule, ...]) -> None:
    if len(rules) > 20:
        raise AlarmConfigurationError("rule count must not exceed 20")
    ids = [rule.id.strip() for rule in rules]
    if not all(ids) or len(ids) != len(set(ids)):
        raise AlarmConfigurationError("ALARM_RULE_CONFLICT")
    if any(rule.severity not in {"CRITICAL", "MAJOR", "WARNING", "INFO"} for rule in rules):
        raise AlarmConfigurationError("ALARM_SEVERITY_INVALID")


class AlarmConfiguration:
    def __init__(
        self,
        repository: AlarmConfigurationRepository,
        *,
        runtime_gate: Any | None = None,
    ) -> None:
        self.repository = repository
        self._runtime_gate = runtime_gate

    def create_rule_set(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        validate_rules(rules)
        return self.repository.save_rule_set_revision(key=key, name=name, rules=rules, actor=actor)

    def create_rule_set_revision(self, *, rule_set_id: UUID, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        validate_rules(rules)
        previous = self.repository.get_rule_set_revision(rule_set_id, 1)
        if previous is None:
            raise AlarmConfigurationError("ALARM_RULE_SET_NOT_FOUND")
        return self.repository.save_rule_set_revision(key=previous.key, name=previous.name, rules=rules, actor=actor)

    def list_rule_set_revisions(self) -> tuple[AlarmRuleSetRevision, ...]:
        return self.repository.list_rule_set_revisions()

    def list_rule_groups(self) -> tuple[AlarmRuleGroup, ...]:
        return self.repository.list_rule_groups()

    def trial(
        self,
        *,
        entity_instance_id: UUID,
        rule: AlarmRule,
        value: Any,
        quality: int,
    ) -> AlarmRuleTrial:
        entities = self.repository.resolve_entities(
            EntitySelection(entity_instance_ids=(entity_instance_id,))
        )
        entity = next((item for item in entities if item.id == entity_instance_id), None)
        if entity is None:
            raise AlarmConfigurationError("ALARM_ENTITY_UNRESOLVED")
        issues = _validate_rule(rule) + _binding_issues(entity, rule)
        if issues:
            raise AlarmConfigurationError(issues[0]["code"])
        if quality != GOOD_QUALITY:
            return AlarmRuleTrial(
                entity_instance_id,
                False,
                False,
                f"{rule.name}：质量非 GOOD，不触发也不恢复。",
            )
        trigger_matches = match_alarm_condition(rule.trigger, value)
        recovery_matches = match_alarm_condition(rule.recovery, value)
        trigger_text = "命中触发条件" if trigger_matches else "未命中触发条件"
        recovery_text = "命中恢复条件" if recovery_matches else "未命中恢复条件"
        return AlarmRuleTrial(
            entity_instance_id,
            trigger_matches,
            recovery_matches,
            f"{rule.name}：当前值{trigger_text}，{recovery_text}。",
        )

    def plan(self, command: PlanAlarmConfiguration) -> AlarmConfigurationPlan:
        if not command.planned_by.strip():
            raise AlarmConfigurationError("ALARM_PLAN_ACTOR_INVALID")
        selections = (command.selection.entity_instance_ids, command.selection.node_ids, command.selection.entity_definition_ids)
        if any(len(values) != len(set(values)) for values in selections):
            raise AlarmConfigurationError("ALARM_RULE_CONFLICT")
        revision = self.repository.get_rule_set_revision(command.rule_set_id, command.rule_set_revision)
        if revision is None:
            raise AlarmConfigurationError("ALARM_RULE_SET_NOT_FOUND")
        entities = tuple(sorted(self.repository.resolve_entities(command.selection), key=lambda item: str(item.id)))
        if not entities:
            raise AlarmConfigurationError("ALARM_ENTITY_UNRESOLVED")
        if len(entities) > 200:
            raise AlarmConfigurationError("entity count must not exceed 200")
        current = self.repository.current_configuration()
        existing = current["definitions"]
        items: list[AlarmConfigurationPlanItem] = []
        blockers: list[dict[str, Any]] = []
        for entity in entities:
            for rule in revision.rules:
                key = f"alarm.{revision.key}.{entity.id}.{rule.id}"
                after = {"entity_instance_id": str(entity.id), "rule": _json_value(rule)}
                before_record = existing.get(key)
                before = None if before_record is None else before_record["payload"]
                issues = _validate_rule(rule) + _binding_issues(entity, rule)
                blockers.extend(issues)
                action = "block" if issues else ("add" if before is None else "preserve" if before == after else "update")
                items.append(AlarmConfigurationPlanItem(key, entity.id, rule.id, action, before, deepcopy(after), issues, None if before_record is None else before_record["id"]))
        selected_ids = {str(item.id) for item in entities}
        prefix = f"alarm.{revision.key}."
        desired = {item.definition_key for item in items}
        for key, record in existing.items():
            payload = record["payload"]
            if key.startswith(prefix) and payload.get("entity_instance_id") in selected_ids and key not in desired:
                items.append(AlarmConfigurationPlanItem(key, UUID(payload["entity_instance_id"]), payload.get("rule", {}).get("id", key.rsplit(".", 1)[-1]), "delete_candidate", payload, None, (), record["id"]))
        items.sort(key=lambda item: item.definition_key)
        if len(items) > 2000:
            raise AlarmConfigurationError("expanded definition count must not exceed 2000")
        base = self.repository.current_configuration_revision()
        digest = canonical_digest({"base_configuration_revision": base, "planned_by": command.planned_by, "rule_set_digest": revision.digest, "selection": command.selection, "items": tuple(items)})
        return self.repository.save_plan(AlarmConfigurationPlan(uuid4(), base, revision, "blocked" if blockers else "ready", tuple(items), tuple(blockers), digest, command.planned_by))

    def apply(self, command: ApplyAlarmConfigurationPlan) -> AppliedAlarmConfiguration:
        if not command.actor.strip() or not command.idempotency_key.strip():
            raise AlarmConfigurationError("ALARM_APPLY_COMMAND_INVALID")
        plan = self.repository.get_plan(command.plan_id)
        if plan is None:
            raise AlarmConfigurationError("ALARM_PLAN_NOT_FOUND")
        if plan.digest != command.plan_digest:
            raise AlarmConfigurationError("ALARM_PLAN_DIGEST_MISMATCH")
        if plan.status == "blocked":
            raise AlarmConfigurationError("ALARM_PLAN_BLOCKED")
        if self._runtime_gate is None:
            return self.repository.apply_plan(
                plan,
                idempotency_key=command.idempotency_key,
                actor=command.actor,
            )
        try:
            self._runtime_gate.begin_configuration_publish(
                plan.base_configuration_revision
            )
        except DataTrunkError as exc:
            raise AlarmConfigurationError(exc.code) from exc
        try:
            result = self.repository.apply_plan(
                plan,
                idempotency_key=command.idempotency_key,
                actor=command.actor,
            )
        except Exception:
            self._runtime_gate.cancel_configuration_publish()
            raise
        try:
            self._runtime_gate.reconcile_configuration_runtime()
        except DataTrunkError as exc:
            raise AlarmConfigurationError(exc.code) from exc
        return result


__all__ = [name for name in globals() if not name.startswith("_")]
