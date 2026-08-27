from __future__ import annotations

import os
from pathlib import Path
import unittest
from datetime import UTC, datetime
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

from tests import test_data_frames_migration_postgres as frame_migration


MIGRATION_047 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_047_committed_frame_payload.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run frame-payload migration tests",
)
class CommittedFramePayloadMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Frame payload tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        migration_test = frame_migration.DataFramesMigrationPostgresTest
        migration_test.connection_kwargs = self.connection_kwargs
        migration_test().setUp()
        with self._connection() as connection, connection.cursor() as cursor:
            migration_test._apply_046(cursor)
            connection.commit()

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    @staticmethod
    def _apply_047(cursor) -> None:
        cursor.execute(MIGRATION_047.read_text(encoding="utf-8"))

    def test_047_installs_versioned_immutable_payload(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_047(cursor)
            connection.commit()
            cursor.execute(
                "SELECT column_name,is_nullable,column_default "
                "FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='t_data_frame_outbox' "
                "AND column_name IN ('payload_version','payload') "
                "ORDER BY column_name"
            )
            self.assertEqual(
                [("payload", "NO", None), ("payload_version", "NO", None)],
                cursor.fetchall(),
            )
            cursor.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname='public' "
                "AND indexname='ix_data_frame_outbox_replay'"
            )
            self.assertEqual((1,), cursor.fetchone())
            self._apply_047(cursor)
            connection.commit()

    def test_047_rejects_existing_outbox_before_partial_ddl(self) -> None:
        migration_test = frame_migration.DataFramesMigrationPostgresTest
        with self._connection() as connection, connection.cursor() as cursor:
            frame_id = migration_test._insert_pending_frame(cursor)
            migration_test._claim(cursor, frame_id)
            cursor.execute(
                "SELECT frame_sequence FROM t_data_frames WHERE frame_id=%s",
                (str(frame_id),),
            )
            sequence = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO t_data_frame_outbox"
                "(frame_id,frame_sequence,terminal_status) VALUES(%s,%s,'COMPLETE')",
                (str(frame_id), sequence),
            )
            cursor.execute(
                "UPDATE t_data_frames SET status='COMPLETE',processing_owner=NULL,"
                "processing_token=NULL,lease_until=NULL,finished_at=clock_timestamp() "
                "WHERE frame_id=%s",
                (str(frame_id),),
            )
            connection.commit()

            with self.assertRaisesRegex(psycopg2.Error, "SCHEMA_047_OUTBOX_NOT_EMPTY"):
                self._apply_047(cursor)
            connection.rollback()
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_data_frame_outbox' "
                "AND column_name IN ('payload_version','payload')"
            )
            self.assertEqual((0,), cursor.fetchone())

    def test_047_allows_delivery_ack_but_rejects_payload_mutation(self) -> None:
        migration_test = frame_migration.DataFramesMigrationPostgresTest
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_047(cursor)
            frame_id = migration_test._insert_pending_frame(cursor)
            migration_test._claim(cursor, frame_id)
            cursor.execute(
                "SELECT frame_sequence FROM t_data_frames WHERE frame_id=%s",
                (str(frame_id),),
            )
            sequence = cursor.fetchone()[0]
            payload = {
                "type": "frame_delta",
                "frame_id": str(frame_id),
                "frame_sequence": sequence,
                "status": "COMPLETE",
                "frame_time": datetime.now(UTC).isoformat(),
                "configuration_revision": 0,
                "l0_changes": [],
                "l2_changes": [],
                "failure": None,
            }
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "UPDATE t_data_frames SET status='COMPLETE',processing_owner=NULL,"
                "processing_token=NULL,lease_until=NULL,finished_at=clock_timestamp() "
                "WHERE frame_id=%s",
                (str(frame_id),),
            )
            cursor.execute(
                "INSERT INTO t_data_frame_outbox"
                "(frame_id,frame_sequence,terminal_status,payload_version,payload) "
                "VALUES(%s,%s,'COMPLETE',1,%s::jsonb)",
                (str(frame_id), sequence, Json(payload)),
            )
            cursor.execute("SET session_replication_role=origin")
            connection.commit()

            cursor.execute(
                "UPDATE t_data_frame_outbox SET published_at=clock_timestamp() "
                "WHERE frame_id=%s",
                (str(frame_id),),
            )
            connection.commit()
            with self.assertRaisesRegex(
                psycopg2.Error, "DATA_FRAME_OUTBOX_PAYLOAD_IMMUTABLE"
            ):
                cursor.execute(
                    "UPDATE t_data_frame_outbox SET payload=payload||'{\"x\":1}'::jsonb "
                    "WHERE frame_id=%s",
                    (str(frame_id),),
                )
            connection.rollback()


if __name__ == "__main__":
    unittest.main()
