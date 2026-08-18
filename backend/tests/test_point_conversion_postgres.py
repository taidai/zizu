from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import UTC, datetime, timedelta
import unittest
from uuid import UUID

import psycopg2

from tests.test_data_trunk_migration_postgres import DataTrunkMigrationPostgresTest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_reference_delivery.py"
SPEC = importlib.util.spec_from_file_location("build_reference_delivery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL point-conversion tests",
)
class PointConversionPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Point-conversion tests require a *_test database")
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
                DataTrunkMigrationPostgresTest._reset_through_037(cursor)
                DataTrunkMigrationPostgresTest._apply_038(cursor)
        init_db_pool(min_conn=1, max_conn=4)

    def test_package_import_persists_complete_versioned_catalog_atomically(self) -> None:
        from app.services.point_conversion_postgres import (
            PostgresPointConversionCatalog,
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

        summaries = PostgresPointConversionCatalog().list_templates("PCS")
        self.assertEqual(
            [item.asset.asset_id for item in summaries],
            ["pcs.brand-a", "pcs.brand-b"],
        )
        self.assertEqual(
            [item.asset.display_name for item in summaries],
            ["PCS 通用品牌 A", "PCS 通用品牌 B"],
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
                      (SELECT count(*) FROM t_point_conversion_templates),
                      (SELECT count(*) FROM t_point_conversion_revisions),
                      (SELECT count(*) FROM t_point_conversion_inputs),
                      (SELECT count(*) FROM t_point_conversion_outputs),
                      (SELECT count(*) FROM t_enum_mapping_entries),
                      (SELECT count(*) FROM t_fault_code_mapping_entries),
                      (SELECT count(*) FROM t_solution_point_conversion_assets)
                    """
                )
                self.assertEqual(cursor.fetchone(), (1, 2, 2, 6, 6, 8, 4, 2))
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM t_solution_point_conversion_assets
                    WHERE package_record_id = %s
                    """,
                    (package.id,),
                )
                self.assertEqual(cursor.fetchone(), (2,))

    def test_independent_brand_replacement_preserves_l2_ids_and_advances_lineage(self) -> None:
        from app.services.point_conversion import (
            ApplyPointConversionPlan,
            PlanPointConversion,
        )
        from app.services.point_conversion_postgres import (
            PostgresPointConversionCatalog,
            build_postgres_point_conversion,
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

        service = build_postgres_point_conversion()
        plan = service.plan(
            PlanPointConversion(
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

        command = ApplyPointConversionPlan(
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

        current = service.inspect_node(node_id, include_engineering=True).public_dict()
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
                           (SELECT count(*) FROM t_point_conversion_applications),
                           (SELECT count(*) FROM t_point_conversion_idempotency),
                           (SELECT count(*) FROM t_solution_installations),
                           (SELECT count(*) FROM t_installed_point_conversions
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

    def test_concurrent_same_plan_allows_one_apply_and_one_stale(self) -> None:
        from app.services.point_conversion import (
            ApplyPointConversionPlan,
            PlanPointConversion,
            PointConversionError,
        )
        from app.services.point_conversion_postgres import build_postgres_point_conversion

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
        service = build_postgres_point_conversion()
        plan = service.plan(
            PlanPointConversion(
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
                    ApplyPointConversionPlan(
                        plan.id,
                        plan.digest,
                        key,
                        "user:engineer-replace",
                    )
                )
                return "APPLIED"
            except PointConversionError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(apply, ("replace-a", "replace-b")))
        self.assertEqual(outcomes, ["APPLIED", "POINT_CONVERSION_PLAN_STALE"])

    def test_solution_install_creates_entities_and_conversion_in_one_transaction(self) -> None:
        delivery, plan, pcs_node_id = self._plan_reference_solution()
        self.assertEqual(plan.status, "ready", plan.blockers)
        self.assertEqual(len(plan.point_conversion_plans), 1)
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
                      (SELECT count(*) FROM t_installed_point_conversions
                       WHERE current = TRUE),
                      (SELECT count(*) FROM t_conversion_output_bindings),
                      (SELECT count(*) FROM t_point_conversion_applications),
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
                    WHERE entity.source_kind = 'point_conversion'
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

    def test_output_binding_failure_rolls_back_entire_solution_install(self) -> None:
        from app.services.solution_delivery_contracts import DeliveryError

        delivery, plan, _ = self._plan_reference_solution()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE OR REPLACE FUNCTION fail_point_conversion_output_binding()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                      RAISE EXCEPTION 'simulated output binding failure';
                    END;
                    $$
                    """
                )
                cursor.execute(
                    """
                    CREATE TRIGGER trg_fail_point_conversion_output_binding
                    BEFORE INSERT ON t_conversion_output_bindings
                    FOR EACH ROW EXECUTE FUNCTION fail_point_conversion_output_binding()
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
                      (SELECT count(*) FROM t_installed_point_conversions),
                      (SELECT count(*) FROM t_point_conversion_applications),
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
        self.assertEqual(latest.source_kind, "point_conversion")
        self.assertIsNotNone(latest.conversion_revision_id)
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
            "POINT_CONVERSION_CATALOG_UNAVAILABLE",
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_solution_packages),
                      (SELECT count(*) FROM t_solution_package_assets),
                      (SELECT count(*) FROM t_point_conversion_templates),
                      (SELECT count(*) FROM t_point_conversion_revisions),
                      (SELECT count(*) FROM t_point_conversion_inputs),
                      (SELECT count(*) FROM t_point_conversion_outputs)
                    """
                )
                self.assertEqual(cursor.fetchone(), (0, 0, 0, 0, 0, 0))

    def test_admin_retirement_hides_new_candidate_but_keeps_installed_revision(self) -> None:
        from app.services.point_conversion_postgres import (
            PostgresPointConversionCatalog,
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
                    FROM t_installed_point_conversions
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
                    "version: 1.0.0",
                    "version: 1.0.1",
                    1,
                ),
                encoding="utf-8",
            )
            asset_path = retired_source / "point-conversions" / "pcs-brand-a.yaml"
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
            for item in PostgresPointConversionCatalog().list_templates("PCS")
        }
        self.assertEqual(active_assets, {"pcs.brand-b"})
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, revision_id
                    FROM t_installed_point_conversions
                    WHERE current = TRUE
                    """
                )
                self.assertEqual(cursor.fetchone(), installed_before)
                cursor.execute(
                    """
                    SELECT actor, outcome, details->>'before', details->>'after'
                    FROM t_audit_events
                    WHERE event = 'point_conversion.template_status'
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
        from app.services.point_conversion_postgres import (
            PostgresPointConversionCatalog,
            build_postgres_point_conversion,
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
            point_conversions=build_postgres_point_conversion(),
        )
        package = delivery.import_package(
            builder.build_archive(),
            "user:engineer-install",
        )
        brand_a_revision = next(
            item.revision_id
            for item in PostgresPointConversionCatalog().list_templates("PCS")
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
            point_conversions=[
                {
                    "node_id": pcs_node_id,
                    "template_revision_id": brand_a_revision,
                    "input_selections": {},
                }
            ],
            actor="user:engineer-install",
        )
        self.assertEqual(plan.status, "ready", plan.blockers)
        self.assertEqual(len(plan.point_conversion_plans), 1)
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
        from app.services.point_conversion_postgres import PostgresPointConversionCatalog
        from app.services.solution_delivery import (
            PostgresDeliveryRepository,
            SolutionDelivery,
        )

        package = SolutionDelivery(
            PostgresDeliveryRepository(),
            platform_version="0.4.77",
        ).import_package(builder.build_archive(), "user:engineer-import")
        summaries = PostgresPointConversionCatalog().list_templates("PCS")
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
                       entity_plan, alarm_plan, point_conversion_plans, digest)
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
                                'point_conversion')
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
                    INSERT INTO t_point_conversion_plans
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
                    INSERT INTO t_installed_point_conversions
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
                    SELECT id, input_key FROM t_point_conversion_inputs
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
                        INSERT INTO t_conversion_input_bindings
                          (installed_conversion_id, input_id, source_kind,
                           l0_tag_id, confirmed_by)
                        VALUES (%s, %s, 'l0', %s, 'user:engineer-install')
                        """,
                        (installed_id, input_id, brand_a_tags[input_key]),
                    )
                cursor.execute(
                    """
                    SELECT id, output_key FROM t_point_conversion_outputs
                    WHERE revision_id = %s
                    """,
                    (revision_id,),
                )
                for output_id, output_key in cursor.fetchall():
                    cursor.execute(
                        """
                        INSERT INTO t_conversion_output_bindings
                          (installed_conversion_id, output_id, entity_instance_id)
                        VALUES (%s, %s, %s)
                        """,
                        (installed_id, output_id, entity_ids[output_key]),
                    )
                cursor.execute(
                    """
                    INSERT INTO t_point_conversion_applications
                      (id, plan_id, installed_conversion_id,
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
                    INSERT INTO t_point_conversion_idempotency
                      (actor, idempotency_key, request_digest, application_id)
                    VALUES ('user:engineer-install', 'initial-brand-a', %s, %s)
                    """,
                    ("e" * 64, application_id),
                )


if __name__ == "__main__":
    unittest.main()
