"""L0—L2 数据主干的唯一公开写入模块。"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.models.schemas import ParsedMessage
from app.services.data_trunk_contracts import (
    CommitReceipt,
    DataTrunkError,
    InputReference,
    InstalledPointConversion,
    L2Observation,
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.data_trunk_conversion import evaluate_conversion


class ConversionEvaluator(Protocol):
    def __call__(
        self,
        *,
        installed: tuple[InstalledPointConversion, ...],
        current_inputs: Mapping[InputReference, RawObservation | L2Observation],
        site_configuration_version: int,
        calculated_at: datetime,
    ) -> tuple[L2Observation, ...]: ...


class DataTrunkRepository(Protocol):
    def transact(
        self,
        raw_observations: tuple[RawObservation, ...],
        evaluator: ConversionEvaluator,
    ) -> CommitReceipt: ...

    def record_failure(
        self,
        raw_observations: tuple[RawObservation, ...],
        *,
        attempts: int,
        error_code: str,
    ) -> UUID: ...

    def acceptance_evidence(
        self,
        *,
        solution_installation_id: UUID,
        entity_definition_ids: tuple[str, ...],
    ) -> dict[str, Any]: ...


class DataTrunk:
    """隐藏转换查找、时序推进、来源图、outbox 与事务顺序。"""

    def __init__(self, repository: DataTrunkRepository) -> None:
        self._repository = repository

    def ingest(self, raw_observations: Sequence[RawObservation]) -> CommitReceipt:
        batch = tuple(raw_observations)
        if not batch:
            raise DataTrunkError(
                "DATA_TRUNK_BATCH_EMPTY",
                "Raw observation batch is empty",
            )
        return self._repository.transact(batch, evaluate_conversion)

    def record_failure(
        self,
        raw_observations: Sequence[RawObservation],
        *,
        attempts: int,
        error_code: str,
    ) -> UUID:
        """Persist a safe terminal retry reference; never stores raw values."""
        batch = tuple(raw_observations)
        if not batch or attempts < 1:
            raise DataTrunkError(
                "DATA_TRUNK_FAILURE_INVALID",
                "Failure reference requires observations and attempts",
            )
        return self._repository.record_failure(
            batch,
            attempts=attempts,
            error_code=error_code,
        )

    def acceptance_evidence(
        self,
        *,
        solution_installation_id: UUID,
        entity_definition_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Read committed data-trunk evidence without changing runtime state."""
        return self._repository.acceptance_evidence(
            solution_installation_id=solution_installation_id,
            entity_definition_ids=tuple(entity_definition_ids),
        )


