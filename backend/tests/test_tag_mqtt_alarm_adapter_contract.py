"""Ticket 13: tag and MQTT inputs only adapt observations for AlarmRuntime."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import UUID


ENTITY_INSTANCE_ID = UUID("72000000-0000-0000-0000-000000000001")
TAG_ID = UUID("72000000-0000-0000-0000-000000000002")
DEFINITION_ID = UUID("72000000-0000-0000-0000-000000000003")
STARTED_AT = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)


class TagMqttAlarmAdapterContractTest(unittest.TestCase):
    def _runtime(self, trigger_duration_seconds: float = 1):
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )

        definition = AlarmDefinition(
            id=DEFINITION_ID,
            asset_id="alarm.pcs.fault",
            version="1.0.0",
            entity_instance_id=ENTITY_INSTANCE_ID,
            entity_definition_id="pcs.faultCode",
            trigger={"op": "gt", "value": 0},
            trigger_duration_seconds=trigger_duration_seconds,
            recovery={"op": "lte", "value": 0},
            recovery_duration_seconds=1,
            severity="MAJOR",
            notification_throttle_seconds=60,
        )
        definitions = InMemoryAlarmDefinitionCatalog((definition,))
        repository = InMemoryAlarmRepository()
        return definitions, AlarmRuntime(definitions, repository), repository

    def _tag_adapter(self, definitions, runtime, sources=None):
        from app.services.tag_mqtt_alarm_adapter import (
            InMemoryTagAlarmSourceResolver,
            TagAlarmAdapter,
            TagAlarmSource,
        )

        return TagAlarmAdapter(
            definitions,
            runtime,
            sources or InMemoryTagAlarmSourceResolver(
                {
                    TAG_ID: TagAlarmSource(
                        tag_id=TAG_ID,
                        entity_instance_id=ENTITY_INSTANCE_ID,
                        tag_name="faultCode",
                        max_observation_gap_seconds=30,
                    )
                }
            ),
        )

    def _tag_trace(self) -> tuple[list[str], list[str], int, int]:
        from app.services.tag_mqtt_alarm_adapter import TagAlarmSample
        from app.services.alarm_runtime import AcknowledgeAlarm

        definitions, runtime, repository = self._runtime()
        adapter = self._tag_adapter(definitions, runtime)
        outcomes = [
            *adapter.submit(TagAlarmSample(TAG_ID, STARTED_AT, 1, 192)),
            *adapter.submit(
                TagAlarmSample(TAG_ID, STARTED_AT + timedelta(seconds=1), 1, 192)
            ),
            runtime.acknowledge(
                AcknowledgeAlarm(
                    event_id=repository.list_events()[0].id,
                    actor="user:operator",
                    acknowledged_at=STARTED_AT + timedelta(seconds=1),
                )
            ),
            *adapter.submit(
                TagAlarmSample(TAG_ID, STARTED_AT + timedelta(seconds=2), 0, 192)
            ),
            *adapter.submit(
                TagAlarmSample(TAG_ID, STARTED_AT + timedelta(seconds=3), 0, 192)
            ),
        ]
        event = repository.list_events()[0]
        return (
            [outcome.code for outcome in outcomes],
            [item.code for item in runtime.timeline(event.id)],
            len(repository.list_events()),
            len(repository.notifications()),
        )

    def _mqtt_trace(self) -> tuple[list[str], list[str], int, int]:
        from app.services.tag_mqtt_alarm_adapter import MqttAlarmAdapter
        from app.services.alarm_runtime import AcknowledgeAlarm

        definitions, runtime, repository = self._runtime()
        tag_adapter = self._tag_adapter(definitions, runtime)
        adapter = MqttAlarmAdapter(tag_adapter, {"faultCode": TAG_ID})
        topic = "/alarm/pcs-01"
        outcomes = [
            *adapter.submit(topic, b'{"quality":192,"error1":{"faultCode":1}}', STARTED_AT),
            *adapter.submit(
                topic,
                b'{"quality":192,"error1":{"faultCode":1}}',
                STARTED_AT + timedelta(seconds=1),
            ),
            runtime.acknowledge(
                AcknowledgeAlarm(
                    event_id=repository.list_events()[0].id,
                    actor="user:operator",
                    acknowledged_at=STARTED_AT + timedelta(seconds=1),
                )
            ),
            *adapter.submit(
                topic,
                b'{"quality":192,"error1":{"faultCode":0}}',
                STARTED_AT + timedelta(seconds=2),
            ),
            *adapter.submit(
                topic,
                b'{"quality":192,"error1":{"faultCode":0}}',
                STARTED_AT + timedelta(seconds=3),
            ),
        ]
        event = repository.list_events()[0]
        transitions = runtime.timeline(event.id)
        self.assertEqual({"mqtt"}, {item.evidence["source_kind"] for item in transitions if item.evidence})
        return (
            [outcome.code for outcome in outcomes],
            [item.code for item in transitions],
            len(repository.list_events()),
            len(repository.notifications()),
        )

    def _entity_trace(self) -> tuple[list[str], list[str], int, int]:
        from app.services.alarm_runtime import AcknowledgeAlarm
        from app.services.entity_alarm_adapter import EntityAlarmAdapter
        from app.services.entity_instance_runtime import EntityInstanceObservation

        definitions, runtime, repository = self._runtime()

        class FixedEntityRuntime:
            def __init__(self) -> None:
                self.value = 1
                self.observed_at = STARTED_AT

            def read_for_alarm(self, entity_instance_id: UUID) -> EntityInstanceObservation:
                if entity_instance_id != ENTITY_INSTANCE_ID:
                    raise AssertionError("Adapter requested an unexpected entity instance")
                return EntityInstanceObservation(
                    entity_instance_id=ENTITY_INSTANCE_ID,
                    definition_id="pcs.faultCode",
                    instance_key="PCS-01",
                    value=self.value,
                    data_type="INT",
                    unit=None,
                    observed_at=self.observed_at,
                    quality=192,
                    age_ms=0,
                    fresh=True,
                    quality_good=True,
                    max_observation_gap_seconds=30,
                )

        entity_runtime = FixedEntityRuntime()
        adapter = EntityAlarmAdapter(definitions, entity_runtime, runtime)
        outcomes = [
            *adapter.submit_entity(ENTITY_INSTANCE_ID),
        ]
        entity_runtime.observed_at += timedelta(seconds=1)
        outcomes.extend(adapter.submit_entity(ENTITY_INSTANCE_ID))
        outcomes.append(
            runtime.acknowledge(
                AcknowledgeAlarm(
                    event_id=repository.list_events()[0].id,
                    actor="user:operator",
                    acknowledged_at=entity_runtime.observed_at,
                )
            )
        )
        entity_runtime.value = 0
        entity_runtime.observed_at += timedelta(seconds=1)
        outcomes.extend(adapter.submit_entity(ENTITY_INSTANCE_ID))
        entity_runtime.observed_at += timedelta(seconds=1)
        outcomes.extend(adapter.submit_entity(ENTITY_INSTANCE_ID))
        event = repository.list_events()[0]
        transitions = runtime.timeline(event.id)
        self.assertEqual(
            {"entity_instance"},
            {item.evidence["source_kind"] for item in transitions if item.evidence},
        )
        return (
            [outcome.code for outcome in outcomes],
            [item.code for item in transitions],
            len(repository.list_events()),
            len(repository.notifications()),
        )

    def test_tag_and_mqtt_sources_share_one_lifecycle_without_sampling_storm(self) -> None:
        tag_trace = self._tag_trace()
        mqtt_trace = self._mqtt_trace()
        entity_trace = self._entity_trace()

        self.assertEqual(tag_trace, mqtt_trace)
        self.assertEqual(tag_trace, entity_trace)
        self.assertEqual(
            [
                "ALARM_TRIGGER_PENDING",
                "ALARM_ACTIVATED",
                "ALARM_ACKNOWLEDGED",
                "ALARM_RECOVERY_PENDING",
                "ALARM_RECOVERED",
            ],
            tag_trace[0],
        )
        self.assertEqual(1, tag_trace[2])
        self.assertEqual(1, tag_trace[3])

    def test_pipeline_no_longer_invokes_legacy_tag_or_mqtt_writers(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "pipeline.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("process_alarm_message", source)
        self.assertNotIn("process_tag_alarms", source)
        self.assertIn("_submit_unified_tag_alarms", source)

    def test_pipeline_submits_only_persisted_confirmed_tag_and_mqtt_observations(self) -> None:
        from app.services.data_trunk_contracts import (
            CommitReceipt,
            RawObservation,
            TrunkQuality,
            TypedValue,
            ValueKind,
        )
        from app.services.pipeline import DataPipeline
        from app.services.tag_mqtt_alarm_adapter import MqttAlarmAdapter

        class RecordingDataTrunk:
            def __init__(self) -> None:
                self.calls = []

            def ingest(self, batch):
                self.calls.append(tuple(batch))
                return CommitReceipt(
                    transaction_id=UUID("72000000-0000-0000-0000-000000000099"),
                    accepted_l0_count=len(batch),
                    duplicate_l0_count=0,
                    l2_event_ids=(),
                    late_observation_count=0,
                )

        definitions, runtime, repository = self._runtime(trigger_duration_seconds=1)
        data_trunk = RecordingDataTrunk()
        pipeline = DataPipeline(data_trunk=data_trunk)
        class RecordingEntityAdapter:
            def __init__(self) -> None:
                self.excluded_entity_instance_ids: set[UUID] | None = None

            def submit_all(self, *, exclude_entity_instance_ids: set[UUID]):
                self.excluded_entity_instance_ids = exclude_entity_instance_ids
                return ()

        entity_adapter = RecordingEntityAdapter()
        pipeline._entity_alarm_adapter = entity_adapter
        from app.services.tag_mqtt_alarm_adapter import TagAlarmSource

        pipeline._tag_alarm_sources.replace(
            {
                TAG_ID: TagAlarmSource(
                    tag_id=TAG_ID,
                    entity_instance_id=ENTITY_INSTANCE_ID,
                    tag_name="faultCode",
                    max_observation_gap_seconds=30,
                )
            }
        )
        pipeline._tag_alarm_adapter = self._tag_adapter(
            definitions,
            runtime,
            pipeline._tag_alarm_sources,
        )
        pipeline._mqtt_alarm_adapter = MqttAlarmAdapter(
            pipeline._tag_alarm_adapter,
            {"faultCode": TAG_ID},
        )
        pipeline._mqtt = SimpleNamespace(is_alarm_topic=lambda topic: topic.startswith("/alarm/"))
        pipeline._buffer.append(
            RawObservation(
                observation_id=UUID("72000000-0000-0000-0000-000000000031"),
                node_id=UUID("72000000-0000-0000-0000-000000000004"),
                tag_id=TAG_ID,
                source_key="pcs/faultCode",
                value=TypedValue(ValueKind.INT, 1),
                raw_unit=None,
                quality=TrunkQuality.GOOD,
                source_timestamp=STARTED_AT + timedelta(seconds=1),
                received_at=STARTED_AT + timedelta(seconds=1),
                source_message_id="message-2",
                source_sequence=2,
                source_digest="2" * 64,
                event_time_basis="observed_at",
            )
        )
        pipeline._buffer.append(
            RawObservation(
                observation_id=UUID("72000000-0000-0000-0000-000000000030"),
                node_id=UUID("72000000-0000-0000-0000-000000000004"),
                tag_id=TAG_ID,
                source_key="pcs/faultCode",
                value=TypedValue(ValueKind.INT, 1),
                raw_unit=None,
                quality=TrunkQuality.GOOD,
                source_timestamp=STARTED_AT,
                received_at=STARTED_AT,
                source_message_id="message-1",
                source_sequence=1,
                source_digest="1" * 64,
                event_time_basis="observed_at",
            )
        )

        import asyncio

        asyncio.run(pipeline.flush_now())

        self.assertEqual(1, len(repository.list_events()))
        self.assertEqual("active_unacknowledged", repository.list_events()[0].state)
        self.assertEqual({ENTITY_INSTANCE_ID}, entity_adapter.excluded_entity_instance_ids)
        self.assertEqual(1, len(data_trunk.calls))

        asyncio.run(
            pipeline.on_message(
                SimpleNamespace(
                    topic="/alarm/pcs-01",
                    payload=b'{"quality":192,"error1":{"faultCode":1}}',
                    qos=0,
                )
            )
        )
        self.assertEqual(
            "mqtt",
            repository.list_events()[0].last_observation["source_kind"],
        )

        unmapped_definitions, unmapped_runtime, unmapped_repository = self._runtime(
            trigger_duration_seconds=0
        )
        unmapped_pipeline = DataPipeline()
        unmapped_pipeline._tag_alarm_adapter = self._tag_adapter(
            unmapped_definitions,
            unmapped_runtime,
        )
        unmapped_pipeline._mqtt_alarm_adapter = MqttAlarmAdapter(
            unmapped_pipeline._tag_alarm_adapter,
            {},
        )
        unmapped_pipeline._mqtt = pipeline._mqtt
        asyncio.run(
            unmapped_pipeline.on_message(
                SimpleNamespace(
                    topic="/alarm/ambiguous",
                    payload=b'{"quality":192,"error1":{"faultCode":1}}',
                    qos=0,
                )
            )
        )
        self.assertEqual((), unmapped_repository.list_events())

    def test_mqtt_without_good_quality_cannot_recover_an_active_event(self) -> None:
        from app.services.tag_mqtt_alarm_adapter import MqttAlarmAdapter, TagAlarmSample

        definitions, runtime, repository = self._runtime(trigger_duration_seconds=0)
        tag_adapter = self._tag_adapter(definitions, runtime)
        tag_adapter.submit(TagAlarmSample(TAG_ID, STARTED_AT, 1, 192))
        adapter = MqttAlarmAdapter(tag_adapter, {"faultCode": TAG_ID})

        outcomes = adapter.submit(
            "/alarm/pcs-01",
            b'{"error1":{"faultCode":0}}',
            STARTED_AT + timedelta(seconds=1),
        )

        self.assertEqual(["ALARM_STILL_ACTIVE"], [item.code for item in outcomes])
        self.assertEqual("active_unacknowledged", repository.list_events()[0].state)


if __name__ == "__main__":
    unittest.main()
