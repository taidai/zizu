from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import os
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")

from app.services.entity_instance_registry import (
    EntityInstanceError,
    EntityInstanceRegistry,
    InMemorySourceCatalog,
    ResolvedEntitySource,
)
from app.services.entity_instance_runtime import (
    EntityInstanceObservation,
    EntityInstanceRuntime,
    InMemoryObservationCatalog,
    SourceObservation,
)
from app.api.entity_instances import read_entity_instance_realtime


ENTITY_ID = UUID("87000000-0000-0000-0000-000000000001")
NODE_ID = UUID("87000000-0000-0000-0000-000000000002")
REVISION_ID = UUID("87000000-0000-0000-0000-000000000003")
EVENT_ID = UUID("87000000-0000-0000-0000-000000000012")


class PointSourceRepository:
    def resolve(self, entity_instance_id: UUID):
        if entity_instance_id != ENTITY_ID:
            return None
        return ResolvedEntitySource(
            entity_instance_id=ENTITY_ID,
            definition_id="pcs.active_power",
            node_key="PCS-01",
            node_id=NODE_ID,
            data_type="FLOAT",
            unit="kW",
            direction="R",
            freshness_seconds=30,
            source_kind="point_processing",
            source_id=ENTITY_ID,
            processing_revision_id=REVISION_ID,
            configuration_revision=1,
        )


class StaticEntityCatalogRepository:
    def list_instances(self):
        from app.services.entity_instance_catalog import EntityInstanceDescriptor

        return (
            EntityInstanceDescriptor(
                id=ENTITY_ID,
                node_id=NODE_ID,
                node_type="pcs",
                node_display_name="PCS-01",
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
                configuration_revision=1,
                source_digest="b" * 64,
            )
        )

    def assert_l2_provenance(self, value: dict) -> None:
        self.assertEqual(value["event_id"], str(EVENT_ID))
        self.assertEqual(value["source_kind"], "point_processing")
        self.assertEqual(value["processing_revision_id"], str(REVISION_ID))
        self.assertEqual(value["configuration_revision"], 1)
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
                configuration_revision=1,
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
                configuration_revision=1,
                source_digest="b" * 64,
            )
        )

        latest = self.runtime.read(ENTITY_ID)
        history = self.runtime.history(ENTITY_ID, "1h")
        self.assertEqual(latest.value, 12.345)
        self.assertEqual(latest.source_kind, "point_processing")
        self.assertEqual(latest.processing_revision_id, REVISION_ID)
        self.assertEqual(latest.event_id, second_event)
        self.assertEqual(now, latest.value_observed_at)
        self.assertEqual(len(history), 2)
        self.assertEqual(now - timedelta(seconds=2), history[0].value_observed_at)

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

    def test_bad_latest_keeps_last_good_value_time_but_remains_unusable(self) -> None:
        value_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        bad_at = datetime.now(timezone.utc)
        self.observations.publish(
            SourceObservation(
                ENTITY_ID,
                bad_at,
                12.345,
                0,
                reason="TYPE_MISMATCH",
                value_observed_at=value_at,
            )
        )

        status = self.runtime.read_for_alarm(ENTITY_ID)

        self.assertEqual(12.345, status.value)
        self.assertEqual(value_at, status.value_observed_at)
        self.assertEqual(bad_at, status.observed_at)
        self.assertFalse(status.quality_good)
        with self.assertRaises(EntityInstanceError) as raised:
            self.runtime.read(ENTITY_ID)
        self.assertEqual("ENTITY_DATA_QUALITY_BAD", raised.exception.code)

    def test_realtime_api_returns_bad_status_instead_of_hiding_it_as_conflict(self) -> None:
        observed_at = datetime.now(timezone.utc)
        value_observed_at = observed_at - timedelta(seconds=1)
        status = EntityInstanceObservation(
            entity_instance_id=ENTITY_ID,
            definition_id="pcs.active_power",
            node_id=NODE_ID,
            node_key="PCS-01",
            value=12.345,
            data_type="FLOAT",
            unit="kW",
            observed_at=observed_at,
            quality=0,
            age_ms=0,
            fresh=True,
            quality_good=False,
            value_observed_at=value_observed_at,
        )

        class _StatusRuntime:
            def read(self, _entity_id):
                raise AssertionError("presentation API must not hide BAD status")

            def read_for_alarm(self, _entity_id):
                return status

        payload = asyncio.run(
            read_entity_instance_realtime(ENTITY_ID, runtime=_StatusRuntime())
        )

        self.assertEqual(0, payload["quality"])
        self.assertEqual(12.345, payload["value"])
        self.assertEqual(value_observed_at.isoformat(), payload["value_observed_at"])

if __name__ == "__main__":
    unittest.main()
