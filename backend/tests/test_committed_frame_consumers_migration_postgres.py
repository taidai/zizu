from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg2

from tests import test_frame_retention_migration_postgres as retention_migration


MIGRATION_049 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_049_committed_frame_consumers.sql"
)


class CommittedFrameConsumersMigrationSourceTest(unittest.TestCase):
    def test_049_declares_the_alarm_receipt_contract(self) -> None:
        sql = MIGRATION_049.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE public.t_committed_frame_consumers", sql)
        self.assertIn("PRIMARY KEY (consumer_key,frame_id)", sql)
        self.assertIn("uq_committed_frame_consumer_sequence", sql)
        self.assertIn("REFERENCES public.t_data_frames(frame_id)", sql)
        self.assertIn("SCHEMA_049_PARTIAL_STRUCTURE", sql)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run committed-frame consumer tests",
)
class CommittedFrameConsumersMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Committed-frame consumer tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        retention_migration.FrameRetentionMigrationPostgresTest.connection_kwargs = (
            cls.connection_kwargs
        )
        retention_migration.FrameRetentionMigrationPostgresTest.setUpClass()

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    def test_049_is_replayable_and_rejects_duplicate_consumer_sequence(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_049.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(MIGRATION_049.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(
                "SELECT current_revision FROM t_configuration_state "
                "WHERE singleton=TRUE"
            )
            configuration_revision = int(cursor.fetchone()[0])
            cursor.execute(
                "INSERT INTO t_data_frames"
                "(frame_id,candidate_digest,capture_beat,shot_at,configuration_revision,"
                " status,attempt_count,finished_at) "
                "VALUES(%s,%s,1,clock_timestamp(),%s,'COMPLETE',1,clock_timestamp()) "
                "RETURNING frame_id,frame_sequence",
                (str(uuid4()), "a" * 64, configuration_revision),
            )
            frame_id, sequence = cursor.fetchone()
            cursor.execute(
                "INSERT INTO t_committed_frame_consumers"
                "(consumer_key,frame_id,frame_sequence,configuration_revision) "
                "VALUES('alarm',%s,%s,%s)",
                (frame_id, sequence, configuration_revision),
            )
            cursor.execute(
                "INSERT INTO t_data_frames"
                "(frame_id,candidate_digest,capture_beat,shot_at,configuration_revision,"
                " status,attempt_count,finished_at) "
                "VALUES(%s,%s,2,clock_timestamp(),%s,'COMPLETE',1,clock_timestamp()) "
                "RETURNING frame_id",
                (str(uuid4()), "b" * 64, configuration_revision),
            )
            other_frame_id = cursor.fetchone()[0]
            with self.assertRaises(psycopg2.errors.UniqueViolation):
                cursor.execute(
                    "INSERT INTO t_committed_frame_consumers"
                    "(consumer_key,frame_id,frame_sequence,configuration_revision) "
                    "VALUES('alarm',%s,%s,%s)",
                    (other_frame_id, sequence, configuration_revision),
                )


if __name__ == "__main__":
    unittest.main()
