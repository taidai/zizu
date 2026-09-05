from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import unittest
from uuid import uuid4

import psycopg2
from psycopg2.extras import execute_values

from app.services.data_trunk_contracts import DataTrunkError, FrameStatus, TrunkQuality
from app.services.data_trunk_outbox import (
    PostgresFrameOutboxRepository,
    build_frame_outbox_event,
)


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

    def _build(self, cursor, *, previous_l0=None, capture_beat=20, status=FrameStatus.COMPLETE):
        return build_frame_outbox_event(
            cursor, frame_id=self.frame_id, frame_sequence=20, status=status,
            configuration_revision=1, capture_beat=capture_beat, frame_time=self.now,
            previous_l0={} if previous_l0 is None else previous_l0,
        )

    def _seed_l0(self, count=1, *, beat=1, quality=192, value=0):
        tags = [(uuid4(), uuid4()) for _ in range(count)]
        with self.connection.cursor() as cursor:
            execute_values(cursor,
                "INSERT INTO t_tags(id,node_id,name,data_type,enabled,timestamp_trusted,source_sequence_trusted) VALUES %s",
                [(str(tag), str(self.node_id), str(tag), 'INT', True, False, True) for tag, _ in tags],
            )
            execute_values(cursor,
                "INSERT INTO t_telemetry_latest(node_id,tag_id,ts,quality,observation_id,"
                "source_message_id,source_sequence,source_digest,raw_value_int,event_received_at,"
                "source_order_key,frame_sequence,accepted_beat,source_order_mode) VALUES %s",
                [(str(self.node_id), str(tag), self.now, quality, str(observation),
                  'fixture', beat, 'a'*64, value, self.now, 'fixture', 19, beat, 'sequence')
                 for tag, observation in tags],
            )
        return tags

    def test_publication_does_not_rematerialize_unchanged_stale_points(self):
        # Adding old, unchanged points must not multiply transaction B's second
        # L0 materialization. A new sample still publishes even at the same value.
        self._seed_l0(1800)
        (tag_id, _), = self._seed_l0(beat=19)
        new_observation = uuid4()
        with self.connection.cursor() as cursor:
            previous = PostgresFrameOutboxRepository._load_l0_latest_state(cursor, 19)
            cursor.execute(
                "UPDATE t_telemetry_latest SET observation_id=%s,accepted_beat=20,frame_sequence=20 "
                "WHERE tag_id=%s", (str(new_observation), str(tag_id)),
            )

            class RowMeter:
                def __init__(self, real):
                    self.real, self.l0_rows = real, 0

                def execute(self, query, parameters=None):
                    self.real.execute(query, parameters)

                def fetchall(self):
                    rows = self.real.fetchall()
                    if self.real.description[0].name == 'tag_id':
                        self.l0_rows += len(rows)
                    return rows

            meter = RowMeter(cursor)
            result = self._build(meter, previous_l0=previous)
        change, = result.l0_changes
        self.assertEqual(tag_id, change.tag_id)
        self.assertEqual(new_observation, change.observation_id)
        self.assertIs(type(change.value.value), int)
        self.assertEqual(0, change.value.value)
        self.assertEqual(TrunkQuality.GOOD, change.effective_quality)
        self.assertEqual(1, meter.l0_rows, "unchanged stale L0 rows were rebuilt a second time")

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

    def test_untouched_point_crosses_stale_boundary_even_in_failed_frame(self):
        (tag_id, observation_id), = self._seed_l0(beat=17)
        self._seed_l0(beat=18)  # Two beats old is not stale yet.
        self._seed_l0(beat=1)   # Already stale is not a new change.
        with self.connection.cursor() as cursor:
            previous = PostgresFrameOutboxRepository._load_l0_latest_state(cursor, 19)
            for status in (FrameStatus.COMPLETE, FrameStatus.FAILED):
                with self.subTest(status=status):
                    change, = self._build(cursor, previous_l0=previous, status=status).l0_changes
                    self.assertEqual(tag_id, change.tag_id)
                    self.assertEqual(observation_id, change.observation_id)
                    self.assertEqual(TrunkQuality.STALE, change.effective_quality)
                    self.assertEqual(0, change.value.value)

    def test_first_publication_includes_retained_points(self):
        tags = self._seed_l0(3, beat=19)
        with self.connection.cursor() as cursor:
            changes = self._build(cursor).l0_changes
        self.assertEqual({tag for tag, _ in tags}, {change.tag_id for change in changes})
        self.assertTrue(all(change.effective_quality == TrunkQuality.GOOD for change in changes))

    def test_new_point_and_revived_point_are_published(self):
        (revived_tag, _), = self._seed_l0(beat=1)
        with self.connection.cursor() as cursor:
            previous = PostgresFrameOutboxRepository._load_l0_latest_state(cursor, 19)
            (new_tag, _), = self._seed_l0(beat=20)
            cursor.execute(
                "UPDATE t_telemetry_latest SET frame_sequence=20,accepted_beat=20,"
                "observation_id=%s WHERE tag_id=%s",
                (str(uuid4()), str(revived_tag)),
            )
            cursor.execute("UPDATE t_telemetry_latest SET frame_sequence=20 WHERE tag_id=%s", (str(new_tag),))
            changes = self._build(cursor, previous_l0=previous).l0_changes
        self.assertEqual({revived_tag, new_tag}, {change.tag_id for change in changes})
        self.assertTrue(all(change.effective_quality == TrunkQuality.GOOD for change in changes))

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
