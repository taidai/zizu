from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.services.realtime_blackboard import RealtimeBlackboard
from app.services.data_trunk_contracts import (
    BlackboardState,
    DataTrunkError,
    FramedRawObservation,
    RawObservation,
    SourceOrder,
    SourceOrderMode,
    TrunkQuality,
    TypedValue,
)


TAG_A = UUID("51000000-0000-0000-0000-000000000001")
TAG_B = UUID("51000000-0000-0000-0000-000000000002")
TAG_C = UUID("51000000-0000-0000-0000-000000000003")
TAG_D = UUID("51000000-0000-0000-0000-000000000004")
TAG_DIAG = UUID("51000000-0000-0000-0000-000000000005")
NODE_ID = UUID("51000000-0000-0000-0000-000000000010")
NOW = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


def _raw(
    tag_id: UUID,
    sequence: int,
    value: float,
    *,
    quality: TrunkQuality = TrunkQuality.GOOD,
) -> RawObservation:
    return RawObservation(
        observation_id=UUID(int=tag_id.int + sequence * 100),
        node_id=NODE_ID,
        tag_id=tag_id,
        source_key=f"node/{tag_id}",
        value=TypedValue.float(value),
        raw_unit="kW",
        quality=quality,
        source_timestamp=NOW + timedelta(milliseconds=sequence),
        received_at=NOW + timedelta(milliseconds=sequence),
        source_message_id=f"message-{tag_id}-{sequence}",
        source_sequence=sequence,
        source_digest=f"{sequence:064x}",
        event_time_basis="observed_at",
        source_order=SourceOrder.sequence(sequence),
    )


def _raw_received(
    tag_id: UUID,
    value: float,
    *,
    received_at: datetime = NOW,
    ordinal: int = 1,
) -> RawObservation:
    return RawObservation(
        observation_id=UUID(int=tag_id.int + ordinal * 100),
        node_id=NODE_ID,
        tag_id=tag_id,
        source_key=f"node/{tag_id}",
        value=TypedValue.float(value),
        raw_unit="kW",
        quality=TrunkQuality.GOOD,
        source_timestamp=received_at,
        received_at=received_at,
        source_message_id=f"received-{tag_id}-{ordinal}",
        source_sequence=None,
        source_digest=f"{ordinal:064x}",
        event_time_basis="received_at",
        source_order=SourceOrder.received_at(received_at, ordinal),
    )


def _ready_board(
    *,
    active_tags: tuple[UUID, ...] = (TAG_A,),
    required_tags: tuple[UUID, ...] = (TAG_A,),
) -> RealtimeBlackboard:
    board = RealtimeBlackboard(
        active_input_contracts={
            tag_id: SourceOrderMode.SEQUENCE for tag_id in active_tags
        },
        required_tag_ids=frozenset(required_tags),
    )
    board.accept_many(
        tuple(_raw(tag_id, 1, float(index * 10)) for index, tag_id in enumerate(active_tags, 1))
    )
    return board


