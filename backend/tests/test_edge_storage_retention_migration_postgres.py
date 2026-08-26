from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import unittest
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg2

os.environ.setdefault("NEURON_PASSWORD", "test-neuron-secret")
os.environ.setdefault("NANOMQ_API_PASSWORD", "test-nanomq-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value-that-is-long-enough")

from tests import test_node_data_trunk_hard_cut_migration_postgres as migration


MIGRATION_045 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_045_edge_storage_retention.sql"
)
REFERENCE = (
    Path(__file__).resolve().parents[2]
    / "reference-point-processings"
    / "pcs-brand-a.zizu-point-processing.json"
)
NODE_ID = UUID("94000000-0000-0000-0000-000000000001")


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run edge retention migration tests",
)
class EdgeStorageRetentionMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Edge retention tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                hard_cut = migration.NodeDataTrunkHardCutMigrationPostgresTest
                hard_cut._reset_through_043(cursor)
                hard_cut._apply_044(cursor)
                self._restore_timescale_001_footprint(cursor)

    @staticmethod
    def _apply_045(cursor) -> None:
        cursor.execute(MIGRATION_045.read_text(encoding="utf-8"))

    @staticmethod
    def _restore_timescale_001_footprint(cursor) -> None:
        """Restore the Timescale objects omitted by the legacy reset helper."""
        cursor.execute(
            "SELECT create_hypertable("
            "'public.t_telemetry', 'ts', "
            "chunk_time_interval => interval '1 day', "
            "if_not_exists => TRUE, migrate_data => TRUE)"
        )
        cursor.execute(
            """
            CREATE MATERIALIZED VIEW public.tel_agg_5min
            WITH (timescaledb.continuous) AS
            SELECT time_bucket('5 minutes', ts) AS bucket,
                   node_id, tag_id,
                   avg(value_float) AS avg_val,
                   min(value_float) AS min_val,
                   max(value_float) AS max_val,
                   count(*) AS count
            FROM public.t_telemetry
            WHERE value_float IS NOT NULL
            GROUP BY 1, 2, 3
            WITH NO DATA;

            CREATE MATERIALIZED VIEW public.tel_agg_1h
            WITH (timescaledb.continuous) AS
            SELECT time_bucket('1 hour', ts) AS bucket,
                   node_id, tag_id,
                   avg(value_float) AS avg_val,
                   sum(CASE WHEN is_virtual THEN 0 ELSE value_float END)
                     AS sum_physical
            FROM public.t_telemetry
            WHERE value_float IS NOT NULL
            GROUP BY 1, 2, 3
            WITH NO DATA;

            CREATE MATERIALIZED VIEW public.tel_agg_1d
            WITH (timescaledb.continuous) AS
            SELECT time_bucket('1 day', ts) AS bucket,
                   node_id, tag_id,
                   avg(value_float) AS avg_val,
                   max(value_float) AS max_daily,
                   min(value_float) AS min_daily
            FROM public.t_telemetry
            WHERE value_float IS NOT NULL
            GROUP BY 1, 2, 3
            WITH NO DATA
            """
        )

    @staticmethod
    def _insert_node_and_tag(cursor) -> tuple[UUID, UUID]:
        node_id = uuid4()
        tag_id = uuid4()
        cursor.execute(
            "INSERT INTO t_nodes(id,name,node_type,enabled) "
            "VALUES(%s,'RETENTION-TEST','PCS',TRUE)",
            (str(node_id),),
        )
        cursor.execute(
            """
            INSERT INTO t_tags
              (id,node_id,name,data_type,unit,read_write,enabled,
               timestamp_trusted)
            VALUES(%s,%s,'ActivePowerRaw','FLOAT','W','R',TRUE,FALSE)
            """,
            (str(tag_id), str(node_id)),
        )
        return node_id, tag_id

    def _insert_upgrade_facts(
        self,
        cursor,
    ) -> tuple[UUID, UUID, UUID, UUID]:
        node_id, tag_id = self._insert_node_and_tag(cursor)
        old_observation_id = uuid4()
        recent_observation_id = uuid4()
        l2_event_id = uuid4()
        cursor.execute("SET session_replication_role = replica")
        try:
            cursor.execute(
                """
                INSERT INTO t_l0_observation_dedup
                  (observation_id,tag_id,observed_at,source_digest,created_at)
                VALUES
                  (%s,%s,clock_timestamp()-interval '7 hours',%s,
                   clock_timestamp()-interval '7 hours'),
                  (%s,%s,clock_timestamp()-interval '5 hours',%s,
                   clock_timestamp()-interval '5 hours');
                INSERT INTO t_telemetry
                  (ts,node_id,tag_id,value_float,is_virtual,quality,
                   observation_id,source_digest,raw_value_float,
                   event_time_basis,event_received_at)
                VALUES
                  (clock_timestamp()-interval '7 hours',%s,%s,1.0,FALSE,192,
                   %s,%s,1.0,'observed_at',
                   clock_timestamp()-interval '7 hours');
                INSERT INTO t_l2_observation_sources
                  (l2_event_id,l2_observed_at,source_kind,l0_observation_id,
                   source_digest,source_event_time_basis)
                VALUES
                  (%s,clock_timestamp()-interval '7 hours','l0',%s,%s,
                   'observed_at')
                """,
                (
                    str(old_observation_id),
                    str(tag_id),
                    "a" * 64,
                    str(recent_observation_id),
                    str(tag_id),
                    "b" * 64,
                    str(node_id),
                    str(tag_id),
                    str(old_observation_id),
                    "a" * 64,
                    str(l2_event_id),
                    str(old_observation_id),
                    "a" * 64,
                ),
            )
        finally:
            cursor.execute("SET session_replication_role = origin")
        return old_observation_id, recent_observation_id, node_id, tag_id

    def test_045_compacts_cache_without_deleting_history_evidence(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                old_observation_id, _, _, _ = self._insert_upgrade_facts(cursor)

                self._apply_045(cursor)

                cursor.execute(
                    "SELECT source_digest FROM t_l0_observation_dedup "
                    "ORDER BY source_digest"
                )
                self.assertEqual(
                    ["b" * 64],
                    [row[0].strip() for row in cursor.fetchall()],
                )
                cursor.execute(
                    "SELECT observation_id, source_digest FROM t_telemetry "
                    "WHERE source_digest=%s",
                    ("a" * 64,),
                )
                observation_id, digest = cursor.fetchone()
                self.assertEqual(
                    (old_observation_id, "a" * 64),
                    (UUID(str(observation_id)), digest.strip()),
                )
                cursor.execute(
                    "SELECT l0_observation_id, source_digest "
                    "FROM t_l2_observation_sources WHERE source_digest=%s",
                    ("a" * 64,),
                )
                observation_id, digest = cursor.fetchone()
                self.assertEqual(
                    (old_observation_id, "a" * 64),
                    (UUID(str(observation_id)), digest.strip()),
                )
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM pg_constraint
                    WHERE confrelid='public.t_l0_observation_dedup'::regclass
                      AND conrelid IN (
                        'public.t_telemetry'::regclass,
                        'public.t_telemetry_latest'::regclass,
                        'public.t_l2_observation_sources'::regclass
                      )
                    """
                )
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid IN (
                      'public.t_telemetry'::regclass,
                      'public.t_telemetry_latest'::regclass
                    )
                      AND conname IN (
                        'chk_telemetry_raw_typed_value',
                        'chk_telemetry_latest_raw_typed_value'
                      )
                    ORDER BY conname
                    """
                )
                self.assertEqual(
                    [
                        "chk_telemetry_latest_raw_typed_value",
                        "chk_telemetry_raw_typed_value",
                    ],
                    [row[0] for row in cursor.fetchall()],
                )

    def test_045_installs_one_fixed_six_hour_dedup_job(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_045(cursor)
                _, tag_id = self._insert_node_and_tag(cursor)
                cursor.execute(
                    """
                    INSERT INTO t_l0_observation_dedup
                      (observation_id,tag_id,observed_at,source_digest,created_at)
                    VALUES
                      (%s,%s,clock_timestamp()-interval '7 hours',%s,
                       clock_timestamp()-interval '7 hours'),
                      (%s,%s,clock_timestamp()-interval '5 hours',%s,
                       clock_timestamp()-interval '5 hours')
                    """,
                    (
                        str(uuid4()),
                        str(tag_id),
                        "c" * 64,
                        str(uuid4()),
                        str(tag_id),
                        "d" * 64,
                    ),
                )
                cursor.execute(
                    """
                    SELECT job_id, schedule_interval
                    FROM timescaledb_information.jobs
                    WHERE proc_schema='public'
                      AND proc_name='prune_l0_observation_dedup'
                    """
                )
                jobs = cursor.fetchall()
                self.assertEqual(1, len(jobs))
                self.assertEqual(timedelta(minutes=15), jobs[0][1])

                cursor.execute(
                    "CALL public.prune_l0_observation_dedup(0, '{}'::jsonb)"
                )
                cursor.execute(
                    "SELECT source_digest FROM t_l0_observation_dedup "
                    "ORDER BY source_digest"
                )
                self.assertEqual(
                    ["d" * 64],
                    [row[0].strip() for row in cursor.fetchall()],
                )

    def test_045_installs_bounded_telemetry_and_long_term_aggregate_jobs(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_045(cursor)
                cursor.execute(
                    """
                    SELECT time_interval
                    FROM timescaledb_information.dimensions
                    WHERE hypertable_schema='public'
                      AND hypertable_name='t_telemetry'
                    """
                )
                self.assertEqual(timedelta(hours=1), cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT jobs.proc_name,
                           COALESCE(cagg.view_name, jobs.hypertable_name),
                           CASE jobs.proc_name
                             WHEN 'policy_compression'
                               THEN (jobs.config->>'compress_after')::interval
                             WHEN 'policy_retention'
                               THEN (jobs.config->>'drop_after')::interval
                             ELSE jobs.schedule_interval
                           END AS fixed_interval
                    FROM timescaledb_information.jobs AS jobs
                    LEFT JOIN timescaledb_information.continuous_aggregates AS cagg
                      ON cagg.materialization_hypertable_schema =
                           jobs.hypertable_schema
                     AND cagg.materialization_hypertable_name =
                           jobs.hypertable_name
                    WHERE jobs.proc_name IN (
                      'policy_compression',
                      'policy_retention',
                      'policy_refresh_continuous_aggregate'
                    )
                    ORDER BY 1, 2
                    """
                )
                actual = set(cursor.fetchall())
                expected = {
                    ("policy_compression", "t_telemetry", timedelta(hours=6)),
                    ("policy_retention", "t_telemetry", timedelta(days=7)),
                    (
                        "policy_refresh_continuous_aggregate",
                        "tel_agg_1h",
                        timedelta(hours=1),
                    ),
                    (
                        "policy_refresh_continuous_aggregate",
                        "tel_agg_1d",
                        timedelta(days=1),
                    ),
                }
                self.assertEqual(expected, actual)
                self.assertNotIn(
                    (
                        "policy_refresh_continuous_aggregate",
                        "tel_agg_5min",
                        timedelta(minutes=5),
                    ),
                    actual,
                )

                node_id, tag_id = self._insert_node_and_tag(cursor)
                cursor.execute(
                    """
                    INSERT INTO t_telemetry
                      (ts,node_id,tag_id,value_float,is_virtual,quality,
                       event_time_basis,event_received_at)
                    VALUES
                      (date_trunc('day',clock_timestamp())-interval '30 hours',
                       %s,%s,1.0,FALSE,192,'observed_at',clock_timestamp()),
                      (date_trunc('day',clock_timestamp())-interval '29 hours',
                       %s,%s,3.0,FALSE,192,'observed_at',clock_timestamp()),
                      (clock_timestamp()-interval '4 hours',
                       %s,%s,5.0,FALSE,192,'observed_at',clock_timestamp()),
                      (clock_timestamp()-interval '3 hours',
                       %s,%s,7.0,FALSE,192,'observed_at',clock_timestamp())
                    """,
                    (
                        str(node_id),
                        str(tag_id),
                        str(node_id),
                        str(tag_id),
                        str(node_id),
                        str(tag_id),
                        str(node_id),
                        str(tag_id),
                    ),
                )
                cursor.execute(
                    "CALL refresh_continuous_aggregate("
                    "'public.tel_agg_1h', "
                    "clock_timestamp()-interval '7 days', "
                    "clock_timestamp()-interval '1 hour')"
                )
                cursor.execute(
                    "CALL refresh_continuous_aggregate("
                    "'public.tel_agg_1d', "
                    "clock_timestamp()-interval '7 days', "
                    "date_trunc('day',clock_timestamp()))"
                )
                cursor.execute(
                    "SELECT count(*) FROM tel_agg_1h WHERE tag_id=%s",
                    (str(tag_id),),
                )
                aggregate_1h_count = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT count(*) FROM tel_agg_1d WHERE tag_id=%s",
                    (str(tag_id),),
                )
                aggregate_1d_count = cursor.fetchone()[0]
                self.assertGreater(aggregate_1h_count, 0)
                self.assertGreater(aggregate_1d_count, 0)

                cursor.execute(
                    "SELECT drop_chunks("
                    "'public.t_telemetry', "
                    "older_than => clock_timestamp()+interval '1 day')"
                )
                cursor.execute(
                    "SELECT count(*) FROM t_telemetry WHERE tag_id=%s",
                    (str(tag_id),),
                )
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM tel_agg_1h WHERE tag_id=%s",
                    (str(tag_id),),
                )
                self.assertEqual(aggregate_1h_count, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM tel_agg_1d WHERE tag_id=%s",
                    (str(tag_id),),
                )
                self.assertEqual(aggregate_1d_count, cursor.fetchone()[0])

    def test_045_schedules_first_aggregate_backfill_promptly(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_045(cursor)
                cursor.execute(
                    """
                    SELECT hypertable_name,
                           initial_start <= clock_timestamp()+interval '2 minutes',
                           fixed_schedule
                    FROM timescaledb_information.jobs
                    WHERE proc_name='policy_refresh_continuous_aggregate'
                      AND hypertable_name IN ('tel_agg_1h', 'tel_agg_1d')
                    ORDER BY hypertable_name
                    """
                )
                self.assertEqual(
                    [
                        ("tel_agg_1d", True, True),
                        ("tel_agg_1h", True, True),
                    ],
                    cursor.fetchall(),
                )

    def test_045_fresh_install_creates_each_object_once(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_045(cursor)
                cursor.execute(
                    """
                    SELECT
                      to_regclass('public.idx_l0_observation_dedup_created_at'),
                      to_regprocedure(
                        'public.prune_l0_observation_dedup(integer,jsonb)'
                      ),
                      count(*) FILTER (
                        WHERE proc_schema='public'
                          AND proc_name='prune_l0_observation_dedup'
                      )
                    FROM timescaledb_information.jobs
                    """
                )
                index_name, procedure_name, prune_jobs = cursor.fetchone()
                self.assertEqual(
                    "idx_l0_observation_dedup_created_at",
                    index_name,
                )
                self.assertEqual(
                    "prune_l0_observation_dedup(integer,jsonb)",
                    procedure_name,
                )
                self.assertEqual(1, prune_jobs)
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM pg_trigger
                    WHERE tgrelid='public.t_l0_observation_dedup'::regclass
                      AND tgname='trg_t_l0_observation_dedup_append_only'
                      AND NOT tgisinternal
                    """
                )
                self.assertEqual(0, cursor.fetchone()[0])

    def test_045_replay_preserves_jobs_cache_and_history(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._insert_upgrade_facts(cursor)
                self._apply_045(cursor)
                cursor.execute(
                    "SELECT array_agg(job_id ORDER BY job_id) "
                    "FROM timescaledb_information.jobs "
                    "WHERE proc_name IN ("
                    "'prune_l0_observation_dedup','policy_compression',"
                    "'policy_retention','policy_refresh_continuous_aggregate')"
                )
                job_ids_before = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT (SELECT count(*) FROM t_l0_observation_dedup), "
                    "(SELECT count(*) FROM t_telemetry WHERE source_digest=%s), "
                    "(SELECT count(*) FROM t_l2_observation_sources "
                    " WHERE source_digest=%s)",
                    ("a" * 64, "a" * 64),
                )
                counts_before = cursor.fetchone()

                self._apply_045(cursor)

                cursor.execute(
                    "SELECT array_agg(job_id ORDER BY job_id) "
                    "FROM timescaledb_information.jobs "
                    "WHERE proc_name IN ("
                    "'prune_l0_observation_dedup','policy_compression',"
                    "'policy_retention','policy_refresh_continuous_aggregate')"
                )
                self.assertEqual(job_ids_before, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT (SELECT count(*) FROM t_l0_observation_dedup), "
                    "(SELECT count(*) FROM t_telemetry WHERE source_digest=%s), "
                    "(SELECT count(*) FROM t_l2_observation_sources "
                    " WHERE source_digest=%s)",
                    ("a" * 64, "a" * 64),
                )
                self.assertEqual(counts_before, cursor.fetchone())

    def test_045_replay_rejects_missing_cache_index_without_writes(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_045(cursor)
                cursor.execute(
                    "SELECT array_agg(job_id ORDER BY job_id) "
                    "FROM timescaledb_information.jobs"
                )
                job_ids_before = cursor.fetchone()[0]
                cursor.execute("DROP INDEX idx_l0_observation_dedup_created_at")

                with self.assertRaises(
                    psycopg2.errors.ObjectNotInPrerequisiteState
                ) as raised:
                    self._apply_045(cursor)
                self.assertIn("SCHEMA_045_PARTIAL_STRUCTURE", str(raised.exception))
                connection.rollback()

                cursor.execute(
                    "SELECT array_agg(job_id ORDER BY job_id) "
                    "FROM timescaledb_information.jobs"
                )
                self.assertEqual(job_ids_before, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT to_regclass("
                    "'public.t_l0_observation_dedup_044_retired')"
                )
                self.assertIsNone(cursor.fetchone()[0])

    def test_045_replay_rejects_duplicate_prune_job_without_writes(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_045(cursor)
                cursor.execute(
                    "SELECT add_job("
                    "'public.prune_l0_observation_dedup', "
                    "interval '15 minutes', config => '{}'::jsonb)"
                )
                cursor.execute(
                    "SELECT count(*) FROM timescaledb_information.jobs "
                    "WHERE proc_schema='public' "
                    "AND proc_name='prune_l0_observation_dedup'"
                )
                self.assertEqual(2, cursor.fetchone()[0])

                with self.assertRaises(
                    psycopg2.errors.ObjectNotInPrerequisiteState
                ) as raised:
                    self._apply_045(cursor)
                self.assertIn("SCHEMA_045_PARTIAL_STRUCTURE", str(raised.exception))
                connection.rollback()

                cursor.execute(
                    "SELECT count(*) FROM timescaledb_information.jobs "
                    "WHERE proc_schema='public' "
                    "AND proc_name='prune_l0_observation_dedup'"
                )
                self.assertEqual(2, cursor.fetchone()[0])

    def test_045_expired_cache_replay_does_not_duplicate_l0_l2_or_outbox(self) -> None:
        from app.services.data_trunk import DataTrunk
        from app.services.data_trunk_contracts import (
            RawObservation,
            TrunkQuality,
            TypedValue,
            ValueKind,
        )
        from app.services.data_trunk_postgres import PostgresDataTrunkRepository
        from app.services.telemetry_store import (
            close_db_pool,
            get_connection,
            init_db_pool,
        )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self._apply_045(cursor)

        init_db_pool(1, 4)
        try:
            tag_ids = self._publish_brand_a()
            observed_at = datetime.now(UTC) - timedelta(minutes=5)
            tag_id = tag_ids["ActivePowerRaw"]
            first = RawObservation(
                observation_id=uuid5(NAMESPACE_URL, "test/retention/l0/first"),
                node_id=NODE_ID,
                tag_id=tag_id,
                source_key="ActivePowerRaw",
                value=TypedValue(ValueKind.FLOAT, 1000.0),
                raw_unit="W",
                quality=TrunkQuality.GOOD,
                source_timestamp=observed_at,
                received_at=observed_at,
                source_message_id="retention-message-1",
                source_sequence=1,
                source_digest="e" * 64,
                event_time_basis="observed_at",
            )
            trunk = DataTrunk(
                PostgresDataTrunkRepository(clock=lambda: observed_at)
            )
            accepted = trunk.ingest((first,))
            self.assertEqual(1, accepted.accepted_l0_count)
            self.assertEqual(1, len(accepted.l2_event_ids))

            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM t_l0_observation_dedup "
                        "WHERE source_digest=%s",
                        ("e" * 64,),
                    )
                connection.commit()

            replay_observation = RawObservation(
                observation_id=uuid5(NAMESPACE_URL, "test/retention/l0/replay"),
                node_id=first.node_id,
                tag_id=first.tag_id,
                source_key=first.source_key,
                value=first.value,
                raw_unit=first.raw_unit,
                quality=first.quality,
                source_timestamp=first.source_timestamp,
                received_at=first.received_at + timedelta(hours=7),
                source_message_id="retention-message-replay",
                source_sequence=2,
                source_digest=first.source_digest,
                event_time_basis=first.event_time_basis,
            )
            replay = trunk.ingest((replay_observation,))
            self.assertEqual(0, replay.accepted_l0_count)
            self.assertEqual(1, replay.duplicate_l0_count)
            self.assertEqual((), replay.l2_event_ids)

            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM t_telemetry "
                        "WHERE source_digest=%s",
                        ("e" * 64,),
                    )
                    self.assertEqual(1, cursor.fetchone()[0])
                    cursor.execute(
                        "SELECT count(*) FROM t_l2_observation_sources "
                        "WHERE source_digest=%s",
                        ("e" * 64,),
                    )
                    self.assertEqual(1, cursor.fetchone()[0])
                    cursor.execute("SELECT count(*) FROM t_l2_stream_outbox")
                    self.assertEqual(1, cursor.fetchone()[0])
        finally:
            close_db_pool()

    def _publish_brand_a(self) -> dict[str, UUID]:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PreviewPointProcessing,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingTemplates,
            build_postgres_point_processing,
        )
        from app.services.telemetry_store import get_connection

        raw = json.loads(REFERENCE.read_text(encoding="utf-8"))
        tags: dict[str, UUID] = {}
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled) "
                    "VALUES(%s,'PCS-RETENTION','PCS',TRUE)",
                    (NODE_ID,),
                )
                for item in raw["inputs"]:
                    tag_id = uuid5(
                        NAMESPACE_URL,
                        f"test/retention/tag/{NODE_ID}/{item['sourceKey']}",
                    )
                    tags[item["sourceKey"]] = tag_id
                    cursor.execute(
                        """
                        INSERT INTO t_tags
                          (id,node_id,name,data_type,unit,read_write,enabled,
                           timestamp_trusted)
                        VALUES(%s,%s,%s,%s,%s,'R',TRUE,FALSE)
                        """,
                        (
                            tag_id,
                            NODE_ID,
                            item["sourceKey"],
                            item["dataType"],
                            item.get("unit"),
                        ),
                    )
            connection.commit()
        registered = PostgresPointProcessingTemplates().import_template(
            raw,
            actor="test:engineer",
        )
        service = build_postgres_point_processing()
        plan = service.preview(
            PreviewPointProcessing(
                NODE_ID,
                registered.revision_id,
                {},
                "test:engineer",
            )
        )
        service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "publish",
                "test:engineer",
            )
        )
        return tags


if __name__ == "__main__":
    unittest.main()
