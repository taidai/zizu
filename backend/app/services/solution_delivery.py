"""版本化解决方案包的交付深模块。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any, Callable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

from app.services.solution_delivery_contracts import (
    DeliveryError,
    DeliveryReport,
    DeliveryRepository,
    InstallationOutcome,
    InstallationPlan,
    MAX_PACKAGE_ARCHIVE_BYTES,
    PackageImport,
    PublicApiProbe,
    SiteConfigurationVersion,
)
from app.services.solution_delivery_repository import (
    InMemoryDeliveryRepository,
    PostgresDeliveryRepository,
)
from app.services.solution_package_archive import (
    _load_mapping,
    _package_digest,
    _read_archive,
    _validate_acceptance_definition,
    _validate_entity_assets,
    _validate_alarm_assets,
    _validate_alarm_lifecycle_acceptances,
    _validate_manifest,
    _validate_platform_range,
    _version_tuple,
)
from app.services.solution_parameters import (
    resolve_site_parameters,
    upgrade_parameter_conflicts,
    validate_parameter_contracts,
)
from app.services.alarm_definitions import (
    AlarmDefinitionInstaller,
    AlarmDefinitionPlan,
    InstalledAlarmDefinition,
)
from app.services.alarm_runtime import AlarmRuntime
from app.services.entity_instance_registry import (
    ApplyEntityInstancePlan,
    EntityInstanceError,
    EntityInstanceRegistry,
    PlanEntityInstances,
)
from app.services.entity_instance_runtime import EntityInstanceRuntime
from app.services.solution_workbench import validate_ems_workbench_assets
from app.services.solution_policies import (
    validate_ems_policy_assets,
    validate_policy_execution_acceptances,
)
from app.services.gateway_readiness import GatewayReadiness

__all__ = [
    "DeliveryError",
    "DeliveryReport",
    "HttpxPublicApiProbe",
    "InMemoryDeliveryRepository",
    "InstallationOutcome",
    "InstallationPlan",
    "MAX_PACKAGE_ARCHIVE_BYTES",
    "PackageImport",
    "PostgresDeliveryRepository",
    "SolutionDelivery",
    "SiteConfigurationVersion",
]


class PolicyExecutionRuntime(Protocol):
    def acceptance_evidence(
        self,
        policy_id: str,
        expected_action: str,
        command_id: str,
        installation_id: str,
    ) -> dict[str, Any]: ...


class ControlCommandEvidenceRuntime(Protocol):
    def get(self, command_id: UUID) -> Any: ...


class AuthorizationEvidenceRuntime(Protocol):
    def find_audit_event(self, event_id: UUID) -> Any | None: ...


class HttpxPublicApiProbe:
    """通过本实例公开 HTTP 接口执行白名单验收。"""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def get(self, path: str, timeout_seconds: float) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.get(path)
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}


class SolutionDelivery:
    """校验、计划、安装并验收解决方案包的深模块。"""

    def __init__(
        self,
        repository: DeliveryRepository,
        platform_version: str,
        public_api_probe: PublicApiProbe | None = None,
        entity_instance_registry: EntityInstanceRegistry | None = None,
        entity_instance_runtime: EntityInstanceRuntime | None = None,
        alarm_definitions: AlarmDefinitionInstaller | None = None,
        alarm_runtime: AlarmRuntime | None = None,
        policy_runtime: PolicyExecutionRuntime | None = None,
        control_command_runtime: ControlCommandEvidenceRuntime | None = None,
        authorization_evidence_runtime: AuthorizationEvidenceRuntime | None = None,
        gateway_readiness: GatewayReadiness | None = None,
        release_lock_reader: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._repository = repository
        self._platform_version = platform_version
        self._public_api_probe = public_api_probe
        self._entity_instance_registry = entity_instance_registry
        self._entity_instance_runtime = entity_instance_runtime
        self._alarm_definitions = alarm_definitions
        self._alarm_runtime = alarm_runtime
        self._policy_runtime = policy_runtime
        self._control_command_runtime = control_command_runtime
        self._authorization_evidence_runtime = authorization_evidence_runtime
        self._gateway_readiness = gateway_readiness
        self._release_lock_reader = release_lock_reader

    def set_policy_runtime(self, policy_runtime: PolicyExecutionRuntime) -> None:
        """Attach the runtime after construction to avoid a delivery-policy cycle."""
        self._policy_runtime = policy_runtime

    def set_control_command_runtime(self, control_command_runtime: ControlCommandEvidenceRuntime) -> None:
        """Attach the command reader in test compositions that build delivery first."""
        self._control_command_runtime = control_command_runtime

    def set_authorization_evidence_runtime(
        self,
        authorization_evidence_runtime: AuthorizationEvidenceRuntime,
    ) -> None:
        self._authorization_evidence_runtime = authorization_evidence_runtime

    def set_gateway_readiness(self, gateway_readiness: GatewayReadiness) -> None:
        self._gateway_readiness = gateway_readiness

    def import_package(self, archive: bytes) -> PackageImport:
        files = _read_archive(archive)
        manifest = _load_mapping(files.get("solution.yaml"), "MANIFEST_INVALID")
        _validate_manifest(manifest)
        _version_tuple(manifest["version"])
        _validate_platform_range(
            str(manifest["platform"]["version"]),
            self._platform_version,
        )

        declared_assets: dict[str, bytes] = {}
        asset_ids: set[str] = set()
        for asset in manifest["assets"]:
            asset_id = asset["id"]
            path = asset["path"]
            content = files.get(path)
            if content is None:
                raise DeliveryError(
                    "ASSET_REFERENCE_INVALID",
                    f"Declared asset is missing: {path}",
                )
            if hashlib.sha256(content).hexdigest() != asset["sha256"]:
                raise DeliveryError(
                    "ASSET_DIGEST_MISMATCH",
                    f"Asset digest does not match: {path}",
                )
            declared_assets[path] = content
            asset_ids.add(asset_id)

        acceptance_ids = tuple(manifest["acceptance"])
        if not acceptance_ids or any(item not in asset_ids for item in acceptance_ids):
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Acceptance references must name declared assets",
            )
        if len(set(acceptance_ids)) != len(acceptance_ids):
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Acceptance references must be unique",
            )
        for acceptance_id in acceptance_ids:
            asset = next(
                item for item in manifest["assets"] if item["id"] == acceptance_id
            )
            _validate_acceptance_definition(
                _load_mapping(declared_assets[asset["path"]], "ASSET_REFERENCE_INVALID"),
                acceptance_id,
            )
        normalized_slots = _validate_entity_assets(manifest, declared_assets)
        if normalized_slots:
            manifest["_entity_slots"] = list(normalized_slots)
        normalized_alarm_assets = _validate_alarm_assets(
            manifest,
            declared_assets,
            normalized_slots,
        )
        if normalized_alarm_assets:
            manifest["_alarm_assets"] = list(normalized_alarm_assets)
        normalized_workbenches = validate_ems_workbench_assets(
            manifest,
            declared_assets,
            normalized_slots,
            _load_mapping,
        )
        if normalized_workbenches:
            manifest["_workbench_assets"] = list(normalized_workbenches)
        normalized_policies = validate_ems_policy_assets(
            manifest,
            declared_assets,
            normalized_slots,
            _load_mapping,
        )
        if normalized_policies:
            manifest["_policy_assets"] = list(normalized_policies)
        validate_policy_execution_acceptances(
            manifest,
            declared_assets,
            normalized_policies,
            _load_mapping,
        )
        _validate_alarm_lifecycle_acceptances(
            manifest,
            declared_assets,
            normalized_alarm_assets,
        )

        package = PackageImport(
            id=uuid4(),
            package_id=manifest["id"],
            version=manifest["version"],
            display_name=manifest["displayName"],
            digest=_package_digest(files),
            status="validated",
            acceptance_ids=acceptance_ids,
            manifest=manifest,
            assets=declared_assets,
        )
        return self._repository.save_package(package)

    def list_packages(self) -> list[PackageImport]:
        return self._repository.list_packages()

    def plan_install(
        self,
        package_record_id: UUID,
        *,
        parameters: dict[str, Any] | None = None,
        secret_references: dict[str, str] | None = None,
        binding_selections: dict[str, UUID] | None = None,
        upgrade_risk_resolutions: dict[str, str] | None = None,
        actor: str = "system:delivery-plan",
    ) -> InstallationPlan:
        package = self._repository.get_package(package_record_id)
        if package is None:
            raise DeliveryError("PACKAGE_NOT_FOUND", "Validated package was not found")
        base_version = self._repository.site_configuration_version()
        parameter_contracts = validate_parameter_contracts(
            package.manifest.get("parameters")
        )
        current_configuration = (
            self._repository.get_site_configuration_version(base_version)
            if base_version > 0
            else None
        )
        previous_package: PackageImport | None = None
        previous_parameter_contracts: tuple[dict[str, Any], ...] = ()
        parameter_values, secret_values, parameter_sources, blockers = resolve_site_parameters(
            parameter_contracts,
            parameters or {},
            secret_references or {},
            current_parameters=(
                current_configuration.parameters if current_configuration else None
            ),
            current_secret_references=(
                current_configuration.secret_references
                if current_configuration
                else None
            ),
            current_metadata=(
                current_configuration.parameter_metadata
                if current_configuration
                else None
            ),
        )
        if current_configuration is not None:
            current_package = self._repository.get_package(
                current_configuration.package_record_id
            )
            if current_package is None:
                raise DeliveryError(
                    "PACKAGE_NOT_FOUND",
                    "Current site configuration package was not found",
                )
            if current_package.package_id != package.package_id:
                current_package = None
            previous_package = current_package
        if previous_package is not None:
            previous_parameter_contracts = validate_parameter_contracts(
                previous_package.manifest.get("parameters")
            )
            blockers = (
                *blockers,
                *upgrade_parameter_conflicts(
                    previous_parameter_contracts,
                    parameter_contracts,
                    current_parameters=current_configuration.parameters,
                    current_metadata=current_configuration.parameter_metadata,
                    submitted=parameters or {},
                    secret_references=secret_references or {},
                ),
                *_secret_reference_removal_blockers(
                    previous_parameter_contracts,
                    parameter_contracts,
                    current_configuration.secret_references,
                ),
            )
        parameter_configuration_content = {
            "package_digest": package.digest,
            "parameters": parameter_values,
            "secret_references": secret_values,
        }
        # Before typed parameters existed, package digest was the complete site
        # configuration identity. Preserve that identity for empty contracts so
        # migration_023 can backfill existing installations without guessing.
        parameter_configuration_digest = (
            package.digest
            if not parameter_values and not secret_values
            else hashlib.sha256(
                json.dumps(
                    parameter_configuration_content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        parameter_items = _parameter_plan_items(
            parameter_contracts,
            parameter_values,
            secret_values,
            parameter_sources,
            blockers,
            current_configuration,
        )
        if previous_package is not None:
            blockers = (
                *blockers,
                *_entity_semantic_upgrade_blockers(previous_package, package),
                *_alarm_recovery_upgrade_blockers(previous_package, package),
            )
        upgrade_risk_resolutions = upgrade_risk_resolutions or {}
        upgrade_risks, blockers = _review_upgrade_risks(
            blockers,
            upgrade_risk_resolutions,
        )
        prospective_entity_identity = uuid5(
            NAMESPACE_URL,
            (
                f"zizu/entity-installation-identity/{package.id}/{base_version}/"
                f"{parameter_configuration_digest}"
            ),
        )
        entity_identity_installation_id = (
            current_configuration.entity_identity_installation_id
            if current_configuration is not None
            else prospective_entity_identity
        )
        entity_plan = None
        entity_items: tuple[dict[str, Any], ...] = ()
        entity_blockers: tuple[dict[str, str], ...] = ()
        alarm_plan: AlarmDefinitionPlan | None = None
        alarm_items: tuple[dict[str, Any], ...] = ()
        alarm_assets = tuple(package.manifest.get("_alarm_assets", ()))
        policy_assets = tuple(package.manifest.get("_policy_assets", ()))
        asset_actions = _manifest_asset_actions(previous_package, package)
        acceptance_items = _manifest_asset_items(
            package,
            asset_actions,
            "acceptance",
        )
        policy_items = tuple(
            {
                "asset_id": item["id"],
                "kind": "ems_policy",
                "action": asset_actions[("ems_policy", item["id"])],
                "revision": item["revision"],
            }
            for item in policy_assets
        )
        if package.manifest.get("_entity_slots"):
            if self._entity_instance_registry is None:
                raise DeliveryError(
                    "ENTITY_REGISTRY_UNAVAILABLE",
                    "Entity instance registry is not configured",
                )
            entity_slots = _resolve_entity_slots(
                tuple(package.manifest["_entity_slots"]),
                parameter_values,
            )
            # Each installation plan owns one deterministic prospective
            # installation identity. Saved-plan deduplication preserves it.
            try:
                entity_plan = self._entity_instance_registry.plan(
                    PlanEntityInstances(
                        package_digest=package.digest,
                        site_configuration_version=base_version,
                        installation_id=entity_identity_installation_id,
                        slots=entity_slots,
                        selections=binding_selections or {},
                        actor=actor,
                    )
                )
            except EntityInstanceError as exc:
                raise DeliveryError(exc.code, str(exc)) from exc
            entity_items = tuple(
                {
                    "asset_id": (
                        f"{item['slot_id']}/{item['instance_key']}/"
                        f"{item['definition_id']}"
                    ),
                    "kind": "entity_binding",
                    "action": item["action"],
                    **item,
                    "entity_plan_id": str(entity_plan.id),
                    "entity_plan_digest": entity_plan.digest,
                }
                for item in entity_plan.items
            )
            entity_blockers = entity_plan.blockers
            blockers = (*blockers, *entity_blockers)
        if alarm_assets:
            if entity_plan is None:
                raise DeliveryError(
                    "ALARM_ENTITY_PLAN_REQUIRED",
                    "Alarm definitions require resolved entity instance slots",
                )
        configuration_content = {
            **parameter_configuration_content,
            "entity_bindings": [
                {
                    "entity_instance_id": item["entity_instance_id"],
                    "selected_tag_id": item["selected_tag_id"],
                    "failover_policy": item.get("failover_policy"),
                    "standby_tag_id": item.get("standby_tag_id"),
                }
                for item in entity_items
            ],
            # Use the validated package declaration here. Definition IDs are
            # derived only after this content determines the installation ID.
            "alarm_definitions": list(alarm_assets) if alarm_assets else None,
            "ems_policies": list(policy_assets) if policy_assets else None,
        }
        configuration_digest = (
            package.digest
            if not parameter_values
            and not secret_values
            and not entity_items
            and not alarm_assets
            and not policy_assets
            else hashlib.sha256(
                json.dumps(
                    configuration_content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        target_installation_id = uuid5(
            NAMESPACE_URL,
            (
                f"zizu/solution-installation/{package.id}/{base_version}/"
                f"{configuration_digest}"
            ),
        )
        if alarm_assets:
            # A definition belongs to this immutable installation, not to the
            # stable entity-identity installation used for binding resolution.
            alarm_plan = _alarm_definition_plan(
                package_digest=package.digest,
                target_installation_id=target_installation_id,
                site_configuration_version=base_version + 1,
                entity_plan=entity_plan.public_dict(),
                assets=alarm_assets,
            )
            alarm_items = tuple(
                {
                    "asset_id": definition.asset_id,
                    "kind": "alarm_definition",
                    "action": asset_actions[("alarm_definition", definition.asset_id)],
                    "entity_instance_id": str(definition.entity_instance_id),
                    "entity_definition_id": definition.entity_definition_id,
                    "severity": definition.severity,
                    "definition_version": definition.version,
                }
                for definition in alarm_plan.definitions
            )
        upgrade_safety_items = _upgrade_blocker_plan_items(
            upgrade_risks,
            upgrade_risk_resolutions,
            actor,
        )
        action = (
            "preserve"
            if current_configuration is not None
            and current_configuration.package_record_id == package.id
            and current_configuration.package_digest == package.digest
            and current_configuration.digest == configuration_digest
            else "update"
            if base_version > 0
            else "add"
        )
        items = (
            {
                "asset_id": package.package_id,
                "kind": "solution_package",
                "action": action,
            },
            *acceptance_items,
            *parameter_items,
            *entity_items,
            *alarm_items,
            *policy_items,
            *_removed_manifest_asset_items(asset_actions),
            *upgrade_safety_items,
        )
        plan_content = {
            "package_record_id": str(package.id),
            "package_digest": package.digest,
            "base_site_configuration_version": base_version,
            "items": items,
            "blockers": blockers,
            "upgrade_risk_resolutions": upgrade_risk_resolutions,
            "parameter_contracts": parameter_contracts,
            "parameters": parameter_values,
            "secret_references": secret_values,
            "parameter_sources": parameter_sources,
            "parameter_metadata": (
                current_configuration.parameter_metadata
                if current_configuration
                else {}
            ),
            "configuration_digest": configuration_digest,
            "target_installation_id": str(target_installation_id),
            "entity_identity_installation_id": str(
                entity_identity_installation_id
            ),
            "entity_plan": entity_plan.public_dict() if entity_plan else None,
            "alarm_plan": alarm_plan.public_dict() if alarm_plan else None,
        }
        digest = hashlib.sha256(
            json.dumps(
                plan_content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return self._repository.save_plan(
            InstallationPlan(
                id=uuid4(),
                package_record_id=package.id,
                package_digest=package.digest,
                base_site_configuration_version=base_version,
                status="blocked" if blockers else "ready",
                items=items,
                blockers=blockers,
                parameter_contracts=parameter_contracts,
                parameters=parameter_values,
                secret_references=secret_values,
                parameter_sources=parameter_sources,
                parameter_metadata=(
                    current_configuration.parameter_metadata
                    if current_configuration
                    else {}
                ),
                configuration_digest=configuration_digest,
                target_installation_id=target_installation_id,
                entity_identity_installation_id=entity_identity_installation_id,
                entity_plan=entity_plan.public_dict() if entity_plan else None,
                digest=digest,
                alarm_plan=alarm_plan.public_dict() if alarm_plan else None,
            )
        )


    def get_install_plan(self, plan_id: UUID) -> InstallationPlan:
        plan = self._repository.get_plan(plan_id)
        if plan is None:
            raise DeliveryError("INSTALL_PLAN_NOT_FOUND", "Installation plan was not found")
        return plan

    def apply_install(
        self,
        plan_id: UUID,
        plan_digest: str,
        idempotency_key: str,
        actor: str,
    ) -> InstallationOutcome:
        if not idempotency_key.strip():
            raise DeliveryError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Idempotency-Key is required",
            )
        request_digest = hashlib.sha256(
            json.dumps(
                {"plan_id": str(plan_id), "plan_digest": plan_digest},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        repeated = self._repository.get_idempotent_installation(
            actor,
            idempotency_key,
            request_digest,
        )
        if repeated is not None:
            return repeated

        plan = self._repository.get_plan(plan_id)
        if plan is None:
            raise DeliveryError("INSTALL_PLAN_NOT_FOUND", "Installation plan was not found")
        if plan.digest != plan_digest:
            raise DeliveryError(
                "INSTALL_PLAN_DIGEST_MISMATCH",
                "Submitted plan digest does not match the saved plan",
            )
        if plan.blockers:
            raise DeliveryError("INSTALL_PLAN_BLOCKED", "Installation plan has blockers")
        package = self._repository.get_package(plan.package_record_id)
        if package is None:
            raise DeliveryError("PACKAGE_NOT_FOUND", "Validated package was not found")
        if package.digest != plan.package_digest:
            raise DeliveryError(
                "INSTALL_PLAN_STALE",
                "Package digest changed after the plan was created",
            )
        entity_plan = plan.entity_plan
        alarm_plan = plan.alarm_plan

        def apply_entities(transaction: Any | None) -> tuple[UUID, ...]:
            if entity_plan is None:
                return ()
            if self._entity_instance_registry is None:
                raise DeliveryError(
                    "ENTITY_REGISTRY_UNAVAILABLE",
                    "Entity instance registry is not configured",
                )
            try:
                entity_outcome = self._entity_instance_registry.apply(
                    ApplyEntityInstancePlan(
                        UUID(entity_plan["id"]),
                        entity_plan["digest"],
                        actor,
                    ),
                    transaction=transaction,
                )
            except EntityInstanceError as exc:
                raise DeliveryError(exc.code, str(exc)) from exc
            return entity_outcome.entity_instance_ids

        def apply_configuration(transaction: Any | None) -> tuple[UUID, ...]:
            entity_ids = apply_entities(transaction)
            if alarm_plan is not None:
                if self._alarm_definitions is None:
                    raise DeliveryError(
                        "ALARM_RUNTIME_UNAVAILABLE",
                        "Alarm definition runtime is not configured",
                    )
                self._alarm_definitions.install_definitions(
                    _alarm_plan_from_dict(alarm_plan),
                    transaction,
                )
            return entity_ids

        return self._repository.install(
            plan,
            actor,
            idempotency_key,
            request_digest,
            apply_configuration if entity_plan is not None else None,
        )

    def list_installations(self) -> list[InstallationOutcome]:
        return self._repository.list_installations()

    def get_site_configuration_version(self, version: int) -> SiteConfigurationVersion:
        configuration = self._repository.get_site_configuration_version(version)
        if configuration is None:
            raise DeliveryError(
                "SITE_CONFIGURATION_VERSION_NOT_FOUND",
                "Site configuration version was not found",
            )
        return configuration

    async def run_acceptance(
        self,
        installation_id: UUID,
        idempotency_key: str,
        actor: str,
        manual_commands: dict[str, str] | None = None,
        policy_commands: dict[str, str] | None = None,
        authorization_denials: dict[str, str] | None = None,
        authorization_evidence_runtime: AuthorizationEvidenceRuntime | None = None,
    ) -> DeliveryReport:
        if not idempotency_key.strip():
            raise DeliveryError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Idempotency-Key is required",
            )
        installation = self._repository.get_installation(installation_id)
        if installation is None:
            raise DeliveryError(
                "INSTALLATION_NOT_FOUND",
                "Solution installation was not found",
            )
        package = self._repository.package_for_installation(installation)
        if package is None:
            raise DeliveryError("PACKAGE_NOT_FOUND", "Installed package was not found")
        acceptance_digest = _acceptance_digest(package)
        manual_commands = dict(manual_commands or {})
        policy_commands = dict(policy_commands or {})
        authorization_denials = dict(authorization_denials or {})
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "installation_id": str(installation_id),
                    "acceptance_digest": acceptance_digest,
                    "manual_commands": manual_commands,
                    "policy_commands": policy_commands,
                    "authorization_denials": authorization_denials,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        repeated = self._repository.get_idempotent_report(
            actor,
            idempotency_key,
            request_digest,
        )
        if repeated is not None:
            return repeated
        items: list[dict[str, Any]] = []
        started_at = datetime.now(timezone.utc)
        run_started = time.monotonic()
        for acceptance_id in package.acceptance_ids:
            asset = next(
                item
                for item in package.manifest["assets"]
                if item["id"] == acceptance_id
            )
            definition = _load_mapping(
                package.assets[asset["path"]],
                "ASSET_REFERENCE_INVALID",
            )
            if (
                definition.get("schemaVersion") != "zizu.acceptance/v1alpha1"
                or definition.get("id") != acceptance_id
                or definition.get("kind") not in {
                    "platform_liveness",
                    "entity_readiness",
                    "history_readiness",
                    "operation_audit",
                    "manual_control_execution",
                    "authorization_rejection",
                    "gateway_readiness",
                    "alarm_lifecycle",
                    "policy_execution",
                    "release_lock",
                }
            ):
                raise DeliveryError(
                    "ASSET_REFERENCE_INVALID",
                    "Unsupported acceptance definition",
                )
            timeout_seconds = _parse_timeout(definition.get("timeout"))
            item_started = time.monotonic()
            if definition["kind"] == "entity_readiness":
                item = self._run_entity_readiness(
                    installation,
                    acceptance_id,
                    definition,
                    item_started,
                )
                items.append(item)
                continue
            if definition["kind"] == "history_readiness":
                items.append(
                    self._run_history_readiness(
                        installation,
                        acceptance_id,
                        definition,
                        item_started,
                    )
                )
                continue
            if definition["kind"] == "operation_audit":
                items.append(
                    self._run_operation_audit(
                        installation,
                        acceptance_id,
                        bool(definition.get("required", True)),
                        item_started,
                    )
                )
                continue
            if definition["kind"] == "gateway_readiness":
                items.append(
                    await self._run_gateway_readiness_acceptance(
                        acceptance_id,
                        definition,
                        item_started,
                    )
                )
                continue
            if definition["kind"] == "manual_control_execution":
                items.append(
                    self._run_manual_control_execution_acceptance(
                        installation,
                        acceptance_id,
                        definition,
                        item_started,
                        manual_commands.get(acceptance_id),
                    )
                )
                continue
            if definition["kind"] == "authorization_rejection":
                items.append(
                    self._run_authorization_rejection_acceptance(
                        acceptance_id,
                        definition,
                        item_started,
                        authorization_denials.get(acceptance_id),
                        authorization_evidence_runtime or self._authorization_evidence_runtime,
                    )
                )
                continue
            if definition["kind"] == "alarm_lifecycle":
                item = self._run_alarm_lifecycle_acceptance(
                    installation,
                    acceptance_id,
                    definition,
                    item_started,
                )
                items.append(item)
                continue
            if definition["kind"] == "policy_execution":
                item = self._run_policy_execution_acceptance(
                    installation,
                    acceptance_id,
                    definition,
                    item_started,
                    policy_commands.get(acceptance_id),
                )
                items.append(item)
                continue
            if definition["kind"] == "release_lock":
                items.append(
                    self._run_release_lock_acceptance(
                        installation,
                        package,
                        acceptance_id,
                        bool(definition.get("required", True)),
                        item_started,
                    )
                )
                continue
            if self._public_api_probe is None:
                raise DeliveryError(
                    "LIVENESS_HTTP_ERROR",
                    "Public API probe is not configured",
                )
            try:
                response_status, evidence = await self._public_api_probe.get(
                    "/api/v1/health/live",
                    timeout_seconds,
                )
            except (TimeoutError, httpx.TimeoutException):
                item_status = "failed"
                code = "LIVENESS_TIMEOUT"
                evidence = {"error": "timeout"}
            except httpx.RequestError:
                item_status = "failed"
                code = "LIVENESS_HTTP_ERROR"
                evidence = {"error": "request_failed"}
            else:
                valid = (
                    200 <= response_status < 300
                    and evidence.get("status") == "alive"
                    and evidence.get("version") == self._platform_version
                )
                item_status = "passed" if valid else "failed"
                code = "PLATFORM_LIVE" if valid else (
                    "LIVENESS_HTTP_ERROR"
                    if not 200 <= response_status < 300
                    else "LIVENESS_RESPONSE_INVALID"
                )
                if valid:
                    evidence = {
                        "status": evidence["status"],
                        "version": evidence["version"],
                    }
                elif not 200 <= response_status < 300:
                    evidence = {"http_status": response_status}
                else:
                    evidence = {
                        "status": evidence.get("status"),
                        "version": evidence.get("version"),
                    }
            item_duration_ms = max(0, round((time.monotonic() - item_started) * 1000))
            items.append(
                {
                    "acceptance_id": acceptance_id,
                    "status": item_status,
                    "code": code,
                    "required": bool(definition.get("required", True)),
                    "duration_ms": item_duration_ms,
                    "evidence": evidence,
                }
            )

        overall = "failed" if any(
            item["required"] and item["status"] != "passed" for item in items
        ) else "passed"
        finished_at = datetime.now(timezone.utc)
        duration_ms = max(0, round((time.monotonic() - run_started) * 1000))
        report_content = {
            "installation_id": str(installation.id),
            "platform_version": self._platform_version,
            "package_id": package.package_id,
            "package_version": package.version,
            "package_digest": package.digest,
            "site_configuration_version": installation.site_configuration_version,
            "actor": actor,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": duration_ms,
            "status": overall,
            "items": items,
        }
        report = DeliveryReport(
            id=uuid4(),
            installation_id=installation.id,
            platform_version=self._platform_version,
            package_id=package.package_id,
            package_version=package.version,
            package_digest=package.digest,
            site_configuration_version=installation.site_configuration_version,
            actor=actor,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_ms=duration_ms,
            status=overall,
            items=tuple(items),
            digest=hashlib.sha256(
                json.dumps(
                    report_content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
        return self._repository.save_report(
            report,
            actor,
            idempotency_key,
            request_digest,
        )

    def _run_release_lock_acceptance(
        self,
        installation: InstallationOutcome,
        package: PackageImport,
        acceptance_id: str,
        required: bool,
        item_started: float,
    ) -> dict[str, Any]:
        """Require a deployment-owned lock for this exact installed configuration."""
        summary = (
            self._release_lock_reader()
            if self._release_lock_reader is not None
            else {"status": "unavailable"}
        )
        status = summary.get("status") if isinstance(summary, dict) else "unavailable"
        if status == "missing":
            code = "RELEASE_LOCK_MISSING"
        elif status != "locked":
            code = "RELEASE_LOCK_UNAVAILABLE"
        else:
            locked_package = summary.get("package")
            consistent = (
                summary.get("platform_version") == self._platform_version
                and summary.get("site_configuration_version")
                == installation.site_configuration_version
                and isinstance(locked_package, dict)
                and locked_package.get("id") == package.package_id
                and locked_package.get("version") == package.version
                and locked_package.get("digest") == package.digest
                and summary.get("architecture") in {"linux/amd64", "linux/arm64"}
            )
            code = "RELEASE_LOCK_CONSISTENT" if consistent else "RELEASE_LOCK_INCONSISTENT"
        evidence = {
            "status": status,
            "id": summary.get("id") if isinstance(summary, dict) else None,
            "platform_version": summary.get("platform_version") if isinstance(summary, dict) else None,
            "architecture": summary.get("architecture") if isinstance(summary, dict) else None,
            "site_configuration_version": (
                summary.get("site_configuration_version") if isinstance(summary, dict) else None
            ),
            "package": summary.get("package") if isinstance(summary, dict) else None,
        }
        return {
            "acceptance_id": acceptance_id,
            "status": "passed" if code == "RELEASE_LOCK_CONSISTENT" else "failed",
            "code": code,
            "required": required,
            "duration_ms": max(0, round((time.monotonic() - item_started) * 1000)),
            "evidence": evidence,
        }

    async def _run_gateway_readiness_acceptance(
        self,
        acceptance_id: str,
        definition: dict[str, Any],
        item_started: float,
    ) -> dict[str, Any]:
        required = bool(definition.get("required", True))
        if self._gateway_readiness is None:
            return _acceptance_result(
                acceptance_id, required, item_started, "failed", "GATEWAY_RUNTIME_UNAVAILABLE", {}
            )
        result = await self._gateway_readiness.check()
        valid = result.gateway == definition["gateway"] and result.status == "connected"
        return _acceptance_result(
            acceptance_id,
            required,
            item_started,
            "passed" if valid else "failed",
            result.code if valid else "GATEWAY_UNAVAILABLE",
            result.public_dict(),
        )

    def _run_manual_control_execution_acceptance(
        self,
        installation: InstallationOutcome,
        acceptance_id: str,
        definition: dict[str, Any],
        item_started: float,
        command_id: str | None,
    ) -> dict[str, Any]:
        """Verify a prior operator command without issuing or repeating a device write."""
        required = bool(definition.get("required", True))
        if self._control_command_runtime is None:
            return _control_acceptance_result(
                acceptance_id, required, item_started, "failed", "CONTROL_RUNTIME_UNAVAILABLE", {}
            )
        if not isinstance(command_id, str) or not command_id:
            return _control_acceptance_result(
                acceptance_id,
                required,
                item_started,
                "failed",
                "MANUAL_CONTROL_COMMAND_REQUIRED",
                {"entity_definition": definition["entityDefinition"]},
            )
        try:
            command = self._control_command_runtime.get(UUID(command_id))
        except (KeyError, ValueError):
            return _control_acceptance_result(
                acceptance_id,
                required,
                item_started,
                "failed",
                "MANUAL_CONTROL_COMMAND_NOT_FOUND",
                {"entity_definition": definition["entityDefinition"]},
            )
        plan = self._repository.get_plan(installation.plan_id)
        entity_plan = plan.entity_plan if plan is not None else None
        expected_instances = {
            UUID(item["entity_instance_id"])
            for item in entity_plan.get("items", [])
            if item.get("code") == "ENTITY_BINDING_READY"
            and item.get("definition_id") == definition["entityDefinition"]
            and item.get("entity_instance_id")
        } if isinstance(entity_plan, dict) else set()
        evidence = getattr(command, "origin_evidence", {})
        actor_role = evidence.get("actor_role") if isinstance(evidence, dict) else None
        expected_value = definition["expectedValue"]
        value_matches = (
            isinstance(command.expected_value, (int, float))
            and not isinstance(command.expected_value, bool)
            and float(command.expected_value) == float(expected_value)
        )
        valid = (
            command.source_type == "manual"
            and command.status == "readback_confirmed"
            and command.entity_instance_id in expected_instances
            and value_matches
            and actor_role == definition["actorRole"]
            and command.audit_event_id is not None
        )
        code = "MANUAL_CONTROL_EXECUTION_CONFIRMED" if valid else "MANUAL_CONTROL_EXECUTION_INCOMPLETE"
        return _control_acceptance_result(
            acceptance_id,
            required,
            item_started,
            "passed" if valid else "failed",
            code,
            {
                "command_id": str(command.id),
                "actor": command.actor,
                "actor_role": actor_role,
                "entity_definition": definition["entityDefinition"],
                "expected_value": command.expected_value,
                "status": command.status,
                "audit_event_id": str(command.audit_event_id) if command.audit_event_id else None,
            },
        )

    def _run_authorization_rejection_acceptance(
        self,
        acceptance_id: str,
        definition: dict[str, Any],
        item_started: float,
        audit_event_id: str | None,
        authorization_evidence_runtime: AuthorizationEvidenceRuntime | None,
    ) -> dict[str, Any]:
        required = bool(definition.get("required", True))
        if authorization_evidence_runtime is None:
            return _acceptance_result(
                acceptance_id,
                required,
                item_started,
                "failed",
                "AUTHORIZATION_EVIDENCE_RUNTIME_UNAVAILABLE",
                {},
            )
        if not audit_event_id:
            return _acceptance_result(
                acceptance_id,
                required,
                item_started,
                "failed",
                "AUTHORIZATION_DENIAL_EVIDENCE_REQUIRED",
                {},
            )
        try:
            evidence = authorization_evidence_runtime.find_audit_event(
                UUID(str(audit_event_id))
            )
        except (ValueError, TypeError):
            evidence = None
        if evidence is None:
            return _acceptance_result(
                acceptance_id,
                required,
                item_started,
                "failed",
                "AUTHORIZATION_EVIDENCE_NOT_FOUND",
                {},
            )
        role = (getattr(evidence, "details", None) or {}).get("role")
        matches = (
            getattr(evidence, "event", None) == "authorization.decision"
            and getattr(evidence, "outcome", None) == "denied"
            and getattr(evidence, "reason", None) == "permission_denied"
            and getattr(evidence, "target", None) == definition["capability"]
            and role == definition["actorRole"]
        )
        return _acceptance_result(
            acceptance_id,
            required,
            item_started,
            "passed" if matches else "failed",
            "AUTHORIZATION_REJECTION_CONFIRMED" if matches else "AUTHORIZATION_EVIDENCE_MISMATCH",
            {
                "audit_event_id": str(getattr(evidence, "id", audit_event_id)),
                "capability": definition["capability"],
                "actor_role": definition["actorRole"],
            },
        )

    def _run_policy_execution_acceptance(
        self,
        installation: InstallationOutcome,
        acceptance_id: str,
        definition: dict[str, Any],
        item_started: float,
        command_id: str | None,
    ) -> dict[str, Any]:
        required = bool(definition.get("required", True))
        if self._policy_runtime is None:
            return _policy_acceptance_result(
                acceptance_id, required, item_started, "failed", "POLICY_RUNTIME_UNAVAILABLE", {}
            )
        if not isinstance(command_id, str) or not command_id:
            return _policy_acceptance_result(
                acceptance_id,
                required,
                item_started,
                "failed",
                "POLICY_EXECUTION_COMMAND_REQUIRED",
                {"policy": definition["policy"]},
            )
        try:
            evidence = self._policy_runtime.acceptance_evidence(
                definition["policy"],
                definition["expectedAction"],
                command_id,
                str(installation.id),
            )
        except DeliveryError as exc:
            return _policy_acceptance_result(
                acceptance_id, required, item_started, "failed", exc.code, {"policy": definition["policy"]}
            )
        return _policy_acceptance_result(
            acceptance_id,
            required,
            item_started,
            "passed",
            "POLICY_EXECUTION_CONFIRMED",
            evidence,
        )

    def _run_alarm_lifecycle_acceptance(
        self,
        installation: InstallationOutcome,
        acceptance_id: str,
        definition: dict[str, Any],
        item_started: float,
    ) -> dict[str, Any]:
        """Prove an installed alarm's lifecycle without exposing a physical source."""
        required = bool(definition.get("required", True))
        if self._alarm_runtime is None:
            return _alarm_acceptance_result(
                acceptance_id,
                required,
                item_started,
                "failed",
                "ALARM_RUNTIME_UNAVAILABLE",
                {"alarm_definition": definition["alarmDefinition"]},
            )
        plan = self._repository.get_plan(installation.plan_id)
        alarm_plan = plan.alarm_plan if plan is not None else None
        definitions = (
            tuple(
                item
                for item in alarm_plan.get("definitions", [])
                if item.get("asset_id") == definition["alarmDefinition"]
            )
            if isinstance(alarm_plan, dict)
            else ()
        )
        if not definitions:
            return _alarm_acceptance_result(
                acceptance_id,
                required,
                item_started,
                "failed",
                "ALARM_DEFINITION_NOT_INSTALLED",
                {"alarm_definition": definition["alarmDefinition"]},
            )
        expected_state = definition["expectedState"]
        required_transition_codes = {
            "ALARM_ACTIVATED",
            "ALARM_ACKNOWLEDGED",
            "ALARM_RECOVERED",
        }
        events_by_definition = {
            str(event.definition_id): event
            for event in self._alarm_runtime.list()
        }
        evidence: list[dict[str, Any]] = []
        passed = True
        for installed_definition in definitions:
            event = events_by_definition.get(installed_definition["id"])
            if event is None:
                passed = False
                evidence.append(
                    {
                        "definition_id": installed_definition["id"],
                        "event": "missing",
                    }
                )
                continue
            timeline = self._alarm_runtime.timeline(event.id)
            transition_codes = {item.code for item in timeline}
            evidence.append(
                {
                    "definition_id": installed_definition["id"],
                    "event_id": str(event.id),
                    "state": event.state,
                    "transitions": [
                        {"to_state": item.to_state, "code": item.code}
                        for item in timeline
                    ],
                    "missing_transition_codes": sorted(
                        required_transition_codes - transition_codes
                    ),
                }
            )
            if (
                event.state != expected_state
                or not required_transition_codes.issubset(transition_codes)
            ):
                passed = False
        return _alarm_acceptance_result(
            acceptance_id,
            required,
            item_started,
            "passed" if passed else "failed",
            "ALARM_LIFECYCLE_CONFIRMED" if passed else "ALARM_LIFECYCLE_INCOMPLETE",
            {"events": evidence},
        )

    def _run_entity_readiness(
        self,
        installation: InstallationOutcome,
        acceptance_id: str,
        definition: dict[str, Any],
        item_started: float,
    ) -> dict[str, Any]:
        if self._entity_instance_runtime is None:
            return {
                "acceptance_id": acceptance_id,
                "status": "failed",
                "code": "ENTITY_RUNTIME_UNAVAILABLE",
                "required": bool(definition.get("required", True)),
                "duration_ms": max(
                    0,
                    round((time.monotonic() - item_started) * 1000),
                ),
                "evidence": {"binding": "unavailable"},
            }
        plan = self._repository.get_plan(installation.plan_id)
        entity_item = next(
            (
                item
                for item in plan.items
                if item.get("kind") == "entity_binding"
                and item.get("slot_id") == definition.get("slot")
                and item.get("definition_id") == definition.get("definition")
            ),
            None,
        ) if plan is not None else None
        if entity_item is None:
            code = "ENTITY_BINDING_MISSING"
            status_text = "failed"
            evidence: dict[str, Any] = {
                "binding": "missing",
                "primary_source_count": 0,
            }
        else:
            entity_instance_id = UUID(entity_item["entity_instance_id"])
            try:
                observation = self._entity_instance_runtime.read(entity_instance_id)
            except EntityInstanceError as exc:
                code = exc.code
                status_text = "failed"
                binding_confirmed = exc.code in {
                    "ENTITY_DATA_MISSING",
                    "ENTITY_DATA_STALE",
                    "ENTITY_DATA_QUALITY_BAD",
                }
                evidence = {
                    "entity_instance_id": str(entity_instance_id),
                    "binding": "confirmed" if binding_confirmed else "unavailable",
                    "primary_source_count": 1 if binding_confirmed else 0,
                }
            else:
                freshness_seconds = _parse_timeout(definition.get("freshness"))
                within_acceptance_freshness = (
                    observation.age_ms <= freshness_seconds * 1000
                )
                status_text = (
                    "passed"
                    if within_acceptance_freshness and observation.quality_good
                    else "failed"
                )
                code = (
                    "ENTITY_DATA_STALE"
                    if not within_acceptance_freshness
                    else "ENTITY_DATA_QUALITY_BAD"
                    if not observation.quality_good
                    else "ENTITY_BINDING_FRESH"
                )
                evidence = {
                    "entity_instance_id": str(entity_instance_id),
                    "definition_id": observation.definition_id,
                    "binding": "confirmed",
                    "primary_source_count": 1,
                    "observed_at": observation.observed_at.isoformat(),
                    "age_ms": observation.age_ms,
                    "quality": observation.quality,
                    "quality_good": observation.quality_good,
                }
        return {
            "acceptance_id": acceptance_id,
            "status": status_text,
            "code": code,
            "required": bool(definition.get("required", True)),
            "duration_ms": max(
                0,
                round((time.monotonic() - item_started) * 1000),
            ),
            "evidence": evidence,
        }

    def _run_history_readiness(
        self,
        installation: InstallationOutcome,
        acceptance_id: str,
        definition: dict[str, Any],
        item_started: float,
    ) -> dict[str, Any]:
        required = bool(definition.get("required", True))
        if self._entity_instance_runtime is None:
            return {
                "acceptance_id": acceptance_id,
                "status": "failed",
                "code": "ENTITY_RUNTIME_UNAVAILABLE",
                "required": required,
                "duration_ms": max(0, round((time.monotonic() - item_started) * 1000)),
                "evidence": {"history": "unavailable"},
            }
        plan = self._repository.get_plan(installation.plan_id)
        entity_item = next(
            (
                item
                for item in plan.items
                if item.get("kind") == "entity_binding"
                and item.get("slot_id") == definition.get("slot")
                and item.get("definition_id") == definition.get("definition")
            ),
            None,
        ) if plan is not None else None
        if entity_item is None:
            return {
                "acceptance_id": acceptance_id,
                "status": "failed",
                "code": "ENTITY_BINDING_MISSING",
                "required": required,
                "duration_ms": max(0, round((time.monotonic() - item_started) * 1000)),
                "evidence": {"binding": "missing", "sample_count": 0},
            }
        entity_instance_id = UUID(entity_item["entity_instance_id"])
        try:
            observations = self._entity_instance_runtime.history(
                entity_instance_id, definition["range"]
            )
        except EntityInstanceError as exc:
            return {
                "acceptance_id": acceptance_id,
                "status": "failed",
                "code": exc.code,
                "required": required,
                "duration_ms": max(0, round((time.monotonic() - item_started) * 1000)),
                "evidence": {"entity_instance_id": str(entity_instance_id), "sample_count": 0},
            }
        good = [item for item in observations if item.quality_good]
        minimum = definition["minimumSamples"]
        if len(observations) < minimum:
            code = "HISTORY_SAMPLE_MISSING"
        elif len(good) < minimum:
            code = "HISTORY_QUALITY_BAD"
        else:
            code = "HISTORY_AVAILABLE"
        return {
            "acceptance_id": acceptance_id,
            "status": "passed" if code == "HISTORY_AVAILABLE" else "failed",
            "code": code,
            "required": required,
            "duration_ms": max(0, round((time.monotonic() - item_started) * 1000)),
            "evidence": {
                "entity_instance_id": str(entity_instance_id),
                "definition_id": definition["definition"],
                "range": definition["range"],
                "sample_count": len(observations),
                "good_sample_count": len(good),
            },
        }

    def _run_operation_audit(
        self,
        installation: InstallationOutcome,
        acceptance_id: str,
        required: bool,
        item_started: float,
    ) -> dict[str, Any]:
        """Prove the installation itself entered the immutable audit trail."""
        try:
            events = self._repository.list_installation_audit_events(installation.id)
        except Exception:
            events = []
            code = "OPERATION_AUDIT_UNAVAILABLE"
        else:
            code = (
                "OPERATION_AUDIT_AVAILABLE"
                if any(event.event == "solution.install" and event.outcome == "allowed" for event in events)
                else "OPERATION_AUDIT_MISSING"
            )
        return {
            "acceptance_id": acceptance_id,
            "status": "passed" if code == "OPERATION_AUDIT_AVAILABLE" else "failed",
            "code": code,
            "required": required,
            "duration_ms": max(0, round((time.monotonic() - item_started) * 1000)),
            "evidence": {
                "installation_id": str(installation.id),
                "event_count": len(events),
                "events": [
                    {"event": event.event, "outcome": event.outcome, "actor": event.actor}
                    for event in events
                ],
            },
        }

    def get_report(self, report_id: UUID) -> DeliveryReport:
        report = self._repository.get_report(report_id)
        if report is None:
            raise DeliveryError("REPORT_NOT_FOUND", "Delivery report was not found")
        return report

    def get_installation_audit_events(self, installation_id: UUID) -> list[dict[str, Any]]:
        """Expose the narrow, immutable audit evidence for one installation."""
        if self._repository.get_installation(installation_id) is None:
            raise DeliveryError("INSTALLATION_NOT_FOUND", "Solution installation was not found")
        return [
            event.public_dict()
            for event in self._repository.list_installation_audit_events(installation_id)
        ]


