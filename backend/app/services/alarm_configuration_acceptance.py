"""Read-only acceptance evidence for applied alarm configurations."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.services.alarm_configuration import (
    AlarmConfigurationPlanItem,
    AppliedAlarmConfiguration,
)
from app.services.alarm_runtime import AlarmEvent, AlarmTransition


class AlarmConfigurationAcceptanceError(ValueError):
    pass


class _FrozenDict(dict[str, Any]):
    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("mapping is immutable")
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in deepcopy(value).items()})
    if isinstance(value, list): return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple): return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class RunAlarmConfigurationAcceptance:
    application_id: UUID
    actor: str
    idempotency_key: str


@dataclass(frozen=True)
class AlarmConfigurationAcceptanceItem:
    definition_id: UUID
    definition_key: str
    action: str
    status: str
    code: str
    event_id: UUID | None
    event_state: str | None
    transition_codes: tuple[str, ...]
    acknowledgement_audit_event_id: UUID | None
    evidence: dict[str, Any]


@dataclass(frozen=True)
class AlarmConfigurationAcceptanceReport:
    id: UUID
    application_id: UUID
    installation_id: UUID
    site_configuration_version: int
    actor: str
    status: str
    items: tuple[AlarmConfigurationAcceptanceItem, ...]
    started_at: datetime
    finished_at: datetime
    digest: str


@dataclass(frozen=True)
class AlarmConfigurationAcceptanceProgressItem:
    definition_id: UUID
    entity_instance_id: UUID
    action: str
    rule_name: str
    stage: str
    code: str
    event_id: UUID | None
    event_state: str | None
    transition_codes: tuple[str, ...]
    acknowledgement_audit_event_id: UUID | None


@dataclass(frozen=True)
class AlarmConfigurationAcceptanceProgress:
    application_id: UUID
    site_configuration_version: int
    applied_at: datetime
    ready_to_report: bool
    report_id: UUID | None
    report_status: str | None
    report_digest: str | None
    items: tuple[AlarmConfigurationAcceptanceProgressItem, ...]


class AlarmConfigurationAcceptanceRuntime(Protocol):
    """The observer receives only the AlarmRuntime query surface."""

    def list(self) -> tuple[AlarmEvent, ...]: ...

    def timeline(self, event_id: UUID) -> tuple[AlarmTransition, ...]: ...


class AlarmConfigurationAcceptanceRepository(Protocol):
    def save(
        self,
        report: AlarmConfigurationAcceptanceReport,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AlarmConfigurationAcceptanceReport: ...

    def get(self, report_id: UUID) -> AlarmConfigurationAcceptanceReport | None: ...

    def find_idempotency(self, actor: str, idempotency_key: str) -> tuple[str, AlarmConfigurationAcceptanceReport] | None: ...

    def latest_passed_item(
        self, definition_id: UUID,
    ) -> tuple[AlarmConfigurationAcceptanceReport, AlarmConfigurationAcceptanceItem] | None: ...

    def latest_for_application(
        self, application_id: UUID,
    ) -> AlarmConfigurationAcceptanceReport | None: ...


class InMemoryAlarmConfigurationAcceptanceRepository:
    def __init__(self) -> None:
        self._reports: dict[UUID, AlarmConfigurationAcceptanceReport] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, UUID]] = {}

    def save(
        self,
        report: AlarmConfigurationAcceptanceReport,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AlarmConfigurationAcceptanceReport:
        if report.id in self._reports:
            raise AlarmConfigurationAcceptanceError("ALARM_ACCEPTANCE_REPORT_EXISTS")
        self._reports[report.id] = report
        self._idempotency[(report.actor, idempotency_key)] = (request_digest, report.id)
        return report

    def get(self, report_id: UUID) -> AlarmConfigurationAcceptanceReport | None:
        return self._reports.get(report_id)

    def find_idempotency(self, actor: str, idempotency_key: str) -> tuple[str, AlarmConfigurationAcceptanceReport] | None:
        stored = self._idempotency.get((actor, idempotency_key))
        return None if stored is None else (stored[0], self._reports[stored[1]])

    def latest_passed_item(
        self, definition_id: UUID,
    ) -> tuple[AlarmConfigurationAcceptanceReport, AlarmConfigurationAcceptanceItem] | None:
        candidates = (
            (report, item)
            for report in self._reports.values()
            if report.status == "passed"
            for item in report.items
            if item.definition_id == definition_id and item.status == "passed"
        )
        return max(candidates, key=lambda value: (value[0].finished_at, str(value[0].id)), default=None)

    def latest_for_application(
        self, application_id: UUID,
    ) -> AlarmConfigurationAcceptanceReport | None:
        return max(
            (
                report
                for report in self._reports.values()
                if report.application_id == application_id
            ),
            key=lambda report: (report.finished_at, str(report.id)),
            default=None,
        )


class AlarmConfigurationAcceptance:
    """Classifies installed definitions from existing lifecycle evidence only."""

    def __init__(
        self,
        *,
        runtime: AlarmConfigurationAcceptanceRuntime,
        repository: AlarmConfigurationAcceptanceRepository,
    ) -> None:
        self._runtime = runtime
        self._repository = repository

    def run(
        self,
        command: RunAlarmConfigurationAcceptance,
        applied: AppliedAlarmConfiguration,
        *,
        latest_application_id: UUID | None = None,
    ) -> AlarmConfigurationAcceptanceReport:
        if not command.actor.strip() or not command.idempotency_key.strip():
            raise AlarmConfigurationAcceptanceError("ALARM_ACCEPTANCE_COMMAND_INVALID")
        if command.application_id != applied.id:
            raise AlarmConfigurationAcceptanceError("ALARM_ACCEPTANCE_APPLICATION_MISMATCH")
        self._validate_applied(applied)
        request_digest = _digest({"application_id": command.application_id, "actor": command.actor, "idempotency_key": command.idempotency_key, "applied": applied})
        existing = self._repository.find_idempotency(command.actor, command.idempotency_key)
        if existing is not None:
            existing_digest, existing_report = existing
            if existing_digest != request_digest:
                raise AlarmConfigurationAcceptanceError("ALARM_ACCEPTANCE_IDEMPOTENCY_KEY_REUSED")
            return existing_report
        if latest_application_id is not None and applied.id != latest_application_id:
            raise AlarmConfigurationAcceptanceError(
                "ALARM_ACCEPTANCE_APPLICATION_STALE"
            )

        started_at = datetime.now(timezone.utc)
        events = {event.definition_id: event for event in self._runtime.list()}
        items = tuple(
            self._classify(definition_id, item, events.get(definition_id))
            for definition_id, item in zip(applied.definition_ids, applied.items, strict=True)
        )
        finished_at = datetime.now(timezone.utc)
        status = "passed" if all(item.status == "passed" for item in items) else "failed"
        report = AlarmConfigurationAcceptanceReport(
            id=uuid4(),
            application_id=command.application_id,
            installation_id=applied.installation_id,
            site_configuration_version=applied.site_configuration_version,
            actor=command.actor,
            status=status,
            items=items,
            started_at=started_at,
            finished_at=finished_at,
            digest="",
        )
        report = replace(report, digest=_digest(_report_payload(report)))
        return self._repository.save(report, idempotency_key=command.idempotency_key, request_digest=request_digest)

    def progress(
        self,
        applied: AppliedAlarmConfiguration,
    ) -> AlarmConfigurationAcceptanceProgress:
        """Classify the latest evidence without creating a report."""
        self._validate_applied(applied)
        events = {event.definition_id: event for event in self._runtime.list()}
        classified = tuple(
            self._classify(definition_id, item, events.get(definition_id))
            for definition_id, item in zip(
                applied.definition_ids, applied.items, strict=True,
            )
        )
        items = tuple(
            AlarmConfigurationAcceptanceProgressItem(
                definition_id=result.definition_id,
                entity_instance_id=plan_item.entity_instance_id,
                action=result.action,
                rule_name=_rule_name(plan_item),
                stage=_progress_stage(result),
                code=result.code,
                event_id=result.event_id,
                event_state=result.event_state,
                transition_codes=result.transition_codes,
                acknowledgement_audit_event_id=result.acknowledgement_audit_event_id,
            )
            for plan_item, result in zip(applied.items, classified, strict=True)
        )
        report = self._repository.latest_for_application(applied.id)
        return AlarmConfigurationAcceptanceProgress(
            application_id=applied.id,
            site_configuration_version=applied.site_configuration_version,
            applied_at=applied.applied_at,
            ready_to_report=all(item.status == "passed" for item in classified),
            report_id=None if report is None else report.id,
            report_status=None if report is None else report.status,
            report_digest=None if report is None else report.digest,
            items=items,
        )

    def get(self, report_id: UUID) -> AlarmConfigurationAcceptanceReport:
        report = self._repository.get(report_id)
        if report is None:
            raise AlarmConfigurationAcceptanceError("ALARM_ACCEPTANCE_REPORT_NOT_FOUND")
        return report

    @staticmethod
    def _validate_applied(applied: AppliedAlarmConfiguration) -> None:
        if not applied.definition_ids or len(applied.definition_ids) != len(applied.items):
            raise AlarmConfigurationAcceptanceError("ALARM_ACCEPTANCE_APPLIED_ITEMS_INVALID")

    def _classify(
        self,
        definition_id: UUID,
        item: AlarmConfigurationPlanItem,
        event: AlarmEvent | None,
    ) -> AlarmConfigurationAcceptanceItem:
        if item.action == "preserve":
            prior = self._repository.latest_passed_item(definition_id)
            if prior is None:
                return _item(definition_id, item, "failed", "ALARM_ACCEPTANCE_PRESERVE_EVIDENCE_MISSING")
            report, previous = prior
            return AlarmConfigurationAcceptanceItem(
                definition_id=definition_id,
                definition_key=item.definition_key,
                action=item.action,
                status="passed",
                code="ALARM_ACCEPTANCE_PRESERVED",
                event_id=previous.event_id,
                event_state=previous.event_state,
                transition_codes=previous.transition_codes,
                acknowledgement_audit_event_id=previous.acknowledgement_audit_event_id,
                evidence=_freeze({**previous.evidence, "prior_report_id": str(report.id)}),
            )
        if event is None:
            return _item(definition_id, item, "failed", "ALARM_ACCEPTANCE_EVENT_MISSING")

        transitions = self._runtime.timeline(event.id)
        required = {"ALARM_ACTIVATED", "ALARM_ACKNOWLEDGED", "ALARM_RECOVERED"}
        transition_codes = tuple(
            transition.code for transition in transitions
            if transition.code in required
        )
        acknowledgement = next(
            (transition for transition in transitions if transition.code == "ALARM_ACKNOWLEDGED"),
            None,
        )
        if acknowledgement is None or acknowledgement.audit_event_id is None:
            return _item(
                definition_id, item, "failed", "ALARM_ACCEPTANCE_ACKNOWLEDGEMENT_MISSING",
                event=event, transition_codes=transition_codes,
            )
        if event.state != "recovered":
            return _item(
                definition_id, item, "failed", "ALARM_ACCEPTANCE_RECOVERY_MISSING",
                event=event, transition_codes=transition_codes,
                acknowledgement_audit_event_id=acknowledgement.audit_event_id,
            )
        if not required.issubset(transition_codes):
            return _item(
                definition_id, item, "failed", "ALARM_ACCEPTANCE_TIMELINE_INCOMPLETE",
                event=event, transition_codes=transition_codes,
                acknowledgement_audit_event_id=acknowledgement.audit_event_id,
            )
        return _item(
            definition_id, item, "passed", "ALARM_ACCEPTANCE_PASSED",
            event=event, transition_codes=transition_codes,
            acknowledgement_audit_event_id=acknowledgement.audit_event_id,
        )


def _item(
    definition_id: UUID,
    plan_item: AlarmConfigurationPlanItem,
    status: str,
    code: str,
    *,
    event: AlarmEvent | None = None,
    transition_codes: tuple[str, ...] = (),
    acknowledgement_audit_event_id: UUID | None = None,
) -> AlarmConfigurationAcceptanceItem:
    return AlarmConfigurationAcceptanceItem(
        definition_id=definition_id,
        definition_key=plan_item.definition_key,
        action=plan_item.action,
        status=status,
        code=code,
        event_id=None if event is None else event.id,
        event_state=None if event is None else event.state,
        transition_codes=transition_codes,
        acknowledgement_audit_event_id=acknowledgement_audit_event_id,
        evidence=_freeze({} if event is None else {"pending_at": event.pending_at.isoformat()}),
    )


def _rule_name(item: AlarmConfigurationPlanItem) -> str:
    payload = item.after or item.before or {}
    rule = payload.get("rule") if isinstance(payload, dict) else None
    name = rule.get("name") if isinstance(rule, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else item.rule_id


def _progress_stage(item: AlarmConfigurationAcceptanceItem) -> str:
    if item.status == "passed":
        return "passed"
    codes = set(item.transition_codes)
    if "ALARM_ACTIVATED" not in codes:
        return "waiting_trigger"
    if item.acknowledgement_audit_event_id is None:
        return "waiting_acknowledgement"
    return "waiting_recovery"


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _digest(value: Any) -> str:
    canonical = json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _report_payload(report: AlarmConfigurationAcceptanceReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "application_id": report.application_id,
        "installation_id": report.installation_id,
        "site_configuration_version": report.site_configuration_version,
        "actor": report.actor,
        "status": report.status,
        "items": report.items,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
    }
