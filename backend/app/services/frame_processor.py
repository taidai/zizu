"""Claim one frozen frame, evaluate the full L1 DAG, and commit it atomically."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from app.services.data_trunk import ConversionEvaluator
from app.services.data_trunk_contracts import (
    ClaimedFrame,
    InputReference,
    L2Observation,
    ProcessingSnapshot,
    TerminalFrame,
)


class FrameRepository(Protocol):
    def claim_next(self, now: datetime) -> ClaimedFrame | None: ...

    def load_processing_snapshot(self, claimed: ClaimedFrame) -> ProcessingSnapshot: ...

    def complete(
        self,
        claimed: ClaimedFrame,
        snapshot: ProcessingSnapshot,
        outputs: tuple[L2Observation, ...],
    ) -> TerminalFrame: ...


class FrameProcessor:
    def __init__(
        self,
        repository: FrameRepository,
        *,
        evaluator: ConversionEvaluator,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._evaluator = evaluator
        self._clock = clock

    def process_next(self, now: datetime | None = None) -> TerminalFrame | None:
        calculated_at = now or self._clock()
        claimed = self._repository.claim_next(calculated_at)
        if claimed is None:
            return None
        snapshot = self._repository.load_processing_snapshot(claimed)
        current_inputs = snapshot.current_inputs()
        outputs: list[L2Observation] = []
        for entity_id in snapshot.topological_output_ids:
            installed = snapshot.installed_by_entity_id[entity_id]
            produced = self._evaluator(
                installed=(installed,),
                current_inputs=current_inputs,
                configuration_revision=claimed.configuration_revision,
                calculated_at=calculated_at,
                frame_id=claimed.frame_id,
                frame_sequence=claimed.frame_sequence,
            )[0]
            outputs.append(produced)
            current_inputs[InputReference.l2(entity_id)] = produced
        return self._repository.complete(claimed, snapshot, tuple(outputs))