def _parameter_plan_items(
    contracts: tuple[dict[str, Any], ...],
    parameters: dict[str, Any],
    secret_references: dict[str, str],
    sources: dict[str, str],
    blockers: tuple[dict[str, str], ...],
    current: SiteConfigurationVersion | None,
) -> tuple[dict[str, Any], ...]:
    current_parameters = current.parameters if current else {}
    current_secrets = current.secret_references if current else {}
    blocked_ids = {
        blocker["parameter_id"]
        if "parameter_id" in blocker
        else blocker.get("asset_id")
        for blocker in blockers
    }
    blocked_ids.discard(None)
    conflict_ids = {
        blocker["parameter_id"]
        for blocker in blockers
        if blocker["code"] == "UPGRADE_PARAMETER_CONFLICT"
    }
    items: list[dict[str, Any]] = []
    declared_ids: set[str] = set()
    for contract in contracts:
        parameter_id = contract["id"]
        declared_ids.add(parameter_id)
        is_secret = contract["type"] == "secret"
        previous_values = current_secrets if is_secret else current_parameters
        next_values = secret_references if is_secret else parameters
        previous = previous_values.get(parameter_id)
        next_value = next_values.get(parameter_id)
        if parameter_id in conflict_ids:
            action = "conflict"
        elif parameter_id in blocked_ids:
            action = "block"
        elif parameter_id not in next_values and parameter_id in previous_values:
            action = "delete_candidate"
        elif parameter_id not in previous_values:
            action = "add"
        elif previous == next_value:
            action = "preserve"
        else:
            action = "update"
        items.append(
            {
                "asset_id": parameter_id,
                "kind": "secret_reference" if is_secret else "site_parameter",
                "action": action,
                "before": previous,
                "after": next_value,
                "source": sources.get(parameter_id),
                "unit": contract.get("unit"),
            }
        )
    for parameter_id in sorted(
        (set(current_parameters) | set(current_secrets)) - declared_ids
    ):
        is_secret = parameter_id in current_secrets
        items.append(
            {
                "asset_id": parameter_id,
                "kind": "secret_reference" if is_secret else "site_parameter",
                "action": "delete_candidate",
                "before": (
                    current_secrets if is_secret else current_parameters
                )[parameter_id],
                "after": None,
                "source": None,
                "unit": None,
            }
        )
    return tuple(items)


