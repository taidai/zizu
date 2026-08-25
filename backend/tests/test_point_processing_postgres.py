"""Real PostgreSQL evidence for direct node-owned L1 publication."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg2

os.environ.setdefault("NEURON_PASSWORD", "test-neuron-secret")
os.environ.setdefault("NANOMQ_API_PASSWORD", "test-nanomq-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value-that-is-long-enough")

from tests import test_node_data_trunk_hard_cut_migration_postgres as migration


REFERENCE_DIR = Path(__file__).resolve().parents[2] / "reference-point-processings"
NODE_ID = UUID("92000000-0000-0000-0000-000000000001")


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL point-processing tests",
)
class PointProcessingPostgresTest(unittest.TestCase):
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

    def setUp(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration.NodeDataTrunkHardCutMigrationPostgresTest._reset_through_043(
                    cursor
                )
                migration.NodeDataTrunkHardCutMigrationPostgresTest._apply_044(cursor)
        from app.services.telemetry_store import init_db_pool

        init_db_pool(1, 4)
        self._seed_node_and_tags()

    def tearDown(self) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    @staticmethod
    def _raw(name: str) -> dict:
        return json.loads(
            (REFERENCE_DIR / f"{name}.zizu-point-processing.json").read_text(
                encoding="utf-8"
            )
        )

    def _seed_node_and_tags(self) -> None:
        from app.services.telemetry_store import get_connection

        contracts: dict[str, tuple[str, str | None]] = {}
        for name in ("pcs-brand-a", "pcs-brand-b"):
            for item in self._raw(name)["inputs"]:
                contracts[item["sourceKey"]] = (item["dataType"], item.get("unit"))
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled) "
                    "VALUES(%s,'PCS-TEST','PCS',TRUE)",
                    (NODE_ID,),
                )
                for key, (data_type, unit) in sorted(contracts.items()):
                    cursor.execute(
                        """
                        INSERT INTO t_tags
                          (id,node_id,name,data_type,unit,read_write,enabled,
                           timestamp_trusted)
                        VALUES(%s,%s,%s,%s,%s,'R',TRUE,FALSE)
                        """,
                        (
                            uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/{key}"),
                            NODE_ID,
                            key,
                            data_type,
                            unit,
                        ),
                    )
            connection.commit()

    def _service_and_revision(self, name: str):
        from app.services.point_processing import PointProcessingService
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )

        registered = PostgresPointProcessingTemplates().import_template(
            self._raw(name),
            actor="test:engineer",
        )
        repository = PostgresPointProcessingRepository()
        return (
            PointProcessingService(repository, PostgresPointProcessingCatalog()),
            repository,
            registered.revision_id,
        )

    def _plan(self, service, revision_id):
        from app.services.point_processing import PreviewPointProcessing

        return service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=revision_id,
                input_selections={},
                actor="test:engineer",
            )
        )

    def test_apply_publishes_one_revision_and_attaches_l2_directly_to_node(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan
        from app.services.telemetry_store import get_connection

        service, repository, revision_id = self._service_and_revision("pcs-brand-a")
        plan = self._plan(service, revision_id)
        application = service.apply(
            ApplyPointProcessingPlan(plan.id, plan.digest, "apply-1", "test:engineer")
        )

        self.assertEqual(0, plan.base_configuration_revision)
        self.assertEqual(1, application.configuration_revision)
        self.assertEqual(1, repository.configuration_revision())
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT node_id FROM t_entity_instances "
                    "WHERE id=ANY(%s)",
                    (list(application.output_entity_instance_ids),),
                )
                self.assertEqual([(NODE_ID,)], cursor.fetchall())

    def test_stale_plan_writes_no_installation(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan, PointProcessingError
        from app.services.telemetry_store import get_connection

        service, _repository, revision_id = self._service_and_revision("pcs-brand-a")
        plan = self._plan(service, revision_id)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_configuration_revisions
                      (revision,previous_revision,actor,action,resource_kind,
                       resource_id,after_digest)
                    VALUES(1,0,'test','test.bump','test','test',%s)
                    """,
                    ("f" * 64,),
                )
                cursor.execute(
                    "UPDATE t_configuration_state SET current_revision=1 "
                    "WHERE singleton=TRUE"
                )
            connection.commit()

        with self.assertRaises(PointProcessingError) as caught:
            service.apply(
                ApplyPointProcessingPlan(plan.id, plan.digest, "stale", "test:engineer")
            )
        self.assertEqual("POINT_PROCESSING_PLAN_STALE", caught.exception.code)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_installed_point_processings")
                self.assertEqual(0, cursor.fetchone()[0])

    def test_brand_replacement_preserves_l2_entity_ids(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan

        service, _repository, revision_a = self._service_and_revision("pcs-brand-a")
        plan_a = self._plan(service, revision_a)
        first = service.apply(
            ApplyPointProcessingPlan(plan_a.id, plan_a.digest, "brand-a", "test:engineer")
        )
        service, _repository, revision_b = self._service_and_revision("pcs-brand-b")
        plan_b = self._plan(service, revision_b)
        second = service.apply(
            ApplyPointProcessingPlan(plan_b.id, plan_b.digest, "brand-b", "test:engineer")
        )

        self.assertEqual(first.output_entity_instance_ids, second.output_entity_instance_ids)
        self.assertEqual(2, second.configuration_revision)

    def test_failure_after_l2_bindings_rolls_back_revision_and_installation(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan, PointProcessingService
        from app.services.point_processing_postgres import PostgresPointProcessingCatalog
        from app.services.telemetry_store import get_connection

        _service, repository, revision_id = self._service_and_revision("pcs-brand-a")
        service = PointProcessingService(repository, PostgresPointProcessingCatalog())
        plan = self._plan(service, revision_id)
        original = repository._install_bindings

        def fail_after_bindings(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected failure")

        with patch.object(repository, "_install_bindings", side_effect=fail_after_bindings):
            with self.assertRaises(RuntimeError):
                service.apply(
                    ApplyPointProcessingPlan(
                        plan.id,
                        plan.digest,
                        "rollback",
                        "test:engineer",
                    )
                )
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_revision FROM t_configuration_state")
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_installed_point_processings")
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_entity_instances")
                self.assertEqual(0, cursor.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
