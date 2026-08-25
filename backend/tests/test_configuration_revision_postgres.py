from __future__ import annotations

import os
import unittest

import psycopg2

from tests import test_data_trunk_migration_postgres
from tests.test_node_data_trunk_hard_cut_migration_postgres import MIGRATION_044


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL configuration revision tests",
)
class ConfigurationRevisionPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Configuration revision tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        migration_test = (
            test_data_trunk_migration_postgres.DataTrunkMigrationPostgresTest
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration_test._reset_through_041(cursor)
                migration_test._apply_042(cursor)
                migration_test._apply_043(cursor)
                cursor.execute(MIGRATION_044.read_text(encoding="utf-8"))

    def test_publish_locks_base_and_appends_revision_and_audit(self) -> None:
        from app.services.configuration_revision_postgres import (
            PostgresConfigurationRevisions,
        )

        subject = PostgresConfigurationRevisions()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            revision = subject.publish(
                transaction=connection,
                base_revision=0,
                actor="user:engineer",
                action="point_processing.publish",
                resource_kind="node",
                resource_id="node-1",
                before_digest=None,
                after_digest="a" * 64,
                details={"template_revision_id": "revision-1"},
            )
            connection.commit()
        self.assertEqual(revision, 1)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_revision FROM t_configuration_state "
                    "WHERE singleton=TRUE"
                )
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute(
                    "SELECT action, resource_kind, resource_id, after_digest "
                    "FROM t_configuration_audit"
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (
                        "point_processing.publish",
                        "node",
                        "node-1",
                        "a" * 64,
                    ),
                )

    def test_stale_base_is_zero_write(self) -> None:
        from app.services.configuration_revision import ConfigurationRevisionError
        from app.services.configuration_revision_postgres import (
            PostgresConfigurationRevisions,
        )

        subject = PostgresConfigurationRevisions()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            subject.publish(
                transaction=connection,
                base_revision=0,
                actor="user:engineer",
                action="alarm_configuration.publish",
                resource_kind="alarm_rule_set",
                resource_id="alarm-1",
                before_digest=None,
                after_digest="b" * 64,
                details={},
            )
            connection.commit()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with self.assertRaisesRegex(
                ConfigurationRevisionError,
                "CONFIGURATION_REVISION_STALE",
            ):
                subject.publish(
                    transaction=connection,
                    base_revision=0,
                    actor="user:engineer",
                    action="jdm.update",
                    resource_kind="jdm_rule",
                    resource_id="rule-1",
                    before_digest="b" * 64,
                    after_digest="c" * 64,
                    details={},
                )
            connection.rollback()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_revision FROM t_configuration_state")
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT count(*) FROM t_configuration_audit")
                self.assertEqual(cursor.fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
