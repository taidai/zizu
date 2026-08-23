from __future__ import annotations

import os
from pathlib import Path
import unittest

import psycopg2

from tests.test_alarm_configuration_postgres import (
    _PostgresAlarmConfigurationTestBase,
)


MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "init-db"
MIGRATIONS_THROUGH_037 = tuple(
    MIGRATIONS_ROOT / name
    for name in (
        "migration_020_solution_delivery.sql",
        "migration_021_identity.sql",
        "migration_022_websocket_tickets.sql",
        "migration_023_site_configuration_parameters.sql",
        "migration_024_entity_instances.sql",
        "migration_025_rule_entity_instance_refs.sql",
        "migration_026_control_commands.sql",
        "migration_027_nullable_control_target.sql",
        "migration_028_rule_control_commands.sql",
        "migration_029_unified_alarm_runtime.sql",
        "migration_030_rule_alarm_and_legacy_gate.sql",
        "migration_031_ems_policy_activations.sql",
        "migration_032_release_locks.sql",
        "migration_034_unified_alarm_configuration.sql",
        "migration_035_legacy_alarm_contract_gate.sql",
        "migration_036_alarm_configuration_acceptance.sql",
        "migration_037_alarm_configuration_application_kinds.sql",
    )
)
MIGRATION_024 = MIGRATIONS_ROOT / "migration_024_entity_instances.sql"
MIGRATION_038 = MIGRATIONS_ROOT / "migration_038_pcs_data_trunk.sql"
MIGRATION_039 = MIGRATIONS_ROOT / "migration_039_pcs_data_trunk_contract_gate.sql"
MIGRATION_040 = MIGRATIONS_ROOT / "migration_040_point_processing.sql"
MIGRATION_041 = MIGRATIONS_ROOT / "migration_041_en9_runtime_evidence.sql"
MIGRATION_042 = MIGRATIONS_ROOT / "migration_042_cross_node_formulas.sql"


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL integration tests",
)
class DataTrunkMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Data trunk tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    @staticmethod
    def _reset_through_037(cursor) -> None:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for migration in MIGRATIONS_THROUGH_037:
            if migration == MIGRATION_024:
                _PostgresAlarmConfigurationTestBase._create_source_catalog_tables(
                    cursor
                )
            cursor.execute(migration.read_text(encoding="utf-8"))

    @staticmethod
    def _apply_038(cursor) -> None:
        cursor.execute(MIGRATION_038.read_text(encoding="utf-8"))

    @staticmethod
    def _apply_039(cursor) -> None:
        cursor.execute(MIGRATION_039.read_text(encoding="utf-8"))

    @staticmethod
    def _apply_040(cursor) -> None:
        cursor.execute(MIGRATION_040.read_text(encoding="utf-8"))

    @staticmethod
    def _apply_041(cursor) -> None:
        cursor.execute(MIGRATION_041.read_text(encoding="utf-8"))

    @staticmethod
    def _apply_042(cursor) -> None:
        cursor.execute(MIGRATION_042.read_text(encoding="utf-8"))

    @classmethod
    def _reset_through_041(cls, cursor) -> None:
        cls._reset_through_037(cursor)
        cls._apply_038(cursor)
        cls._apply_039(cursor)
        cls._apply_040(cursor)
        cls._apply_041(cursor)

    def test_042_adds_canonical_formula_and_frozen_selector_schema(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_041(cursor)
                self._apply_042(cursor)

                cursor.execute(
                    """
                    SELECT to_regclass('t_point_processing_expressions'),
                           to_regclass('t_point_processing_selectors'),
                           to_regclass('t_point_processing_selector_members'),
                           to_regclass('t_point_processing_dependencies'),
                           to_regclass('t_point_processing_formula_runs')
                    """
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (
                        "t_point_processing_expressions",
                        "t_point_processing_selectors",
                        "t_point_processing_selector_members",
                        "t_point_processing_dependencies",
                        "t_point_processing_formula_runs",
                    ),
                )
                cursor.execute(
                    """
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 't_nodes'
                      AND column_name = 'parent_id'
                    """
                )
                self.assertEqual(("uuid",), cursor.fetchone())

                cursor.execute(
                    """
                    SELECT constraint_name
                    FROM information_schema.table_constraints
                    WHERE table_schema = 'public'
                      AND table_name = 't_point_processing_dependencies'
                      AND constraint_type = 'CHECK'
                    ORDER BY constraint_name
                    """
                )
                self.assertIn(
                    "chk_point_processing_dependency_not_self",
                    {row[0] for row in cursor.fetchall()},
                )

    def test_042_replays_after_complete_application(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_041(cursor)
                self._apply_042(cursor)
                self._apply_042(cursor)
                cursor.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name LIKE 't_point_processing_%'"
                )
                self.assertGreaterEqual(cursor.fetchone()[0], 10)

    def test_042_rejects_mixed_schema_without_recording_version(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_041(cursor)
                cursor.execute(
                    "CREATE TABLE t_point_processing_expressions "
                    "(output_id UUID PRIMARY KEY)"
                )
                cursor.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
                )
                with self.assertRaises(psycopg2.DatabaseError):
                    self._apply_042(cursor)
                cursor.execute(
                    "SELECT count(*) FROM schema_migrations WHERE version='042'"
                )
                self.assertEqual(cursor.fetchone(), (0,))

    def test_040_hard_cuts_point_conversion_to_point_processing(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_037(cursor)
                self._apply_038(cursor)
                self._apply_039(cursor)
                self._apply_040(cursor)
                self._apply_041(cursor)

                cursor.execute(
                    """
                    SELECT to_regclass('t_point_processing_templates'),
                           to_regclass('t_point_conversion_templates'),
                           to_regclass('t_installed_point_processings'),
                           to_regclass('t_boolean_set_transform_rules')
                    """
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (
                        "t_point_processing_templates",
                        None,
                        "t_installed_point_processings",
                        "t_boolean_set_transform_rules",
                    ),
                )

                cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 't_tags'
                      AND column_name IN (
                        'wire_data_type', 'value_data_type', 'source_address',
                        'decimal', 'read_only'
                      )
                    ORDER BY column_name
                    """
                )
                self.assertEqual(
                    [row[0] for row in cursor.fetchall()],
                    [
                        "decimal",
                        "read_only",
                        "source_address",
                        "value_data_type",
                        "wire_data_type",
                    ],
                )

    def test_040_replays_without_reintroducing_legacy_schema(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_037(cursor)
                self._apply_038(cursor)
                self._apply_039(cursor)
                self._apply_040(cursor)
                self._apply_040(cursor)
                self._apply_041(cursor)
                self._apply_041(cursor)
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name LIKE '%point_conversion%'
                    """
                )
                self.assertEqual(cursor.fetchone(), (0,))

    def test_041_upgrades_a_database_that_already_recorded_040(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_037(cursor)
                self._apply_038(cursor)
                self._apply_039(cursor)
                self._apply_040(cursor)
                cursor.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
                )
                cursor.execute(
                    "INSERT INTO schema_migrations(version) VALUES ('040') "
                    "ON CONFLICT DO NOTHING"
                )
                self._apply_041(cursor)
                cursor.execute(
                    """
                    SELECT to_regclass('t_runtime_health_samples'),
                           column_name
                    FROM information_schema.columns
                    WHERE table_name = 't_l2_observations'
                      AND column_name = 'producing_runtime_instance_id'
                    """
                )
                self.assertEqual(
                    cursor.fetchone(),
                    ("t_runtime_health_samples", "producing_runtime_instance_id"),
                )

    def test_039_upgrades_038_and_replays_contract_triggers(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_037(cursor)
                self._apply_038(cursor)
                self._apply_039(cursor)
                self._apply_039(cursor)

                cursor.execute(
                    """
                    SELECT tgname
                    FROM pg_trigger
                    WHERE NOT tgisinternal
                      AND tgname IN (
                        'trg_entity_instance_binding_single_source',
                        'trg_conversion_output_binding_single_source',
                        'trg_entity_instance_kind_single_source',
                        'trg_installed_conversion_single_source'
                      )
                    ORDER BY tgname
                    """
                )
                self.assertEqual(
                    [row[0] for row in cursor.fetchall()],
                    [
                        "trg_conversion_output_binding_single_source",
                        "trg_entity_instance_binding_single_source",
                        "trg_entity_instance_kind_single_source",
                        "trg_installed_conversion_single_source",
                    ],
                )

    def test_038_upgrades_037_and_replays(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_037(cursor)
                self._apply_038(cursor)
                self._apply_038(cursor)

                for table in (
                    "t_point_conversion_revisions",
                    "t_enum_transform_rules",
                    "t_fault_code_transform_rules",
                    "t_l2_observations",
                    "t_l2_latest",
                    "t_l2_stream_outbox",
                ):
                    with self.subTest(table=table):
                        cursor.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                        self.assertEqual(cursor.fetchone(), (table,))

                for table, column in (
                    ("t_telemetry", "raw_value_float"),
                    ("t_telemetry_latest", "source_digest"),
                ):
                    with self.subTest(table=table, column=column):
                        cursor.execute(
                            """
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = %s
                              AND column_name = %s
                            """,
                            (table, column),
                        )
                        self.assertEqual(cursor.fetchone(), (1,))

    def test_038_preserves_existing_legacy_telemetry(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_037(cursor)
                cursor.execute(
                    """
                    INSERT INTO t_nodes (id, name, source_catalog_key)
                    VALUES (
                      '10000000-0000-0000-0000-000000000001',
                      'PCS-01',
                      'PCS-01'
                    );
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, unit, read_write, enabled)
                    VALUES (
                      '10000000-0000-0000-0000-000000000011',
                      '10000000-0000-0000-0000-000000000001',
                      'ActivePower',
                      'FLOAT',
                      'W',
                      'R',
                      TRUE
                    );
                    INSERT INTO t_telemetry
                      (ts, node_id, tag_id, value_float, quality)
                    VALUES (
                      '2026-08-17T00:00:00Z',
                      '10000000-0000-0000-0000-000000000001',
                      '10000000-0000-0000-0000-000000000011',
                      12345,
                      192
                    );
                    INSERT INTO t_telemetry_latest
                      (node_id, tag_id, ts, value_float, quality)
                    VALUES (
                      '10000000-0000-0000-0000-000000000001',
                      '10000000-0000-0000-0000-000000000011',
                      '2026-08-17T00:00:00Z',
                      12345,
                      192
                    );
                    """
                )

                self._apply_038(cursor)

                cursor.execute(
                    """
                    SELECT value_float, raw_value_float, source_digest
                    FROM t_telemetry
                    """
                )
                self.assertEqual(cursor.fetchone(), (12345.0, None, None))
                cursor.execute(
                    """
                    SELECT value_float, raw_value_float, source_digest
                    FROM t_telemetry_latest
                    """
                )
                self.assertEqual(cursor.fetchone(), (12345.0, None, None))

    def test_l2_typed_value_is_checked_against_entity_definition(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_037(cursor)
                self._apply_038(cursor)
                installation_id, _ = (
                    _PostgresAlarmConfigurationTestBase._insert_installed_site(cursor)
                )
                entity_id, _ = _PostgresAlarmConfigurationTestBase._insert_entities(
                    cursor,
                    installation_id,
                )
                cursor.execute(
                    """
                    INSERT INTO t_point_conversion_templates
                      (id, asset_id, device_category, brand, model,
                       display_name, status)
                    VALUES (
                      '20000000-0000-0000-0000-000000000001',
                      'pcs.brand-a', 'pcs', 'Brand A', 'PCS-A',
                      'Brand A PCS', 'active'
                    );
                    INSERT INTO t_point_conversion_revisions
                      (id, template_id, revision, content_digest, published_at)
                    VALUES (
                      '20000000-0000-0000-0000-000000000002',
                      '20000000-0000-0000-0000-000000000001',
                      1,
                      %s,
                      '2026-08-17T00:00:00Z'
                    );
                    """,
                    ("c" * 64,),
                )
                cursor.execute(
                    """
                    INSERT INTO t_l2_observations
                      (observed_at, event_id, entity_instance_id, received_at,
                       calculated_at, value_float, quality,
                       conversion_revision_id, site_configuration_version,
                       source_digest, source_order_key)
                    VALUES (
                      '2026-08-17T00:00:00Z',
                      '20000000-0000-0000-0000-000000000101',
                      %s,
                      '2026-08-17T00:00:01Z',
                      '2026-08-17T00:00:02Z',
                      12.345,
                      192,
                      '20000000-0000-0000-0000-000000000002',
                      1,
                      %s,
                      'S:00000000000000000001:source'
                    )
                    """,
                    (entity_id, "d" * 64),
                )

                with self.assertRaises(psycopg2.errors.CheckViolation) as raised:
                    cursor.execute(
                        """
                        INSERT INTO t_l2_observations
                          (observed_at, event_id, entity_instance_id, received_at,
                           calculated_at, value_text, quality,
                           conversion_revision_id, site_configuration_version,
                           source_digest, source_order_key)
                        VALUES (
                          '2026-08-17T00:00:03Z',
                          '20000000-0000-0000-0000-000000000102',
                          %s,
                          '2026-08-17T00:00:04Z',
                          '2026-08-17T00:00:05Z',
                          '12.345',
                          192,
                          '20000000-0000-0000-0000-000000000002',
                          1,
                          %s,
                          'S:00000000000000000002:source'
                        )
                        """,
                        (entity_id, "e" * 64),
                    )
                self.assertEqual(
                    raised.exception.diag.constraint_name,
                    "chk_l2_entity_data_type",
                )

                cursor.execute(
                    """
                    INSERT INTO t_l2_latest
                      (entity_instance_id, event_id, observed_at, received_at,
                       calculated_at, quality, reason, conversion_revision_id,
                       site_configuration_version, source_digest,
                       source_order_key)
                    VALUES (
                      %s,
                      '20000000-0000-0000-0000-000000000103',
                      '2026-08-17T00:00:06Z',
                      '2026-08-17T00:00:07Z',
                      '2026-08-17T00:00:08Z',
                      0,
                      'UNIT_MISMATCH',
                      '20000000-0000-0000-0000-000000000002',
                      1,
                      %s,
                      'S:00000000000000000003:source'
                    )
                    """,
                    (entity_id, "f" * 64),
                )
                cursor.execute("SELECT count(*) FROM t_l2_observations")
                self.assertEqual(cursor.fetchone(), (1,))
                cursor.execute("SELECT count(*) FROM t_l2_latest")
                self.assertEqual(cursor.fetchone(), (1,))

    def test_038_rejects_mutation_of_append_only_configuration_and_history(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._reset_through_037(cursor)
                self._apply_038(cursor)
                cursor.execute(
                    """
                    INSERT INTO t_point_conversion_templates
                      (id, asset_id, device_category, brand, model,
                       display_name, status)
                    VALUES (
                      '30000000-0000-0000-0000-000000000001',
                      'pcs.append-only', 'pcs', 'Brand A', 'PCS-A',
                      'Append-only PCS', 'active'
                    );
                    INSERT INTO t_point_conversion_revisions
                      (id, template_id, revision, content_digest, published_at)
                    VALUES (
                      '30000000-0000-0000-0000-000000000002',
                      '30000000-0000-0000-0000-000000000001',
                      1, %s, '2026-08-17T00:00:00Z'
                    );
                    """,
                    ("a" * 64,),
                )

                for statement in (
                    "UPDATE t_point_conversion_revisions SET revision = 2",
                    "DELETE FROM t_point_conversion_revisions",
                    "TRUNCATE t_point_conversion_revisions CASCADE",
                ):
                    with self.subTest(statement=statement):
                        with self.assertRaises(psycopg2.DatabaseError) as raised:
                            cursor.execute(statement)
                        self.assertEqual(raised.exception.pgcode, "55000")

                cursor.execute(
                    "SELECT revision FROM t_point_conversion_revisions"
                )
                self.assertEqual(cursor.fetchone(), (1,))


if __name__ == "__main__":
    unittest.main()
