"""告警定义的安装期资产记录。

告警运行时不接受调用方拼装阈值或目标；安装器把经过包校验的定义绑定到实体实例，
运行期只按稳定定义 ID 读取它们。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class InstalledAlarmDefinition:
    id: UUID
    asset_id: str
    version: str
    installation_id: UUID
    site_configuration_version: int
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
        value["installation_id"] = str(self.installation_id)
        value["entity_instance_id"] = str(self.entity_instance_id)
        return value


@dataclass(frozen=True)
class AlarmDefinitionPlan:
    installation_id: UUID
    site_configuration_version: int
    package_digest: str
    definitions: tuple[InstalledAlarmDefinition, ...]
    digest: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "installation_id": str(self.installation_id),
            "site_configuration_version": self.site_configuration_version,
            "package_digest": self.package_digest,
            "definitions": [item.public_dict() for item in self.definitions],
            "digest": self.digest,
        }


class AlarmDefinitionInstaller(Protocol):
    def install_definitions(
        self,
        plan: AlarmDefinitionPlan,
        transaction: Any | None = None,
    ) -> tuple[UUID, ...]: ...
