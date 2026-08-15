from __future__ import annotations

import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from datetime import datetime, timezone
import hashlib
import json
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg2


os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_ROOT = BACKEND_ROOT.parent / "init-db"
MIGRATION_034 = MIGRATIONS_ROOT / "migration_034_unified_alarm_configuration.sql"
MIGRATION_035 = MIGRATIONS_ROOT / "migration_035_legacy_alarm_contract_gate.sql"
BASE_MIGRATIONS = tuple(
    MIGRATIONS_ROOT / f"migration_{version:03d}{suffix}"
    for version, suffix in (
        (20, "_solution_delivery.sql"),
        (21, "_identity.sql"),
        (22, "_websocket_tickets.sql"),
        (23, "_site_configuration_parameters.sql"),
        (24, "_entity_instances.sql"),
        (25, "_rule_entity_instance_refs.sql"),
        (26, "_control_commands.sql"),
        (27, "_nullable_control_target.sql"),
        (28, "_rule_control_commands.sql"),
        (29, "_unified_alarm_runtime.sql"),
        (30, "_rule_alarm_and_legacy_gate.sql"),
        (31, "_ems_policy_activations.sql"),
        (32, "_release_locks.sql"),
    )
)


class _PostgresAlarmConfigurationTestBase:
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Postgres alarm configuration tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    @staticmethod
    def _create_source_catalog_tables(cursor) -> None:
        from tests.test_delivery_postgres_public_api import DeliveryPostgresPublicApiTest

        DeliveryPostgresPublicApiTest._create_source_catalog_tables(cursor)
        cursor.execute(
            """
            ALTER TABLE t_tags ADD COLUMN IF NOT EXISTS display_name TEXT;
            CREATE TABLE IF NOT EXISTS t_fault_maps (
                id UUID PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                entries JSONB NOT NULL DEFAULT '[]'::jsonb
            );
            CREATE TABLE IF NOT EXISTS t_alarm_levels (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                severity TEXT NOT NULL,
                color TEXT,
                trigger_rules JSONB NOT NULL DEFAULT '[]'::jsonb,
                enabled BOOLEAN DEFAULT TRUE,
                sort_order INTEGER DEFAULT 0,
                is_system BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS t_entity_alarm_bindings (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                entity_id UUID NOT NULL REFERENCES t_entities(id),
                alarm_level_id UUID NOT NULL REFERENCES t_alarm_levels(id),
                trigger_rules JSONB,
                fault_map_id UUID REFERENCES t_fault_maps(id),
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (entity_id, alarm_level_id)
            );
            """
        )

    def _reset_schema_through_032(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
                for migration in BASE_MIGRATIONS:
                    if migration.name.startswith("migration_024"):
                        self._create_source_catalog_tables(cursor)
                    cursor.execute(migration.read_text(encoding="utf-8"))

    @staticmethod
    def _apply_alarm_migrations(cursor) -> None:
        cursor.execute(MIGRATION_034.read_text(encoding="utf-8"))
        cursor.execute(MIGRATION_035.read_text(encoding="utf-8"))

    @staticmethod
    def _insert_installed_site(cursor) -> tuple[str, str]:
        package_record_id = str(uuid4())
        install_plan_id = str(uuid4())
        installation_id = str(uuid4())
        package_digest = "a" * 64
        configuration_digest = "b" * 64
        cursor.execute(
            """
            INSERT INTO t_solution_packages
              (id, package_id, version, display_name, digest, status,
               acceptance_ids, manifest)
            VALUES (%s, 'org.zizu.alarm-configuration-test', '1.0.0',
                    'Alarm configuration test', %s, 'validated', '[]', '{}')
            """,
            (package_record_id, package_digest),
        )
        cursor.execute(
            """
            INSERT INTO t_solution_install_plans
              (id, package_record_id, package_digest,
               base_site_configuration_version, status, items, blockers,
               parameter_contracts, parameters, secret_references,
               parameter_sources, parameter_metadata, configuration_digest,
               target_installation_id, entity_identity_installation_id, digest)
            VALUES (%s, %s, %s, 0, 'ready', '[]', '[]', '[]',
                    '{"rated_power_kw": 100}',
                    '{"neuron.credentials": "secret://site/neuron"}',
                    '{"rated_power_kw": "site_override"}',
                    '{"rated_power_kw": {"unit": "kW"}}', %s, %s, %s, %s)
            """,
            (
                install_plan_id,
                package_record_id,
                package_digest,
                configuration_digest,
                installation_id,
                installation_id,
                "c" * 64,
            ),
        )
        cursor.execute(
            """
            INSERT INTO t_solution_installations
              (id, plan_id, package_record_id, package_digest,
               site_configuration_version, status)
            VALUES (%s, %s, %s, %s, 1, 'installed')
            """,
            (installation_id, install_plan_id, package_record_id, package_digest),
        )
        cursor.execute(
            """
            INSERT INTO t_site_configuration_versions
              (version, previous_version, installation_id, package_record_id,
               package_digest, parameters, secret_references,
               parameter_metadata, configuration_digest, actor,
               entity_identity_installation_id)
            VALUES (1, 0, %s, %s, %s, '{"rated_power_kw": 100}',
                    '{"neuron.credentials": "secret://site/neuron"}',
                    '{"rated_power_kw": {"unit": "kW"}}', %s,
                    'user:installer', %s)
            """,
            (
                installation_id,
                package_record_id,
                package_digest,
                configuration_digest,
                installation_id,
            ),
        )
        cursor.execute(
            "UPDATE t_site_configuration_state SET current_version = 1 WHERE singleton = TRUE"
        )
        return installation_id, package_record_id

    @staticmethod
    def _insert_entities(cursor, installation_id: str) -> tuple[str, str]:
        entity_ids: list[str] = []
        for index in (1, 2):
            node_id = str(uuid4())
            tag_id = str(uuid4())
            device_id = str(uuid4())
            entity_id = str(uuid4())
            confirmation_id = str(uuid4())
            binding_id = str(uuid4())
            cursor.execute(
                "INSERT INTO t_nodes (id, name, source_catalog_key) VALUES (%s, %s, %s)",
                (node_id, f"PCS-{index:02d}", f"PCS-{index:02d}"),
            )
            cursor.execute(
                """
                INSERT INTO t_tags
                  (id, node_id, name, data_type, unit, read_write, enabled)
                VALUES (%s, %s, 'ActivePower', 'FLOAT', 'kW', 'R', TRUE)
                """,
                (tag_id, node_id),
            )
            cursor.execute(
                """
                INSERT INTO t_device_instances
                  (id, identity_installation_id, slot_id, instance_key,
                   device_category, display_name)
                VALUES (%s, %s, 'pcs', %s, 'pcs', %s)
                """,
                (device_id, installation_id, f"PCS-{index:02d}", f"PCS {index:02d}"),
            )
            cursor.execute(
                """
                INSERT INTO t_entity_instances
                  (id, device_instance_id, definition_id, display_name,
                   data_type, unit, direction, freshness_seconds)
                VALUES (%s, %s, 'pcs.activePower', %s,
                        'FLOAT', 'kW', 'R', 30)
                """,
                (entity_id, device_id, f"PCS {index:02d} active power"),
            )
            cursor.execute(
                """
                INSERT INTO t_entity_binding_confirmations
                  (id, entity_instance_id, binding_id, actor, matcher_id,
                   reason, plan_digest, selected_tag_id)
                VALUES (%s, %s, %s, 'user:installer', 'exact-name',
                        'confirmed test source', %s, %s)
                """,
                (confirmation_id, entity_id, binding_id, "d" * 64, tag_id),
            )
            cursor.execute(
                """
                INSERT INTO t_entity_instance_bindings
                  (id, entity_instance_id, tag_id, matcher_id,
                   confirmation_audit_id, active)
                VALUES (%s, %s, %s, 'exact-name', %s, TRUE)
                """,
                (binding_id, entity_id, tag_id, confirmation_id),
            )
            entity_ids.append(entity_id)
        cursor.execute(
            "UPDATE t_solution_installations SET entity_instance_ids = %s::uuid[] WHERE id = %s",
            (entity_ids, installation_id),
        )
        return entity_ids[0], entity_ids[1]


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the isolated Postgres alarm configuration seam",
)
class AlarmConfigurationMigrationTest(_PostgresAlarmConfigurationTestBase, unittest.TestCase):
    def _build_legacy_plan(self, repository, installation_id, selections=None):
        from app.services.alarm_configuration import compile_legacy_migration_plan

        current_installation_id, sources = repository.list_legacy_alarm_sources()
        self.assertEqual(UUID(installation_id), current_installation_id)
        return compile_legacy_migration_plan(
            installation_id=current_installation_id,
            sources=sources,
            selections=selections or {},
            actor="user:engineer",
        )

    def test_runner_applies_035_after_recorded_034_and_enforces_legacy_gate(self) -> None:
        from app.core.migrations import run_migrations
        from app.services import telemetry_store

        self._reset_schema_through_032()
        level_id = uuid4()
        entity_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                self._insert_entities(cursor, installation_id)
                cursor.execute(
                    "INSERT INTO t_alarm_levels (id, code, name, severity) VALUES (%s, 'runner-legacy', 'Runner legacy', 'MAJOR')",
                    (str(level_id),),
                )
                cursor.execute(
                    "INSERT INTO t_entities (id, name, enabled) VALUES (%s, 'pcs.runnerLegacy', TRUE)",
                    (str(entity_id),),
                )
                cursor.execute(
                    "UPDATE t_tags SET alarm_type = 'fault' WHERE id = (SELECT id FROM t_tags LIMIT 1)"
                )
                cursor.execute(MIGRATION_034.read_text(encoding="utf-8"))
                cursor.execute(
                    "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
                )
                versions = sorted({
                    path.name.split("_")[1]
                    for path in MIGRATIONS_ROOT.glob("migration_*.sql")
                    if int(path.name.split("_")[1]) <= 34
                })
                cursor.executemany(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    [(version,) for version in versions],
                )

        runner_connection = psycopg2.connect(**self.connection_kwargs)
        try:
            with patch.dict(
                os.environ,
                {"DEPLOYMENT_MODE": "development", "MIGRATIONS_DIR": str(MIGRATIONS_ROOT)},
            ), patch.object(
                telemetry_store, "get_connection", return_value=runner_connection
            ):
                result = run_migrations()
        finally:
            runner_connection.close()

        self.assertEqual(["035"], result["applied"])
        self.assertEqual(0, result["errors"])
        rejected = (
            "INSERT INTO t_alarm_levels (code, name, severity) VALUES ('blocked', 'Blocked', 'INFO')",
            "DELETE FROM t_tags WHERE alarm_type = 'fault'",
        )
        for statement in rejected:
            with self.subTest(statement=statement):
                connection = psycopg2.connect(**self.connection_kwargs)
                try:
                    with connection.cursor() as cursor:
                        with self.assertRaises(psycopg2.errors.RaiseException):
                            cursor.execute(statement)
                finally:
                    connection.close()

    def test_migration_applies_to_a_fresh_schema(self) -> None:
        self._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_alarm_migrations(cursor)
                cursor.execute(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name = 't_alarm_definitions' "
                    "AND constraint_type = 'UNIQUE'"
                )
                self.assertIn(
                    "uq_alarm_definitions_installation_asset_entity_digest",
                    {row[0] for row in cursor.fetchall()},
                )

    def test_migration_upgrades_an_installed_site_and_replays(self) -> None:
        self._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                migration_034 = MIGRATION_034.read_text(encoding="utf-8")
                migration_035 = MIGRATION_035.read_text(encoding="utf-8")
                cursor.execute(migration_034)
                cursor.execute(migration_035)
                rule_set_id = uuid4()
                legacy_plan_json = {"legacy": "canonical-plan-is-immutable"}
                cursor.execute(
                    """
                    INSERT INTO t_alarm_rule_sets
                      (id, rule_set_key, name, created_by)
                    VALUES (%s, 'legacy-key', 'Legacy name', 'user:planner')
                    """,
                    (str(rule_set_id),),
                )
                cursor.execute(
                    """
                    INSERT INTO t_alarm_rule_set_revisions
                      (rule_set_id, revision, rule_set_key, rule_set_name,
                       rules, digest, actor)
                    VALUES (%s, 1, 'legacy-key', 'Legacy name', '[]', %s,
                            'user:planner')
                    """,
                    (str(rule_set_id), "b" * 64),
                )
                cursor.execute(
                    """
                    INSERT INTO t_alarm_configuration_plans
                      (id, source_installation_id,
                       base_site_configuration_version, rule_set_id,
                       rule_set_revision, canonical_plan, digest, status,
                       planned_by)
                    VALUES (%s, %s, 1, %s, 1, %s, %s, 'ready',
                            'user:planner')
                    """,
                    (
                        str(uuid4()),
                        installation_id,
                        str(rule_set_id),
                        json.dumps(legacy_plan_json),
                        "c" * 64,
                    ),
                )
                cursor.execute(migration_034)
                cursor.execute(migration_035)
                cursor.execute(
                    "SELECT canonical_plan FROM t_alarm_configuration_plans"
                )
                self.assertEqual(legacy_plan_json, cursor.fetchone()[0])
                for table_name in (
                    "t_alarm_rule_sets",
                    "t_alarm_rule_set_revisions",
                    "t_alarm_configuration_plans",
                    "t_alarm_definition_origins",
                    "t_legacy_alarm_migrations",
                    "t_legacy_alarm_migration_targets",
                    "t_alarm_configuration_idempotency",
                ):
                    cursor.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
                    self.assertEqual(table_name, cursor.fetchone()[0])

    def test_upgrade_marks_unversioned_digests_unknown_without_rewriting_evidence(self) -> None:
        from app.services.alarm_definitions import (
            AlarmDefinitionPlan,
            InstalledAlarmDefinition,
        )
        from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog
        from psycopg2.extras import register_uuid

        self._reset_schema_through_032()
        register_uuid()
        legacy_definition_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _package_record_id = self._insert_installed_site(cursor)
                entity_id, _ = self._insert_entities(cursor, installation_id)
                definition = InstalledAlarmDefinition(
                    id=legacy_definition_id,
                    asset_id="site.alarm.legacy.high",
                    version="package:1",
                    installation_id=UUID(installation_id),
                    site_configuration_version=1,
                    entity_instance_id=UUID(entity_id),
                    entity_definition_id="pcs.activePower",
                    trigger={"operator": "gt", "value": 90},
                    trigger_duration_seconds=1,
                    recovery={"operator": "lt", "value": 80},
                    recovery_duration_seconds=1,
                    severity="WARNING",
                    notification_throttle_seconds=1,
                )
                old_digest = hashlib.sha256(
                    json.dumps(
                        {
                            "asset_id": definition.asset_id,
                            "version": definition.version,
                            "installation_id": str(definition.installation_id),
                            "site_configuration_version": definition.site_configuration_version,
                            "entity_instance_id": str(definition.entity_instance_id),
                            "entity_definition_id": definition.entity_definition_id,
                            "trigger": definition.trigger,
                            "trigger_duration_seconds": definition.trigger_duration_seconds,
                            "recovery": definition.recovery,
                            "recovery_duration_seconds": definition.recovery_duration_seconds,
                            "severity": definition.severity,
                            "notification_throttle_seconds": definition.notification_throttle_seconds,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                cursor.execute(
                    """
                    INSERT INTO t_alarm_definitions
                      (id, asset_id, definition_version, installation_id,
                       site_configuration_version, entity_instance_id,
                       entity_definition_id, trigger_condition,
                       trigger_duration_seconds, recovery_condition,
                       recovery_duration_seconds, severity,
                       notification_throttle_seconds, content_digest)
                    VALUES (%s, %s, %s, %s, 1, %s, %s, %s, 1, %s, 1,
                            'WARNING', 1, %s)
                    """,
                    (
                        definition.id,
                        definition.asset_id,
                        definition.version,
                        definition.installation_id,
                        definition.entity_instance_id,
                        definition.entity_definition_id,
                        json.dumps(definition.trigger),
                        json.dumps(definition.recovery),
                        old_digest,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_alarm_definition_current
                      (asset_id, entity_instance_id, definition_id,
                       site_configuration_version)
                    VALUES (%s, %s, %s, 1)
                    """,
                    (definition.asset_id, definition.entity_instance_id, definition.id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_alarm_events
                      (id, definition_id, definition_version,
                       entity_instance_id, state, severity, pending_at,
                       recovered_at)
                    VALUES (%s, %s, %s, %s, 'recovered', 'WARNING', %s, %s)
                    """,
                    (
                        uuid4(),
                        definition.id,
                        definition.version,
                        definition.entity_instance_id,
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                    ),
                )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_alarm_migrations(cursor)

        replacement = InstalledAlarmDefinition(
            **{**definition.__dict__, "id": uuid4()}
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            installed_ids = PostgresAlarmDefinitionCatalog().install_definitions(
                AlarmDefinitionPlan(
                    installation_id=definition.installation_id,
                    site_configuration_version=1,
                    package_digest="a" * 64,
                    definitions=(replacement,),
                    digest="e" * 64,
                ),
                transaction=connection,
            )
        self.assertEqual((legacy_definition_id,), installed_ids)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*), min(content_digest),
                           min(content_digest_algorithm)
                    FROM t_alarm_definitions
                    WHERE installation_id = %s AND asset_id = %s
                    """,
                    (definition.installation_id, definition.asset_id),
                )
                self.assertEqual(
                    (1, old_digest, "legacy-unknown"),
                    cursor.fetchone(),
                )
                cursor.execute("SELECT definition_id FROM t_alarm_events")
                self.assertEqual(legacy_definition_id, cursor.fetchone()[0])
                cursor.execute("SELECT definition_id FROM t_alarm_definition_current")
                self.assertEqual(legacy_definition_id, cursor.fetchone()[0])

    def test_upgrade_does_not_mislabel_original_034_v2_digest_as_v1(self) -> None:
        self._reset_schema_through_032()
        definition_id = str(uuid4())
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                entity_id, _ = self._insert_entities(cursor, installation_id)
                content = {
                    "asset_id": "site.alarm.original-034.high",
                    "version": "rule-set:original-034:1",
                    "entity_instance_id": entity_id,
                    "entity_definition_id": "pcs.activePower",
                    "trigger": {"operator": "gt", "value": 90},
                    "trigger_duration_seconds": 1,
                    "recovery": {"operator": "lt", "value": 80},
                    "recovery_duration_seconds": 1,
                    "severity": "WARNING",
                    "notification_throttle_seconds": 1,
                }
                original_034_v2_digest = hashlib.sha256(
                    json.dumps(
                        content,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                cursor.execute(
                    """
                    INSERT INTO t_alarm_definitions
                      (id, asset_id, definition_version, installation_id,
                       site_configuration_version, entity_instance_id,
                       entity_definition_id, trigger_condition,
                       trigger_duration_seconds, recovery_condition,
                       recovery_duration_seconds, severity,
                       notification_throttle_seconds, content_digest)
                    VALUES (%s, %s, %s, %s, 1, %s, %s, %s, 1, %s, 1,
                            'WARNING', 1, %s)
                    """,
                    (
                        definition_id,
                        content["asset_id"],
                        content["version"],
                        installation_id,
                        entity_id,
                        content["entity_definition_id"],
                        json.dumps(content["trigger"]),
                        json.dumps(content["recovery"]),
                        original_034_v2_digest,
                    ),
                )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION_034.read_text(encoding="utf-8"))
                cursor.execute(
                    """
                    SELECT content_digest, content_digest_algorithm
                    FROM t_alarm_definitions WHERE id = %s
                    """,
                    (definition_id,),
                )
                self.assertEqual(
                    (original_034_v2_digest, "legacy-unknown"),
                    cursor.fetchone(),
                )

    def test_legacy_migration_targets_are_fk_backed_nonempty_evidence(self) -> None:
        from app.services.alarm_definitions import (
            AlarmDefinitionPlan,
            InstalledAlarmDefinition,
        )
        from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog
        from psycopg2.extras import register_uuid

        self._reset_schema_through_032()
        register_uuid()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                entity_id, _ = self._insert_entities(cursor, installation_id)
                self._apply_alarm_migrations(cursor)
        definition = InstalledAlarmDefinition(
            id=uuid4(),
            asset_id="site.alarm.legacy.target",
            version="legacy-migration:1",
            installation_id=UUID(installation_id),
            site_configuration_version=1,
            entity_instance_id=UUID(entity_id),
            entity_definition_id="pcs.activePower",
            trigger={"operator": "gt", "value": 90},
            trigger_duration_seconds=0,
            recovery={"operator": "lt", "value": 80},
            recovery_duration_seconds=0,
            severity="WARNING",
            notification_throttle_seconds=0,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            PostgresAlarmDefinitionCatalog().install_definitions(
                AlarmDefinitionPlan(
                    installation_id=definition.installation_id,
                    site_configuration_version=1,
                    package_digest="a" * 64,
                    definitions=(definition,),
                    digest="f" * 64,
                ),
                transaction=connection,
            )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    cursor.execute(
                        """
                        INSERT INTO t_alarm_definition_origins
                          (definition_id, origin_type, plan_id, details, actor)
                        VALUES (%s, 'package', %s, '{}', 'user:engineer')
                        """,
                        (definition.id, uuid4()),
                    )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_alarm_definition_origins
                      (definition_id, origin_type, source_kind, source_key,
                       details, actor)
                    VALUES (%s, 'legacy_migration', 'legacy_alarm', 'alarm-1',
                            '{}', 'user:engineer')
                    """,
                    (definition.id,),
                )
        migration_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_legacy_alarm_migrations
                      (id, source_kind, source_key, state, actor)
                    VALUES (%s, 'legacy_alarm', 'alarm-1', 'migrated',
                            'user:engineer')
                    """,
                    (migration_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO t_legacy_alarm_migration_targets
                      (migration_id, definition_id, source_kind, source_key,
                       origin_type)
                    VALUES (%s, %s, 'legacy_alarm', 'alarm-1',
                            'legacy_migration')
                    """,
                    (migration_id, definition.id),
                )
        with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
            with psycopg2.connect(**self.connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    mismatched_migration_id = uuid4()
                    cursor.execute(
                        """
                        INSERT INTO t_legacy_alarm_migrations
                          (id, source_kind, source_key, state, actor)
                        VALUES (%s, 'legacy_alarm', 'different-source',
                                'migrated', 'user:engineer')
                        """,
                        (mismatched_migration_id,),
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_legacy_alarm_migration_targets
                          (migration_id, definition_id, source_kind,
                           source_key, origin_type)
                        VALUES (%s, %s, 'legacy_alarm', 'different-source',
                                'legacy_migration')
                        """,
                        (mismatched_migration_id, definition.id),
                    )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
                    cursor.execute(
                        """
                        INSERT INTO t_legacy_alarm_migration_targets
                          (migration_id, definition_id, source_kind,
                           source_key, origin_type)
                        VALUES (%s, %s, 'legacy_alarm', 'alarm-1',
                                'legacy_migration')
                        """,
                        (migration_id, uuid4()),
                    )
        with self.assertRaises(psycopg2.errors.RaiseException):
            with psycopg2.connect(**self.connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO t_legacy_alarm_migrations
                          (id, source_kind, source_key, state, actor)
                        VALUES (%s, 'legacy_alarm', 'missing-target',
                                'migrated', 'user:engineer')
                        """,
                        (uuid4(),),
                    )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 't_legacy_alarm_migrations'
                      AND column_name = 'target_definition_ids'
                    """
                )
                self.assertIsNone(cursor.fetchone())

    def test_legacy_configuration_tables_and_tag_alarm_columns_are_database_read_only(self) -> None:
        self._reset_schema_through_032()
        level_id = uuid4()
        entity_id = uuid4()
        binding_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                self._insert_entities(cursor, installation_id)
                cursor.execute(
                    "INSERT INTO t_alarm_levels (id, code, name, severity) VALUES (%s, 'legacy', 'Legacy', 'MAJOR')",
                    (str(level_id),),
                )
                cursor.execute(
                    "INSERT INTO t_entities (id, name, enabled) VALUES (%s, 'pcs.legacy', TRUE)",
                    (str(entity_id),),
                )
                cursor.execute(
                    "INSERT INTO t_entity_alarm_bindings (id, entity_id, alarm_level_id) VALUES (%s, %s, %s)",
                    (str(binding_id), str(entity_id), str(level_id)),
                )
                cursor.execute("SELECT id FROM t_nodes ORDER BY id LIMIT 1")
                node_id = cursor.fetchone()[0]
                fault_map_id = uuid4()
                cursor.execute(
                    "INSERT INTO t_fault_maps (id, name) VALUES (%s, 'delete-gate-map')",
                    (str(fault_map_id),),
                )
                legacy_delete_ids = [uuid4() for _index in range(4)]
                cursor.execute(
                    "INSERT INTO t_tags (id, node_id, name, data_type, alarm_level) VALUES (%s, %s, 'delete-level', 'FLOAT', 'error1')",
                    (str(legacy_delete_ids[0]), node_id),
                )
                cursor.execute(
                    "INSERT INTO t_tags (id, node_id, name, data_type, alarm_type) VALUES (%s, %s, 'delete-type', 'FLOAT', 'fault')",
                    (str(legacy_delete_ids[1]), node_id),
                )
                cursor.execute(
                    "INSERT INTO t_tags (id, node_id, name, data_type, alarm_threshold) VALUES (%s, %s, 'delete-threshold', 'FLOAT', 10)",
                    (str(legacy_delete_ids[2]), node_id),
                )
                cursor.execute(
                    "INSERT INTO t_tags (id, node_id, name, data_type, fault_map_id) VALUES (%s, %s, 'delete-map', 'FLOAT', %s)",
                    (str(legacy_delete_ids[3]), node_id, str(fault_map_id)),
                )
                self._apply_alarm_migrations(cursor)

        rejected_statements = (
            ("INSERT INTO t_alarm_levels (code, name, severity) VALUES ('new', 'New', 'INFO')", None),
            ("UPDATE t_alarm_levels SET name = 'changed' WHERE id = %s", (str(level_id),)),
            ("DELETE FROM t_alarm_levels WHERE id = %s", (str(level_id),)),
            ("TRUNCATE t_alarm_levels CASCADE", None),
            ("INSERT INTO t_entity_alarm_bindings (entity_id, alarm_level_id) VALUES (%s, %s)", (str(entity_id), str(level_id))),
            ("UPDATE t_entity_alarm_bindings SET enabled = FALSE WHERE id = %s", (str(binding_id),)),
            ("DELETE FROM t_entity_alarm_bindings WHERE id = %s", (str(binding_id),)),
            ("TRUNCATE t_entity_alarm_bindings", None),
            ("INSERT INTO t_tags (id, node_id, name, data_type, alarm_level) SELECT gen_random_uuid(), id, 'blocked', 'FLOAT', 'error1' FROM t_nodes LIMIT 1", None),
            ("UPDATE t_tags SET alarm_threshold = alarm_threshold WHERE id = (SELECT id FROM t_tags LIMIT 1)", None),
            ("TRUNCATE t_tags CASCADE", None),
            *((
                "DELETE FROM t_tags WHERE id = %s",
                (str(tag_id),),
            ) for tag_id in legacy_delete_ids),
        )
        for statement, parameters in rejected_statements:
            with self.subTest(statement=statement):
                connection = psycopg2.connect(**self.connection_kwargs)
                try:
                    with connection.cursor() as cursor:
                        try:
                            cursor.execute(statement, parameters)
                        except psycopg2.errors.RaiseException:
                            connection.rollback()
                        else:
                            connection.rollback()
                            self.fail("legacy alarm mutation was not rejected")
                finally:
                    connection.close()

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE t_tags SET display_name = 'ordinary update' WHERE id = (SELECT id FROM t_tags LIMIT 1) RETURNING display_name"
                )
                self.assertEqual("ordinary update", cursor.fetchone()[0])

    def test_ready_tag_migration_persists_definition_origin_and_fk_target_once(self) -> None:
        from app.services.alarm_configuration import AlarmConfiguration
        from app.services.alarm_configuration_postgres import (
            PostgresAlarmConfigurationRepository,
        )

        self._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                self._insert_entities(cursor, installation_id)
                cursor.execute(
                    "UPDATE t_tags SET alarm_level = 'error1' WHERE id = (SELECT tag_id FROM t_entity_instance_bindings ORDER BY tag_id LIMIT 1) RETURNING id"
                )
                legacy_tag_id = cursor.fetchone()[0]
                self._apply_alarm_migrations(cursor)

        repository = PostgresAlarmConfigurationRepository(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )
        service = AlarmConfiguration(repository)
        preview = service.preview_legacy_migration()
        candidate = next(
            item for item in preview
            if item.source_kind == "tag_alarm" and item.source_key == str(legacy_tag_id)
        )
        self.assertEqual("ready", candidate.status)
        self.assertEqual("CRITICAL", candidate.severity)

        first = service.apply_legacy_migration(
            installation_id=UUID(installation_id),
            selections={},
            actor="user:engineer",
        )
        replay = service.apply_legacy_migration(
            installation_id=UUID(installation_id),
            selections={},
            actor="user:engineer",
        )
        self.assertEqual(first.target_definition_ids, replay.target_definition_ids)
        self.assertEqual("migrated", replay.status)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT alarm_level FROM t_tags WHERE id = %s", (legacy_tag_id,))
                self.assertEqual("error1", cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_legacy_alarm_migrations")
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_legacy_alarm_migration_targets")
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT origin_type, actor, details FROM t_alarm_definition_origins"
                )
                origin_type, actor, origin_details = cursor.fetchone()
                self.assertEqual(("legacy_migration", "user:engineer"), (origin_type, actor))
                self.assertEqual(
                    str(candidate.entity_instance_id),
                    origin_details["entity_instance_id"],
                )
                self.assertIsNotNone(origin_details["confirmation_id"])
                cursor.execute("SELECT details FROM t_legacy_alarm_migrations")
                migration_details = cursor.fetchone()[0]
                self.assertEqual(
                    str(candidate.entity_instance_id),
                    migration_details["selected_entity_instance_id"],
                )
                self.assertEqual(
                    [str(value) for value in candidate.entity_instance_candidates],
                    migration_details["entity_instance_candidates"],
                )
                self.assertEqual(
                    "unique_confirmed_binding",
                    migration_details["selection_reason"],
                )

    def test_apply_recompiles_after_source_lock_and_rejects_new_ambiguity(self) -> None:
        from app.services.alarm_configuration import AlarmConfigurationError
        from app.services.alarm_configuration_postgres import (
            PostgresAlarmConfigurationRepository,
        )

        self._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                self._insert_entities(cursor, installation_id)
                cursor.execute(
                    "SELECT tag_id FROM t_entity_instance_bindings ORDER BY tag_id"
                )
                tag_ids = [row[0] for row in cursor.fetchall()]
                level_id = uuid4()
                legacy_entity_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO t_alarm_levels
                      (id, code, name, severity, trigger_rules, enabled)
                    VALUES (%s, 'revalidate', 'Revalidate', 'MAJOR',
                            '[{"op":"active"}]', TRUE)
                    """,
                    (str(level_id),),
                )
                cursor.execute(
                    "INSERT INTO t_entities (id, name, enabled) VALUES (%s, 'pcs.revalidate', TRUE)",
                    (str(legacy_entity_id),),
                )
                cursor.execute("SELECT node_id FROM t_tags WHERE id = %s", (tag_ids[0],))
                node_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO t_entity_bindings (id, entity_id, tag_id, node_id, enabled) VALUES (%s, %s, %s, %s, TRUE)",
                    (str(uuid4()), str(legacy_entity_id), tag_ids[0], node_id),
                )
                cursor.execute(
                    "INSERT INTO t_entity_alarm_bindings (id, entity_id, alarm_level_id, enabled) VALUES (%s, %s, %s, TRUE)",
                    (str(uuid4()), str(legacy_entity_id), str(level_id)),
                )
                self._apply_alarm_migrations(cursor)

        repository = PostgresAlarmConfigurationRepository(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )
        plan = self._build_legacy_plan(repository, installation_id)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT node_id FROM t_tags WHERE id = %s", (tag_ids[1],))
                second_node_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO t_entity_bindings (id, entity_id, tag_id, node_id, enabled) VALUES (%s, %s, %s, %s, TRUE)",
                    (str(uuid4()), str(legacy_entity_id), tag_ids[1], second_node_id),
                )
                applying_repository = PostgresAlarmConfigurationRepository(
                    connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
                )
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        applying_repository.apply_legacy_alarm_migration,
                        plan,
                        actor="user:engineer",
                    )
                    for _ in range(20):
                        if future.done():
                            break
                        Event().wait(0.05)
                    self.assertFalse(
                        future.done(),
                        "apply must wait for an in-flight candidate write",
                    )
                    connection.commit()
                    with self.assertRaisesRegex(
                        AlarmConfigurationError, "ALARM_MIGRATION_AMBIGUOUS"
                    ):
                        future.result(timeout=10)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_definitions")
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_legacy_alarm_migrations")
                self.assertEqual(0, cursor.fetchone()[0])

    def test_repository_rejects_forged_specs_and_omitted_sources_without_writes(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfigurationError,
            legacy_migration_plan_digest,
        )
        from app.services.alarm_configuration_postgres import (
            PostgresAlarmConfigurationRepository,
        )

        for tamper in ("severity", "trigger", "omit_source"):
            with self.subTest(tamper=tamper):
                self._reset_schema_through_032()
                with psycopg2.connect(**self.connection_kwargs) as connection:
                    connection.autocommit = True
                    with connection.cursor() as cursor:
                        installation_id, _ = self._insert_installed_site(cursor)
                        self._insert_entities(cursor, installation_id)
                        cursor.execute(
                            "UPDATE t_tags SET alarm_level = 'error1' WHERE id IN (SELECT tag_id FROM t_entity_instance_bindings)"
                        )
                        self._apply_alarm_migrations(cursor)

                repository = PostgresAlarmConfigurationRepository(
                    connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
                )
                plan = self._build_legacy_plan(repository, installation_id)
                if tamper == "omit_source":
                    forged_items = plan.items[:-1]
                else:
                    first = plan.items[0]
                    definition = first.definitions[0]
                    if tamper == "severity":
                        forged_definition = replace(definition, severity="INFO")
                        forged_first = replace(
                            first,
                            severity="INFO",
                            definitions=(forged_definition,),
                        )
                    else:
                        forged_definition = replace(
                            definition, trigger={"op": "gt", "value": 999}
                        )
                        forged_first = replace(
                            first, definitions=(forged_definition,)
                        )
                    forged_items = (forged_first, *plan.items[1:])
                forged = replace(
                    plan,
                    items=tuple(forged_items),
                    digest=legacy_migration_plan_digest(
                        plan.installation_id,
                        tuple(forged_items),
                        "user:engineer",
                    ),
                )
                applying_repository = PostgresAlarmConfigurationRepository(
                    connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
                )
                with self.assertRaisesRegex(
                    AlarmConfigurationError, "ALARM_MIGRATION_PLAN_STALE"
                ):
                    applying_repository.apply_legacy_alarm_migration(
                        forged, actor="user:engineer"
                    )
                with psycopg2.connect(**self.connection_kwargs) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT count(*) FROM t_alarm_definitions")
                        self.assertEqual(0, cursor.fetchone()[0])
                        cursor.execute("SELECT count(*) FROM t_legacy_alarm_migrations")
                        self.assertEqual(0, cursor.fetchone()[0])

    def test_postgres_preview_classifies_unresolved_ambiguous_custom_and_missing_map(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmConfigurationError,
        )
        from app.services.alarm_configuration_postgres import (
            PostgresAlarmConfigurationRepository,
        )

        self._reset_schema_through_032()
        missing_map_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                self._insert_entities(cursor, installation_id)
                cursor.execute(
                    "SELECT tag_id FROM t_entity_instance_bindings ORDER BY tag_id"
                )
                tag_ids = [row[0] for row in cursor.fetchall()]
                cursor.execute(
                    "UPDATE t_tags SET alarm_level = 'error1' WHERE id = %s",
                    (tag_ids[0],),
                )
                cursor.execute("SELECT node_id FROM t_tags WHERE id = %s", (tag_ids[0],))
                node_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT device_instance_id
                    FROM t_entity_instances
                    WHERE id = (
                        SELECT entity_instance_id
                        FROM t_entity_instance_bindings
                        WHERE tag_id = %s
                    )
                    """,
                    (tag_ids[0],),
                )
                device_id = cursor.fetchone()[0]
                mismatched_tag_id = uuid4()
                mismatched_entity_id = uuid4()
                mismatched_binding_id = uuid4()
                mismatched_confirmation_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, alarm_level, enabled)
                    VALUES (%s, %s, 'mismatched-confirmation', 'FLOAT',
                            'error2', TRUE)
                    """,
                    (str(mismatched_tag_id), node_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_instances
                      (id, device_instance_id, definition_id, display_name,
                       data_type, direction, freshness_seconds)
                    VALUES (%s, %s, 'pcs.mismatchedConfirmation',
                            'Mismatched confirmation', 'FLOAT', 'R', 30)
                    """,
                    (str(mismatched_entity_id), device_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_binding_confirmations
                      (id, entity_instance_id, binding_id, actor, matcher_id,
                       reason, plan_digest, selected_tag_id)
                    VALUES (%s, %s, %s, 'user:installer', 'invalid-test',
                            'confirmation names a different tag', %s, %s)
                    """,
                    (
                        str(mismatched_confirmation_id),
                        str(mismatched_entity_id),
                        str(mismatched_binding_id),
                        "e" * 64,
                        tag_ids[0],
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_instance_bindings
                      (id, entity_instance_id, tag_id, matcher_id,
                       confirmation_audit_id, active)
                    VALUES (%s, %s, %s, 'invalid-test', %s, TRUE)
                    """,
                    (
                        str(mismatched_binding_id),
                        str(mismatched_entity_id),
                        str(mismatched_tag_id),
                        str(mismatched_confirmation_id),
                    ),
                )
                unresolved_tag_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, alarm_level, enabled)
                    VALUES (%s, %s, 'unresolved-alarm', 'FLOAT', 'error2', TRUE)
                    """,
                    (str(unresolved_tag_id), node_id),
                )

                custom_level_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO t_alarm_levels
                      (id, code, name, severity, trigger_rules, enabled)
                    VALUES (%s, 'custom-info', 'Custom info', 'INFO',
                            '[{"op":"active"}]', TRUE)
                    """,
                    (str(custom_level_id),),
                )
                ambiguous_entity_id = uuid4()
                cursor.execute(
                    "INSERT INTO t_entities (id, name, enabled) VALUES (%s, 'pcs.ambiguous', TRUE)",
                    (str(ambiguous_entity_id),),
                )
                for tag_id in tag_ids:
                    cursor.execute("SELECT node_id FROM t_tags WHERE id = %s", (tag_id,))
                    old_node_id = cursor.fetchone()[0]
                    cursor.execute(
                        "INSERT INTO t_entity_bindings (id, entity_id, tag_id, node_id, enabled) VALUES (%s, %s, %s, %s, TRUE)",
                        (str(uuid4()), str(ambiguous_entity_id), tag_id, old_node_id),
                    )
                ambiguous_binding_id = uuid4()
                cursor.execute(
                    "INSERT INTO t_entity_alarm_bindings (id, entity_id, alarm_level_id, enabled) VALUES (%s, %s, %s, TRUE)",
                    (str(ambiguous_binding_id), str(ambiguous_entity_id), str(custom_level_id)),
                )

                existing_map_id = uuid4()
                cursor.execute(
                    "INSERT INTO t_fault_maps (id, name) VALUES (%s, 'Existing map')",
                    (str(existing_map_id),),
                )
                valid_fault_tag_id = tag_ids[1]
                cursor.execute(
                    """
                    UPDATE t_tags
                    SET alarm_level = 'error3', alarm_type = 'fault', fault_map_id = %s
                    WHERE id = %s
                    """,
                    (str(existing_map_id), valid_fault_tag_id),
                )
                missing_level_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO t_alarm_levels
                      (id, code, name, severity, trigger_rules, enabled)
                    VALUES (%s, 'missing-map', 'Missing map', 'MAJOR', %s, TRUE)
                    """,
                    (
                        str(missing_level_id),
                        json.dumps([
                            {"op": "fault", "fault_map_id": str(existing_map_id)},
                            {"op": "fault", "fault_map_id": str(missing_map_id)},
                        ]),
                    ),
                )
                missing_entity_id = uuid4()
                cursor.execute(
                    "INSERT INTO t_entities (id, name, enabled) VALUES (%s, 'pcs.missing-map', TRUE)",
                    (str(missing_entity_id),),
                )
                cursor.execute("SELECT node_id FROM t_tags WHERE id = %s", (tag_ids[0],))
                old_node_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO t_entity_bindings (id, entity_id, tag_id, node_id, enabled) VALUES (%s, %s, %s, %s, TRUE)",
                    (str(uuid4()), str(missing_entity_id), tag_ids[0], old_node_id),
                )
                cursor.execute("SELECT node_id FROM t_tags WHERE id = %s", (tag_ids[1],))
                second_old_node_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO t_entity_bindings (id, entity_id, tag_id, node_id, enabled) VALUES (%s, %s, %s, %s, TRUE)",
                    (str(uuid4()), str(missing_entity_id), tag_ids[1], second_old_node_id),
                )
                missing_binding_id = uuid4()
                cursor.execute(
                    "INSERT INTO t_entity_alarm_bindings (id, entity_id, alarm_level_id, enabled) VALUES (%s, %s, %s, TRUE)",
                    (str(missing_binding_id), str(missing_entity_id), str(missing_level_id)),
                )
                self._apply_alarm_migrations(cursor)

        service = AlarmConfiguration(
            PostgresAlarmConfigurationRepository(
                connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
            )
        )
        items = {
            (item.source_kind, item.source_key): item
            for item in service.preview_legacy_migration()
        }
        ready = next(
            item for item in items.values()
            if item.source_kind == "tag_alarm" and item.status == "ready"
        )
        self.assertEqual("CRITICAL", ready.severity)
        self.assertEqual(
            "ALARM_LEGACY_RULE_UNSUPPORTED",
            items[("tag_alarm", str(valid_fault_tag_id))].blockers[0]["code"],
        )
        self.assertEqual(
            "ALARM_ENTITY_UNRESOLVED",
            items[("tag_alarm", str(mismatched_tag_id))].blockers[0]["code"],
        )
        self.assertEqual(
            "ALARM_ENTITY_UNRESOLVED",
            items[("tag_alarm", str(unresolved_tag_id))].blockers[0]["code"],
        )
        ambiguous = items[("entity_alarm_binding", str(ambiguous_binding_id))]
        self.assertEqual("INFO", ambiguous.severity)
        self.assertEqual("ALARM_MIGRATION_AMBIGUOUS", ambiguous.blockers[0]["code"])
        self.assertEqual(2, len(ambiguous.entity_instance_candidates))
        self.assertEqual(
            "ALARM_FAULT_MAP_UNRESOLVED",
            items[("entity_alarm_binding", str(missing_binding_id))].blockers[0]["code"],
        )
        with self.assertRaisesRegex(
            AlarmConfigurationError,
            "ALARM_FAULT_MAP_UNRESOLVED",
        ):
            service.apply_legacy_migration(
                installation_id=UUID(installation_id),
                selections={},
                actor="user:engineer",
            )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_definitions")
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_legacy_alarm_migrations")
                self.assertEqual(0, cursor.fetchone()[0])


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the isolated Postgres alarm configuration seam",
)
class PostgresAlarmConfigurationRepositoryTest(
    _PostgresAlarmConfigurationTestBase,
    unittest.TestCase,
):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()

    def setUp(self) -> None:
        self._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self.installation_id, self.package_record_id = self._insert_installed_site(cursor)
                self.entity_ids = self._insert_entities(cursor, self.installation_id)
                self._apply_alarm_migrations(cursor)

    @staticmethod
    def _rules():
        from app.services.alarm_configuration import AlarmRule

        return (
            AlarmRule(
                id="high",
                name="High active power",
                severity="WARNING",
                trigger={"operator": "gt", "value": 90},
                trigger_duration_seconds=0,
                recovery={"operator": "lt", "value": 80},
                recovery_duration_seconds=0,
                notification_throttle_seconds=0,
                unit="kW",
            ),
            AlarmRule(
                id="critical",
                name="Critical active power",
                severity="CRITICAL",
                trigger={"operator": "gt", "value": 110},
                trigger_duration_seconds=0,
                recovery={"operator": "lt", "value": 100},
                recovery_duration_seconds=0,
                notification_throttle_seconds=0,
                unit="kW",
            ),
        )

    def _repository(self):
        from app.services.alarm_configuration_postgres import (
            PostgresAlarmConfigurationRepository,
        )

        return PostgresAlarmConfigurationRepository(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )

    def test_definition_catalog_requires_the_outer_atomic_transaction(self) -> None:
        from app.services.alarm_definitions import AlarmDefinitionPlan
        from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog

        plan = AlarmDefinitionPlan(
            installation_id=uuid4(),
            site_configuration_version=2,
            package_digest="a" * 64,
            definitions=(),
            digest="b" * 64,
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "alarm definition installation requires an outer transaction",
        ):
            PostgresAlarmDefinitionCatalog().install_definitions(plan)

    def test_public_apply_seam_rejects_blank_actor_before_database_writes(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmConfigurationError,
            EntitySelection,
            PlanAlarmConfiguration,
        )

        repository = self._repository()
        service = AlarmConfiguration(repository)
        revision = service.create_rule_set(
            key="blank-actor",
            name="Blank actor",
            rules=self._rules(),
            actor="user:engineer",
        )
        plan = service.plan(
            PlanAlarmConfiguration(
                installation_id=UUID(self.installation_id),
                selection=EntitySelection(
                    entity_instance_ids=tuple(
                        UUID(value) for value in self.entity_ids
                    ),
                ),
                rule_set_id=revision.rule_set_id,
                rule_set_revision=revision.revision,
                planned_by="user:planner",
            )
        )

        with self.assertRaisesRegex(
            AlarmConfigurationError,
            "ALARM_APPLY_COMMAND_INVALID",
        ):
            repository.apply_plan(
                plan,
                idempotency_key="blank-actor",
                actor="  ",
            )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_version FROM t_site_configuration_state"
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM t_alarm_configuration_idempotency"
                )
                self.assertEqual(0, cursor.fetchone()[0])

    def test_apply_persists_derived_site_definitions_origins_audit_and_replay(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmConfigurationError,
            ApplyAlarmConfigurationPlan,
            EntitySelection,
            PlanAlarmConfiguration,
        )
        repository = self._repository()
        service = AlarmConfiguration(repository)
        revision = service.create_rule_set(
            key="pcs-active-power",
            name="PCS active power",
            rules=self._rules(),
            actor="user:engineer",
        )
        plan = service.plan(
            PlanAlarmConfiguration(
                installation_id=UUID(self.installation_id),
                selection=EntitySelection(
                    entity_instance_ids=tuple(UUID(value) for value in self.entity_ids),
                ),
                rule_set_id=revision.rule_set_id,
                rule_set_revision=revision.revision,
                planned_by="user:planner",
            )
        )
        result = service.apply(
            ApplyAlarmConfigurationPlan(
                plan_id=plan.id,
                plan_digest=plan.digest,
                idempotency_key="apply-pcs-rules-v1",
                actor="user:engineer",
            )
        )

        restarted_repository = self._repository()
        restarted_service = AlarmConfiguration(restarted_repository)
        replay = restarted_service.apply(
            ApplyAlarmConfigurationPlan(
                plan_id=plan.id,
                plan_digest=plan.digest,
                idempotency_key="apply-pcs-rules-v1",
                actor="user:engineer",
            )
        )
        self.assertEqual(result, replay)
        persisted_plan = restarted_repository.get_plan(plan.id)
        self.assertEqual("applied", persisted_plan.status)
        self.assertEqual(result, persisted_plan.applied_result)
        self.assertEqual("user:planner", persisted_plan.planned_by)
        self.assertEqual(2, result.site_configuration_version)
        self.assertEqual(4, len(result.definition_ids))

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                for table_name, expected in (
                    ("t_solution_installations", 2),
                    ("t_site_configuration_versions", 3),
                    ("t_alarm_definitions", 4),
                    ("t_alarm_definition_current", 4),
                    ("t_alarm_definition_origins", 4),
                    ("t_solution_delivery_audit", 1),
                    ("t_alarm_configuration_idempotency", 1),
                ):
                    cursor.execute(f"SELECT count(*) FROM {table_name}")
                    self.assertEqual(expected, cursor.fetchone()[0], table_name)
                cursor.execute(
                    "SELECT count(*) FROM t_audit_events WHERE event = 'alarm.configuration.apply'"
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT event, outcome, actor, target,
                           details->>'site_configuration_version'
                    FROM t_audit_events
                    WHERE event = 'solution.install'
                    """
                )
                self.assertEqual(
                    (
                        "solution.install",
                        "allowed",
                        "user:engineer",
                        f"installation:{result.installation_id}",
                        "2",
                    ),
                    cursor.fetchone(),
                )
                cursor.execute(
                    """
                    SELECT planned_by, applied_by,
                           canonical_plan->>'status',
                           canonical_plan->>'planned_by'
                    FROM t_alarm_configuration_plans WHERE id = %s
                    """,
                    (plan.id,),
                )
                self.assertEqual(
                    ("user:planner", "user:engineer", "ready", "user:planner"),
                    cursor.fetchone(),
                )
                cursor.execute(
                    """
                    SELECT next.package_record_id = previous.package_record_id,
                           next.package_digest = previous.package_digest,
                           next.parameters = previous.parameters,
                           next.secret_references = previous.secret_references,
                           next.parameter_metadata = previous.parameter_metadata,
                           next.configuration_digest = previous.configuration_digest,
                           next.entity_identity_installation_id = previous.entity_identity_installation_id
                    FROM t_site_configuration_versions previous
                    JOIN t_site_configuration_versions next ON next.previous_version = previous.version
                    WHERE previous.version = 1 AND next.version = 2
                    """
                )
                self.assertEqual((True,) * 7, cursor.fetchone())

    def test_origin_constraint_failure_rolls_back_every_apply_write(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmConfigurationError,
            ApplyAlarmConfigurationPlan,
            EntitySelection,
            PlanAlarmConfiguration,
        )

        service = AlarmConfiguration(self._repository())
        revision = service.create_rule_set(
            key="rollback-test",
            name="Rollback test",
            rules=self._rules(),
            actor="user:engineer",
        )
        plan = service.plan(
            PlanAlarmConfiguration(
                installation_id=UUID(self.installation_id),
                selection=EntitySelection(
                    entity_instance_ids=tuple(UUID(value) for value in self.entity_ids),
                ),
                rule_set_id=revision.rule_set_id,
                rule_set_revision=revision.revision,
                planned_by="user:planner",
            )
        )
        counted_tables = (
            "t_solution_install_plans",
            "t_solution_installations",
            "t_site_configuration_versions",
            "t_alarm_definitions",
            "t_alarm_definition_current",
            "t_alarm_definition_origins",
            "t_solution_delivery_audit",
            "t_audit_events",
            "t_alarm_configuration_idempotency",
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    ALTER TABLE t_alarm_definition_origins
                    ADD CONSTRAINT test_reject_rollback_actor
                    CHECK (actor <> 'user:rollback-test')
                    """
                )
                before: dict[str, int] = {}
                for table_name in counted_tables:
                    cursor.execute(f"SELECT count(*) FROM {table_name}")
                    before[table_name] = cursor.fetchone()[0]

        with self.assertRaisesRegex(
            AlarmConfigurationError,
            "ALARM_CONFIGURATION_PERSISTENCE_FAILED",
        ):
            service.apply(
                ApplyAlarmConfigurationPlan(
                    plan_id=plan.id,
                    plan_digest=plan.digest,
                    idempotency_key="rollback-apply",
                    actor="user:rollback-test",
                )
            )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                for table_name, expected in before.items():
                    cursor.execute(f"SELECT count(*) FROM {table_name}")
                    self.assertEqual(expected, cursor.fetchone()[0], table_name)
                cursor.execute(
                    "SELECT current_version FROM t_site_configuration_state WHERE singleton = TRUE"
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT status FROM t_alarm_configuration_plans WHERE id = %s",
                    (plan.id,),
                )
                self.assertEqual("ready", cursor.fetchone()[0])

    def test_deferred_commit_failure_rolls_back_before_connection_reuse(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmConfigurationError,
            ApplyAlarmConfigurationPlan,
            EntitySelection,
            PlanAlarmConfiguration,
        )
        from psycopg2.extensions import TRANSACTION_STATUS_IDLE

        setup_service = AlarmConfiguration(self._repository())
        revision = setup_service.create_rule_set(
            key="deferred-commit",
            name="Deferred commit",
            rules=self._rules(),
            actor="user:engineer",
        )
        plan = setup_service.plan(
            PlanAlarmConfiguration(
                installation_id=UUID(self.installation_id),
                selection=EntitySelection(
                    entity_instance_ids=tuple(UUID(value) for value in self.entity_ids),
                ),
                rule_set_id=revision.rule_set_id,
                rule_set_revision=revision.revision,
                planned_by="user:planner",
            )
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE OR REPLACE FUNCTION fail_alarm_configuration_commit()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        IF NEW.actor = 'user:commit-fail' THEN
                            RAISE EXCEPTION 'injected deferred commit failure';
                        END IF;
                        RETURN NEW;
                    END;
                    $$;
                    CREATE CONSTRAINT TRIGGER test_alarm_configuration_commit_failure
                    AFTER INSERT ON t_alarm_configuration_idempotency
                    DEFERRABLE INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION fail_alarm_configuration_commit();
                    """
                )

        shared_connection = psycopg2.connect(**self.connection_kwargs)

        class ReusableConnection:
            def __getattr__(self, name):
                return getattr(shared_connection, name)

            def commit(self) -> None:
                try:
                    shared_connection.commit()
                except psycopg2.errors.RaiseException as deferred_error:
                    try:
                        with shared_connection.cursor() as cursor:
                            cursor.execute("SELECT 1 / 0")
                    except psycopg2.errors.DivisionByZero:
                        pass
                    raise deferred_error

            def close(self) -> None:
                pass

        repository = self._repository()
        repository._connection_factory = lambda: ReusableConnection()
        service = AlarmConfiguration(repository)
        try:
            with self.assertRaisesRegex(
                AlarmConfigurationError,
                "ALARM_CONFIGURATION_PERSISTENCE_UNAVAILABLE",
            ):
                service.apply(
                    ApplyAlarmConfigurationPlan(
                        plan.id,
                        plan.digest,
                        "deferred-failure",
                        "user:commit-fail",
                    )
                )
            self.assertEqual(
                TRANSACTION_STATUS_IDLE,
                shared_connection.get_transaction_status(),
            )
            result = service.apply(
                ApplyAlarmConfigurationPlan(
                    plan.id,
                    plan.digest,
                    "after-deferred-failure",
                    "user:engineer",
                )
            )
            self.assertEqual(2, result.site_configuration_version)
        finally:
            shared_connection.close()

    def test_concurrent_ready_plans_serialize_one_success_and_one_stale(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmConfigurationError,
            EntitySelection,
            PlanAlarmConfiguration,
        )

        service = AlarmConfiguration(self._repository())
        plans = []
        for suffix in ("a", "b"):
            revision = service.create_rule_set(
                key=f"concurrent-{suffix}",
                name=f"Concurrent {suffix.upper()}",
                rules=self._rules(),
                actor="user:engineer",
            )
            plans.append(
                service.plan(
                    PlanAlarmConfiguration(
                        installation_id=UUID(self.installation_id),
                        selection=EntitySelection(
                            entity_instance_ids=tuple(
                                UUID(value) for value in self.entity_ids
                            ),
                        ),
                        rule_set_id=revision.rule_set_id,
                        rule_set_revision=revision.revision,
                        planned_by="user:planner",
                    )
                )
            )
        self.assertNotEqual(plans[0].digest, plans[1].digest)
        start = Barrier(2)

        def apply(index: int):
            repository = self._repository()
            start.wait(timeout=3)
            try:
                return repository.apply_plan(
                    plans[index],
                    idempotency_key=f"concurrent-{index}",
                    actor="user:engineer",
                )
            except AlarmConfigurationError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(apply, (0, 1)))
        successes = [value for value in outcomes if not isinstance(value, str)]
        failures = [value for value in outcomes if isinstance(value, str)]
        self.assertEqual(1, len(successes))
        self.assertEqual(["ALARM_PLAN_STALE"], failures)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT version, previous_version FROM t_site_configuration_versions ORDER BY version"
                )
                self.assertEqual([(0, None), (1, 0), (2, 1)], cursor.fetchall())
                for table_name, expected in (
                    ("t_solution_installations", 2),
                    ("t_alarm_definitions", 4),
                    ("t_alarm_definition_current", 4),
                    ("t_alarm_definition_origins", 4),
                    ("t_alarm_configuration_idempotency", 1),
                ):
                    cursor.execute(f"SELECT count(*) FROM {table_name}")
                    self.assertEqual(expected, cursor.fetchone()[0], table_name)
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM t_alarm_definitions definition
                    LEFT JOIN t_alarm_definition_origins origin
                      ON origin.definition_id = definition.id
                    WHERE origin.definition_id IS NULL
                    """
                )
                self.assertEqual(0, cursor.fetchone()[0])

    def test_concurrent_same_key_rule_set_creates_share_one_locked_revision_stream(self) -> None:
        from app.services.alarm_configuration import AlarmConfiguration

        start = Barrier(2)

        def create_revision(_index: int):
            service = AlarmConfiguration(self._repository())
            start.wait(timeout=3)
            return service.create_rule_set(
                key="concurrent-rule-set",
                name="Concurrent rule set",
                rules=self._rules(),
                actor="user:engineer",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            revisions = list(executor.map(create_revision, (0, 1)))
        self.assertEqual(1, len({revision.rule_set_id for revision in revisions}))
        self.assertEqual({1, 2}, {revision.revision for revision in revisions})
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM t_alarm_rule_sets WHERE rule_set_key = %s",
                    ("concurrent-rule-set",),
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT count(*) FROM t_alarm_rule_set_revisions
                    WHERE rule_set_id = %s
                    """,
                    (revisions[0].rule_set_id,),
                )
                self.assertEqual(2, cursor.fetchone()[0])

    def test_revision_reads_its_immutable_key_and_name_snapshot(self) -> None:
        from app.services.alarm_configuration import AlarmConfiguration

        repository = self._repository()
        revision = AlarmConfiguration(repository).create_rule_set(
            key="snapshot-key",
            name="Snapshot name",
            rules=self._rules(),
            actor="user:engineer",
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE t_alarm_rule_sets
                    SET rule_set_key = 'renamed-parent', name = 'Renamed parent'
                    WHERE id = %s
                    """,
                    (revision.rule_set_id,),
                )
        persisted = repository.get_rule_set_revision(
            revision.rule_set_id,
            revision.revision,
        )
        self.assertEqual("snapshot-key", persisted.key)
        self.assertEqual("Snapshot name", persisted.name)

    def test_same_idempotency_key_is_actor_scoped_and_restart_readable(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            ApplyAlarmConfigurationPlan,
            EntitySelection,
            PlanAlarmConfiguration,
        )

        service = AlarmConfiguration(self._repository())
        first_revision = service.create_rule_set(
            key="actor-scoped-idempotency",
            name="Actor scoped idempotency",
            rules=self._rules(),
            actor="user:engineer",
        )
        selection = EntitySelection(
            entity_instance_ids=tuple(UUID(value) for value in self.entity_ids)
        )
        first_plan = service.plan(
            PlanAlarmConfiguration(
                UUID(self.installation_id),
                selection,
                first_revision.rule_set_id,
                first_revision.revision,
                "user:planner-a",
            )
        )
        first = service.apply(
            ApplyAlarmConfigurationPlan(
                first_plan.id,
                first_plan.digest,
                "shared-restart-key",
                "user:applier-a",
            )
        )
        second_revision = service.create_rule_set_revision(
            rule_set_id=first_revision.rule_set_id,
            rules=(self._rules()[0],),
            actor="user:engineer",
        )
        second_plan = service.plan(
            PlanAlarmConfiguration(
                first.installation_id,
                selection,
                second_revision.rule_set_id,
                second_revision.revision,
                "user:planner-b",
            )
        )
        second = service.apply(
            ApplyAlarmConfigurationPlan(
                second_plan.id,
                second_plan.digest,
                "shared-restart-key",
                "user:applier-b",
            )
        )

        restarted = self._repository()
        self.assertEqual(
            first,
            restarted.find_idempotency(
                "user:applier-a",
                "shared-restart-key",
            )[3],
        )
        self.assertEqual(
            second,
            restarted.find_idempotency(
                "user:applier-b",
                "shared-restart-key",
            )[3],
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT actor FROM t_alarm_configuration_idempotency
                    WHERE idempotency_key = 'shared-restart-key'
                    ORDER BY actor
                    """
                )
                self.assertEqual(
                    [("user:applier-a",), ("user:applier-b",)],
                    cursor.fetchall(),
                )

    def test_delete_candidates_remain_preview_only_when_a_revision_is_applied(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            ApplyAlarmConfigurationPlan,
            EntitySelection,
            PlanAlarmConfiguration,
        )

        service = AlarmConfiguration(self._repository())
        first_revision = service.create_rule_set(
            key="delete-preview",
            name="Delete preview",
            rules=self._rules(),
            actor="user:engineer",
        )
        selection = EntitySelection(
            entity_instance_ids=tuple(UUID(value) for value in self.entity_ids)
        )
        first_plan = service.plan(
            PlanAlarmConfiguration(
                installation_id=UUID(self.installation_id),
                selection=selection,
                rule_set_id=first_revision.rule_set_id,
                rule_set_revision=first_revision.revision,
                planned_by="user:planner",
            )
        )
        first_result = service.apply(
            ApplyAlarmConfigurationPlan(
                plan_id=first_plan.id,
                plan_digest=first_plan.digest,
                idempotency_key="delete-preview-v1",
                actor="user:engineer",
            )
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT current.asset_id, current.definition_id
                    FROM t_alarm_definition_current current
                    WHERE current.asset_id LIKE '%%.critical'
                    ORDER BY current.asset_id
                    """
                )
                critical_pointers = cursor.fetchall()

        second_revision = service.create_rule_set_revision(
            rule_set_id=first_revision.rule_set_id,
            rules=(self._rules()[0],),
            actor="user:engineer",
        )
        second_plan = service.plan(
            PlanAlarmConfiguration(
                installation_id=first_result.installation_id,
                selection=selection,
                rule_set_id=second_revision.rule_set_id,
                rule_set_revision=second_revision.revision,
                planned_by="user:planner",
            )
        )
        self.assertEqual(
            {"preserve": 2, "delete_candidate": 2},
            {
                action: sum(item.action == action for item in second_plan.items)
                for action in ("preserve", "delete_candidate")
            },
        )
        service.apply(
            ApplyAlarmConfigurationPlan(
                plan_id=second_plan.id,
                plan_digest=second_plan.digest,
                idempotency_key="delete-preview-v2",
                actor="user:engineer",
            )
        )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_definitions")
                self.assertEqual(6, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarm_definition_current")
                self.assertEqual(4, cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT current.asset_id, current.definition_id
                    FROM t_alarm_definition_current current
                    WHERE current.asset_id LIKE '%%.critical'
                    ORDER BY current.asset_id
                    """
                )
                self.assertEqual(critical_pointers, cursor.fetchall())
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM t_solution_installations installation
                    LEFT JOIN t_site_configuration_versions site
                      ON site.installation_id = installation.id
                    WHERE site.installation_id IS NULL
                    """
                )
                self.assertEqual(0, cursor.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
