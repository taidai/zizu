"""实体实例运行读取公开 Adapter。"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.business_security import RUNTIME_READ, protected
from app.services.entity_instance_registry import EntityInstanceError
from app.services.entity_instance_runtime import EntityInstanceRuntime


router = APIRouter()


def get_entity_instance_runtime() -> EntityInstanceRuntime:
    from app.api.solution_delivery import get_default_entity_instance_runtime

    return get_default_entity_instance_runtime()


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
