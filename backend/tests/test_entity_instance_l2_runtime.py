from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from app.services.entity_instance_registry import (
    EntityInstanceError,
    EntityInstanceRegistry,
    InMemorySourceCatalog,
    ResolvedEntitySource,
)
from app.services.entity_instance_runtime import (
    EntityInstanceRuntime,
    InMemoryObservationCatalog,
    SourceObservation,
)


ENTITY_ID = UUID("87000000-0000-0000-0000-000000000001")
DEVICE_ID = UUID("87000000-0000-0000-0000-000000000002")
REVISION_ID = UUID("87000000-0000-0000-0000-000000000003")
EVENT_ID = UUID("87000000-0000-0000-0000-000000000012")
INSTALLATION_ID = UUID("87000000-0000-0000-0000-000000000004")


class PointSourceRepository:
    def resolve(self, entity_instance_id: UUID):
        if entity_instance_id != ENTITY_ID:
            return None
        return ResolvedEntitySource(
            entity_instance_id=ENTITY_ID,
            definition_id="pcs.active_power",
            instance_key="PCS-01",
            device_instance_id=DEVICE_ID,
            binding_id=None,
            tag_id=None,
            matcher_id=None,
            confirmation_audit_id=None,
            data_type="FLOAT",
            unit="kW",
            direction="R",
            freshness_seconds=30,
            source_kind="point_processing",
            source_id=ENTITY_ID,
            processing_revision_id=REVISION_ID,
            site_configuration_version=1,
        )


class StaticEntityCatalogRepository:
    def list_instances(self):
        from app.services.entity_instance_catalog import EntityInstanceDescriptor

        return (
            EntityInstanceDescriptor(
                id=ENTITY_ID,
                device_instance_id=DEVICE_ID,
                slot_id="slot.pcs-primary",
                instance_key="PCS-01",
                device_category="pcs",
                device_display_name="PCS-01",
                definition_id="pcs.active_power",
                display_name="PCS 有功功率",
                data_type="FLOAT",
                unit="kW",
                direction="R",
                freshness_seconds=30,
                confirmed=True,
            ),
        )

    def preview_legacy(self):
        return ()


class StaticDeliveryRepository:
    def __init__(self, manifest: dict) -> None:
        self._manifest = manifest

    def site_configuration_version(self) -> int:
        return 1

    def get_site_configuration_version(self, version: int):
        return SimpleNamespace(installation_id=INSTALLATION_ID) if version == 1 else None

    def get_installation(self, installation_id: UUID):
        if installation_id != INSTALLATION_ID:
            return None
        return SimpleNamespace(id=installation_id, entity_instance_ids=(ENTITY_ID,))

    def package_for_installation(self, installation):
        if installation.id != INSTALLATION_ID:
            return None
        return SimpleNamespace(manifest=self._manifest)


