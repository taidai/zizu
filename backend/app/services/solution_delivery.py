"""版本化解决方案包的交付深模块。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any
from uuid import UUID, uuid4

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
    _validate_manifest,
    _validate_platform_range,
    _version_tuple,
)
from app.services.solution_parameters import (
    resolve_site_parameters,
    validate_parameter_contracts,
)

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
    ) -> None:
        self._repository = repository
        self._platform_version = platform_version
        self._public_api_probe = public_api_probe

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
        configuration_content = {
            "package_digest": package.digest,
            "parameters": parameter_values,
            "secret_references": secret_values,
        }
        # Before typed parameters existed, package digest was the complete site
        # configuration identity. Preserve that identity for empty contracts so
        # migration_023 can backfill existing installations without guessing.
        configuration_digest = (
            package.digest
            if not parameter_values and not secret_values
            else hashlib.sha256(
                json.dumps(
                    configuration_content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
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
        parameter_items = _parameter_plan_items(
            parameter_contracts,
            parameter_values,
            secret_values,
            parameter_sources,
            blockers,
            current_configuration,
        )
        items = (
            {
                "asset_id": package.package_id,
                "kind": "solution_package",
                "action": action,
            },
            *(
                {
                    "asset_id": acceptance_id,
                    "kind": "acceptance",
                    "action": action,
                }
                for acceptance_id in package.acceptance_ids
            ),
            *parameter_items,
        )
        plan_content = {
            "package_record_id": str(package.id),
            "package_digest": package.digest,
            "base_site_configuration_version": base_version,
            "items": items,
            "blockers": blockers,
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
                digest=digest,
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
        return self._repository.install(
            plan,
            actor,
            idempotency_key,
            request_digest,
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
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "installation_id": str(installation_id),
                    "acceptance_digest": acceptance_digest,
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
        if self._public_api_probe is None:
            raise DeliveryError(
                "LIVENESS_HTTP_ERROR",
                "Public API probe is not configured",
            )

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
                or definition.get("kind") != "platform_liveness"
            ):
                raise DeliveryError(
                    "ASSET_REFERENCE_INVALID",
                    "Unsupported acceptance definition",
                )
            timeout_seconds = _parse_timeout(definition.get("timeout"))
            item_started = time.monotonic()
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

    def get_report(self, report_id: UUID) -> DeliveryReport:
        report = self._repository.get_report(report_id)
        if report is None:
            raise DeliveryError("REPORT_NOT_FOUND", "Delivery report was not found")
        return report


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
    blocked_ids = {blocker["parameter_id"] for blocker in blockers}
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
        if parameter_id in blocked_ids:
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
