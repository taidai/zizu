from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg2

from tests import test_data_frames_postgres as frame_runtime


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_062 = ROOT / "init-db" / "migration_062_dispatch_strategies.sql"


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run dispatch-strategy migration tests",
)
class DispatchStrategyMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Dispatch-strategy tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        frame_runtime.DataFramesPostgresTest.connection_kwargs = self.connection_kwargs
        frame_runtime.DataFramesPostgresTest.setUpClass()
        with self._connection() as connection, connection.cursor() as cursor:
            for version in (52, 53, 55, 56, 57, 58, 60, 61):
                migration = next((ROOT / "init-db").glob(f"migration_{version:03d}_*.sql"))
                cursor.execute(migration.read_text(encoding="utf-8"))
            connection.commit()

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    def test_migration_is_replayable_and_installs_the_complete_contract(self) -> None:
        sql = MIGRATION_062.read_text(encoding="utf-8")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(sql)
            connection.commit()
            cursor.execute(sql)
            connection.commit()

            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=ANY(%s)",
                (
                    [
                        "t_dispatch_strategies",
                        "t_dispatch_strategy_revisions",
                        "t_dispatch_strategy_bindings",
                        "t_dispatch_strategy_owners",
                        "t_dispatch_control_intents",
                        "t_dispatch_strategy_events",
                    ],
                ),
            )
            self.assertEqual(
                {
                    "t_dispatch_strategies",
                    "t_dispatch_strategy_revisions",
                    "t_dispatch_strategy_bindings",
                    "t_dispatch_strategy_owners",
                    "t_dispatch_control_intents",
                    "t_dispatch_strategy_events",
                },
                {row[0] for row in cursor.fetchall()},
            )

            cursor.execute(
                "SELECT 1 FROM timescaledb_information.hypertables "
                "WHERE hypertable_schema='public' "
                "AND hypertable_name='t_dispatch_strategy_events'"
            )
            self.assertEqual((1,), cursor.fetchone())

            cursor.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='public.t_control_commands'::regclass "
                "AND contype='c' AND pg_get_constraintdef(oid) LIKE '%source_type%'"
            )
            source_contract = " ".join(row[0] for row in cursor.fetchall())
            self.assertIn("strategy", source_contract)

            cursor.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='public.t_dispatch_control_intents'::regclass "
                "AND contype='c' AND pg_get_constraintdef(oid) LIKE '%status%'"
            )
            intent_contract = " ".join(row[0] for row in cursor.fetchall())
            for status in ("PENDING", "IN_FLIGHT", "CONFIRMED", "CANCELLED", "FAILED"):
                self.assertIn(status, intent_contract)

            cursor.execute(
                "SELECT indisprimary FROM pg_index "
                "WHERE indrelid='public.t_dispatch_strategy_owners'::regclass"
            )
            self.assertIn((True,), cursor.fetchall())

    def test_only_one_draft_revision_can_exist_per_strategy(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_062.read_text(encoding="utf-8"))
            cursor.execute("SELECT current_revision FROM t_configuration_state")
            configuration_revision = int(cursor.fetchone()[0])
            strategy_id = uuid4()
            cursor.execute(
                "INSERT INTO t_dispatch_strategies"
                "(id,name,created_by,updated_by) VALUES(%s,'test','test','test')",
                (strategy_id,),
            )
            cursor.execute(
                "INSERT INTO t_dispatch_strategy_revisions"
                "(id,strategy_id,revision,lifecycle,trigger_kind,site_timezone,"
                "jdm_content,content_digest,base_configuration_revision,created_by) "
                "VALUES(%s,%s,1,'DRAFT','DATA_CHANGE','Asia/Shanghai','{}',%s,%s,'test')",
                (uuid4(), strategy_id, "a" * 64, configuration_revision),
            )
            cursor.execute("SAVEPOINT before_duplicate_draft")
            with self.assertRaises(psycopg2.errors.UniqueViolation):
                cursor.execute(
                    "INSERT INTO t_dispatch_strategy_revisions"
                    "(id,strategy_id,revision,lifecycle,trigger_kind,site_timezone,"
                    "jdm_content,content_digest,base_configuration_revision,created_by) "
                    "VALUES(%s,%s,2,'DRAFT','FIXED_TICK','Asia/Shanghai','{}',%s,%s,'test')",
                    (uuid4(), strategy_id, "b" * 64, configuration_revision),
                )
            cursor.execute("ROLLBACK TO SAVEPOINT before_duplicate_draft")


if __name__ == "__main__":
    unittest.main()