class InMemoryDataTrunkRepository:
    """Test adapter with the same dedup-and-convert boundary as PostgreSQL."""

    def __init__(
        self,
        *,
        installed_provider: Callable[[], tuple[InstalledPointConversion, ...]],
        site_configuration_version: Callable[[], int],
        on_l2_committed: Callable[[tuple[L2Observation, ...]], None] | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        self._installed_provider = installed_provider
        self._site_configuration_version = site_configuration_version
        self._on_l2_committed = on_l2_committed or (lambda _items: None)
        self._clock = clock
        self._source_digests: set[str] = set()
        self._l0_history: list[RawObservation] = []
        self._l2_history: list[L2Observation] = []
        self._failures: set[UUID] = set()
        self._lock = RLock()

    def transact(
        self,
        raw_observations: tuple[RawObservation, ...],
        evaluator: ConversionEvaluator,
    ) -> CommitReceipt:
        with self._lock:
            batch_digests: set[str] = set()
            accepted_items: list[RawObservation] = []
            for item in raw_observations:
                if (
                    item.source_digest in self._source_digests
                    or item.source_digest in batch_digests
                ):
                    continue
                batch_digests.add(item.source_digest)
                accepted_items.append(item)
            accepted = tuple(accepted_items)
            installed = self._installed_provider()
            calculated_at = self._clock()
            produced: list[L2Observation] = []
            for observation in sorted(
                accepted,
                key=lambda item: (
                    item.source_timestamp,
                    _raw_order_key(item),
                    str(item.tag_id),
                ),
            ):
                input_reference = InputReference.l0(observation.tag_id)
                affected = tuple(
                    item
                    for item in installed
                    if item.transform.input == input_reference
                )
                if not affected:
                    continue
                produced.extend(
                    evaluator(
                        installed=affected,
                        current_inputs={input_reference: observation},
                        site_configuration_version=(
                            self._site_configuration_version()
                        ),
                        calculated_at=calculated_at,
                    )
                )

            committed_l2 = tuple(produced)
            self._source_digests.update(item.source_digest for item in accepted)
            self._l0_history.extend(accepted)
            self._l2_history.extend(committed_l2)
            self._on_l2_committed(committed_l2)

        return CommitReceipt(
            transaction_id=uuid4(),
            accepted_l0_count=len(accepted),
            duplicate_l0_count=len(raw_observations) - len(accepted),
            l2_event_ids=tuple(item.event_id for item in committed_l2),
            late_observation_count=0,
            accepted_l0_observation_ids=tuple(
                item.observation_id for item in accepted
            ),
        )

    def record_failure(
        self,
        raw_observations: tuple[RawObservation, ...],
        *,
        attempts: int,
        error_code: str,
    ) -> UUID:
        source_digest = hashlib.sha256(
            "\n".join(
                sorted(item.source_digest for item in raw_observations)
            ).encode("ascii")
        ).hexdigest()
        failure_id = uuid5(
            NAMESPACE_URL,
            f"zizu:ingestion-failure:{source_digest}:{attempts}:{error_code}",
        )
        with self._lock:
            self._failures.add(failure_id)
        return failure_id

    def acceptance_evidence(
        self,
        *,
        solution_installation_id: UUID,
        entity_definition_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        del solution_installation_id
        required = set(entity_definition_ids)
        with self._lock:
            installed = self._installed_provider()
            observed = {
                item.definition_id
                for item in self._l2_history
                if item.definition_id in required
            }
            revisions = {
                str(item.revision_id)
                for item in installed
                if item.entity_definition_id in required
            }
            entity_ids = {
                str(item.entity_instance_id)
                for item in installed
                if item.entity_definition_id in required
            }
            site_versions = sorted(
                {
                    item.site_configuration_version
                    for item in self._l2_history
                    if item.definition_id in required
                }
            )
            source_ids = {
                str(source_id)
                for item in self._l2_history
                if item.definition_id in required
                for source_id in item.source_observation_ids
            }
            latest_by_entity = {}
            for item in self._l2_history:
                if item.definition_id not in required:
                    continue
                current = latest_by_entity.get(item.entity_instance_id)
                if current is None or (
                    item.observed_at,
                    item.source_order_key,
                    str(item.event_id),
                ) > (
                    current.observed_at,
                    current.source_order_key,
                    str(current.event_id),
                ):
                    latest_by_entity[item.entity_instance_id] = item
            good_latest_count = sum(
                item.quality == TrunkQuality.GOOD
                for item in latest_by_entity.values()
            )
            ordered_timestamp_count = sum(
                item.observed_at <= item.received_at <= item.calculated_at
                for item in latest_by_entity.values()
            )
            return {
                "required_entity_definitions": sorted(required),
                "observed_entity_definitions": sorted(observed),
                "entity_instance_ids": sorted(entity_ids),
                "conversion_revision_ids": sorted(revisions),
                "site_configuration_versions": site_versions,
                "l0_observation_count": len(self._l0_history),
                "l2_observation_count": sum(
                    item.definition_id in required for item in self._l2_history
                ),
                "l2_latest_count": len(latest_by_entity),
                "source_observation_count": len(source_ids),
                "committed_event_count": sum(
                    item.definition_id in required for item in self._l2_history
                ),
                "outbox_event_count": sum(
                    item.definition_id in required for item in self._l2_history
                ),
                "good_latest_count": good_latest_count,
                "ordered_timestamp_count": ordered_timestamp_count,
            }


def _raw_order_key(observation: RawObservation) -> str:
    if observation.source_sequence is None:
        return f"D:{observation.source_digest}"
    return f"S:{observation.source_sequence:020d}:{observation.source_digest}"


@dataclass(frozen=True)
class TagMetadata:
    node_id: UUID
    tag_id: UUID
    stable_source_key: str
    data_type: str
    unit: str | None


class RawObservationAdapter:
    """Translate parsed protocol values into deterministic canonical L0 facts."""

    def from_parsed(
        self,
        parsed: ParsedMessage,
        tag_catalog: Mapping[str, TagMetadata],
        *,
        received_at: datetime,
        source_message_id: str | None,
        source_sequence: int | None,
    ) -> tuple[RawObservation, ...]:
        observations: list[RawObservation] = []
        for source_key, raw_value in sorted(parsed.tags.items()):
            metadata = tag_catalog.get(source_key)
            if metadata is None:
                continue
            value = _raw_typed_value(raw_value, metadata.data_type)
            if value is None:
                continue
            digest = _raw_source_digest(
                metadata,
                parsed.timestamp,
                source_sequence,
                value,
            )
            observations.append(
                RawObservation(
                    observation_id=uuid5(NAMESPACE_URL, f"zizu:l0:{digest}"),
                    node_id=metadata.node_id,
                    tag_id=metadata.tag_id,
                    source_key=metadata.stable_source_key,
                    value=value,
                    raw_unit=metadata.unit,
                    quality=TrunkQuality.GOOD,
                    source_timestamp=parsed.timestamp,
                    received_at=received_at,
                    source_message_id=source_message_id,
                    source_sequence=source_sequence,
                    source_digest=digest,
                )
            )
        return tuple(observations)


def _raw_typed_value(raw_value, data_type: str) -> TypedValue | None:
    kind = data_type.upper()
    if isinstance(raw_value, bool):
        return TypedValue(ValueKind.BOOL, raw_value)
    if isinstance(raw_value, int):
        if kind == "FLOAT":
            return TypedValue(ValueKind.FLOAT, float(raw_value))
        if kind == "ENUM":
            return TypedValue(ValueKind.ENUM, str(raw_value))
        return TypedValue(ValueKind.INT, raw_value)
    if isinstance(raw_value, float):
        number = float(raw_value)
        return TypedValue(ValueKind.FLOAT, number) if math.isfinite(number) else None
    if isinstance(raw_value, str):
        value_kind = ValueKind.ENUM if kind == "ENUM" else ValueKind.STRING
        return TypedValue(value_kind, raw_value)
    return None


def _raw_source_digest(
    metadata: TagMetadata,
    observed_at: datetime,
    source_sequence: int | None,
    value: TypedValue,
) -> str:
    canonical = json.dumps(
        {
            "node_id": str(metadata.node_id),
            "tag_id": str(metadata.tag_id),
            "source_key": metadata.stable_source_key,
            "observed_at": observed_at.isoformat(),
            "source_sequence": source_sequence,
            "value_kind": value.kind.value,
            "value": value.value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class _FreshnessRepository(Protocol):
    def mark_expired_outputs_stale(self, now: datetime) -> int: ...


class _FreshnessScheduler:
    """由 DataTrunk implementation 持有的内部新鲜度调度器。"""

    def __init__(
        self,
        repository: _FreshnessRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._clock = clock

    def run_once(self, now: datetime | None = None) -> int:
        return self._repository.mark_expired_outputs_stale(now or self._clock())
