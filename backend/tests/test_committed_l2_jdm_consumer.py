"""Behavior tests for the thin committed-L2 strategy adapter."""
from __future__ import annotations

from datetime import UTC, datetime
import unittest
from uuid import UUID, uuid4

from app.services.committed_l2_jdm_consumer import CommittedL2JdmConsumer
from app.services.data_trunk_contracts import FrameStatus, TrunkQuality, TypedValue
from app.services.data_trunk_outbox import CommittedL2Change, FrameOutboxEvent


NOW = datetime(2026, 9, 4, 2, 30, tzinfo=UTC)
FIRST_ID = UUID("72000000-0000-0000-0000-000000000001")
SECOND_ID = UUID("72000000-0000-0000-0000-000000000002")


def _change(entity_id: UUID) -> CommittedL2Change:
    return CommittedL2Change(
        entity_instance_id=entity_id,
        event_id=uuid4(),
        value=TypedValue.float(12.0),
        quality=TrunkQuality.GOOD,
        reason=None,
        observed_at=NOW,
    )


def _event(*changes: CommittedL2Change) -> FrameOutboxEvent:
    return FrameOutboxEvent(
        frame_id=UUID("72000000-0000-0000-0000-000000000010"),
        frame_sequence=42,
        status=FrameStatus.COMPLETE,
        configuration_revision=7,
        l0_changes=(),
        l2_changes=changes,
        failure_id=None,
        failure_code=None,
        frame_time=NOW,
    )


class _Runtime:
    def __init__(self) -> None:
        self.calls = []

    def evaluate_data_change(self, changed_entity_ids, trigger):
        self.calls.append((changed_entity_ids, trigger))
        return ()


class CommittedL2JdmConsumerTest(unittest.IsolatedAsyncioTestCase):
    async def test_frame_changes_only_locate_affected_strategies(self) -> None:
        runtime = _Runtime()
        consumer = CommittedL2JdmConsumer(runtime)

        await consumer.publish(
            _event(_change(FIRST_ID), _change(SECOND_ID), _change(FIRST_ID))
        )

        self.assertEqual(1, len(runtime.calls))
        changed_ids, trigger = runtime.calls[0]
        self.assertEqual((FIRST_ID, SECOND_ID), changed_ids)
        self.assertEqual("DATA_CHANGE", trigger.kind)
        self.assertEqual(
            "frame:72000000-0000-0000-0000-000000000010:42",
            trigger.trigger_key,
        )
        self.assertEqual(42, trigger.frame_sequence)
        self.assertEqual(NOW, trigger.evaluated_at)

    async def test_empty_l2_change_set_is_a_safe_noop_locator(self) -> None:
        runtime = _Runtime()

        await CommittedL2JdmConsumer(runtime).publish(_event())

        self.assertEqual((), runtime.calls[0][0])


if __name__ == "__main__":
    unittest.main()
