from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import unittest
from uuid import UUID, uuid4

import psycopg2

from tests import test_edge_storage_retention_migration_postgres as edge_retention
from tests import test_node_data_trunk_hard_cut_migration_postgres as hard_cut


MIGRATION_046 = (
    Path(__file__).resolve().parents[2] / "init-db" / "migration_046_data_frames.sql"
)
MIGRATION_053 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_053_frame_head_index.sql"
)
MIGRATION_054 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_054_l0_latest_accepted_beat.sql"
)
MIGRATION_055 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_055_l0_write_path_indexes.sql"
)
NOW = datetime(2026, 8, 27, tzinfo=UTC)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run data-frame migration tests",
)
class DataFramesMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Data-frame migration tests require a *_test database")
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
                cursor.execute("SELECT _timescaledb_functions.stop_background_workers()")
                try:
                    hard_cut.NodeDataTrunkHardCutMigrationPostgresTest._reset_through_043(cursor)
                    hard_cut.NodeDataTrunkHardCutMigrationPostgresTest._apply_044(cursor)
                    edge_retention.EdgeStorageRetentionMigrationPostgresTest._restore_timescale_001_footprint(cursor)
                    edge_retention.EdgeStorageRetentionMigrationPostgresTest._apply_045(cursor)
                finally:
                    cursor.execute(
                        "SELECT _timescaledb_functions.start_background_workers()"
                    )

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    @staticmethod
    def _apply_046(cursor) -> None:
        cursor.execute(MIGRATION_046.read_text(encoding="utf-8"))
        # The migration owns its transaction. Start the behavior transaction
        # explicitly so deferred terminal evidence can be written in either order.
        cursor.execute("BEGIN")
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    @staticmethod
    def _frame_columns(cursor, table: str) -> set[str]:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s",
            (table,),
        )
        return {row[0] for row in cursor.fetchall()}

    @staticmethod
    def _insert_pending_frame(cursor, *, capture_beat: int = 1) -> UUID:
        frame_id = uuid4()
        cursor.execute(
            "INSERT INTO t_data_frames"
            "(frame_id,candidate_digest,capture_beat,shot_at,"
            " configuration_revision,status) "
            "VALUES(%s,%s,%s,%s,0,'PENDING')",
            (str(frame_id), "a" * 64, capture_beat, NOW),
        )
        return frame_id

    @staticmethod
    def _claim(cursor, frame_id: UUID) -> tuple[UUID, UUID]:
        owner, token = uuid4(), uuid4()
        cursor.execute(
            "UPDATE t_data_frames SET status='PROCESSING',attempt_count=1,"
            "processing_owner=%s,processing_token=%s,lease_until=clock_timestamp()+interval '30 seconds' "
            "WHERE frame_id=%s",
            (str(owner), str(token), str(frame_id)),
        )
        return owner, token

    def test_046_installs_single_site_frame_contract(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            cursor.execute(
                "SELECT to_regclass('public.t_data_frames'),"
                "to_regclass('public.t_data_frame_outbox'),"
                "to_regclass('public.t_l2_stream_outbox')"
            )
            self.assertEqual(
                ("t_data_frames", "t_data_frame_outbox", None),
                cursor.fetchone(),
            )
            frame_columns = self._frame_columns(cursor, "t_data_frames")
            self.assertNotIn("site_id", frame_columns)
            self.assertTrue(
                {
                    "frame_id",
                    "frame_sequence",
                    "candidate_digest",
                    "capture_beat",
                    "configuration_revision",
                    "status",
                    "processing_owner",
                    "processing_token",
                }.issubset(frame_columns)
            )
            self.assertTrue(
                {"frame_id", "frame_sequence", "accepted_beat", "source_order_mode", "source_receive_ordinal"}
                .issubset(self._frame_columns(cursor, "t_telemetry"))
            )
            self.assertIn("frame_id", self._frame_columns(cursor, "t_l2_observations"))

    def test_046_refuses_unpublished_legacy_outbox_without_partial_writes(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_l2_stream_outbox(event_id,entity_instance_id,payload) "
                "VALUES(%s,%s,'{}'::jsonb)",
                (str(uuid4()), str(uuid4())),
            )
            cursor.execute("SET session_replication_role=origin")
            connection.commit()
            with self.assertRaisesRegex(
                psycopg2.Error, "SCHEMA_046_OUTBOX_NOT_DRAINED"
            ):
                self._apply_046(cursor)
            connection.rollback()
            cursor.execute("SELECT to_regclass('public.t_data_frames')")
            self.assertIsNone(cursor.fetchone()[0])

    def test_046_replays_and_rejects_mutated_constraint_without_repair(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            connection.commit()
            self._apply_046(cursor)
            connection.commit()
            cursor.execute(
                "ALTER TABLE t_data_frames DROP CONSTRAINT chk_data_frame_status"
            )
            connection.commit()
            with self.assertRaisesRegex(
                psycopg2.Error, "SCHEMA_046_PARTIAL_STRUCTURE"
            ):
                self._apply_046(cursor)
            connection.rollback()
            cursor.execute(
                "SELECT count(*) FROM pg_constraint WHERE conrelid='t_data_frames'::regclass "
                "AND conname='chk_data_frame_status'"
            )
            self.assertEqual(0, cursor.fetchone()[0])

    def test_046_marks_legacy_latest_zero_and_removes_new_sequence_defaults(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            node_id, tag_id = edge_retention.EdgeStorageRetentionMigrationPostgresTest._insert_node_and_tag(cursor)
            cursor.execute(
                "INSERT INTO t_telemetry_latest(node_id,tag_id,ts,value_float) "
                "VALUES(%s,%s,%s,1.0)",
                (str(node_id), str(tag_id), NOW),
            )
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_l2_latest"
                "(entity_instance_id,event_id,observed_at,received_at,calculated_at,value_float,quality,"
                " processing_revision_id,configuration_revision,source_digest,source_order_key,event_time_basis) "
                "VALUES(%s,%s,%s,%s,%s,1.0,192,%s,0,%s,'legacy','observed_at')",
                (str(uuid4()), str(uuid4()), NOW, NOW, NOW, str(uuid4()), "b" * 64),
            )
            cursor.execute("SET session_replication_role=origin")
            connection.commit()
            self._apply_046(cursor)
            cursor.execute("SELECT frame_sequence FROM t_telemetry_latest")
            self.assertEqual((0,), cursor.fetchone())
            cursor.execute("SELECT frame_sequence FROM t_l2_latest")
            self.assertEqual((0,), cursor.fetchone())
            cursor.execute(
                "SELECT table_name,column_default FROM information_schema.columns "
                "WHERE table_schema='public' AND (table_name,column_name) IN "
                "(('t_telemetry_latest','frame_sequence'),('t_l2_latest','frame_sequence'),"
                " ('t_l2_observations','commit_sequence')) ORDER BY table_name"
            )
            self.assertEqual(
                [("t_l2_latest", None), ("t_l2_observations", None), ("t_telemetry_latest", None)],
                cursor.fetchall(),
            )

    def test_046_framed_l0_requires_complete_order_evidence_but_legacy_remains_readable(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            frame_id = self._insert_pending_frame(cursor)
            node_id, tag_id = edge_retention.EdgeStorageRetentionMigrationPostgresTest._insert_node_and_tag(cursor)
            cursor.execute(
                "SELECT frame_sequence FROM t_data_frames WHERE frame_id=%s",
                (str(frame_id),),
            )
            sequence = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO t_telemetry(ts,node_id,tag_id,value_float) VALUES(%s,%s,%s,1.0)",
                (NOW, str(node_id), str(tag_id)),
            )
            with self.assertRaises(psycopg2.errors.CheckViolation):
                cursor.execute(
                    "INSERT INTO t_telemetry"
                    "(ts,node_id,tag_id,value_float,frame_id,frame_sequence) "
                    "VALUES(%s,%s,%s,2.0,%s,%s)",
                    (NOW + timedelta(seconds=1), str(node_id), str(tag_id), str(frame_id), sequence),
                )
            connection.rollback()

    def test_046_allows_stale_value_but_bad_must_be_empty(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            cursor.execute("SET session_replication_role=replica")
            values = (str(uuid4()), str(uuid4()), NOW, NOW, NOW, str(uuid4()))
            cursor.execute(
                "INSERT INTO t_l2_observations"
                "(entity_instance_id,event_id,observed_at,received_at,calculated_at,value_float,quality,reason,"
                " processing_revision_id,configuration_revision,source_digest,source_order_key,event_time_basis,commit_sequence) "
                "VALUES(%s,%s,%s,%s,%s,12.5,1,'SOURCE_STALE',%s,0,%s,'stale','observed_at',1)",
                (*values, "c" * 64),
            )
            with self.assertRaises(psycopg2.errors.CheckViolation):
                cursor.execute(
                    "INSERT INTO t_l2_observations"
                    "(entity_instance_id,event_id,observed_at,received_at,calculated_at,value_float,quality,reason,"
                    " processing_revision_id,configuration_revision,source_digest,source_order_key,event_time_basis,commit_sequence) "
                    "VALUES(%s,%s,%s,%s,%s,12.5,0,'SOURCE_BAD',%s,0,%s,'bad','observed_at',2)",
                    (str(uuid4()), str(uuid4()), NOW, NOW, NOW, str(uuid4()), "d" * 64),
                )
            connection.rollback()

    def test_046_rejects_illegal_transition_and_keeps_terminal_frame_immutable(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            frame_id = self._insert_pending_frame(cursor)
            with self.assertRaisesRegex(psycopg2.Error, "DATA_FRAME_TRANSITION_INVALID"):
                cursor.execute(
                    "UPDATE t_data_frames SET status='COMPLETE',finished_at=clock_timestamp() "
                    "WHERE frame_id=%s",
                    (str(frame_id),),
                )
            connection.rollback()

        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            frame_id = self._insert_pending_frame(cursor, capture_beat=2)
            self._claim(cursor, frame_id)
            cursor.execute(
                "SELECT frame_sequence FROM t_data_frames WHERE frame_id=%s",
                (str(frame_id),),
            )
            sequence = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO t_data_frame_outbox(frame_id,frame_sequence,terminal_status) "
                "VALUES(%s,%s,'COMPLETE')",
                (str(frame_id), sequence),
            )
            cursor.execute(
                "UPDATE t_data_frames SET status='COMPLETE',processing_owner=NULL,"
                "processing_token=NULL,lease_until=NULL,finished_at=clock_timestamp() WHERE frame_id=%s",
                (str(frame_id),),
            )
            connection.commit()
            with self.assertRaisesRegex(psycopg2.Error, "DATA_FRAME_TERMINAL_IMMUTABLE"):
                cursor.execute(
                    "UPDATE t_data_frames SET status='FAILED' WHERE frame_id=%s",
                    (str(frame_id),),
                )

    def test_046_failed_frame_requires_matching_failure_fact(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            frame_id = self._insert_pending_frame(cursor)
            self._claim(cursor, frame_id)
            cursor.execute(
                "SELECT frame_sequence FROM t_data_frames WHERE frame_id=%s",
                (str(frame_id),),
            )
            sequence = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO t_data_frame_outbox(frame_id,frame_sequence,terminal_status) "
                "VALUES(%s,%s,'FAILED')",
                (str(frame_id), sequence),
            )
            cursor.execute(
                "UPDATE t_data_frames SET status='FAILED',failure_code='FRAME_PROCESSING_FAILED',"
                "processing_owner=NULL,processing_token=NULL,lease_until=NULL,finished_at=clock_timestamp() "
                "WHERE frame_id=%s",
                (str(frame_id),),
            )
            with self.assertRaisesRegex(psycopg2.Error, "DATA_FRAME_FAILURE_EVIDENCE_INVALID"):
                connection.commit()

    def test_046_outbox_claim_is_all_null_or_all_present(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            frame_id = self._insert_pending_frame(cursor)
            self._claim(cursor, frame_id)
            cursor.execute(
                "SELECT frame_sequence FROM t_data_frames WHERE frame_id=%s",
                (str(frame_id),),
            )
            sequence = cursor.fetchone()[0]
            with self.assertRaises(psycopg2.errors.CheckViolation):
                cursor.execute(
                    "INSERT INTO t_data_frame_outbox"
                    "(frame_id,frame_sequence,terminal_status,claimed_by) "
                    "VALUES(%s,%s,'COMPLETE',%s)",
                    (str(frame_id), sequence, str(uuid4())),
                )

    def test_053_claims_oldest_unfinished_frame_without_scanning_terminal_history(
        self,
    ) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            connection.commit()
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                """
                INSERT INTO t_data_frames
                  (frame_id,candidate_digest,capture_beat,shot_at,
                   configuration_revision,status,finished_at)
                SELECT gen_random_uuid(),repeat(md5(item::text),2),item,
                       %s-make_interval(secs=>item),0,'COMPLETE',%s
                FROM generate_series(1,2000) AS item
                """,
                (NOW, NOW),
            )
            cursor.execute("SET session_replication_role=origin")
            self._insert_pending_frame(cursor, capture_beat=2001)
            connection.commit()

            cursor.execute(MIGRATION_053.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(MIGRATION_053.read_text(encoding="utf-8"))
            connection.commit()

            cursor.execute(
                """
                EXPLAIN (ANALYZE, FORMAT JSON)
                SELECT frame_id,frame_sequence,capture_beat,shot_at,
                       configuration_revision,attempt_count,processing_owner,
                       processing_token,lease_until,created_at,status
                FROM t_data_frames
                WHERE status IN ('PENDING','PROCESSING')
                ORDER BY frame_sequence
                LIMIT 1
                FOR UPDATE
                """
            )
            root = cursor.fetchone()[0][0]["Plan"]
            pending = [root]
            nodes: list[dict] = []
            while pending:
                node = pending.pop()
                nodes.append(node)
                pending.extend(node.get("Plans", []))
            scan = next(
                node
                for node in nodes
                if node.get("Index Name") == "ix_data_frames_claim"
            )
            self.assertEqual(0, scan.get("Rows Removed by Filter", 0))
            self.assertLess(scan["Actual Rows"], 2)

    def test_054_persists_true_accepted_beat_in_l0_latest(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            connection.commit()
            node_id, tag_id = (
                edge_retention.EdgeStorageRetentionMigrationPostgresTest
                ._insert_node_and_tag(cursor)
            )
            _, legacy_tag_id = (
                edge_retention.EdgeStorageRetentionMigrationPostgresTest
                ._insert_node_and_tag(cursor)
            )
            frame_id = self._insert_pending_frame(cursor, capture_beat=7)
            cursor.execute(
                "SELECT frame_sequence FROM t_data_frames WHERE frame_id=%s",
                (str(frame_id),),
            )
            frame_sequence = int(cursor.fetchone()[0])
            observation_id = uuid4()
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_telemetry"
                "(ts,node_id,tag_id,value_float,observation_id,frame_id,"
                " frame_sequence,accepted_beat,source_order_mode) "
                "VALUES(%s,%s,%s,1.0,%s,%s,%s,7,'sequence')",
                (
                    NOW,
                    str(node_id),
                    str(tag_id),
                    str(observation_id),
                    str(frame_id),
                    frame_sequence,
                ),
            )
            cursor.execute(
                "INSERT INTO t_telemetry_latest"
                "(node_id,tag_id,ts,value_float,observation_id,frame_sequence,"
                " source_order_mode) VALUES(%s,%s,%s,1.0,%s,%s,'sequence')",
                (
                    str(node_id),
                    str(tag_id),
                    NOW,
                    str(observation_id),
                    frame_sequence,
                ),
            )
            cursor.execute(
                "INSERT INTO t_telemetry_latest"
                "(node_id,tag_id,ts,value_float,frame_sequence) "
                "VALUES(%s,%s,%s,2.0,0)",
                (str(node_id), str(legacy_tag_id), NOW),
            )
            cursor.execute("SET session_replication_role=origin")
            connection.commit()

            cursor.execute(MIGRATION_054.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(MIGRATION_054.read_text(encoding="utf-8"))
            connection.commit()

            cursor.execute(
                "SELECT tag_id,accepted_beat FROM t_telemetry_latest "
                "WHERE tag_id IN (%s,%s) ORDER BY tag_id",
                (str(tag_id), str(legacy_tag_id)),
            )
            self.assertEqual(
                sorted([(tag_id, 7), (legacy_tag_id, 0)], key=lambda item: item[0]),
                [(UUID(str(row[0])), int(row[1])) for row in cursor.fetchall()],
            )
            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_telemetry_latest' "
                "AND column_name='accepted_beat'"
            )
            self.assertEqual(("NO",), cursor.fetchone())

    def test_054_rejects_pre_frame_schema_without_partial_ddl(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            with self.assertRaisesRegex(
                psycopg2.Error,
                "SCHEMA_054_REQUIRES_046",
            ):
                cursor.execute(MIGRATION_054.read_text(encoding="utf-8"))
            connection.rollback()
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_telemetry_latest' "
                "AND column_name='accepted_beat'"
            )
            self.assertIsNone(cursor.fetchone())

    def test_055_removes_only_redundant_l0_write_indexes(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply_046(cursor)
            connection.commit()

            cursor.execute(MIGRATION_055.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(MIGRATION_055.read_text(encoding="utf-8"))
            connection.commit()

            cursor.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='t_telemetry'"
            )
            telemetry_indexes = {row[0] for row in cursor.fetchall()}
            self.assertNotIn(
                "uq_telemetry_source_observation", telemetry_indexes
            )
            self.assertNotIn("idx_tel_node_tag", telemetry_indexes)
            self.assertIn("idx_tel_tag_ts", telemetry_indexes)
            self.assertIn(
                "ix_telemetry_observation_time", telemetry_indexes
            )
            self.assertIn(
                "ix_telemetry_tag_frame_sequence", telemetry_indexes
            )

            cursor.execute(
                "SELECT count(*) FROM pg_indexes "
                "WHERE schemaname='public' "
                "AND tablename='t_l0_observation_dedup' "
                "AND indexdef LIKE 'CREATE UNIQUE INDEX%%(source_digest)'"
            )
            self.assertEqual((1,), cursor.fetchone())


if __name__ == "__main__":
    unittest.main()
