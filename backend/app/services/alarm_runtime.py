"""统一告警状态机。

来源 Adapter 只提交 ``AlarmObservation``；它们不创建事件、计数、恢复或发送通知。
``AlarmRuntime`` 是告警生命周期的唯一写入协调者，外部接口仅保留 submit/acknowledge。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4


GOOD_QUALITY = 192
OPEN_STATES = frozenset({"pending", "active_unacknowledged", "active_acknowledged"})


class AlarmRuntimeError(ValueError):
    """Stable machine-readable failure from the alarm lifecycle."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AlarmDefinition:
    id: UUID
    asset_id: str
    version: str
    entity_instance_id: UUID
    entity_definition_id: str
    trigger: dict[str, Any]
    trigger_duration_seconds: float
    recovery: dict[str, Any]
    recovery_duration_seconds: float
    severity: str
    notification_throttle_seconds: float


@dataclass(frozen=True)
class AlarmObservation:
    definition_id: UUID
    entity_instance_id: UUID
    observed_at: datetime
    value: Any
    quality: int
    source_kind: str
    source_ref: str
    evidence: dict[str, Any]
    max_observation_gap_seconds: float | None = None


@dataclass(frozen=True)
class AcknowledgeAlarm:
    event_id: UUID
    actor: str
    acknowledged_at: datetime
    note: str | None = None


@dataclass(frozen=True)
class AlarmEvent:
    id: UUID
    definition_id: UUID
    definition_version: str
    entity_instance_id: UUID
    state: str
    severity: str
    pending_at: datetime
    active_at: datetime | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    acknowledgement_note: str | None = None
    recovery_candidate_since: datetime | None = None
    recovered_at: datetime | None = None
    first_observation: dict[str, Any] | None = None
    last_observation: dict[str, Any] | None = None
    recovery_observation: dict[str, Any] | None = None


@dataclass(frozen=True)
class AlarmEventPresentation:
    node_name: str
    entity_name: str
    alarm_name: str


@dataclass(frozen=True)
class AlarmTransition:
    event_id: UUID
    from_state: str | None
    to_state: str
    occurred_at: datetime
    code: str
    evidence: dict[str, Any] | None = None
    actor: str | None = None
    note: str | None = None
    audit_event_id: UUID | None = None


@dataclass(frozen=True)
class AlarmNotification:
    id: UUID
    event_id: UUID
    definition_id: UUID
    entity_instance_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class AlarmOutcome:
    event_id: UUID | None
    state: str
    transition: dict[str, str] | None
    code: str
    notification_created: bool
    audit_event_id: UUID | None = None


@dataclass(frozen=True)
class AlarmEvaluation:
    definition: AlarmDefinition
    observation: AlarmObservation


class AlarmDefinitionCatalog(Protocol):
    def get(self, definition_id: UUID) -> AlarmDefinition | None: ...

    def for_entity(self, entity_instance_id: UUID) -> tuple[AlarmDefinition, ...]: ...

    def all_definitions(self) -> tuple[AlarmDefinition, ...]: ...

    def all_versions(self) -> tuple[AlarmDefinition, ...]: ...


class AlarmRepository(Protocol):
    def transaction(self) -> Any: ...

    def lock_stream(self, definition_id: UUID, entity_instance_id: UUID) -> None: ...

    def begin_committed_frame(
        self,
        consumer_key: str,
        frame_id: UUID,
        frame_sequence: int,
        configuration_revision: int,
    ) -> bool: ...

    def find_open(
        self,
        definition_id: UUID,
        entity_instance_id: UUID,
    ) -> AlarmEvent | None: ...

    def get_event(self, event_id: UUID) -> AlarmEvent | None: ...

    def list_events(self) -> tuple[AlarmEvent, ...]: ...

    def transitions(self, event_id: UUID) -> tuple[AlarmTransition, ...]: ...

    def save_event(self, event: AlarmEvent) -> AlarmEvent: ...

    def append_transition(self, transition: AlarmTransition) -> UUID | None: ...

    def last_notification_at(
        self,
        definition_id: UUID,
        entity_instance_id: UUID,
    ) -> datetime | None: ...

    def enqueue_notification(self, notification: AlarmNotification) -> None: ...


