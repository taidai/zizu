"""解决方案包公开 HTTP Adapter。"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field, ValidationError

from app.api.security import require_capability
from app.api.health import _VERSION
from app.core.config import settings
from app.services.identity import Principal
from app.services.solution_delivery import (
    DeliveryError,
    HttpxPublicApiProbe,
    MAX_PACKAGE_ARCHIVE_BYTES,
    PostgresDeliveryRepository,
    SolutionDelivery,
)


router = APIRouter()
_delivery = SolutionDelivery(
    PostgresDeliveryRepository(),
    platform_version=_VERSION,
    public_api_probe=HttpxPublicApiProbe(settings.effective_public_api_base_url),
)


def get_solution_delivery() -> SolutionDelivery:
    return _delivery


class ApplyInstallationRequest(BaseModel):
    plan_digest: str = Field(..., min_length=64, max_length=64)


class CreateInstallationPlanRequest(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    secret_references: dict[str, str] = Field(default_factory=dict)


@router.get("/solution-packages")
async def list_solution_packages(
    principal: Principal = Depends(require_capability("solution.package.read")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    del principal
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
    del principal
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
        ).public_dict()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
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
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_capability("solution.acceptance.run")),
    delivery: SolutionDelivery = Depends(get_solution_delivery),
) -> dict:
    try:
        report = await delivery.run_acceptance(
            installation_id=installation_id,
            idempotency_key=idempotency_key or "",
            actor=principal.actor,
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
