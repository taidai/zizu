"""单站实时黑板：接收最新 L0，并按统一节拍冻结一致数据帧。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from threading import RLock
from types import MappingProxyType
from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

from app.services.data_trunk_contracts import (
    AcceptReceipt,
    BlackboardState,
    DataTrunkError,
    FramedRawObservation,
    FrozenFrameCandidate,
    RawObservation,
    SourceOrderMode,
    TrunkQuality,
)


@dataclass
class _Cell:
    observation: RawObservation
    accepted_beat: int
    effective_quality: TrunkQuality


def _error(code: str) -> DataTrunkError:
    return DataTrunkError(code, code)


def _typed_value_material(value: object) -> object:
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, tuple):
        return list(value)
    return value


def _timestamp_material(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("frame timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _observation_changed(previous: _Cell | None, current: RawObservation) -> bool:
    if previous is None:
        return True
    old = previous.observation
    return (
        old.value != current.value
        or old.raw_unit != current.raw_unit
        or old.quality != current.quality
        or previous.effective_quality != current.quality
    )


class RealtimeBlackboard:
    """保存单站最新活动 L0；数据库和网络 I/O 必须留在本类之外。"""

    def __init__(
        self,
        *,
        active_input_contracts: Mapping[UUID, SourceOrderMode],
        required_tag_ids: frozenset[UUID],
        stale_after_beats: int = 3,
        capture_beat: int = 0,
    ) -> None:
        if stale_after_beats < 1:
            raise ValueError("stale beats must be positive")
        if capture_beat < 0:
            raise ValueError("capture beat must not be negative")
        self._validate_contracts(active_input_contracts, required_tag_ids)
        self._lock = RLock()
        self._active_input_contracts = dict(active_input_contracts)
        self._required_tag_ids = frozenset(required_tag_ids)
        self._stale_after_beats = stale_after_beats
        self._capture_beat = capture_beat
        self._configuration_revision: int | None = None
        self._cells: dict[UUID, _Cell] = {}
        self._seen_this_revision: set[UUID] = set()
        self._changed_l0_tags: set[UUID] = set()
        self._state_dirty = False
        self._generation = 0
        self._frozen: FrozenFrameCandidate | None = None

    @staticmethod
    def _validate_contracts(
        active_input_contracts: Mapping[UUID, SourceOrderMode],
        required_tag_ids: frozenset[UUID],
    ) -> None:
        if not required_tag_ids.issubset(active_input_contracts):
            raise ValueError("required tags must be active")
        if any(not isinstance(mode, SourceOrderMode) for mode in active_input_contracts.values()):
            raise ValueError("source order mode is invalid")

    @property
    def capture_beat(self) -> int:
        with self._lock:
            return self._capture_beat

    @property
    def missing_required_tags(self) -> frozenset[UUID]:
        with self._lock:
            return frozenset(self._required_tag_ids - self._seen_this_revision)

    @property
    def state(self) -> BlackboardState:
        return (
            BlackboardState.WARMING
            if self.missing_required_tags
            else BlackboardState.READY
        )

    def accept_many(self, observations: Sequence[RawObservation]) -> AcceptReceipt:
        incoming = tuple(observations)
        with self._lock:
            for observation in incoming:
                expected_mode = self._active_input_contracts.get(observation.tag_id)
                if expected_mode is None:
                    continue
                if observation.source_order is None:
                    raise _error("DATA_FRAME_SOURCE_ORDER_MISSING")
                if observation.source_order.mode is not expected_mode:
                    raise _error("DATA_FRAME_SOURCE_ORDER_MODE_MISMATCH")

            accepted = 0
            dropped = 0
            for observation in incoming:
                if observation.tag_id not in self._active_input_contracts:
                    dropped += 1
                    continue
                previous = self._cells.get(observation.tag_id)
                assert observation.source_order is not None
                if previous is not None:
                    assert previous.observation.source_order is not None
                    if not observation.source_order.is_after(
                        previous.observation.source_order
                    ):
                        dropped += 1
                        continue

                changed = _observation_changed(previous, observation)
                accepted_beat = self._capture_beat + 1
                self._cells[observation.tag_id] = _Cell(
                    observation=observation,
                    accepted_beat=accepted_beat,
                    effective_quality=observation.quality,
                )
                self._seen_this_revision.add(observation.tag_id)
                if changed:
                    self._changed_l0_tags.add(observation.tag_id)
                    self._state_dirty = True
                accepted += 1
            return AcceptReceipt(accepted_count=accepted, dropped_count=dropped)

    def tick(
        self,
        shot_at: datetime,
        *,
        configuration_revision: int,
    ) -> FrozenFrameCandidate | None:
        _timestamp_material(shot_at)
        with self._lock:
            if self._configuration_revision is None:
                self._configuration_revision = configuration_revision
            elif self._configuration_revision != configuration_revision:
                raise _error("DATA_FRAME_CONFIGURATION_STALE")

            self._capture_beat += 1
            self._advance_freshness()
            if self._frozen is not None:
                return self._frozen
            if self._required_tag_ids - self._seen_this_revision:
                return None
            if not self._state_dirty:
                return None

            self._generation += 1
            cells = {
                tag_id: FramedRawObservation(
                    observation=cell.observation,
                    accepted_beat=cell.accepted_beat,
                    effective_quality=cell.effective_quality,
                )
                for tag_id, cell in sorted(
                    self._cells.items(), key=lambda item: str(item[0])
                )
                if tag_id in self._active_input_contracts
            }
            changed_l0 = tuple(
                cells[tag_id]
                for tag_id in sorted(self._changed_l0_tags, key=str)
                if tag_id in cells
            )
            frame_id = uuid4()
            digest = self._candidate_digest(
                frame_id=frame_id,
                generation=self._generation,
                shot_at=shot_at,
                configuration_revision=configuration_revision,
                cells=cells,
                changed_l0=changed_l0,
            )
            self._frozen = FrozenFrameCandidate(
                frame_id=frame_id,
                candidate_digest=digest,
                generation=self._generation,
                capture_beat=self._capture_beat,
                shot_at=shot_at,
                configuration_revision=configuration_revision,
                cells=MappingProxyType(cells),
                changed_l0=changed_l0,
            )
            self._changed_l0_tags.clear()
            self._state_dirty = False
            return self._frozen

    def acknowledge(self, generation: int) -> None:
        with self._lock:
            if self._frozen is None or self._frozen.generation != generation:
                raise _error("DATA_FRAME_GENERATION_MISMATCH")
            self._frozen = None

    def reset_revision(
        self,
        revision: int,
        active_input_contracts: Mapping[UUID, SourceOrderMode],
        required_tag_ids: frozenset[UUID],
    ) -> None:
        self._validate_contracts(active_input_contracts, required_tag_ids)
        with self._lock:
            retained: dict[UUID, _Cell] = {}
            retained_seen: set[UUID] = set()
            for tag_id, new_mode in active_input_contracts.items():
                if self._active_input_contracts.get(tag_id) is not new_mode:
                    continue
                cell = self._cells.get(tag_id)
                if cell is None:
                    continue
                retained[tag_id] = cell
                if tag_id in self._seen_this_revision:
                    retained_seen.add(tag_id)

            self._configuration_revision = revision
            self._active_input_contracts = dict(active_input_contracts)
            self._required_tag_ids = frozenset(required_tag_ids)
            self._cells = retained
            self._seen_this_revision = retained_seen
            self._changed_l0_tags = set(retained)
            self._state_dirty = bool(retained)
            self._frozen = None

    def _advance_freshness(self) -> None:
        for tag_id, cell in self._cells.items():
            missed_beats = self._capture_beat - cell.accepted_beat
            desired = (
                TrunkQuality.STALE
                if missed_beats >= self._stale_after_beats
                else cell.observation.quality
            )
            if desired == cell.effective_quality:
                continue
            cell.effective_quality = desired
            self._state_dirty = True

    def _candidate_digest(
        self,
        *,
        frame_id: UUID,
        generation: int,
        shot_at: datetime,
        configuration_revision: int,
        cells: Mapping[UUID, FramedRawObservation],
        changed_l0: tuple[FramedRawObservation, ...],
    ) -> str:
        material = {
            "frame_id": str(frame_id),
            "generation": generation,
            "capture_beat": self._capture_beat,
            "shot_at": _timestamp_material(shot_at),
            "configuration_revision": configuration_revision,
            "cells": [
                {
                    "tag_id": str(tag_id),
                    "observation_id": str(cell.observation.observation_id),
                    "value_kind": cell.observation.value.kind.value,
                    "value": _typed_value_material(cell.observation.value.value),
                    "source_quality": int(cell.observation.quality),
                    "effective_quality": int(cell.effective_quality),
                    "accepted_beat": cell.accepted_beat,
                    "source_order": {
                        "mode": cell.observation.source_order.mode.value,
                        "primary": cell.observation.source_order.primary,
                        "secondary": cell.observation.source_order.secondary,
                        "tie_breaker": cell.observation.source_order.tie_breaker,
                    },
                }
                for tag_id, cell in cells.items()
            ],
            "changed_l0": [
                str(cell.observation.observation_id) for cell in changed_l0
            ],
        }
        encoded = json.dumps(
            material,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
