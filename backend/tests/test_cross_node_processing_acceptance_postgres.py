from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from datetime import UTC, datetime
from uuid import UUID

import psycopg2
from psycopg2.extras import register_uuid

from tests import test_data_trunk_postgres as trunk_fixture


SECOND_PCS_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000307")
FORMULA_APPLICATION_ID = UUID("00000000-0000-0000-0000-000000000408")


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run cross-node acceptance tests",
)
class CrossNodeProcessingAcceptancePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_uuid()
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Cross-node acceptance requires a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        self.fixture = trunk_fixture.DataTrunkPostgresTest()
        self.fixture.connection_kwargs = self.connection_kwargs
        self.fixture.setUp()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                self.fixture._seed_formula_processing(cursor)
                self._seed_second_pcs_and_application(cursor)
        self.fixture.trunk.ingest(
            (self.fixture.raw_power(10_000.0, sequence=1),)
        )
        self.assertEqual(1, len(self.fixture.trunk.evaluate_due_formulas()))
        self._run_after_runtime_restart()
        self._ack_latest_formula_over_authenticated_stream()
        self._ingest_late_source_without_rewinding_latest()

    def _run_after_runtime_restart(self) -> None:
        from app.services.data_trunk import DataTrunk
        from app.services.data_trunk_postgres import PostgresDataTrunkRepository
        from app.services import data_trunk_postgres

        original_runtime_id = data_trunk_postgres.RUNTIME_INSTANCE_ID
        data_trunk_postgres.RUNTIME_INSTANCE_ID = UUID(
            "00000000-0000-0000-0000-000000000414"
        )
        try:
            restarted = DataTrunk(
                PostgresDataTrunkRepository(
                    connection_factory=self.fixture._connection,
                    clock=lambda: datetime(2026, 8, 17, 1, 0, 1, tzinfo=UTC),
                )
            )
            restarted.ingest(
                (
                    self.fixture.raw_power(
                        10_000.0,
                        sequence=2,
                        observed_at=datetime(
                            2026, 8, 17, 1, 0, 1, tzinfo=UTC
                        ),
                    ),
                )
            )
            self.assertEqual(1, len(restarted.evaluate_due_formulas()))
        finally:
            data_trunk_postgres.RUNTIME_INSTANCE_ID = original_runtime_id

    def _ack_latest_formula_over_authenticated_stream(self) -> None:
        from app.services.data_trunk_outbox import OutboxEvent
        from app.services.en9_point_processing_acceptance import (
            PostgresEN9StreamEvidence,
        )

        runtime_id = UUID("00000000-0000-0000-0000-000000000414")
        stream = PostgresEN9StreamEvidence(self.fixture._connection)
        binding = stream.bind(
            FORMULA_APPLICATION_ID,
            (trunk_fixture.FORMULA_ENTITY_ID,),
            SimpleNamespace(
                user_id=UUID("00000000-0000-0000-0000-000000000415"),
                session_id=UUID("00000000-0000-0000-0000-000000000416"),
            ),
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_id, entity_instance_id, payload
                    FROM t_l2_stream_outbox
                    WHERE entity_instance_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (str(trunk_fixture.FORMULA_ENTITY_ID),),
                )
                event_id, entity_id, payload = cursor.fetchone()
        stream.record_acknowledgement(
            binding,
            OutboxEvent(UUID(str(event_id)), UUID(str(entity_id)), payload),
            runtime_id,
        )

    def _ingest_late_source_without_rewinding_latest(self) -> None:
        from app.services.data_trunk import DataTrunk
        from app.services.data_trunk_postgres import PostgresDataTrunkRepository

        late_trunk = DataTrunk(
            PostgresDataTrunkRepository(
                connection_factory=self.fixture._connection,
                clock=lambda: datetime(2026, 8, 17, 1, 0, 2, tzinfo=UTC),
            )
        )
        receipt = late_trunk.ingest(
            (
                self.fixture.raw_power(
                    99_999.0,
                    sequence=3,
                    observed_at=datetime(2026, 8, 17, 0, 59, 30, tzinfo=UTC),
                ),
            )
        )
        self.assertEqual(1, receipt.late_observation_count)

    @staticmethod
    def _seed_second_pcs_and_application(cursor) -> None:
        cursor.execute(
            "SELECT identity_installation_id FROM t_device_instances WHERE node_id = %s",
            (str(UUID(int=1)),),
        )
        identity_id = cursor.fetchone()[0]
        cursor.execute(
            "SELECT solution_installation_id FROM t_installed_point_processings WHERE id = %s",
            (str(trunk_fixture.FORMULA_INSTALLATION_ID),),
        )
        solution_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO t_nodes (id, name, parent_id, node_type)
            VALUES (
              '00000000-0000-0000-0000-000000000410', 'PCS-02',
              '00000000-0000-0000-0000-000000000400', 'PCS'
            );
            INSERT INTO t_device_instances
              (id, identity_installation_id, slot_id, instance_key,
               device_category, display_name, node_id)
            VALUES (
              '00000000-0000-0000-0000-000000000411', %s,
              'pcs.second', 'PCS-02', 'PCS', 'PCS 02',
              '00000000-0000-0000-0000-000000000410'
            );
            INSERT INTO t_entity_instances
              (id, device_instance_id, definition_id, display_name,
               data_type, unit, direction, freshness_seconds, source_kind)
            VALUES (
              %s, '00000000-0000-0000-0000-000000000411',
              'pcs.active_power', 'PCS 02 有功功率',
              'FLOAT', 'kW', 'R', 30, 'point_processing'
            );
            INSERT INTO t_point_processing_selector_members
              (installed_processing_id, input_id, ordinal,
               entity_instance_id, selector_digest)
            VALUES (
              %s, '00000000-0000-0000-0000-000000000405', 1, %s, %s
            );
            INSERT INTO t_point_processing_dependencies
              (installed_processing_id, input_id, output_id,
               source_entity_instance_id, target_entity_instance_id)
            VALUES (
              %s, '00000000-0000-0000-0000-000000000405',
              '00000000-0000-0000-0000-000000000406', %s, %s
            );
            INSERT INTO t_point_processing_plans
              (id, node_id, template_revision_id,
               entity_identity_installation_id, solution_installation_id,
               base_site_configuration_version, source_catalog_digest,
               status, items, blockers, digest, planned_by)
            VALUES (
              '00000000-0000-0000-0000-000000000412',
              '00000000-0000-0000-0000-000000000410', %s, %s, %s,
              1, %s, 'applied', '[]', '[]', %s, 'user:installer'
            );
            INSERT INTO t_installed_point_processings
              (id, node_id, revision_id, source_plan_id,
               solution_installation_id, site_configuration_version,
               installed_by, current)
            VALUES (
              '00000000-0000-0000-0000-000000000413',
              '00000000-0000-0000-0000-000000000410', %s,
              '00000000-0000-0000-0000-000000000412', %s,
              1, 'user:installer', TRUE
            );
            INSERT INTO t_point_processing_input_bindings
              (installed_processing_id, input_id, source_kind, l0_tag_id,
               confirmed_by)
            VALUES (
              '00000000-0000-0000-0000-000000000413',
              '00000000-0000-0000-0000-000000000207', 'l0',
              '00000000-0000-0000-0000-000000000011', 'user:installer'
            );
            INSERT INTO t_point_processing_output_bindings
              (installed_processing_id, output_id, entity_instance_id)
            VALUES (
              '00000000-0000-0000-0000-000000000413',
              '00000000-0000-0000-0000-000000000208', %s
            );
            INSERT INTO t_point_processing_applications
              (id, plan_id, installed_processing_id, solution_installation_id,
               site_configuration_version, actor, output_entity_instance_ids)
            VALUES (
              %s, '00000000-0000-0000-0000-000000000407', %s, %s,
              1, 'user:acceptance', ARRAY[%s]::uuid[]
            )
            """,
            (
                identity_id,
                str(SECOND_PCS_ENTITY_ID),
                str(trunk_fixture.FORMULA_INSTALLATION_ID), str(SECOND_PCS_ENTITY_ID), "c" * 64,
                str(trunk_fixture.FORMULA_INSTALLATION_ID), str(SECOND_PCS_ENTITY_ID),
                str(trunk_fixture.FORMULA_ENTITY_ID),
                str(trunk_fixture.REVISION_ID), identity_id, solution_id,
                "8" * 64, "7" * 64,
                str(trunk_fixture.REVISION_ID), solution_id,
                str(SECOND_PCS_ENTITY_ID),
                str(FORMULA_APPLICATION_ID), str(trunk_fixture.FORMULA_INSTALLATION_ID),
                solution_id, str(trunk_fixture.FORMULA_ENTITY_ID),
            ),
        )

    def test_report_proves_two_frozen_sources_and_immutable_runtime_evidence(self) -> None:
        from app.services.cross_node_processing_acceptance import (
            run_cross_node_processing_acceptance,
        )

        report = run_cross_node_processing_acceptance(
            FORMULA_APPLICATION_ID,
            connection_factory=self.fixture._connection,
        )

        self.assertTrue(report.passed, report.checks)
        self.assertEqual(2, report.frozen_member_count)
        self.assertEqual("site.total_pcs_power", report.output_definition_id)
        self.assertEqual(2, report.source_entity_count)
        self.assertEqual(20.0, report.output_value)
        self.assertTrue(report.restart_continuity)
        self.assertTrue(report.authenticated_ws_delivery)
        self.assertTrue(report.late_input_protected)
        replay = run_cross_node_processing_acceptance(
            FORMULA_APPLICATION_ID,
            connection_factory=self.fixture._connection,
        )
        self.assertEqual(report.id, replay.id)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                with self.assertRaises(psycopg2.DatabaseError):
                    cursor.execute(
                        "UPDATE t_cross_node_processing_acceptance_reports "
                        "SET status = 'failed' WHERE id = %s",
                        (str(report.id),),
                    )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT evidence FROM t_cross_node_processing_acceptance_reports "
                    "WHERE id = %s",
                    (str(report.id),),
                )
                evidence = cursor.fetchone()[0]
        self.assertEqual(str(FORMULA_APPLICATION_ID), evidence["application_id"])
        self.assertEqual(
            str(trunk_fixture.FORMULA_INSTALLATION_ID),
            evidence["installed_processing_id"],
        )


if __name__ == "__main__":
    unittest.main()
