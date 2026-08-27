"""Adapt one committed L2 frame to the unified alarm lifecycle."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from app.services.alarm_definition_dispatch import AlarmDefinitionDispatcher
from app.services.alarm_runtime import (
    AlarmDefinition,
    AlarmDefinitionCatalog,
    AlarmEvaluation,
    AlarmObservation,
    AlarmRuntime,
    AlarmRuntimeError,
)
from app.services.data_trunk_contracts import ValueKind
from app.services.data_trunk_outbox import CommittedL2Change, FrameOutboxEvent


class CommittedL2AlarmConsumer:
    """Consume only terminal-frame L2 facts, never L0 or mutable latest state."""

    def __init__(
        self,
        definitions: AlarmDefinitionCatalog,
        runtime: AlarmRuntime,
    ) -> None:
        self._runtime = runtime
        self._dispatcher = AlarmDefinitionDispatcher(definitions, runtime)

    async def publish(self, event: FrameOutboxEvent) -> None:
        await asyncio.to_thread(self._consume, event)

    def _consume(self, event: FrameOutboxEvent) -> None:
        entity_ids = frozenset(
            change.entity_instance_id for change in event.l2_changes
        )
        definitions = self._dispatcher.for_entities(entity_ids)
        evaluations = tuple(
            _evaluation(event, change, definition)
            for change in event.l2_changes
            for definition in definitions.get(change.entity_instance_id, ())
        )
        self._runtime.submit_frame(
            frame_id=event.frame_id,
            frame_sequence=event.frame_sequence,
            configuration_revision=event.configuration_revision,
            evaluations=evaluations,
        )


def _evaluation(
    event: FrameOutboxEvent,
    change: CommittedL2Change,
    definition: AlarmDefinition,
) -> AlarmEvaluation:
    if change.observed_at is None:
        raise AlarmRuntimeError(
            "ALARM_FRAME_OBSERVATION_INVALID",
            "Committed L2 alarm observation requires observed_at",
        )
    observation = AlarmObservation(
        definition_id=definition.id,
        entity_instance_id=change.entity_instance_id,
        observed_at=change.observed_at,
        value=_alarm_value(change),
        quality=int(change.quality),
        source_kind="committed_l2",
        source_ref=(
            f"frame:{event.frame_id}/entity:{change.entity_instance_id}"
        ),
        evidence={
            "frame_id": str(event.frame_id),
            "frame_sequence": event.frame_sequence,
            "configuration_revision": event.configuration_revision,
            "l2_event_id": str(change.event_id),
            "node_id": None if change.node_id is None else str(change.node_id),
            "unit": change.unit,
            "reason": change.reason,
            "processing_revision_id": (
                None
                if change.processing_revision_id is None
                else str(change.processing_revision_id)
            ),
            "source_digest": change.source_digest,
        },
    )
    return AlarmEvaluation(definition, observation)


def _alarm_value(change: CommittedL2Change) -> Any:
    value = change.value.value
    if isinstance(value, Decimal):
        if change.value.kind is ValueKind.INT:
            return int(value)
        return float(value)
    return value


def build_postgres_committed_l2_alarm_consumer() -> CommittedL2AlarmConsumer:
    from app.services.alarm_postgres import (
        PostgresAlarmDefinitionCatalog,
        PostgresAlarmRepository,
    )

    definitions = PostgresAlarmDefinitionCatalog()
    return CommittedL2AlarmConsumer(
        definitions,
        AlarmRuntime(definitions, PostgresAlarmRepository()),
    )


__all__ = [
    "CommittedL2AlarmConsumer",
    "build_postgres_committed_l2_alarm_consumer",
]
