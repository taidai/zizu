"""Read-only acceptance evidence for applied alarm configurations."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.services.alarm_configuration import (
    AlarmConfigurationPlanItem,
    AppliedAlarmConfiguration,
)
from app.services.alarm_runtime import AlarmEvent, AlarmTransition


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
    ) -> AlarmConfigurationAcceptanceReport: ...

    def get(self, report_id: UUID) -> AlarmConfigurationAcceptanceReport | None: ...

    def find_idempotency(
        self, application_id: UUID, actor: str, idempotency_key: str,
    ) -> AlarmConfigurationAcceptanceReport | None: ...

    def latest_passed_item(
        self, definition_id: UUID,
    ) -> tuple[AlarmConfigurationAcceptanceReport, AlarmConfigurationAcceptanceItem] | None: ...


class InMemoryAlarmConfigurationAcceptanceRepository:
    def __init__(self) -> None:
        self._reports: dict[UUID, AlarmConfigurationAcceptanceReport] = {}
        self._idempotency: dict[tuple[UUID, str, str], UUID] = {}

    def save(
        self,
        report: AlarmConfigurationAcceptanceReport,
        *,
        idempotency_key: str,
    ) -> AlarmConfigurationAcceptanceReport:
        self._reports[report.id] = report
        self._idempotency[(report.application_id, report.actor, idempotency_key)] = report.id
        return report

    def get(self, report_id: UUID) -> AlarmConfigurationAcceptanceReport | None:
        return self._reports.get(report_id)

    def find_idempotency(
        self, application_id: UUID, actor: str, idempotency_key: str,
    ) -> AlarmConfigurationAcceptanceReport | None:
        report_id = self._idempotency.get((application_id, actor, idempotency_key))
        return None if report_id is None else self._reports[report_id]

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
    ) -> AlarmConfigurationAcceptanceReport:
        if not command.actor.strip() or not command.idempotency_key.strip():
            raise ValueError("ALARM_ACCEPTANCE_COMMAND_INVALID")
        existing = self._repository.find_idempotency(
            command.application_id, command.actor, command.idempotency_key,
        )
        if existing is not None:
            return existing

        started_at = datetime.now(timezone.utc)
        events = {event.definition_id: event for event in self._runtime.list()}
        items = tuple(
            self._classify(definition_id, item, events.get(definition_id))
            for definition_id, item in zip(applied.definition_ids, applied.items, strict=True)
        )
        finished_at = datetime.now(timezone.utc)
        status = "passed" if all(item.status == "passed" for item in items) else "failed"
        digest = _digest({
            "application_id": command.application_id,
            "installation_id": applied.installation_id,
            "site_configuration_version": applied.site_configuration_version,
            "actor": command.actor,
            "idempotency_key": command.idempotency_key,
            "status": status,
            "items": items,
        })
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
            digest=digest,
        )
        return self._repository.save(report, idempotency_key=command.idempotency_key)

    def get(self, report_id: UUID) -> AlarmConfigurationAcceptanceReport:
        report = self._repository.get(report_id)
        if report is None:
            raise ValueError("ALARM_ACCEPTANCE_REPORT_NOT_FOUND")
        return report

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
                evidence={**previous.evidence, "prior_report_id": str(report.id)},
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
        if acknowledgement is None:
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
        evidence={} if event is None else {"pending_at": event.pending_at.isoformat()},
    )


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
