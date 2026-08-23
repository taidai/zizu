from __future__ import annotations

import os
import unittest
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json, register_uuid

from tests import test_data_trunk_migration_postgres as migration_test


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL integration tests",
)
class BusinessMetricPostgresTest(unittest.TestCase):
    SITE_ID = UUID("91000000-0000-0000-0000-000000000001")
    SOURCE_NODE_ID = UUID("91000000-0000-0000-0000-000000000002")
    PACKAGE_ID = UUID("91000000-0000-0000-0000-000000000003")
    BASE_PLAN_ID = UUID("91000000-0000-0000-0000-000000000004")
    BASE_INSTALLATION_ID = UUID("91000000-0000-0000-0000-000000000005")
    IDENTITY_INSTALLATION_ID = UUID("91000000-0000-0000-0000-000000000006")
    SOURCE_DEVICE_ID = UUID("91000000-0000-0000-0000-000000000007")
    COUNTER_ID = UUID("91000000-0000-0000-0000-000000000008")
    TEMPLATE_ROW_ID = UUID("91000000-0000-0000-0000-000000000009")
    TEMPLATE_REVISION_ID = UUID("91000000-0000-0000-0000-000000000010")
    SOURCE_TAG_ID = UUID("91000000-0000-0000-0000-000000000011")
    SOURCE_CONFIRMATION_ID = UUID("91000000-0000-0000-0000-000000000012")
    SOURCE_BINDING_ID = UUID("91000000-0000-0000-0000-000000000013")
    SECOND_TEMPLATE_ROW_ID = UUID("91000000-0000-0000-0000-000000000014")
    SECOND_TEMPLATE_REVISION_ID = UUID("91000000-0000-0000-0000-000000000015")

    @classmethod
    def setUpClass(cls) -> None:
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": os.environ["DB_NAME"],
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        if not cls.connection_kwargs["dbname"].endswith("_test"):
            raise RuntimeError("Business metric tests require a *_test database")

    @classmethod
    def tearDownClass(cls) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    def setUp(self) -> None:
        from app.services.telemetry_store import close_db_pool, init_db_pool

        register_uuid()
        close_db_pool()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration_test.DataTrunkMigrationPostgresTest._reset_through_041(cursor)
                migration_test.DataTrunkMigrationPostgresTest._apply_042(cursor)
                migration_test.DataTrunkMigrationPostgresTest._apply_043(cursor)
        init_db_pool(min_conn=1, max_conn=4)
        self.template_raw = {
            "schemaVersion": "zizu.business-metric/v1alpha1",
            "id": "ems.pv-energy-today",
            "revision": 1,
            "displayName": "今日光伏发电量",
            "targetNodeType": "SITE",
            "output": {
                "entityDefinition": "site.pv_energy_today",
                "dataType": "FLOAT",
                "unit": "kWh",
                "temporalSemantics": "windowed",
            },
            "window": {"kind": "aligned_daily"},
            "sources": [
                {
                    "method": "counter_delta",
                    "entityDefinition": "pv.energy_total",
                    "priority": 1,
                },
                {
                    "method": "power_integral",
                    "entityDefinition": "pv.active_power",
                    "priority": 2,
                },
            ],
            "quality": {"goodCoverage": 0.98, "minimumUsableCoverage": 0.80},
            "allowedLateness": "5m",
            "correction": {"automaticHorizon": "7d"},
            "capabilities": {"controlEligible": False},
        }
        self._seed_site_and_template()

    def _seed_site_and_template(self) -> None:
        from app.services.solution_business_metrics import parse_business_metric_asset

        template = parse_business_metric_asset(self.template_raw)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_solution_packages
                      (id, package_id, version, display_name, digest, status,
                       acceptance_ids, manifest)
                    VALUES (%s, 'ems.reference', '1.0.0', 'EMS Reference', %s,
                            'validated', '[]', '{}')
                    """,
                    (self.PACKAGE_ID, "a" * 64),
                )
                cursor.execute(
                    """
                    INSERT INTO t_solution_install_plans
                      (id, package_record_id, package_digest,
                       base_site_configuration_version, status, items, blockers,
                       digest, target_installation_id,
                       entity_identity_installation_id)
                    VALUES (%s, %s, %s, 0, 'ready', '[]', '[]', %s, %s, %s)
                    """,
                    (
                        self.BASE_PLAN_ID,
                        self.PACKAGE_ID,
                        "a" * 64,
                        "b" * 64,
                        self.BASE_INSTALLATION_ID,
                        self.IDENTITY_INSTALLATION_ID,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_solution_installations
                      (id, plan_id, package_record_id, package_digest,
                       site_configuration_version, status)
                    VALUES (%s, %s, %s, %s, 1, 'installed')
                    """,
                    (
                        self.BASE_INSTALLATION_ID,
                        self.BASE_PLAN_ID,
                        self.PACKAGE_ID,
                        "a" * 64,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_site_configuration_versions
                      (version, previous_version, installation_id,
                       package_record_id, package_digest, actor, parameters,
                       configuration_digest, entity_identity_installation_id)
                    VALUES (1, 0, %s, %s, %s, 'user:seed', %s, %s, %s)
                    """,
                    (
                        self.BASE_INSTALLATION_ID,
                        self.PACKAGE_ID,
                        "a" * 64,
                        Json(
                            {
                                "timezone": "Asia/Shanghai",
                                "raw_detail_retention_days": 30,
                            }
                        ),
                        "c" * 64,
                        self.IDENTITY_INSTALLATION_ID,
                    ),
                )
                cursor.execute(
                    "UPDATE t_site_configuration_state SET current_version = 1 WHERE singleton = TRUE"
                )
                cursor.execute(
                    """
                    INSERT INTO t_nodes (id, name, node_type, parent_id)
                    VALUES (%s, 'Site', 'SITE', NULL),
                           (%s, 'PV-01', 'INVERTER', %s)
                    """,
                    (self.SITE_ID, self.SOURCE_NODE_ID, self.SITE_ID),
                )
                cursor.execute(
                    """
                    INSERT INTO t_device_instances
                      (id, identity_installation_id, slot_id, instance_key,
                       device_category, display_name, node_id)
                    VALUES (%s, %s, 'pv', 'PV-01', 'INVERTER', 'PV-01', %s)
                    """,
                    (
                        self.SOURCE_DEVICE_ID,
                        self.IDENTITY_INSTALLATION_ID,
                        self.SOURCE_NODE_ID,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, unit, unit_to, read_write,
                       enabled, freshness_seconds)
                    VALUES (%s, %s, 'pv.energy_total.raw', 'FLOAT', 'kWh',
                            'kWh', 'R', TRUE, 5)
                    """,
                    (self.SOURCE_TAG_ID, self.SOURCE_NODE_ID),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_instances
                      (id, device_instance_id, definition_id, display_name,
                       data_type, unit, direction, freshness_seconds, source_kind)
                    VALUES (%s, %s, 'pv.energy_total', 'PV total energy',
                            'FLOAT', 'kWh', 'R', 5, 'legacy_tag')
                    """,
                    (self.COUNTER_ID, self.SOURCE_DEVICE_ID),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_binding_confirmations
                      (id, entity_instance_id, binding_id, actor, matcher_id,
                       reason, plan_digest, selected_tag_id)
                    VALUES (%s, %s, %s, 'user:seed', 'exact', 'seed source',
                            %s, %s)
                    """,
                    (
                        self.SOURCE_CONFIRMATION_ID,
                        self.COUNTER_ID,
                        self.SOURCE_BINDING_ID,
                        "d" * 64,
                        self.SOURCE_TAG_ID,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_instance_bindings
                      (id, entity_instance_id, tag_id, matcher_id,
                       confirmation_audit_id, active)
                    VALUES (%s, %s, %s, 'exact', %s, TRUE)
                    """,
                    (
                        self.SOURCE_BINDING_ID,
                        self.COUNTER_ID,
                        self.SOURCE_TAG_ID,
                        self.SOURCE_CONFIRMATION_ID,
                    ),
                )
                cursor.execute(
                    "INSERT INTO t_business_metric_templates (id, template_key) VALUES (%s, %s)",
                    (self.TEMPLATE_ROW_ID, template.template_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_revisions
                      (id, template_id, revision, content, content_digest,
                       package_record_id, published_at)
                    VALUES (%s, %s, 1, %s, %s, %s, now())
                    """,
                    (
                        self.TEMPLATE_REVISION_ID,
                        self.TEMPLATE_ROW_ID,
                        Json(self.template_raw),
                        template.content_digest,
                        self.PACKAGE_ID,
                    ),
                )

    def _delivery(self):
        from app.services.business_metrics import BusinessMetricDelivery
        from app.services.business_metrics_postgres import (
            PostgresBusinessMetricCatalog,
            PostgresBusinessMetricRepository,
        )

        return BusinessMetricDelivery(
            PostgresBusinessMetricCatalog(), PostgresBusinessMetricRepository()
        )

    def _preview(self):
        from app.services.business_metrics import PreviewMetricInstallation

        return self._delivery().preview(
            PreviewMetricInstallation(
                node_id=self.SITE_ID,
                template_id="ems.pv-energy-today",
                actor="user:engineer",
            )
        )

    def _seed_second_metric_template(self) -> None:
        from app.services.solution_business_metrics import parse_business_metric_asset

        second_raw = {
            **self.template_raw,
            "id": "ems.pv-energy-yesterday",
            "displayName": "昨日光伏发电量",
            "output": {
                **self.template_raw["output"],
                "entityDefinition": "site.pv_energy_yesterday",
            },
        }
        template = parse_business_metric_asset(second_raw)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO t_business_metric_templates (id, template_key) VALUES (%s, %s)",
                    (self.SECOND_TEMPLATE_ROW_ID, template.template_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_revisions
                      (id, template_id, revision, content, content_digest,
                       package_record_id, published_at)
                    VALUES (%s, %s, 1, %s, %s, %s, now())
                    """,
                    (
                        self.SECOND_TEMPLATE_REVISION_ID,
                        self.SECOND_TEMPLATE_ROW_ID,
                        Json(second_raw),
                        template.content_digest,
                        self.PACKAGE_ID,
                    ),
                )

    def _seed_template_revision_two(self) -> None:
        from app.services.solution_business_metrics import parse_business_metric_asset

        revision_raw = {
            **self.template_raw,
            "revision": 2,
            "displayName": "今日光伏发电量 v2",
        }
        template = parse_business_metric_asset(revision_raw)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_revisions
                      (id, template_id, revision, content, content_digest,
                       package_record_id, published_at)
                    VALUES (%s, %s, 2, %s, %s, %s, now())
                    """,
                    (
                        uuid4(),
                        self.TEMPLATE_ROW_ID,
                        Json(revision_raw),
                        template.content_digest,
                        self.PACKAGE_ID,
                    ),
                )

    def _preview_template(self, template_id: str):
        from app.services.business_metrics import PreviewMetricInstallation

        return self._delivery().preview(
            PreviewMetricInstallation(
                node_id=self.SITE_ID,
                template_id=template_id,
                actor="user:engineer",
            )
        )

    @staticmethod
    def _apply_command(plan, *, key: str = "metric-install"):
        from app.services.business_metrics import ApplyMetricInstallation

        return ApplyMetricInstallation(
            plan_id=plan.id,
            expected_digest=plan.digest,
            actor="user:engineer",
            idempotency_key=key,
        )

    def _counts(self) -> tuple[int, ...]:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_installed_business_metrics),
                      (SELECT count(*) FROM t_installed_point_processings),
                      (SELECT count(*) FROM t_point_processing_applications),
                      (SELECT count(*) FROM t_business_metric_source_bindings),
                      (SELECT count(*) FROM t_entity_capability_contracts),
                      (SELECT count(*) FROM t_business_metric_audit)
                    """
                )
                return tuple(int(item) for item in cursor.fetchone())

    def test_preview_persists_counter_plan_and_frozen_contract_evidence(self) -> None:
        plan = self._preview()

        self.assertEqual(plan.sources[0].entity_instance_id, self.COUNTER_ID)
        self.assertEqual(plan.sources[0].method.value, "counter_delta")
        self.assertFalse(plan.sources[0].estimated)
        self.assertEqual(plan.timezone, "Asia/Shanghai")
        self.assertEqual(plan.raw_detail_retention_days, 30)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, frozen_timezone, raw_detail_retention_days,
                           digest
                    FROM t_business_metric_installation_plans WHERE id = %s
                    """,
                    (plan.id,),
                )
                self.assertEqual(
                    cursor.fetchone(), ("ready", "Asia/Shanghai", 30, plan.digest)
                )
                cursor.execute(
                    """
                    SELECT item_kind, method, estimated, source_entity_instance_id
                    FROM t_business_metric_plan_items
                    WHERE plan_id = %s ORDER BY ordinal
                    """,
                    (plan.id,),
                )
                items = cursor.fetchall()
                self.assertEqual(
                    items[0], ("source", "counter_delta", False, self.COUNTER_ID)
                )
                self.assertEqual(
                    tuple(item[0] for item in items),
                    ("source", "output", "capability"),
                )
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))

    def test_apply_reads_persisted_plan_and_installs_complete_graph_idempotently(self) -> None:
        from app.services.business_metrics import BusinessMetricDelivery
        from app.services.business_metrics_postgres import (
            PostgresBusinessMetricCatalog,
            PostgresBusinessMetricRepository,
        )

        plan = self._preview()
        restarted = BusinessMetricDelivery(
            PostgresBusinessMetricCatalog(), PostgresBusinessMetricRepository()
        )
        first = restarted.apply(self._apply_command(plan))
        repeated = restarted.apply(self._apply_command(plan))

        self.assertEqual(repeated, first)
        self.assertEqual(first.entity_instance_id, plan.output_entity_instance_id)
        self.assertEqual(first.timezone, "Asia/Shanghai")
        self.assertEqual(self._counts(), (1, 1, 1, 1, 1, 1))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT entity.definition_id, entity.source_kind,
                           processing.revision_id, capability.temporal_semantics,
                           capability.control_eligible, binding.method,
                           binding.data_type, binding.unit, binding.direction,
                           binding.estimated,
                           installed.raw_detail_retention_days, audit.action
                    FROM t_installed_business_metrics AS installed
                    JOIN t_entity_instances AS entity
                      ON entity.id = installed.entity_instance_id
                    JOIN t_installed_point_processings AS processing
                      ON processing.id = installed.installed_processing_id
                    JOIN t_entity_capability_contracts AS capability
                      ON capability.installed_metric_id = installed.id
                    JOIN t_business_metric_source_bindings AS binding
                      ON binding.installed_metric_id = installed.id
                    JOIN t_business_metric_audit AS audit
                      ON audit.installed_metric_id = installed.id
                    """
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (
                        "site.pv_energy_today",
                        "point_processing",
                        first.processing_revision_id,
                        "windowed",
                        False,
                        "counter_delta",
                        "FLOAT",
                        "kWh",
                        "R",
                        False,
                        30,
                        "installed",
                    ),
                )

    def test_two_metrics_on_same_site_keep_independent_current_processing_sources(self) -> None:
        first_plan = self._preview()
        self._delivery().apply(self._apply_command(first_plan, key="first-metric"))
        self._seed_second_metric_template()

        second_plan = self._preview_template("ems.pv-energy-yesterday")
        self._delivery().apply(self._apply_command(second_plan, key="second-metric"))

        self.assertEqual(self._counts(), (2, 2, 2, 2, 2, 2))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*), count(*) FILTER (WHERE current),
                           count(DISTINCT processing_scope)
                    FROM t_installed_point_processings
                    WHERE node_id = %s
                    """,
                    (self.SITE_ID,),
                )
                self.assertEqual(cursor.fetchone(), (2, 2, 1))
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM t_installed_business_metrics AS metric
                    JOIN t_point_processing_output_bindings AS binding
                      ON binding.installed_processing_id = metric.installed_processing_id
                     AND binding.entity_instance_id = metric.entity_instance_id
                    JOIN t_installed_point_processings AS processing
                      ON processing.id = binding.installed_processing_id
                    WHERE metric.state = 'active' AND processing.current = TRUE
                    """
                )
                self.assertEqual(cursor.fetchone(), (2,))

    def test_revision_upgrade_reuses_entity_and_replaces_only_its_metric_processing(self) -> None:
        first_plan = self._preview()
        first = self._delivery().apply(
            self._apply_command(first_plan, key="metric-revision-1")
        )
        self._seed_second_metric_template()
        other_plan = self._preview_template("ems.pv-energy-yesterday")
        other = self._delivery().apply(
            self._apply_command(other_plan, key="other-metric")
        )
        self._seed_template_revision_two()

        upgrade_plan = self._preview()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT item_kind, action, before_value IS NOT NULL,
                           after_value IS NOT NULL
                    FROM t_business_metric_plan_items
                    WHERE plan_id = %s
                    ORDER BY ordinal
                    """,
                    (upgrade_plan.id,),
                )
                self.assertEqual(
                    cursor.fetchall(),
                    [
                        ("source", "preserve", True, True),
                        ("output", "reuse", True, True),
                        ("capability", "update", True, True),
                    ],
                )
        upgraded = self._delivery().apply(
            self._apply_command(upgrade_plan, key="metric-revision-2")
        )

        self.assertEqual(upgraded.entity_instance_id, first.entity_instance_id)
        self.assertNotEqual(
            upgraded.processing_revision_id, first.processing_revision_id
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*) FILTER (WHERE current),
                           count(*) FILTER (
                             WHERE current AND processing_owner_key = %s
                           ),
                           count(*) FILTER (
                             WHERE current AND processing_owner_key = %s
                           )
                    FROM t_installed_point_processings
                    WHERE processing_scope = 'business_metric'
                    """,
                    (first.entity_instance_id, other.entity_instance_id),
                )
                self.assertEqual(cursor.fetchone(), (2, 1, 1))

    def test_same_plan_with_new_key_reuses_installation_and_records_idempotency(self) -> None:
        plan = self._preview()
        first = self._delivery().apply(
            self._apply_command(plan, key="first-request-key")
        )

        repeated = self._delivery().apply(
            self._apply_command(plan, key="second-request-key")
        )

        self.assertEqual(repeated, first)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT idempotency_key, request_digest, action
                    FROM t_business_metric_audit
                    WHERE installed_metric_id = %s
                    ORDER BY created_at, id
                    """,
                    (first.id,),
                )
                rows = cursor.fetchall()
                self.assertEqual(
                    tuple(row[0] for row in rows),
                    ("first-request-key", "second-request-key"),
                )
                self.assertTrue(all(row[1] is not None for row in rows))
                self.assertEqual(tuple(row[2] for row in rows), ("installed", "reused"))

    def test_same_key_checks_plan_existence_before_returning_old_installation(self) -> None:
        from app.services.business_metrics import (
            ApplyMetricInstallation,
            BusinessMetricError,
        )

        plan = self._preview()
        self._delivery().apply(self._apply_command(plan, key="bound-request-key"))

        with self.assertRaises(BusinessMetricError) as raised:
            self._delivery().apply(
                ApplyMetricInstallation(
                    plan_id=uuid4(),
                    expected_digest=plan.digest,
                    actor="user:engineer",
                    idempotency_key="bound-request-key",
                )
            )
        self.assertEqual(raised.exception.code, "BUSINESS_METRIC_PLAN_MISSING")

    def test_same_key_with_a_different_persisted_plan_is_a_conflict(self) -> None:
        from app.services.business_metrics import BusinessMetricError

        first_plan = self._preview()
        self._seed_second_metric_template()
        other_plan = self._preview_template("ems.pv-energy-yesterday")
        self._delivery().apply(
            self._apply_command(first_plan, key="persisted-plan-bound-key")
        )

        with self.assertRaises(BusinessMetricError) as raised:
            self._delivery().apply(
                self._apply_command(other_plan, key="persisted-plan-bound-key")
            )

        self.assertEqual(
            raised.exception.code, "BUSINESS_METRIC_IDEMPOTENCY_CONFLICT"
        )
        self.assertEqual(self._counts(), (1, 1, 1, 1, 1, 1))

    def test_apply_rejects_stale_sources_without_runtime_partial_write(self) -> None:
        from app.services.business_metrics import BusinessMetricError

        plan = self._preview()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                second_device_id = uuid4()
                second_entity_id = uuid4()
                second_tag_id = uuid4()
                second_confirmation_id = uuid4()
                second_binding_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO t_device_instances
                      (id, identity_installation_id, slot_id, instance_key,
                       device_category, display_name, node_id)
                    VALUES (%s, %s, 'pv', 'PV-02', 'INVERTER', 'PV-02', %s)
                    """,
                    (
                        second_device_id,
                        self.IDENTITY_INSTALLATION_ID,
                        self.SOURCE_NODE_ID,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, unit, unit_to, read_write,
                       enabled, freshness_seconds)
                    VALUES (%s, %s, 'pv.energy.total.2', 'FLOAT', 'kWh',
                            'kWh', 'R', TRUE, 5)
                    """,
                    (second_tag_id, self.SOURCE_NODE_ID),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_instances
                      (id, device_instance_id, definition_id, display_name,
                       data_type, unit, direction, freshness_seconds, source_kind)
                    VALUES (%s, %s, 'pv.energy_total', 'PV total energy 2',
                            'FLOAT', 'kWh', 'R', 5, 'legacy_tag')
                    """,
                    (second_entity_id, second_device_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_binding_confirmations
                      (id, entity_instance_id, binding_id, actor, matcher_id,
                       reason, plan_digest, selected_tag_id)
                    VALUES (%s, %s, %s, 'user:seed', 'exact', 'second source',
                            %s, %s)
                    """,
                    (
                        second_confirmation_id,
                        second_entity_id,
                        second_binding_id,
                        "e" * 64,
                        second_tag_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_instance_bindings
                      (id, entity_instance_id, tag_id, matcher_id,
                       confirmation_audit_id, active)
                    VALUES (%s, %s, %s, 'exact', %s, TRUE)
                    """,
                    (
                        second_binding_id,
                        second_entity_id,
                        second_tag_id,
                        second_confirmation_id,
                    ),
                )

        with self.assertRaisesRegex(BusinessMetricError, "BUSINESS_METRIC_PLAN_STALE"):
            self._delivery().apply(self._apply_command(plan))

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))
        blocked = self._preview()
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(
            blocked.blockers, ({"code": "BUSINESS_METRIC_SOURCE_AMBIGUOUS"},)
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT plan.status, item.item_kind, item.blocker_code
                    FROM t_business_metric_installation_plans AS plan
                    JOIN t_business_metric_plan_items AS item
                      ON item.plan_id = plan.id
                    WHERE plan.id = %s
                    """,
                    (blocked.id,),
                )
                self.assertEqual(
                    cursor.fetchone(),
                    ("blocked", "blocker", "BUSINESS_METRIC_SOURCE_AMBIGUOUS"),
                )

    def test_preview_and_apply_reject_incompatible_authoritative_source_contract(self) -> None:
        from app.services.business_metrics import BusinessMetricError

        plan = self._preview()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE t_entity_instances
                    SET data_type = 'BOOL', unit = NULL, direction = 'W'
                    WHERE id = %s
                    """,
                    (self.COUNTER_ID,),
                )

        blocked = self._preview()
        self.assertEqual(
            blocked.blockers,
            ({"code": "BUSINESS_METRIC_SOURCE_INCOMPATIBLE"},),
        )
        with self.assertRaises(BusinessMetricError) as raised:
            self._delivery().apply(self._apply_command(plan))
        self.assertEqual(
            raised.exception.code, "BUSINESS_METRIC_SOURCE_INCOMPATIBLE"
        )
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))

    def test_apply_rechecks_invalid_iana_timezone_with_stable_error_code(self) -> None:
        from app.services.business_metrics import BusinessMetricError

        plan = self._preview()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE t_site_configuration_versions DISABLE TRIGGER USER"
                )
                cursor.execute(
                    """
                    UPDATE t_site_configuration_versions
                    SET parameters = jsonb_set(
                      parameters, '{timezone}', '"Mars/Olympus"'
                    )
                    WHERE version = 1
                    """
                )
                cursor.execute(
                    "ALTER TABLE t_site_configuration_versions ENABLE TRIGGER USER"
                )

        with self.assertRaises(BusinessMetricError) as raised:
            self._delivery().apply(self._apply_command(plan))
        self.assertEqual(raised.exception.code, "BUSINESS_METRIC_TIMEZONE_INVALID")
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))

    def test_window_result_lifecycle_and_l2_reference_checks_are_enforced(self) -> None:
        plan = self._preview()
        installed = self._delivery().apply(self._apply_command(plan))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_projections
                      (installed_metric_id, window_started_at, window_ended_at,
                       coverage, quality, estimated, state)
                    VALUES (%s, now() - interval '1 hour', now(), 0.5, 64,
                            FALSE, '{"sampleCount": 1}')
                    """,
                    (installed.id,),
                )
                cursor.execute(
                    """
                    UPDATE t_business_metric_projections
                    SET coverage = 0.75, state = '{"sampleCount": 2}'
                    WHERE installed_metric_id = %s
                    RETURNING coverage, state ->> 'sampleCount'
                    """,
                    (installed.id,),
                )
                self.assertEqual(cursor.fetchone(), (0.75, "2"))
        statement = """
            INSERT INTO t_business_metric_window_results
              (installed_metric_id, window_started_at, window_ended_at,
               revision, lifecycle, calculation_method, quality, coverage,
               estimated, source_count, result_event_id, result_observed_at,
               result_entity_instance_id, content_digest, source_summary)
            VALUES (%s, now() - interval '1 day', now(), 1, %s,
                    'counter_delta', 192, 1, FALSE, 0, %s, %s, %s, %s, '{}')
        """
        cases = (
            (
                psycopg2.errors.CheckViolation,
                (installed.id, "completed", None, None, None, "f" * 64),
            ),
            (
                psycopg2.errors.CheckViolation,
                (installed.id, "invalid", uuid4(), None, None, "f" * 64),
            ),
            (
                psycopg2.errors.ForeignKeyViolation,
                (
                    installed.id,
                    "completed",
                    uuid4(),
                    "2026-01-01T00:00:00Z",
                    installed.entity_instance_id,
                    "f" * 64,
                ),
            ),
        )
        for expected, parameters in cases:
            with psycopg2.connect(**self.connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    with self.assertRaises(expected):
                        cursor.execute(statement, parameters)

    def test_projection_allows_recovery_updates_but_guards_its_identity(self) -> None:
        installed = self._delivery().apply(self._apply_command(self._preview()))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_projections
                      (installed_metric_id, window_started_at, window_ended_at,
                       watermark_at, coverage, quality, estimated, state)
                    VALUES (%s, now() - interval '1 hour', now(), NULL,
                            0.5, 64, TRUE, '{"sampleCount": 1}')
                    """,
                    (installed.id,),
                )
                cursor.execute(
                    """
                    UPDATE t_business_metric_projections
                    SET watermark_at = now(), coverage = 1, quality = 192,
                        estimated = FALSE, state = '{"sampleCount": 2}',
                        updated_at = now()
                    WHERE installed_metric_id = %s
                    """,
                    (installed.id,),
                )
                connection.commit()
                with self.assertRaises(
                    psycopg2.errors.ObjectNotInPrerequisiteState
                ):
                    cursor.execute(
                        """
                        UPDATE t_business_metric_projections
                        SET installed_metric_id = %s
                        WHERE installed_metric_id = %s
                        """,
                        (uuid4(), installed.id),
                    )
                connection.rollback()
                with self.assertRaises(
                    psycopg2.errors.ObjectNotInPrerequisiteState
                ):
                    cursor.execute(
                        "DELETE FROM t_business_metric_projections "
                        "WHERE installed_metric_id = %s",
                        (installed.id,),
                    )
                connection.rollback()
                with self.assertRaises(
                    psycopg2.errors.ObjectNotInPrerequisiteState
                ):
                    cursor.execute("TRUNCATE t_business_metric_projections")
                connection.rollback()

    def test_metric_lifecycle_and_recomputation_progress_by_appending_events(self) -> None:
        installed = self._delivery().apply(self._apply_command(self._preview()))
        request_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                with self.assertRaises(
                    psycopg2.errors.ObjectNotInPrerequisiteState
                ):
                    cursor.execute(
                        "UPDATE t_installed_business_metrics SET state = 'disabled' "
                        "WHERE id = %s",
                        (installed.id,),
                    )
                connection.rollback()
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_audit
                      (id, installed_metric_id, action, actor, resulting_state,
                       evidence, digest)
                    VALUES (%s, %s, 'disabled', 'user:operator', 'disabled',
                            '{}', %s)
                    """,
                    (uuid4(), installed.id, "e" * 64),
                )
                for revision, status in ((1, "requested"), (2, "approved")):
                    cursor.execute(
                        """
                        INSERT INTO t_business_metric_recomputations
                          (id, request_id, revision, installed_metric_id,
                           requested_by, approved_by, range_started_at,
                           range_ended_at, reason, status, evidence)
                        VALUES (%s, %s, %s, %s, 'user:operator', %s,
                                now() - interval '1 day', now(), 'repair gap',
                                %s, '{}')
                        """,
                        (
                            uuid4(),
                            request_id,
                            revision,
                            installed.id,
                            "user:approver" if revision == 2 else None,
                            status,
                        ),
                    )
                cursor.execute(
                    """
                    SELECT revision, status
                    FROM t_business_metric_recomputations
                    WHERE request_id = %s ORDER BY revision
                    """,
                    (request_id,),
                )
                self.assertEqual(
                    cursor.fetchall(), [(1, "requested"), (2, "approved")]
                )
                connection.commit()
                with self.assertRaises(
                    psycopg2.errors.ObjectNotInPrerequisiteState
                ):
                    cursor.execute(
                        "UPDATE t_business_metric_recomputations "
                        "SET status = 'running' WHERE request_id = %s",
                        (request_id,),
                    )
                connection.rollback()
                with self.assertRaises(
                    psycopg2.errors.ObjectNotInPrerequisiteState
                ):
                    cursor.execute(
                        "DELETE FROM t_business_metric_recomputations "
                        "WHERE request_id = %s",
                        (request_id,),
                    )
                connection.rollback()

    def test_window_results_reject_provisional_and_incomplete_source_evidence(self) -> None:
        installed = self._delivery().apply(self._apply_command(self._preview()))
        statement = """
            INSERT INTO t_business_metric_window_results
              (installed_metric_id, window_started_at, window_ended_at,
               revision, lifecycle, calculation_method, quality, coverage,
               estimated, source_count, content_digest, source_summary)
            VALUES (%s, now() - interval '1 day', now(), 1, %s,
                    %s, 64, 0.5, TRUE, %s, %s, '{}')
        """
        cases = (
            ("provisional", "counter_delta", 0),
            ("invalid", "counter_delta", 1),
            ("invalid", "free_form_formula", 0),
        )
        for lifecycle, method, source_count in cases:
            with psycopg2.connect(**self.connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    with self.assertRaises(psycopg2.errors.CheckViolation):
                        cursor.execute(
                            statement,
                            (
                                installed.id,
                                lifecycle,
                                method,
                                source_count,
                                "f" * 64,
                            ),
                        )

    def test_acceptance_requires_runtime_and_exact_window_result_binding(self) -> None:
        installed = self._delivery().apply(self._apply_command(self._preview()))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                with self.assertRaises(psycopg2.errors.NotNullViolation):
                    cursor.execute(
                        """
                        INSERT INTO t_business_metric_acceptance_reports
                          (id, installed_metric_id, schema_version, status,
                           report, digest)
                        VALUES (%s, %s, '043', 'failed', '{}', %s)
                        """,
                        (uuid4(), installed.id, "f" * 64),
                    )

        self._seed_second_metric_template()
        second_plan = self._preview_template("ems.pv-energy-yesterday")
        second = self._delivery().apply(
            self._apply_command(second_plan, key="acceptance-second-metric")
        )
        runtime_id = uuid4()
        result_event_id = uuid4()
        observed_at = "2026-08-22T16:00:00Z"
        window_started_at = "2026-08-21T16:00:00Z"
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_runtime_instances
                      (id, started_at, platform_version)
                    VALUES (%s, now(), 'test-runtime')
                    """,
                    (runtime_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO t_l2_observations
                      (observed_at, event_id, entity_instance_id, received_at,
                       calculated_at, value_float, quality,
                       processing_revision_id, site_configuration_version,
                       source_digest, source_order_key,
                       producing_runtime_instance_id)
                    VALUES (%s, %s, %s, %s, %s, 12.5, 192, %s, 1, %s,
                            'metric-result', %s)
                    """,
                    (
                        observed_at,
                        result_event_id,
                        installed.entity_instance_id,
                        observed_at,
                        observed_at,
                        installed.processing_revision_id,
                        "a" * 64,
                        runtime_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_window_results
                      (installed_metric_id, window_started_at, window_ended_at,
                       revision, lifecycle, calculation_method, quality,
                       coverage, estimated, source_count,
                       first_source_event_id, first_source_observed_at,
                       last_source_event_id, last_source_observed_at,
                       result_event_id, result_observed_at,
                       result_entity_instance_id, content_digest,
                       source_summary)
                    VALUES (%s, %s, %s, 1, 'completed', 'counter_delta',
                            192, 1, FALSE, 1, %s, %s, %s, %s, %s, %s, %s,
                            %s, '{}')
                    """,
                    (
                        installed.id,
                        window_started_at,
                        observed_at,
                        result_event_id,
                        observed_at,
                        result_event_id,
                        observed_at,
                        result_event_id,
                        observed_at,
                        installed.entity_instance_id,
                        "b" * 64,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_acceptance_reports
                      (id, installed_metric_id,
                       window_result_installed_metric_id,
                       window_result_started_at, window_result_ended_at,
                       window_result_revision, runtime_instance_id,
                       schema_version, status, report, digest)
                    VALUES (%s, %s, %s, %s, %s, 1, %s, '043', 'passed',
                            '{}', %s)
                    """,
                    (
                        uuid4(),
                        installed.id,
                        installed.id,
                        window_started_at,
                        observed_at,
                        runtime_id,
                        "c" * 64,
                    ),
                )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    cursor.execute(
                        """
                        INSERT INTO t_business_metric_acceptance_reports
                          (id, installed_metric_id,
                           window_result_installed_metric_id,
                           window_result_started_at, window_result_ended_at,
                           window_result_revision, runtime_instance_id,
                           schema_version, status, report, digest)
                        VALUES (%s, %s, %s, %s, %s, 1, %s, '042', 'failed',
                                '{}', %s)
                        """,
                        (
                            uuid4(),
                            installed.id,
                            installed.id,
                            window_started_at,
                            observed_at,
                            runtime_id,
                            "e" * 64,
                        ),
                    )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    cursor.execute(
                        """
                        INSERT INTO t_business_metric_acceptance_reports
                          (id, installed_metric_id,
                           window_result_installed_metric_id,
                           window_result_started_at, window_result_ended_at,
                           window_result_revision, runtime_instance_id,
                           schema_version, status, report, digest)
                        VALUES (%s, %s, %s, %s, %s, 1, %s, '043', 'failed',
                                '{}', %s)
                        """,
                        (
                            uuid4(),
                            second.id,
                            installed.id,
                            window_started_at,
                            observed_at,
                            runtime_id,
                            "d" * 64,
                        ),
                    )

    def test_database_failure_after_internal_apply_rolls_back_entire_runtime_graph(self) -> None:
        plan = self._preview()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE FUNCTION fail_metric_source_binding() RETURNS trigger
                    LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected'; END $$
                    """
                )
                cursor.execute(
                    """
                    CREATE TRIGGER trg_fail_metric_source_binding
                    BEFORE INSERT ON t_business_metric_source_bindings
                    FOR EACH ROW EXECUTE FUNCTION fail_metric_source_binding()
                    """
                )

        with self.assertRaises(Exception):
            self._delivery().apply(self._apply_command(plan))

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0, 0))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_version FROM t_site_configuration_state")
                self.assertEqual(cursor.fetchone(), (1,))
                cursor.execute(
                    "SELECT count(*) FROM t_entity_instances WHERE definition_id = 'site.pv_energy_today'"
                )
                self.assertEqual(cursor.fetchone(), (0,))

    def test_internal_business_metric_transform_round_trips_without_formula_surface(self) -> None:
        from app.services.business_metric_contracts import (
            MetricAggregator,
            MetricSourceResolution,
            ResolvedMetricSource,
        )
        from app.services.business_metrics_postgres import persist_internal_business_metric_asset
        from app.services.point_processing_postgres import PostgresPointProcessingCatalog
        from app.services.solution_business_metrics import (
            compile_business_metric,
            parse_business_metric_asset,
        )

        template = parse_business_metric_asset(self.template_raw)
        compiled = compile_business_metric(
            template,
            MetricSourceResolution(
                "Asia/Shanghai",
                (
                    ResolvedMetricSource(
                        self.COUNTER_ID,
                        "pv.energy_total",
                        MetricAggregator.COUNTER_DELTA,
                        "FLOAT",
                        "kWh",
                        False,
                    ),
                ),
            ),
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                persist_internal_business_metric_asset(
                    cursor, compiled.point_processing_asset
                )
                loaded = PostgresPointProcessingCatalog._load_asset(
                    cursor,
                    compiled.processing_revision_id,
                    include_internal=True,
                )

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.outputs[0].transform["kind"], "business_metric")
        self.assertEqual(loaded.outputs[0].transform["qualityGoodCoverage"], 0.98)
        self.assertFalse(loaded.outputs[0].transform["controlEligible"])

    def test_internal_processing_revision_is_hidden_from_public_point_catalog(self) -> None:
        from app.services.point_processing_postgres import PostgresPointProcessingCatalog

        plan = self._preview()
        installed = self._delivery().apply(self._apply_command(plan))
        catalog = PostgresPointProcessingCatalog()

        self.assertIsNone(catalog.get_template(installed.processing_revision_id))
        self.assertNotIn(
            installed.processing_revision_id,
            tuple(item.revision_id for item in catalog.list_templates("SITE")),
        )

    def test_ordinary_point_processing_apply_rejects_internal_metric_revision(self) -> None:
        from app.services.business_metric_contracts import (
            MetricSourceResolution,
        )
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            InMemoryPointProcessingCatalog,
            PointProcessingError,
            PointProcessingSource,
            PreviewPointProcessing,
            compile_point_processing_plan,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
        )
        from app.services.solution_business_metrics import (
            compile_business_metric,
            parse_business_metric_asset,
        )
        from app.services.business_metrics_postgres import _PointPlanningRepository

        metric_plan = self._preview()
        self._delivery().apply(self._apply_command(metric_plan))
        compiled = compile_business_metric(
            parse_business_metric_asset(self.template_raw),
            MetricSourceResolution(metric_plan.timezone, metric_plan.sources),
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state.current_version, site.installation_id,
                           site.entity_identity_installation_id
                    FROM t_site_configuration_state AS state
                    JOIN t_site_configuration_versions AS site
                      ON site.version = state.current_version
                    WHERE state.singleton = TRUE
                    """
                )
                current_version, solution_id, identity_id = cursor.fetchone()
        point_catalog = InMemoryPointProcessingCatalog(
            templates={
                compiled.processing_revision_id: compiled.point_processing_asset
            },
            sources=(
                PointProcessingSource(
                    self.COUNTER_ID,
                    "l2",
                    self.SOURCE_NODE_ID,
                    "pv.energy_total",
                    "FLOAT",
                    "kWh",
                    True,
                ),
            ),
        )
        point_plan = compile_point_processing_plan(
            PreviewPointProcessing(
                node_id=self.SITE_ID,
                template_revision_id=compiled.processing_revision_id,
                input_selections={"source_1": self.COUNTER_ID},
                actor="user:ordinary-point-processing",
                entity_identity_installation_id=identity_id,
                planned_output_entity_ids={"metric_value": uuid4()},
                solution_installation_id=solution_id,
            ),
            point_catalog,
            _PointPlanningRepository(int(current_version)),
        )
        repository = PostgresPointProcessingRepository()
        repository.save_plan(point_plan)

        with self.assertRaises(PointProcessingError) as raised:
            repository.apply_plan(
                ApplyPointProcessingPlan(
                    plan_id=point_plan.id,
                    plan_digest=point_plan.digest,
                    idempotency_key="ordinary-internal-bypass",
                    actor="user:ordinary-point-processing",
                ),
                PostgresPointProcessingCatalog(),
            )
        self.assertEqual(raised.exception.code, "POINT_PROCESSING_INTERNAL_REVISION")

    def test_formula_tick_ignores_private_business_metric_processing(self) -> None:
        from app.services.data_trunk import DataTrunk
        from app.services.data_trunk_postgres import PostgresDataTrunkRepository

        plan = self._preview()
        self._delivery().apply(self._apply_command(plan))

        self.assertEqual(
            DataTrunk(PostgresDataTrunkRepository()).evaluate_due_formulas(),
            (),
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT (SELECT count(*) FROM t_l2_observations),
                           (SELECT count(*) FROM t_point_processing_formula_runs)
                    """
                )
                self.assertEqual(cursor.fetchone(), (0, 0))


if __name__ == "__main__":
    unittest.main()
