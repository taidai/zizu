"""Real PostgreSQL L0 -> L1 -> L2 transaction evidence on Schema 044."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from uuid import NAMESPACE_URL, UUID, uuid5
from datetime import UTC, datetime

import psycopg2

os.environ.setdefault("NEURON_PASSWORD", "test-neuron-secret")
os.environ.setdefault("NANOMQ_API_PASSWORD", "test-nanomq-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value-that-is-long-enough")

from tests import test_node_data_trunk_hard_cut_migration_postgres as migration


REFERENCE = (
    Path(__file__).resolve().parents[2]
    / "reference-point-processings"
    / "pcs-brand-a.zizu-point-processing.json"
)
NODE_ID = UUID("93000000-0000-0000-0000-000000000001")


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL data-trunk tests",
)
class DataTrunkPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Data-trunk tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration.NodeDataTrunkHardCutMigrationPostgresTest._reset_through_043(
                    cursor
                )
                migration.NodeDataTrunkHardCutMigrationPostgresTest._apply_044(cursor)
        from app.services.telemetry_store import init_db_pool

        init_db_pool(1, 4)
        self.tag_ids = self._publish_brand_a()

    def tearDown(self) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    def _publish_brand_a(self) -> dict[str, UUID]:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PreviewPointProcessing,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingTemplates,
            build_postgres_point_processing,
        )
        from app.services.telemetry_store import get_connection

        raw = json.loads(REFERENCE.read_text(encoding="utf-8"))
        tags: dict[str, UUID] = {}
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled) "
                    "VALUES(%s,'PCS-TEST','PCS',TRUE)",
                    (NODE_ID,),
                )
                for item in raw["inputs"]:
                    tag_id = uuid5(
                        NAMESPACE_URL,
                        f"test/tag/{NODE_ID}/{item['sourceKey']}",
                    )
                    tags[item["sourceKey"]] = tag_id
                    cursor.execute(
                        """
                        INSERT INTO t_tags
                          (id,node_id,name,data_type,unit,read_write,enabled,
                           timestamp_trusted)
                        VALUES(%s,%s,%s,%s,%s,'R',TRUE,FALSE)
                        """,
                        (
                            tag_id,
                            NODE_ID,
                            item["sourceKey"],
                            item["dataType"],
                            item.get("unit"),
                        ),
                    )
            connection.commit()
        registered = PostgresPointProcessingTemplates().import_template(
            raw,
            actor="test:engineer",
        )
        service = build_postgres_point_processing()
        plan = service.preview(
            PreviewPointProcessing(
                NODE_ID,
                registered.revision_id,
                {},
                "test:engineer",
            )
        )
        service.apply(
            ApplyPointProcessingPlan(plan.id, plan.digest, "publish", "test:engineer")
        )
        return tags

    def test_raw_fact_commits_l2_value_history_latest_source_and_outbox(self) -> None:
        from app.services.data_trunk import DataTrunk
        from app.services.data_trunk_contracts import (
            RawObservation,
            TrunkQuality,
            TypedValue,
            ValueKind,
        )
        from app.services.data_trunk_postgres import PostgresDataTrunkRepository
        from app.services.telemetry_store import get_connection

        observed_at = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        tag_id = self.tag_ids["ActivePowerRaw"]
        raw = RawObservation(
            observation_id=uuid5(NAMESPACE_URL, "test/l0/active-power/1"),
            node_id=NODE_ID,
            tag_id=tag_id,
            source_key="ActivePowerRaw",
            value=TypedValue(ValueKind.FLOAT, 1000.0),
            raw_unit="W",
            quality=TrunkQuality.GOOD,
            source_timestamp=observed_at,
            received_at=observed_at,
            source_message_id="test-message-1",
            source_sequence=1,
            source_digest="a" * 64,
            event_time_basis="observed_at",
        )

        receipt = DataTrunk(
            PostgresDataTrunkRepository(clock=lambda: observed_at)
        ).ingest((raw,))

        self.assertEqual(1, receipt.accepted_l0_count)
        self.assertEqual(1, len(receipt.l2_event_ids))
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT entity.definition_id, latest.value_float,
                           latest.quality, latest.configuration_revision
                    FROM t_l2_latest AS latest
                    JOIN t_entity_instances AS entity
                      ON entity.id=latest.entity_instance_id
                    WHERE entity.definition_id='pcs.active_power'
                    """
                )
                self.assertEqual(
                    ("pcs.active_power", 1.0, int(TrunkQuality.GOOD), 1),
                    cursor.fetchone(),
                )
                cursor.execute(
                    "SELECT count(*) FROM t_l2_observation_sources "
                    "WHERE source_kind='l0'"
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_l2_stream_outbox")
                self.assertEqual(1, cursor.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