def _manifest_asset_actions(
    previous_package: PackageImport | None,
    next_package: PackageImport,
) -> dict[tuple[str, str], str]:
    """Classify declared assets by stable ID and content digest for one upgrade."""
    previous_assets = {
        (asset["kind"], asset["id"]): asset
        for asset in (previous_package.manifest.get("assets", ()) if previous_package else ())
    }
    next_assets = {
        (asset["kind"], asset["id"]): asset
        for asset in next_package.manifest.get("assets", ())
    }
    actions: dict[tuple[str, str], str] = {}
    for key in previous_assets.keys() | next_assets.keys():
        previous = previous_assets.get(key)
        next_asset = next_assets.get(key)
        actions[key] = (
            "add"
            if previous is None
            else "delete_candidate"
            if next_asset is None
            else "preserve"
            if previous["sha256"] == next_asset["sha256"]
            else "update"
        )
    return actions


def _manifest_asset_items(
    package: PackageImport,
    actions: dict[tuple[str, str], str],
    kind: str,
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "asset_id": asset["id"],
            "kind": kind,
            "action": actions[(kind, asset["id"])],
        }
        for asset in package.manifest.get("assets", ())
        if asset["kind"] == kind
    )


def _removed_manifest_asset_items(
    actions: dict[tuple[str, str], str],
) -> tuple[dict[str, str], ...]:
    """Keep removed configurable assets visible even when no new object exists."""
    visible_kinds = {"alarm_definition", "ems_policy"}
    return tuple(
        {"asset_id": asset_id, "kind": kind, "action": action}
        for (kind, asset_id), action in sorted(actions.items())
        if action == "delete_candidate" and kind in visible_kinds
    )


