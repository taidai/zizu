"""公开的统一告警配置 HTTP 适配器。"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import CONFIGURATION_READ, CONFIGURATION_WRITE, RUNTIME_READ, capability_metadata, principal_for, protected
from app.services.alarm_configuration import AlarmConfiguration, AlarmConfigurationError, AlarmConfigurationPlan, AlarmConfigurationPlanItem, AlarmRule, AlarmRuleSetRevision, ApplyAlarmConfigurationPlan, EntitySelection, LegacyAlarmMigrationCandidate, LegacyAlarmMigrationPlan, PlanAlarmConfiguration
from app.services.alarm_configuration_acceptance import AlarmConfigurationAcceptanceError, AlarmConfigurationAcceptanceProgress, AlarmConfigurationAcceptanceReport, RunAlarmConfigurationAcceptance
from app.services.identity import Principal


class AlarmConfigurationRoute(APIRoute):
    """Give only this router a stable validation-error envelope."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def stable_validation_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "ALARM_CONFIGURATION_REQUEST_INVALID",
                            "message": "Alarm configuration request is invalid",
                        }
                    },
                )

        return stable_validation_handler


router = APIRouter(route_class=AlarmConfigurationRoute)
_configuration: AlarmConfiguration | None = None
_configuration_acceptance: Any | None = None


def get_alarm_configuration() -> AlarmConfiguration:
    """Resolve production persistence while retaining the public test seam."""
    global _configuration
    if _configuration is None:
        from app.services.alarm_configuration_postgres import PostgresAlarmConfigurationRepository
        _configuration = AlarmConfiguration(PostgresAlarmConfigurationRepository())
    return _configuration


def get_alarm_configuration_acceptance() -> Any:
    """Resolve the one-transaction PostgreSQL acceptance application service."""
    global _configuration_acceptance
    if _configuration_acceptance is None:
        from app.services.alarm_configuration_acceptance_postgres import (
            PostgresAlarmConfigurationAcceptance,
        )

        _configuration_acceptance = PostgresAlarmConfigurationAcceptance()
    return _configuration_acceptance


class AlarmConditionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    value: float


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
        return AlarmRule(
            **self.model_dump(exclude={"trigger", "recovery"}),
            trigger=self.trigger.model_dump(),
            recovery=self.recovery.model_dump(),
        )


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
    device_instance_ids: list[UUID] = Field(default_factory=list, max_length=200)
    entity_definition_ids: list[str] = Field(default_factory=list, max_length=200)

    def domain(self) -> EntitySelection:
        return EntitySelection(
            entity_instance_ids=tuple(self.entity_instance_ids),
            device_instance_ids=tuple(self.device_instance_ids),
            entity_definition_ids=tuple(self.entity_definition_ids),
        )


class CreatePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: UUID
    selection: EntitySelectionRequest
    rule_set_id: UUID
    rule_set_revision: int = Field(ge=1)


class ApplyPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Field(pattern="^[0-9a-f]{64}$")


class LegacyMigrationSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: Literal["tag_alarm", "entity_alarm_binding"]
    source_key: str = Field(min_length=1, max_length=200)
    entity_instance_id: UUID


class LegacyMigrationPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: UUID
    selections: list[LegacyMigrationSelectionRequest] = Field(
        default_factory=list,
        max_length=2000,
    )


def _rule(rule: AlarmRule) -> dict[str, Any]:
    return {"id": rule.id, "name": rule.name, "severity": rule.severity, "trigger": dict(rule.trigger), "trigger_duration_seconds": rule.trigger_duration_seconds, "recovery": dict(rule.recovery), "recovery_duration_seconds": rule.recovery_duration_seconds, "notification_throttle_seconds": rule.notification_throttle_seconds, "unit": rule.unit, "fault_map_id": str(rule.fault_map_id) if rule.fault_map_id else None}


def _revision(revision: AlarmRuleSetRevision) -> dict[str, Any]:
    return {"rule_set_id": str(revision.rule_set_id), "key": revision.key, "name": revision.name, "revision": revision.revision, "rules": [_rule(rule) for rule in revision.rules], "digest": revision.digest}


def _item(item: AlarmConfigurationPlanItem) -> dict[str, Any]:
    return {"definition_key": item.definition_key, "entity_instance_id": str(item.entity_instance_id), "rule_id": item.rule_id, "action": item.action, "before": item.before, "after": item.after, "blockers": list(item.blockers)}


