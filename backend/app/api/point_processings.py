"""Authenticated REST adapter for L1 point-processing planning and apply."""
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
from app.services.point_processing import (
    ApplyPointProcessingPlan,
    PreviewPointProcessing,
    PointProcessingDelivery,
    PointProcessingError,
)


router = APIRouter()
_point_processings: PointProcessingDelivery | None = None


def get_point_processings() -> PointProcessingDelivery:
    global _point_processings
    if _point_processings is None:
        from app.services.point_processing_postgres import build_postgres_point_processing

        _point_processings = build_postgres_point_processing()
    return _point_processings


class PointProcessingPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_revision_id: UUID
    input_selections: dict[str, UUID] = Field(default_factory=dict, max_length=256)

    def to_command(self, *, node_id: UUID, actor: str) -> PreviewPointProcessing:
        return PreviewPointProcessing(
            node_id=node_id,
            template_revision_id=self.template_revision_id,
            input_selections=self.input_selections,
            actor=actor,
        )


class ApplyPointProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Field(pattern="^[0-9a-f]{64}$")


def _raise_point_processing_http(exc: PointProcessingError) -> NoReturn:
    status_by_code = {
        "POINT_PROCESSING_PLAN_NOT_FOUND": status.HTTP_404_NOT_FOUND,
        "POINT_PROCESSING_INPUT_SELECTION_INVALID": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "POINT_PROCESSING_PLAN_STALE": status.HTTP_409_CONFLICT,
        "POINT_PROCESSING_PLAN_DIGEST_MISMATCH": status.HTTP_409_CONFLICT,
        "POINT_PROCESSING_IDEMPOTENCY_KEY_REUSED": status.HTTP_409_CONFLICT,
        "NEURON_POINT_CATALOG_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
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
    "/point-processing-templates",
    **protected(CONFIGURATION_READ),
)
async def list_point_processing_templates(
    device_category: str,
    service: PointProcessingDelivery = Depends(get_point_processings),
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
    service: PointProcessingDelivery = Depends(get_point_processings),
) -> dict:
    return service.inspect(
        node_id,
        include_engineering=principal.role in {"admin", "engineer"},
    ).public_dict()


@router.post(
    "/nodes/{node_id}/point-processing-plans",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def create_point_processing_plan(
    node_id: UUID,
    body: PointProcessingPlanRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointProcessingDelivery = Depends(get_point_processings),
) -> dict:
    try:
        return service.preview(
            body.to_command(node_id=node_id, actor=principal.actor)
        ).public_dict()
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)


@router.get(
    "/point-processing-plans/{plan_id}",
    **protected(CONFIGURATION_READ),
)
async def read_point_processing_plan(
    plan_id: UUID,
    service: PointProcessingDelivery = Depends(get_point_processings),
) -> dict:
    try:
        return service.get_plan(plan_id).public_dict()
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)


@router.post(
    "/point-processing-plans/{plan_id}/apply",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def apply_point_processing_plan(
    plan_id: UUID,
    body: ApplyPointProcessingRequest,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointProcessingDelivery = Depends(get_point_processings),
) -> dict:
    try:
        return service.apply(
            ApplyPointProcessingPlan(
                plan_id=plan_id,
                plan_digest=body.plan_digest,
                idempotency_key=idempotency_key,
                actor=principal.actor,
            )
        ).public_dict()
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)
