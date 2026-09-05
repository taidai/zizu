from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import unittest
from uuid import uuid4

import psycopg2

from app.services.data_trunk_contracts import DataTrunkError, FrameStatus, TrunkQuality
from app.services.data_trunk_outbox import build_frame_outbox_event


@unittest.skipUnless(os.environ.get("ZIZU_POSTGRES_TEST") == "1", "requires isolated PostgreSQL")
class FrameOutboxLatestPostgresTest(unittest.TestCase):
    """Exercise transaction B's real SQL, with private tables and no site data."""

    def setUp(self):
        if not os.environ.get("DB_NAME", "").endswith("_test"):
            raise RuntimeError("isolated *_test database required")
        self.connection = psycopg2.connect(
            host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
            dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
        )
        self.addCleanup(self.connection.close)
        self.now = datetime(2026, 9, 5, tzinfo=UTC)
        self.frame_id, self.entity_id, self.event_id = uuid4(), uuid4(), uuid4()
        self.node_id, self.revision_id = uuid4(), uuid4()
        with self.connection.cursor() as cursor:
            for table in ("t_l2_observations", "t_l2_latest", "t_entity_instances",
                          "t_telemetry_latest", "t_tags"):
                cursor.execute(f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP")
            cursor.execute(
                "INSERT INTO t_entity_instances(id,node_id,definition_id,display_name,data_type,direction,"
                "freshness_seconds,active,source_kind,created_at,updated_at) "
                "VALUES(%s,%s,'test.status','Status','BOOL','R',10,TRUE,'point_processing',%s,%s)",
                (str(self.entity_id), str(self.node_id), self.now, self.now),
            )

    def _seed_current(self, *, quality=0, value=None, retained=False):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_l2_observations(observed_at,event_id,entity_instance_id,received_at,"
                "calculated_at,value_bool,quality,reason,processing_revision_id,configuration_revision,"
                "source_digest,source_order_key,producing_runtime_instance_id,event_time_basis,"
                "frame_id,commit_sequence) VALUES(%s,%s,%s,%s,%s,%s,%s,'TEST',%s,1,%s,'test',%s,"
                "'received_at',%s,20)",
                (self.now, str(self.event_id), str(self.entity_id), self.now, self.now, value,
                 quality, str(self.revision_id), "a"*64, str(uuid4()), str(self.frame_id)),
            )
            cursor.execute(
                "INSERT INTO t_l2_latest(entity_instance_id,event_id,observed_at,received_at,calculated_at,"
                "value_bool,value_observed_at,quality,reason,processing_revision_id,configuration_revision,"
                "source_digest,source_order_key,producing_runtime_instance_id,event_time_basis,frame_sequence) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'TEST',%s,1,%s,'test',%s,'received_at',20)",
                (str(self.entity_id), str(self.event_id), self.now, self.now, self.now, retained,
                 self.now-timedelta(seconds=5) if retained is not None else None,
                 quality, str(self.revision_id), "a"*64, str(uuid4())),
            )

    def _build(self, cursor):
        return build_frame_outbox_event(
            cursor, frame_id=self.frame_id, frame_sequence=20, status=FrameStatus.COMPLETE,
            configuration_revision=1, capture_beat=20, frame_time=self.now, previous_l0={},
        )

    def test_bad_frame_preserves_retained_false_after_old_history_is_pruned(self):
        # A history rescan loses this valid retained value; the transaction's
        # latest row still contains its original observation time.
        self._seed_current()
        with self.connection.cursor() as cursor:
            result = self._build(cursor)
        change, = result.l2_changes
        self.assertIs(False, change.value.value)
        self.assertEqual(TrunkQuality.BAD, change.quality)
        self.assertEqual(self.now, change.observed_at)
        self.assertEqual(self.now-timedelta(seconds=5), change.value_observed_at)

    def test_first_bad_frame_does_not_invent_a_value(self):
        self._seed_current(retained=None)
        with self.connection.cursor() as cursor:
            change, = self._build(cursor).l2_changes
        self.assertIsNone(change.value.value)
        self.assertIsNone(change.value_observed_at)
        self.assertEqual(TrunkQuality.BAD, change.quality)

    def test_publication_rejects_latest_from_another_frame_or_event(self):
        self._seed_current(quality=192, value=False)
        with self.connection.cursor() as cursor:
            for assignment in ("frame_sequence=21", "event_id='00000000-0000-0000-0000-000000000001'"):
                with self.subTest(assignment=assignment):
                    cursor.execute("SAVEPOINT wrong_latest")
                    cursor.execute("UPDATE t_l2_latest SET " + assignment)
                    with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_L2_LATEST_MISMATCH"):
                        self._build(cursor)
                    cursor.execute("ROLLBACK TO SAVEPOINT wrong_latest")

    def test_current_frame_does_not_read_unrelated_history(self):
        # Deliberately no history indexes except the frame lookup: historical
        # data growth must not increase work to publish an already computed row.
        self._seed_current(quality=192, value=False)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_l2_observations SELECT "
                "(jsonb_populate_record(NULL::t_l2_observations, to_jsonb(observation) || "
                "jsonb_build_object('frame_id',%s::text))).* FROM t_l2_observations observation "
                "CROSS JOIN generate_series(1,12000)", (str(uuid4()),),
            )
            cursor.execute("CREATE INDEX ON t_l2_observations(frame_id)")
            cursor.execute("ANALYZE t_l2_observations")
            cursor.execute("ANALYZE t_l2_latest")
            cursor.execute("ANALYZE t_entity_instances")

            class MeasuredCursor:
                def __init__(self, real):
                    self.real, self.blocks = real, 0

                def execute(self, query, parameters=None):
                    self.real.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, parameters)
                    plan = self.real.fetchone()[0][0]["Plan"]
                    self.blocks += sum(plan.get(key, 0) for key in (
                        "Shared Hit Blocks", "Shared Read Blocks", "Local Hit Blocks", "Local Read Blocks"))
                    self.real.execute(query, parameters)

                def fetchall(self):
                    return self.real.fetchall()

            measured = MeasuredCursor(cursor)
            result = self._build(measured)
            self.assertEqual(1, len(result.l2_changes))
            self.assertIs(False, result.l2_changes[0].value.value)
            self.assertLess(measured.blocks, 100, "frame publication scanned unrelated L2 history")
