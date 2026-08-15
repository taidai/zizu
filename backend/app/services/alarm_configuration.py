from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal, Mapping, Protocol
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
    items: tuple[AlarmConfigurationPlanItem, ...] = ()


@dataclass(frozen=True)
class LegacyAlarmSource:
    source_kind: str
    source_key: str
    display_name: str
    entity_candidates: tuple[ResolvedAlarmEntity, ...]
    level_code: str
    stored_severity: Severity | None
    trigger_rules: tuple[dict[str, Any], ...]
    fault_map_id: UUID | None = None
    fault_map_exists: bool = True
    target_definition_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class LegacyAlarmDefinitionSpec:
    definition_key: str
    name: str
    source_kind: str
    source_key: str
    entity: ResolvedAlarmEntity
    severity: Severity
    trigger: dict[str, Any]
    recovery: dict[str, Any]
    fault_map_id: UUID | None
    legacy_rule: dict[str, Any]


@dataclass(frozen=True)
class LegacyAlarmProposedDefinition:
    """一条旧规则针对某个实体候选的完整、只读编译结果。"""

    name: str
    severity: Severity | None
    trigger: dict[str, Any] | None
    recovery: dict[str, Any] | None
    blockers: tuple[dict[str, Any], ...]
    legacy_rule: dict[str, Any]
    fault_map_id: UUID | None = None
    trigger_duration_seconds: float = 0
    recovery_duration_seconds: float = 0
    notification_throttle_seconds: float = 0


@dataclass(frozen=True)
class LegacyAlarmProposedRule:
    """逐个实体候选编译全部旧规则，阻断只作用于该候选。"""

    entity_instance_id: UUID
    display_name: str
    blockers: tuple[dict[str, Any], ...]
    proposed_definitions: tuple[LegacyAlarmProposedDefinition, ...]


@dataclass(frozen=True)
class LegacyAlarmMigrationCandidate:
    source_kind: str
    source_key: str
    display_name: str
    status: str
    severity: Severity | None
    entity_instance_id: UUID | None
    entity_instance_candidates: tuple[UUID, ...]
    blockers: tuple[dict[str, Any], ...]
    target_definition_ids: tuple[UUID, ...]
    definitions: tuple[LegacyAlarmDefinitionSpec, ...] = ()
    proposed_rules: tuple[LegacyAlarmProposedRule, ...] = ()


@dataclass(frozen=True)
class LegacyAlarmMigrationPlan:
    installation_id: UUID
    status: str
    items: tuple[LegacyAlarmMigrationCandidate, ...]
    blockers: tuple[dict[str, Any], ...]
    digest: str
    target_definition_ids: tuple[UUID, ...] = ()


