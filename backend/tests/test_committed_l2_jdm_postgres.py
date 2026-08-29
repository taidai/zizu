"""PostgreSQL evidence for committed-L2 JDM schema and transactions."""
from __future__ import annotations

import os
from pathlib import Path
import unittest

import psycopg2

from tests import test_data_frames_postgres as frame_runtime


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_052 = ROOT / "init-db" / "migration_052_committed_l2_jdm.sql"


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run committed-L2 JDM PostgreSQL tests",
)
class JdmSchemaPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Committed-L2 JDM tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        frame_runtime.DataFramesPostgresTest.connection_kwargs = (
            self.connection_kwargs
        )
        frame_runtime.DataFramesPostgresTest.setUpClass()

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    def test_schema_052_is_replayable_and_complete(self) -> None:
        sql = MIGRATION_052.read_text(encoding="utf-8")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(sql)
            connection.commit()
            cursor.execute(sql)
            connection.commit()

            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_rules' "
                "AND column_name='configuration_revision'"
            )
            self.assertEqual(("NO",), cursor.fetchone())

            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_jdm_executions'"
            )
            self.assertEqual(
                {
                    "id",
                    "rule_id",
                    "rule_version",
                    "frame_id",
                    "frame_sequence",
                    "configuration_revision",
                    "model_digest",
                    "status",
                    "reason_code",
                    "inputs",
                    "outputs",
                    "actions",
                    "executed_at",
                },
                {row[0] for row in cursor.fetchall()},
            )

            cursor.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='public.t_jdm_executions'::regclass"
            )
            self.assertTrue(
                {
                    "t_jdm_executions_pkey",
                    "uq_jdm_execution_rule_frame",
                    "fk_jdm_execution_frame",
                    "fk_jdm_execution_configuration_revision",
                    "chk_jdm_execution_rule_version",
                    "chk_jdm_execution_frame_sequence",
                    "chk_jdm_execution_model_digest",
                    "chk_jdm_execution_status",
                    "chk_jdm_execution_reason",
                }.issubset({row[0] for row in cursor.fetchall()})
            )

    def test_schema_052_rejects_an_unknown_partial_execution_table(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE public.t_jdm_executions "
                "(id UUID PRIMARY KEY, status TEXT NOT NULL)"
            )
            connection.commit()
            with self.assertRaisesRegex(
                psycopg2.DatabaseError,
                "SCHEMA_052_PARTIAL_STRUCTURE",
            ):
                cursor.execute(MIGRATION_052.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
