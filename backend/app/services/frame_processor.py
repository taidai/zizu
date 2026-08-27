"""Claim one frozen frame, evaluate the full L1 DAG, and commit it atomically."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.services.data_trunk_contracts import (
    ClaimedFrame,
    BudgetTerminalizationClaim,
    DataTrunkError,
    FrameFailure,
    InputReference,
    InstalledPointProcessing,
    L2Observation,
    ProcessingSnapshot,
    RawObservation,
    TerminalFrame,
)


class ConversionEvaluator(Protocol):
    def __call__(
        self,
        *,
        installed: tuple[InstalledPointProcessing, ...],
        current_inputs: Mapping[InputReference, RawObservation | L2Observation],
        configuration_revision: int,
        calculated_at: datetime,
        frame_id: UUID | None = None,
        frame_sequence: int = 0,
    ) -> tuple[L2Observation, ...]: ...


class FrameRepository(Protocol):
    def claim_next(
        self, now: datetime
    ) -> ClaimedFrame | BudgetTerminalizationClaim | None: ...

    def load_processing_snapshot(self, claimed: ClaimedFrame) -> ProcessingSnapshot: ...

    def complete(
        self,
        claimed: ClaimedFrame,
        snapshot: ProcessingSnapshot,
        outputs: tuple[L2Observation, ...],
    ) -> TerminalFrame: ...

    def retry_or_fail(
        self,
        claimed: ClaimedFrame,
        failure: FrameFailure,
        now: datetime,
    ) -> TerminalFrame | None: ...

    def fail_budget(
        self,
        claimed: BudgetTerminalizationClaim,
        now: datetime,
    ) -> TerminalFrame: ...


def downstream_closure(
    failed_ids: frozenset[UUID],
    edges: tuple[tuple[UUID, UUID], ...],
) -> frozenset[UUID]:
    outgoing: dict[UUID, set[UUID]] = {}
    for source, target in edges:
        outgoing.setdefault(source, set()).add(target)
    affected = set(failed_ids)
    pending = list(failed_ids)
    while pending:
        current = pending.pop()
        for target in outgoing.get(current, set()):
            if target not in affected:
                affected.add(target)
                pending.append(target)
    return frozenset(affected)


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
        if isinstance(claimed, BudgetTerminalizationClaim):
            return self._repository.fail_budget(claimed, calculated_at)
        snapshot = self._repository.load_processing_snapshot(claimed)
        current_inputs = snapshot.current_inputs()
        outputs: list[L2Observation] = []
        current_entity_id = None
        try:
            for entity_id in snapshot.topological_output_ids:
                current_entity_id = entity_id
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
        except Exception as exc:
            failed = (
                frozenset({current_entity_id})
                if current_entity_id is not None
                else frozenset(snapshot.installed_by_entity_id)
            )
            affected = downstream_closure(failed, snapshot.dependency_edges)
            code = exc.code if isinstance(exc, DataTrunkError) else "FRAME_PROCESSING_FAILED"
            return self._repository.retry_or_fail(
                claimed,
                FrameFailure(code, affected),
                calculated_at,
            )
        return self._repository.complete(claimed, snapshot, tuple(outputs))
