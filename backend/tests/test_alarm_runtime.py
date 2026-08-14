from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from uuid import UUID


DEFINITION_ID = UUID("70000000-0000-0000-0000-000000000001")
ENTITY_INSTANCE_ID = UUID("70000000-0000-0000-0000-000000000002")


class AlarmRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmObservation,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )

        self.observation_type = AlarmObservation
        self.started_at = datetime(2026, 8, 14, 9, tzinfo=timezone.utc)
        self.repository = InMemoryAlarmRepository()
        self.runtime = AlarmRuntime(
            definitions=InMemoryAlarmDefinitionCatalog(
                (
                    AlarmDefinition(
                        id=DEFINITION_ID,
                        asset_id="alarm.pcs.overpower",
                        version="1.0.0",
                        entity_instance_id=ENTITY_INSTANCE_ID,
                        entity_definition_id="pcs.activePower",
                        trigger={"op": "gt", "value": 100},
                        trigger_duration_seconds=10,
                        recovery={"op": "lte", "value": 90},
                        recovery_duration_seconds=5,
                        severity="MAJOR",
                        notification_throttle_seconds=60,
                    ),
                )
            ),
            repository=self.repository,
        )

    def observe(
        self,
        *,
        value: object,
        after_seconds: int,
        max_observation_gap_seconds: float | None = None,
    ):
        return self.runtime.submit(
            self.observation_type(
                definition_id=DEFINITION_ID,
                entity_instance_id=ENTITY_INSTANCE_ID,
                observed_at=self.started_at + timedelta(seconds=after_seconds),
                value=value,
                quality=192,
                source_kind="entity",
                source_ref="PCS-01.activePower",
                evidence={"sample": after_seconds},
                max_observation_gap_seconds=max_observation_gap_seconds,
            )
        )

    def test_continuous_fault_creates_one_active_event_and_one_notification(self) -> None:
        pending = self.observe(value=101, after_seconds=0)
        not_yet_active = self.observe(value=101, after_seconds=9)
        active = self.observe(value=101, after_seconds=10)
        repeated = self.observe(value=101, after_seconds=11)

        self.assertEqual("pending", pending.state)
        self.assertEqual("ALARM_TRIGGER_PENDING", pending.code)
        self.assertFalse(pending.notification_created)
        self.assertEqual(pending.event_id, not_yet_active.event_id)
        self.assertIsNone(not_yet_active.transition)

        self.assertEqual("active_unacknowledged", active.state)
        self.assertEqual("ALARM_ACTIVATED", active.code)
        self.assertEqual(
            {"from": "pending", "to": "active_unacknowledged"},
            active.transition,
        )
        self.assertTrue(active.notification_created)

        self.assertEqual(active.event_id, repeated.event_id)
        self.assertEqual("active_unacknowledged", repeated.state)
        self.assertIsNone(repeated.transition)
        self.assertFalse(repeated.notification_created)
        self.assertEqual(1, len(self.repository.active_events()))
        self.assertEqual(1, len(self.repository.notifications()))

    def test_acknowledgement_keeps_event_active_until_recovery_is_stable(self) -> None:
        from app.services.alarm_runtime import AcknowledgeAlarm

        self.observe(value=101, after_seconds=0)
        active = self.observe(value=101, after_seconds=10)
        acknowledged = self.runtime.acknowledge(
            AcknowledgeAlarm(
                event_id=active.event_id,
                actor="user:operator-1",
                acknowledged_at=self.started_at + timedelta(seconds=11),
                note="已知悉",
            )
        )
        recovery_pending = self.observe(value=90, after_seconds=12)
        jittered_active = self.observe(value=95, after_seconds=15)
        second_recovery_pending = self.observe(value=90, after_seconds=21)
        recovered = self.observe(value=90, after_seconds=26)

        self.assertEqual("active_acknowledged", acknowledged.state)
        self.assertEqual("ALARM_ACKNOWLEDGED", acknowledged.code)
        self.assertIsNotNone(acknowledged.audit_event_id)
        self.assertEqual("active_acknowledged", recovery_pending.state)
        self.assertEqual("ALARM_RECOVERY_PENDING", recovery_pending.code)
        self.assertEqual("active_acknowledged", jittered_active.state)
        self.assertEqual("ALARM_STILL_ACTIVE", jittered_active.code)
        self.assertEqual("ALARM_RECOVERY_PENDING", second_recovery_pending.code)
        self.assertEqual("recovered", recovered.state)
        self.assertEqual("ALARM_RECOVERED", recovered.code)
        self.assertEqual(active.event_id, recovered.event_id)

    def test_recovery_cannot_span_a_stale_observation_gap(self) -> None:
        self.observe(value=101, after_seconds=0, max_observation_gap_seconds=30)
        active = self.observe(value=101, after_seconds=10, max_observation_gap_seconds=30)
        recovery_pending = self.observe(
            value=90,
            after_seconds=12,
            max_observation_gap_seconds=30,
        )
        after_gap = self.observe(
            value=90,
            after_seconds=43,
            max_observation_gap_seconds=30,
        )
        second_recovery_pending = self.observe(
            value=90,
            after_seconds=44,
            max_observation_gap_seconds=30,
        )
        recovered = self.observe(
            value=90,
            after_seconds=49,
            max_observation_gap_seconds=30,
        )

        self.assertEqual("ALARM_RECOVERY_PENDING", recovery_pending.code)
        self.assertEqual("ALARM_STILL_ACTIVE", after_gap.code)
        self.assertEqual("ALARM_RECOVERY_PENDING", second_recovery_pending.code)
        self.assertEqual("ALARM_RECOVERED", recovered.code)
        self.assertEqual(active.event_id, recovered.event_id)
