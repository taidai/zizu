from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import os
import unittest
from uuid import UUID, uuid5, NAMESPACE_URL

import psycopg2

from app.services.data_trunk import DataTrunk
from app.services.data_trunk_contracts import (
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.data_trunk_postgres import PostgresDataTrunkRepository
from tests.test_neuron_point_processing_catalog import FakeNeuron
from tests import test_data_trunk_migration_postgres as migration_support
from tests import test_point_processing_postgres as point_processing_support


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run EN9 acceptance tests",
)
class EN9PointProcessingAcceptancePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("EN9 acceptance tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        from app.services.telemetry_store import close_db_pool, init_db_pool

        close_db_pool()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration_support.DataTrunkMigrationPostgresTest._reset_through_037(cursor)
                migration_support.DataTrunkMigrationPostgresTest._apply_038(cursor)
                migration_support.DataTrunkMigrationPostgresTest._apply_039(cursor)
                migration_support.DataTrunkMigrationPostgresTest._apply_040(cursor)
        init_db_pool(min_conn=1, max_conn=4)

    @classmethod
    def tearDownClass(cls) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    @contextmanager
    def _connection(self):
        connection = psycopg2.connect(**self.connection_kwargs)
        try:
            yield connection
        finally:
            connection.close()

    def test_en9_report_requires_all_ninety_sources_and_three_l2_entities(self) -> None:
        from app.services.en9_point_processing_acceptance import run_en9_acceptance
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingDelivery,
            PreviewPointProcessing,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
        )

        helper = point_processing_support.PointProcessingPostgresTest(
            methodName="test_package_import_persists_complete_versioned_catalog_atomically"
        )
        helper.connection_kwargs = self.connection_kwargs
        package, brand_a_revision, _ = helper._import_reference_package()
        node_id = UUID("85000000-0000-0000-0000-000000000001")
        helper._seed_brand_a_site(
            package.id,
            package.digest,
            brand_a_revision,
            node_id,
            {
                "active_power": UUID("85000000-0000-0000-0000-000000000101"),
                "operating_state": UUID("85000000-0000-0000-0000-000000000102"),
                "fault_codes": UUID("85000000-0000-0000-0000-000000000103"),
            },
        )
        catalog = PostgresPointProcessingCatalog()
        en9_revision = next(
            item.revision_id for item in catalog.list_templates("PCS")
            if item.asset.asset_id == "pcs.en9"
        )
        service = PointProcessingDelivery(
            PostgresPointProcessingRepository(),
            catalog,
            point_scanner=NeuronPointCatalog(FakeNeuron()),
        )
        plan = service.preview(
            PreviewPointProcessing(node_id, en9_revision, {}, "user:acceptance")
        )
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id, plan.digest, "en9-acceptance", "user:acceptance"
            )
        )

        observed_at = datetime(2026, 8, 23, 1, tzinfo=UTC)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, name, value_data_type, unit, source_address
                    FROM t_tags
                    WHERE node_id = %s AND source_address IS NOT NULL
                    ORDER BY source_address
                    """,
                    (str(node_id),),
                )
                sources = cursor.fetchall()
        observations = []
        for sequence, (tag_id, name, data_type, unit, address) in enumerate(sources, 1):
            if address == "1!424634":
                value = TypedValue.float(25.0)
            elif address == "1!424669":
                value = TypedValue(ValueKind.INT, 2)
            else:
                value = TypedValue(ValueKind.BOOL, False)
            digest = hashlib.sha256(f"{address}:{sequence}".encode()).hexdigest()
            observations.append(
                RawObservation(
                    observation_id=uuid5(NAMESPACE_URL, f"en9:{address}"),
                    node_id=node_id,
                    tag_id=UUID(str(tag_id)),
                    source_key=name,
                    value=value,
                    raw_unit=unit,
                    quality=TrunkQuality.GOOD,
                    source_timestamp=observed_at,
                    received_at=observed_at + timedelta(milliseconds=100),
                    source_message_id="en9-acceptance-snapshot",
                    source_sequence=sequence,
                    source_digest=digest,
                )
            )
        receipt = DataTrunk(
            PostgresDataTrunkRepository(
                clock=lambda: observed_at + timedelta(seconds=1)
            )
        ).ingest(tuple(observations))
        self.assertEqual((90, 3), (receipt.accepted_l0_count, len(receipt.l2_event_ids)))

        report = run_en9_acceptance(
            application.id,
            10,
            connection_factory=self._connection,
            ws_authenticated=True,
            clock=lambda: observed_at + timedelta(seconds=1),
        )

        self.assertEqual(90, report.required_input_count)
        self.assertEqual(3, report.output_entity_count)
        self.assertTrue(report.passed, report.public_dict())


if __name__ == "__main__":
    unittest.main()
