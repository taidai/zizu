from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg2

from tests.test_alarm_configuration_postgres import (
    _PostgresAlarmConfigurationTestBase,
)
from tests import test_data_trunk_migration_postgres


MIGRATION_044 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_044_node_data_trunk_hard_cut.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL hard-cut migration tests",
)
class NodeDataTrunkHardCutMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Hard-cut migration tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    @staticmethod
    def _reset_through_043(cursor) -> None:
        migration_test = (
            test_data_trunk_migration_postgres.DataTrunkMigrationPostgresTest
        )
        migration_test._reset_through_041(cursor)
        migration_test._apply_042(cursor)
        migration_test._apply_043(cursor)

    @staticmethod
    def _apply_044(cursor) -> None:
        cursor.execute(MIGRATION_044.read_text(encoding="utf-8"))

    def test_044_creates_direct_configuration_schema_and_removes_product_tables(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_043(cursor)
                self._apply_044(cursor)
                cursor.execute(
                    "SELECT to_regclass('t_configuration_state'), "
                    "to_regclass('t_configuration_revisions'), "
                    "to_regclass('t_configuration_audit')"
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (
                        "t_configuration_state",
                        "t_configuration_revisions",
                        "t_configuration_audit",
                    ),
                )
                cursor.execute("SELECT to_regclass('t_l2_control_bindings')")
                self.assertEqual(cursor.fetchone()[0], "t_l2_control_bindings")
                for removed in (
                    "t_solution_packages",
                    "t_solution_installations",
                    "t_device_instances",
                    "t_delivery_reports",
                    "t_en9_acceptance_reports",
                    "t_cross_node_processing_acceptance_reports",
                    "t_business_metric_templates",
                    "t_business_metric_window_results",
                ):
                    with self.subTest(table=removed):
                        cursor.execute("SELECT to_regclass(%s)", (removed,))
                        self.assertIsNone(cursor.fetchone()[0])

    def test_044_preserves_entity_uuid_and_moves_ownership_to_node(self) -> None:
        entity_id = uuid4()
        node_id = uuid4()
        device_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_043(cursor)
                installation_id, _ = (
                    _PostgresAlarmConfigurationTestBase._insert_installed_site(cursor)
                )
                cursor.execute(
                    "INSERT INTO t_nodes (id, name, source_catalog_key) "
                    "VALUES (%s, 'PCS-01', 'PCS-01')",
                    (str(node_id),),
                )
                cursor.execute(
                    """
                    INSERT INTO t_device_instances
                      (id, identity_installation_id, slot_id, instance_key,
                       device_category, display_name, node_id)
                    VALUES (%s, %s, 'pcs', 'PCS-01', 'pcs', 'PCS 01', %s)
                    """,
                    (str(device_id), installation_id, str(node_id)),
                )
                cursor.execute(
                    """
                    SET session_replication_role = replica;
                    INSERT INTO t_entity_instances
                      (id, device_instance_id, definition_id, display_name,
                       data_type, unit, direction, freshness_seconds, source_kind)
                    VALUES (%s, %s, 'pcs.activePower', 'Active power',
                            'FLOAT', 'kW', 'R', 30, 'point_processing')
                    """,
                    (str(entity_id), str(device_id)),
                )
                cursor.execute("SET session_replication_role = origin")
                self._apply_044(cursor)
                cursor.execute(
                    "SELECT id, node_id, definition_id FROM t_entity_instances WHERE id=%s",
                    (str(entity_id),),
                )
                migrated = cursor.fetchone()
                self.assertEqual(
                    (str(migrated[0]), str(migrated[1]), migrated[2]),
                    (str(entity_id), str(node_id), "pcs.activePower"),
                )
                cursor.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='t_entity_instances' "
                    "AND column_name='device_instance_id'"
                )
                self.assertEqual(cursor.fetchone()[0], 0)
                cursor.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='public' "
                    "AND table_name='t_point_processing_revisions' "
                    "AND column_name='content'"
                )
                self.assertEqual(cursor.fetchone()[0], 1)

    def test_044_replay_keeps_the_same_configuration_revision(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_043(cursor)
                self._apply_044(cursor)
                self._apply_044(cursor)
                cursor.execute(
                    "SELECT current_revision FROM t_configuration_state "
                    "WHERE singleton=TRUE"
                )
                self.assertEqual(cursor.fetchone()[0], 0)
                cursor.execute("SELECT count(*) FROM t_configuration_revisions")
                self.assertEqual(cursor.fetchone()[0], 1)

    def test_044_rejects_unmapped_entity_before_dropping_solution_tables(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_043(cursor)
                installation_id, _ = (
                    _PostgresAlarmConfigurationTestBase._insert_installed_site(cursor)
                )
                cursor.execute(
                    """
                    INSERT INTO t_device_instances
                      (id, identity_installation_id, slot_id, instance_key,
                       device_category, display_name)
                    VALUES (%s, %s, 'pcs', 'PCS-UNKNOWN', 'pcs', 'PCS unknown')
                    """,
                    (str(uuid4()), installation_id),
                )
                cursor.execute(
                    """
                    SET session_replication_role = replica;
                    INSERT INTO t_entity_instances
                      (id, device_instance_id, definition_id, display_name,
                       data_type, unit, direction, freshness_seconds, source_kind)
                    SELECT %s, id, 'pcs.activePower', 'Unmapped power',
                           'FLOAT', 'kW', 'R', 30, 'point_processing'
                    FROM t_device_instances WHERE instance_key='PCS-UNKNOWN'
                    """,
                    (str(uuid4()),),
                )
                cursor.execute("SET session_replication_role = origin")
                with self.assertRaisesRegex(
                    psycopg2.DatabaseError,
                    "HARD_CUT_ENTITY_NODE_AMBIGUOUS",
                ):
                    self._apply_044(cursor)
                connection.rollback()
                cursor.execute("SELECT to_regclass('t_solution_packages')")
                self.assertEqual(cursor.fetchone()[0], "t_solution_packages")


if __name__ == "__main__":
    unittest.main()