class RealtimeBlackboardTest(unittest.TestCase):
    def test_restart_uses_complete_committed_baseline_without_waiting_for_sparse_inputs(self):
        board = RealtimeBlackboard(
            active_input_contracts={
                TAG_A: SourceOrderMode.SEQUENCE,
                TAG_B: SourceOrderMode.SEQUENCE,
            },
            required_tag_ids=frozenset({TAG_A, TAG_B}),
            capture_beat=20,
        )
        board.restore(
            (
                FramedRawObservation(
                    observation=_raw(TAG_A, 1, 10.0),
                    accepted_beat=20,
                    effective_quality=TrunkQuality.GOOD,
                ),
                FramedRawObservation(
                    observation=_raw(TAG_B, 1, 20.0),
                    accepted_beat=10,
                    effective_quality=TrunkQuality.GOOD,
                ),
            ),
            configuration_revision=7,
        )

        self.assertEqual(BlackboardState.READY, board.state)
        recovered = board.tick(NOW, configuration_revision=7)

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(TrunkQuality.GOOD, recovered.cells[TAG_A].effective_quality)
        self.assertEqual(TrunkQuality.STALE, recovered.cells[TAG_B].effective_quality)
        self.assertEqual((), recovered.changed_l0)

    def test_warms_until_required_inputs_arrive_and_keeps_last_sample_per_beat(self):
        board = RealtimeBlackboard(
            active_input_contracts={
                TAG_A: SourceOrderMode.SEQUENCE,
                TAG_B: SourceOrderMode.SEQUENCE,
            },
            required_tag_ids=frozenset({TAG_A, TAG_B}),
        )
        receipt = board.accept_many((_raw(TAG_A, 1, 10.0),))
        self.assertEqual((1, 0), (receipt.accepted_count, receipt.dropped_count))
        self.assertEqual(BlackboardState.WARMING, board.state)
        self.assertIsNone(board.tick(NOW, configuration_revision=7))

        receipt = board.accept_many(
            (_raw(TAG_A, 2, 20.0), _raw(TAG_A, 1, 99.0), _raw(TAG_B, 1, 30.0))
        )
        self.assertEqual((2, 1), (receipt.accepted_count, receipt.dropped_count))
        frozen = board.tick(NOW + timedelta(seconds=1), configuration_revision=7)

        self.assertIsNotNone(frozen)
        assert frozen is not None
        self.assertEqual(BlackboardState.READY, board.state)
        self.assertEqual(20.0, frozen.cells[TAG_A].observation.value.value)
        self.assertEqual(
            (TAG_A, TAG_B),
            tuple(item.observation.tag_id for item in frozen.changed_l0),
        )

    def test_same_value_new_sample_writes_the_next_frame_with_fresh_evidence(self):
        board = _ready_board()
        first = board.tick(NOW, configuration_revision=3)
        assert first is not None
        board.acknowledge(first.generation)

        board.accept_many((_raw(TAG_A, 2, 10.0),))

        second = board.tick(
            NOW + timedelta(seconds=1), configuration_revision=3
        )

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(
            _raw(TAG_A, 2, 10.0).observation_id,
            second.cells[TAG_A].observation.observation_id,
        )
        self.assertEqual(
            (TAG_A,),
            tuple(item.observation.tag_id for item in second.changed_l0),
        )
        board.acknowledge(second.generation)
        self.assertIsNone(
            board.tick(NOW + timedelta(seconds=2), configuration_revision=3)
        )

    def test_third_missed_beat_emits_one_stale_frame_without_erasing_value(self):
        board = _ready_board()
        first = board.tick(NOW, configuration_revision=3)
        assert first is not None
        board.acknowledge(first.generation)
        self.assertIsNone(board.tick(NOW + timedelta(seconds=1), configuration_revision=3))
        self.assertIsNone(board.tick(NOW + timedelta(seconds=2), configuration_revision=3))

        stale = board.tick(NOW + timedelta(seconds=3), configuration_revision=3)

        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertEqual(TrunkQuality.STALE, stale.cells[TAG_A].effective_quality)
        self.assertEqual(10.0, stale.cells[TAG_A].observation.value.value)
        self.assertEqual((), stale.changed_l0)
        board.acknowledge(stale.generation)
        self.assertIsNone(board.tick(NOW + timedelta(seconds=4), configuration_revision=3))

    def test_new_sample_after_stale_restores_its_actual_quality(self):
        board = _ready_board()
        first = board.tick(NOW, configuration_revision=3)
        assert first is not None
        board.acknowledge(first.generation)
        board.tick(NOW + timedelta(seconds=1), configuration_revision=3)
        board.tick(NOW + timedelta(seconds=2), configuration_revision=3)
        stale = board.tick(NOW + timedelta(seconds=3), configuration_revision=3)
        assert stale is not None
        board.acknowledge(stale.generation)

        board.accept_many(
            (_raw(TAG_A, 2, 11.0, quality=TrunkQuality.UNCERTAIN),)
        )
        restored = board.tick(NOW + timedelta(seconds=4), configuration_revision=3)

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(
            TrunkQuality.UNCERTAIN,
            restored.cells[TAG_A].effective_quality,
        )
        self.assertEqual((TAG_A,), tuple(item.observation.tag_id for item in restored.changed_l0))

    def test_updates_after_freeze_are_reserved_for_the_next_frame(self):
        board = _ready_board()
        frozen = board.tick(NOW, configuration_revision=2)
        assert frozen is not None

        board.accept_many((_raw(TAG_A, 2, 40.0),))

        self.assertEqual(10.0, frozen.cells[TAG_A].observation.value.value)
        self.assertIs(frozen, board.tick(NOW + timedelta(milliseconds=10), configuration_revision=2))
        board.acknowledge(frozen.generation)
        next_frame = board.tick(NOW + timedelta(seconds=1), configuration_revision=2)
        self.assertIsNotNone(next_frame)
        assert next_frame is not None
        self.assertEqual(40.0, next_frame.cells[TAG_A].observation.value.value)

    def test_frozen_mapping_is_immutable(self):
        frozen = _ready_board().tick(NOW, configuration_revision=2)
        assert frozen is not None
        with self.assertRaises(TypeError):
            frozen.cells[TAG_B] = frozen.cells[TAG_A]  # type: ignore[index]

    def test_optional_active_diagnostic_does_not_block_ready_and_enters_later_frame(self):
        board = RealtimeBlackboard(
            active_input_contracts={
                TAG_A: SourceOrderMode.SEQUENCE,
                TAG_DIAG: SourceOrderMode.SEQUENCE,
            },
            required_tag_ids=frozenset({TAG_A}),
        )
        board.accept_many((_raw(TAG_A, 1, 10.0),))
        first = board.tick(NOW, configuration_revision=7)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertNotIn(TAG_DIAG, first.cells)
        board.acknowledge(first.generation)

        board.accept_many((_raw(TAG_DIAG, 1, 99.0),))
        second = board.tick(NOW + timedelta(seconds=1), configuration_revision=7)

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(99.0, second.cells[TAG_DIAG].observation.value.value)
        self.assertIn(TAG_DIAG, {item.observation.tag_id for item in second.changed_l0})

    def test_revision_reset_reclassifies_candidates_and_rewarms_changed_inputs(self):
        board = _ready_board(
            active_tags=(TAG_A, TAG_B, TAG_D, TAG_DIAG),
            required_tags=(TAG_A, TAG_B),
        )
        board.accept_many(
            (
                _raw(TAG_A, 2, 11.0),
                _raw(TAG_B, 2, 22.0),
                _raw(TAG_D, 2, 44.0),
                _raw(TAG_DIAG, 2, 99.0),
            )
        )

        board.reset_revision(
            revision=8,
            active_input_contracts={
                TAG_A: SourceOrderMode.SEQUENCE,
                TAG_B: SourceOrderMode.RECEIVED_AT,
                TAG_C: SourceOrderMode.SEQUENCE,
                TAG_DIAG: SourceOrderMode.SEQUENCE,
            },
            required_tag_ids=frozenset({TAG_A, TAG_B, TAG_C}),
        )

        self.assertEqual(BlackboardState.WARMING, board.state)
        self.assertEqual(frozenset({TAG_B, TAG_C}), board.missing_required_tags)
        board.accept_many((_raw_received(TAG_B, 23.0), _raw(TAG_C, 1, 33.0)))
        first_new = board.tick(NOW + timedelta(seconds=1), configuration_revision=8)
        self.assertIsNotNone(first_new)
        assert first_new is not None
        self.assertEqual({TAG_A, TAG_B, TAG_C, TAG_DIAG}, set(first_new.cells))
        self.assertEqual(11.0, first_new.cells[TAG_A].observation.value.value)
        self.assertEqual(99.0, first_new.cells[TAG_DIAG].observation.value.value)
        self.assertNotIn(TAG_D, first_new.cells)

    def test_required_tags_must_be_active(self):
        with self.assertRaisesRegex(ValueError, "required tags must be active"):
            RealtimeBlackboard(
                active_input_contracts={TAG_A: SourceOrderMode.SEQUENCE},
                required_tag_ids=frozenset({TAG_B}),
            )

    def test_equal_or_older_sequence_is_dropped(self):
        board = _ready_board()
        receipt = board.accept_many((_raw(TAG_A, 1, 20.0), _raw(TAG_A, 0, 30.0)))
        self.assertEqual((0, 2), (receipt.accepted_count, receipt.dropped_count))
        frame = board.tick(NOW, configuration_revision=1)
        assert frame is not None
        self.assertEqual(10.0, frame.cells[TAG_A].observation.value.value)

    def test_source_order_mode_mismatch_is_rejected_without_mutating_cell(self):
        board = _ready_board()
        before = board.accept_many((_raw(TAG_A, 2, 20.0),))
        self.assertEqual(1, before.accepted_count)
        mismatched = _raw_received(TAG_A, 99.0, received_at=NOW, ordinal=3)

        with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_SOURCE_ORDER_MODE_MISMATCH"):
            board.accept_many((mismatched,))

        frame = board.tick(NOW, configuration_revision=1)
        assert frame is not None
        self.assertEqual(20.0, frame.cells[TAG_A].observation.value.value)

    def test_source_order_normalizes_equivalent_timezones(self):
        utc = datetime(2026, 8, 27, 1, 2, 3, 456789, tzinfo=timezone.utc)
        east_eight = utc.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(
            SourceOrder.observed_at(utc, 4, "a"),
            SourceOrder.observed_at(east_eight, 4, "a"),
        )

    def test_received_at_ordinal_breaks_same_microsecond_tie_after_recovery(self):
        recovered = SourceOrder.received_at(NOW, 41, "old")
        next_value = SourceOrder.received_at(NOW, 42, "new")
        self.assertTrue(next_value.is_after(recovered))
        self.assertFalse(recovered.is_after(next_value))

    def test_candidate_identity_and_digest_are_stable_until_acknowledged(self):
        board = _ready_board()
        first = board.tick(NOW, configuration_revision=9)
        retry = board.tick(NOW + timedelta(seconds=1), configuration_revision=9)
        self.assertIs(first, retry)
        assert first is not None and retry is not None
        self.assertEqual(first.frame_id, retry.frame_id)
        self.assertEqual(first.candidate_digest, retry.candidate_digest)
        self.assertEqual(64, len(first.candidate_digest))

    def test_wrong_generation_cannot_acknowledge_frozen_candidate(self):
        board = _ready_board()
        frozen = board.tick(NOW, configuration_revision=9)
        assert frozen is not None
        with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_GENERATION_MISMATCH"):
            board.acknowledge(frozen.generation + 1)
        self.assertIs(frozen, board.tick(NOW + timedelta(seconds=1), configuration_revision=9))


if __name__ == "__main__":
    unittest.main()
