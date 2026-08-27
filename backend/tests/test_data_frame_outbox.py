from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

from app.services.data_trunk_contracts import (
    FrameStatus,
    TrunkQuality,
    TypedValue,
)
from app.services.data_trunk_outbox import (
    CommittedL0Change,
    FrameOutboxDispatcher,
    FrameOutboxEvent,
    InMemoryFrameOutboxRepository,
)


NOW = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)


def _event(sequence: int = 10) -> FrameOutboxEvent:
    return FrameOutboxEvent(
        frame_id=uuid4(),
        frame_sequence=sequence,
        status=FrameStatus.COMPLETE,
        configuration_revision=4,
        l0_changes=(
            CommittedL0Change(
                tag_id=uuid4(),
                observation_id=uuid4(),
                value=TypedValue.float(10.0),
                source_quality=TrunkQuality.GOOD,
                effective_quality=TrunkQuality.GOOD,
                source_timestamp=NOW,
                received_at=NOW,
                accepted_beat=sequence,
            ),
        ),
        l2_changes=(),
        failure_id=None,
        failure_code=None,
    )


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[FrameOutboxEvent] = []

    async def publish(self, event: FrameOutboxEvent) -> None:
        self.events.append(event)


class _FailOncePublisher(_RecordingPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def publish(self, event: FrameOutboxEvent) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("offline")
        await super().publish(event)


class DataFrameOutboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_one_atomic_event_per_terminal_frame(self) -> None:
        event = _event()
        repository = InMemoryFrameOutboxRepository((event,), clock=lambda: NOW)
        publisher = _RecordingPublisher()

        dispatched = await FrameOutboxDispatcher(
            repository, publisher
        ).run_once(now=NOW)

        self.assertEqual(1, dispatched)
        self.assertEqual([event], publisher.events)
        self.assertEqual((event.frame_id,), repository.published_ids)

    async def test_failed_head_blocks_later_frame_until_retry(self) -> None:
        first = _event(10)
        second = _event(11)
        repository = InMemoryFrameOutboxRepository(
            (second, first), clock=lambda: NOW
        )
        publisher = _FailOncePublisher()
        dispatcher = FrameOutboxDispatcher(repository, publisher)

        self.assertEqual(0, await dispatcher.run_once(now=NOW))
        self.assertEqual(0, await dispatcher.run_once(now=NOW + timedelta(seconds=1)))
        self.assertEqual(1, await dispatcher.run_once(now=NOW + timedelta(seconds=2)))
        self.assertEqual([10], [event.frame_sequence for event in publisher.events])
        self.assertEqual(1, repository.attempts[first.frame_id])

    def test_outbox_event_does_not_embed_mutable_payload(self) -> None:
        event = _event()
        self.assertFalse(hasattr(event, "payload"))


if __name__ == "__main__":
    unittest.main()