class AlarmConfigurationRepository(Protocol):
    def save_rule_set_revision(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision: ...
    def list_rule_set_revisions(self) -> tuple[AlarmRuleSetRevision, ...]: ...
    def get_rule_set_revision(self, rule_set_id: UUID, revision: int) -> AlarmRuleSetRevision | None: ...
    def resolve_entities(self, installation_id: UUID, selection: EntitySelection) -> tuple[ResolvedAlarmEntity, ...]: ...
    def current_site_version(self) -> int: ...
    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan: ...
    def get_plan(self, plan_id: UUID) -> AlarmConfigurationPlan | None: ...
    def current_site_context(self) -> dict[str, Any]: ...
    def find_idempotency(self, actor: str, idempotency_key: str) -> tuple[UUID, str, str, AppliedAlarmConfiguration] | None: ...
    def apply_plan(self, plan: AlarmConfigurationPlan, *, idempotency_key: str, actor: str) -> AppliedAlarmConfiguration: ...
    def list_legacy_alarm_sources(self) -> tuple[UUID, tuple[LegacyAlarmSource, ...]]: ...
    def apply_legacy_alarm_migration(self, plan: LegacyAlarmMigrationPlan, *, actor: str) -> LegacyAlarmMigrationPlan: ...


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


_LEGACY_LEVEL_SEVERITIES: dict[str, Severity] = {
    "error1": "CRITICAL",
    "error2": "MAJOR",
    "error3": "WARNING",
}

_LEGACY_BLOCKER_PRIORITY = (
    "ALARM_FAULT_MAP_UNRESOLVED",
    "ALARM_MIGRATION_AMBIGUOUS",
    "ALARM_LEGACY_RULE_UNSUPPORTED",
    "ALARM_MIGRATION_SELECTION_INVALID",
    "ALARM_ENTITY_UNRESOLVED",
    "ALARM_SEVERITY_INVALID",
    "ALARM_THRESHOLD_INVALID",
)


def _raise_legacy_blockers(blockers: tuple[dict[str, Any], ...]) -> None:
    codes = {blocker["code"] for blocker in blockers}
    for code in _LEGACY_BLOCKER_PRIORITY:
        if code in codes:
            raise AlarmConfigurationError(code)
    if blockers:
        raise AlarmConfigurationError(blockers[0]["code"])


def _legacy_condition_pair(
    rule: Mapping[str, Any],
    *,
    data_type: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    operator = str(rule.get("op", "active")).lower()
    normalized_type = data_type.strip().lower()
    if operator == "active" and normalized_type in _NUMERIC_DATA_TYPES:
        return {"op": "ne", "value": 0}, {"op": "eq", "value": 0}
    if operator == "active" and normalized_type in {"bool", "boolean"}:
        return {"op": "eq", "value": True}, {"op": "eq", "value": False}
    if operator in {"active", "fault"}:
        raise AlarmConfigurationError("ALARM_LEGACY_RULE_UNSUPPORTED")
    if operator in {"gte", "gt", "lte", "lt"}:
        if normalized_type not in _NUMERIC_DATA_TYPES:
            raise AlarmConfigurationError("ALARM_LEGACY_RULE_UNSUPPORTED")
        value = rule.get("threshold")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise AlarmConfigurationError("ALARM_THRESHOLD_INVALID")
        inverse = {"gte": "lt", "gt": "lte", "lte": "gt", "lt": "gte"}
        return {"op": operator, "value": value}, {"op": inverse[operator], "value": value}
    if operator in {"eq", "ne"}:
        value = rule.get("value")
        supported_scalar = isinstance(value, (str, bool))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                supported_scalar = math.isfinite(float(value))
            except OverflowError:
                supported_scalar = False
        if not supported_scalar:
            raise AlarmConfigurationError("ALARM_LEGACY_RULE_UNSUPPORTED")
        return {"op": operator, "value": value}, {"op": "ne" if operator == "eq" else "eq", "value": value}
    raise AlarmConfigurationError("ALARM_LEGACY_RULE_UNSUPPORTED")


def _legacy_fault_map_id(
    rule: Mapping[str, Any],
    fallback: UUID | None,
) -> UUID | None:
    reference = rule.get("fault_map_id")
    if reference is None:
        return fallback
    try:
        return UUID(str(reference))
    except ValueError as error:
        raise AlarmConfigurationError("ALARM_FAULT_MAP_UNRESOLVED") from error


def _legacy_definition_name(display_name: str, index: int, count: int) -> str:
    return display_name if count == 1 else f"{display_name}（规则 {index}）"


def _legacy_candidate(
    source: LegacyAlarmSource,
    selection: UUID | None,
) -> LegacyAlarmMigrationCandidate:
    entity_candidates = tuple(
        sorted(
            (entity for entity in source.entity_candidates if entity.confirmation_id is not None),
            key=lambda entity: str(entity.id),
        )
    )
    candidate_ids = tuple(entity.id for entity in entity_candidates)
    if source.target_definition_ids:
        return LegacyAlarmMigrationCandidate(
            source_kind=source.source_kind,
            source_key=source.source_key,
            display_name=source.display_name,
            status="migrated",
            severity=(
                _LEGACY_LEVEL_SEVERITIES.get(source.level_code)
                or source.stored_severity
            ),
            entity_instance_id=None,
            entity_instance_candidates=candidate_ids,
            blockers=(),
            target_definition_ids=tuple(source.target_definition_ids),
        )
    severity = _LEGACY_LEVEL_SEVERITIES.get(source.level_code) or source.stored_severity
    blockers: list[dict[str, Any]] = []
    if severity not in {"CRITICAL", "MAJOR", "WARNING", "INFO"}:
        blockers.append(_blocker("ALARM_SEVERITY_INVALID", "legacy severity is invalid"))
    if not source.fault_map_exists:
        blockers.append(
            _blocker(
                "ALARM_FAULT_MAP_UNRESOLVED",
                "legacy fault-map reference does not exist",
            )
        )
    selected_entity: ResolvedAlarmEntity | None = None
    if selection is not None:
        selected_entity = next(
            (entity for entity in entity_candidates if entity.id == selection),
            None,
        )
        if selected_entity is None:
            blockers.append(
                _blocker(
                    "ALARM_MIGRATION_SELECTION_INVALID",
                    "explicit entity instance is not a candidate for this source",
                )
            )
    elif not entity_candidates:
        blockers.append(
            _blocker(
                "ALARM_ENTITY_UNRESOLVED",
                "legacy alarm source has no confirmed entity instance",
            )
        )
    elif len(entity_candidates) > 1:
        blockers.append(
            _blocker(
                "ALARM_MIGRATION_AMBIGUOUS",
                "legacy alarm source resolves to multiple entity instances",
            )
        )
    else:
        selected_entity = entity_candidates[0]

    rules = tuple(source.trigger_rules) or ({"op": "active"},)
    proposed_rules: list[LegacyAlarmProposedRule] = []
    for entity in entity_candidates:
        proposed_definitions: list[LegacyAlarmProposedDefinition] = []
        proposal_blockers: list[dict[str, Any]] = []
        if severity not in {"CRITICAL", "MAJOR", "WARNING", "INFO"}:
            proposal_blockers.append(
                _blocker("ALARM_SEVERITY_INVALID", "legacy severity is invalid")
            )
        if not source.fault_map_exists:
            proposal_blockers.append(
                _blocker(
                    "ALARM_FAULT_MAP_UNRESOLVED",
                    "legacy fault-map reference does not exist",
                )
            )
        for index, legacy_rule in enumerate(rules, start=1):
            definition_blockers: list[dict[str, Any]] = []
            trigger: dict[str, Any] | None = None
            recovery: dict[str, Any] | None = None
            fault_map_id: UUID | None = None
            try:
                trigger, recovery = _legacy_condition_pair(
                    legacy_rule, data_type=entity.data_type
                )
                fault_map_id = _legacy_fault_map_id(
                    legacy_rule, source.fault_map_id
                )
            except AlarmConfigurationError as error:
                definition_blockers.append(_blocker(str(error), str(error)))
            existing_proposal_codes = {
                blocker["code"] for blocker in proposal_blockers
            }
            proposal_blockers.extend(
                blocker
                for blocker in definition_blockers
                if blocker["code"] not in existing_proposal_codes
            )
            proposed_definitions.append(
                LegacyAlarmProposedDefinition(
                    name=_legacy_definition_name(
                        source.display_name, index, len(rules)
                    ),
                    severity=severity,
                    trigger=trigger,
                    recovery=recovery,
                    blockers=tuple(definition_blockers),
                    legacy_rule=dict(legacy_rule),
                    fault_map_id=fault_map_id,
                )
            )
        proposed_rules.append(
            LegacyAlarmProposedRule(
                entity_instance_id=entity.id,
                display_name=entity.display_name,
                blockers=tuple(proposal_blockers),
                proposed_definitions=tuple(proposed_definitions),
            )
        )

    definitions: list[LegacyAlarmDefinitionSpec] = []
    selected_proposal = next(
        (
            proposal
            for proposal in proposed_rules
            if selected_entity is not None
            and proposal.entity_instance_id == selected_entity.id
        ),
        None,
    )
    if selected_proposal is not None:
        existing_codes = {blocker["code"] for blocker in blockers}
        blockers.extend(
            blocker
            for blocker in selected_proposal.blockers
            if blocker["code"] not in existing_codes
        )
    if selected_entity is not None and selected_proposal is not None and not blockers:
        for index, proposed in enumerate(
            selected_proposal.proposed_definitions, start=1
        ):
            if (
                proposed.severity is None
                or proposed.trigger is None
                or proposed.recovery is None
            ):
                raise AlarmConfigurationError("ALARM_LEGACY_RULE_UNSUPPORTED")
            definitions.append(
                LegacyAlarmDefinitionSpec(
                    definition_key=(
                        f"site.alarm.legacy.{source.source_kind}."
                        f"{source.source_key}.{index}"
                    ),
                    name=proposed.name,
                    source_kind=source.source_kind,
                    source_key=source.source_key,
                    entity=selected_entity,
                    severity=proposed.severity,
                    trigger=proposed.trigger,
                    recovery=proposed.recovery,
                    fault_map_id=proposed.fault_map_id,
                    legacy_rule=proposed.legacy_rule,
                )
            )
    return LegacyAlarmMigrationCandidate(
        source_kind=source.source_kind,
        source_key=source.source_key,
        display_name=source.display_name,
        status="blocked" if blockers else "ready",
        severity=severity,
        entity_instance_id=selected_entity.id if selected_entity else None,
        entity_instance_candidates=candidate_ids,
        blockers=tuple(blockers),
        target_definition_ids=(),
        definitions=tuple(definitions),
        proposed_rules=tuple(proposed_rules),
    )


def legacy_migration_plan_digest(
    installation_id: UUID,
    items: tuple[LegacyAlarmMigrationCandidate, ...],
    actor: str,
) -> str:
    return _digest(
        {
            "installation_id": installation_id,
            "items": items,
            "actor": actor,
        }
    )


def compile_legacy_migration_plan(
    *,
    installation_id: UUID,
    sources: tuple[LegacyAlarmSource, ...],
    selections: Mapping[tuple[str, str], UUID],
    actor: str,
) -> LegacyAlarmMigrationPlan:
    source_keys = {(source.source_kind, source.source_key) for source in sources}
    if set(selections) - source_keys:
        raise AlarmConfigurationError("ALARM_MIGRATION_SELECTION_INVALID")
    items = tuple(
        _legacy_candidate(
            source,
            selections.get((source.source_kind, source.source_key)),
        )
        for source in sorted(
            sources,
            key=lambda item: (item.source_kind, item.source_key),
        )
    )
    blockers = tuple(blocker for item in items for blocker in item.blockers)
    return LegacyAlarmMigrationPlan(
        installation_id=installation_id,
        status="blocked" if blockers else "ready",
        items=items,
        blockers=blockers,
        digest=legacy_migration_plan_digest(installation_id, items, actor),
        target_definition_ids=tuple(
            definition_id
            for item in items
            for definition_id in item.target_definition_ids
        ),
    )


class InMemoryAlarmConfigurationRepository:
    def __init__(self, *, installation_id: UUID | None = None, entities: tuple[ResolvedAlarmEntity, ...] = (), site_version: int = 1, legacy_sources: tuple[LegacyAlarmSource, ...] = ()) -> None:
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
        self._legacy_sources = list(legacy_sources)
        self.legacy_migration_write_count = 0
        self.last_legacy_migration_actor: str | None = None

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

    def list_rule_set_revisions(self) -> tuple[AlarmRuleSetRevision, ...]:
        revisions = (
            revision
            for rule_set in self._rule_sets.values()
            for revision in rule_set["revisions"].values()
        )
        return tuple(sorted(revisions, key=lambda item: (item.key, item.revision)))

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
        entities = {str(entity.id): entity for entity in self._entities}
        definitions = deepcopy(self._definitions)
        for record in definitions.values():
            payload = record["payload"]
            entity = entities.get(payload["entity_instance_id"])
            rule = payload["rule"]
            record.update({
                "entity_display_name": entity.display_name if entity else "已确认实体实例",
                "rule_name": rule["name"],
                "severity": rule["severity"],
                "trigger": rule["trigger"],
                "recovery": rule["recovery"],
                "source": record.get("origin_type", "rule_set"),
                "version_description": (
                    f"规则集第 {record.get('rule_set_revision', 1)} 版"
                ),
                "enabled": True,
                "status": "current",
            })
        return {"site_configuration_version": self._site_version, "definitions": definitions}

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
            definitions[item.definition_key] = {
                "id": definition_id,
                "payload": deepcopy(item.after),
                "origin_type": "rule_set",
                "rule_set_revision": plan.rule_set_revision.revision,
            }
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
            items=tuple(
                item for item in plan.items
                if item.action in {"add", "update", "preserve"}
            ),
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

    def list_legacy_alarm_sources(self) -> tuple[UUID, tuple[LegacyAlarmSource, ...]]:
        return self.current_installation_id, tuple(self._legacy_sources)

    def apply_legacy_alarm_migration(
        self,
        plan: LegacyAlarmMigrationPlan,
        *,
        actor: str,
    ) -> LegacyAlarmMigrationPlan:
        if not actor.strip():
            raise AlarmConfigurationError("ALARM_MIGRATION_ACTOR_INVALID")
        target_ids: list[UUID] = []
        migrated_items: list[LegacyAlarmMigrationCandidate] = []
        changed = False
        for item in plan.items:
            if item.status == "migrated":
                target_ids.extend(item.target_definition_ids)
                migrated_items.append(item)
                continue
            created = tuple(uuid4() for _definition in item.definitions)
            target_ids.extend(created)
            migrated_items.append(
                replace(item, status="migrated", target_definition_ids=created)
            )
            for index, source in enumerate(self._legacy_sources):
                if (
                    source.source_kind == item.source_kind
                    and source.source_key == item.source_key
                ):
                    if is_dataclass(source):
                        self._legacy_sources[index] = replace(
                            source, target_definition_ids=created
                        )
                    else:
                        source.target_definition_ids = created
                    break
            changed = True
        if changed:
            self.legacy_migration_write_count += 1
            self.last_legacy_migration_actor = actor
        return replace(
            plan,
            status="migrated",
            items=tuple(migrated_items),
            target_definition_ids=tuple(target_ids),
        )


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
            raise AlarmConfigurationError("ALARM_RULE_SET_NOT_FOUND")
        return self.repository.save_rule_set_revision(key=previous.key, name=previous.name, rules=rules, actor=actor)

    def list_rule_set_revisions(self) -> tuple[AlarmRuleSetRevision, ...]:
        return self.repository.list_rule_set_revisions()

    def preview_legacy_migration(
        self,
        selections: Mapping[tuple[str, str], UUID] | None = None,
    ) -> tuple[LegacyAlarmMigrationCandidate, ...]:
        return self.preview_legacy_migration_snapshot(selections)[1]

    def preview_legacy_migration_snapshot(
        self,
        selections: Mapping[tuple[str, str], UUID] | None = None,
    ) -> tuple[UUID, tuple[LegacyAlarmMigrationCandidate, ...]]:
        installation_id, sources = self.repository.list_legacy_alarm_sources()
        plan = compile_legacy_migration_plan(
            installation_id=installation_id,
            sources=sources,
            selections=dict(selections or {}),
            actor="preview",
        )
        return installation_id, plan.items

    def apply_legacy_migration(
        self,
        *,
        installation_id: UUID,
        selections: Mapping[tuple[str, str], UUID],
        actor: str,
    ) -> LegacyAlarmMigrationPlan:
        if not actor.strip():
            raise AlarmConfigurationError("ALARM_MIGRATION_ACTOR_INVALID")
        current_installation_id, sources = self.repository.list_legacy_alarm_sources()
        if installation_id != current_installation_id:
            raise AlarmConfigurationError("ALARM_MIGRATION_INSTALLATION_STALE")
        plan = compile_legacy_migration_plan(
            installation_id=installation_id,
            sources=sources,
            selections=dict(selections),
            actor=actor,
        )
        _raise_legacy_blockers(plan.blockers)
        return self.repository.apply_legacy_alarm_migration(plan, actor=actor)

    def plan(self, command: PlanAlarmConfiguration) -> AlarmConfigurationPlan:
        if not command.planned_by.strip():
            raise AlarmConfigurationError("ALARM_PLAN_ACTOR_INVALID")
        selection_values = (
            command.selection.entity_instance_ids,
            command.selection.device_instance_ids,
            command.selection.entity_definition_ids,
        )
        if any(len(values) != len(set(values)) for values in selection_values):
            raise AlarmConfigurationError("ALARM_RULE_CONFLICT")
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
