from __future__ import annotations

import os
import unittest
from uuid import UUID

import psycopg2

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


if __name__ == "__main__":
    unittest.main()
