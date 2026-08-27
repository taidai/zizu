"""L0—L2 数据主干的唯一公开写入模块。"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.models.schemas import ParsedMessage
from app.services.data_trunk_contracts import (
    BlackboardRecovery,
    AcceptReceipt,
    FrozenFrameCandidate,
    PendingFrame,
    RawObservation,
    SourceOrder,
    TrunkQuality,
    TypedValue,
    TerminalFrame,
    ValueKind,
)
from app.services.realtime_blackboard import RealtimeBlackboard
from app.services.configuration_revision import ConfigurationRuntimeGate


class DataTrunkRepository(Protocol):
    def acquire_writer(self) -> object: ...

    def restore_blackboard(self) -> BlackboardRecovery: ...

    def commit_pending(self, candidate: FrozenFrameCandidate) -> PendingFrame: ...

    def current_configuration_revision(self) -> int: ...

    def unfinished_frame_count(self) -> int: ...

    def unpublished_frame_outbox_count(self) -> int: ...


class DataTrunk:
    """The only public runtime seam: accept, capture, then process."""

    def __init__(
        self,
        repository: DataTrunkRepository,
        *,
        blackboard: RealtimeBlackboard,
        processor: Any,
        writer_lease: object | None = None,
    ) -> None:
        self._repository = repository
        self._blackboard = blackboard
        self._processor = processor
        self._writer_lease = writer_lease
        self._configuration_gate = ConfigurationRuntimeGate(
            repository, blackboard
        )

    def accept(self, raw_observations: Sequence[RawObservation]) -> AcceptReceipt:
        batch = tuple(raw_observations)
        return self._blackboard.accept_many(batch)

    def capture_tick(self, now: datetime) -> PendingFrame | None:
        if not self._configuration_gate.enter_capture():
            return None
        try:
            candidate = self._blackboard.tick(
                now,
                configuration_revision=(
                    self._repository.current_configuration_revision()
                ),
            )
            if candidate is None:
                return None
            pending = self._repository.commit_pending(candidate)
            self._blackboard.acknowledge(candidate.generation)
            return pending
        finally:
            self._configuration_gate.leave_capture()

    def process_next(self, now: datetime) -> TerminalFrame | None:
        if not self._configuration_gate.enter_processor():
            return None
        try:
            return self._processor.process_next(now)
        finally:
            self._configuration_gate.leave_processor()

    @property
    def configuration_gate(self) -> ConfigurationRuntimeGate:
        return self._configuration_gate

    def close(self) -> None:
        if self._writer_lease is not None:
            self._writer_lease.close()
            self._writer_lease = None


@dataclass(frozen=True)
class TagMetadata:
    node_id: UUID
    tag_id: UUID
    stable_source_key: str
    data_type: str
    unit: str | None
    timestamp_trusted: bool
    source_sequence_trusted: bool = False


class RawObservationAdapter:
    """Translate parsed protocol values into deterministic canonical L0 facts."""

    def __init__(self) -> None:
        self._receive_ordinal = 0
        self._ordinal_lock = RLock()

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
            with self._ordinal_lock:
                self._receive_ordinal += 1
                receive_ordinal = self._receive_ordinal
            if metadata.source_sequence_trusted:
                if source_sequence is None:
                    continue
                source_order = SourceOrder.sequence(source_sequence)
            elif metadata.timestamp_trusted:
                source_order = SourceOrder.observed_at(
                    parsed.timestamp,
                    receive_ordinal,
                    digest,
                )
            else:
                source_order = SourceOrder.received_at(
                    received_at,
                    receive_ordinal,
                    digest,
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
                    event_time_basis=(
                        "received_at"
                        if parsed.event_time_basis == "received_at"
                        or not metadata.timestamp_trusted
                        else "observed_at"
                    ),
                    source_order=source_order,
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
