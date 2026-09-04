from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg2


MIGRATION_061 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_061_alarm_record_archiving.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run alarm record archive migration tests",
)
class AlarmRecordArchivingMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Alarm archive tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
                cursor.execute(
                    """
                    CREATE TABLE public.t_alarm_events (
                      id UUID PRIMARY KEY,
                      state TEXT NOT NULL,
                      recovered_at TIMESTAMPTZ
                    );
                    CREATE TABLE public.t_alarm_rule_sets (
                      id UUID PRIMARY KEY,
                      rule_set_key TEXT NOT NULL UNIQUE
                    );
                    """
                )

    def test_migration_is_replayable_and_preserves_archived_evidence(self) -> None:
        recovered_id = uuid4()
        active_id = uuid4()
        rule_set_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION_061.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_061.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO t_alarm_events(id,state,recovered_at) VALUES (%s,'recovered',now()),(%s,'active_unacknowledged',NULL)",
                    (recovered_id, active_id),
                )
                cursor.execute(
                    "UPDATE t_alarm_events SET archived_at=now(),archived_by='user:test' WHERE id=%s",
                    (recovered_id,),
                )
                cursor.execute(
                    "INSERT INTO t_alarm_rule_sets(id,rule_set_key,archived_at,archived_by) VALUES (%s,'test-rule',now(),'user:test')",
                    (rule_set_id,),
                )
                cursor.execute(
                    "SELECT state,archived_by FROM t_alarm_events WHERE id=%s",
                    (recovered_id,),
                )
                self.assertEqual(("recovered", "user:test"), cursor.fetchone())
                cursor.execute(
                    "SELECT rule_set_key,archived_by FROM t_alarm_rule_sets WHERE id=%s",
                    (rule_set_id,),
                )
                self.assertEqual(("test-rule", "user:test"), cursor.fetchone())
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    cursor.execute(
                        "UPDATE t_alarm_events SET archived_at=now(),archived_by='user:test' WHERE id=%s",
                        (active_id,),
                    )


if __name__ == "__main__":
    unittest.main()