class InMemoryAlarmDefinitionCatalog:
    """Fixed definition adapter for lifecycle and public API tests."""

    def __init__(self, definitions: tuple[AlarmDefinition, ...] = ()) -> None:
        self._definitions = {item.id: item for item in definitions}
        self._current_by_asset_entity = {
            (item.asset_id, item.entity_instance_id): item.id
            for item in definitions
        }

    def get(self, definition_id: UUID) -> AlarmDefinition | None:
        return self._definitions.get(definition_id)

    def for_entity(self, entity_instance_id: UUID) -> tuple[AlarmDefinition, ...]:
        return tuple(
            self._definitions[definition_id]
            for (asset_id, candidate_entity_id), definition_id
            in self._current_by_asset_entity.items()
            if candidate_entity_id == entity_instance_id
        )

    def all_definitions(self) -> tuple[AlarmDefinition, ...]:
        return tuple(
            self._definitions[definition_id]
            for definition_id in self._current_by_asset_entity.values()
        )

    def all_versions(self) -> tuple[AlarmDefinition, ...]:
        return tuple(self._definitions.values())

    def install_definitions(self, plan, transaction: Any | None = None) -> tuple[UUID, ...]:
        del transaction
        definitions = tuple(plan.definitions)
        for installed in definitions:
            self._definitions[installed.id] = AlarmDefinition(
                id=installed.id,
                asset_id=installed.asset_id,
                version=installed.version,
                entity_instance_id=installed.entity_instance_id,
                entity_definition_id=installed.entity_definition_id,
                trigger=dict(installed.trigger),
                trigger_duration_seconds=installed.trigger_duration_seconds,
                recovery=dict(installed.recovery),
                recovery_duration_seconds=installed.recovery_duration_seconds,
                severity=installed.severity,
                notification_throttle_seconds=installed.notification_throttle_seconds,
            )
            self._current_by_asset_entity[
                (installed.asset_id, installed.entity_instance_id)
            ] = installed.id
        return tuple(item.id for item in definitions)


