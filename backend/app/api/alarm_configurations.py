"""L2-only alarm configuration HTTP API."""
from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import CONFIGURATION_READ, CONFIGURATION_WRITE, capability_metadata, principal_for, protected
from app.services.alarm_configuration import AlarmConfiguration, AlarmConfigurationError, AlarmConfigurationPlan, AlarmConfigurationPlanItem, AlarmRule, AlarmRuleSetRevision, ApplyAlarmConfigurationPlan, EntitySelection, PlanAlarmConfiguration
from app.services.identity import Principal


class AlarmConfigurationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def stable_validation_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": {"code": "ALARM_CONFIGURATION_REQUEST_INVALID", "message": "Alarm configuration request is invalid"}})
        return stable_validation_handler


router = APIRouter(route_class=AlarmConfigurationRoute)
_configuration: AlarmConfiguration | None = None


def get_alarm_configuration() -> AlarmConfiguration:
    global _configuration
    if _configuration is None:
        from app.services.alarm_configuration_postgres import (
            build_postgres_alarm_configuration,
        )
        _configuration = build_postgres_alarm_configuration()
    return _configuration


class AlarmConditionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains"]
    value: float | int | bool | str


class AlarmRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    severity: Literal["CRITICAL", "MAJOR", "WARNING", "INFO"]
    trigger: AlarmConditionRequest
    trigger_duration_seconds: float = Field(ge=0)
    recovery: AlarmConditionRequest
    recovery_duration_seconds: float = Field(ge=0)
    notification_throttle_seconds: float = Field(ge=0)
    unit: str | None = Field(default=None, max_length=100)
    fault_map_id: UUID | None = None

    def domain(self) -> AlarmRule:
        return AlarmRule(**self.model_dump(exclude={"trigger", "recovery"}), trigger=self.trigger.model_dump(), recovery=self.recovery.model_dump())


class CreateRuleSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    rules: list[AlarmRuleRequest] = Field(default_factory=list)


class CreateRuleSetRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[AlarmRuleRequest] = Field(default_factory=list)


class EntitySelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_instance_ids: list[UUID] = Field(default_factory=list, max_length=200)
    node_ids: list[UUID] = Field(default_factory=list, max_length=200)
    entity_definition_ids: list[str] = Field(default_factory=list, max_length=200)

    def domain(self) -> EntitySelection:
        return EntitySelection(tuple(self.entity_instance_ids), tuple(self.node_ids), tuple(self.entity_definition_ids))


class CreatePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: EntitySelectionRequest
    rule_set_id: UUID
    rule_set_revision: int = Field(ge=1)


class ApplyPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Field(pattern="^[0-9a-f]{64}$")


class TrialAlarmRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_instance_id: UUID
    rule: AlarmRuleRequest
    value: Any
    quality: int = Field(ge=0, le=255)


def _rule(rule: AlarmRule) -> dict[str, Any]:
    return {"id": rule.id, "name": rule.name, "severity": rule.severity, "trigger": dict(rule.trigger), "trigger_duration_seconds": rule.trigger_duration_seconds, "recovery": dict(rule.recovery), "recovery_duration_seconds": rule.recovery_duration_seconds, "notification_throttle_seconds": rule.notification_throttle_seconds, "unit": rule.unit, "fault_map_id": str(rule.fault_map_id) if rule.fault_map_id else None}


def _revision(revision: AlarmRuleSetRevision) -> dict[str, Any]:
    return {"rule_set_id": str(revision.rule_set_id), "key": revision.key, "name": revision.name, "revision": revision.revision, "rules": [_rule(rule) for rule in revision.rules], "digest": revision.digest}


def _item(item: AlarmConfigurationPlanItem) -> dict[str, Any]:
    return {"definition_key": item.definition_key, "entity_instance_id": str(item.entity_instance_id), "rule_id": item.rule_id, "action": item.action, "before": item.before, "after": item.after, "blockers": list(item.blockers)}


def _plan(plan: AlarmConfigurationPlan) -> dict[str, Any]:
    return {"id": str(plan.id), "base_configuration_revision": plan.base_configuration_revision, "rule_set_revision": _revision(plan.rule_set_revision), "status": plan.status, "items": [_item(item) for item in plan.items], "blockers": list(plan.blockers), "digest": plan.digest}