def _review_upgrade_risks(
    blockers: tuple[dict[str, str], ...],
    resolutions: dict[str, str],
) -> tuple[tuple[dict[str, str], ...], tuple[dict[str, str], ...]]:
    """Require a named engineering resolution before a safety risk can execute."""
    risks = tuple(
        blocker
        for blocker in blockers
        if blocker["code"].startswith("UPGRADE_")
        and blocker["code"] != "UPGRADE_PARAMETER_CONFLICT"
        and "asset_id" in blocker
    )
    risk_keys = {_upgrade_risk_key(risk) for risk in risks}
    unknown_keys = set(resolutions) - risk_keys
    invalid_notes = {
        key
        for key, note in resolutions.items()
        if not isinstance(note, str) or not note.strip()
    }
    if unknown_keys or invalid_notes:
        raise DeliveryError(
            "UPGRADE_RISK_RESOLUTION_INVALID",
            "Upgrade risk resolutions must target a listed risk and include a note",
        )
    return (
        risks,
        tuple(
            blocker
            for blocker in blockers
            if blocker not in risks or _upgrade_risk_key(blocker) not in resolutions
        ),
    )


def _upgrade_risk_key(blocker: dict[str, str]) -> str:
    return f"{blocker['code']}:{blocker['asset_id']}"


