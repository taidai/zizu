"""统一告警事件的公开只读与确认 Adapter。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

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
    AlarmEventPresentation,
    AlarmRuntime,
    AlarmRuntimeError,
    AlarmTransition,
)
from app.services.identity import Principal


router = APIRouter()
_runtime: AlarmRuntime | None = None


def get_alarm_runtime() -> AlarmRuntime:
    global _runtime
    if _runtime is None:
        from app.services.alarm_postgres import (
            PostgresAlarmDefinitionCatalog,
            PostgresAlarmRepository,
        )

        _runtime = AlarmRuntime(
            PostgresAlarmDefinitionCatalog(),
            PostgresAlarmRepository(),
        )
    return _runtime


class AcknowledgeAlarmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(None, max_length=500)


def _event_public(
    event: AlarmEvent,
    presentation: AlarmEventPresentation,
) -> dict[str, Any]:
    started_at = event.active_at or event.pending_at
    ended_at = event.recovered_at or datetime.now(timezone.utc)
    return {
        "model_version": "v1",
        "id": str(event.id),
        "definition_id": str(event.definition_id),
        "entity_instance_id": str(event.entity_instance_id),
        "state": event.state,
        "severity": event.severity,
        "pending_at": event.pending_at.isoformat(),
        "active_at": event.active_at.isoformat() if event.active_at else None,
        "acknowledged_at": event.acknowledged_at.isoformat() if event.acknowledged_at else None,
        "acknowledged_by": event.acknowledged_by,
        "acknowledgement_note": event.acknowledgement_note,
        "recovered_at": event.recovered_at.isoformat() if event.recovered_at else None,
        "node_name": presentation.node_name,
        "entity_name": presentation.entity_name,
        "alarm_name": presentation.alarm_name,
        "duration_seconds": max(0, int((ended_at - started_at).total_seconds())),
    }


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
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    state: str | None = Query(
        None,
        pattern="^(pending|active_unacknowledged|active_acknowledged|recovered|open)$",
    ),
    severity: str | None = Query(None, pattern="^(INFO|WARNING|MAJOR|CRITICAL)$"),
    entity_instance_id: UUID | None = None,
    runtime: AlarmRuntime = Depends(get_alarm_runtime),
) -> dict[str, Any]:
    result = await run_in_threadpool(runtime.query, page, page_size, state, severity, entity_instance_id)
    items = result.items
    presentations = await run_in_threadpool(runtime.describe, items)
    return {
        "items": [_event_public(item, presentations[item.id]) for item in items],
        "total": result.total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (result.total + page_size - 1) // page_size),
        "summary": {
            "active": result.active,
            "unacknowledged": result.unacknowledged,
            "critical": result.critical,
        },
        "model_version": "v1",
    }


@router.get("/alarm-events/{event_id}", **protected(RUNTIME_READ))
async def get_alarm_event(
    event_id: UUID,
    runtime: AlarmRuntime = Depends(get_alarm_runtime),
) -> dict[str, Any]:
    try:
        event = runtime.get(event_id)
        return _event_public(event, runtime.describe((event,))[event.id])
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
            "audit_event_id": str(outcome.audit_event_id) if outcome.audit_event_id else None,
        }
    except AlarmRuntimeError as exc:
        raise _error(exc) from exc