class InMemoryAlarmRepository:
    """Persistent-shape adapter; it enforces one open event per definition/instance."""

    def __init__(self) -> None:
        self._events: dict[UUID, AlarmEvent] = {}
        self._transitions: list[AlarmTransition] = []
        self._notifications: list[AlarmNotification] = []
        self._consumed_frames: dict[tuple[str, UUID], tuple[int, int]] = {}

    @contextmanager
    def transaction(self):
        snapshot = (
            dict(self._events),
            list(self._transitions),
            list(self._notifications),
            dict(self._consumed_frames),
        )
        try:
            yield self
        except Exception:
            (
                self._events,
                self._transitions,
                self._notifications,
                self._consumed_frames,
            ) = snapshot
            raise

    def begin_committed_frame(
        self,
        consumer_key: str,
        frame_id: UUID,
        frame_sequence: int,
        configuration_revision: int,
    ) -> bool:
        key = (consumer_key, frame_id)
        if key in self._consumed_frames:
            return False
        if any(
            candidate_consumer == consumer_key and sequence == frame_sequence
            for (candidate_consumer, _), (sequence, _) in self._consumed_frames.items()
        ):
            raise AlarmRuntimeError(
                "ALARM_FRAME_SEQUENCE_CONFLICT",
                "Alarm consumer frame sequence belongs to another frame",
            )
        self._consumed_frames[key] = (frame_sequence, configuration_revision)
        return True

    def has_consumed_frame(self, consumer_key: str, frame_id: UUID) -> bool:
        return (consumer_key, frame_id) in self._consumed_frames

    def find_open(
        self,
        definition_id: UUID,
        entity_instance_id: UUID,
    ) -> AlarmEvent | None:
        events = [
            item
            for item in self._events.values()
            if item.definition_id == definition_id
            and item.entity_instance_id == entity_instance_id
            and item.state in OPEN_STATES
        ]
        if len(events) > 1:
            raise AlarmRuntimeError(
                "ALARM_EVENT_INTEGRITY_ERROR",
                "Alarm definition and entity instance have more than one open event",
            )
        return events[0] if events else None

    def lock_stream(self, definition_id: UUID, entity_instance_id: UUID) -> None:
        del definition_id, entity_instance_id

    def get_event(self, event_id: UUID) -> AlarmEvent | None:
        return self._events.get(event_id)

    def list_events(self) -> tuple[AlarmEvent, ...]:
        return tuple(
            sorted(
                self._events.values(),
                key=lambda event: (event.pending_at, str(event.id)),
                reverse=True,
            )
        )

    def transitions(self, event_id: UUID) -> tuple[AlarmTransition, ...]:
        return tuple(
            item for item in self._transitions if item.event_id == event_id
        )

    def save_event(self, event: AlarmEvent) -> AlarmEvent:
        current = self.find_open(event.definition_id, event.entity_instance_id)
        if current is not None and current.id != event.id:
            raise AlarmRuntimeError(
                "ALARM_EVENT_INTEGRITY_ERROR",
                "A second open alarm event is not allowed",
            )
        self._events[event.id] = event
        return event

    def append_transition(self, transition: AlarmTransition) -> UUID:
        audit_event_id = transition.audit_event_id or uuid4()
        self._transitions.append(replace(transition, audit_event_id=audit_event_id))
        return audit_event_id

    def last_notification_at(
        self,
        definition_id: UUID,
        entity_instance_id: UUID,
    ) -> datetime | None:
        values = [
            item.created_at
            for item in self._notifications
            if item.definition_id == definition_id
            and item.entity_instance_id == entity_instance_id
        ]
        return max(values) if values else None

    def enqueue_notification(self, notification: AlarmNotification) -> None:
        self._notifications.append(notification)

    def active_events(self) -> tuple[AlarmEvent, ...]:
        return tuple(item for item in self._events.values() if item.state in OPEN_STATES)

    def notifications(self) -> tuple[AlarmNotification, ...]:
        return tuple(self._notifications)


