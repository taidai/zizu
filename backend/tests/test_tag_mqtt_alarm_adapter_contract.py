"""Ticket 13: tag and MQTT inputs only adapt observations for AlarmRuntime."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from uuid import UUID


ENTITY_INSTANCE_ID = UUID("72000000-0000-0000-0000-000000000001")
TAG_ID = UUID("72000000-0000-0000-0000-000000000002")
DEFINITION_ID = UUID("72000000-0000-0000-0000-000000000003")
STARTED_AT = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)


class TagMqttAlarmAdapterContractTest(unittest.TestCase):
    def _runtime(self):
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
            trigger_duration_seconds=1,
            recovery={"op": "lte", "value": 0},
            recovery_duration_seconds=1,
            severity="MAJOR",
            notification_throttle_seconds=60,
        )
        definitions = InMemoryAlarmDefinitionCatalog((definition,))
        repository = InMemoryAlarmRepository()
        return definitions, AlarmRuntime(definitions, repository), repository

    def _tag_adapter(self, definitions, runtime):
        from app.services.tag_mqtt_alarm_adapter import (
            InMemoryTagAlarmSourceResolver,
            TagAlarmAdapter,
            TagAlarmSource,
        )

        return TagAlarmAdapter(
            definitions,
            runtime,
            InMemoryTagAlarmSourceResolver(
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
            *adapter.submit(topic, b'{"error1":{"faultCode":1}}', STARTED_AT),
            *adapter.submit(
                topic,
                b'{"error1":{"faultCode":1}}',
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
                b'{"error1":{"faultCode":0}}',
                STARTED_AT + timedelta(seconds=2),
            ),
            *adapter.submit(
                topic,
                b'{"error1":{"faultCode":0}}',
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


if __name__ == "__main__":
    unittest.main()
