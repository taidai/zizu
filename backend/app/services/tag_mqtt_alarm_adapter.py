"""Tag and MQTT inputs adapted to the ADR-0004 alarm observation interface."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Mapping, Protocol
from uuid import UUID

from app.services.alarm_definition_dispatch import AlarmDefinitionDispatcher
from app.services.alarm_runtime import (
    AlarmDefinitionCatalog,
    AlarmObservation,
    AlarmOutcome,
    AlarmRuntime,
)


ERROR_LEVELS = frozenset({"error1", "error2", "error3"})
_NESTED_CONTAINER_KEYS = frozenset({"values", "tags", "data", "metrics", "payload"})


@dataclass(frozen=True)
class TagAlarmSource:
    tag_id: UUID
    entity_instance_id: UUID
    tag_name: str
    max_observation_gap_seconds: float | None


@dataclass(frozen=True)
class TagAlarmSample:
    tag_id: UUID
    observed_at: datetime
    value: Any
    quality: int


class TagAlarmSourceResolver(Protocol):
    def resolve(self, tag_id: UUID) -> TagAlarmSource | None: ...


class InMemoryTagAlarmSourceResolver:
    """Reloadable production cache and deterministic test adapter."""

    def __init__(self, sources: Mapping[UUID, TagAlarmSource] | None = None) -> None:
        self._sources = dict(sources or {})

    def replace(self, sources: Mapping[UUID, TagAlarmSource]) -> None:
        self._sources = dict(sources)

    def resolve(self, tag_id: UUID) -> TagAlarmSource | None:
        return self._sources.get(tag_id)


class TagAlarmAdapter:
    """Adapt confirmed physical tag observations without owning a lifecycle."""

    def __init__(
        self,
        definitions: AlarmDefinitionCatalog,
        alarm_runtime: AlarmRuntime,
        sources: TagAlarmSourceResolver,
    ) -> None:
        self._runtime = alarm_runtime
        self._sources = sources
        self._definitions = AlarmDefinitionDispatcher(definitions, alarm_runtime)

    def submit(self, sample: TagAlarmSample) -> tuple[AlarmOutcome, ...]:
        return self._submit(
            sample,
            source_kind="tag",
            source_ref=str(sample.tag_id),
            evidence={},
        )

    def submit_mqtt(
        self,
        sample: TagAlarmSample,
        *,
        topic: str,
        source_key: str,
        external_id: str,
    ) -> tuple[AlarmOutcome, ...]:
        return self._submit(
            sample,
            source_kind="mqtt",
            source_ref=f"{topic}#{source_key}:{external_id}",
            evidence={
                "mqtt_topic": topic,
                "source_key": source_key,
                "external_id": external_id,
                "source_quality": sample.quality,
            },
        )

    def _submit(
        self,
        sample: TagAlarmSample,
        *,
        source_kind: str,
        source_ref: str,
        evidence: Mapping[str, Any],
    ) -> tuple[AlarmOutcome, ...]:
        source = self._sources.resolve(sample.tag_id)
        if source is None:
            return ()
        outcomes: list[AlarmOutcome] = []
        for definition in self._definitions.for_entity(source.entity_instance_id):
            outcomes.append(
                self._runtime.submit(
                    AlarmObservation(
                        definition_id=definition.id,
                        entity_instance_id=source.entity_instance_id,
                        observed_at=sample.observed_at,
                        value=sample.value,
                        quality=sample.quality,
                        source_kind=source_kind,
                        source_ref=source_ref,
                        evidence={
                            "tag_id": str(source.tag_id),
                            "tag_name": source.tag_name,
                            **dict(evidence),
                        },
                        max_observation_gap_seconds=(
                            source.max_observation_gap_seconds
                        ),
                    )
                )
            )
        return tuple(outcomes)


class MqttAlarmAdapter:
    """Decode configured MQTT fault groups, then delegate every lifecycle decision."""

    def __init__(
        self,
        tag_adapter: TagAlarmAdapter,
        tag_ids_by_external_id: Mapping[str, UUID] | None = None,
    ) -> None:
        self._tag_adapter = tag_adapter
        self._tag_ids_by_external_id = dict(tag_ids_by_external_id or {})

    def replace_tag_ids(self, tag_ids_by_external_id: Mapping[str, UUID]) -> None:
        self._tag_ids_by_external_id = dict(tag_ids_by_external_id)

    def submit(
        self,
        topic: str,
        payload: bytes,
        observed_at: datetime,
    ) -> tuple[AlarmOutcome, ...]:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ()
        if not isinstance(decoded, dict):
            return ()
        quality = decoded.get("quality")
        if not isinstance(quality, int) or isinstance(quality, bool):
            quality = 0
        outcomes: list[AlarmOutcome] = []
        for source_key, external_id, value in _error_items(decoded):
            tag_id = self._tag_ids_by_external_id.get(external_id)
            if tag_id is None:
                continue
            outcomes.extend(
                self._tag_adapter.submit_mqtt(
                    TagAlarmSample(tag_id, observed_at, value, quality),
                    topic=topic,
                    source_key=source_key,
                    external_id=external_id,
                )
            )
        return tuple(outcomes)


def _error_items(data: dict[str, Any], depth: int = 0) -> tuple[tuple[str, str, Any], ...]:
    if depth > 2:
        return ()
    items: list[tuple[str, str, Any]] = []
    for key, value in data.items():
        if key in ERROR_LEVELS:
            if isinstance(value, dict):
                items.extend((key, str(external_id), item) for external_id, item in value.items())
            elif isinstance(value, list):
                items.extend((key, str(item), 1) for item in value if item not in (None, ""))
            else:
                items.append((key, "", value))
        elif key in _NESTED_CONTAINER_KEYS and isinstance(value, dict):
            items.extend(_error_items(value, depth + 1))
    return tuple(items)