def _error(error: AlarmConfigurationError) -> HTTPException:
    code = str(error)
    response_status = 422
    if code in {"ALARM_PLAN_NOT_FOUND", "ALARM_RULE_SET_NOT_FOUND"}:
        response_status = 404
    elif code in {"ALARM_PLAN_STALE", "ALARM_PLAN_DIGEST_MISMATCH", "ALARM_PLAN_BLOCKED", "IDEMPOTENCY_KEY_REUSED", "ALARM_ENTITY_UNRESOLVED"}:
        response_status = 409
    elif code.endswith("_PERSISTENCE_FAILED") or code.endswith("_PERSISTENCE_UNAVAILABLE"):
        response_status = 503
    return HTTPException(status_code=response_status, detail={"code": code, "message": code})


@router.get("/alarm-configurations", **protected(CONFIGURATION_READ))
async def current_alarm_configuration(configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        context = configuration.repository.current_configuration()
    except AlarmConfigurationError as error:
        raise _error(error) from error
    return {"configuration_revision": context["configuration_revision"], "definitions": [{key: value for key, value in record.items() if key not in {"id", "payload"}} for _, record in sorted(context["definitions"].items())]}


@router.post("/alarm-configurations/trials", **protected(CONFIGURATION_READ))
async def trial_alarm_rule(body: TrialAlarmRuleRequest, configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        result = configuration.trial(
            entity_instance_id=body.entity_instance_id,
            rule=body.rule.domain(),
            value=body.value,
            quality=body.quality,
        )
    except AlarmConfigurationError as error:
        raise _error(error) from error
    return {
        "entity_instance_id": str(result.entity_instance_id),
        "trigger_matches": result.trigger_matches,
        "recovery_matches": result.recovery_matches,
        "description": result.description,
    }


@router.get("/alarm-rule-sets", **protected(CONFIGURATION_READ))
async def list_alarm_rule_sets(configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        return {"items": [_revision(item) for item in configuration.list_rule_set_revisions()]}
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.get("/alarm-rule-groups", **protected(CONFIGURATION_READ))
async def list_alarm_rule_groups(configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        items = configuration.list_rule_groups()
    except AlarmConfigurationError as error:
        raise _error(error) from error
    return {
        "items": [
            {
                "rule_set_id": str(item.rule_set_id),
                "key": item.key,
                "name": item.name,
                "latest_revision": item.latest_revision,
                "last_non_empty_revision": item.last_non_empty_revision,
                "entity_instance_ids": [str(value) for value in item.entity_instance_ids],
                "enabled_entity_instance_ids": [str(value) for value in item.enabled_entity_instance_ids],
                "device_count": item.device_count,
                "rule_count": item.rule_count,
                "highest_severity": item.highest_severity,
            }
            for item in items
        ]
    }


@router.post("/alarm-rule-sets", status_code=201, openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def create_alarm_rule_set(body: CreateRuleSetRequest, principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)), configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        return _revision(configuration.create_rule_set(key=body.key, name=body.name, rules=tuple(rule.domain() for rule in body.rules), actor=principal.actor))
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.post("/alarm-rule-sets/{rule_set_id}/revisions", status_code=201, openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def create_alarm_rule_set_revision(rule_set_id: UUID, body: CreateRuleSetRevisionRequest, principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)), configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        return _revision(configuration.create_rule_set_revision(rule_set_id=rule_set_id, rules=tuple(rule.domain() for rule in body.rules), actor=principal.actor))
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.post("/alarm-configuration-plans", status_code=201, openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def create_alarm_configuration_plan(body: CreatePlanRequest, principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)), configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        return _plan(configuration.plan(PlanAlarmConfiguration(body.selection.domain(), body.rule_set_id, body.rule_set_revision, principal.actor)))
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.get("/alarm-configuration-plans/{plan_id}", **protected(CONFIGURATION_READ))
async def get_alarm_configuration_plan(plan_id: UUID, configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    plan = configuration.repository.get_plan(plan_id)
    if plan is None:
        raise _error(AlarmConfigurationError("ALARM_PLAN_NOT_FOUND"))
    return _plan(plan)


@router.post("/alarm-configuration-plans/{plan_id}/apply", openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def apply_alarm_configuration_plan(plan_id: UUID, body: ApplyPlanRequest, idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200), principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)), configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            configuration.apply,
            ApplyAlarmConfigurationPlan(
                plan_id,
                body.plan_digest,
                idempotency_key,
                principal.actor,
            ),
        )
        return {"id": str(result.id), "plan_id": str(result.plan_id), "configuration_revision": result.configuration_revision, "definition_ids": [str(value) for value in result.definition_ids], "audit_event_id": str(result.audit_event_id), "applied_at": result.applied_at.isoformat()}
    except AlarmConfigurationError as error:
        raise _error(error) from error
