from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import UTC, datetime, timedelta
import unittest
from uuid import UUID

import psycopg2

from tests import test_data_trunk_migration_postgres as migration_test


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_reference_delivery.py"
SPEC = importlib.util.spec_from_file_location("build_reference_delivery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL point-processing tests",
)
class PointProcessingPostgresTest(unittest.TestCase):
    def test_site_formula_freezes_members_and_persists_dag_atomically(self) -> None:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingDelivery,
            PreviewPointProcessing,
            _stable_output_entity_id,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
        )

        package, brand_a_revision, _ = self._import_reference_package()
        formula_summary = next(
            item
            for item in PostgresPointProcessingCatalog().list_templates("SITE")
            if item.asset.asset_id == "site.total-pcs-power"
        )
        formula_revision = formula_summary.revision_id
        pcs_1 = UUID("85000000-0000-0000-0000-000000000001")
        pcs_2 = UUID("85000000-0000-0000-0000-000000000002")
        site_id = UUID("85000000-0000-0000-0000-000000000010")
        pcs_1_node = UUID("85000000-0000-0000-0000-000000000011")
        pcs_2_node = UUID("85000000-0000-0000-0000-000000000012")
        entity_ids = {
            "active_power": pcs_1,
            "operating_state": UUID("85000000-0000-0000-0000-000000000101"),
            "fault_codes": UUID("85000000-0000-0000-0000-000000000102"),
        }
        self._seed_brand_a_site(
            package.id,
            package.digest,
            brand_a_revision,
            pcs_1_node,
            entity_ids,
        )
        identity_id = UUID("85000000-0000-0000-0000-000000000203")
        installation_id = UUID("85000000-0000-0000-0000-000000000202")
        site_output = _stable_output_entity_id(
            identity_id,
            site_id,
            "site.total_pcs_power",
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_nodes (id, name, node_type)
                    VALUES (%s, 'SITE-01', 'SITE')
                    """,
                    (site_id,),
                )
                cursor.execute(
                    """
                    UPDATE t_nodes
                    SET parent_id = %s, node_type = 'PCS'
                    WHERE id = %s
                    """,
                    (site_id, pcs_1_node),
                )
                cursor.execute(
                    """
                    INSERT INTO t_nodes (id, name, parent_id, node_type)
                    VALUES (%s, 'PCS-02', %s, 'PCS')
                    """,
                    (pcs_2_node, site_id),
                )
                pcs_2_device = UUID("85000000-0000-0000-0000-000000000302")
                cursor.execute(
                    """
                    INSERT INTO t_device_instances
                      (id, identity_installation_id, slot_id, instance_key,
                       device_category, display_name, node_id)
                    VALUES (%s, %s, 'slot.pcs-2', 'PCS-02', 'PCS', 'PCS-02', %s)
                    """,
                    (pcs_2_device, identity_id, pcs_2_node),
                )
                cursor.execute(
                    """
                    INSERT INTO t_entity_instances
                      (id, device_instance_id, definition_id, display_name,
                       data_type, unit, direction, freshness_seconds, source_kind)
                    VALUES (%s, %s, 'pcs.active_power', 'PCS-02 有功功率',
                            'FLOAT', 'kW', 'R', 30, 'point_processing')
                    """,
                    (pcs_2, pcs_2_device),
                )
                second_installed = UUID("85000000-0000-0000-0000-000000000306")
                cursor.execute(
                    """
                    INSERT INTO t_installed_point_processings
                      (id, node_id, revision_id, source_plan_id,
                       solution_installation_id, site_configuration_version,
                       installed_by, current)
                    VALUES (%s, %s, %s,
                            '85000000-0000-0000-0000-000000000205',
                            %s, 1, 'user:engineer-install', TRUE)
                    """,
                    (second_installed, pcs_2_node, brand_a_revision, installation_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_output_bindings
                      (installed_processing_id, output_id, entity_instance_id)
                    SELECT %s, id, %s
                    FROM t_point_processing_outputs
                    WHERE revision_id = %s AND output_key = 'active_power'
                    """,
                    (second_installed, pcs_2, brand_a_revision),
                )

        service = PointProcessingDelivery(
            PostgresPointProcessingRepository(),
            PostgresPointProcessingCatalog(),
        )
        plan = service.preview(
            PreviewPointProcessing(
                node_id=site_id,
                template_revision_id=formula_revision,
                input_selections={},
                actor="user:engineer-formula",
                entity_identity_installation_id=identity_id,
                solution_installation_id=installation_id,
            )
        )
        self.assertEqual("ready", plan.status, plan.blockers)
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "install-site-formula",
                "user:engineer-formula",
            )
        )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_point_processing_selector_members
                       WHERE installed_processing_id = %s),
                      (SELECT count(*) FROM t_point_processing_dependencies
                       WHERE installed_processing_id = %s)
                    """,
                    (
                        application.installed_processing_id,
                        application.installed_processing_id,
                    ),
                )
                self.assertEqual((2, 2), cursor.fetchone())
                cursor.execute(
                    """
                    SELECT entity.definition_id, device.node_id
                    FROM t_entity_instances AS entity
                    JOIN t_device_instances AS device
                      ON device.id = entity.device_instance_id
                    WHERE entity.id = %s
                    """,
                    (site_output,),
                )
                self.assertEqual(("site.total_pcs_power", site_id), cursor.fetchone())
                cursor.execute(
                    """
                    SELECT entity_instance_id
                    FROM t_point_processing_selector_members
                    WHERE installed_processing_id = %s
                    ORDER BY ordinal
                    """,
                    (application.installed_processing_id,),
                )
                self.assertEqual((pcs_1, pcs_2), tuple(row[0] for row in cursor))

    def test_en9_unified_plan_applies_l0_l1_l2_atomically(self) -> None:
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
        from tests.test_neuron_point_processing_catalog import FakeNeuron

        package, brand_a_revision, _brand_b_revision = self._import_reference_package()
        catalog = PostgresPointProcessingCatalog()
        en9_revision = next(
            item.revision_id for item in catalog.list_templates("PCS")
            if item.asset.asset_id == "pcs.en9"
        )
        node_id = UUID("85000000-0000-0000-0000-000000000001")
        entity_ids = {
            "active_power": UUID("85000000-0000-0000-0000-000000000101"),
            "operating_state": UUID("85000000-0000-0000-0000-000000000102"),
            "fault_codes": UUID("85000000-0000-0000-0000-000000000103"),
        }
        self._seed_brand_a_site(
            package.id,
            package.digest,
            brand_a_revision,
            node_id,
            entity_ids,
        )
        service = PointProcessingDelivery(
            PostgresPointProcessingRepository(),
            catalog,
            point_scanner=NeuronPointCatalog(FakeNeuron()),
        )

        plan = service.preview(
            PreviewPointProcessing(
                node_id=node_id,
                template_revision_id=en9_revision,
                input_selections={},
                actor="user:engineer-en9",
            )
        )
        self.assertEqual("ready", plan.status)
        self.assertEqual({"L0", "L1", "L2"}, {item["layer"] for item in plan.items})
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "apply-en9-unified",
                "user:engineer-en9",
            )
        )

        self.assertEqual(en9_revision, application.revision_id)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_tags
                       WHERE node_id = %s AND source_address IS NOT NULL),
                      (SELECT count(*) FROM t_point_processing_input_bindings
                       WHERE installed_processing_id = %s),
                      (SELECT count(*) FROM t_point_processing_output_bindings
                       WHERE installed_processing_id = %s),
                      (SELECT count(*) FROM t_tags
                       WHERE node_id = %s
                         AND source_address IS NOT NULL
                         AND read_only IS NOT TRUE),
                      (SELECT count(*) FROM t_tags
                       WHERE node_id = %s
                         AND source_address IS NOT NULL
                         AND (
                           source_type IS DISTINCT FROM 'neuron'
                           OR source_path NOT LIKE 'edge-pcs-a/data/%%'
                           OR freshness_seconds IS DISTINCT FROM 5.0
                         )),
                      (SELECT count(*) FROM t_point_processing_inputs
                       WHERE revision_id = %s
                         AND expected_group = 'data'
                         AND expected_address IS NOT NULL
                         AND expected_wire_data_type IS NOT NULL
                         AND expected_read_only IS TRUE)
                    """,
                    (
                        str(node_id),
                        str(application.installed_processing_id),
                        str(application.installed_processing_id),
                        str(node_id),
                        str(node_id),
                        str(en9_revision),
                    ),
                )
                self.assertEqual((90, 90, 3, 0, 0, 90), cursor.fetchone())

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Point-processing tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
    @classmethod
    def tearDownClass(cls) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    def setUp(self) -> None:
        from app.services.telemetry_store import close_db_pool, init_db_pool

        close_db_pool()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration_test.DataTrunkMigrationPostgresTest._reset_through_037(cursor)
                migration_test.DataTrunkMigrationPostgresTest._apply_038(cursor)
                migration_test.DataTrunkMigrationPostgresTest._apply_039(cursor)
                migration_test.DataTrunkMigrationPostgresTest._apply_040(cursor)
                migration_test.DataTrunkMigrationPostgresTest._apply_041(cursor)
                migration_test.DataTrunkMigrationPostgresTest._apply_042(cursor)
        init_db_pool(min_conn=1, max_conn=4)

    def test_package_import_persists_complete_versioned_catalog_atomically(self) -> None:
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
        )
        from app.services.solution_delivery import (
            PostgresDeliveryRepository,
            SolutionDelivery,
        )

        delivery = SolutionDelivery(
            PostgresDeliveryRepository(),
            platform_version="0.4.77",
        )
        package = delivery.import_package(
            builder.build_archive(),
            actor="user:engineer-import",
        )

        summaries = PostgresPointProcessingCatalog().list_templates("PCS")
        self.assertEqual(
            [item.asset.asset_id for item in summaries],
            ["pcs.brand-a", "pcs.brand-b", "pcs.en9"],
        )
        self.assertEqual(
            [item.asset.display_name for item in summaries],
            ["PCS 通用品牌 A", "PCS 通用品牌 B", "恩玖 EN9 PCS"],
        )
        brand_a = next(item for item in summaries if item.asset.asset_id == "pcs.brand-a")
        self.assertEqual(len(brand_a.asset.inputs), 3)
        self.assertEqual(len(brand_a.asset.outputs), 3)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_solution_packages),
                      (SELECT count(*) FROM t_point_processing_templates),
                      (SELECT count(*) FROM t_point_processing_revisions),
                      (SELECT count(*) FROM t_point_processing_inputs),
                      (SELECT count(*) FROM t_point_processing_outputs),
                      (SELECT count(*) FROM t_enum_mapping_entries),
                      (SELECT count(*) FROM t_fault_code_mapping_entries),
                      (SELECT count(*) FROM t_boolean_set_mapping_entries),
                      (SELECT count(*) FROM t_solution_point_processing_assets)
                    """
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (1, 4, 4, 97, 10, 15, 4, 88, 4),
                )
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM t_solution_point_processing_assets
                    WHERE package_record_id = %s
                    """,
                    (package.id,),
                )
                self.assertEqual(cursor.fetchone(), (4,))

    def test_independent_brand_replacement_preserves_l2_ids_and_advances_lineage(self) -> None:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PreviewPointProcessing,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            build_postgres_point_processing,
        )

        package, brand_a_revision, brand_b_revision = self._import_reference_package()
        node_id = UUID("85000000-0000-0000-0000-000000000001")
        entity_ids = {
            "active_power": UUID("85000000-0000-0000-0000-000000000101"),
            "operating_state": UUID("85000000-0000-0000-0000-000000000102"),
            "fault_codes": UUID("85000000-0000-0000-0000-000000000103"),
        }
        self._seed_brand_a_site(
            package.id,
            package.digest,
            brand_a_revision,
            node_id,
            entity_ids,
        )

        service = build_postgres_point_processing()
        plan = service.preview(
            PreviewPointProcessing(
                node_id=node_id,
                template_revision_id=brand_b_revision,
                input_selections={},
                actor="user:engineer-replace",
            )
        )
        self.assertEqual(plan.status, "ready")
        self.assertEqual(
            {
                item["action"]
                for item in plan.items
                if item["kind"] == "output_binding"
            },
            {"preserve"},
        )

        command = ApplyPointProcessingPlan(
            plan.id,
            plan.digest,
            "replace-brand-b",
            "user:engineer-replace",
        )
        application = service.apply(command)
        repeated = service.apply(command)
        self.assertEqual(repeated, application)
        self.assertEqual(application.revision_id, brand_b_revision)
        self.assertEqual(application.site_configuration_version, 2)
        self.assertEqual(set(application.output_entity_instance_ids), set(entity_ids.values()))

        current = service.inspect(node_id, include_engineering=True).public_dict()
        self.assertEqual(current["l1_summary"]["revision_id"], str(brand_b_revision))
        self.assertEqual(
            {item["entity_instance_id"] for item in current["l2"]},
            {str(item) for item in entity_ids.values()},
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT current_version,
                           (SELECT count(*) FROM t_point_processing_applications),
                           (SELECT count(*) FROM t_point_processing_idempotency),
                           (SELECT count(*) FROM t_solution_installations),
                           (SELECT count(*) FROM t_installed_point_processings
                            WHERE current = TRUE)
                    FROM t_site_configuration_state
                    WHERE singleton = TRUE
                    """
                )
                self.assertEqual(cursor.fetchone(), (2, 2, 2, 2, 1))
                cursor.execute(
                    """
                    SELECT site_configuration_version
                    FROM t_solution_installations
                    WHERE id = %s
                    """,
                    (application.solution_installation_id,),
                )
                self.assertEqual(cursor.fetchone(), (2,))

    def test_schema_043_preserves_ordinary_node_processing_replacement(self) -> None:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PreviewPointProcessing,
        )
        from app.services.point_processing_postgres import (
            build_postgres_point_processing,
        )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration_test.DataTrunkMigrationPostgresTest._apply_043(cursor)

        package, brand_a_revision, brand_b_revision = self._import_reference_package()
        node_id = UUID("85000000-0000-0000-0000-000000000001")
        entity_ids = {
            "active_power": UUID("85000000-0000-0000-0000-000000000101"),
            "operating_state": UUID("85000000-0000-0000-0000-000000000102"),
            "fault_codes": UUID("85000000-0000-0000-0000-000000000103"),
        }
        self._seed_brand_a_site(
            package.id,
            package.digest,
            brand_a_revision,
            node_id,
            entity_ids,
        )
        service = build_postgres_point_processing()
        plan = service.preview(
            PreviewPointProcessing(
                node_id=node_id,
                template_revision_id=brand_b_revision,
                input_selections={},
                actor="user:schema-043-replace",
            )
        )
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "schema-043-node-replacement",
                "user:schema-043-replace",
            )
        )

        self.assertEqual(application.revision_id, brand_b_revision)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*), count(*) FILTER (WHERE current),
                           bool_and(processing_scope = 'node'),
                           count(processing_owner_key),
                           count(*) FILTER (WHERE revision.internal_kind IS NULL)
                    FROM t_installed_point_processings AS installed
                    JOIN t_point_processing_revisions AS revision
                      ON revision.id = installed.revision_id
                    WHERE installed.node_id = %s
                    """,
                    (node_id,),
                )
                self.assertEqual(cursor.fetchone(), (2, 1, True, 0, 2))

    def test_concurrent_same_plan_allows_one_apply_and_one_stale(self) -> None:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PreviewPointProcessing,
            PointProcessingError,
        )
        from app.services.point_processing_postgres import build_postgres_point_processing

        package, brand_a_revision, brand_b_revision = self._import_reference_package()
        node_id = UUID("85000000-0000-0000-0000-000000000001")
        entity_ids = {
            "active_power": UUID("85000000-0000-0000-0000-000000000101"),
            "operating_state": UUID("85000000-0000-0000-0000-000000000102"),
            "fault_codes": UUID("85000000-0000-0000-0000-000000000103"),
        }
        self._seed_brand_a_site(
            package.id,
            package.digest,
            brand_a_revision,
            node_id,
            entity_ids,
        )
        service = build_postgres_point_processing()
        plan = service.preview(
            PreviewPointProcessing(
                node_id=node_id,
                template_revision_id=brand_b_revision,
                input_selections={},
                actor="user:engineer-replace",
            )
        )
        barrier = Barrier(2)

        def apply(key: str) -> str:
            barrier.wait(timeout=5)
            try:
                service.apply(
                    ApplyPointProcessingPlan(
                        plan.id,
                        plan.digest,
                        key,
                        "user:engineer-replace",
                    )
                )
                return "APPLIED"
            except PointProcessingError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(apply, ("replace-a", "replace-b")))
        self.assertEqual(outcomes, ["APPLIED", "POINT_PROCESSING_PLAN_STALE"])

    def test_solution_install_creates_entities_and_conversion_in_one_transaction(self) -> None:
        delivery, plan, pcs_node_id = self._plan_reference_solution()
        self.assertEqual(plan.status, "ready", plan.blockers)
        self.assertEqual(len(plan.point_processing_plans), 1)
        outcome = delivery.apply_install(
            plan.id,
            plan.digest,
            "solution-first-install",
            "user:engineer-install",
        )
        repeated = delivery.apply_install(
            plan.id,
            plan.digest,
            "solution-first-install",
            "user:engineer-install",
        )
        self.assertEqual(repeated, outcome)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_entity_instances),
                      (SELECT count(*) FROM t_installed_point_processings
                       WHERE current = TRUE),
                      (SELECT count(*) FROM t_point_processing_output_bindings),
                      (SELECT count(*) FROM t_point_processing_applications),
                      (SELECT current_version FROM t_site_configuration_state
                       WHERE singleton = TRUE)
                    """
                )
                self.assertEqual(cursor.fetchone(), (10, 1, 3, 1, 1))
                cursor.execute(
                    """
                    SELECT entity.definition_id, entity.id, device.node_id
                    FROM t_entity_instances AS entity
                    JOIN t_device_instances AS device
                      ON device.id = entity.device_instance_id
                    WHERE entity.source_kind = 'point_processing'
                    ORDER BY entity.definition_id
                    """
                )
                rows = cursor.fetchall()
                self.assertEqual(len(rows), 3)
                self.assertEqual({row[2] for row in rows}, {pcs_node_id})
                self.assertEqual(
                    {row[1] for row in rows},
                    set(outcome.entity_instance_ids) & {row[1] for row in rows},
                )

    def test_040_rejects_a_direct_tag_binding_for_a_processed_entity(self) -> None:
        delivery, plan, _ = self._plan_reference_solution()
        delivery.apply_install(
            plan.id,
            plan.digest,
            "solution-contract-gate",
            "user:engineer-install",
        )

        with self.assertRaises(psycopg2.errors.CheckViolation):
            with psycopg2.connect(**self.connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT entity.id, tag.id
                        FROM t_entity_instances AS entity
                        CROSS JOIN LATERAL (
                          SELECT id FROM t_tags ORDER BY id LIMIT 1
                        ) AS tag
                        WHERE entity.source_kind = 'point_processing'
                        ORDER BY entity.id
                        LIMIT 1
                        """
                    )
                    entity_id, tag_id = cursor.fetchone()
                    confirmation_id = UUID(
                        "86000000-0000-0000-0009-000000000001"
                    )
                    binding_id = UUID("86000000-0000-0000-0009-000000000002")
                    cursor.execute(
                        """
                        INSERT INTO t_entity_binding_confirmations
                          (id, entity_instance_id, binding_id, actor, matcher_id,
                           reason, plan_digest, selected_tag_id)
                        VALUES (%s, %s, %s, 'user:engineer', 'forbidden-direct',
                                'contract-negative', %s, %s)
                        """,
                        (confirmation_id, entity_id, binding_id, "f" * 64, tag_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_entity_instance_bindings
                          (id, entity_instance_id, tag_id, matcher_id,
                           confirmation_audit_id, active)
                        VALUES (%s, %s, %s, 'forbidden-direct', %s, TRUE)
                        """,
                        (binding_id, entity_id, tag_id, confirmation_id),
                    )

    def test_production_gate_rejects_a_preexisting_zero_source_entity(self) -> None:
        from app.services.data_trunk_postgres import (
            verify_data_trunk_contract_gate,
        )

        delivery, plan, _ = self._plan_reference_solution()
        delivery.apply_install(
            plan.id,
            plan.digest,
            "startup-gate-solution-install",
            "user:engineer-install",
        )

        @contextmanager
        def connection_factory():
            connection = psycopg2.connect(**self.connection_kwargs)
            try:
                yield connection
            finally:
                connection.close()

        self.assertGreater(verify_data_trunk_contract_gate(connection_factory), 0)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    ALTER TABLE t_point_processing_output_bindings
                    DISABLE TRIGGER trg_processing_output_binding_single_source
                    """
                )
                cursor.execute(
                    """
                    DELETE FROM t_point_processing_output_bindings
                    WHERE entity_instance_id = (
                      SELECT id
                      FROM t_entity_instances
                      WHERE source_kind = 'point_processing'
                      ORDER BY id
                      LIMIT 1
                    )
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE t_point_processing_output_bindings
                    ENABLE TRIGGER trg_processing_output_binding_single_source
                    """
                )

        with self.assertRaises(psycopg2.errors.CheckViolation):
            verify_data_trunk_contract_gate(connection_factory)

    def test_output_binding_failure_rolls_back_entire_solution_install(self) -> None:
        from app.services.solution_delivery_contracts import DeliveryError

        delivery, plan, _ = self._plan_reference_solution()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE OR REPLACE FUNCTION fail_point_processing_output_binding()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                      RAISE EXCEPTION 'simulated output binding failure';
                    END;
                    $$
                    """
                )
                cursor.execute(
                    """
                    CREATE TRIGGER trg_fail_point_processing_output_binding
                    BEFORE INSERT ON t_point_processing_output_bindings
                    FOR EACH ROW EXECUTE FUNCTION fail_point_processing_output_binding()
                    """
                )
        with self.assertRaises(DeliveryError) as raised:
            delivery.apply_install(
                plan.id,
                plan.digest,
                "solution-output-failure",
                "user:engineer-install",
            )
        self.assertEqual(raised.exception.code, "DATA_TRUNK_UNAVAILABLE")
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_entity_instances),
                      (SELECT count(*) FROM t_solution_installations),
                      (SELECT count(*) FROM t_site_configuration_versions
                       WHERE installation_id IS NOT NULL),
                      (SELECT count(*) FROM t_installed_point_processings),
                      (SELECT count(*) FROM t_point_processing_applications),
                      (SELECT count(*) FROM t_solution_delivery_audit),
                      (SELECT current_version FROM t_site_configuration_state
                       WHERE singleton = TRUE)
                    """
                )
                self.assertEqual(cursor.fetchone(), (0, 0, 0, 0, 0, 0, 0))

    def test_installed_conversion_drives_l2_realtime_and_history(self) -> None:
        from app.services.data_trunk_contracts import (
            RawObservation,
            TrunkQuality,
            TypedValue,
            ValueKind,
        )
        from app.services.data_trunk_postgres import build_postgres_data_trunk
        from app.services.entity_instance_postgres import (
            PostgresEntityInstanceRepository,
            PostgresObservationCatalog,
            PostgresSourceCatalog,
        )
        from app.services.entity_instance_registry import EntityInstanceRegistry
        from app.services.entity_instance_runtime import EntityInstanceRuntime

        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration_test.DataTrunkMigrationPostgresTest._apply_043(cursor)

        delivery, plan, node_id = self._plan_reference_solution()
        delivery.apply_install(
            plan.id,
            plan.digest,
            "solution-runtime-install",
            "user:engineer-install",
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT name, id, data_type, COALESCE(unit_to, unit)
                    FROM t_tags
                    WHERE node_id = %s
                      AND name IN ('ActivePowerRaw','RunningState','FaultCodeText')
                    """,
                    (node_id,),
                )
                tags = {row[0]: row[1:] for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT id FROM t_entity_instances
                    WHERE definition_id = 'pcs.active_power'
                    """
                )
                entity_id = cursor.fetchone()[0]
        observed_at = datetime.now(UTC) - timedelta(seconds=1)
        values = {
            "ActivePowerRaw": (TypedValue.float(12345.0), "W"),
            "RunningState": (TypedValue(ValueKind.STRING, "2"), None),
            "FaultCodeText": (TypedValue(ValueKind.STRING, "E30"), None),
        }
        raw = tuple(
            RawObservation(
                observation_id=UUID(int=0x900 + index),
                node_id=node_id,
                tag_id=tags[name][0],
                source_key=name,
                value=value,
                raw_unit=unit,
                quality=TrunkQuality.GOOD,
                source_timestamp=observed_at,
                received_at=observed_at + timedelta(milliseconds=100),
                source_message_id="task6-runtime-message",
                source_sequence=index,
                source_digest=chr(ord("a") + index) * 64,
                event_time_basis="observed_at",
            )
            for index, (name, (value, unit)) in enumerate(values.items(), 1)
        )
        receipt = build_postgres_data_trunk().ingest(raw)
        self.assertEqual(len(receipt.l2_event_ids), 3)
        runtime = EntityInstanceRuntime(
            EntityInstanceRegistry(
                PostgresEntityInstanceRepository(),
                PostgresSourceCatalog(),
                lambda _transaction=None: 1,
            ),
            PostgresObservationCatalog(),
        )
        latest = runtime.read(entity_id)
        history = runtime.history(entity_id, "1h")
        self.assertEqual(latest.value, 12.345)
        self.assertEqual(latest.source_kind, "point_processing")
        self.assertIsNotNone(latest.processing_revision_id)
        self.assertEqual(len(history), 1)

    def test_template_catalog_failure_rolls_back_package_and_catalog(self) -> None:
        from app.services.solution_delivery import (
            PostgresDeliveryRepository,
            SolutionDelivery,
        )
        from app.services.solution_delivery_contracts import DeliveryError

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE OR REPLACE FUNCTION fail_enum_mapping_entry()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                      RAISE EXCEPTION 'simulated enum mapping failure';
                    END;
                    $$
                    """
                )
                cursor.execute(
                    """
                    CREATE TRIGGER trg_fail_enum_mapping_entry
                    BEFORE INSERT ON t_enum_mapping_entries
                    FOR EACH ROW EXECUTE FUNCTION fail_enum_mapping_entry()
                    """
                )
        delivery = SolutionDelivery(
            PostgresDeliveryRepository(),
            platform_version="0.4.77",
        )
        with self.assertRaises(DeliveryError) as raised:
            delivery.import_package(
                builder.build_archive(),
                "user:engineer-import",
            )
        self.assertEqual(
            raised.exception.code,
            "POINT_PROCESSING_CATALOG_UNAVAILABLE",
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_solution_packages),
                      (SELECT count(*) FROM t_solution_package_assets),
                      (SELECT count(*) FROM t_point_processing_templates),
                      (SELECT count(*) FROM t_point_processing_revisions),
                      (SELECT count(*) FROM t_point_processing_inputs),
                      (SELECT count(*) FROM t_point_processing_outputs)
                    """
                )
                self.assertEqual(cursor.fetchone(), (0, 0, 0, 0, 0, 0))

    def test_admin_retirement_hides_new_candidate_but_keeps_installed_revision(self) -> None:
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
        )
        from app.services.solution_delivery import (
            PostgresDeliveryRepository,
            SolutionDelivery,
        )

        package, brand_a_revision, _brand_b_revision = (
            self._import_reference_package()
        )
        node_id = UUID("85000000-0000-0000-0000-000000000001")
        entity_ids = {
            "active_power": UUID("85000000-0000-0000-0000-000000000101"),
            "operating_state": UUID("85000000-0000-0000-0000-000000000102"),
            "fault_codes": UUID("85000000-0000-0000-0000-000000000103"),
        }
        self._seed_brand_a_site(
            package.id,
            package.digest,
            brand_a_revision,
            node_id,
            entity_ids,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, revision_id
                    FROM t_installed_point_processings
                    WHERE current = TRUE
                    """
                )
                installed_before = cursor.fetchone()

        with TemporaryDirectory() as temporary_directory:
            retired_source = Path(temporary_directory) / "reference"
            shutil.copytree(builder.DEFAULT_SOURCE, retired_source)
            package_path = retired_source / "package.yaml"
            package_path.write_text(
                package_path.read_text(encoding="utf-8").replace(
                    "version: 1.1.0",
                    "version: 1.1.1",
                    1,
                ),
                encoding="utf-8",
            )
            asset_path = retired_source / "point-processings" / "pcs-brand-a.yaml"
            retired_asset = asset_path.read_text(encoding="utf-8")
            retired_asset = retired_asset.replace("revision: 1", "revision: 2", 1)
            retired_asset = retired_asset.replace(
                "status: active",
                "status: retired",
                1,
            )
            asset_path.write_text(retired_asset, encoding="utf-8")
            SolutionDelivery(
                PostgresDeliveryRepository(),
                platform_version="0.4.77",
            ).import_package(
                builder.build_archive(retired_source),
                "user:admin-retire",
            )

        active_assets = {
            item.asset.asset_id
            for item in PostgresPointProcessingCatalog().list_templates("PCS")
        }
        self.assertEqual(active_assets, {"pcs.brand-b", "pcs.en9"})
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, revision_id
                    FROM t_installed_point_processings
                    WHERE current = TRUE
                    """
                )
                self.assertEqual(cursor.fetchone(), installed_before)
                cursor.execute(
                    """
                    SELECT actor, outcome, details->>'before', details->>'after'
                    FROM t_audit_events
                    WHERE event = 'point_processing.template_status'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                self.assertEqual(
                    cursor.fetchone(),
                    ("user:admin-retire", "allowed", "active", "retired"),
                )

    def _plan_reference_solution(self):
        from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog
        from app.services.entity_instance_postgres import (
            PostgresEntityInstanceRepository,
            PostgresSourceCatalog,
        )
        from app.services.entity_instance_registry import EntityInstanceRegistry
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            build_postgres_point_processing,
        )
        from app.services.solution_delivery import (
            PostgresDeliveryRepository,
            SolutionDelivery,
        )

        self._seed_reference_sources()
        repository = PostgresDeliveryRepository()
        delivery = SolutionDelivery(
            repository,
            platform_version="0.4.77",
            entity_instance_registry=EntityInstanceRegistry(
                PostgresEntityInstanceRepository(),
                PostgresSourceCatalog(),
                repository.site_configuration_version,
            ),
            alarm_definitions=PostgresAlarmDefinitionCatalog(),
            point_processings=build_postgres_point_processing(),
        )
        package = delivery.import_package(
            builder.build_archive(),
            "user:engineer-install",
        )
        brand_a_revision = next(
            item.revision_id
            for item in PostgresPointProcessingCatalog().list_templates("PCS")
            if item.asset.asset_id == "pcs.brand-a"
        )
        pcs_node_id = UUID("86000000-0000-0000-0000-000000000001")
        plan = delivery.plan_install(
            package.id,
            parameters={
                "pcs.instances": [
                    {"instance_key": "PCS-01", "device_key": "PCS-01"}
                ],
                "bms.instances": [
                    {"instance_key": "BMS-01", "device_key": "BMS-01"}
                ],
                "pv.instances": [
                    {"instance_key": "PV-01", "device_key": "PV-01"}
                ],
                "evse.instances": [
                    {"instance_key": "EVSE-01", "device_key": "EVSE-01"}
                ],
                "meter.instance_key": "METER-01",
                "meter.device_key": "METER-01",
            },
            secret_references={
                "gateway.credentials": "secret://reference/gateway"
            },
            point_processings=[
                {
                    "node_id": pcs_node_id,
                    "template_revision_id": brand_a_revision,
                    "input_selections": {},
                }
            ],
            actor="user:engineer-install",
        )
        self.assertEqual(plan.status, "ready", plan.blockers)
        self.assertEqual(len(plan.point_processing_plans), 1)
        return delivery, plan, pcs_node_id

    def _seed_reference_sources(self) -> None:
        nodes = (
            (
                UUID("86000000-0000-0000-0000-000000000001"),
                "PCS-01",
                (
                    ("ActivePowerRaw", "FLOAT", "W", "R"),
                    ("RunningState", "STRING", None, "R"),
                    ("FaultCodeText", "STRING", None, "R"),
                    ("ActivePowerSetpoint", "FLOAT", "kW", "RW"),
                    ("ActivePowerReadback", "FLOAT", "kW", "R"),
                    ("BmsReady", "BOOL", None, "R"),
                ),
            ),
            (
                UUID("86000000-0000-0000-0000-000000000002"),
                "BMS-01",
                (("StateOfCharge", "FLOAT", "%", "R"),),
            ),
            (
                UUID("86000000-0000-0000-0000-000000000003"),
                "PV-01",
                (("ActivePower", "FLOAT", "kW", "R"),),
            ),
            (
                UUID("86000000-0000-0000-0000-000000000004"),
                "EVSE-01",
                (("ActivePower", "FLOAT", "kW", "R"),),
            ),
            (
                UUID("86000000-0000-0000-0000-000000000005"),
                "METER-01",
                (("ActivePower", "FLOAT", "kW", "R"),),
            ),
        )
        tag_index = 1
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                for node_id, source_key, tags in nodes:
                    cursor.execute(
                        """
                        INSERT INTO t_nodes (id, name, source_catalog_key)
                        VALUES (%s, %s, %s)
                        """,
                        (node_id, source_key, source_key),
                    )
                    for name, data_type, unit, direction in tags:
                        tag_id = UUID(
                            f"86000000-0000-0000-0001-{tag_index:012d}"
                        )
                        tag_index += 1
                        cursor.execute(
                            """
                            INSERT INTO t_tags
                              (id, node_id, name, data_type, unit,
                               read_write, enabled)
                            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                            """,
                            (tag_id, node_id, name, data_type, unit, direction),
                        )

    def _import_reference_package(self):
        from app.services.point_processing_postgres import PostgresPointProcessingCatalog
        from app.services.solution_delivery import (
            PostgresDeliveryRepository,
            SolutionDelivery,
        )

        package = SolutionDelivery(
            PostgresDeliveryRepository(),
            platform_version="0.4.77",
        ).import_package(builder.build_archive(), "user:engineer-import")
        summaries = PostgresPointProcessingCatalog().list_templates("PCS")
        by_asset = {item.asset.asset_id: item.revision_id for item in summaries}
        return package, by_asset["pcs.brand-a"], by_asset["pcs.brand-b"]

    def _seed_brand_a_site(
        self,
        package_id: UUID,
        package_digest: str,
        revision_id: UUID,
        node_id: UUID,
        entity_ids: dict[str, UUID],
    ) -> None:
        plan_id = UUID("85000000-0000-0000-0000-000000000201")
        installation_id = UUID("85000000-0000-0000-0000-000000000202")
        identity_id = UUID("85000000-0000-0000-0000-000000000203")
        device_id = UUID("85000000-0000-0000-0000-000000000204")
        conversion_plan_id = UUID("85000000-0000-0000-0000-000000000205")
        installed_id = UUID("85000000-0000-0000-0000-000000000206")
        application_id = UUID("85000000-0000-0000-0000-000000000207")
        source_specs = (
            ("PActKw", "FLOAT", "kW"),
            ("ModeCode", "STRING", None),
            ("AlarmList", "STRING", None),
            ("ActivePowerRaw", "FLOAT", "W"),
            ("RunningState", "STRING", None),
            ("FaultCodeText", "STRING", None),
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO t_nodes (id, name, source_catalog_key) VALUES (%s, 'PCS-01', 'edge-pcs-a')",
                    (node_id,),
                )
                tag_ids: dict[str, UUID] = {}
                for index, (name, data_type, unit) in enumerate(source_specs, 1):
                    tag_id = UUID(f"85000000-0000-0000-0000-{index:012d}")
                    tag_ids[name] = tag_id
                    cursor.execute(
                        """
                        INSERT INTO t_tags
                          (id, node_id, name, data_type, unit, read_write, enabled)
                        VALUES (%s, %s, %s, %s, %s, 'R', TRUE)
                        """,
                        (tag_id, node_id, name, data_type, unit),
                    )
                cursor.execute(
                    """
                    INSERT INTO t_solution_install_plans
                      (id, package_record_id, package_digest,
                       base_site_configuration_version, status, items, blockers,
                       parameter_contracts, parameters, secret_references,
                       parameter_sources, parameter_metadata, configuration_digest,
                       target_installation_id, entity_identity_installation_id,
                       entity_plan, alarm_plan, point_processing_plans, digest)
                    VALUES (%s, %s, %s, 0, 'ready', '[]', '[]', '[]', '{}', '{}',
                            '{}', '{}', %s, %s, %s, NULL, NULL, '[]', %s)
                    """,
                    (
                        plan_id,
                        package_id,
                        package_digest,
                        "a" * 64,
                        installation_id,
                        identity_id,
                        "b" * 64,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_solution_installations
                      (id, plan_id, package_record_id, package_digest,
                       site_configuration_version, status, entity_instance_ids)
                    VALUES (%s, %s, %s, %s, 1, 'installed', %s)
                    """,
                    (
                        installation_id,
                        plan_id,
                        package_id,
                        package_digest,
                        list(entity_ids.values()),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_site_configuration_versions
                      (version, previous_version, installation_id,
                       package_record_id, package_digest, actor, parameters,
                       secret_references, parameter_metadata,
                       configuration_digest, entity_identity_installation_id)
                    VALUES (1, 0, %s, %s, %s, 'user:engineer-install', '{}',
                            '{}', '{}', %s, %s)
                    """,
                    (installation_id, package_id, package_digest, "a" * 64, identity_id),
                )
                cursor.execute(
                    "UPDATE t_site_configuration_state SET current_version = 1 WHERE singleton = TRUE"
                )
                cursor.execute(
                    """
                    INSERT INTO t_device_instances
                      (id, identity_installation_id, slot_id, instance_key,
                       device_category, display_name, node_id)
                    VALUES (%s, %s, 'slot.pcs', 'PCS-01', 'pcs', 'PCS-01', %s)
                    """,
                    (device_id, identity_id, node_id),
                )
                definitions = (
                    ("active_power", "pcs.active_power", "FLOAT", "kW"),
                    ("operating_state", "pcs.operating_state", "ENUM", None),
                    ("fault_codes", "pcs.fault_codes", "CODE_SET", None),
                )
                for output_key, definition_id, data_type, unit in definitions:
                    cursor.execute(
                        """
                        INSERT INTO t_entity_instances
                          (id, device_instance_id, definition_id, display_name,
                           data_type, unit, direction, freshness_seconds,
                           source_kind)
                        VALUES (%s, %s, %s, %s, %s, %s, 'R', 30,
                                'point_processing')
                        """,
                        (
                            entity_ids[output_key],
                            device_id,
                            definition_id,
                            definition_id,
                            data_type,
                            unit,
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_plans
                      (id, node_id, template_revision_id,
                       entity_identity_installation_id, solution_installation_id,
                       base_site_configuration_version, source_catalog_digest,
                       status, items, blockers, digest, planned_by)
                    VALUES (%s, %s, %s, %s, %s, 0, %s, 'applied', '[]', '[]',
                            %s, 'user:engineer-install')
                    """,
                    (
                        conversion_plan_id,
                        node_id,
                        revision_id,
                        identity_id,
                        installation_id,
                        "c" * 64,
                        "d" * 64,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_installed_point_processings
                      (id, node_id, revision_id, source_plan_id,
                       solution_installation_id, site_configuration_version,
                       installed_by, current)
                    VALUES (%s, %s, %s, %s, %s, 1,
                            'user:engineer-install', TRUE)
                    """,
                    (installed_id, node_id, revision_id, conversion_plan_id, installation_id),
                )
                cursor.execute(
                    """
                    SELECT id, input_key FROM t_point_processing_inputs
                    WHERE revision_id = %s
                    """,
                    (revision_id,),
                )
                brand_a_tags = {
                    "active_power_raw": tag_ids["ActivePowerRaw"],
                    "operating_state_raw": tag_ids["RunningState"],
                    "fault_codes_raw": tag_ids["FaultCodeText"],
                }
                for input_id, input_key in cursor.fetchall():
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_input_bindings
                          (installed_processing_id, input_id, source_kind,
                           l0_tag_id, confirmed_by)
                        VALUES (%s, %s, 'l0', %s, 'user:engineer-install')
                        """,
                        (installed_id, input_id, brand_a_tags[input_key]),
                    )
                cursor.execute(
                    """
                    SELECT id, output_key FROM t_point_processing_outputs
                    WHERE revision_id = %s
                    """,
                    (revision_id,),
                )
                for output_id, output_key in cursor.fetchall():
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_output_bindings
                          (installed_processing_id, output_id, entity_instance_id)
                        VALUES (%s, %s, %s)
                        """,
                        (installed_id, output_id, entity_ids[output_key]),
                    )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_applications
                      (id, plan_id, installed_processing_id,
                       solution_installation_id, site_configuration_version,
                       actor, output_entity_instance_ids)
                    VALUES (%s, %s, %s, %s, 1, 'user:engineer-install', %s)
                    """,
                    (
                        application_id,
                        conversion_plan_id,
                        installed_id,
                        installation_id,
                        list(entity_ids.values()),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_idempotency
                      (actor, idempotency_key, request_digest, application_id)
                    VALUES ('user:engineer-install', 'initial-brand-a', %s, %s)
                    """,
                    ("e" * 64, application_id),
                )


if __name__ == "__main__":
    unittest.main()
