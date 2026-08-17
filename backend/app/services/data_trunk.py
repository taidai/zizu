"""L0—L2 数据主干的唯一公开写入模块。"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

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
