"""消费者只读的实体实例目录，不暴露物理点位或绑定内部标识。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol
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

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = str(self.id)
        value["device_instance_id"] = str(self.device_instance_id)
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
) -> tuple[tuple[str, str, UUID], ...]:
    """Reject new legacy references and validate stable rule input instance IDs."""
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
    if not isinstance(source_ids, list) or not isinstance(input_mappings, dict):
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
    return normalized
