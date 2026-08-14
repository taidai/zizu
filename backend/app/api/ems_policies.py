"""Public engineer seams for installed, declarative EMS policies."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.business_security import (
    CONFIGURATION_WRITE,
    capability_metadata,
    principal_for,
    protected,
)
from app.api.solution_delivery import get_default_ems_policy_runtime
from app.services.identity import Principal
from app.services.ems_policy_runtime import EmsPolicyRuntime
from app.services.solution_delivery_contracts import DeliveryError


router = APIRouter()


def _require_policy_engineer(principal: Principal) -> Principal:
    if principal.role != "engineer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "POLICY_ENGINEER_REQUIRED",
                "message": "Only an implementation engineer may change policy activation",
            },
        )
    return principal


@router.post("/ems-policies/{policy_id}/simulate", **protected(CONFIGURATION_WRITE))
async def simulate_ems_policy(
    policy_id: str,
    policies: EmsPolicyRuntime = Depends(get_default_ems_policy_runtime),
) -> dict:
    try:
        return policies.simulate(policy_id)
    except DeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post(
    "/ems-policies/{policy_id}/enable",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def enable_ems_policy(
    policy_id: str,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    policies: EmsPolicyRuntime = Depends(get_default_ems_policy_runtime),
) -> dict:
    _require_policy_engineer(principal)
    try:
        return policies.enable(policy_id, actor=principal.actor)
    except DeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post(
    "/ems-policies/{policy_id}/disable",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def disable_ems_policy(
    policy_id: str,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    policies: EmsPolicyRuntime = Depends(get_default_ems_policy_runtime),
) -> dict:
    _require_policy_engineer(principal)
    try:
        return policies.disable(policy_id, actor=principal.actor)
    except DeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/ems-policies/{policy_id}/evaluate", **protected(CONFIGURATION_WRITE))
async def evaluate_ems_policy(
    policy_id: str,
    policies: EmsPolicyRuntime = Depends(get_default_ems_policy_runtime),
) -> dict:
    try:
        return policies.evaluate(policy_id)
    except DeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc
