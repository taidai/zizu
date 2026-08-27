"""Ticket 14: rules adapt observations; AlarmRuntime owns every lifecycle state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID


RULE_ID = UUID("73000000-0000-0000-0000-000000000001")
ENTITY_INSTANCE_ID = UUID("73000000-0000-0000-0000-000000000002")
DEFINITION_ID = UUID("73000000-0000-0000-0000-000000000003")
OBSERVED_AT = datetime(2026, 8, 14, 11, tzinfo=timezone.utc)


class RuleAlarmAdapterContractTest(unittest.TestCase):
    def test_rule_tick_submits_an_observation_without_a_legacy_alarm_write(self) -> None:
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )
        from app.services.rule_alarm_adapter import RuleAlarmAdapter
        from app.services.rule_engine import run_rule_tick

        definition = AlarmDefinition(
            id=DEFINITION_ID,
            asset_id="alarm.pcs.export-limit",
            version="1.0.0",
            entity_instance_id=ENTITY_INSTANCE_ID,
            entity_definition_id="pcs.activePower",
            trigger={"op": "gt", "value": 100},
            trigger_duration_seconds=30,
            recovery={"op": "lte", "value": 90},
            recovery_duration_seconds=1,
            severity="MAJOR",
            notification_throttle_seconds=60,
        )
        definitions = InMemoryAlarmDefinitionCatalog((definition,))
        runtime = AlarmRuntime(definitions, InMemoryAlarmRepository())
        adapter = RuleAlarmAdapter(definitions, runtime)
        rule = {
            "_config": {
                "sourceEntityInstanceIds": [str(ENTITY_INSTANCE_ID)],
                "inputMappings": {"grid_power": str(ENTITY_INSTANCE_ID)},
                "actions": [{
                    "id": "export-limit",
                    "type": "alarm",
                    "alarm_definition": "alarm.pcs.export-limit",
                    "entity_instance_id": str(ENTITY_INSTANCE_ID),
                    "value": "{{grid_power}}",
                }],
            },
        }

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, _query, _params=()):
                return None

            def fetchall(self):
                return [(RULE_ID, "alarm", json.dumps(rule), True, 3)]

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return Cursor()

            def commit(self):
                return None

        @contextmanager
        def connection():
            yield Connection()

        context = {
            str(ENTITY_INSTANCE_ID): {
                "value": 101,
                "entity_instance_id": ENTITY_INSTANCE_ID,
                "observed_at": OBSERVED_AT.isoformat(),
                "quality": 192,
                "fresh": True,
                "max_observation_gap_seconds": 30,
                "is_entity_instance": True,
            }
        }
        with patch("app.services.telemetry_store.get_connection", connection), patch(
            "app.services.rule_engine._rule_context", return_value=context
        ), patch(
            "app.services.rule_engine.evaluate_rule",
            return_value={
                "triggered": True,
                "actions": rule["_config"]["actions"],
                "outputs": {"decision": "over-limit", "node_id": "must-not-persist", "topic": "must-not-persist"},
            },
        ), patch(
            "app.services.rule_engine._default_rule_alarm_adapter", return_value=adapter
        ):
            result = run_rule_tick()

        self.assertEqual({"evaluated": 1, "alarms": 0, "controls": 0, "errors": 0}, result)
        event = runtime.list()[0]
        self.assertEqual("pending", event.state)
        self.assertEqual("rule", event.last_observation["source_kind"])
        self.assertEqual(OBSERVED_AT.isoformat(), event.last_observation["observed_at"])
        self.assertEqual(192, event.last_observation["quality"])
        self.assertEqual(101, event.last_observation["value"])
        self.assertEqual("over-limit", event.last_observation["evidence"]["outputs"]["decision"])
        self.assertNotIn("node_id", event.last_observation["evidence"]["outputs"])
        self.assertNotIn("topic", event.last_observation["evidence"]["outputs"])

        gap_context = {
            str(ENTITY_INSTANCE_ID): {
                **context[str(ENTITY_INSTANCE_ID)],
                "observed_at": (OBSERVED_AT + timedelta(seconds=31)).isoformat(),
            }
        }
        with patch("app.services.telemetry_store.get_connection", connection), patch(
            "app.services.rule_engine._rule_context", return_value=gap_context
        ), patch(
            "app.services.rule_engine.evaluate_rule",
            return_value={"triggered": True, "actions": rule["_config"]["actions"], "outputs": {}},
        ), patch(
            "app.services.rule_engine._default_rule_alarm_adapter", return_value=adapter
        ):
            second = run_rule_tick()
        self.assertEqual(0, second["alarms"])
        self.assertEqual("normal", runtime.list()[0].state)
        self.assertEqual(192, runtime.list()[0].last_observation["quality"])

    def test_rule_and_legacy_sources_have_no_old_alarm_writer(self) -> None:
        services = Path(__file__).resolve().parents[1] / "app" / "services"
        for filename in (
            "rule_engine.py",
            "entity_alarm_engine.py",
        ):
            source = (services / filename).read_text(encoding="utf-8").lower()
            with self.subTest(source=filename):
                self.assertNotIn("insert into t_alarms", source)
                self.assertNotIn("update t_alarms", source)

        for removed in (
            "alarm_processor.py",
            "tag_alarm_engine.py",
            "tag_mqtt_alarm_adapter.py",
            "entity_alarm_adapter.py",
        ):
            self.assertFalse((services / removed).exists())

        migration = (
            Path(__file__).resolve().parents[2]
            / "init-db"
            / "migration_030_rule_alarm_and_legacy_gate.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("trg_legacy_alarms_read_only", migration)
        self.assertIn("before insert or update or delete or truncate on t_alarms", migration.lower())
        self.assertIn("drop constraint if exists t_alarms_node_id_fkey", migration.lower())
        self.assertNotIn("on delete cascade", migration.lower())

    def test_rule_configuration_requires_a_currently_installed_definition(self) -> None:
        from app.services.alarm_runtime import (
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )
        from app.services.rule_alarm_adapter import RuleAlarmAdapter

        definitions = InMemoryAlarmDefinitionCatalog()
        adapter = RuleAlarmAdapter(
            definitions,
            AlarmRuntime(definitions, InMemoryAlarmRepository()),
        )
        self.assertFalse(
            adapter.has_installed_definition(
                "alarm.pcs.export-limit",
                ENTITY_INSTANCE_ID,
            )
        )

    def test_rule_validation_rejects_an_alarm_asset_not_installed_for_its_instance(self) -> None:
        from unittest.mock import Mock

        from app.services.entity_instance_catalog import (
            EntityInstanceReferenceError,
            validate_rule_entity_references,
        )

        catalog = Mock()
        content = {
            "_config": {
                "sourceEntityInstanceIds": [str(ENTITY_INSTANCE_ID)],
                "actions": [{
                    "id": "export-limit",
                    "type": "alarm",
                    "alarm_definition": "alarm.pcs.export-limit",
                    "entity_instance_id": str(ENTITY_INSTANCE_ID),
                    "value": "{{grid_power}}",
                }],
            },
        }
        with self.assertRaises(EntityInstanceReferenceError) as caught:
            validate_rule_entity_references(
                content,
                catalog,
                has_installed_alarm_definition=lambda _asset, _instance: False,
            )
        self.assertEqual("RULE_ALARM_DEFINITION_NOT_INSTALLED", caught.exception.code)
        catalog.require.assert_called_once_with((ENTITY_INSTANCE_ID,))

    def test_rule_observation_uses_the_installed_definition_lifecycle(self) -> None:
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )
        from app.services.rule_alarm_adapter import (
            RuleAlarmAdapter,
            RuleAlarmObservation,
        )

        definition = AlarmDefinition(
            id=DEFINITION_ID,
            asset_id="alarm.pcs.export-limit",
            version="1.0.0",
            entity_instance_id=ENTITY_INSTANCE_ID,
            entity_definition_id="pcs.activePower",
            trigger={"op": "gt", "value": 100},
            trigger_duration_seconds=1,
            recovery={"op": "lte", "value": 90},
            recovery_duration_seconds=1,
            severity="MAJOR",
            notification_throttle_seconds=60,
        )
        definitions = InMemoryAlarmDefinitionCatalog((definition,))
        runtime = AlarmRuntime(definitions, InMemoryAlarmRepository())
        adapter = RuleAlarmAdapter(definitions, runtime)

        observation = RuleAlarmObservation(
            rule_id=RULE_ID,
            rule_version=7,
            action_id="export-limit",
            alarm_definition="alarm.pcs.export-limit",
            entity_instance_id=ENTITY_INSTANCE_ID,
            observed_at=OBSERVED_AT,
            value=101,
            quality=192,
            evidence={"inputs": [{"field": "grid_power", "value": 101}]},
        )
        outcomes = [
            *adapter.submit(observation),
            *adapter.submit(
                RuleAlarmObservation(
                    **{
                        **observation.__dict__,
                        "observed_at": OBSERVED_AT + timedelta(seconds=1),
                    }
                )
            ),
        ]

        self.assertEqual(
            ["ALARM_TRIGGER_PENDING", "ALARM_ACTIVATED"],
            [item.code for item in outcomes],
        )
        event = runtime.list()[0]
        self.assertEqual(DEFINITION_ID, event.definition_id)
        self.assertEqual(ENTITY_INSTANCE_ID, event.entity_instance_id)
        transition = runtime.timeline(event.id)[0]
        self.assertEqual("rule", transition.evidence["source_kind"])
        self.assertEqual("7", transition.evidence["evidence"]["rule_version"])


if __name__ == "__main__":
    unittest.main()
