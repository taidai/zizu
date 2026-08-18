"""实体实例运行读取公开 Adapter。"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    RUNTIME_READ,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.identity import Principal
from app.services.entity_instance_registry import EntityInstanceError
from app.services.entity_instance_runtime import EntityInstanceRuntime
from app.services.entity_instance_catalog import EntityInstanceCatalog
from app.services.entity_instance_failover import EntityFailoverPolicy


router = APIRouter()


def get_entity_instance_runtime() -> EntityInstanceRuntime:
    from app.api.solution_delivery import get_default_entity_instance_runtime

    return get_default_entity_instance_runtime()


def get_entity_instance_catalog() -> EntityInstanceCatalog:
    from app.api.solution_delivery import get_default_entity_instance_catalog

    return get_default_entity_instance_catalog()


def get_entity_instance_failover() -> EntityFailoverPolicy:
    from app.api.solution_delivery import get_default_entity_instance_failover

    return get_default_entity_instance_failover()


class SourceFailoverRequest(BaseModel):
    expected_current_role: str = Field(pattern="^(primary|standby)$")
    target_role: str = Field(pattern="^(primary|standby)$")
    reason: str = Field(min_length=1, max_length=500)


@router.get(
    "/entity-instances/{entity_instance_id}/source-failover",
    **protected(CONFIGURATION_READ),
)
async def read_source_failover(
    entity_instance_id: UUID,
    failover: EntityFailoverPolicy = Depends(get_entity_instance_failover),
) -> dict:
    try:
        return failover.state(entity_instance_id).public_dict()
    except EntityInstanceError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.post(
    "/entity-instances/{entity_instance_id}/source-failover",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def switch_source_failover(
    entity_instance_id: UUID,
    command: SourceFailoverRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    failover: EntityFailoverPolicy = Depends(get_entity_instance_failover),
) -> dict:
    try:
        return failover.switch(
            entity_instance_id,
            expected_current_role=command.expected_current_role,
            target_role=command.target_role,
            actor=principal.actor,
            reason=command.reason,
        ).public_dict()
    except EntityInstanceError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get(
    "/entity-instances/legacy-migration-preview",
    **protected(CONFIGURATION_READ),
)
async def preview_legacy_entity_migration(
    catalog: EntityInstanceCatalog = Depends(get_entity_instance_catalog),
) -> dict:
    items = catalog.preview_legacy()
    counts = {classification: 0 for classification in ("unique", "missing", "ambiguous")}
    for item in items:
        counts[item.classification] += 1
    return {
        "items": [item.public_dict() for item in items],
        "counts": counts,
        "writes_applied": 0,
    }


@router.get("/entity-instances", **protected(RUNTIME_READ))
async def list_entity_instances(
    catalog: EntityInstanceCatalog = Depends(get_entity_instance_catalog),
) -> dict:
    items = catalog.list()
    return {"items": [item.public_dict() for item in items], "total": len(items)}


@router.get(
    "/entity-instances/{entity_instance_id}/realtime",
    **protected(RUNTIME_READ),
)
async def read_entity_instance_realtime(
    entity_instance_id: UUID,
    runtime: EntityInstanceRuntime = Depends(get_entity_instance_runtime),
) -> dict:
    try:
        return runtime.read(entity_instance_id).public_dict()
    except EntityInstanceError as exc:
        response_status = (
            status.HTTP_409_CONFLICT
            if exc.code in {"ENTITY_DATA_STALE", "ENTITY_DATA_QUALITY_BAD"}
            else status.HTTP_404_NOT_FOUND
        )
        raise HTTPException(
            status_code=response_status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get(
    "/entity-instances/{entity_instance_id}/history",
    **protected(RUNTIME_READ),
)
async def read_entity_instance_history(
    entity_instance_id: UUID,
    range_key: str = Query(
        "1h",
        alias="range",
        pattern="^(1h|6h|24h|7d)$",
    ),
    runtime: EntityInstanceRuntime = Depends(get_entity_instance_runtime),
) -> dict:
    try:
        items = runtime.history(entity_instance_id, range_key)
    except EntityInstanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {"items": [item.public_dict() for item in items], "total": len(items)}
