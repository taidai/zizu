from __future__ import annotations

import os
from pathlib import Path
import asyncio
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg2

from tests import test_data_trunk_migration_postgres
from tests.test_node_data_trunk_hard_cut_migration_postgres import MIGRATION_044


MIGRATION_050 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_050_node_l0_usability.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL node usability tests",
)
class NodeUsabilityPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Node usability tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        migration = test_data_trunk_migration_postgres.DataTrunkMigrationPostgresTest
        self.root_id = str(uuid4())
        self.child_id = str(uuid4())
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration._reset_through_041(cursor)
                migration._apply_042(cursor)
                migration._apply_043(cursor)
                cursor.execute(MIGRATION_044.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled) VALUES (%s,'站点','SITE',TRUE)",
                    (self.root_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO t_nodes(id,name,node_type,parent_id,enabled)
                    VALUES (%s,'PCS','DEVICE',%s,TRUE)
                    """,
                    (self.child_id, self.root_id),
                )

    def test_schema_050_is_replayable_and_backfills_active_tree(self) -> None:
        sql = MIGRATION_050.read_text(encoding="utf-8")
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql)
                cursor.execute(sql)
                cursor.execute(
                    """
                    SELECT name,layer,retired_at,config,sort_order
                    FROM t_nodes ORDER BY layer
                    """
                )
                self.assertEqual(
                    [("站点", 1, None, {}, 0), ("PCS", 2, None, {}, 0)],
                    cursor.fetchall(),
                )

    def test_schema_050_enforces_one_source_identity_per_node(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION_050.read_text(encoding="utf-8"))
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id,node_id,name,data_type,source_type,source_path,enabled)
                    VALUES (%s,%s,'P1','INT','neuron','PCS/data/P',TRUE)
                    """,
                    (str(uuid4()), self.child_id),
                )
                with self.assertRaises(psycopg2.errors.UniqueViolation):
                    cursor.execute(
                        """
                        INSERT INTO t_tags
                          (id,node_id,name,data_type,source_type,source_path,enabled)
                        VALUES (%s,%s,'P2','INT','NEURON','PCS/data/P',TRUE)
                        """,
                        (str(uuid4()), self.child_id),
                    )

    def _repository(self):
        from app.services.node_tree_postgres import PostgresNodeTree

        return PostgresNodeTree(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )

    def _apply_050(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION_050.read_text(encoding="utf-8"))

    def test_create_and_move_compute_the_whole_subtree_layer(self) -> None:
        self._apply_050()
        repository = self._repository()
        second_root = repository.create(
            name="第二站点",
            node_type="SITE",
            parent_id=None,
            config={},
            sort_order=0,
            source_catalog_key=None,
            actor="user:engineer",
            base_revision=0,
        )
        moved = repository.update(
            node_id=self.root_id,
            changes={"parent_id": second_root["node"]["id"]},
            actor="user:engineer",
            base_revision=1,
        )

        self.assertEqual(2, moved["node"]["layer"])
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT layer FROM t_nodes WHERE id=%s", (self.child_id,))
                self.assertEqual(3, cursor.fetchone()[0])

    def test_move_rejects_a_descendant_parent_without_writes(self) -> None:
        self._apply_050()
        from app.services.node_tree_postgres import NodeTreeError

        repository = self._repository()
        with self.assertRaisesRegex(NodeTreeError, "NODE_TREE_CYCLE"):
            repository.update(
                node_id=self.root_id,
                changes={"parent_id": self.child_id},
                actor="user:engineer",
                base_revision=0,
            )
        self.assertEqual(0, repository.current_revision())

    def test_retire_keeps_evidence_and_removes_the_subtree_from_active_projection(self) -> None:
        self._apply_050()
        tag_id = str(uuid4())
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_tags(id,node_id,name,data_type,source_type,source_path,enabled)
                    VALUES (%s,%s,'P','INT','neuron','PCS/data/P',TRUE)
                    """,
                    (tag_id, self.child_id),
                )
            connection.commit()

        result = self._repository().retire(
            node_id=self.root_id,
            actor="user:engineer",
            base_revision=0,
        )

        self.assertEqual(2, result["retired_nodes"])
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM t_nodes WHERE retired_at IS NULL"
                )
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT enabled FROM t_tags WHERE id=%s", (tag_id,))
                self.assertFalse(cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_nodes")
                self.assertEqual(2, cursor.fetchone()[0])
                cursor.execute("SELECT current_revision FROM t_configuration_state")
                self.assertEqual(1, cursor.fetchone()[0])

    def test_raw_point_catalog_reads_hard_cut_l0_without_legacy_tag_columns(self) -> None:
        self._apply_050()
        from app.api.tags import list_tags
        from app.services.identity import Principal

        tag_id = str(uuid4())
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id,node_id,name,display_name,data_type,unit,source_type,
                       source_path,enabled)
                    VALUES (%s,%s,'ActivePower','有功功率','FLOAT','kW',
                            'neuron','PCS/data/ActivePower',TRUE)
                    """,
                    (tag_id, self.child_id),
                )
            connection.commit()

        principal = Principal(uuid4(), "engineer", "engineer", uuid4())
        with patch(
            "app.services.telemetry_store.get_connection",
            new=lambda: psycopg2.connect(**self.connection_kwargs),
        ):
            response = asyncio.run(list_tags(
                node_id=self.child_id,
                data_type=None,
                tag_type="PHYSICAL",
                read_write=None,
                search=None,
                enabled=True,
                include_disabled=False,
                page=1,
                page_size=50,
                sort_by="sort_order",
                sort_order="asc",
                principal=principal,
            ))

        self.assertNotIn("error", response)
        self.assertEqual(1, response["total"])
        self.assertEqual(tag_id, response["tags"][0]["id"])

    def test_raw_point_catalog_can_include_disabled_points_for_maintenance(self) -> None:
        self._apply_050()
        from app.api.tags import list_tags
        from app.services.identity import Principal

        enabled_id = str(uuid4())
        disabled_id = str(uuid4())
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id,node_id,name,data_type,source_type,source_path,enabled)
                    VALUES
                      (%s,%s,'P1','FLOAT','neuron','PCS/data/P1',TRUE),
                      (%s,%s,'P2','FLOAT','neuron','PCS/data/P2',FALSE)
                    """,
                    (enabled_id, self.child_id, disabled_id, self.child_id),
                )
            connection.commit()

        principal = Principal(uuid4(), "engineer", "engineer", uuid4())
        with patch(
            "app.services.telemetry_store.get_connection",
            new=lambda: psycopg2.connect(**self.connection_kwargs),
        ):
            response = asyncio.run(list_tags(
                node_id=self.child_id,
                data_type=None,
                tag_type="PHYSICAL",
                read_write=None,
                search=None,
                enabled=True,
                include_disabled=True,
                page=1,
                page_size=50,
                sort_by="sort_order",
                sort_order="asc",
                principal=principal,
            ))

        self.assertNotIn("error", response)
        self.assertEqual(2, response["total"])
        self.assertEqual({enabled_id, disabled_id}, {tag["id"] for tag in response["tags"]})


if __name__ == "__main__":
    unittest.main()
