"""JDM configuration, side-effect-free simulation, and execution evidence."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    RUNTIME_READ,
    principal_for,
    protected,
)
from app.api.entity_instances import get_entity_instance_catalog
from app.services.entity_instance_catalog import (
    EntityInstanceCatalog,
    EntityInstanceReferenceError,
    validate_rule_entity_references,
)
from app.services.identity import Principal
from app.services.jdm_runtime import (
    JdmRuntimeError,
    evaluate_model_content,
    required_inputs,
)


router = APIRouter()


class RuleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    rule_type: str = Field(min_length=1, max_length=32)
    jdm_content: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class RuleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=200)
    rule_type: str | None = Field(None, min_length=1, max_length=32)
    jdm_content: dict[str, Any] | None = None
    enabled: bool | None = None


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: dict[str, Any] = Field(default_factory=dict)


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


def get_jdm_rules():
    from app.services.jdm_postgres import PostgresJdmRules

    return PostgresJdmRules()


def get_jdm_runtime():
    from app.main import get_pipeline

    runtime = get_pipeline()
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATA_TRUNK_UNAVAILABLE",
                "message": "数据主干尚未启动",
            },
        )
    return runtime


def _serialize(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialize(item) for item in value]
    return value


def _serialize_rule(row: dict[str, Any]) -> dict[str, Any]:
    return _serialize(dict(row))


def _raise_jdm_http(error: Exception) -> None:
    from app.services.configuration_revision import ConfigurationRevisionError
    from app.services.data_trunk_contracts import DataTrunkError
    from app.services.jdm_postgres import JdmRuleError

    if isinstance(error, EntityInstanceReferenceError):
        code = error.code
    elif isinstance(error, (JdmRuleError, JdmRuntimeError)):
        code = error.code
    elif isinstance(error, ConfigurationRevisionError):
        code = error.code
    elif isinstance(error, DataTrunkError):
        code = error.code
    else:
        logger.exception("[API/rules] JDM operation failed")
        raise HTTPException(
            status_code=500,
            detail={"code": "JDM_OPERATION_FAILED", "message": "JDM 操作失败"},
        ) from error

    status_by_code = {
        "JDM_RULE_NOT_FOUND": 404,
        "CONFIGURATION_REVISION_UNAVAILABLE": 503,
        "DATA_TRUNK_UNAVAILABLE": 503,
        "CONFIGURATION_REVISION_STALE": 409,
        "DATA_FRAME_CONFIGURATION_STALE": 409,
        "CONFIGURATION_RUNTIME_BUSY": 409,
    }
    raise HTTPException(
        status_code=status_by_code.get(code, 409),
        detail={"code": code, "message": str(error)},
    ) from error


def _require_runtime_type(rule_type: str) -> None:
    if rule_type not in {"control", "linkage"}:
        from app.services.jdm_postgres import JdmRuleError

        raise JdmRuleError("JDM_RULE_TYPE_UNSUPPORTED")


def _validate_content(
    content: dict[str, Any],
    catalog: EntityInstanceCatalog,
) -> tuple[tuple[str, str, UUID], ...]:
    from app.services.jdm_postgres import JdmRuleError

    config = content.get("_config")
    if not isinstance(config, dict):
        raise JdmRuleError("JDM_MODEL_INVALID")
    forbidden_sources = {
        "sourceNodeIds",
        "sourceTagIds",
        "sourceTags",
        "tagMappings",
        "nodeMappings",
    }
    if forbidden_sources.intersection(config):
        raise JdmRuleError("JDM_L2_INPUT_REQUIRED")
    try:
        required_inputs(content)
    except JdmRuntimeError as error:
        raise JdmRuleError(error.code) from error

    actions = [
        *content.get("actions", ()),
        *config.get("actions", ()),
    ]
    if not all(
        isinstance(action, dict) and action.get("type") == "control"
        for action in actions
    ):
        raise JdmRuleError("JDM_ACTION_UNSUPPORTED")
    return validate_rule_entity_references(content, catalog)


async def _apply_jdm_change(repository, runtime, operation):
    base_revision = await asyncio.to_thread(repository.current_revision)
    gate = runtime.data_trunk.configuration_gate
    await asyncio.to_thread(gate.begin_configuration_publish, base_revision)
    try:
        result = await asyncio.to_thread(operation, base_revision)
    except Exception:
        gate.cancel_configuration_publish()
        raise
    await runtime.reload_rules_now()
    await asyncio.to_thread(gate.reconcile_configuration_runtime)
    return result


@router.get("/rules", **protected(CONFIGURATION_READ))
async def list_rules(
    enabled: bool | None = Query(None),
    repository=Depends(get_jdm_rules),
) -> dict[str, Any]:
    try:
        rows = await asyncio.to_thread(repository.list, enabled)
        return {"rules": [_serialize_rule(row) for row in rows]}
    except Exception as error:
        _raise_jdm_http(error)


@router.post("/rules/evaluate", **protected(CONFIGURATION_WRITE))
async def evaluate_rule(request: EvaluateRequest) -> dict[str, Any]:
    """Evaluate an editor draft through the production model adapter only."""
    evaluation = await asyncio.to_thread(
        evaluate_model_content,
        request.content,
        request.context,
    )
    return {"context": request.context, "evaluation": evaluation}


@router.post("/rules", **protected(CONFIGURATION_WRITE))
async def create_rule(
    request: RuleCreateRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    catalog: EntityInstanceCatalog = Depends(get_entity_instance_catalog),
    repository=Depends(get_jdm_rules),
    runtime=Depends(get_jdm_runtime),
) -> dict[str, Any]:
    try:
        _require_runtime_type(request.rule_type)
        references = _validate_content(request.jdm_content, catalog)
        row = await _apply_jdm_change(
            repository,
            runtime,
            lambda base_revision: repository.create(
                name=request.name,
                rule_type=request.rule_type,
                jdm_content=request.jdm_content,
                enabled=request.enabled,
                references=references,
                actor=principal.actor,
                base_revision=base_revision,
            ),
        )
        return _serialize_rule(row)
    except Exception as error:
        _raise_jdm_http(error)


@router.get("/rules/{rule_id}", **protected(CONFIGURATION_READ))
async def get_rule(
    rule_id: UUID,
    repository=Depends(get_jdm_rules),
) -> dict[str, Any]:
    try:
        row = await asyncio.to_thread(repository.get, rule_id)
        if row is None:
            from app.services.jdm_postgres import JdmRuleError

            raise JdmRuleError("JDM_RULE_NOT_FOUND")
        return _serialize_rule(row)
    except Exception as error:
        _raise_jdm_http(error)


@router.put("/rules/{rule_id}", **protected(CONFIGURATION_WRITE))
async def update_rule(
    rule_id: UUID,
    request: RuleUpdateRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    catalog: EntityInstanceCatalog = Depends(get_entity_instance_catalog),
    repository=Depends(get_jdm_rules),
    runtime=Depends(get_jdm_runtime),
) -> dict[str, Any]:
    try:
        current = await asyncio.to_thread(repository.get, rule_id)
        if current is None:
            from app.services.jdm_postgres import JdmRuleError

            raise JdmRuleError("JDM_RULE_NOT_FOUND")
        if current["rule_type"] not in {"control", "linkage"}:
            from app.services.jdm_postgres import JdmRuleError

            raise JdmRuleError("JDM_RULE_LEGACY_READ_ONLY")
        changes = request.model_dump(exclude_none=True)
        if not changes:
            return _serialize_rule(current)
        rule_type = str(changes.get("rule_type", current["rule_type"]))
        _require_runtime_type(rule_type)
        content = changes.get("jdm_content", current["jdm_content"])
        references = (
            _validate_content(content, catalog)
            if "jdm_content" in changes
            else None
        )
        row = await _apply_jdm_change(
            repository,
            runtime,
            lambda base_revision: repository.update(
                rule_id=rule_id,
                changes=changes,
                references=references,
                actor=principal.actor,
                base_revision=base_revision,
            ),
        )
        return _serialize_rule(row)
    except Exception as error:
        _raise_jdm_http(error)


@router.delete("/rules/{rule_id}", **protected(CONFIGURATION_WRITE))
async def delete_rule(
    rule_id: UUID,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_jdm_rules),
    runtime=Depends(get_jdm_runtime),
) -> dict[str, Any]:
    try:
        result = await _apply_jdm_change(
            repository,
            runtime,
            lambda base_revision: repository.delete(
                rule_id=rule_id,
                actor=principal.actor,
                base_revision=base_revision,
            ),
        )
        return _serialize(result)
    except Exception as error:
        _raise_jdm_http(error)


@router.post("/rules/{rule_id}/simulate", **protected(CONFIGURATION_WRITE))
async def simulate_rule(
    rule_id: UUID,
    request: SimulateRequest,
    repository=Depends(get_jdm_rules),
) -> dict[str, Any]:
    try:
        rule = await asyncio.to_thread(repository.get, rule_id)
        if rule is None:
            from app.services.jdm_postgres import JdmRuleError

            raise JdmRuleError("JDM_RULE_NOT_FOUND")
        _require_runtime_type(str(rule["rule_type"]))
        evaluation = await asyncio.to_thread(
            evaluate_model_content,
            rule["jdm_content"],
            request.context,
        )
        return {
            "rule_id": str(rule_id),
            "rule_name": rule["name"],
            "context": request.context,
            "evaluation": evaluation,
        }
    except Exception as error:
        _raise_jdm_http(error)


@router.get("/rules/{rule_id}/executions", **protected(RUNTIME_READ))
async def list_rule_executions(
    rule_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    repository=Depends(get_jdm_rules),
) -> dict[str, Any]:
    try:
        rows = await asyncio.to_thread(repository.executions, rule_id, limit)
        return {"executions": [_serialize(dict(row)) for row in rows]}
    except Exception as error:
        _raise_jdm_http(error)


__all__ = [
    "get_jdm_rules",
    "get_jdm_runtime",
    "router",
]
