"""解决方案包公开 HTTP Adapter。"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field, ValidationError

from app.api.security import get_identity, require_capability
from app.api.health import _VERSION
from app.core.config import settings
from app.services.identity import Identity, Principal
from app.services.entity_instance_postgres import (
    PostgresEntityInstanceRepository,
    PostgresObservationCatalog,
    PostgresSourceCatalog,
)
from app.services.entity_instance_registry import EntityInstanceRegistry
from app.services.entity_instance_runtime import EntityInstanceRuntime
from app.services.entity_instance_catalog import EntityInstanceCatalog
from app.services.entity_instance_failover import EntityFailoverPolicy
from app.services.control_commands import (
    ControlCommandCompatibility,
    ControlCommandRuntime,
    NeuronControlDispatcher,
    PostgresControlCommandRepository,
    PostgresControlTargetResolver,
)
from app.services.automated_control_commands import AutomatedControlCommands
from app.services.alarm_postgres import (
    PostgresAlarmDefinitionCatalog,
    PostgresAlarmRepository,
)
from app.services.alarm_runtime import AlarmRuntime
from app.services.solution_delivery import (
    DeliveryError,
    HttpxPublicApiProbe,
    MAX_PACKAGE_ARCHIVE_BYTES,
    PostgresDeliveryRepository,
    SolutionDelivery,
)
from app.services.ems_workbench import EmsWorkbench
from app.services.ems_policy_runtime import (
    EmsPolicyRuntime,
    PostgresPolicyActivationRepository,
)
from app.services.release_lock import current_release_lock_summary
from app.services.gateway_readiness import NeuronGatewayReadiness
from app.services.neuron_client import get_neuron_client


router = APIRouter()
_repository = PostgresDeliveryRepository()
_entity_instance_repository = PostgresEntityInstanceRepository()
_entity_instance_registry = EntityInstanceRegistry(
    _entity_instance_repository,
    PostgresSourceCatalog(),
    _repository.site_configuration_version,
)
_entity_instance_runtime = EntityInstanceRuntime(
    _entity_instance_registry,
    PostgresObservationCatalog(),
)
_entity_instance_catalog = EntityInstanceCatalog(_entity_instance_repository)
_entity_instance_failover = EntityFailoverPolicy(_entity_instance_repository)
_alarm_definition_catalog = PostgresAlarmDefinitionCatalog()
_alarm_runtime = AlarmRuntime(_alarm_definition_catalog, PostgresAlarmRepository())
_control_commands = ControlCommandRuntime(
    registry=_entity_instance_registry,
    policies=_entity_instance_repository,
    readback=_entity_instance_runtime,
    dispatcher=NeuronControlDispatcher(),
    repository=PostgresControlCommandRepository(),
)
_control_compatibility = ControlCommandCompatibility(
    _control_commands,
    PostgresControlTargetResolver(),
)
_automated_control_commands = AutomatedControlCommands(_control_commands)
_delivery = SolutionDelivery(
    _repository,
    platform_version=_VERSION,
    public_api_probe=HttpxPublicApiProbe(settings.effective_public_api_base_url),
    entity_instance_registry=_entity_instance_registry,
    entity_instance_runtime=_entity_instance_runtime,
    alarm_definitions=_alarm_definition_catalog,
    alarm_runtime=_alarm_runtime,
    control_command_runtime=_control_commands,
    gateway_readiness=NeuronGatewayReadiness(get_neuron_client),
    release_lock_reader=current_release_lock_summary,
)
_ems_workbench = EmsWorkbench(
    _repository,
    _entity_instance_catalog,
    _entity_instance_runtime,
)
_ems_policies = EmsPolicyRuntime(
    _repository,
    _entity_instance_catalog,
    _entity_instance_runtime,
    _automated_control_commands,
    PostgresPolicyActivationRepository(),
)
_control_commands.set_policy_high_risk_authorizer(
    _ems_policies.authorizes_high_risk_command
)
_delivery.set_policy_runtime(_ems_policies)


def get_solution_delivery() -> SolutionDelivery:
    return _delivery


def get_default_ems_workbench() -> EmsWorkbench:
    return _ems_workbench


def get_default_ems_policy_runtime() -> EmsPolicyRuntime:
    return _ems_policies


def get_default_entity_instance_runtime() -> EntityInstanceRuntime:
    return _entity_instance_runtime


def get_default_alarm_runtime() -> AlarmRuntime:
    return _alarm_runtime


def get_default_entity_instance_catalog() -> EntityInstanceCatalog:
    return _entity_instance_catalog


def get_default_entity_instance_registry() -> EntityInstanceRegistry:
    return _entity_instance_registry


def get_default_entity_instance_failover() -> EntityFailoverPolicy:
    return _entity_instance_failover


def get_default_control_commands() -> ControlCommandRuntime:
    return _control_commands


def get_default_control_compatibility() -> ControlCommandCompatibility:
    return _control_compatibility


def get_default_automated_control_commands() -> AutomatedControlCommands:
    return _automated_control_commands


class ApplyInstallationRequest(BaseModel):
    plan_digest: str = Field(..., min_length=64, max_length=64)


class CreateInstallationPlanRequest(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    secret_references: dict[str, str] = Field(default_factory=dict)
    binding_selections: dict[str, UUID] = Field(default_factory=dict)
    binding_overrides: dict[str, UUID] = Field(default_factory=dict)
    upgrade_risk_resolutions: dict[str, str] = Field(default_factory=dict)


class RunAcceptanceRequest(BaseModel):
    manual_commands: dict[str, UUID] = Field(default_factory=dict)
    policy_commands: dict[str, UUID] = Field(default_factory=dict)
    authorization_denials: dict[str, UUID] = Field(default_factory=dict)


@router.get("/solution-packages")
async def list_solution_packages(
    principal: Principal = Depends(require_capability("solution.package.read")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    packages = delivery.list_packages()
    return {
        "items": [package.public_dict() for package in packages],
        "total": len(packages),
    }


@router.post(
    "/solution-packages/{package_record_id}/install-plans",
    status_code=status.HTTP_201_CREATED,
)
async def create_installation_plan(
    package_record_id: UUID,
    request: Request,
    principal: Principal = Depends(require_capability("solution.install.plan")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    try:
        body = await request.json()
        plan_request = CreateInstallationPlanRequest.model_validate(body)
    except (ValueError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "INSTALL_PLAN_REQUEST_INVALID",
                "message": "Installation plan request is invalid",
            },
        ) from None
    try:
        return delivery.plan_install(
            package_record_id,
            parameters=plan_request.parameters,
            secret_references=plan_request.secret_references,
            binding_selections=plan_request.binding_selections,
            binding_overrides=plan_request.binding_overrides,
            upgrade_risk_resolutions=plan_request.upgrade_risk_resolutions,
            actor=principal.actor,
        ).public_dict()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_CONTENT
                if exc.code == "UPGRADE_RISK_RESOLUTION_INVALID"
                else status.HTTP_404_NOT_FOUND
            ),
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/install-plans/{plan_id}")
async def get_installation_plan(
    plan_id: UUID,
    principal: Principal = Depends(require_capability("solution.install.plan")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    del principal
    try:
        return delivery.get_install_plan(plan_id).public_dict()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.post(
    "/install-plans/{plan_id}/apply",
    status_code=status.HTTP_201_CREATED,
)
async def apply_installation_plan(
    plan_id: UUID,
    request: ApplyInstallationRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_capability("solution.install.apply")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    try:
        return delivery.apply_install(
            plan_id=plan_id,
            plan_digest=request.plan_digest,
            idempotency_key=idempotency_key or "",
            actor=principal.actor,
        ).public_dict()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/solution-installations")
async def list_solution_installations(
    principal: Principal = Depends(require_capability("solution.installation.read")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    del principal
    installations = delivery.list_installations()
    return {
        "items": [installation.public_dict() for installation in installations],
        "total": len(installations),
    }


@router.get("/solution-installations/{installation_id}/audit-events")
async def get_installation_audit_events(
    installation_id: UUID,
    principal: Principal = Depends(require_capability("solution.report.read")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    """Read the narrow, immutable audit trail for one delivery installation."""
    del principal
    try:
        events = delivery.get_installation_audit_events(installation_id)
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {"installation_id": str(installation_id), "items": events, "total": len(events)}


@router.get("/site-configuration-versions/{version}")
async def get_site_configuration_version(
    version: int,
    principal: Principal = Depends(require_capability("solution.configuration.read")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    del principal
    try:
        return delivery.get_site_configuration_version(version).public_dict()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.post(
    "/solution-installations/{installation_id}/acceptance-runs",
    status_code=status.HTTP_201_CREATED,
)
async def run_delivery_acceptance(
    installation_id: UUID,
    request: RunAcceptanceRequest = RunAcceptanceRequest(),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_capability("solution.acceptance.run")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
    identity: Identity = Depends(get_identity),
) -> dict:
    try:
        report = await delivery.run_acceptance(
            installation_id=installation_id,
            idempotency_key=idempotency_key or "",
            actor=principal.actor,
            manual_commands={key: str(value) for key, value in request.manual_commands.items()},
            policy_commands={key: str(value) for key, value in request.policy_commands.items()},
            authorization_denials={
                key: str(value) for key, value in request.authorization_denials.items()
            },
            authorization_evidence_runtime=identity,
        )
        return report.public_dict()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/delivery-reports/{report_id}")
async def get_delivery_report(
    report_id: UUID,
    principal: Principal = Depends(require_capability("solution.report.read")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    del principal
    try:
        return delivery.get_report(report_id).public_dict()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.post("/solution-packages/import", status_code=status.HTTP_201_CREATED)
async def import_solution_package(
    archive: UploadFile = File(...),
    principal: Principal = Depends(require_capability("solution.package.import")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    del principal
    try:
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = await archive.read(min(1024 * 1024, MAX_PACKAGE_ARCHIVE_BYTES + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if received > MAX_PACKAGE_ARCHIVE_BYTES:
                raise DeliveryError(
                    "PACKAGE_LIMIT_EXCEEDED",
                    "ZIP archive exceeds 10 MiB",
                )
        package = delivery.import_package(b"".join(chunks))
        return package.public_dict()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
