"""Single public API for dispatch-strategy configuration and runtime evidence."""
from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from decimal import Decimal
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import (
    CONFIGURATION_WRITE,
    RUNTIME_READ,
    principal_for,
    protected,
)
from app.services.dispatch_strategies import (
    DispatchWindow,
    EvaluationResult,
    StrategyBindingDraft,
    StrategyDraft,
    StrategyModelError,
    StrategyRevision,
    StrategyRuntime,
    StrategyView,
    build_two_charge_two_discharge_jdm,
)
from app.services.dispatch_strategy_postgres import (
    PostgresStrategyRepository,
    StrategyRepositoryError,
)
from app.services.gorules_adapter import StandardJdmError
from app.services.identity import Principal


router = APIRouter()


class StrategyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=1000)
    site_timezone: str = Field("Asia/Shanghai", min_length=1, max_length=100)
    starter: Literal["two_charge_two_discharge"] = "two_charge_two_discharge"


class BindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["INPUT", "OUTPUT"]
    binding_key: str = Field(min_length=1, max_length=200)
    ordinal: int = Field(ge=0)
    entity_instance_id: UUID
    expected_data_type: Literal["FLOAT", "INT", "BOOL", "STRING", "ENUM", "CODE_SET"]
    unit: str | None = Field(None, max_length=64)
    freshness_seconds: float = Field(gt=0, le=86400)


class StrategyDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_digest: str = Field(min_length=64, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=1000)
    trigger_kind: Literal["DATA_CHANGE", "FIXED_TICK"]
    site_timezone: str = Field(min_length=1, max_length=100)
    base_configuration_revision: int = Field(ge=0)
    jdm_content: dict[str, Any]
    bindings: list[BindingRequest] = Field(default_factory=list, max_length=100)


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: UUID | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_digest: str = Field(min_length=64, max_length=64)
    configuration_revision: int = Field(ge=0)


class EnableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: UUID


def get_dispatch_strategy_repository() -> PostgresStrategyRepository:
    return PostgresStrategyRepository()


def get_dispatch_strategy_runtime(
    repository: PostgresStrategyRepository = Depends(
        get_dispatch_strategy_repository
    ),
) -> StrategyRuntime:
    return StrategyRuntime(repository)


