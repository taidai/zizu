from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
import unittest
from uuid import UUID

from app.services.data_trunk_contracts import (
    ClaimedFrame,
    DataTrunkError,
    FrameStatus,
    InstalledPointProcessing,
    L2Observation,
    ProcessingSnapshot,
    TerminalFrame,
    TrunkQuality,
    TypedValue,
)
from app.services.frame_processor import FrameProcessor, downstream_closure


NOW = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)
FRAME_ID = UUID("00000000-0000-0000-0000-000000000901")
FIRST_ENTITY = UUID("00000000-0000-0000-0000-000000000101")
SECOND_ENTITY = UUID("00000000-0000-0000-0000-000000000102")


def _processing(entity_id: UUID, suffix: int) -> InstalledPointProcessing:
    return InstalledPointProcessing.numeric(
        installation_id=UUID(f"00000000-0000-0000-0000-{suffix:012d}"),
        revision_id=UUID(f"10000000-0000-0000-0000-{suffix:012d}"),
        input_tag_id=UUID(f"20000000-0000-0000-0000-{suffix:012d}"),
        output_entity_instance_id=entity_id,
        output_definition_id=f"output.{suffix}",
        scale=1.0,
        offset=0.0,
        input_unit="kW",
        output_unit="kW",
        minimum=None,
        maximum=None,
    )


class _Repository:
    def __init__(self) -> None:
        self.claimed = ClaimedFrame(
            frame_id=FRAME_ID,
            frame_sequence=9,
            capture_beat=4,
            shot_at=NOW,
            configuration_revision=7,
            attempt_count=1,
            processing_owner=UUID("30000000-0000-0000-0000-000000000001"),
            processing_token=UUID("30000000-0000-0000-0000-000000000002"),
            lease_until=NOW + timedelta(seconds=30),
            created_at=NOW,
        )
        installed = {
            FIRST_ENTITY: _processing(FIRST_ENTITY, 1),
            SECOND_ENTITY: _processing(SECOND_ENTITY, 2),
        }
        self.snapshot = ProcessingSnapshot(
            l0_by_tag=MappingProxyType({}),
            installed_by_entity_id=MappingProxyType(installed),
            topological_output_ids=(FIRST_ENTITY, SECOND_ENTITY),
            dependency_edges=((FIRST_ENTITY, SECOND_ENTITY),),
        )
        self.outputs: tuple[L2Observation, ...] = ()
        self.failure = None

    def claim_next(self, now: datetime) -> ClaimedFrame | None:
        return self.claimed

    def load_processing_snapshot(self, claimed: ClaimedFrame) -> ProcessingSnapshot:
        return self.snapshot

    def complete(self, claimed, snapshot, outputs):
        self.outputs = outputs
        return TerminalFrame(
            frame_id=claimed.frame_id,
            frame_sequence=claimed.frame_sequence,
            configuration_revision=claimed.configuration_revision,
            status=FrameStatus.COMPLETE,
            finished_at=NOW,
        )

    def retry_or_fail(self, claimed, failure, now):
        self.failure = failure
        return None


class FrameProcessorTest(unittest.TestCase):
    def test_downstream_closure_includes_transitive_dependents(self) -> None:
        third = UUID("00000000-0000-0000-0000-000000000103")
        self.assertEqual(
            frozenset({FIRST_ENTITY, SECOND_ENTITY, third}),
            downstream_closure(
                frozenset({FIRST_ENTITY}),
                ((FIRST_ENTITY, SECOND_ENTITY), (SECOND_ENTITY, third)),
            ),
        )

    def test_evaluates_each_active_output_once_in_dag_order(self) -> None:
        repository = _Repository()
        evaluated: list[UUID] = []

        def evaluator(*, installed, configuration_revision, calculated_at,
                      frame_id=None, frame_sequence=0, **_kwargs):
            item = installed[0]
            evaluated.append(item.entity_instance_id)
            return (
                L2Observation(
                    event_id=UUID(f"40000000-0000-0000-0000-{len(evaluated):012d}"),
                    entity_instance_id=item.entity_instance_id,
                    definition_id=item.entity_definition_id,
                    value=TypedValue.float(float(len(evaluated))),
                    unit="kW",
                    quality=TrunkQuality.GOOD,
                    reason=None,
                    observed_at=NOW,
                    received_at=NOW,
                    calculated_at=NOW,
                    processing_revision_id=item.revision_id,
                    configuration_revision=configuration_revision,
                    source_observation_ids=(),
                    source_digest="a" * 64,
                    source_order_key="frame",
                    event_time_basis="calculated_at",
                    frame_id=frame_id,
                    frame_sequence=frame_sequence,
                ),
            )

        terminal = FrameProcessor(
            repository,
            evaluator=evaluator,
            clock=lambda: NOW,
        ).process_next(NOW)

        self.assertEqual(FrameStatus.COMPLETE, terminal.status)
        self.assertEqual([FIRST_ENTITY, SECOND_ENTITY], evaluated)
        self.assertEqual(FRAME_ID, repository.outputs[0].frame_id)
        self.assertTrue(
            all(output.frame_sequence == 9 for output in repository.outputs)
        )

    def test_returns_none_without_pending_frame(self) -> None:
        repository = _Repository()
        repository.claim_next = lambda _now: None
        processor = FrameProcessor(
            repository,
            evaluator=lambda **_kwargs: (),
            clock=lambda: NOW,
        )
        self.assertIsNone(processor.process_next())

    def test_evaluation_failure_targets_transitive_downstream_for_retry(self) -> None:
        repository = _Repository()

        def fail(**_kwargs):
            raise RuntimeError("formula failed")

        result = FrameProcessor(
            repository,
            evaluator=fail,
            clock=lambda: NOW,
        ).process_next(NOW)

        self.assertIsNone(result)
        self.assertEqual(
            frozenset({FIRST_ENTITY, SECOND_ENTITY}),
            repository.failure.failed_entity_ids,
        )

    def test_completion_failure_is_retried_without_leaving_claim_stuck(self) -> None:
        repository = _Repository()

        def evaluator(*, installed, configuration_revision, calculated_at,
                      frame_id=None, frame_sequence=0, **_kwargs):
            item = installed[0]
            return (
                L2Observation(
                    event_id=UUID("40000000-0000-0000-0000-000000000099"),
                    entity_instance_id=item.entity_instance_id,
                    definition_id=item.entity_definition_id,
                    value=TypedValue.float(1.0),
                    unit="kW",
                    quality=TrunkQuality.GOOD,
                    reason=None,
                    observed_at=NOW,
                    received_at=NOW,
                    calculated_at=NOW,
                    processing_revision_id=item.revision_id,
                    configuration_revision=configuration_revision,
                    source_observation_ids=(),
                    source_digest="a" * 64,
                    source_order_key="frame",
                    event_time_basis="calculated_at",
                    frame_id=frame_id,
                    frame_sequence=frame_sequence,
                ),
            )

        def fail_complete(*_args, **_kwargs):
            raise DataTrunkError(
                "POINT_PROCESSING_SOURCE_MISSING",
                "source evidence was pruned",
            )

        repository.complete = fail_complete

        result = FrameProcessor(
            repository,
            evaluator=evaluator,
            clock=lambda: NOW,
        ).process_next(NOW)

        self.assertIsNone(result)
        self.assertEqual(
            "POINT_PROCESSING_SOURCE_MISSING",
            repository.failure.code,
        )
        self.assertEqual(
            frozenset({FIRST_ENTITY, SECOND_ENTITY}),
            repository.failure.failed_entity_ids,
        )


if __name__ == "__main__":
    unittest.main()
