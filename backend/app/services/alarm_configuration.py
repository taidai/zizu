from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

Severity = Literal["CRITICAL", "MAJOR", "WARNING", "INFO"]


class _FrozenDict(dict[str, Any]):
    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("mapping is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


class AlarmConfigurationError(ValueError):
    """Raised when an alarm configuration cannot be planned safely."""


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger", _freeze(deepcopy(self.trigger)))
        object.__setattr__(self, "recovery", _freeze(deepcopy(self.recovery)))


@dataclass(frozen=True)
class AlarmRuleSetRevision:
    rule_set_id: UUID
    key: str
    name: str
    revision: int
    rules: tuple[AlarmRule, ...]
    digest: str


@dataclass(frozen=True)
class ResolvedAlarmEntity:
    id: UUID
    device_instance_id: UUID
    definition_id: str
    display_name: str
    data_type: str
    unit: str | None
    confirmation_id: UUID | None


@dataclass(frozen=True)
class EntitySelection:
    entity_instance_ids: tuple[UUID, ...] = ()
    device_instance_ids: tuple[UUID, ...] = ()
    entity_definition_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanAlarmConfiguration:
    installation_id: UUID
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
    blockers: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AlarmConfigurationPlan:
    id: UUID
    installation_id: UUID
    base_site_configuration_version: int
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
    installation_id: UUID
    site_configuration_version: int
    definition_ids: tuple[UUID, ...]
    audit_event_id: UUID
    applied_at: datetime


class AlarmConfigurationRepository(Protocol):
    def save_rule_set_revision(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision: ...
    def get_rule_set_revision(self, rule_set_id: UUID, revision: int) -> AlarmRuleSetRevision | None: ...
    def resolve_entities(self, installation_id: UUID, selection: EntitySelection) -> tuple[ResolvedAlarmEntity, ...]: ...
    def current_site_version(self) -> int: ...
    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan: ...
    def get_plan(self, plan_id: UUID) -> AlarmConfigurationPlan | None: ...
    def current_site_context(self) -> dict[str, Any]: ...
    def find_idempotency(self, actor: str, idempotency_key: str) -> tuple[UUID, str, str, AppliedAlarmConfiguration] | None: ...
    def apply_plan(self, plan: AlarmConfigurationPlan, *, idempotency_key: str, actor: str) -> AppliedAlarmConfiguration: ...


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _rule_payload(rule: AlarmRule) -> dict[str, Any]:
    return _json_value(asdict(rule))


def _clone_rule(rule: AlarmRule) -> AlarmRule:
    return AlarmRule(
        id=rule.id,
        name=rule.name,
        severity=rule.severity,
        trigger=deepcopy(dict(rule.trigger)),
        trigger_duration_seconds=rule.trigger_duration_seconds,
        recovery=deepcopy(dict(rule.recovery)),
        recovery_duration_seconds=rule.recovery_duration_seconds,
        notification_throttle_seconds=rule.notification_throttle_seconds,
        unit=rule.unit,
        fault_map_id=rule.fault_map_id,
    )


def _validate_rules(rules: tuple[AlarmRule, ...]) -> None:
    if len(rules) > 20:
        raise AlarmConfigurationError("rule count must not exceed 20")
    ids = [rule.id for rule in rules]
    if any(not rule_id.strip() for rule_id in ids):
        raise AlarmConfigurationError("rule id must be non-empty")
    if len(ids) != len(set(ids)):
        raise AlarmConfigurationError("rule ids must be unique")
    allowed = {"CRITICAL", "MAJOR", "WARNING", "INFO"}
    if any(rule.severity not in allowed for rule in rules):
        raise AlarmConfigurationError("rule severity is invalid")


_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte"}
_ORDERED_OPERATORS = {"gt", "gte", "lt", "lte"}
_NUMERIC_DATA_TYPES = {"number", "numeric", "integer", "int", "float", "double", "decimal"}


def _blocker(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def _condition_issues(condition: dict[str, Any], *, side: str) -> tuple[dict[str, Any], ...]:
    operator = condition.get("operator")
    value = condition.get("value")
    if operator not in _OPERATORS:
        return (_blocker("ALARM_OPERATOR_UNSUPPORTED", f"{side} operator is unsupported"),)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return (_blocker("ALARM_THRESHOLD_INVALID", f"{side} threshold must be finite"),)
    return ()


def _rule_issues(rule: AlarmRule) -> tuple[dict[str, Any], ...]:
    issues = list(_condition_issues(rule.trigger, side="trigger"))
    issues.extend(_condition_issues(rule.recovery, side="recovery"))
    durations = (rule.trigger_duration_seconds, rule.recovery_duration_seconds, rule.notification_throttle_seconds)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0 for value in durations):
        issues.append(_blocker("ALARM_DURATION_INVALID", "alarm durations must be finite and non-negative"))
    trigger_operator, recovery_operator = rule.trigger.get("operator"), rule.recovery.get("operator")
    trigger_value, recovery_value = rule.trigger.get("value"), rule.recovery.get("value")
    if trigger_operator in {"gt", "gte"} and recovery_operator in {"lt", "lte"}:
        if isinstance(trigger_value, (int, float)) and isinstance(recovery_value, (int, float)) and recovery_value >= trigger_value:
            issues.append(_blocker("ALARM_THRESHOLD_INVALID", "high alarm recovery threshold must be lower than trigger"))
    elif trigger_operator in {"lt", "lte"} and recovery_operator in {"gt", "gte"}:
        if isinstance(trigger_value, (int, float)) and isinstance(recovery_value, (int, float)) and recovery_value <= trigger_value:
            issues.append(_blocker("ALARM_THRESHOLD_INVALID", "low alarm recovery threshold must be higher than trigger"))
    elif trigger_operator in _ORDERED_OPERATORS or recovery_operator in _ORDERED_OPERATORS:
        issues.append(_blocker("ALARM_THRESHOLD_INVALID", "trigger and recovery operators must use opposite directions"))
    return tuple(issues)


def _binding_issues(entity: ResolvedAlarmEntity, rule: AlarmRule) -> tuple[dict[str, Any], ...]:
    issues: list[dict[str, Any]] = []
    if entity.confirmation_id is None:
        issues.append(_blocker("ALARM_ENTITY_UNRESOLVED", "entity is not confirmed and active"))
    if (rule.trigger.get("operator") in _ORDERED_OPERATORS or rule.recovery.get("operator") in _ORDERED_OPERATORS) and entity.data_type.strip().lower() not in _NUMERIC_DATA_TYPES:
        issues.append(_blocker("ALARM_DATA_TYPE_UNSUPPORTED", "ordered alarm comparisons require a numeric entity"))
    rule_unit = rule.unit.strip() if rule.unit is not None else None
    entity_unit = entity.unit.strip() if entity.unit is not None else None
    if rule_unit is not None and rule_unit != entity_unit:
        issues.append(_blocker("ALARM_UNIT_MISMATCH", "alarm rule and entity units must match"))
    return tuple(issues)


class InMemoryAlarmConfigurationRepository:
    def __init__(self, *, installation_id: UUID | None = None, entities: tuple[ResolvedAlarmEntity, ...] = (), site_version: int = 1) -> None:
        self.current_installation_id = installation_id or uuid4()
        self.entity_ids = tuple(entity.id for entity in entities)
        self._entities = tuple(entities)
        self._site_version = site_version
        self._rule_sets: dict[UUID, dict[str, Any]] = {}
        self._rule_set_ids_by_key: dict[str, UUID] = {}
        self.plans: list[AlarmConfigurationPlan] = []
        self._plans_by_id: dict[UUID, AlarmConfigurationPlan] = {}
        self._definitions: dict[str, dict[str, Any]] = {}
        self._current_pointers: dict[str, UUID] = {}
        self._idempotency: dict[tuple[str, str], tuple[UUID, str, str, AppliedAlarmConfiguration]] = {}
        self._derived_installation_id = uuid4()
        self._audit_events: dict[UUID, dict[str, Any]] = {}
        self._apply_lock = RLock()
        self.applied_count = 0
        self.fail_audit = False

    def save_rule_set_revision(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        _validate_rules(rules)
        rule_set_id = self._rule_set_ids_by_key.setdefault(key, uuid4())
        revision_number = self._rule_sets.get(rule_set_id, {}).get("revision", 0) + 1
        normalized_rules = tuple(sorted((_clone_rule(rule) for rule in rules), key=lambda item: item.id))
        revision = AlarmRuleSetRevision(
            rule_set_id=rule_set_id,
            key=key,
            name=name,
            revision=revision_number,
            rules=normalized_rules,
            digest=_digest({"key": key, "name": name, "rules": [_rule_payload(rule) for rule in normalized_rules]}),
        )
        rule_set = self._rule_sets.setdefault(rule_set_id, {"revision": 0, "revisions": {}})
        rule_set["revision"] = revision_number
        rule_set["revisions"][revision_number] = revision
        rule_set["actor"] = actor
        return revision

    def get_rule_set_revision(self, rule_set_id: UUID, revision: int) -> AlarmRuleSetRevision | None:
        rule_set = self._rule_sets.get(rule_set_id)
        return None if rule_set is None else rule_set["revisions"].get(revision)

    def resolve_entities(self, installation_id: UUID, selection: EntitySelection) -> tuple[ResolvedAlarmEntity, ...]:
        if installation_id != self.current_installation_id:
            return ()
        entities = self._entities
        if selection.entity_instance_ids:
            wanted = set(selection.entity_instance_ids)
            entities = tuple(entity for entity in entities if entity.id in wanted)
        if selection.device_instance_ids:
            wanted = set(selection.device_instance_ids)
            entities = tuple(entity for entity in entities if entity.device_instance_id in wanted)
        if selection.entity_definition_ids:
            wanted = set(selection.entity_definition_ids)
            entities = tuple(entity for entity in entities if entity.definition_id in wanted)
        return tuple(sorted(entities, key=lambda entity: entity.id))

    def current_site_version(self) -> int:
        return self._site_version

    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan:
        self.plans.append(plan)
        self._plans_by_id[plan.id] = plan
        return plan

    def get_plan(self, plan_id: UUID) -> AlarmConfigurationPlan | None:
        return self._plans_by_id.get(plan_id)

    def current_site_context(self) -> dict[str, Any]:
        return {"site_configuration_version": self._site_version, "definitions": deepcopy(self._definitions)}

    def find_idempotency(self, actor: str, idempotency_key: str) -> tuple[UUID, str, str, AppliedAlarmConfiguration] | None:
        return self._idempotency.get((actor, idempotency_key))

    def _stage_apply_plan(self, plan: AlarmConfigurationPlan, *, idempotency_key: str, actor: str) -> AppliedAlarmConfiguration:
        definitions = deepcopy(self._definitions)
        pointers = dict(self._current_pointers)
        definition_ids: list[UUID] = []
        for item in plan.items:
            if item.action not in {"add", "update", "preserve"}:
                continue
            existing = definitions.get(item.definition_key)
            definition_id = existing["id"] if existing is not None else uuid4()
            definitions[item.definition_key] = {"id": definition_id, "payload": deepcopy(item.after)}
            pointers[item.definition_key] = definition_id
            definition_ids.append(definition_id)
        audit_event_id = uuid4()
        if self.fail_audit:
            raise AlarmConfigurationError("ALARM_AUDIT_FAILED")
        derived_installation_id = uuid4()
        result = AppliedAlarmConfiguration(
            id=uuid4(), plan_id=plan.id, installation_id=derived_installation_id,
            site_configuration_version=self._site_version + 1,
            definition_ids=tuple(definition_ids), audit_event_id=audit_event_id,
            applied_at=datetime.now(timezone.utc),
        )
        idempotency = dict(self._idempotency)
        idempotency[(actor, idempotency_key)] = (plan.id, plan.digest, actor, result)
        audits = dict(self._audit_events)
        audits[audit_event_id] = {"plan_id": plan.id, "actor": actor}
        self._definitions = definitions
        self._current_pointers = pointers
        self._site_version = result.site_configuration_version
        self._idempotency = idempotency
        self._audit_events = audits
        self._derived_installation_id = derived_installation_id
        self.applied_count += 1
        return result

    def apply_plan(self, plan: AlarmConfigurationPlan, *, idempotency_key: str, actor: str) -> AppliedAlarmConfiguration:
        if not idempotency_key.strip() or not actor.strip():
            raise AlarmConfigurationError("ALARM_APPLY_COMMAND_INVALID")
        with self._apply_lock:
            previous = self._idempotency.get((actor, idempotency_key))
            if previous is not None:
                plan_id, digest, previous_actor, result = previous
                if (plan_id, digest, previous_actor) != (plan.id, plan.digest, actor):
                    raise AlarmConfigurationError("IDEMPOTENCY_KEY_REUSED")
                return result
            stored_plan = self._plans_by_id.get(plan.id)
            if stored_plan is None:
                raise AlarmConfigurationError("ALARM_PLAN_NOT_FOUND")
            if stored_plan.digest != plan.digest:
                raise AlarmConfigurationError("ALARM_PLAN_DIGEST_MISMATCH")
            plan = stored_plan
            if plan.status != "ready":
                raise AlarmConfigurationError("ALARM_PLAN_BLOCKED")
            if plan.base_site_configuration_version != self._site_version:
                raise AlarmConfigurationError("ALARM_PLAN_STALE")
            result = self._stage_apply_plan(plan, idempotency_key=idempotency_key, actor=actor)
            applied_plan = replace(plan, status="applied", applied_result=result)
            self._plans_by_id[plan.id] = applied_plan
            self.plans = [applied_plan if item.id == plan.id else item for item in self.plans]
            return result


class AlarmConfiguration:
    def __init__(self, repository: AlarmConfigurationRepository) -> None:
        self.repository = repository

    def create_rule_set(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        _validate_rules(rules)
        return self.repository.save_rule_set_revision(key=key, name=name, rules=rules, actor=actor)

    def create_rule_set_revision(self, *, rule_set_id: UUID, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        _validate_rules(rules)
        previous = self.repository.get_rule_set_revision(rule_set_id, 1)
        if previous is None:
            raise AlarmConfigurationError(f"unknown rule set: {rule_set_id}")
        return self.repository.save_rule_set_revision(key=previous.key, name=previous.name, rules=rules, actor=actor)

    def plan(self, command: PlanAlarmConfiguration) -> AlarmConfigurationPlan:
        if not command.planned_by.strip():
            raise AlarmConfigurationError("ALARM_PLAN_ACTOR_INVALID")
        revision = self.repository.get_rule_set_revision(command.rule_set_id, command.rule_set_revision)
        if revision is None:
            raise AlarmConfigurationError("rule set revision not found")
        entities = self.repository.resolve_entities(command.installation_id, command.selection)
        if len(entities) > 200:
            raise AlarmConfigurationError("entity count must not exceed 200")
        entities = tuple(sorted(entities, key=lambda entity: entity.id))
        context = self.repository.current_site_context()
        existing_definitions = context["definitions"]
        items: list[AlarmConfigurationPlanItem] = []
        blockers: list[dict[str, Any]] = []
        for entity in entities:
            for alarm_rule in revision.rules:
                if entity.confirmation_id is None:
                    blockers.extend(_binding_issues(entity, alarm_rule))
                    continue
                definition_key = f"site.alarm.{revision.key}.{entity.id}.{alarm_rule.id}"
                after = {"rule": _rule_payload(alarm_rule), "entity_instance_id": str(entity.id)}
                before_record = existing_definitions.get(definition_key)
                before = None if before_record is None else before_record["payload"]
                item_blockers = _binding_issues(entity, alarm_rule) + _rule_issues(alarm_rule)
                if item_blockers:
                    action = "block"
                    blockers.extend(item_blockers)
                elif before is None:
                    action = "add"
                elif before == after:
                    action = "preserve"
                else:
                    action = "update"
                items.append(AlarmConfigurationPlanItem(
                    definition_key=definition_key, entity_instance_id=entity.id, rule_id=alarm_rule.id,
                    action=action, before=before, after=after, blockers=item_blockers,
                ))
        if not entities:
            blockers.append(_blocker("NO_ENTITIES", "no alarm entities resolved"))
        desired_keys = {item.definition_key for item in items}
        selected_entity_ids = {str(entity.id) for entity in entities}
        definition_prefix = f"site.alarm.{revision.key}."
        for definition_key, record in sorted(existing_definitions.items()):
            before = record["payload"]
            if (
                definition_key.startswith(definition_prefix)
                and before.get("entity_instance_id") in selected_entity_ids
                and definition_key not in desired_keys
            ):
                rule_payload = before.get("rule", {})
                items.append(AlarmConfigurationPlanItem(
                    definition_key=definition_key,
                    entity_instance_id=UUID(before["entity_instance_id"]),
                    rule_id=rule_payload.get("id", definition_key.rsplit(".", 1)[-1]),
                    action="delete_candidate",
                    before=before,
                    after=None,
                    blockers=(),
                ))
        items.sort(key=lambda item: item.definition_key)
        items = tuple(items)
        if len(items) > 2000:
            raise AlarmConfigurationError("expanded definition count must not exceed 2000")
        digest = _digest({
            "base_site_configuration_version": self.repository.current_site_version(),
            "installation_id": command.installation_id,
            "planned_by": command.planned_by,
            "rule_set_revision_digest": revision.digest,
            "selection": {
                "entity_instance_ids": tuple(sorted(command.selection.entity_instance_ids, key=str)),
                "device_instance_ids": tuple(sorted(command.selection.device_instance_ids, key=str)),
                "entity_definition_ids": tuple(sorted(command.selection.entity_definition_ids)),
            },
            "items": items,
        })
        plan = AlarmConfigurationPlan(
            id=uuid4(),
            installation_id=command.installation_id,
            base_site_configuration_version=self.repository.current_site_version(),
            rule_set_revision=revision,
            status="blocked" if blockers else "ready",
            items=items,
            blockers=tuple(blockers),
            digest=digest,
            planned_by=command.planned_by,
        )
        return self.repository.save_plan(plan)

    def apply(self, command: ApplyAlarmConfigurationPlan) -> AppliedAlarmConfiguration:
        if not command.idempotency_key.strip() or not command.actor.strip():
            raise AlarmConfigurationError("ALARM_APPLY_COMMAND_INVALID")
        plan = self.repository.get_plan(command.plan_id)
        if plan is None:
            raise AlarmConfigurationError("ALARM_PLAN_NOT_FOUND")
        if plan.digest != command.plan_digest:
            raise AlarmConfigurationError("ALARM_PLAN_DIGEST_MISMATCH")
        return self.repository.apply_plan(plan, idempotency_key=command.idempotency_key, actor=command.actor)
