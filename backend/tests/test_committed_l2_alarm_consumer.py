from __future__ import annotations

from datetime import UTC, datetime
import unittest
from uuid import UUID

from app.services.alarm_runtime import (
    AlarmDefinition,
    AlarmRuntime,
    AlarmRuntimeError,
    InMemoryAlarmDefinitionCatalog,
    InMemoryAlarmRepository,
)
from app.services.committed_l2_alarm_consumer import CommittedL2AlarmConsumer
from app.services.data_trunk_contracts import FrameStatus, TrunkQuality, TypedValue
from app.services.data_trunk_outbox import (
    CommittedL0Change,
    CommittedL2Change,
    FrameOutboxEvent,
)


NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
FRAME_ID = UUID("81000000-0000-0000-0000-000000000001")
NODE_ID = UUID("81000000-0000-0000-0000-000000000002")
TAG_ID = UUID("81000000-0000-0000-0000-000000000003")
ENTITY_A = UUID("81000000-0000-0000-0000-000000000004")
ENTITY_B = UUID("81000000-0000-0000-0000-000000000005")
DEFINITION_A = UUID("81000000-0000-0000-0000-000000000006")
DEFINITION_B = UUID("81000000-0000-0000-0000-000000000007")
L2_EVENT_A = UUID("81000000-0000-0000-0000-000000000008")
L2_EVENT_B = UUID("81000000-0000-0000-0000-000000000009")
PROCESSING_REVISION = UUID("81000000-0000-0000-0000-000000000010")


def _definition(
    definition_id: UUID,
    entity_id: UUID,
    *,
    trigger: dict | None = None,
    recovery: dict | None = None,
) -> AlarmDefinition:
    return AlarmDefinition(
        id=definition_id,
        asset_id=f"alarm.test.{entity_id}",
        version="7",
        entity_instance_id=entity_id,
        entity_definition_id="grid.activePower",
        trigger=trigger or {"op": "gt", "value": 10},
        trigger_duration_seconds=0,
        recovery=recovery or {"op": "lte", "value": 9},
        recovery_duration_seconds=0,
        severity="MAJOR",
        notification_throttle_seconds=60,
    )


def _l2_change(
    entity_id: UUID = ENTITY_A,
    event_id: UUID = L2_EVENT_A,
    *,
    value: int = 11,
    quality: TrunkQuality = TrunkQuality.GOOD,
) -> CommittedL2Change:
    return CommittedL2Change(
        entity_instance_id=entity_id,
        event_id=event_id,
        value=TypedValue.float(value),
        quality=quality,
        reason=None if quality is TrunkQuality.GOOD else "SOURCE_STALE",
        node_id=NODE_ID,
        unit="kW",
        observed_at=NOW,
        received_at=NOW,
        calculated_at=NOW,
        processing_revision_id=PROCESSING_REVISION,
        source_digest="a" * 64,
    )


def _frame(
    *changes: CommittedL2Change,
    frame_id: UUID = FRAME_ID,
) -> FrameOutboxEvent:
    return FrameOutboxEvent(
        frame_id=frame_id,
        frame_sequence=41,
        status=FrameStatus.COMPLETE,
        configuration_revision=7,
        l0_changes=(
            CommittedL0Change(
                tag_id=TAG_ID,
                observation_id=UUID("81000000-0000-0000-0000-000000000011"),
                value=TypedValue.float(999),
                source_quality=TrunkQuality.GOOD,
                effective_quality=TrunkQuality.GOOD,
                source_timestamp=NOW,
                received_at=NOW,
                accepted_beat=41,
                node_id=NODE_ID,
                unit="kW",
            ),
        ),
        l2_changes=changes or (_l2_change(),),
        failure_id=None,
        failure_code=None,
        frame_time=NOW,
    )


def _consumer(*definitions: AlarmDefinition):
    catalog = InMemoryAlarmDefinitionCatalog(definitions)
    repository = InMemoryAlarmRepository()
    runtime = AlarmRuntime(catalog, repository)
    return CommittedL2AlarmConsumer(catalog, runtime), repository


class _TargetedOpenEventRepository(InMemoryAlarmRepository):
    def __init__(self) -> None:
        super().__init__()
        self.open_event_queries: list[frozenset[UUID]] = []

    def list_events(self):
        raise AssertionError("committed-frame dispatch must not load every alarm event")

    def list_open_for_entities(
        self,
        entity_instance_ids: frozenset[UUID],
    ):
        self.open_event_queries.append(entity_instance_ids)
        return super().list_open_for_entities(entity_instance_ids)


