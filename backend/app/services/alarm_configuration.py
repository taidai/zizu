from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

Severity = Literal["CRITICAL", "MAJOR", "WARNING", "INFO"]


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


class AlarmConfigurationRepository(Protocol):
    def save_rule_set_revision(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision: ...
    def get_rule_set_revision(self, rule_set_id: UUID, revision: int) -> AlarmRuleSetRevision | None: ...
    def resolve_entities(self, installation_id: UUID, selection: EntitySelection) -> tuple[ResolvedAlarmEntity, ...]: ...
    def current_site_version(self) -> int: ...
    def save_plan(self, plan: AlarmConfigurationPlan) -> AlarmConfigurationPlan: ...


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


class InMemoryAlarmConfigurationRepository:
    def __init__(self, *, installation_id: UUID | None = None, entities: tuple[ResolvedAlarmEntity, ...] = (), site_version: int = 1) -> None:
        self.current_installation_id = installation_id or uuid4()
        self.entity_ids = tuple(entity.id for entity in entities)
        self._entities = tuple(entities)
        self._site_version = site_version
        self._rule_sets: dict[UUID, dict[str, Any]] = {}
        self._rule_set_ids_by_key: dict[str, UUID] = {}
        self.plans: list[AlarmConfigurationPlan] = []

    def save_rule_set_revision(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        rule_set_id = self._rule_set_ids_by_key.setdefault(key, uuid4())
        revision_number = self._rule_sets.get(rule_set_id, {}).get("revision", 0) + 1
        normalized_rules = tuple(sorted(rules, key=lambda item: item.id))
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
        return plan


class AlarmConfiguration:
    def __init__(self, repository: AlarmConfigurationRepository) -> None:
        self.repository = repository

    def create_rule_set(self, *, key: str, name: str, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        return self.repository.save_rule_set_revision(key=key, name=name, rules=rules, actor=actor)

    def create_rule_set_revision(self, *, rule_set_id: UUID, rules: tuple[AlarmRule, ...], actor: str) -> AlarmRuleSetRevision:
        previous = self.repository.get_rule_set_revision(rule_set_id, 1)
        if previous is None:
            raise AlarmConfigurationError(f"unknown rule set: {rule_set_id}")
        return self.repository.save_rule_set_revision(key=previous.key, name=previous.name, rules=rules, actor=actor)

    def plan(self, command: PlanAlarmConfiguration) -> AlarmConfigurationPlan:
        revision = self.repository.get_rule_set_revision(command.rule_set_id, command.rule_set_revision)
        if revision is None:
            raise AlarmConfigurationError("rule set revision not found")
        entities = self.repository.resolve_entities(command.installation_id, command.selection)
        items = tuple(
            AlarmConfigurationPlanItem(
                definition_key=f"site.alarm.{revision.key}.{entity.id}.{alarm_rule.id}",
                entity_instance_id=entity.id,
                rule_id=alarm_rule.id,
                action="add",
                before=None,
                after={"rule": _rule_payload(alarm_rule), "entity_instance_id": str(entity.id)},
                blockers=(),
            )
            for entity in entities
            for alarm_rule in revision.rules
        )
        blockers: tuple[dict[str, Any], ...] = ()
        if not entities:
            blockers = ({"code": "NO_ENTITIES", "message": "no confirmed alarm entities resolved"},)
        digest = _digest({
            "base_site_configuration_version": self.repository.current_site_version(),
            "installation_id": command.installation_id,
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
            blockers=blockers,
            digest=digest,
        )
        return self.repository.save_plan(plan)
