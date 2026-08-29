"""PostgreSQL evidence for node-private immutable point-processing revisions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from uuid import UUID

import psycopg2

from tests import test_node_data_trunk_hard_cut_migration_postgres as migration


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_050 = ROOT / "init-db" / "migration_050_node_l0_usability.sql"
MIGRATION_051 = ROOT / "init-db" / "migration_051_node_private_point_processing.sql"
REFERENCE = ROOT / "reference-point-processings" / "pcs-brand-a.zizu-point-processing.json"
NODE_ID = UUID("93000000-0000-0000-0000-000000000001")


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL node-private processing tests",
)
class NodePrivatePointProcessingPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Node-private processing tests require a *_test database")
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
                migration.NodeDataTrunkHardCutMigrationPostgresTest._reset_through_043(cursor)
                migration.NodeDataTrunkHardCutMigrationPostgresTest._apply_044(cursor)
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled) "
                    "VALUES(%s,'PCS-PRIVATE','PCS',TRUE)",
                    (str(NODE_ID),),
                )
                cursor.execute(MIGRATION_050.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_051.read_text(encoding="utf-8"))
        from app.services.telemetry_store import init_db_pool

        init_db_pool(1, 4)

    def tearDown(self) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    @staticmethod
    def _raw() -> dict:
        return json.loads(REFERENCE.read_text(encoding="utf-8"))

    def test_schema_051_is_replayable_and_rejects_scope_without_matching_owner(self) -> None:
        sql = MIGRATION_051.read_text(encoding="utf-8")
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql)
                cursor.execute(
                    "SELECT reuse_scope,owner_node_id FROM t_point_processing_templates"
                )
                self.assertEqual([], cursor.fetchall())
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_templates
                          (id,asset_id,display_name,device_category,brand,model,status,
                           reuse_scope,owner_node_id)
                        VALUES(gen_random_uuid(),'bad','bad','PCS','x','x','active',
                               'node',NULL)
                        """
                    )

    def test_node_definition_is_owned_and_absent_from_shared_template_list(self) -> None:
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingTemplates,
        )

        templates = PostgresPointProcessingTemplates()
        registered = templates.import_node_definition(
            self._raw(),
            node_id=NODE_ID,
            actor="test:engineer",
        )

        self.assertEqual("node", registered.reuse_scope)
        self.assertEqual(NODE_ID, registered.owner_node_id)
        self.assertEqual((), PostgresPointProcessingCatalog().list_templates("PCS"))


if __name__ == "__main__":
    unittest.main()