@router.get("/dispatch-strategies", **protected(RUNTIME_READ))
async def list_dispatch_strategies(
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    try:
        rows = await asyncio.to_thread(repository.list_strategies)
        return {"strategies": [_strategy(item) for item in rows]}
    except Exception as error:
        _raise_http(error)


@router.post("/dispatch-strategies", **protected(CONFIGURATION_WRITE))
async def create_dispatch_strategy(
    request: StrategyCreateRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    try:
        configuration_revision = await asyncio.to_thread(
            repository.current_configuration_revision
        )
        draft = StrategyDraft(
            request.name,
            request.description,
            "FIXED_TICK",
            request.site_timezone,
            _starter_jdm(),
            configuration_revision,
            (),
        )
        return _strategy(
            await asyncio.to_thread(repository.create_strategy, draft, principal.actor)
        )
    except Exception as error:
        _raise_http(error)


@router.get("/dispatch-strategies/{strategy_id}", **protected(RUNTIME_READ))
async def get_dispatch_strategy(
    strategy_id: UUID,
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    try:
        return _strategy(
            await asyncio.to_thread(repository.get_strategy, strategy_id)
        )
    except Exception as error:
        _raise_http(error)


@router.put("/dispatch-strategies/{strategy_id}/draft", **protected(CONFIGURATION_WRITE))
async def save_dispatch_strategy_draft(
    strategy_id: UUID,
    request: StrategyDraftRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    draft = StrategyDraft(
        request.name,
        request.description,
        request.trigger_kind,
        request.site_timezone,
        request.jdm_content,
        request.base_configuration_revision,
        tuple(_binding(item) for item in request.bindings),
    )
    try:
        view = await asyncio.to_thread(
            repository.save_draft,
            strategy_id,
            draft,
            request.expected_digest,
            principal.actor,
        )
        return _strategy(view)
    except Exception as error:
        _raise_http(error)


@router.post("/dispatch-strategies/{strategy_id}/simulate", **protected(RUNTIME_READ))
async def simulate_dispatch_strategy(
    strategy_id: UUID,
    request: SimulateRequest,
    repository=Depends(get_dispatch_strategy_repository),
    runtime=Depends(get_dispatch_strategy_runtime),
) -> dict[str, object]:
    try:
        view = await asyncio.to_thread(repository.get_strategy, strategy_id)
        allowed = tuple(
            item.id for item in (view.draft, view.active_revision) if item is not None
        )
        revision_id = request.revision_id or (
            view.draft.id if view.draft is not None else view.active_revision_id
        )
        if revision_id is None or revision_id not in allowed:
            raise StrategyRepositoryError("STRATEGY_REVISION_NOT_FOUND")
        result = await asyncio.to_thread(
            runtime.simulate,
            revision_id,
            request.overrides,
            datetime.now(UTC),
        )
        return _evaluation_result(result)
    except Exception as error:
        _raise_http(error)


@router.post("/dispatch-strategies/{strategy_id}/publish", **protected(CONFIGURATION_WRITE))
async def publish_dispatch_strategy(
    strategy_id: UUID,
    request: PublishRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    try:
        revision = await asyncio.to_thread(
            repository.publish,
            strategy_id,
            request.expected_digest,
            request.configuration_revision,
            principal.actor,
        )
        return _revision(revision)
    except Exception as error:
        _raise_http(error)


@router.post("/dispatch-strategies/{strategy_id}/enable", **protected(CONFIGURATION_WRITE))
async def enable_dispatch_strategy(
    strategy_id: UUID,
    request: EnableRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    try:
        return _strategy(
            await asyncio.to_thread(
                repository.enable,
                strategy_id,
                request.revision_id,
                principal.actor,
            )
        )
    except Exception as error:
        _raise_http(error)


@router.post("/dispatch-strategies/{strategy_id}/disable", **protected(CONFIGURATION_WRITE))
async def disable_dispatch_strategy(
    strategy_id: UUID,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    try:
        return _strategy(
            await asyncio.to_thread(repository.disable, strategy_id, principal.actor)
        )
    except Exception as error:
        _raise_http(error)


@router.post(
    "/dispatch-strategies/{strategy_id}/failure-latch/clear",
    **protected(CONFIGURATION_WRITE),
)
async def clear_dispatch_strategy_failure(
    strategy_id: UUID,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    try:
        return _strategy(
            await asyncio.to_thread(
                repository.clear_failure, strategy_id, principal.actor
            )
        )
    except Exception as error:
        _raise_http(error)


@router.get("/dispatch-strategies/{strategy_id}/events", **protected(RUNTIME_READ))
async def list_dispatch_strategy_events(
    strategy_id: UUID,
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    repository=Depends(get_dispatch_strategy_repository),
) -> dict[str, object]:
    try:
        before_at, before_id = _decode_cursor(cursor)
        rows, has_more = await asyncio.to_thread(
            repository.list_events,
            strategy_id,
            before_at,
            before_id,
            limit,
        )
        next_cursor = None
        if has_more and rows:
            next_cursor = _encode_cursor(rows[-1]["occurred_at"], rows[-1]["id"])
        return {"items": [_json(item) for item in rows], "next_cursor": next_cursor}
    except Exception as error:
        _raise_http(error)


def _starter_jdm() -> dict[str, object]:
    rows = (
        DispatchWindow("charge-1", "00:00", "06:00", "CHARGE", Decimal("0"), Decimal("10"), Decimal("90")),
        DispatchWindow("discharge-1", "10:00", "12:00", "DISCHARGE", Decimal("0"), Decimal("10"), Decimal("90")),
        DispatchWindow("charge-2", "12:00", "14:00", "CHARGE", Decimal("0"), Decimal("10"), Decimal("90")),
        DispatchWindow("discharge-2", "18:00", "22:00", "DISCHARGE", Decimal("0"), Decimal("10"), Decimal("90")),
    )
    return build_two_charge_two_discharge_jdm(rows, Decimal("0"))


def _binding(value: BindingRequest) -> StrategyBindingDraft:
    return StrategyBindingDraft(
        value.direction,
        value.binding_key,
        value.ordinal,
        value.entity_instance_id,
        value.expected_data_type,
        value.unit,
        value.freshness_seconds,
    )


def _strategy(value: StrategyView) -> dict[str, object]:
    return _json(
        {
            "id": value.id,
            "name": value.name,
            "description": value.description,
            "active_revision_id": value.active_revision_id,
            "enabled": value.enabled,
            "runtime_health": value.runtime_health,
            "last_trigger_key": value.last_trigger_key,
            "last_evaluated_at": value.last_evaluated_at,
            "last_desired": value.last_desired,
            "last_actual": value.last_actual,
            "last_evidence": value.last_evidence,
            "failure_code": value.failure_code,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
            "draft": None if value.draft is None else _revision(value.draft),
            "active_revision": None if value.active_revision is None else _revision(value.active_revision),
        }
    )


def _revision(value: StrategyRevision) -> dict[str, object]:
    return _json(
        {
            "id": value.id,
            "strategy_id": value.strategy_id,
            "revision": value.revision,
            "lifecycle": value.lifecycle,
            "trigger_kind": value.trigger_kind,
            "site_timezone": value.site_timezone,
            "jdm_content": value.jdm_content,
            "content_digest": value.content_digest,
            "base_configuration_revision": value.base_configuration_revision,
            "bindings": value.bindings,
            "created_by": value.created_by,
            "created_at": value.created_at,
            "published_by": value.published_by,
            "published_at": value.published_at,
        }
    )


def _evaluation_result(value: EvaluationResult) -> dict[str, object]:
    snapshot = {}
    if value.snapshot is not None:
        snapshot = {
            item.field_key: _json(
                {
                    "entity_instance_id": item.entity_instance_id,
                    "value": item.value,
                    "data_type": item.data_type,
                    "unit": item.unit,
                    "quality": item.quality,
                    "observed_at": item.observed_at,
                    "frame_sequence": item.frame_sequence,
                    "configuration_revision": item.configuration_revision,
                }
            )
            for item in value.snapshot.inputs
        }
    return _json(
        {
            "status": value.status,
            "reason_code": value.reason_code,
            "frame_sequence": None if value.snapshot is None else value.snapshot.frame_sequence,
            "configuration_revision": None if value.snapshot is None else value.snapshot.configuration_revision,
            "snapshot": snapshot,
            "engine_inputs": value.engine_inputs,
            "matched_rules": () if value.evaluation is None else value.evaluation.matched_rules,
            "decision": None if value.evaluation is None else value.evaluation.decision,
            "proposed_intents": value.intents,
        }
    )


def _json(value: object) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _json(getattr(value, key))
            for key in value.__dataclass_fields__
        }
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _encode_cursor(occurred_at: datetime, event_id: UUID) -> str:
    raw = json.dumps([occurred_at.isoformat(), str(event_id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(value: str | None) -> tuple[datetime | None, UUID | None]:
    if value is None:
        return None, None
    try:
        raw_at, raw_id = json.loads(
            base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        )
        return datetime.fromisoformat(raw_at), UUID(raw_id)
    except Exception as error:
        raise StrategyModelError("STRATEGY_EVENT_CURSOR_INVALID", "cursor is invalid") from error


def _raise_http(error: Exception) -> None:
    if isinstance(error, (StrategyRepositoryError, StrategyModelError, StandardJdmError)):
        code = error.code
        status = 404 if code in {
            "STRATEGY_NOT_FOUND",
            "STRATEGY_REVISION_NOT_FOUND",
            "STRATEGY_PUBLISHED_REVISION_NOT_FOUND",
        } else 409
        if code in {"COMMITTED_L2_SNAPSHOT_UNAVAILABLE"}:
            status = 503
        raise HTTPException(
            status_code=status,
            detail={"code": code, "message": str(error)},
        ) from error
    raise HTTPException(
        status_code=500,
        detail={"code": "DISPATCH_STRATEGY_OPERATION_FAILED", "message": "调度策略操作失败"},
    ) from error


__all__ = [
    "get_dispatch_strategy_repository",
    "get_dispatch_strategy_runtime",
    "router",
]
