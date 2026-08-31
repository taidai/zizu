"""Authenticated REST adapter for L1 point-processing planning and apply."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    RUNTIME_READ,
    SYSTEM_MANAGE,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.identity import Principal
from app.services.point_processing import (
    ApplyPointProcessingPlan,
    PreviewPointProcessing,
    PointProcessingService,
    PointProcessingError,
)
from app.services.point_processing_templates import (
    PointProcessingTemplateError,
    canonical_point_processing_content,
    parse_point_processing_template,
    point_processing_revision_id,
)


router = APIRouter()
_point_processings: PointProcessingService | None = None
_point_processing_templates: Any | None = None


def get_point_processings() -> PointProcessingService:
    global _point_processings
    if _point_processings is None:
        from app.services.point_processing_postgres import build_postgres_point_processing

        _point_processings = build_postgres_point_processing()
    return _point_processings


def get_point_processing_templates() -> Any:
    global _point_processing_templates
    if _point_processing_templates is None:
        from app.services.point_processing_postgres import (
            PostgresPointProcessingTemplates,
        )

        _point_processing_templates = PostgresPointProcessingTemplates()
    return _point_processing_templates


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


class PointProcessingDraftPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: dict[str, Any]
    input_selections: dict[str, UUID] = Field(default_factory=dict, max_length=256)


class PromotePointProcessingTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)
    brand: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)


class ApplyPointProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Field(pattern="^[0-9a-f]{64}$")


class PointProcessingFormulaPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_revision_id: UUID
    expression: str = Field(min_length=1, max_length=4096)


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


def _raise_template_http(exc: PointProcessingTemplateError) -> NoReturn:
    status_by_code = {
        "POINT_PROCESSING_TEMPLATE_NOT_FOUND": status.HTTP_404_NOT_FOUND,
        "POINT_PROCESSING_REVISION_IMMUTABLE": status.HTTP_409_CONFLICT,
        "POINT_PROCESSING_CATALOG_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    raise HTTPException(
        status_code=status_by_code.get(
            exc.code,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


def _plan_with_trial(
    service: PointProcessingService,
    plan: Any,
) -> dict[str, Any]:
    payload = plan.public_dict()
    try:
        trial = service.trial(plan)
    except PointProcessingError as exc:
        payload["trial"] = {
            "available": False,
            "reason": exc.code,
            "message": str(exc),
        }
    else:
        payload["trial"] = (
            None
            if trial is None
            else {"available": True, **trial.public_dict()}
        )
    return payload


@router.post(
    "/point-processing-templates/import",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
async def import_point_processing_template(
    body: dict[str, Any],
    principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
    templates: Any = Depends(get_point_processing_templates),
) -> dict[str, Any]:
    try:
        return templates.import_template(body, actor=principal.actor).public_dict()
    except PointProcessingTemplateError as exc:
        _raise_template_http(exc)


@router.post(
    "/point-processing-templates/validate",
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
async def validate_point_processing_template(
    body: dict[str, Any],
    _principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
) -> dict[str, Any]:
    """Validate an immutable revision without changing the template registry."""
    try:
        template = parse_point_processing_template(body)
    except PointProcessingTemplateError as exc:
        _raise_template_http(exc)
    return {
        "revision_id": str(point_processing_revision_id(template)),
        "content_digest": template.content_digest,
        "content": canonical_point_processing_content(template),
    }


@router.get(
    "/point-processing-templates/{revision_id}/export",
    **protected(CONFIGURATION_READ),
)
async def export_point_processing_template(
    revision_id: UUID,
    templates: Any = Depends(get_point_processing_templates),
) -> JSONResponse:
    try:
        content = templates.export_template(revision_id)
    except PointProcessingTemplateError as exc:
        _raise_template_http(exc)
    key = str(content["id"]).replace('"', "")
    digest = hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return JSONResponse(
        content=content,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{key}.zizu-point-processing.json"'
            ),
            "ETag": f'"{digest}"',
        },
    )


@router.get(
    "/point-processing-templates",
    **protected(CONFIGURATION_READ),
)
async def list_point_processing_templates(
    device_category: str,
    service: PointProcessingService = Depends(get_point_processings),
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
    service: PointProcessingService = Depends(get_point_processings),
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
    service: PointProcessingService = Depends(get_point_processings),
) -> dict:
    try:
        plan = service.preview(
            body.to_command(node_id=node_id, actor=principal.actor)
        )
        return _plan_with_trial(service, plan)
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)


@router.post(
    "/nodes/{node_id}/point-processing-drafts/plan",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def create_point_processing_draft_plan(
    node_id: UUID,
    body: PointProcessingDraftPlanRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointProcessingService = Depends(get_point_processings),
) -> dict:
    try:
        plan = service.preview_node_definition(
            node_id=node_id,
            content=body.content,
            input_selections=body.input_selections,
            actor=principal.actor,
        )
        return _plan_with_trial(service, plan)
    except PointProcessingTemplateError as exc:
        _raise_template_http(exc)
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)


@router.post(
    "/nodes/{node_id}/point-processing-deactivation-plan",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def create_point_processing_deactivation_plan(
    node_id: UUID,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointProcessingService = Depends(get_point_processings),
) -> dict:
    try:
        return _plan_with_trial(
            service,
            service.preview_deactivation(
                node_id=node_id,
                actor=principal.actor,
            ),
        )
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)


@router.post(
    "/nodes/{node_id}/point-processing-templates/promote",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
async def promote_point_processing_template(
    node_id: UUID,
    body: PromotePointProcessingTemplateRequest,
    principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
    service: PointProcessingService = Depends(get_point_processings),
) -> dict:
    try:
        return service.promote_node_definition(
            node_id=node_id,
            asset_id=body.asset_id,
            display_name=body.display_name,
            brand=body.brand,
            model=body.model,
            actor=principal.actor,
        ).public_dict()
    except PointProcessingTemplateError as exc:
        _raise_template_http(exc)
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)

@router.post(
    "/nodes/{node_id}/point-processing-formula-preview",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def preview_point_processing_formula(
    node_id: UUID,
    body: PointProcessingFormulaPreviewRequest,
    _principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointProcessingService = Depends(get_point_processings),
) -> dict:
    try:
        return dict(
            service.preview_formula(
                node_id=node_id,
                template_revision_id=body.template_revision_id,
                expression=body.expression,
            )
        )
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)


@router.get(
    "/point-processing-plans/{plan_id}",
    **protected(CONFIGURATION_READ),
)
async def read_point_processing_plan(
    plan_id: UUID,
    service: PointProcessingService = Depends(get_point_processings),
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
    service: PointProcessingService = Depends(get_point_processings),
) -> dict:
    try:
        command = ApplyPointProcessingPlan(
            plan_id=plan_id,
            plan_digest=body.plan_digest,
            idempotency_key=idempotency_key,
            actor=principal.actor,
        )
        return (await asyncio.to_thread(service.apply, command)).public_dict()
    except PointProcessingError as exc:
        _raise_point_processing_http(exc)
