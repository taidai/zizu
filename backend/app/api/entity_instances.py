"""实体实例运行读取公开 Adapter。"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.business_security import (
    RUNTIME_READ,
    protected,
)
from app.services.entity_instance_registry import EntityInstanceError
from app.services.entity_instance_runtime import EntityInstanceRuntime
from app.services.entity_instance_catalog import EntityInstanceCatalog


router = APIRouter()
_repository = None
_runtime = None
_catalog = None
_registry = None


def _entity_repository():
    global _repository
    if _repository is None:
        from app.services.entity_instance_postgres import PostgresEntityInstanceRepository

        _repository = PostgresEntityInstanceRepository()
    return _repository


def get_entity_instance_repository():
    return _entity_repository()


def get_entity_instance_registry():
    global _registry
    if _registry is None:
        from app.services.configuration_revision_postgres import (
            PostgresConfigurationRevisions,
        )
        from app.services.entity_instance_postgres import PostgresSourceCatalog
        from app.services.entity_instance_registry import EntityInstanceRegistry

        _registry = EntityInstanceRegistry(
            _entity_repository(),
            PostgresSourceCatalog(),
            PostgresConfigurationRevisions().current,
        )
    return _registry


def get_entity_instance_runtime() -> EntityInstanceRuntime:
    global _runtime
    if _runtime is None:
        from app.services.entity_instance_postgres import PostgresObservationCatalog

        _runtime = EntityInstanceRuntime(
            get_entity_instance_registry(),
            PostgresObservationCatalog(),
        )
    return _runtime


def get_entity_instance_catalog() -> EntityInstanceCatalog:
    global _catalog
    if _catalog is None:
        _catalog = EntityInstanceCatalog(_entity_repository())
    return _catalog


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
        return runtime.read_for_alarm(entity_instance_id).public_dict()
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
