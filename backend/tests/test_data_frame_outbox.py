from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import unittest
from uuid import uuid4


os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")

from app.services.data_trunk_contracts import (
    FrameStatus,
    TrunkQuality,
    TypedValue,
)
from app.services.data_trunk_outbox import (
    CommittedL0Change,
    CommittedL2Change,
    CommittedFrameFanout,
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


class _CallbackPublisher:
    def __init__(self, callback) -> None:
        self._callback = callback

    async def publish(self, event: FrameOutboxEvent) -> None:
        self._callback(event)


class _FailingPublisher:
    def __init__(self, calls: list[str], name: str) -> None:
        self._calls = calls
        self._name = name

    async def publish(self, event: FrameOutboxEvent) -> None:
        del event
        self._calls.append(self._name)
        raise RuntimeError("consumer offline")


class DataFrameOutboxTest(unittest.IsolatedAsyncioTestCase):
    def test_l2_retained_value_time_survives_public_payload_round_trip(self) -> None:
        change = CommittedL2Change(
            entity_instance_id=uuid4(),
            event_id=uuid4(),
            value=TypedValue.boolean(False),
            quality=TrunkQuality.BAD,
            reason="TYPE_MISMATCH",
            observed_at=NOW + timedelta(seconds=1),
            value_observed_at=NOW,
        )

        restored = CommittedL2Change.from_public_dict(change.public_dict())

        self.assertEqual(TypedValue.boolean(False), restored.value)
        self.assertEqual(TrunkQuality.BAD, restored.quality)
        self.assertEqual(NOW, restored.value_observed_at)

    def test_l0_quality_reason_survives_public_payload_round_trip(self) -> None:
        change = CommittedL0Change(
            tag_id=uuid4(),
            observation_id=uuid4(),
            value=TypedValue.integer(2),
            source_quality=TrunkQuality.BAD,
            effective_quality=TrunkQuality.BAD,
            source_timestamp=NOW,
            received_at=NOW,
            accepted_beat=10,
            quality_reason="BIT_VALUE_OUT_OF_RANGE",
        )

        restored = CommittedL0Change.from_public_dict(change.public_dict())

        self.assertEqual("BIT_VALUE_OUT_OF_RANGE", restored.quality_reason)
        self.assertEqual(TypedValue.integer(2), restored.value)

    async def test_production_fanout_delivers_alarm_jdm_then_stream(self) -> None:
        from app.main import build_committed_frame_fanout

        calls: list[str] = []
        event = _event(10)
        fanout = build_committed_frame_fanout(
            _CallbackPublisher(lambda _value: calls.append("alarm")),
            _CallbackPublisher(lambda _value: calls.append("jdm")),
            _CallbackPublisher(lambda _value: calls.append("stream")),
        )

        await fanout.publish(event)

        self.assertEqual(["alarm", "jdm", "stream"], calls)

    async def test_fanout_delivers_one_frame_in_registration_order(self) -> None:
        calls: list[tuple[str, int]] = []
        event = _event(10)

        await CommittedFrameFanout(
            (
                _CallbackPublisher(
                    lambda value: calls.append(("alarm", value.frame_sequence))
                ),
                _CallbackPublisher(
                    lambda value: calls.append(("stream", value.frame_sequence))
                ),
            )
        ).publish(event)

        self.assertEqual([("alarm", 10), ("stream", 10)], calls)

    async def test_fanout_failure_stops_later_consumers_and_keeps_head(self) -> None:
        event = _event(10)
        repository = InMemoryFrameOutboxRepository((event,), clock=lambda: NOW)
        calls: list[object] = []
        fanout = CommittedFrameFanout(
            (
                _FailingPublisher(calls, "alarm"),
                _CallbackPublisher(
                    lambda value: calls.append(("stream", value.frame_sequence))
                ),
            )
        )

        dispatched = await FrameOutboxDispatcher(repository, fanout).run_once(
            now=NOW
        )

        self.assertEqual(0, dispatched)
        self.assertEqual(["alarm"], calls)
        self.assertEqual((), repository.published_ids)

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
