"""Fixed EMS workbench read API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.business_security import RUNTIME_READ, protected
from app.services.ems_workbench import EmsWorkbench, EmsWorkbenchError


router = APIRouter()
_workbench: EmsWorkbench | None = None


def get_ems_workbench() -> EmsWorkbench:
    global _workbench
    if _workbench is None:
        from app.api.entity_instances import get_entity_instance_catalog, get_entity_instance_runtime
        from app.services.configuration_revision_postgres import PostgresConfigurationRevisions

        _workbench = EmsWorkbench(
            get_entity_instance_catalog(),
            get_entity_instance_runtime(),
            PostgresConfigurationRevisions().current,
        )
    return _workbench


def _error(error: EmsWorkbenchError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": error.code, "message": str(error)})


@router.get("/ems-workbench", **protected(RUNTIME_READ))
async def read_ems_workbench(workbench: EmsWorkbench = Depends(get_ems_workbench)) -> dict:
    return workbench.read()


@router.get("/ems-workbench/trends/{trend_id}", **protected(RUNTIME_READ))
async def read_ems_workbench_trend(trend_id: str, range: str = Query("24h", pattern="^(1h|24h|7d|30d)$"), workbench: EmsWorkbench = Depends(get_ems_workbench)) -> dict:
    try:
        return workbench.trend(trend_id, range)
    except EmsWorkbenchError as error:
        raise _error(error) from error