def _upgrade_blocker_plan_items(
    risks: tuple[dict[str, str], ...],
    resolutions: dict[str, str],
    actor: str,
) -> tuple[dict[str, str], ...]:
    """Expose upgrade safety items and their explicit engineering decisions."""
    return tuple(
        {
            "asset_id": blocker["asset_id"],
            "kind": "upgrade_safety",
            "action": (
                "update" if _upgrade_risk_key(blocker) in resolutions else "block"
            ),
            "code": blocker["code"],
            "message": blocker["message"],
            "risk_key": _upgrade_risk_key(blocker),
            "resolution": resolutions.get(_upgrade_risk_key(blocker)),
            "reviewed_by": (
                actor if _upgrade_risk_key(blocker) in resolutions else None
            ),
        }
        for blocker in risks
    )


def _entity_semantic_upgrade_blockers(
    previous_package: PackageImport,
    next_package: PackageImport,
) -> tuple[dict[str, str], ...]:
    """Block automatic changes to already-installed entity semantics."""
    previous_definitions = {
        definition["id"]: definition
        for slot in previous_package.manifest.get("_entity_slots", ())
        for definition in slot["definitions"]
    }
    next_definitions = {
        definition["id"]: definition
        for slot in next_package.manifest.get("_entity_slots", ())
        for definition in slot["definitions"]
    }
    blockers: list[dict[str, str]] = []
    for definition_id in sorted(set(previous_definitions) - set(next_definitions)):
        blockers.append(
            {
                "code": "UPGRADE_RUNNING_REFERENCE_REMOVAL",
                "asset_id": definition_id,
                "message": "Upgrade removes a running entity definition",
            }
        )
    for definition_id in sorted(set(previous_definitions) & set(next_definitions)):
        previous = previous_definitions[definition_id]
        next_definition = next_definitions[definition_id]
        if (
            previous.get("unit") != next_definition.get("unit")
            or previous["direction"] != next_definition["direction"]
        ):
            blockers.append(
                {
                    "code": "UPGRADE_ENTITY_SEMANTICS_CHANGED",
                    "asset_id": definition_id,
                    "message": "Entity unit or direction changed",
                }
            )
        if _control_permission_broadened(
            previous.get("control"),
            next_definition.get("control"),
        ):
            blockers.append(
                {
                    "code": "UPGRADE_CONTROL_POLICY_CHANGED",
                    "asset_id": definition_id,
                    "message": "Upgrade broadens control permissions and requires review",
                }
            )
    return tuple(blockers)


