from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import inspect
from threading import Event
from types import MappingProxyType
import unittest
import time
from uuid import UUID

from app.services.data_trunk import DataTrunk
from app.services.data_trunk_contracts import (
    BlackboardRecovery,
    FrameStatus,
    PendingFrame,
    RawObservation,
    SourceOrder,
    SourceOrderMode,
    TrunkQuality,
    TypedValue,
)
from app.services.configuration_revision import (
    CONFIGURATION_DRAIN_TIMEOUT_SECONDS,
    ConfigurationRuntimeGate,
)
from app.services.realtime_blackboard import RealtimeBlackboard


NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
NODE_ID = UUID("51000000-0000-0000-0000-000000000001")
TAG_ID = UUID("51000000-0000-0000-0000-000000000002")


def _raw(sequence: int) -> RawObservation:
    return RawObservation(
        observation_id=UUID(f"51000000-0000-0000-0000-{sequence:012d}"),
        node_id=NODE_ID,
        tag_id=TAG_ID,
        source_key="pcs/power",
        value=TypedValue.float(float(sequence)),
        raw_unit="kW",
        quality=TrunkQuality.GOOD,
        source_timestamp=NOW,
        received_at=NOW,
        source_message_id=f"message-{sequence}",
        source_sequence=sequence,
        source_digest=f"{sequence:064x}",
        event_time_basis="observed_at",
        source_order=SourceOrder.sequence(sequence),
    )


class _Repository:
    def __init__(self) -> None:
        self.pending_write_count = 0
        self.unfinished_count = 0
        self.committed_candidates = []

    def current_configuration_revision(self) -> int:
        return 0

    def commit_pending(self, candidate):
        self.pending_write_count += 1
        self.committed_candidates.append(candidate)
        return PendingFrame(
            frame_id=candidate.frame_id,
            frame_sequence=self.pending_write_count,
            capture_beat=candidate.capture_beat,
            shot_at=candidate.shot_at,
            configuration_revision=0,
            status=FrameStatus.PENDING,
        )

    def unfinished_frame_count(self) -> int:
        return self.unfinished_count

    def unpublished_frame_outbox_count(self) -> int:
        return 0

    def restore_blackboard(self) -> BlackboardRecovery:
        return BlackboardRecovery(
            capture_beat=0,
            configuration_revision=1,
            active_input_contracts=MappingProxyType(
                {TAG_ID: SourceOrderMode.SEQUENCE}
            ),
            required_tag_ids=frozenset({TAG_ID}),
            observations=(),
        )


class _Processor:
    def process_next(self, _now):
        return None


def _runtime(repository=None) -> DataTrunk:
    return DataTrunk(
        repository or _Repository(),
        blackboard=RealtimeBlackboard(
            active_input_contracts=MappingProxyType(
                {TAG_ID: SourceOrderMode.SEQUENCE}
            ),
            required_tag_ids=frozenset({TAG_ID}),
        ),
        processor=_Processor(),
    )


class DataFrameRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_busy_processor_skips_old_photo_and_later_captures_latest_value(self) -> None:
        repository = _Repository()
        repository.unfinished_count = 1
        runtime = _runtime(repository)

        runtime.accept((_raw(1),))
        self.assertIsNone(runtime.capture_tick(NOW))
        self.assertEqual(0, repository.pending_write_count)

        runtime.accept((_raw(2),))
        repository.unfinished_count = 0
        runtime.capture_tick(NOW + timedelta(seconds=1))

        committed = repository.committed_candidates[0]
        self.assertEqual(1, repository.pending_write_count)
        self.assertEqual(2.0, committed.changed_l0[0].observation.value.value)

    async def test_busy_ticks_still_age_an_old_value_to_stale(self) -> None:
        repository = _Repository()
        repository.unfinished_count = 1
        runtime = _runtime(repository)
        runtime.accept((_raw(1),))

        for second in range(4):
            self.assertIsNone(runtime.capture_tick(NOW + timedelta(seconds=second)))

        repository.unfinished_count = 0
        runtime.capture_tick(NOW + timedelta(seconds=4))

        committed = repository.committed_candidates[0]
        self.assertEqual(5, committed.capture_beat)
        self.assertEqual(TrunkQuality.STALE, committed.changed_l0[0].effective_quality)

    async def test_configuration_drain_budget_covers_normal_edge_backlog(self) -> None:
        self.assertEqual(30.0, CONFIGURATION_DRAIN_TIMEOUT_SECONDS)
        timeout = inspect.signature(
            ConfigurationRuntimeGate.begin_configuration_publish
        ).parameters["timeout_seconds"]
        self.assertEqual(CONFIGURATION_DRAIN_TIMEOUT_SECONDS, timeout.default)

    async def test_configuration_publish_without_consumer_fails_immediately(self) -> None:
        gate = _runtime().configuration_gate
        started = time.monotonic()
        with self.assertRaisesRegex(
            Exception, "COMMITTED_FRAME_CONSUMER_MISSING"
        ):
            gate.begin_configuration_publish(0)
        self.assertLess(time.monotonic() - started, 0.1)

    async def test_quiesced_revision_reconciles_from_database_truth(self) -> None:
        runtime = _runtime()
        gate = runtime.configuration_gate
        gate.register_committed_frame_consumer()
        gate.begin_configuration_publish(0)
        self.assertIsNone(runtime.capture_tick(NOW))
        revision = gate.reconcile_configuration_runtime()
        self.assertEqual(1, revision.revision)

    async def test_sixty_empty_ticks_after_stale_transition_create_no_more_frames(self) -> None:
        repository = _Repository()
        runtime = _runtime(repository)
        runtime.accept((_raw(1),))
        for second in range(4):
            runtime.capture_tick(NOW + timedelta(seconds=second))
        writes_after_stale = repository.pending_write_count
        for second in range(4, 64):
            runtime.capture_tick(NOW + timedelta(seconds=second))
        self.assertEqual(2, writes_after_stale)
        self.assertEqual(writes_after_stale, repository.pending_write_count)

    async def test_slow_transaction_a_does_not_block_mqtt_accept(self) -> None:
        class BlockingRepository(_Repository):
            def __init__(self) -> None:
                super().__init__()
                self.started = Event()
                self.release = Event()

            def commit_pending(self, candidate):
                self.started.set()
                self.release.wait(timeout=2)
                return super().commit_pending(candidate)

        repository = BlockingRepository()
        runtime = _runtime(repository)
        runtime.accept((_raw(1),))
        capture = asyncio.create_task(
            asyncio.to_thread(runtime.capture_tick, NOW)
        )
        await asyncio.to_thread(repository.started.wait, 1)
        receipt = runtime.accept((_raw(2),))
        self.assertEqual(1, receipt.accepted_count)
        repository.release.set()
        await asyncio.wait_for(capture, timeout=1)


if __name__ == "__main__":
    unittest.main()
