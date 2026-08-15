"""消费者只读的实体实例目录，不暴露物理点位或绑定内部标识。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol
from uuid import UUID


class EntityInstanceReferenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EntityInstanceDescriptor:
    id: UUID
    device_instance_id: UUID
    slot_id: str
    instance_key: str
    device_category: str
    device_display_name: str
    definition_id: str
    display_name: str
    data_type: str
    unit: str | None
    direction: str
    freshness_seconds: float
    confirmed: bool = False

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = str(self.id)
        value["device_instance_id"] = str(self.device_instance_id)
        value["confirmed"] = self.confirmed
        return value


@dataclass(frozen=True)
class LegacyEntityMigrationItem:
    legacy_entity_id: UUID
    legacy_entity_name: str
    classification: str
    candidate_entity_instance_ids: tuple[UUID, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "legacy_entity_id": str(self.legacy_entity_id),
            "legacy_entity_name": self.legacy_entity_name,
            "classification": self.classification,
            "candidate_entity_instance_ids": [
                str(item) for item in self.candidate_entity_instance_ids
            ],
        }


class EntityInstanceCatalogRepository(Protocol):
    def list_instances(self) -> tuple[EntityInstanceDescriptor, ...]: ...

    def preview_legacy(self) -> tuple[LegacyEntityMigrationItem, ...]: ...


class EntityInstanceCatalog:
    """为规则、告警、控制和工作台提供稳定实例引用。"""

    def __init__(self, repository: EntityInstanceCatalogRepository) -> None:
        self._repository = repository

    def list(self) -> tuple[EntityInstanceDescriptor, ...]:
        return self._repository.list_instances()

    def require(self, instance_ids: tuple[UUID, ...]) -> tuple[EntityInstanceDescriptor, ...]:
        by_id = {item.id: item for item in self._repository.list_instances()}
        if len(set(instance_ids)) != len(instance_ids) or any(
            instance_id not in by_id for instance_id in instance_ids
        ):
            raise EntityInstanceReferenceError(
                "ENTITY_INSTANCE_REFERENCE_INVALID",
                "Entity instance reference is missing, inactive, or duplicated",
            )
        return tuple(by_id[instance_id] for instance_id in instance_ids)

    def preview_legacy(self) -> tuple[LegacyEntityMigrationItem, ...]:
        return self._repository.preview_legacy()


def validate_rule_entity_references(
    content: dict[str, Any],
    catalog: EntityInstanceCatalog,
    *,
    has_installed_alarm_definition: Callable[[str, UUID], bool] | None = None,
) -> tuple[tuple[str, str, UUID], ...]:
    """Reject legacy control addresses and validate every stable rule instance ID."""
    config = content.get("_config", {})
    if not isinstance(config, dict):
        raise EntityInstanceReferenceError(
            "ENTITY_INSTANCE_REFERENCE_INVALID",
            "Rule entity reference configuration must be a mapping",
        )
    if "sourceEntityIds" in config:
        raise EntityInstanceReferenceError(
            "ENTITY_REFERENCE_LEGACY_FORBIDDEN",
            "Legacy global entity references require migration preview",
        )
    source_ids = config.get("sourceEntityInstanceIds", [])
    input_mappings = config.get("inputMappings", {})
    config_actions = config.get("actions", [])
    top_level_actions = content.get("actions", [])
    if (
        not isinstance(source_ids, list)
        or not isinstance(input_mappings, dict)
        or not isinstance(config_actions, list)
        or not isinstance(top_level_actions, list)
    ):
        raise EntityInstanceReferenceError(
            "ENTITY_INSTANCE_REFERENCE_INVALID",
            "Rule entity instance references are invalid",
        )
    references = [
        ("source", str(index), value) for index, value in enumerate(source_ids)
    ] + [
        ("input", str(field), value)
        for field, value in input_mappings.items()
        if value not in (None, "")
    ]
    action_references = _rule_action_references(config_actions, section="config")
    action_references.extend(
        _rule_action_references(top_level_actions, section="actions")
    )
    action_keys = [reference[1] for reference in action_references]
    if len(action_keys) != len(set(action_keys)):
        raise EntityInstanceReferenceError(
            "RULE_CONTROL_ACTION_INVALID",
            "Rule control action identifiers must be unique",
        )
    references.extend(action_references)
    if action_references:
        control_input_values = [
            *source_ids,
            *[
                value for value in input_mappings.values()
                if value not in (None, "")
            ],
        ]
        if not control_input_values:
            raise EntityInstanceReferenceError(
                "RULE_CONTROL_INPUTS_REQUIRED",
                "Automatic control rules require entity instance inputs",
            )
        if "sourceNodeIds" in config:
            raise EntityInstanceReferenceError(
                "RULE_CONTROL_LEGACY_FORBIDDEN",
                "Automatic control rules cannot read physical node sources",
            )
    try:
        normalized = tuple((kind, key, UUID(value)) for kind, key, value in references)
    except (TypeError, ValueError, AttributeError) as exc:
        raise EntityInstanceReferenceError(
            "ENTITY_INSTANCE_REFERENCE_INVALID",
            "Rule inputs must reference entity instance UUIDs",
        ) from exc
    instance_ids = tuple(dict.fromkeys(item[2] for item in normalized))
    if instance_ids:
        catalog.require(instance_ids)
    if has_installed_alarm_definition is not None:
        for action in (*config_actions, *top_level_actions):
            if isinstance(action, dict) and action.get("type") == "alarm":
                if not has_installed_alarm_definition(
                    action["alarm_definition"].strip(),
                    UUID(action["entity_instance_id"]),
                ):
                    raise EntityInstanceReferenceError(
                        "RULE_ALARM_DEFINITION_NOT_INSTALLED",
                        "Rule alarm action must reference a currently installed definition for its entity instance",
                    )
    return normalized


def _rule_action_references(
    actions: list[object],
    *,
    section: str,
) -> list[tuple[str, str, object]]:
    """Accept only declarative entity-instance targets for rule control or alarm observations."""
    references: list[tuple[str, str, object]] = []
    forbidden_fields = {
        "node", "group", "tag", "topic", "payload", "command",
        "entity_id", "entity", "entity_name", "cooldown",
    }
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise EntityInstanceReferenceError(
                "RULE_CONTROL_ACTION_INVALID",
                "Rule actions must be mappings",
            )
        action_type = action.get("type")
        if action_type == "neuron_write":
            raise EntityInstanceReferenceError(
                "RULE_CONTROL_LEGACY_FORBIDDEN",
                "Rule control actions must target an entity_instance_id, not a Neuron address",
            )
        if action_type not in {"control", "alarm"}:
            continue
        if action_type == "alarm":
            allowed = {"type", "id", "alarm_definition", "entity_instance_id", "value"}
            if set(action) != allowed:
                raise EntityInstanceReferenceError(
                    "RULE_ALARM_ACTION_INVALID",
                    "Rule alarm actions require only id, alarm_definition, entity_instance_id, and value",
                )
            if not isinstance(action.get("id"), str) or not action["id"].strip():
                raise EntityInstanceReferenceError(
                    "RULE_ALARM_ACTION_INVALID",
                    "Rule alarm actions require a stable string id",
                )
            if not isinstance(action.get("alarm_definition"), str) or not action["alarm_definition"].strip():
                raise EntityInstanceReferenceError(
                    "RULE_ALARM_ACTION_INVALID",
                    "Rule alarm actions require an installed alarm definition asset id",
                )
            references.append(("alarm", action["id"].strip(), action.get("entity_instance_id")))
            continue
        if forbidden_fields.intersection(action):
            raise EntityInstanceReferenceError(
                "RULE_CONTROL_LEGACY_FORBIDDEN",
                "Rule control actions cannot contain physical addresses, MQTT payloads, or local cooldowns",
            )
        if "entity_instance_id" not in action or "value" not in action:
            raise EntityInstanceReferenceError(
                "RULE_CONTROL_ACTION_INVALID",
                "Rule control actions require entity_instance_id and value",
            )
        if not isinstance(action.get("id"), str):
            raise EntityInstanceReferenceError(
                "RULE_CONTROL_ACTION_INVALID",
                "Rule control actions require a stable string id",
            )
        action_key = action["id"].strip()
        if not action_key or len(action_key) > 200:
            raise EntityInstanceReferenceError(
                "RULE_CONTROL_ACTION_INVALID",
                "Rule control action identifiers must be unique and at most 200 characters",
            )
        references.append(("control", action_key, action["entity_instance_id"]))
    return references
