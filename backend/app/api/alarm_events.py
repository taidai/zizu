"""统一告警事件的公开只读与确认 Adapter。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import (
    ALARM_ACKNOWLEDGE,
    RUNTIME_READ,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.alarm_runtime import (
    AcknowledgeAlarm,
    AlarmEvent,
    AlarmRuntime,
    AlarmRuntimeError,
    AlarmTransition,
)
from app.services.identity import Principal


router = APIRouter()


def get_alarm_runtime() -> AlarmRuntime:
    from app.api.solution_delivery import get_default_alarm_runtime

    return get_default_alarm_runtime()


class AcknowledgeAlarmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(None, max_length=500)


def _event_public(event: AlarmEvent) -> dict[str, Any]:
    value = asdict(event)
    value["model_version"] = "v1"
    value["id"] = str(event.id)
    value["definition_id"] = str(event.definition_id)
    value["entity_instance_id"] = str(event.entity_instance_id)
    for field in (
        "pending_at",
        "active_at",
        "acknowledged_at",
        "recovery_candidate_since",
        "recovered_at",
    ):
        if value[field] is not None:
            value[field] = value[field].isoformat()
    return value


def _transition_public(transition: AlarmTransition) -> dict[str, Any]:
    value = asdict(transition)
    value["event_id"] = str(transition.event_id)
    value["occurred_at"] = transition.occurred_at.isoformat()
    return value


def _error(exc: AlarmRuntimeError) -> HTTPException:
    response_status = (
        status.HTTP_404_NOT_FOUND
        if exc.code == "ALARM_EVENT_NOT_FOUND"
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(
        status_code=response_status,
        detail={"code": exc.code, "message": str(exc)},
    )


@router.get("/alarm-events", **protected(RUNTIME_READ))
async def list_alarm_events(
    runtime: AlarmRuntime = Depends(get_alarm_runtime),
) -> dict[str, Any]:
    items = runtime.list()
    return {
        "items": [_event_public(item) for item in items],
        "total": len(items),
        "model_version": "v1",
    }


@router.get("/alarm-events/{event_id}", **protected(RUNTIME_READ))
async def get_alarm_event(
    event_id: UUID,
    runtime: AlarmRuntime = Depends(get_alarm_runtime),
) -> dict[str, Any]:
    try:
        return _event_public(runtime.get(event_id))
    except AlarmRuntimeError as exc:
        raise _error(exc) from exc


@router.get("/alarm-events/{event_id}/transitions", **protected(RUNTIME_READ))
async def list_alarm_event_transitions(
    event_id: UUID,
    runtime: AlarmRuntime = Depends(get_alarm_runtime),
) -> dict[str, Any]:
    try:
        items = runtime.timeline(event_id)
    except AlarmRuntimeError as exc:
        raise _error(exc) from exc
    return {
        "items": [_transition_public(item) for item in items],
        "total": len(items),
        "model_version": "v1",
    }


@router.post(
    "/alarm-events/{event_id}/acknowledgements",
    openapi_extra=capability_metadata(ALARM_ACKNOWLEDGE),
)
async def acknowledge_alarm_event(
    event_id: UUID,
    command: AcknowledgeAlarmRequest,
    principal: Principal = Depends(principal_for(ALARM_ACKNOWLEDGE)),
    runtime: AlarmRuntime = Depends(get_alarm_runtime),
) -> dict[str, Any]:
    try:
        outcome = runtime.acknowledge(
            AcknowledgeAlarm(
                event_id=event_id,
                actor=principal.actor,
                acknowledged_at=datetime.now(timezone.utc),
                note=command.note,
            )
        )
        return {
            "event_id": str(outcome.event_id),
            "state": outcome.state,
            "transition": outcome.transition,
            "code": outcome.code,
        }
    except AlarmRuntimeError as exc:
        raise _error(exc) from exc
