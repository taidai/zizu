"""Real PostgreSQL evidence for revision-bound JDM CRUD."""
from __future__ import annotations

from contextlib import contextmanager
import os
import unittest
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json, register_uuid

from tests import test_data_frames_postgres as frame_runtime
from tests.test_committed_l2_jdm_postgres import MIGRATION_052


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run JDM configuration tests",
)
class JdmConfigurationRevisionPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_uuid()
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("JDM configuration tests require a *_test database")
        cls.kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        frame_runtime.DataFramesPostgresTest.connection_kwargs = self.kwargs
        frame_runtime.DataFramesPostgresTest.setUpClass()
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_052.read_text(encoding="utf-8"))

    def _factory(self):
        kwargs = dict(self.kwargs)

        @contextmanager
        def factory():
            connection = psycopg2.connect(**kwargs)
            try:
                yield connection
            finally:
                connection.close()

        return factory

    @staticmethod
    def _content(source_id: UUID, target_id: UUID, threshold: int = 10) -> dict:
        return {
            "when": f"power > {threshold}",
            "_config": {
                "sourceEntityInstanceIds": [str(source_id)],
                "inputMappings": {"power": str(source_id)},
                "actions": [
                    {
                        "id": "set-limit",
                        "type": "control",
                        "entity_instance_id": str(target_id),
                        "value": 5,
                    }
                ],
            },
        }

    def test_crud_binds_each_row_version_to_one_published_revision(self) -> None:
        from app.services.jdm_postgres import PostgresJdmRules

        source_id = uuid4()
        target_id = uuid4()
        repository = PostgresJdmRules(connection_factory=self._factory())

        created = repository.create(
            name="charge limit",
            rule_type="control",
            jdm_content=self._content(source_id, target_id),
            enabled=True,
            references=(),
            actor="operator:test",
            base_revision=0,
        )
        updated = repository.update(
            rule_id=created["id"],
            changes={"jdm_content": self._content(source_id, target_id, 20)},
            references=(),
            actor="operator:test",
            base_revision=1,
        )
        deleted = repository.delete(
            rule_id=created["id"],
            actor="operator:test",
            base_revision=2,
        )

        self.assertEqual(1, created["configuration_revision"])
        self.assertEqual(2, updated["configuration_revision"])
        self.assertEqual(2, updated["version"])
        self.assertEqual(3, deleted["configuration_revision"])
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_revision FROM t_configuration_state")
            self.assertEqual(3, cursor.fetchone()[0])
            cursor.execute(
                "SELECT action FROM t_configuration_audit "
                "WHERE resource_kind='jdm_rule' ORDER BY configuration_revision"
            )
            self.assertEqual(
                ["jdm_rule.create", "jdm_rule.update", "jdm_rule.delete"],
                [row[0] for row in cursor.fetchall()],
            )
            cursor.execute("SELECT count(*) FROM t_rules WHERE id=%s", (created["id"],))
            self.assertEqual(0, cursor.fetchone()[0])

    def test_stale_revision_rolls_back_without_a_rule_row(self) -> None:
        from app.services.configuration_revision import ConfigurationRevisionError
        from app.services.jdm_postgres import PostgresJdmRules

        repository = PostgresJdmRules(connection_factory=self._factory())
        with self.assertRaises(ConfigurationRevisionError):
            repository.create(
                name="stale rule",
                rule_type="control",
                jdm_content=self._content(uuid4(), uuid4()),
                enabled=True,
                references=(),
                actor="operator:test",
                base_revision=9,
            )

        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_rules WHERE name='stale rule'")
            self.assertEqual(0, cursor.fetchone()[0])
            cursor.execute("SELECT current_revision FROM t_configuration_state")
            self.assertEqual(0, cursor.fetchone()[0])

    def test_legacy_rule_row_is_read_only(self) -> None:
        from app.services.jdm_postgres import JdmRuleError, PostgresJdmRules

        rule_id = uuid4()
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_rules
                  (id,name,rule_type,jdm_content,enabled,configuration_revision)
                VALUES(%s,'legacy alarm','alarm',%s,TRUE,0)
                """,
                (rule_id, Json({"when": "power > 10"})),
            )

        repository = PostgresJdmRules(connection_factory=self._factory())
        with self.assertRaisesRegex(JdmRuleError, "JDM_RULE_LEGACY_READ_ONLY"):
            repository.update(
                rule_id=rule_id,
                changes={"name": "cannot change"},
                references=None,
                actor="operator:test",
                base_revision=0,
            )


if __name__ == "__main__":
    unittest.main()