class AlarmRuntime:
    """Own the full lifecycle for one definition/entity-instance observation stream."""

    def __init__(
        self,
        definitions: AlarmDefinitionCatalog,
        repository: AlarmRepository,
    ) -> None:
        self._definitions = definitions
        self._repository = repository

    def submit(self, observation: AlarmObservation) -> AlarmOutcome:
        definition = self._definition_for(observation)
        with self._repository.transaction() as repository:
            return self._submit_with_definition(repository, definition, observation)

    def submit_frame(
        self,
        *,
        frame_id: UUID,
        frame_sequence: int,
        configuration_revision: int,
        evaluations: tuple[AlarmEvaluation, ...],
    ) -> tuple[AlarmOutcome, ...]:
        with self._repository.transaction() as repository:
            if not repository.begin_committed_frame(
                "alarm",
                frame_id,
                frame_sequence,
                configuration_revision,
            ):
                return ()
            return tuple(
                self._submit_with_definition(
                    repository,
                    evaluation.definition,
                    evaluation.observation,
                )
                for evaluation in evaluations
            )

    def _submit_with_definition(
        self,
        repository: AlarmRepository,
        definition: AlarmDefinition,
        observation: AlarmObservation,
    ) -> AlarmOutcome:
        self._validate_definition_observation(definition, observation)
        observed_at = _utc(observation.observed_at)
        repository.lock_stream(
            observation.definition_id,
            observation.entity_instance_id,
        )
        event = repository.find_open(
            observation.definition_id,
            observation.entity_instance_id,
        )
        condition = (
            match_alarm_condition(definition.trigger, observation.value)
            and observation.quality == GOOD_QUALITY
        )

        if event is None:
            if not condition:
                return AlarmOutcome(None, "normal", None, "ALARM_NORMAL", False)
            pending = AlarmEvent(
                id=uuid4(),
                definition_id=definition.id,
                definition_version=definition.version,
                entity_instance_id=definition.entity_instance_id,
                state="pending",
                severity=definition.severity,
                pending_at=observed_at,
                first_observation=_evidence(observation),
                last_observation=_evidence(observation),
            )
            repository.save_event(pending)
            repository.append_transition(
                AlarmTransition(
                    pending.id,
                    None,
                    "pending",
                    observed_at,
                    "ALARM_TRIGGER_PENDING",
                    _evidence(observation),
                )
            )
            if definition.trigger_duration_seconds <= 0:
                active = replace(
                    pending,
                    state="active_unacknowledged",
                    active_at=observed_at,
                )
                repository.save_event(active)
                repository.append_transition(
                    AlarmTransition(
                        active.id,
                        "pending",
                        active.state,
                        observed_at,
                        "ALARM_ACTIVATED",
                        _evidence(observation),
                    )
                )
                notified = self._notify_if_allowed(
                    repository,
                    active,
                    definition,
                    observed_at,
                )
                return AlarmOutcome(
                    active.id,
                    active.state,
                    {"from": "pending", "to": active.state},
                    "ALARM_ACTIVATED",
                    notified,
                )
            return AlarmOutcome(
                pending.id,
                pending.state,
                None,
                "ALARM_TRIGGER_PENDING",
                False,
            )

        if event.state == "pending":
            return self._advance_pending(
                repository,
                event,
                definition,
                observation,
                condition,
            )
        return self._advance_active(repository, event, definition, observation)

    def acknowledge(self, command: AcknowledgeAlarm) -> AlarmOutcome:
        with self._repository.transaction() as repository:
            event = repository.get_event(command.event_id)
            if event is None:
                raise AlarmRuntimeError("ALARM_EVENT_NOT_FOUND", "Alarm event was not found")
            if event.state == "active_acknowledged":
                return AlarmOutcome(
                    event.id,
                    event.state,
                    None,
                    "ALARM_ALREADY_ACKNOWLEDGED",
                    False,
                )
            if event.state != "active_unacknowledged":
                raise AlarmRuntimeError(
                    "ALARM_ACKNOWLEDGE_NOT_ALLOWED",
                    "Only an active unacknowledged alarm can be acknowledged",
                )
            occurred_at = _utc(command.acknowledged_at)
            updated = replace(
                event,
                state="active_acknowledged",
                acknowledged_at=occurred_at,
                acknowledged_by=command.actor,
                acknowledgement_note=command.note,
            )
            repository.save_event(updated)
            audit_event_id = repository.append_transition(
                AlarmTransition(
                    event.id,
                    event.state,
                    updated.state,
                    occurred_at,
                    "ALARM_ACKNOWLEDGED",
                    actor=command.actor,
                    note=command.note,
                )
            )
            return AlarmOutcome(
                updated.id,
                updated.state,
                {"from": event.state, "to": updated.state},
                "ALARM_ACKNOWLEDGED",
                False,
                audit_event_id,
            )

    def get(self, event_id: UUID) -> AlarmEvent:
        event = self._repository.get_event(event_id)
        if event is None:
            raise AlarmRuntimeError("ALARM_EVENT_NOT_FOUND", "Alarm event was not found")
        return event

    def list(self) -> tuple[AlarmEvent, ...]:
        return self._repository.list_events()

    def describe(
        self,
        events: tuple[AlarmEvent, ...],
    ) -> dict[UUID, AlarmEventPresentation]:
        repository_describe = getattr(self._repository, "describe_events", None)
        described = dict(repository_describe(events)) if callable(repository_describe) else {}
        for event in events:
            if event.id in described:
                continue
            definition = self._definitions.get(event.definition_id)
            observation = event.last_observation or event.first_observation or {}
            evidence = observation.get("evidence") if isinstance(observation, dict) else {}
            if not isinstance(evidence, dict):
                evidence = {}
            node_name = evidence.get("node_name") or evidence.get("node_id") or "未命名节点"
            entity_name = evidence.get("entity_name") or (
                definition.entity_definition_id if definition is not None else "未命名实体"
            )
            alarm_name = evidence.get("alarm_name") or (
                definition.asset_id if definition is not None else "未命名告警"
            )
            described[event.id] = AlarmEventPresentation(
                str(node_name),
                str(entity_name),
                str(alarm_name),
            )
        return described

    def timeline(self, event_id: UUID) -> tuple[AlarmTransition, ...]:
        self.get(event_id)
        return self._repository.transitions(event_id)

    def _advance_pending(
        self,
        repository: AlarmRepository,
        event: AlarmEvent,
        definition: AlarmDefinition,
        observation: AlarmObservation,
        condition: bool,
    ) -> AlarmOutcome:
        observed_at = _utc(observation.observed_at)
        evidence = _evidence(observation)
        # A pending trigger has the same continuity requirement as recovery:
        # samples separated by a freshness gap cannot prove a sustained fault.
        # In particular, do not activate merely because the next good sample
        # arrives after the configured trigger duration.
        if not condition or _has_continuity_gap(event, observation):
            closed = replace(event, state="normal", last_observation=evidence)
            repository.save_event(closed)
            repository.append_transition(
                AlarmTransition(
                    event.id,
                    event.state,
                    "normal",
                    observed_at,
                    "ALARM_TRIGGER_CLEARED",
                    evidence,
                )
            )
            return AlarmOutcome(
                event.id,
                "normal",
                {"from": event.state, "to": "normal"},
                "ALARM_TRIGGER_CLEARED",
                False,
            )
        updated = replace(event, last_observation=evidence)
        if observed_at < event.pending_at + timedelta(seconds=definition.trigger_duration_seconds):
            repository.save_event(updated)
            return AlarmOutcome(event.id, "pending", None, "ALARM_TRIGGER_PENDING", False)
        active = replace(updated, state="active_unacknowledged", active_at=observed_at)
        repository.save_event(active)
        repository.append_transition(
            AlarmTransition(
                event.id,
                event.state,
                active.state,
                observed_at,
                "ALARM_ACTIVATED",
                evidence,
            )
        )
        notified = self._notify_if_allowed(repository, active, definition, observed_at)
        return AlarmOutcome(
            active.id,
            active.state,
            {"from": event.state, "to": active.state},
            "ALARM_ACTIVATED",
            notified,
        )

    def _advance_active(
        self,
        repository: AlarmRepository,
        event: AlarmEvent,
        definition: AlarmDefinition,
        observation: AlarmObservation,
    ) -> AlarmOutcome:
        observed_at = _utc(observation.observed_at)
        evidence = _evidence(observation)
        recovery_condition = (
            match_alarm_condition(definition.recovery, observation.value)
            and observation.quality == GOOD_QUALITY
            and not _has_continuity_gap(event, observation)
        )
        if not recovery_condition:
            updated = replace(
                event,
                last_observation=evidence,
                recovery_candidate_since=None,
            )
            repository.save_event(updated)
            return AlarmOutcome(event.id, event.state, None, "ALARM_STILL_ACTIVE", False)
        candidate_since = event.recovery_candidate_since or observed_at
        updated = replace(
            event,
            last_observation=evidence,
            recovery_candidate_since=candidate_since,
        )
        if observed_at < candidate_since + timedelta(seconds=definition.recovery_duration_seconds):
            repository.save_event(updated)
            return AlarmOutcome(event.id, event.state, None, "ALARM_RECOVERY_PENDING", False)
        recovered = replace(
            updated,
            state="recovered",
            recovered_at=observed_at,
            recovery_observation=evidence,
        )
        repository.save_event(recovered)
        repository.append_transition(
            AlarmTransition(
                event.id,
                event.state,
                recovered.state,
                observed_at,
                "ALARM_RECOVERED",
                evidence,
            )
        )
        return AlarmOutcome(
            event.id,
            recovered.state,
            {"from": event.state, "to": recovered.state},
            "ALARM_RECOVERED",
            False,
        )

    def _notify_if_allowed(
        self,
        repository: AlarmRepository,
        event: AlarmEvent,
        definition: AlarmDefinition,
        occurred_at: datetime,
    ) -> bool:
        last = repository.last_notification_at(
            definition.id,
            definition.entity_instance_id,
        )
        if last is not None and occurred_at < last + timedelta(
            seconds=definition.notification_throttle_seconds
        ):
            return False
        repository.enqueue_notification(
            AlarmNotification(
                uuid4(),
                event.id,
                definition.id,
                definition.entity_instance_id,
                occurred_at,
            )
        )
        return True

    def _definition_for(self, observation: AlarmObservation) -> AlarmDefinition:
        definition = self._definitions.get(observation.definition_id)
        if definition is None:
            raise AlarmRuntimeError("ALARM_DEFINITION_NOT_FOUND", "Alarm definition was not found")
        self._validate_definition_observation(definition, observation)
        return definition

    @staticmethod
    def _validate_definition_observation(
        definition: AlarmDefinition,
        observation: AlarmObservation,
    ) -> None:
        if definition.entity_instance_id != observation.entity_instance_id:
            raise AlarmRuntimeError(
                "ALARM_ENTITY_INSTANCE_MISMATCH",
                "Alarm observation does not match the definition entity instance",
            )
        if definition.id != observation.definition_id:
            raise AlarmRuntimeError(
                "ALARM_DEFINITION_MISMATCH",
                "Alarm observation does not match the supplied definition",
            )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _evidence(observation: AlarmObservation) -> dict[str, Any]:
    return {
        "observed_at": _utc(observation.observed_at).isoformat(),
        "value": observation.value,
        "quality": observation.quality,
        "source_kind": observation.source_kind,
        "source_ref": observation.source_ref,
        "evidence": dict(observation.evidence),
        "max_observation_gap_seconds": observation.max_observation_gap_seconds,
    }