def _plan(plan: AlarmConfigurationPlan) -> dict[str, Any]:
    return {"id": str(plan.id), "installation_id": str(plan.installation_id), "base_site_configuration_version": plan.base_site_configuration_version, "rule_set_revision": _revision(plan.rule_set_revision), "status": plan.status, "items": [_item(item) for item in plan.items], "blockers": list(plan.blockers), "digest": plan.digest}


def _legacy_candidate(candidate: LegacyAlarmMigrationCandidate) -> dict[str, Any]:
    return {
        "source_kind": candidate.source_kind,
        "source_key": candidate.source_key,
        "display_name": candidate.display_name,
        "status": candidate.status,
        "severity": candidate.severity,
        "entity_instance_id": (
            str(candidate.entity_instance_id)
            if candidate.entity_instance_id is not None
            else None
        ),
        "entity_instance_candidates": [
            str(value) for value in candidate.entity_instance_candidates
        ],
        "blockers": list(candidate.blockers),
        "target_definition_ids": [
            str(value) for value in candidate.target_definition_ids
        ],
        "proposed_rules": [
            {
                "entity_instance_id": str(item.entity_instance_id),
                "display_name": item.display_name,
                "blockers": list(item.blockers),
                "proposed_definitions": [
                    {
                        "name": definition.name,
                        "severity": definition.severity,
                        "trigger": None if definition.trigger is None else {
                            "operator": definition.trigger["op"],
                            "value": definition.trigger["value"],
                        },
                        "trigger_duration_seconds": definition.trigger_duration_seconds,
                        "recovery": None if definition.recovery is None else {
                            "operator": definition.recovery["op"],
                            "value": definition.recovery["value"],
                        },
                        "recovery_duration_seconds": definition.recovery_duration_seconds,
                        "notification_throttle_seconds": definition.notification_throttle_seconds,
                        "blockers": list(definition.blockers),
                    }
                    for definition in item.proposed_definitions
                ],
            }
            for item in candidate.proposed_rules
        ],
    }


def _legacy_plan(plan: LegacyAlarmMigrationPlan) -> dict[str, Any]:
    return {
        "installation_id": str(plan.installation_id),
        "status": plan.status,
        "items": [_legacy_candidate(item) for item in plan.items],
        "blockers": list(plan.blockers),
        "digest": plan.digest,
        "target_definition_ids": [
            str(value) for value in plan.target_definition_ids
        ],
    }


def _acceptance_report(report: AlarmConfigurationAcceptanceReport) -> dict[str, Any]:
    return {
        "id": str(report.id),
        "application_id": str(report.application_id),
        "installation_id": str(report.installation_id),
        "site_configuration_version": report.site_configuration_version,
        "actor": report.actor,
        "status": report.status,
        "items": [
            {
                "definition_id": str(item.definition_id),
                "definition_key": item.definition_key,
                "action": item.action,
                "status": item.status,
                "code": item.code,
                "event_id": None if item.event_id is None else str(item.event_id),
                "event_state": item.event_state,
                "transition_codes": list(item.transition_codes),
                "acknowledgement_audit_event_id": (
                    None
                    if item.acknowledgement_audit_event_id is None
                    else str(item.acknowledgement_audit_event_id)
                ),
                "evidence": dict(item.evidence),
            }
            for item in report.items
        ],
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "digest": report.digest,
    }


def _acceptance_progress(progress: AlarmConfigurationAcceptanceProgress) -> dict[str, Any]:
    return {
        "application_id": str(progress.application_id),
        "site_configuration_version": progress.site_configuration_version,
        "applied_at": progress.applied_at.isoformat(),
        "ready_to_report": progress.ready_to_report,
        "report_id": None if progress.report_id is None else str(progress.report_id),
        "report_status": progress.report_status,
        "report_digest": progress.report_digest,
        "items": [
            {
                "definition_id": str(item.definition_id),
                "entity_instance_id": str(item.entity_instance_id),
                "action": item.action,
                "rule_name": item.rule_name,
                "stage": item.stage,
                "code": item.code,
                "event_id": None if item.event_id is None else str(item.event_id),
                "event_state": item.event_state,
                "transition_codes": list(item.transition_codes),
                "acknowledgement_audit_event_id": (
                    None
                    if item.acknowledgement_audit_event_id is None
                    else str(item.acknowledgement_audit_event_id)
                ),
            }
            for item in progress.items
        ],
    }


