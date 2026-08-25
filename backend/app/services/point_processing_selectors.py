"""Deterministically freeze a declarative L2 selector into explicit members."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from uuid import UUID


class PointProcessingSelectorError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Selector:
    scope: str
    node_type: str
    entity_definition_id: str
    cardinality: str

    def __post_init__(self) -> None:
        if (
            self.scope != "descendants"
            or not self.node_type.strip()
            or not self.entity_definition_id.strip()
            or self.cardinality not in {"one", "many"}
        ):
            raise PointProcessingSelectorError(
                "POINT_PROCESSING_SELECTOR_INVALID",
                "Point processing selector contract is invalid",
            )
        object.__setattr__(self, "node_type", self.node_type.strip())
        object.__setattr__(
            self,
            "entity_definition_id",
            self.entity_definition_id.strip(),
        )


@dataclass(frozen=True)
class FrozenSelection:
    selector: Selector
    target_node_id: UUID
    configuration_revision: int
    entity_instance_ids: tuple[UUID, ...]
    digest: str


def freeze_selector(
    *,
    selector: Selector,
    target_node_id: UUID,
    configuration_revision: int,
    entity_instance_ids: tuple[UUID, ...],
) -> FrozenSelection:
    if configuration_revision < 0 or len(set(entity_instance_ids)) != len(
        entity_instance_ids
    ):
        raise PointProcessingSelectorError(
            "POINT_PROCESSING_SELECTOR_INVALID",
            "Point processing selector members are invalid",
        )
    members = tuple(sorted(entity_instance_ids, key=str))
    if not members:
        raise PointProcessingSelectorError(
            "POINT_PROCESSING_INPUT_MISSING",
            "Point processing selector matched no L2 entities",
        )
    if selector.cardinality == "one" and len(members) != 1:
        raise PointProcessingSelectorError(
            "POINT_PROCESSING_INPUT_AMBIGUOUS",
            "Single-value point processing selector matched multiple entities",
        )
    material = json.dumps(
        {
            "scope": selector.scope,
            "node_type": selector.node_type,
            "entity_definition_id": selector.entity_definition_id,
            "cardinality": selector.cardinality,
            "target_node_id": str(target_node_id),
            "configuration_revision": configuration_revision,
            "entity_instance_ids": [str(item) for item in members],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return FrozenSelection(
        selector=selector,
        target_node_id=target_node_id,
        configuration_revision=configuration_revision,
        entity_instance_ids=members,
        digest=hashlib.sha256(material.encode("utf-8")).hexdigest(),
    )
