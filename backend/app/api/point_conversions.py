"""Authenticated REST adapter for L1 point-conversion planning and apply."""
from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    RUNTIME_READ,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.identity import Principal
from app.services.point_conversion import (
    ApplyPointConversionPlan,
    PlanPointConversion,
    PointConversion,
    PointConversionError,
)


router = APIRouter()
_point_conversions: PointConversion | None = None


def get_point_conversions() -> PointConversion:
    global _point_conversions
    if _point_conversions is None:
        from app.services.point_conversion_postgres import build_postgres_point_conversion

        _point_conversions = build_postgres_point_conversion()
    return _point_conversions


class PointConversionPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_revision_id: UUID
    input_selections: dict[str, UUID] = Field(default_factory=dict, max_length=64)

    def to_command(self, *, node_id: UUID, actor: str) -> PlanPointConversion:
        return PlanPointConversion(
            node_id=node_id,
            template_revision_id=self.template_revision_id,
            input_selections=self.input_selections,
            actor=actor,
        )


class ApplyPointConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Field(pattern="^[0-9a-f]{64}$")


def _raise_point_conversion_http(exc: PointConversionError) -> NoReturn:
    status_by_code = {
        "POINT_CONVERSION_PLAN_NOT_FOUND": status.HTTP_404_NOT_FOUND,
        "POINT_CONVERSION_INPUT_SELECTION_INVALID": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "POINT_CONVERSION_PLAN_STALE": status.HTTP_409_CONFLICT,
        "POINT_CONVERSION_PLAN_DIGEST_MISMATCH": status.HTTP_409_CONFLICT,
        "POINT_CONVERSION_IDEMPOTENCY_KEY_REUSED": status.HTTP_409_CONFLICT,
        "DATA_TRUNK_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    raise HTTPException(
        status_code=status_by_code.get(
            exc.code,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.get(
    "/point-conversion-templates",
    **protected(CONFIGURATION_READ),
)
async def list_point_conversion_templates(
    device_category: str,
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    items = service.list_templates(device_category.upper())
    return {
        "items": [item.public_dict() for item in items],
        "total": len(items),
    }


@router.get(
    "/nodes/{node_id}/data-trunk",
    openapi_extra=capability_metadata(RUNTIME_READ),
)
async def read_node_data_trunk(
    node_id: UUID,
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    return service.inspect_node(
        node_id,
        include_engineering=principal.role in {"admin", "engineer"},
    ).public_dict()


@router.post(
    "/nodes/{node_id}/point-conversion-plans",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def create_point_conversion_plan(
    node_id: UUID,
    body: PointConversionPlanRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    try:
        return service.plan(
            body.to_command(node_id=node_id, actor=principal.actor)
        ).public_dict()
    except PointConversionError as exc:
        _raise_point_conversion_http(exc)


@router.get(
    "/point-conversion-plans/{plan_id}",
    **protected(CONFIGURATION_READ),
)
async def read_point_conversion_plan(
    plan_id: UUID,
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    try:
        return service.get_plan(plan_id).public_dict()
    except PointConversionError as exc:
        _raise_point_conversion_http(exc)


@router.post(
    "/point-conversion-plans/{plan_id}/apply",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def apply_point_conversion_plan(
    plan_id: UUID,
    body: ApplyPointConversionRequest,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    try:
        return service.apply(
            ApplyPointConversionPlan(
                plan_id=plan_id,
                plan_digest=body.plan_digest,
                idempotency_key=idempotency_key,
                actor=principal.actor,
            )
        ).public_dict()
    except PointConversionError as exc:
        _raise_point_conversion_http(exc)