def _error(error: AlarmConfigurationError) -> HTTPException:
    raw_code = str(error)
    code = {"ALARM_AUDIT_FAILED": "AUDIT_UNAVAILABLE", "rule ids must be unique": "ALARM_RULE_CONFLICT", "rule id must be non-empty": "ALARM_RULE_CONFLICT", "rule count must not exceed 20": "ALARM_BATCH_LIMIT_EXCEEDED", "entity count must not exceed 200": "ALARM_BATCH_LIMIT_EXCEEDED", "expanded definition count must not exceed 2000": "ALARM_BATCH_LIMIT_EXCEEDED", "rule set revision not found": "ALARM_RULE_SET_NOT_FOUND"}.get(raw_code, raw_code)
    response_status = 422
    if code in {"ALARM_PLAN_NOT_FOUND", "ALARM_RULE_SET_NOT_FOUND"}:
        response_status = status.HTTP_404_NOT_FOUND
    elif code in {"ALARM_PLAN_STALE", "ALARM_PLAN_DIGEST_MISMATCH", "ALARM_PLAN_BLOCKED", "IDEMPOTENCY_KEY_REUSED", "ALARM_MIGRATION_AMBIGUOUS", "ALARM_ENTITY_UNRESOLVED", "ALARM_FAULT_MAP_UNRESOLVED", "ALARM_MIGRATION_SELECTION_INVALID", "ALARM_MIGRATION_INSTALLATION_STALE", "ALARM_MIGRATION_PLAN_STALE", "ALARM_MIGRATION_PLAN_BLOCKED"}:
        response_status = status.HTTP_409_CONFLICT
    elif (
        code == "AUDIT_UNAVAILABLE"
        or code.endswith("_PERSISTENCE_FAILED")
        or code.endswith("_PERSISTENCE_UNAVAILABLE")
    ):
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(status_code=response_status, detail={"code": code, "message": raw_code})


def _acceptance_error(error: AlarmConfigurationAcceptanceError) -> HTTPException:
    raw_code = str(error)
    code = {
        "ALARM_ACCEPTANCE_IDEMPOTENCY_KEY_REUSED": "IDEMPOTENCY_KEY_REUSED",
    }.get(raw_code, raw_code)
    response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    if code in {
        "ALARM_ACCEPTANCE_APPLICATION_NOT_FOUND",
        "ALARM_ACCEPTANCE_REPORT_NOT_FOUND",
    }:
        response_status = status.HTTP_404_NOT_FOUND
    elif code == "IDEMPOTENCY_KEY_REUSED":
        response_status = status.HTTP_409_CONFLICT
    elif code.endswith("_PERSISTENCE_FAILED") or code.endswith(
        "_PERSISTENCE_UNAVAILABLE"
    ):
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(
        status_code=response_status,
        detail={"code": code, "message": raw_code},
    )


@router.get("/alarm-configurations", **protected(CONFIGURATION_READ))
async def current_alarm_configuration(configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        context = configuration.repository.current_site_context()
    except AlarmConfigurationError as error:
        raise _error(error) from error
    return {"site_configuration_version": context["site_configuration_version"], "definitions": [
        {
            "entity_display_name": record["entity_display_name"],
            "rule_name": record["rule_name"],
            "severity": record["severity"],
            "trigger": record["trigger"],
            "recovery": record["recovery"],
            "source": record["source"],
            "version_description": record["version_description"],
            "enabled": record["enabled"],
            "status": record["status"],
        }
        for _key, record in sorted(context["definitions"].items())
    ]}


@router.get("/alarm-rule-sets", **protected(CONFIGURATION_READ))
async def list_alarm_rule_sets(
    configuration: AlarmConfiguration = Depends(get_alarm_configuration),
) -> dict[str, list[dict[str, Any]]]:
    try:
        return {
            "items": [
                _revision(item) for item in configuration.list_rule_set_revisions()
            ]
        }
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.post("/alarm-rule-sets", status_code=status.HTTP_201_CREATED, openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def create_alarm_rule_set(body: CreateRuleSetRequest, principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)), configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        return _revision(configuration.create_rule_set(key=body.key, name=body.name, rules=tuple(rule.domain() for rule in body.rules), actor=principal.actor))
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.post("/alarm-rule-sets/{rule_set_id}/revisions", status_code=status.HTTP_201_CREATED, openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def create_alarm_rule_set_revision(rule_set_id: UUID, body: CreateRuleSetRevisionRequest, principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)), configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        return _revision(configuration.create_rule_set_revision(rule_set_id=rule_set_id, rules=tuple(rule.domain() for rule in body.rules), actor=principal.actor))
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.post("/alarm-configuration-plans", status_code=status.HTTP_201_CREATED, openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def create_alarm_configuration_plan(body: CreatePlanRequest, principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)), configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        return _plan(configuration.plan(PlanAlarmConfiguration(installation_id=body.installation_id, selection=body.selection.domain(), rule_set_id=body.rule_set_id, rule_set_revision=body.rule_set_revision, planned_by=principal.actor)))
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.get("/alarm-configuration-plans/{plan_id}", **protected(CONFIGURATION_READ))
async def get_alarm_configuration_plan(plan_id: UUID, configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        plan = configuration.repository.get_plan(plan_id)
    except AlarmConfigurationError as error:
        raise _error(error) from error
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "ALARM_PLAN_NOT_FOUND", "message": "Alarm configuration plan was not found"})
    return _plan(plan)


