"""Ticket 12 contract: the pipeline no longer writes legacy entity alarms."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from uuid import UUID


class EntityAlarmAdapterContractTest(unittest.TestCase):
    def test_alarm_definition_schema_rejects_mutation(self) -> None:
        migration = (
            Path(__file__).resolve().parents[2]
            / "init-db"
            / "migration_029_unified_alarm_runtime.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("trg_alarm_definitions_immutable", migration)
        self.assertIn(
            "BEFORE UPDATE OR DELETE OR TRUNCATE ON t_alarm_definitions",
            migration,
        )

    def test_pipeline_uses_installed_entity_alarm_adapter_not_legacy_engine(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "pipeline.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "app.services.entity_alarm_engine"
            for alias in node.names
        }
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("process_entity_alarms", imported)
        self.assertNotIn("process_entity_alarms", called)
        self.assertIn("_submit_installed_entity_alarms", source)

    def test_open_event_keeps_its_immutable_definition_after_an_upgrade(self) -> None:
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmObservation,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )
        from app.services.entity_alarm_adapter import EntityAlarmAdapter
        from app.services.entity_instance_runtime import EntityInstanceObservation

        entity_instance_id = UUID("71000000-0000-0000-0000-000000000002")
        old_definition = AlarmDefinition(
            id=UUID("71000000-0000-0000-0000-000000000010"),
            asset_id="alarm.pcs.overpower",
            version="1.0.0",
            entity_instance_id=entity_instance_id,
            entity_definition_id="pcs.activePower",
            trigger={"op": "gt", "value": 100},
            trigger_duration_seconds=1,
            recovery={"op": "lte", "value": 90},
            recovery_duration_seconds=5,
            severity="MAJOR",
            notification_throttle_seconds=60,
        )
        upgraded_definition = AlarmDefinition(
            id=UUID("71000000-0000-0000-0000-000000000011"),
            asset_id=old_definition.asset_id,
            version="2.0.0",
            entity_instance_id=entity_instance_id,
            entity_definition_id=old_definition.entity_definition_id,
            trigger=old_definition.trigger,
            trigger_duration_seconds=1,
            recovery=old_definition.recovery,
            recovery_duration_seconds=old_definition.recovery_duration_seconds,
            severity=old_definition.severity,
            notification_throttle_seconds=old_definition.notification_throttle_seconds,
        )
        definitions = InMemoryAlarmDefinitionCatalog((old_definition, upgraded_definition))
        alarms = AlarmRuntime(definitions, InMemoryAlarmRepository())
        started_at = datetime(2026, 8, 14, 9, tzinfo=timezone.utc)
        for after_seconds in (0, 1):
            alarms.submit(
                AlarmObservation(
                    definition_id=old_definition.id,
                    entity_instance_id=entity_instance_id,
                    observed_at=started_at + timedelta(seconds=after_seconds),
                    value=101,
                    quality=192,
                    source_kind="entity_instance",
                    source_ref=str(entity_instance_id),
                    evidence={},
                )
            )

        class CurrentEntityObservation:
            def __init__(self) -> None:
                self.fresh = True
                self.observed_at = started_at + timedelta(seconds=2)
                self.value = 101

            def read_for_alarm(self, requested_id: UUID) -> EntityInstanceObservation:
                if requested_id != entity_instance_id:
                    raise AssertionError("Adapter requested an unexpected entity instance")
                return EntityInstanceObservation(
                    entity_instance_id=entity_instance_id,
                    definition_id="pcs.activePower",
                    instance_key="PCS-01",
                    value=self.value,
                    data_type="FLOAT",
                    unit="kW",
                    observed_at=self.observed_at,
                    quality=192,
                    age_ms=0,
                    fresh=self.fresh,
                    quality_good=True,
                )

        entity_runtime = CurrentEntityObservation()
        adapter = EntityAlarmAdapter(definitions, entity_runtime, alarms)
        continuing_outcomes = adapter.submit_entity(entity_instance_id)

        self.assertEqual(
            {"ALARM_STILL_ACTIVE"},
            {outcome.code for outcome in continuing_outcomes},
        )
        self.assertEqual(
            1,
            len([event for event in alarms.list() if event.state != "normal"]),
        )
        entity_runtime.value = 90
        entity_runtime.observed_at = started_at + timedelta(seconds=3)
        recovery_outcomes = adapter.submit_entity(entity_instance_id)
        self.assertEqual(
            {"ALARM_RECOVERY_PENDING"},
            {outcome.code for outcome in recovery_outcomes},
        )
        entity_runtime.fresh = False
        entity_runtime.observed_at = started_at + timedelta(seconds=4)
        invalid_outcomes = adapter.submit_entity(entity_instance_id)

        self.assertEqual(
            {"ALARM_STILL_ACTIVE"},
            {outcome.code for outcome in invalid_outcomes},
        )
        active_event = next(event for event in alarms.list() if event.state != "normal")
        self.assertIsNone(active_event.recovery_candidate_since)


if __name__ == "__main__":
    unittest.main()
