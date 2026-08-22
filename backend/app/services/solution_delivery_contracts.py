"""解决方案交付门面的稳定记录与 Adapter 契约。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID


MAX_PACKAGE_ARCHIVE_BYTES = 10 * 1024 * 1024


class DeliveryError(ValueError):
    """携带稳定机器码的解决方案交付错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PackageImport:
    id: UUID
    package_id: str
    version: str
    display_name: str
    digest: str
    status: str
    acceptance_ids: tuple[str, ...]
    manifest: dict[str, Any]
    assets: dict[str, bytes]

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = str(self.id)
        value["acceptance_ids"] = list(self.acceptance_ids)
        value["parameter_contracts"] = list(self.manifest.get("parameters", []))
        value["entity_definition_ids"] = [
            item["id"]
            for item in self.manifest.get("assets", [])
            if item.get("kind") == "entity_definition"
        ]
        value["entity_slot_ids"] = [
            item["id"]
            for item in self.manifest.get("assets", [])
            if item.get("kind") == "entity_instance_slot"
        ]
        value["workbench_asset_ids"] = [
            item["id"]
            for item in self.manifest.get("assets", [])
            if item.get("kind") == "ems_workbench"
        ]
        value["policy_asset_ids"] = [
            item["id"]
            for item in self.manifest.get("assets", [])
            if item.get("kind") == "ems_policy"
        ]
        value.pop("manifest")
        value.pop("assets")
        return value


@dataclass(frozen=True)
class InstallationPlan:
    id: UUID
    package_record_id: UUID
    package_digest: str
    base_site_configuration_version: int
    status: str
    items: tuple[dict[str, Any], ...]
    blockers: tuple[dict[str, str], ...]
    parameter_contracts: tuple[dict[str, Any], ...]
    parameters: dict[str, Any]
    secret_references: dict[str, str]
    parameter_sources: dict[str, str]
    parameter_metadata: dict[str, dict[str, str]]
    configuration_digest: str
    target_installation_id: UUID
    entity_identity_installation_id: UUID
    entity_plan: dict[str, Any] | None
    digest: str
    alarm_plan: dict[str, Any] | None = None
    point_processing_plans: tuple[dict[str, Any], ...] = ()

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = str(self.id)
        value["package_record_id"] = str(self.package_record_id)
        value["target_installation_id"] = str(self.target_installation_id)
        value["entity_identity_installation_id"] = str(
            self.entity_identity_installation_id
        )
        value["items"] = list(self.items)
        value["blockers"] = list(self.blockers)
        value["parameter_contracts"] = list(self.parameter_contracts)
        return value


@dataclass(frozen=True)
class InstallationOutcome:
    id: UUID
    plan_id: UUID
    package_record_id: UUID
    package_digest: str
    site_configuration_version: int
    status: str
    entity_instance_ids: tuple[UUID, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = str(self.id)
        value["plan_id"] = str(self.plan_id)
        value["package_record_id"] = str(self.package_record_id)
        value["entity_instance_ids"] = [str(item) for item in self.entity_instance_ids]
        return value


@dataclass(frozen=True)
class SiteConfigurationVersion:
    version: int
    previous_version: int | None
    installation_id: UUID
    package_record_id: UUID
    package_digest: str
    parameters: dict[str, Any]
    secret_references: dict[str, str]
    parameter_metadata: dict[str, dict[str, str]]
    digest: str
    actor: str
    entity_identity_installation_id: UUID

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["installation_id"] = str(self.installation_id)
        value["package_record_id"] = str(self.package_record_id)
        value["entity_identity_installation_id"] = str(
            self.entity_identity_installation_id
        )
        return value


@dataclass(frozen=True)
class DeliveryReport:
    id: UUID
    installation_id: UUID
    platform_version: str
    package_id: str
    package_version: str
    package_digest: str
    site_configuration_version: int
    actor: str
    started_at: str
    finished_at: str
    duration_ms: int
    status: str
    items: tuple[dict[str, Any], ...]
    digest: str

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = str(self.id)
        value["installation_id"] = str(self.installation_id)
        value["items"] = list(self.items)
        return value


@dataclass(frozen=True)
class InstallationAuditEvent:
    """A narrow, delivery-owned projection of the append-only audit stream."""

    event: str
    outcome: str
    reason: str | None
    actor: str | None
    target: str
    created_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "outcome": self.outcome,
            "reason": self.reason,
            "actor": self.actor,
            "target": self.target,
            "created_at": self.created_at.isoformat(),
        }


class DeliveryRepository(Protocol):
    """`SolutionDelivery` 使用的最小持久化端口。"""

    def save_package(self, package: PackageImport, actor: str) -> PackageImport: ...

    def list_packages(self) -> list[PackageImport]: ...

    def get_package(self, package_record_id: UUID) -> PackageImport | None: ...

    def site_configuration_version(self, transaction: Any | None = None) -> int: ...

    def save_plan(self, plan: InstallationPlan) -> InstallationPlan: ...

    def get_plan(self, plan_id: UUID) -> InstallationPlan | None: ...

    def get_idempotent_installation(
        self,
        actor: str,
        key: str,
        request_digest: str,
    ) -> InstallationOutcome | None: ...

    def install(
        self,
        plan: InstallationPlan,
        actor: str,
        key: str,
        request_digest: str,
        apply_entities: Callable[[Any | None], tuple[UUID, ...]] | None = None,
    ) -> InstallationOutcome: ...

    def list_installations(self) -> list[InstallationOutcome]: ...

    def get_installation(self, installation_id: UUID) -> InstallationOutcome | None: ...

    def get_site_configuration_version(
        self,
        version: int,
    ) -> SiteConfigurationVersion | None: ...

    def package_for_installation(
        self,
        installation: InstallationOutcome,
    ) -> PackageImport | None: ...

    def get_idempotent_report(
        self,
        actor: str,
        key: str,
        request_digest: str,
    ) -> DeliveryReport | None: ...

    def save_report(
        self,
        report: DeliveryReport,
        actor: str,
        key: str,
        request_digest: str,
    ) -> DeliveryReport: ...

    def get_report(self, report_id: UUID) -> DeliveryReport | None: ...

    def list_installation_audit_events(
        self,
        installation_id: UUID,
    ) -> list[InstallationAuditEvent]: ...


class PublicApiProbe(Protocol):
    """白名单公开 HTTP 验收探针端口。"""

    async def get(
        self,
        path: str,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...
