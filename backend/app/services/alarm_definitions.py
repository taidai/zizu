"""Immutable L2 alarm definition installation records."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class InstalledAlarmDefinition:
    id: UUID
    asset_id: str
    version: str
    configuration_revision: int
    entity_instance_id: UUID
    entity_definition_id: str
    trigger: dict[str, Any]
    trigger_duration_seconds: float
    recovery: dict[str, Any]
    recovery_duration_seconds: float
    severity: str
    notification_throttle_seconds: float

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = str(self.id)
        value["entity_instance_id"] = str(self.entity_instance_id)
        return value


@dataclass(frozen=True)
class AlarmDefinitionPlan:
    configuration_revision: int
    content_digest: str
    definitions: tuple[InstalledAlarmDefinition, ...]
    digest: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "configuration_revision": self.configuration_revision,
            "content_digest": self.content_digest,
            "definitions": [item.public_dict() for item in self.definitions],
            "digest": self.digest,
        }


class AlarmDefinitionInstaller(Protocol):
    def install_definitions(self, plan: AlarmDefinitionPlan, transaction: Any | None = None) -> tuple[UUID, ...]: ...
