from __future__ import annotations

import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from uuid import UUID, uuid4

import psycopg2


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_ROOT = BACKEND_ROOT.parent / "init-db"
MIGRATION_034 = MIGRATIONS_ROOT / "migration_034_unified_alarm_configuration.sql"
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
    def test_migration_applies_to_a_fresh_schema(self) -> None:
        self._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION_034.read_text(encoding="utf-8"))
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
                self._insert_installed_site(cursor)
                migration = MIGRATION_034.read_text(encoding="utf-8")
                cursor.execute(migration)
                cursor.execute(migration)
                for table_name in (
                    "t_alarm_rule_sets",
                    "t_alarm_rule_set_revisions",
                    "t_alarm_configuration_plans",
                    "t_alarm_definition_origins",
                    "t_legacy_alarm_migrations",
                    "t_alarm_configuration_idempotency",
                ):
                    cursor.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
                    self.assertEqual(table_name, cursor.fetchone()[0])


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
                cursor.execute(MIGRATION_034.read_text(encoding="utf-8"))

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

    def test_apply_persists_derived_site_definitions_origins_audit_and_replay(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
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

        restarted_service = AlarmConfiguration(self._repository())
        replay = restarted_service.apply(
            ApplyAlarmConfigurationPlan(
                plan_id=plan.id,
                plan_digest=plan.digest,
                idempotency_key="apply-pcs-rules-v1",
                actor="user:engineer",
            )
        )
        self.assertEqual(result, replay)
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
            )
        )
        counted_tables = (
            "t_solution_install_plans",
            "t_solution_installations",
            "t_site_configuration_versions",
            "t_alarm_definitions",
            "t_alarm_definition_current",
            "t_alarm_definition_origins",
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

        with self.assertRaises(psycopg2.errors.CheckViolation):
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