class EntityInstanceL2RuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.observations = InMemoryObservationCatalog()
        self.registry = EntityInstanceRegistry(
            PointSourceRepository(),  # type: ignore[arg-type]
            InMemorySourceCatalog(),
            lambda _transaction=None: 1,
        )
        self.runtime = EntityInstanceRuntime(
            self.registry,
            self.observations,
        )

    def publish_l2(self, *, value: float = 12.345) -> None:
        self.observations.publish(
            SourceObservation(
                ENTITY_ID,
                datetime.now(timezone.utc),
                value,
                192,
                event_id=EVENT_ID,
                processing_revision_id=REVISION_ID,
                site_configuration_version=1,
                source_digest="b" * 64,
            )
        )

    def assert_l2_provenance(self, value: dict) -> None:
        self.assertEqual(value["event_id"], str(EVENT_ID))
        self.assertEqual(value["source_kind"], "point_processing")
        self.assertEqual(value["processing_revision_id"], str(REVISION_ID))
        self.assertEqual(value["site_configuration_version"], 1)
        self.assertEqual(value["source_digest"], "b" * 64)

    def test_point_processing_entity_reads_l2_latest_and_history(self) -> None:
        now = datetime.now(timezone.utc)
        first_event = UUID("87000000-0000-0000-0000-000000000011")
        second_event = UUID("87000000-0000-0000-0000-000000000012")
        self.observations.publish(
            SourceObservation(
                ENTITY_ID,
                now - timedelta(seconds=2),
                11.5,
                192,
                event_id=first_event,
                processing_revision_id=REVISION_ID,
                site_configuration_version=1,
                source_digest="a" * 64,
            )
        )
        self.observations.publish(
            SourceObservation(
                ENTITY_ID,
                now,
                12.345,
                192,
                event_id=second_event,
                processing_revision_id=REVISION_ID,
                site_configuration_version=1,
                source_digest="b" * 64,
            )
        )

        latest = self.runtime.read(ENTITY_ID)
        history = self.runtime.history(ENTITY_ID, "1h")
        self.assertEqual(latest.value, 12.345)
        self.assertEqual(latest.source_kind, "point_processing")
        self.assertEqual(latest.processing_revision_id, REVISION_ID)
        self.assertEqual(latest.event_id, second_event)
        self.assertEqual(len(history), 2)

    def test_missing_l2_does_not_fall_back_to_a_legacy_value(self) -> None:
        unrelated_legacy_tag = UUID("87000000-0000-0000-0000-000000000099")
        self.observations.publish(
            SourceObservation(
                unrelated_legacy_tag,
                datetime.now(timezone.utc),
                99.0,
                192,
            )
        )
        with self.assertRaises(EntityInstanceError) as raised:
            self.runtime.read(ENTITY_ID)
        self.assertEqual(raised.exception.code, "ENTITY_DATA_MISSING")

    def test_alarm_evidence_keeps_the_l2_event_and_conversion_provenance(self) -> None:
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )
        from app.services.entity_alarm_adapter import EntityAlarmAdapter

        self.publish_l2()
        definition = AlarmDefinition(
            id=UUID("87000000-0000-0000-0000-000000000021"),
            asset_id="alarm.pcs.active-power-high",
            version="1",
            entity_instance_id=ENTITY_ID,
            entity_definition_id="pcs.active_power",
            trigger={"op": "gt", "value": 10},
            trigger_duration_seconds=0,
            recovery={"op": "lte", "value": 9},
            recovery_duration_seconds=0,
            severity="MAJOR",
            notification_throttle_seconds=60,
        )
        definitions = InMemoryAlarmDefinitionCatalog((definition,))
        repository = InMemoryAlarmRepository()
        adapter = EntityAlarmAdapter(
            definitions,
            self.runtime,
            AlarmRuntime(definitions, repository),
        )

        adapter.submit_entity(ENTITY_ID)

        event = repository.list_events()[0]
        self.assertIsNotNone(event.first_observation)
        self.assert_l2_provenance(event.first_observation["evidence"])

    def test_rule_context_keeps_the_l2_event_and_conversion_provenance(self) -> None:
        from app.services.rule_engine import _entity_instance_context

        self.publish_l2()
        with patch(
            "app.api.solution_delivery.get_default_entity_instance_registry",
            return_value=self.registry,
        ), patch(
            "app.api.solution_delivery.get_default_entity_instance_runtime",
            return_value=self.runtime,
        ):
            context = _entity_instance_context({str(ENTITY_ID)})

        self.assert_l2_provenance(context[str(ENTITY_ID)])

    def test_policy_input_keeps_the_l2_event_and_conversion_provenance(self) -> None:
        from app.services.ems_policy_runtime import EmsPolicyRuntime
        from app.services.entity_instance_catalog import EntityInstanceCatalog

        self.publish_l2()
        policy = {
            "id": "policy.pcs-observe",
            "revision": 1,
            "input": {
                "slot": "slot.pcs-primary",
                "definition": "pcs.active_power",
            },
            "condition": {"operator": "gt", "threshold": 10},
            "action": {
                "id": "observe-only",
                "target": {
                    "slot": "slot.pcs-primary",
                    "definition": "pcs.active_power",
                },
                "value": 0,
            },
            "simulation": {
                "input": {"value": 12.345},
                "expected": {"triggered": True, "actionValue": 0},
            },
        }
        runtime = EmsPolicyRuntime(
            StaticDeliveryRepository({"_policy_assets": [policy]}),  # type: ignore[arg-type]
            EntityInstanceCatalog(StaticEntityCatalogRepository()),
            self.runtime,
            SimpleNamespace(),  # command dispatch is outside this read-only test
        )

        enabled = runtime.enable("policy.pcs-observe", "user:engineer")

        self.assert_l2_provenance(enabled["input"])

    def test_workbench_metric_keeps_the_l2_event_and_conversion_provenance(self) -> None:
        from app.services.ems_workbench import EmsWorkbench
        from app.services.entity_instance_catalog import EntityInstanceCatalog

        self.publish_l2()
        workbench = {
            "id": "ems.main",
            "navigation": [],
            "groups": [
                {
                    "id": "pcs",
                    "label": "PCS",
                    "entities": [
                        {
                            "slot": "slot.pcs-primary",
                            "definition": "pcs.active_power",
                        }
                    ],
                }
            ],
            "kpis": [],
            "trends": [],
            "alarms": {},
            "controls": {},
        }
        runtime = EmsWorkbench(
            StaticDeliveryRepository({"_workbench_assets": [workbench]}),  # type: ignore[arg-type]
            EntityInstanceCatalog(StaticEntityCatalogRepository()),
            self.runtime,
        )

        metric = runtime.read()["groups"][0]["entities"][0]

        self.assert_l2_provenance(metric)


if __name__ == "__main__":
    unittest.main()