@router.post("/alarm-configuration-plans/{plan_id}/apply", openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def apply_alarm_configuration_plan(plan_id: UUID, body: ApplyPlanRequest, idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200), principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)), configuration: AlarmConfiguration = Depends(get_alarm_configuration)) -> dict[str, Any]:
    try:
        result = configuration.apply(ApplyAlarmConfigurationPlan(plan_id=plan_id, plan_digest=body.plan_digest, idempotency_key=idempotency_key, actor=principal.actor))
        return {"id": str(result.id), "plan_id": str(result.plan_id), "installation_id": str(result.installation_id), "site_configuration_version": result.site_configuration_version, "definition_ids": [str(definition_id) for definition_id in result.definition_ids], "audit_event_id": str(result.audit_event_id), "applied_at": result.applied_at.isoformat()}
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.post(
    "/alarm-configuration-applications/{application_id}/acceptance",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def run_alarm_configuration_acceptance(
    application_id: UUID,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
    ),
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    acceptance: Any = Depends(get_alarm_configuration_acceptance),
) -> dict[str, Any]:
    try:
        return _acceptance_report(acceptance.run(RunAlarmConfigurationAcceptance(
            application_id=application_id,
            actor=principal.actor,
            idempotency_key=idempotency_key,
        )))
    except AlarmConfigurationAcceptanceError as error:
        raise _acceptance_error(error) from error


@router.get(
    "/alarm-configuration-applications/latest/acceptance-progress",
    **protected(CONFIGURATION_READ),
)
async def get_latest_alarm_configuration_acceptance_progress(
    acceptance: Any = Depends(get_alarm_configuration_acceptance),
) -> dict[str, Any]:
    try:
        return _acceptance_progress(acceptance.progress())
    except AlarmConfigurationAcceptanceError as error:
        raise _acceptance_error(error) from error


@router.get(
    "/alarm-configuration-reports/{report_id}",
    **protected(RUNTIME_READ),
)
async def get_alarm_configuration_acceptance_report(
    report_id: UUID,
    acceptance: Any = Depends(get_alarm_configuration_acceptance),
) -> dict[str, Any]:
    try:
        return _acceptance_report(acceptance.get(report_id))
    except AlarmConfigurationAcceptanceError as error:
        raise _acceptance_error(error) from error


@router.get("/alarm-configuration-migrations/legacy", **protected(CONFIGURATION_READ))
async def list_legacy_alarm_configuration_migrations(
    configuration: AlarmConfiguration = Depends(get_alarm_configuration),
) -> dict[str, Any]:
    try:
        installation_id, items = configuration.preview_legacy_migration_snapshot()
        return {
            "installation_id": str(installation_id),
            "items": [_legacy_candidate(item) for item in items],
        }
    except AlarmConfigurationError as error:
        raise _error(error) from error


@router.post("/alarm-configuration-migrations/legacy/plans", openapi_extra=capability_metadata(CONFIGURATION_WRITE))
async def create_legacy_alarm_configuration_migration_plan(
    body: LegacyMigrationPlanRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    configuration: AlarmConfiguration = Depends(get_alarm_configuration),
) -> dict[str, Any]:
    keys = [(item.source_kind, item.source_key) for item in body.selections]
    if len(keys) != len(set(keys)):
        raise _error(AlarmConfigurationError("ALARM_MIGRATION_SELECTION_INVALID"))
    try:
        return _legacy_plan(
            configuration.apply_legacy_migration(
                installation_id=body.installation_id,
                selections={
                    (item.source_kind, item.source_key): item.entity_instance_id
                    for item in body.selections
                },
                actor=principal.actor,
            )
        )
    except AlarmConfigurationError as error:
        raise _error(error) from error