def _control_permission_broadened(
    previous: dict[str, Any] | None,
    next_control: dict[str, Any] | None,
) -> bool:
    """Identify changes that let an existing control act more freely.

    Tightening a limit or adding an interlock is safe to stage automatically;
    relaxing a limit, cooldown, high-risk confirmation or existing interlock is
    an engineering decision rather than a package-default update.
    """
    if previous is None:
        return next_control is not None
    if next_control is None:
        return False
    if float(next_control["minimum"]) < float(previous["minimum"]):
        return True
    if float(next_control["maximum"]) > float(previous["maximum"]):
        return True
    if float(next_control["cooldown_seconds"]) < float(previous["cooldown_seconds"]):
        return True
    if previous.get("high_risk") and not next_control.get("high_risk"):
        return True
    previous_interlocks = {
        json.dumps(item, sort_keys=True, separators=(",", ":"))
        for item in previous.get("interlocks", ())
    }
    next_interlocks = {
        json.dumps(item, sort_keys=True, separators=(",", ":"))
        for item in next_control.get("interlocks", ())
    }
    return not previous_interlocks.issubset(next_interlocks)


def _secret_reference_removal_blockers(
    previous_contracts: tuple[dict[str, Any], ...],
    next_contracts: tuple[dict[str, Any], ...],
    current_secret_references: dict[str, str],
) -> tuple[dict[str, str], ...]:
    """Protect runtime Secret references from being dropped by a package update."""
    next_secret_ids = {
        contract["id"] for contract in next_contracts if contract["type"] == "secret"
    }
    return tuple(
        {
            "code": "UPGRADE_SECRET_REFERENCE_REMOVAL",
            "asset_id": contract["id"],
            "message": "Upgrade removes an in-use Secret reference",
        }
        for contract in previous_contracts
        if contract["type"] == "secret"
        and contract["id"] in current_secret_references
        and contract["id"] not in next_secret_ids
    )


