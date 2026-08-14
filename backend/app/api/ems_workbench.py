"""Operator-facing fixed EMS workbench read interface."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.business_security import RUNTIME_READ, protected
from app.api.solution_delivery import get_default_ems_workbench
from app.services.ems_workbench import EmsWorkbench
from app.services.solution_delivery_contracts import DeliveryError


router = APIRouter()


@router.get("/ems-workbench", **protected(RUNTIME_READ))
async def read_ems_workbench(
    workbench: EmsWorkbench = Depends(get_default_ems_workbench),
) -> dict:
    try:
        return workbench.read()
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/ems-workbench/trends/{trend_id}", **protected(RUNTIME_READ))
async def read_ems_workbench_trend(
    trend_id: str,
    range: str = Query("24h", pattern="^(1h|24h|7d|30d)$"),
    workbench: EmsWorkbench = Depends(get_default_ems_workbench),
) -> dict:
    try:
        return workbench.trend(trend_id, range)
    except DeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
