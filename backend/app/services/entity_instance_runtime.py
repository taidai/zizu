"""只从确认主绑定读取实体实例观测。"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
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
from app.services.data_trunk import (
    DataTrunk,
    RawObservationAdapter,
    TagMetadata,
)
from app.services.normalizer import normalize
from app.services.parser import parse_neuron_json


@dataclass(frozen=True)
class SourceObservation:
    tag_id: UUID
    observed_at: datetime
    value: Any
    quality: int
    event_id: UUID | None = None
    reason: str | None = None
    received_at: datetime | None = None
    calculated_at: datetime | None = None
    processing_revision_id: UUID | None = None
    configuration_revision: int | None = None
    source_digest: str | None = None


class ObservationCatalog(Protocol):
    def latest(self, source) -> SourceObservation | None: ...

    def history(self, source, range_key: str) -> list[SourceObservation]: ...


class InMemoryObservationCatalog:
    def __init__(self) -> None:
        self._observations: dict[UUID, SourceObservation] = {}
        self._history: dict[UUID, list[SourceObservation]] = {}

    def publish(self, observation: SourceObservation) -> None:
        self._history.setdefault(observation.tag_id, []).append(observation)
        current = self._observations.get(observation.tag_id)
        if current is None or observation.observed_at >= current.observed_at:
            self._observations[observation.tag_id] = observation

    def latest(self, source) -> SourceObservation | None:
        source_id = source if isinstance(source, UUID) else source.source_id or source.control_tag_id
        return self._observations.get(source_id)

    def history(self, source, range_key: str) -> list[SourceObservation]:
        # The in-memory adapter intentionally keeps the public test seam small:
        # the caller still validates the range grammar at its HTTP boundary.
        source_id = source if isinstance(source, UUID) else source.source_id or source.control_tag_id
        return sorted(self._history.get(source_id, []), key=lambda item: item.observed_at)


class InMemoryNeuronProtocolSimulator:
    """Publish Neuron-shaped protocol payloads through parser/normalizer seams."""

    def __init__(
        self,
        sources: SourceCatalog,
        observations: InMemoryObservationCatalog,
        *,
        data_trunk: DataTrunk | None = None,
        point_tag_catalog: Mapping[str, TagMetadata] | None = None,
    ) -> None:
        self._sources = sources
        self._observations = observations
        self._data_trunk = data_trunk
        self._point_tag_catalog = dict(point_tag_catalog or {})
        self._raw_adapter = RawObservationAdapter()

    def publish(self, *, topic: str, payload: bytes, quality: int = 192) -> int:
        parsed = parse_neuron_json(RawMessage(topic=topic, payload=payload))
        if parsed is None:
            return 0
        trunk_published = 0
        if self._data_trunk is not None and self._point_tag_catalog:
            raw_observations = self._raw_adapter.from_parsed(
                parsed,
                self._point_tag_catalog,
                received_at=datetime.now(timezone.utc),
                source_message_id=hashlib.sha256(payload).hexdigest(),
                source_sequence=None,
            )
            if raw_observations:
                receipt = self._data_trunk.ingest(raw_observations)
                trunk_published = receipt.accepted_l0_count
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
        return published + trunk_published


@dataclass(frozen=True)
class EntityInstanceObservation:
    entity_instance_id: UUID
    definition_id: str
    node_id: UUID
    node_key: str
    value: Any
    data_type: str
    unit: str | None
    observed_at: datetime
    quality: int
    age_ms: int
    fresh: bool
    quality_good: bool
    max_observation_gap_seconds: float | None = None
    source_kind: str = "point_processing"
    event_id: UUID | None = None
    reason: str | None = None
    received_at: datetime | None = None
    calculated_at: datetime | None = None
    processing_revision_id: UUID | None = None
    configuration_revision: int | None = None
    source_digest: str | None = None

    def source_evidence(self) -> dict[str, Any]:
        """Return the one source identity every upper-layer consumer records."""
        return {
            "source_kind": self.source_kind,
            "event_id": str(self.event_id) if self.event_id else None,
            "processing_revision_id": (
                str(self.processing_revision_id)
                if self.processing_revision_id
                else None
            ),
            "configuration_revision": self.configuration_revision,
            "source_digest": self.source_digest,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "entity_instance_id": str(self.entity_instance_id),
            "definition_id": self.definition_id,
            "node_id": str(self.node_id),
            "node_key": self.node_key,
            "value": self.value,
            "data_type": self.data_type,
            "unit": self.unit,
            "observed_at": self.observed_at.isoformat(),
            "quality": self.quality,
            "age_ms": self.age_ms,
            "fresh": self.fresh,
            "quality_good": self.quality_good,
            **self.source_evidence(),
            "reason": self.reason,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "calculated_at": (
                self.calculated_at.isoformat() if self.calculated_at else None
            ),
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
        observation = self._observations.latest(resolved)
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
            node_id=resolved.node_id,
            node_key=resolved.node_key,
            value=observation.value,
            data_type=resolved.data_type,
            unit=resolved.unit,
            observed_at=observed_at,
            quality=observation.quality,
            age_ms=round(age_seconds * 1000),
            fresh=age_seconds <= resolved.freshness_seconds,
            quality_good=observation.quality == 192,
            max_observation_gap_seconds=resolved.freshness_seconds,
            source_kind=resolved.source_kind,
            event_id=observation.event_id,
            reason=observation.reason,
            received_at=observation.received_at,
            calculated_at=observation.calculated_at,
            processing_revision_id=observation.processing_revision_id,
            configuration_revision=observation.configuration_revision,
            source_digest=observation.source_digest,
        )

    def history(self, entity_instance_id: UUID, range_key: str) -> list[EntityInstanceObservation]:
        """Return engineering observations from the one confirmed source only."""
        resolved = self._registry.resolve(entity_instance_id)
        return [
            EntityInstanceObservation(
                entity_instance_id=resolved.entity_instance_id,
                definition_id=resolved.definition_id,
                node_id=resolved.node_id,
                node_key=resolved.node_key,
                value=item.value,
                data_type=resolved.data_type,
                unit=resolved.unit,
                observed_at=item.observed_at,
                quality=item.quality,
                age_ms=0,
                fresh=True,
                quality_good=item.quality == 192,
                max_observation_gap_seconds=resolved.freshness_seconds,
                source_kind=resolved.source_kind,
                event_id=item.event_id,
                reason=item.reason,
                received_at=item.received_at,
                calculated_at=item.calculated_at,
                processing_revision_id=item.processing_revision_id,
                configuration_revision=item.configuration_revision,
                source_digest=item.source_digest,
            )
            for item in self._observations.history(resolved, range_key)
        ]