class CommittedL2AlarmConsumerTest(unittest.IsolatedAsyncioTestCase):
    async def test_queries_open_events_only_for_entities_in_frame(self) -> None:
        catalog = InMemoryAlarmDefinitionCatalog(
            (_definition(DEFINITION_A, ENTITY_A),)
        )
        repository = _TargetedOpenEventRepository()
        consumer = CommittedL2AlarmConsumer(
            catalog,
            AlarmRuntime(catalog, repository),
        )

        await consumer.publish(_frame())

        self.assertEqual(
            [frozenset({ENTITY_A})],
            repository.open_event_queries,
        )
        self.assertEqual(1, len(repository.active_events()))

    async def test_submits_only_l2_with_complete_frame_evidence(self) -> None:
        consumer, repository = _consumer(_definition(DEFINITION_A, ENTITY_A))
        event = _frame()

        await consumer.publish(event)

        active = repository.active_events()[0]
        observation = active.last_observation
        self.assertEqual("committed_l2", observation["source_kind"])
        self.assertEqual(
            f"frame:{FRAME_ID}/entity:{ENTITY_A}", observation["source_ref"]
        )
        self.assertEqual(
            {
                "frame_id": str(FRAME_ID),
                "frame_sequence": 41,
                "configuration_revision": 7,
                "l2_event_id": str(L2_EVENT_A),
                "node_id": str(NODE_ID),
                "unit": "kW",
                "reason": None,
                "processing_revision_id": str(PROCESSING_REVISION),
                "source_digest": "a" * 64,
            },
            observation["evidence"],
        )
        self.assertEqual(11, observation["value"])
        self.assertNotEqual(999, observation["value"])

    async def test_same_frame_is_noop_after_atomic_alarm_commit(self) -> None:
        consumer, repository = _consumer(
            _definition(
                DEFINITION_A,
                ENTITY_A,
                trigger={"op": "eq", "value": 1},
                recovery={"op": "eq", "value": 1},
            )
        )
        event = _frame(_l2_change(value=1))

        await consumer.publish(event)
        first_events = repository.list_events()
        first_transitions = repository.transitions(first_events[0].id)
        await consumer.publish(event)

        self.assertEqual(first_events, repository.list_events())
        self.assertEqual(first_transitions, repository.transitions(first_events[0].id))
        self.assertTrue(repository.has_consumed_frame("alarm", event.frame_id))

    async def test_stale_l2_never_triggers_alarm(self) -> None:
        consumer, repository = _consumer(_definition(DEFINITION_A, ENTITY_A))

        await consumer.publish(
            _frame(_l2_change(value=999, quality=TrunkQuality.STALE))
        )

        self.assertEqual((), repository.list_events())
        self.assertTrue(repository.has_consumed_frame("alarm", FRAME_ID))

    async def test_invalid_second_evaluation_rolls_back_first_and_receipt(self) -> None:
        consumer, repository = _consumer(
            _definition(DEFINITION_A, ENTITY_A),
            _definition(
                DEFINITION_B,
                ENTITY_B,
                trigger={"op": "contains", "value": "fault"},
            ),
        )
        event = _frame(
            _l2_change(ENTITY_A, L2_EVENT_A),
            _l2_change(ENTITY_B, L2_EVENT_B),
        )

        with self.assertRaises(AlarmRuntimeError) as raised:
            await consumer.publish(event)
        self.assertEqual("ALARM_DEFINITION_INVALID", raised.exception.code)

        self.assertEqual((), repository.list_events())
        self.assertFalse(repository.has_consumed_frame("alarm", FRAME_ID))

    async def test_missing_observed_at_rejects_whole_frame(self) -> None:
        consumer, repository = _consumer(_definition(DEFINITION_A, ENTITY_A))
        invalid = _l2_change()
        invalid = CommittedL2Change(
            **{
                **invalid.__dict__,
                "observed_at": None,
            }
        )

        with self.assertRaises(AlarmRuntimeError) as raised:
            await consumer.publish(_frame(invalid))
        self.assertEqual(
            "ALARM_FRAME_OBSERVATION_INVALID",
            raised.exception.code,
        )

        self.assertEqual((), repository.list_events())
        self.assertFalse(repository.has_consumed_frame("alarm", FRAME_ID))


if __name__ == "__main__":
    unittest.main()
