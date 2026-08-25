from __future__ import annotations

import os
import unittest
from uuid import uuid4

import psycopg2

from app.api.rules import _publish_jdm_revision
from tests import test_node_data_trunk_hard_cut_migration_postgres


@unittest.skipUnless(os.environ.get("ZIZU_POSTGRES_TEST") == "1", "set ZIZU_POSTGRES_TEST=1")
class JdmConfigurationRevisionPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kwargs = {
            "host": os.environ["DB_HOST"], "port": int(os.environ["DB_PORT"]),
            "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def test_crud_publishes_one_revision_each(self) -> None:
        migration = test_node_data_trunk_hard_cut_migration_postgres.NodeDataTrunkHardCutMigrationPostgresTest
        with psycopg2.connect(**self.kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration._reset_through_043(cursor)
                migration._apply_044(cursor)
                cursor.execute("SELECT current_revision FROM t_configuration_state WHERE singleton=TRUE")
                base = int(cursor.fetchone()[0])
        rule_id = uuid4()
        for action, content in (
            ("jdm_rule.create", {"name": "charge-limit"}),
            ("jdm_rule.update", {"name": "charge-limit", "version": 2}),
            ("jdm_rule.delete", {"deleted": str(rule_id)}),
        ):
            with psycopg2.connect(**self.kwargs) as connection:
                _publish_jdm_revision(connection, actor="operator:test", action=action, rule_id=rule_id, content=content)
                connection.commit()
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_revision FROM t_configuration_state WHERE singleton=TRUE")
            self.assertEqual(cursor.fetchone()[0], base + 3)
            cursor.execute("SELECT action FROM t_configuration_audit WHERE resource_kind='jdm_rule' ORDER BY configuration_revision")
            self.assertEqual([row[0] for row in cursor.fetchall()], ["jdm_rule.create", "jdm_rule.update", "jdm_rule.delete"])


if __name__ == "__main__":
    unittest.main()