def _alarm_recovery_upgrade_blockers(
    previous_package: PackageImport,
    next_package: PackageImport,
) -> tuple[dict[str, str], ...]:
    """Block automatic changes to recovery conditions of installed alarms."""
    previous_alarms = {
        alarm["id"]: alarm
        for alarm in previous_package.manifest.get("_alarm_assets", ())
    }
    next_alarms = {
        alarm["id"]: alarm
        for alarm in next_package.manifest.get("_alarm_assets", ())
    }
    blockers: list[dict[str, str]] = []
    for alarm_id in sorted(set(previous_alarms) & set(next_alarms)):
        previous = previous_alarms[alarm_id]
        next_alarm = next_alarms[alarm_id]
        if (
            previous["recovery"] != next_alarm["recovery"]
            or previous["recovery_duration_seconds"]
            != next_alarm["recovery_duration_seconds"]
        ):
            blockers.append(
                {
                    "code": "UPGRADE_ALARM_RECOVERY_CHANGED",
                    "asset_id": alarm_id,
                    "message": "Alarm recovery behavior changed",
                }
            )
    return tuple(blockers)


def _resolve_entity_slots(
    slots: tuple[dict[str, Any], ...],
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    resolved: list[dict[str, Any]] = []
    for slot in slots:
        instances_parameter = slot.get("instances_parameter")
        if instances_parameter is not None:
            instances = parameters.get(instances_parameter)
            if not isinstance(instances, list) or not instances:
                raise DeliveryError(
                    "ENTITY_SLOT_PARAMETER_INVALID",
                    "Entity slot device instance parameter is missing",
                )
            for instance in instances:
                definitions = []
                for definition in slot["definitions"]:
                    matcher = {
                        "id": definition["matcher"]["id"],
                        "device_key": instance["device_key"],
                        "tag_name": definition["matcher"]["tag_name"],
                    }
                    if definition["matcher"].get("failover_policy") == "manual":
                        standby_device_key = instance.get("standby_device_key")
                        if not isinstance(standby_device_key, str) or not standby_device_key:
                            raise DeliveryError(
                                "ENTITY_SLOT_PARAMETER_INVALID",
                                "Manual failover requires a standby device key",
                            )
                        matcher.update(
                            failover_policy="manual",
                            standby_device_key=standby_device_key,
                        )
                    definitions.append({**definition, "matcher": matcher})
                resolved.append(
                    {
                        "id": slot["id"],
                        "device_category": slot["device_category"],
                        "instance_key": instance["instance_key"],
                        "display_name": instance.get("display_name") or (
                            f"{slot['display_name']} {instance['instance_key']}"
                        ),
                        "freshness_seconds": slot["freshness_seconds"],
                        "definitions": definitions,
                    }
                )
            continue
        instance_key = parameters.get(slot["instance_key_parameter"])
        if not isinstance(instance_key, str) or not instance_key:
            raise DeliveryError(
                "ENTITY_SLOT_PARAMETER_INVALID",
                "Entity slot instance key parameter is missing",
            )
        definitions: list[dict[str, Any]] = []
        for definition in slot["definitions"]:
            device_key = parameters.get(
                definition["matcher"]["device_key_parameter"]
            )
            if not isinstance(device_key, str) or not device_key:
                raise DeliveryError(
                    "ENTITY_SLOT_PARAMETER_INVALID",
                    "Entity matcher device key parameter is missing",
                )
            definitions.append(
                {
                    **definition,
                    "matcher": {
                        "id": definition["matcher"]["id"],
                        "device_key": device_key,
                        "tag_name": definition["matcher"]["tag_name"],
                    },
                }
            )
        resolved.append(
            {
                "id": slot["id"],
                "device_category": slot["device_category"],
                "instance_key": instance_key,
                "display_name": slot["display_name"],
                "freshness_seconds": slot["freshness_seconds"],
                "definitions": definitions,
            }
        )
    return tuple(resolved)


def _parse_timeout(value: Any) -> float:
    if not isinstance(value, str):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Acceptance timeout is invalid")
    match = re.fullmatch(r"([1-9]\d*)s", value)
    if match is None:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Acceptance timeout is invalid")
    return float(match.group(1))


def _acceptance_digest(package: PackageImport) -> str:
    """锁定验收引用、声明和内容，防止幂等键跨清单静默复用。"""
    content = []
    assets_by_id = {asset["id"]: asset for asset in package.manifest["assets"]}
    for acceptance_id in package.acceptance_ids:
        asset = assets_by_id[acceptance_id]
        asset_content = package.assets[asset["path"]]
        content.append(
            {
                "id": acceptance_id,
                "path": asset["path"],
                "sha256": hashlib.sha256(asset_content).hexdigest(),
            }
        )
    return hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _alarm_definition_plan(
    *,
    package_digest: str,
    target_installation_id: UUID,
    site_configuration_version: int,
    entity_plan: dict[str, Any],
    assets: tuple[dict[str, Any], ...],
) -> AlarmDefinitionPlan:
    by_slot_definition = {
        (item["slot_id"], item["definition_id"]): UUID(item["entity_instance_id"])
        for item in entity_plan["items"]
        if item.get("code") == "ENTITY_BINDING_READY"
    }
    definitions: list[InstalledAlarmDefinition] = []
    for asset in assets:
        entity_instance_id = by_slot_definition.get(
            (asset["slot"], asset["entity_definition"])
        )
        if entity_instance_id is None:
            raise DeliveryError(
                "ALARM_ENTITY_INSTANCE_MISSING",
                "Alarm definition does not have a confirmed entity instance",
            )
        definition_id = uuid5(
            NAMESPACE_URL,
            (
                f"zizu/alarm-definition/{package_digest}/{target_installation_id}/"
                f"{asset['id']}/{entity_instance_id}"
            ),
        )
        definitions.append(
            InstalledAlarmDefinition(
                id=definition_id,
                asset_id=asset["id"],
                version=asset["version"],
                installation_id=target_installation_id,
                site_configuration_version=site_configuration_version,
                entity_instance_id=entity_instance_id,
                entity_definition_id=asset["entity_definition"],
                trigger=dict(asset["trigger"]),
                trigger_duration_seconds=asset["trigger_duration_seconds"],
                recovery=dict(asset["recovery"]),
                recovery_duration_seconds=asset["recovery_duration_seconds"],
                severity=asset["severity"],
                notification_throttle_seconds=asset[
                    "notification_throttle_seconds"
                ],
            )
        )
    content = {
        "package_digest": package_digest,
        "installation_id": str(target_installation_id),
        "site_configuration_version": site_configuration_version,
        "definitions": [item.public_dict() for item in definitions],
    }
    return AlarmDefinitionPlan(
        installation_id=target_installation_id,
        site_configuration_version=site_configuration_version,
        package_digest=package_digest,
        definitions=tuple(definitions),
        digest=hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def _alarm_plan_from_dict(raw: dict[str, Any]) -> AlarmDefinitionPlan:
    definitions = tuple(
        InstalledAlarmDefinition(
            id=UUID(item["id"]),
            asset_id=item["asset_id"],
            version=item["version"],
            installation_id=UUID(item["installation_id"]),
            site_configuration_version=item["site_configuration_version"],
            entity_instance_id=UUID(item["entity_instance_id"]),
            entity_definition_id=item["entity_definition_id"],
            trigger=dict(item["trigger"]),
            trigger_duration_seconds=item["trigger_duration_seconds"],
            recovery=dict(item["recovery"]),
            recovery_duration_seconds=item["recovery_duration_seconds"],
            severity=item["severity"],
            notification_throttle_seconds=item["notification_throttle_seconds"],
        )
        for item in raw["definitions"]
    )
    return AlarmDefinitionPlan(
        installation_id=UUID(raw["installation_id"]),
        site_configuration_version=raw["site_configuration_version"],
        package_digest=raw["package_digest"],
        definitions=definitions,
        digest=raw["digest"],
    )


def _alarm_acceptance_result(
    acceptance_id: str,
    required: bool,
    item_started: float,
    state: str,
    code: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return _acceptance_result(acceptance_id, required, item_started, state, code, evidence)


def _policy_acceptance_result(
    acceptance_id: str,
    required: bool,
    item_started: float,
    state: str,
    code: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Format policy acceptance evidence with the same immutable report contract."""
    return _acceptance_result(acceptance_id, required, item_started, state, code, evidence)


def _control_acceptance_result(
    acceptance_id: str,
    required: bool,
    item_started: float,
    state: str,
    code: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return _acceptance_result(acceptance_id, required, item_started, state, code, evidence)


def _acceptance_result(
    acceptance_id: str,
    required: bool,
    item_started: float,
    state: str,
    code: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "acceptance_id": acceptance_id,
        "status": state,
        "code": code,
        "required": required,
        "duration_ms": max(0, round((time.monotonic() - item_started) * 1000)),
        "evidence": evidence,
    }