def _has_continuity_gap(event: AlarmEvent, observation: AlarmObservation) -> bool:
    """Reject recovery when the source was unobserved past its freshness window."""
    window = observation.max_observation_gap_seconds
    if window is None or window <= 0 or event.last_observation is None:
        return False
    previous_at = event.last_observation.get("observed_at")
    if not isinstance(previous_at, str):
        return True
    try:
        previous = _utc(datetime.fromisoformat(previous_at))
    except ValueError:
        return True
    return _utc(observation.observed_at) > previous + timedelta(seconds=window)


def match_alarm_condition(condition: dict[str, Any], value: Any) -> bool:
    operator = condition.get("op", condition.get("operator"))
    expected = condition.get("value")
    if operator == "eq":
        return value == expected
    if operator == "ne":
        return value != expected
    if operator in {"gt", "gte", "lt", "lte"}:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        if not isinstance(expected, (int, float)) or isinstance(expected, bool):
            return False
        return {
            "gt": value > expected,
            "gte": value >= expected,
            "lt": value < expected,
            "lte": value <= expected,
        }[operator]
    if operator in {"contains", "not_contains"}:
        if not isinstance(expected, str) or not expected:
            return False
        if not isinstance(value, (list, tuple, frozenset)):
            return False
        if not all(isinstance(item, str) for item in value):
            return False
        matched = expected in value
        return matched if operator == "contains" else not matched
    return False
