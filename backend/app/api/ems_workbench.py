"""Fixed EMS workbench read API."""
from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import (
    CONFIGURATION_WRITE,
    RUNTIME_READ,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.configuration_revision import ConfigurationRevisionError
from app.services.data_trunk_contracts import DataTrunkError
from app.services.ems_workbench import EmsWorkbench, EmsWorkbenchError
from app.services.ems_workbench_slots import EmsWorkbenchSlotError, EmsWorkbenchSlots
from app.services.identity import Principal


router = APIRouter()
_workbench: EmsWorkbench | None = None
_slots: EmsWorkbenchSlots | None = None


class WorkbenchSlotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_instance_id: UUID | None
    base_configuration_revision: int = Field(ge=0)


def get_ems_workbench_slots() -> EmsWorkbenchSlots:
    global _slots
    if _slots is None:
        from app.api.entity_instances import get_entity_instance_catalog
        from app.services.ems_workbench_slots_postgres import (
            PostgresWorkbenchSlotRepository,
        )

        _slots = EmsWorkbenchSlots(
            get_entity_instance_catalog(),
            PostgresWorkbenchSlotRepository(),
        )
    return _slots


def get_ems_workbench_runtime():
    from app.main import get_pipeline

    runtime = get_pipeline()
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DATA_TRUNK_UNAVAILABLE",
                "message": "数据主干尚未启动",
            },
        )
    return runtime


def get_ems_workbench() -> EmsWorkbench:
    global _workbench
    if _workbench is None:
        from app.api.entity_instances import get_entity_instance_catalog, get_entity_instance_runtime
        from app.services.configuration_revision_postgres import PostgresConfigurationRevisions

        _workbench = EmsWorkbench(
            get_entity_instance_catalog(),
            get_entity_instance_runtime(),
            PostgresConfigurationRevisions().current,
            get_ems_workbench_slots(),
        )
    return _workbench


def _error(error: EmsWorkbenchError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": error.code, "message": str(error)})


def _slot_error(
    error: EmsWorkbenchSlotError | ConfigurationRevisionError | DataTrunkError,
) -> HTTPException:
    status_by_code = {
        "WORKBENCH_SLOT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
        "WORKBENCH_SLOT_ENTITY_NOT_AVAILABLE": status.HTTP_404_NOT_FOUND,
        "WORKBENCH_SLOT_IDEMPOTENCY_CONFLICT": status.HTTP_409_CONFLICT,
        "CONFIGURATION_REVISION_STALE": status.HTTP_409_CONFLICT,
        "DATA_FRAME_CONFIGURATION_STALE": status.HTTP_409_CONFLICT,
        "CONFIGURATION_RUNTIME_BUSY": status.HTTP_409_CONFLICT,
        "CONFIGURATION_RUNTIME_DRAIN_TIMEOUT": status.HTTP_409_CONFLICT,
        "COMMITTED_FRAME_CONSUMER_MISSING": status.HTTP_503_SERVICE_UNAVAILABLE,
        "CONFIGURATION_RUNTIME_NOT_QUIESCED": status.HTTP_503_SERVICE_UNAVAILABLE,
        "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED": status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    return HTTPException(
        status_code=status_by_code.get(error.code, status.HTTP_422_UNPROCESSABLE_CONTENT),
        detail={"code": error.code, "message": str(error)},
    )


@router.get("/ems-workbench", **protected(RUNTIME_READ))
async def read_ems_workbench(workbench: EmsWorkbench = Depends(get_ems_workbench)) -> dict:
    return workbench.read()


@router.get("/ems-workbench/trends/{trend_id}", **protected(RUNTIME_READ))
async def read_ems_workbench_trend(trend_id: str, range: str = Query("24h", pattern="^(1h|24h|7d|30d)$"), workbench: EmsWorkbench = Depends(get_ems_workbench)) -> dict:
    try:
        return workbench.trend(trend_id, range)
    except EmsWorkbenchError as error:
        raise _error(error) from error


@router.put(
    "/ems-workbench/slots/{slot_key}",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def put_ems_workbench_slot(
    slot_key: str,
    body: WorkbenchSlotRequest,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
    ),
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    slots: EmsWorkbenchSlots = Depends(get_ems_workbench_slots),
    runtime=Depends(get_ems_workbench_runtime),
) -> dict:
    try:
        receipt = await asyncio.to_thread(
            slots.bind,
            slot_key=slot_key,
            entity_instance_id=body.entity_instance_id,
            base_configuration_revision=body.base_configuration_revision,
            actor=principal.actor,
            idempotency_key=idempotency_key,
            runtime_gate=runtime.data_trunk.configuration_gate,
        )
    except (EmsWorkbenchSlotError, ConfigurationRevisionError, DataTrunkError) as error:
        raise _slot_error(error) from error
    return {
        "slot_key": receipt.slot_key,
        "entity_instance_id": (
            str(receipt.entity_instance_id)
            if receipt.entity_instance_id is not None
            else None
        ),
        "configuration_revision": receipt.configuration_revision,
        "replayed": receipt.replayed,
    }
