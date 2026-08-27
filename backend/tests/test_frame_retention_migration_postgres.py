from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import unittest
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json

from tests import test_data_frames_migration_postgres as frame_migration
from tests import test_committed_frame_payload_migration_postgres as payload_migration


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
MIGRATION_048 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_048_frame_retention.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run frame-retention tests",
)
class FrameRetentionMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Frame retention tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        with cls._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS zizu_internal CASCADE")
            connection.commit()
        migration_test = frame_migration.DataFramesMigrationPostgresTest
        migration_test.connection_kwargs = cls.connection_kwargs
        migration_test().setUp()
        with cls._connection() as connection, connection.cursor() as cursor:
            migration_test._apply_046(cursor)
            payload_migration.CommittedFramePayloadMigrationPostgresTest._apply_047(
                cursor
            )
            cursor.execute(MIGRATION_048.read_text(encoding="utf-8"))
            connection.commit()

    def setUp(self) -> None:
        self._next_beat = 1
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            for table in (
                "t_data_frame_outbox",
                "t_l2_observation_sources",
                "t_l2_latest",
                "t_l2_observations",
                "t_ingestion_failures",
                "t_telemetry_latest",
                "t_telemetry",
                "t_l0_observation_dedup",
                "t_data_frames",
            ):
                cursor.execute(f"DELETE FROM {table}")
            cursor.execute("SET session_replication_role=origin")

    @classmethod
    def _connection(cls):
        return psycopg2.connect(**cls.connection_kwargs)

    def _insert_outbox(
        self,
        cursor,
        *,
        age: timedelta,
        published: bool,
        claimed: bool = False,
        status: str = "COMPLETE",
    ) -> tuple[UUID, int]:
        frame_id = uuid4()
        created_at = NOW - age
        capture_beat = self._next_beat
        self._next_beat += 1
        cursor.execute("SET session_replication_role=replica")
        cursor.execute(
            "INSERT INTO t_data_frames"
            "(frame_id,candidate_digest,capture_beat,shot_at,configuration_revision,"
            " status,attempt_count,finished_at,created_at,failure_code) "
            "VALUES(%s,%s,%s,%s,0,%s,1,%s,%s,%s) RETURNING frame_sequence",
            (
                str(frame_id),
                "a" * 64,
                capture_beat,
                created_at,
                status,
                created_at,
                created_at,
                None if status == "COMPLETE" else "FRAME_PROCESSING_FAILED",
            ),
        )
        sequence = int(cursor.fetchone()[0])
        payload = {
            "type": "frame_delta",
            "frame_id": str(frame_id),
            "frame_sequence": sequence,
            "status": status,
            "frame_time": created_at.isoformat(),
            "configuration_revision": 0,
            "l0_changes": [],
            "l2_changes": [],
            "failure": None,
        }
        claim_id = uuid4() if claimed else None
        cursor.execute(
            "INSERT INTO t_data_frame_outbox"
            "(frame_id,frame_sequence,terminal_status,created_at,published_at,"
            " claimed_by,claim_token,claimed_until,payload_version,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s)",
            (
                str(frame_id),
                sequence,
                status,
                created_at,
                created_at if published else None,
                str(claim_id) if claim_id is not None else None,
                str(uuid4()) if claimed else None,
                NOW + timedelta(minutes=5) if claimed else None,
                Json(payload),
            ),
        )
        cursor.execute("SET session_replication_role=origin")
        return frame_id, sequence

    @staticmethod
    def _call_prune(cursor) -> None:
        cursor.execute(
            "CALL public.prune_committed_frame_history(NULL,%s)",
            (Json({"now": NOW.isoformat()}),),
        )

    def test_048_is_replayable_and_installs_aggregates_and_one_job(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_048.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(
                "SELECT count(*) FROM timescaledb_information.continuous_aggregates "
                "WHERE view_schema='public' AND view_name IN ('l2_agg_1h','l2_agg_1d')"
            )
            self.assertEqual((2,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM timescaledb_information.jobs "
                "WHERE proc_schema='public' "
                "AND proc_name='prune_committed_frame_history'"
            )
            self.assertEqual((1,), cursor.fetchone())
            cursor.execute(
                "SELECT prosecdef,proconfig,COALESCE(proacl::text,'') "
                "FROM pg_proc WHERE oid="
                "'public.prune_committed_frame_history(integer,jsonb)'::regprocedure"
            )
            security_definer, settings_value, acl = cursor.fetchone()
            self.assertTrue(security_definer)
            self.assertIn("search_path=pg_catalog, public", settings_value)
            self.assertFalse(
                any(item.startswith("=") for item in acl.strip("{}").split(","))
            )
            cursor.execute(
                "SELECT COALESCE(nspacl::text,'') FROM pg_namespace "
                "WHERE nspname='zizu_internal'"
            )
            schema_acl = cursor.fetchone()[0]
            self.assertFalse(
                any(
                    item.startswith("=")
                    for item in schema_acl.strip("{}").split(",")
                )
            )

    def test_048_prunes_only_old_published_outbox(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            _old_id, old_sequence = self._insert_outbox(
                cursor, age=timedelta(minutes=61), published=True
            )
            _recent_id, recent_sequence = self._insert_outbox(
                cursor, age=timedelta(minutes=59), published=True
            )
            _pending_id, pending_sequence = self._insert_outbox(
                cursor, age=timedelta(days=8), published=False, claimed=True
            )
            self._call_prune(cursor)
            cursor.execute(
                "SELECT frame_sequence FROM t_data_frame_outbox ORDER BY frame_sequence"
            )
            self.assertEqual(
                [recent_sequence, pending_sequence],
                [int(row[0]) for row in cursor.fetchall()],
            )
            self.assertNotEqual(old_sequence, recent_sequence)

    def test_048_keeps_normal_frame_fencing_closed(self) -> None:
        frame_id, owner, token = uuid4(), uuid4(), uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_data_frames"
                "(frame_id,candidate_digest,capture_beat,shot_at,"
                " configuration_revision,status) "
                "VALUES(%s,%s,900,%s,0,'PENDING')",
                (str(frame_id), "e" * 64, NOW),
            )
            cursor.execute(
                "UPDATE t_data_frames SET status='PROCESSING',attempt_count=1,"
                "processing_owner=%s,processing_token=%s,"
                "lease_until=clock_timestamp()+interval '30 seconds' "
                "WHERE frame_id=%s",
                (str(owner), str(token), str(frame_id)),
            )
            self.assertEqual(1, cursor.rowcount)
            with self.assertRaisesRegex(
                psycopg2.Error, "DATA_FRAME_TERMINAL_IMMUTABLE"
            ):
                cursor.execute(
                    "DELETE FROM t_data_frames WHERE frame_id=%s",
                    (str(frame_id),),
                )
            connection.rollback()

    def test_048_preserves_failure_frame_but_removes_unreferenced_old_frame(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            failed_id, _ = self._insert_outbox(
                cursor,
                age=timedelta(days=8),
                published=True,
                status="FAILED",
            )
            removable_id, _ = self._insert_outbox(
                cursor,
                age=timedelta(days=8),
                published=True,
            )
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_ingestion_failures"
                "(id,source_digest,stage,safe_summary,attempts,frame_id) "
                "VALUES(%s,%s,'frame',%s,1,%s)",
                (
                    str(uuid4()),
                    "a" * 64,
                    Json({"code": "FRAME_PROCESSING_FAILED"}),
                    str(failed_id),
                ),
            )
            cursor.execute("SET session_replication_role=origin")
            self._call_prune(cursor)
            cursor.execute(
                "SELECT frame_id FROM t_data_frames WHERE frame_id=ANY(%s::uuid[]) ",
                ([str(failed_id), str(removable_id)],),
            )
            self.assertEqual({failed_id}, {UUID(str(row[0])) for row in cursor.fetchall()})

    def test_048_keeps_latest_projection_while_old_l2_history_expires(self) -> None:
        node_id, entity_id, event_id = uuid4(), uuid4(), uuid4()
        revision_id, runtime_id = uuid4(), uuid4()
        observed_at = NOW - timedelta(days=8)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_nodes(id,name,node_type,enabled) "
                "VALUES(%s,%s,'DEVICE',TRUE)",
                (str(node_id), f"retention-{node_id}"),
            )
            cursor.execute(
                "INSERT INTO t_entity_instances"
                "(id,node_id,definition_id,display_name,data_type,unit,direction,"
                " freshness_seconds,active,source_kind) "
                "VALUES(%s,%s,'test.power','Power','FLOAT','kW','R',30,TRUE,"
                " 'point_processing')",
                (str(entity_id), str(node_id)),
            )
            common = (
                observed_at,
                str(event_id),
                str(entity_id),
                observed_at,
                observed_at,
                12.5,
                192,
                str(revision_id),
                "b" * 64,
                str(runtime_id),
            )
            cursor.execute(
                "INSERT INTO t_l2_observations"
                "(observed_at,event_id,entity_instance_id,received_at,calculated_at,"
                " value_float,quality,processing_revision_id,configuration_revision,"
                " source_digest,source_order_key,producing_runtime_instance_id,"
                " event_time_basis,commit_sequence) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,%s,'test',%s,'received_at',1)",
                common,
            )
            cursor.execute(
                "INSERT INTO t_l2_latest"
                "(entity_instance_id,event_id,observed_at,received_at,calculated_at,"
                " value_float,quality,processing_revision_id,configuration_revision,"
                " source_digest,source_order_key,producing_runtime_instance_id,"
                " event_time_basis,frame_sequence) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,%s,'test',%s,'received_at',1)",
                (
                    str(entity_id),
                    str(event_id),
                    observed_at,
                    observed_at,
                    observed_at,
                    12.5,
                    192,
                    str(revision_id),
                    "b" * 64,
                    str(runtime_id),
                ),
            )
            cursor.execute("SET session_replication_role=origin")
            self._call_prune(cursor)
            cursor.execute(
                "SELECT count(*) FROM t_l2_observations WHERE event_id=%s",
                (str(event_id),),
            )
            self.assertEqual((0,), cursor.fetchone())
            cursor.execute(
                "SELECT value_float FROM t_l2_latest WHERE entity_instance_id=%s",
                (str(entity_id),),
            )
            self.assertEqual((12.5,), cursor.fetchone())

    def test_048_materializes_numeric_and_discrete_l2_aggregates(self) -> None:
        numeric_entity, text_entity = uuid4(), uuid4()
        revision_id, runtime_id = uuid4(), uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            for offset, numeric, text in ((-30, 10.0, "OFF"), (-20, 20.0, "ON")):
                observed_at = NOW + timedelta(minutes=offset)
                cursor.execute(
                    "INSERT INTO t_l2_observations"
                    "(observed_at,event_id,entity_instance_id,received_at,calculated_at,"
                    " value_float,quality,processing_revision_id,configuration_revision,"
                    " source_digest,source_order_key,producing_runtime_instance_id,"
                    " event_time_basis,commit_sequence) "
                    "VALUES(%s,%s,%s,%s,%s,%s,192,%s,0,%s,%s,%s,'received_at',1)",
                    (
                        observed_at,
                        str(uuid4()),
                        str(numeric_entity),
                        observed_at,
                        observed_at,
                        numeric,
                        str(revision_id),
                        "c" * 64,
                        f"numeric-{offset}",
                        str(runtime_id),
                    ),
                )
                cursor.execute(
                    "INSERT INTO t_l2_observations"
                    "(observed_at,event_id,entity_instance_id,received_at,calculated_at,"
                    " value_text,quality,processing_revision_id,configuration_revision,"
                    " source_digest,source_order_key,producing_runtime_instance_id,"
                    " event_time_basis,commit_sequence) "
                    "VALUES(%s,%s,%s,%s,%s,%s,192,%s,0,%s,%s,%s,'received_at',1)",
                    (
                        observed_at,
                        str(uuid4()),
                        str(text_entity),
                        observed_at,
                        observed_at,
                        text,
                        str(revision_id),
                        "d" * 64,
                        f"text-{offset}",
                        str(runtime_id),
                    ),
                )
            cursor.execute("SET session_replication_role=origin")
            connection.commit()
        refresh_connection = self._connection()
        try:
            refresh_connection.autocommit = True
            with refresh_connection.cursor() as cursor:
                cursor.execute(
                    "CALL refresh_continuous_aggregate('public.l2_agg_1h',%s,%s)",
                    (NOW - timedelta(days=1), NOW + timedelta(days=1)),
                )
                cursor.execute(
                    "SELECT numeric_min,numeric_max,numeric_avg FROM l2_agg_1h "
                    "WHERE entity_instance_id=%s",
                    (str(numeric_entity),),
                )
                self.assertEqual((10.0, 20.0, 15.0), cursor.fetchone())
                cursor.execute(
                    "SELECT text_first,text_last FROM l2_agg_1h "
                    "WHERE entity_instance_id=%s",
                    (str(text_entity),),
                )
                self.assertEqual(("OFF", "ON"), cursor.fetchone())
        finally:
            refresh_connection.close()


if __name__ == "__main__":
    unittest.main()
