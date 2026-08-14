"""只从确认主绑定读取实体实例观测。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from app.services.entity_instance_registry import (
    EntityInstanceError,
    EntityInstanceRegistry,
    SourceCatalog,
)
from app.models.schemas import RawMessage
from app.services.normalizer import normalize
from app.services.parser import parse_neuron_json


@dataclass(frozen=True)
class SourceObservation:
    tag_id: UUID
    observed_at: datetime
    value: Any
    quality: int


class ObservationCatalog(Protocol):
    def latest(self, tag_id: UUID) -> SourceObservation | None: ...


class InMemoryObservationCatalog:
    def __init__(self) -> None:
        self._observations: dict[UUID, SourceObservation] = {}

    def publish(self, observation: SourceObservation) -> None:
        current = self._observations.get(observation.tag_id)
        if current is None or observation.observed_at >= current.observed_at:
            self._observations[observation.tag_id] = observation

    def latest(self, tag_id: UUID) -> SourceObservation | None:
        return self._observations.get(tag_id)


class InMemoryNeuronProtocolSimulator:
    """Publish Neuron-shaped protocol payloads through parser/normalizer seams."""

    def __init__(
        self,
        sources: SourceCatalog,
        observations: InMemoryObservationCatalog,
    ) -> None:
        self._sources = sources
        self._observations = observations

    def publish(self, *, topic: str, payload: bytes, quality: int = 192) -> int:
        parsed = parse_neuron_json(RawMessage(topic=topic, payload=payload))
        if parsed is None:
            return 0
        normalized = normalize(parsed)
        published = 0
        for point in normalized.points:
            candidates = tuple(
                source
                for source in self._sources.list_sources()
                if source.enabled
                and source.device_key == parsed.node_name
                and source.tag_name.casefold() == point.tag_name.casefold()
            )
            if len(candidates) != 1:
                continue
            self._observations.publish(
                SourceObservation(
                    tag_id=candidates[0].tag_id,
                    observed_at=point.ts,
                    value=point.value,
                    quality=quality,
                )
            )
            published += 1
        return published


@dataclass(frozen=True)
class EntityInstanceObservation:
    entity_instance_id: UUID
    definition_id: str
    instance_key: str
    value: Any
    data_type: str
    unit: str | None
    observed_at: datetime
    quality: int
    age_ms: int
    fresh: bool
    quality_good: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "entity_instance_id": str(self.entity_instance_id),
            "definition_id": self.definition_id,
            "instance_key": self.instance_key,
            "value": self.value,
            "data_type": self.data_type,
            "unit": self.unit,
            "observed_at": self.observed_at.isoformat(),
            "quality": self.quality,
            "age_ms": self.age_ms,
            "fresh": self.fresh,
            "quality_good": self.quality_good,
        }


class EntityInstanceRuntime:
    """运行读取不包含候选或旧优先级回退。"""

    def __init__(
        self,
        registry: EntityInstanceRegistry,
        observations: ObservationCatalog,
    ) -> None:
        self._registry = registry
        self._observations = observations

    def read(self, entity_instance_id: UUID) -> EntityInstanceObservation:
        observation = self.read_for_alarm(entity_instance_id)
        if not observation.fresh:
            raise EntityInstanceError(
                "ENTITY_DATA_STALE",
                "Confirmed entity source observation is stale",
            )
        if not observation.quality_good:
            raise EntityInstanceError(
                "ENTITY_DATA_QUALITY_BAD",
                "Confirmed entity source quality is not good",
            )
        return observation

    def read_for_alarm(self, entity_instance_id: UUID) -> EntityInstanceObservation:
        """Read the confirmed source without hiding bad/stale samples from alarms.

        The normal read seam rejects unavailable data for callers that need a
        usable engineering value.  The alarm seam must instead observe every
        sample from the confirmed source so an invalid sample resets a pending
        recovery interval rather than bridging a data-quality gap.
        """
        resolved = self._registry.resolve(entity_instance_id)
        observation = self._observations.latest(resolved.tag_id)
        if observation is None:
            raise EntityInstanceError(
                "ENTITY_DATA_MISSING",
                "Confirmed entity source has no observation",
            )
        observed_at = observation.observed_at
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds(),
        )
        return EntityInstanceObservation(
            entity_instance_id=resolved.entity_instance_id,
            definition_id=resolved.definition_id,
            instance_key=resolved.instance_key,
            value=observation.value,
            data_type=resolved.data_type,
            unit=resolved.unit,
            observed_at=observed_at,
            quality=observation.quality,
            age_ms=round(age_seconds * 1000),
            fresh=age_seconds <= resolved.freshness_seconds,
            quality_good=observation.quality == 192,
        )
