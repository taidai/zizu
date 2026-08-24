from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import os
from threading import Event, Thread
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extensions import cursor as PsycopgCursor
from psycopg2.extras import register_uuid

from app.services.data_trunk_contracts import TrunkQuality
from app.services.metric_projection_postgres import MetricProjection, ProjectionReceipt
from tests import test_data_trunk_migration_postgres as migration_support
from tests import test_business_metrics_postgres as business_metric_support


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL integration tests",
)
class MetricProjectionPostgresTest(unittest.TestCase):
    SOURCE_RUNTIME_ID = UUID("92000000-0000-0000-0000-000000000001")
    SOURCE_TEMPLATE_ID = UUID("92000000-0000-0000-0000-000000000002")
    SOURCE_REVISION_ID = UUID("92000000-0000-0000-0000-000000000003")
    SOURCE_OUTPUT_ID = UUID("92000000-0000-0000-0000-000000000004")
    SOURCE_PLAN_ID = UUID("92000000-0000-0000-0000-000000000005")
    SOURCE_PROCESSING_ID = UUID("92000000-0000-0000-0000-000000000006")
    DAY_START = datetime(2026, 8, 22, 16, tzinfo=UTC)

    @classmethod
    def setUpClass(cls) -> None:
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": os.environ["DB_NAME"],
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        if not cls.connection_kwargs["dbname"].endswith("_test"):
            raise RuntimeError("Metric projection tests require a *_test database")
        register_uuid()

    @classmethod
    def tearDownClass(cls) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    def setUp(self) -> None:
        self._reset_and_install()

    @contextmanager
    def _connection(self):
        connection = psycopg2.connect(**self.connection_kwargs)
        try:
            yield connection
        finally:
            connection.close()

    def _reset_and_install(
        self,
        *,
        method: str = "average",
        window_kind: str = "aligned_daily",
        counter_contract: dict[str, object] | None = None,
        source_data_type: str = "FLOAT",
    ) -> None:
        from app.services.telemetry_store import close_db_pool, init_db_pool

        close_db_pool()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration_support.DataTrunkMigrationPostgresTest._reset_through_041(
                    cursor
                )
                migration_support.DataTrunkMigrationPostgresTest._apply_042(cursor)
                migration_support.DataTrunkMigrationPostgresTest._apply_043(cursor)
        init_db_pool(min_conn=1, max_conn=4)

        fixture = business_metric_support.BusinessMetricPostgresTest("runTest")
        self.fixture = fixture
        fixture.connection_kwargs = self.connection_kwargs
        fixture.template_raw = {
            "schemaVersion": "zizu.business-metric/v1alpha1",
            "id": "ems.daily-average-power",
            "revision": 1,
            "displayName": "日平均功率",
            "targetNodeType": "SITE",
            "output": {
                "entityDefinition": (
                    "site.daily_energy"
                    if method == "counter_delta"
                    else "site.daily_average_power"
                ),
                "dataType": "FLOAT",
                "unit": "kWh" if method == "counter_delta" else "kW",
                "temporalSemantics": "windowed",
            },
            "window": (
                {"kind": "aligned_daily"}
                if window_kind == "aligned_daily"
                else {"kind": "rolling", "duration": "6h"}
            ),
            "sources": [
                {
                    "method": method,
                    "entityDefinition": (
                        "pv.energy_total" if method == "counter_delta" else "pv.active_power"
                    ),
                    "priority": 1,
                    **(
                        {
                            "counter": counter_contract
                            or {
                                "maximum": "4294967295",
                                "bitWidth": 32,
                                "resetOnDecrease": False,
                                "rolloverOnDecrease": True,
                            }
                        }
                        if method == "counter_delta"
                        else {}
                    ),
                }
            ],
            "quality": {"goodCoverage": 0.40, "minimumUsableCoverage": 0.20},
            "allowedLateness": "1m",
            "correction": {
                "automaticHorizon": (
                    "7d" if window_kind == "aligned_daily" else "6h"
                )
            },
            "capabilities": {"controlEligible": False},
        }
        fixture._seed_site_and_template()
        self.source_data_type = source_data_type
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE t_entity_instances SET data_type = %s WHERE id = %s",
                    (source_data_type, fixture.COUNTER_ID),
                )
        if method != "counter_delta":
            with psycopg2.connect(**self.connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE t_entity_instances
                        SET definition_id = 'pv.active_power', unit = 'kW',
                            freshness_seconds = %s
                        WHERE id = %s
                        """,
                        (
                            86400 if window_kind == "aligned_daily" else 21600,
                            fixture.COUNTER_ID,
                        ),
                    )
        self._install_source_producer(
            fixture,
            definition_id=(
                "pv.energy_total" if method == "counter_delta" else "pv.active_power"
            ),
            unit="kWh" if method == "counter_delta" else "kW",
            data_type=source_data_type,
            freshness_seconds=(
                5
                if method == "counter_delta"
                else 86400
                if window_kind == "aligned_daily"
                else 21600
            ),
        )
        plan = fixture._preview_template("ems.daily-average-power")
        self.installed = fixture._delivery().apply(fixture._apply_command(plan))
        self.source_entity_id = fixture.COUNTER_ID
        self.output_entity_id = self.installed.entity_instance_id
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_runtime_instances (id, started_at, platform_version)
                    VALUES (%s, %s, 'metric-source-test')
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (self.SOURCE_RUNTIME_ID, self.DAY_START),
                )

    def _install_source_producer(
        self,
        fixture,
        *,
        definition_id: str,
        unit: str,
        data_type: str,
        freshness_seconds: int,
    ) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE t_entity_instance_bindings
                    SET active = FALSE
                    WHERE entity_instance_id = %s AND active = TRUE;
                    UPDATE t_entity_instances
                    SET source_kind = 'point_processing'
                    WHERE id = %s;
                    INSERT INTO t_point_processing_templates
                      (id, asset_id, device_category, brand, model,
                       display_name, status)
                    VALUES (%s, 'metric-source-test', 'inverter', 'Test', 'Source',
                            'Metric source producer', 'active');
                    INSERT INTO t_point_processing_revisions
                      (id, template_id, revision, content_digest, published_at)
                    VALUES (%s, %s, 1, %s, %s);
                    INSERT INTO t_point_processing_outputs
                      (id, revision_id, output_key, entity_definition_id,
                       data_type, unit, freshness_seconds)
                    VALUES (%s, %s, 'source', %s, %s, %s, %s);
                    INSERT INTO t_point_processing_plans
                      (id, node_id, template_revision_id,
                       entity_identity_installation_id, solution_installation_id,
                       base_site_configuration_version, source_catalog_digest,
                       status, items, blockers, digest, planned_by)
                    VALUES (%s, %s, %s, %s, %s, 1, %s, 'applied', '[]', '[]',
                            %s, 'user:metric-source');
                    INSERT INTO t_installed_point_processings
                      (id, node_id, revision_id, source_plan_id,
                       solution_installation_id, site_configuration_version,
                       installed_by, current)
                    VALUES (%s, %s, %s, %s, %s, 1, 'user:metric-source', TRUE);
                    INSERT INTO t_point_processing_output_bindings
                      (installed_processing_id, output_id, entity_instance_id)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        fixture.COUNTER_ID,
                        fixture.COUNTER_ID,
                        self.SOURCE_TEMPLATE_ID,
                        self.SOURCE_REVISION_ID,
                        self.SOURCE_TEMPLATE_ID,
                        "1" * 64,
                        self.DAY_START,
                        self.SOURCE_OUTPUT_ID,
                        self.SOURCE_REVISION_ID,
                        definition_id,
                        data_type,
                        unit,
                        freshness_seconds,
                        self.SOURCE_PLAN_ID,
                        fixture.SOURCE_NODE_ID,
                        self.SOURCE_REVISION_ID,
                        fixture.IDENTITY_INSTALLATION_ID,
                        fixture.BASE_INSTALLATION_ID,
                        "2" * 64,
                        "3" * 64,
                        self.SOURCE_PROCESSING_ID,
                        fixture.SOURCE_NODE_ID,
                        self.SOURCE_REVISION_ID,
                        self.SOURCE_PLAN_ID,
                        fixture.BASE_INSTALLATION_ID,
                        self.SOURCE_PROCESSING_ID,
                        self.SOURCE_OUTPUT_ID,
                        fixture.COUNTER_ID,
                    ),
                )

    def _runtime(self, *, now: datetime, failed_stage: str | None = None):
        def fault_hook(stage: str) -> None:
            if stage == failed_stage:
                raise RuntimeError(f"injected {stage} failure")

        return MetricProjection(
            connection_factory=self._connection,
            clock=lambda: now,
            fault_hook=fault_hook,
        )

    def _seed_source(
        self,
        *,
        event_id: UUID,
        observed_at: datetime,
        value: float | None,
        quality: TrunkQuality = TrunkQuality.GOOD,
        received_at: datetime | None = None,
        entity_id: UUID | None = None,
        event_time_basis: str = "observed_at",
        processing_revision_id: UUID | None = None,
    ) -> None:
        received = received_at or observed_at + timedelta(seconds=1)
        digest = hashlib.sha256(str(event_id).encode("ascii")).hexdigest()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_l2_observations
                      (observed_at, event_id, entity_instance_id, received_at,
                       calculated_at, value_float, value_int, quality, reason,
                       processing_revision_id, site_configuration_version,
                       source_digest, source_order_key,
                       producing_runtime_instance_id, event_time_basis)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        observed_at,
                        event_id,
                        entity_id or self.source_entity_id,
                        received,
                        max(received, observed_at),
                        value if self.source_data_type == "FLOAT" else None,
                        int(value) if value is not None and self.source_data_type == "INT" else None,
                        int(quality),
                        None if quality is TrunkQuality.GOOD else "SOURCE_BAD",
                        processing_revision_id or self.SOURCE_REVISION_ID,
                        self.installed.site_configuration_version,
                        digest,
                        f"S:{event_id.int:032d}",
                        self.SOURCE_RUNTIME_ID,
                        event_time_basis,
                    ),
                )

    def _metric_counts(self) -> tuple[int, int, int, int, int]:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM t_business_metric_projections
                       WHERE installed_metric_id = %s),
                      (SELECT count(*) FROM t_business_metric_window_results
                       WHERE installed_metric_id = %s),
                      (SELECT count(*) FROM t_l2_observations
                       WHERE entity_instance_id = %s),
                      (SELECT count(*) FROM t_l2_observation_sources AS source
                       JOIN t_l2_observations AS observation
                         ON observation.event_id = source.l2_event_id
                        AND observation.observed_at = source.l2_observed_at
                       WHERE observation.entity_instance_id = %s),
                      (SELECT count(*) FROM t_l2_stream_outbox
                       WHERE entity_instance_id = %s)
                    """,
                    (
                        self.installed.id,
                        self.installed.id,
                        self.output_entity_id,
                        self.output_entity_id,
                        self.output_entity_id,
                    ),
                )
                return tuple(int(item) for item in cursor.fetchone())

    def _seed_closable_day(self) -> tuple[UUID, UUID, UUID]:
        first = UUID("92000000-0000-0000-0000-000000000101")
        last = UUID("92000000-0000-0000-0000-000000000102")
        watermark = UUID("92000000-0000-0000-0000-000000000103")
        self._seed_source(event_id=first, observed_at=self.DAY_START, value=10.0)
        self._seed_source(
            event_id=last,
            observed_at=self.DAY_START + timedelta(hours=23),
            value=20.0,
        )
        self._seed_source(
            event_id=watermark,
            observed_at=self.DAY_START + timedelta(days=1, minutes=1, seconds=1),
            value=20.0,
        )
        return first, last, watermark

    def test_provisional_updates_projection_without_l2_fact(self) -> None:
        first = UUID("92000000-0000-0000-0000-000000000011")
        second = UUID("92000000-0000-0000-0000-000000000012")
        self._seed_source(event_id=first, observed_at=self.DAY_START, value=10.0)
        self._seed_source(
            event_id=second,
            observed_at=self.DAY_START + timedelta(hours=12),
            value=20.0,
        )
        runtime = self._runtime(now=self.DAY_START + timedelta(hours=12, minutes=1))

        hint_receipt = runtime.observe_committed((first, second))

        self.assertEqual(hint_receipt.projection_count, 0)
        self.assertEqual(self._metric_counts(), (0, 0, 0, 0, 0))
        receipt = runtime.advance(now=self.DAY_START + timedelta(hours=12, minutes=1))

        self.assertEqual(receipt.projection_count, 1)
        self.assertEqual(self._metric_counts(), (1, 0, 0, 0, 0))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state ->> 'lifecycle', state ->> 'value', coverage,
                           quality, state -> 'sourceEventIds'
                    FROM t_business_metric_projections
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                lifecycle, value, coverage, quality, source_ids = cursor.fetchone()
        self.assertEqual(lifecycle, "provisional")
        self.assertEqual(value, "15.0")
        self.assertEqual(coverage, 0.5)
        self.assertEqual(quality, int(TrunkQuality.GOOD))
        self.assertEqual(source_ids, [str(first), str(second)])

    def test_empty_source_history_does_not_create_projection(self) -> None:
        receipt = self._runtime(
            now=self.DAY_START + timedelta(hours=1)
        ).advance()

        self.assertEqual(receipt.projection_count, 0)
        self.assertEqual(self._metric_counts(), (0, 0, 0, 0, 0))

    def test_rolling_six_hour_low_coverage_current_stays_provisional(self) -> None:
        self._reset_and_install(window_kind="rolling")
        first = UUID("92000000-0000-0000-0000-000000000018")
        second = UUID("92000000-0000-0000-0000-000000000019")
        observed = self.DAY_START + timedelta(hours=1)
        self._seed_source(event_id=first, observed_at=observed, value=10.0)
        self._seed_source(
            event_id=second,
            observed_at=observed + timedelta(seconds=30),
            value=20.0,
        )
        now = observed + timedelta(seconds=40)

        self._runtime(now=now).advance(now=now)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT window_ended_at - window_started_at,
                           state ->> 'lifecycle', quality, state ->> 'reason'
                    FROM t_business_metric_projections
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                row = cursor.fetchone()
        self.assertEqual(row[0], timedelta(hours=6))
        self.assertEqual(row[1], "provisional")
        self.assertEqual(row[2], int(TrunkQuality.BAD))
        self.assertIsNotNone(row[3])
        self.assertEqual(self._metric_counts()[1:3], (0, 0))

    def test_rolling_tick_batches_latest_results_and_bounds_source_scan(self) -> None:
        self._reset_and_install(window_kind="rolling")
        sql: list[str] = []

        class CountingCursor(PsycopgCursor):
            def execute(self, query, vars=None):
                sql.append(" ".join(str(query).split()))
                return super().execute(query, vars)

        @contextmanager
        def counting_connection():
            connection = psycopg2.connect(
                **self.connection_kwargs,
                cursor_factory=CountingCursor,
            )
            try:
                yield connection
            finally:
                connection.close()

        first = UUID("92000000-0000-0000-0000-000000000081")
        second = UUID("92000000-0000-0000-0000-000000000082")
        initial_now = self.DAY_START + timedelta(hours=7)
        self._seed_source(
            event_id=first,
            observed_at=initial_now - timedelta(minutes=1),
            value=10.0,
        )
        runtime = MetricProjection(
            connection_factory=counting_connection,
            clock=lambda: initial_now,
        )
        runtime.advance(now=initial_now)
        sql.clear()
        next_now = initial_now + timedelta(minutes=1)
        self._seed_source(event_id=second, observed_at=next_now, value=20.0)

        runtime.advance(now=next_now)

        latest_result_queries = [
            query
            for query in sql
            if "FROM t_business_metric_window_results" in query
            and "DISTINCT ON" in query
        ]
        source_bound_queries = [
            query
            for query in sql
            if "FROM t_l2_observations" in query and "min(" in query
        ]
        self.assertEqual(len(latest_result_queries), 1)
        self.assertEqual(len(source_bound_queries), 1)
        self.assertIn("received_at >", source_bound_queries[0])
        self.assertIn("END >=", source_bound_queries[0])

    def test_first_completion_after_nine_day_stop_is_not_cut_by_correction_horizon(self) -> None:
        first = UUID("92000000-0000-0000-0000-000000000016")
        last = UUID("92000000-0000-0000-0000-000000000017")
        self._seed_source(event_id=first, observed_at=self.DAY_START, value=10.0)
        self._seed_source(
            event_id=last,
            observed_at=self.DAY_START + timedelta(hours=23),
            value=20.0,
        )
        now = self.DAY_START + timedelta(days=9)

        receipt = self._runtime(now=now).advance(now=now)

        self.assertEqual(receipt.completed_count, 1)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT window_started_at, window_ended_at, revision, lifecycle
                    FROM t_business_metric_window_results
                    WHERE installed_metric_id = %s
                    ORDER BY window_started_at, revision
                    """,
                    (self.installed.id,),
                )
                rows = cursor.fetchall()
        self.assertEqual(
            rows,
            [(self.DAY_START, self.DAY_START + timedelta(days=1), 1, "completed")],
        )

    def test_frozen_producer_contract_ignores_mutable_entity_unit_and_freshness(self) -> None:
        event_id = UUID("92000000-0000-0000-0000-000000000013")
        second_id = UUID("92000000-0000-0000-0000-000000000015")
        self._seed_source(event_id=event_id, observed_at=self.DAY_START, value=10.0)
        self._seed_source(
            event_id=second_id,
            observed_at=self.DAY_START + timedelta(hours=12),
            value=20.0,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT maximum_sample_gap_seconds, producer_contract_digest
                    FROM t_business_metric_source_bindings
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                frozen = cursor.fetchone()
                cursor.execute(
                    """
                    UPDATE t_entity_instances
                    SET unit = 'MW', freshness_seconds = 1
                    WHERE id = %s
                    """,
                    (self.source_entity_id,),
                )

        self._runtime(now=self.DAY_START + timedelta(hours=12, minutes=1)).advance(
            now=self.DAY_START + timedelta(hours=12, minutes=1)
        )

        self.assertEqual(frozen[0], 86400)
        self.assertRegex(frozen[1].strip(), r"^[0-9a-f]{64}$")
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state ->> 'lifecycle', state ->> 'value', quality
                    FROM t_business_metric_projections
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                row = cursor.fetchone()
        self.assertEqual(row, ("provisional", "15.0", int(TrunkQuality.GOOD)))

    def test_l2_processing_revision_mismatch_fails_closed_without_relabeling(self) -> None:
        event_id = UUID("92000000-0000-0000-0000-000000000014")
        self._seed_source(
            event_id=event_id,
            observed_at=self.DAY_START,
            value=10.0,
            processing_revision_id=self.installed.processing_revision_id,
        )

        self._runtime(now=self.DAY_START + timedelta(minutes=1)).advance(
            now=self.DAY_START + timedelta(minutes=1)
        )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state ->> 'lifecycle', state ->> 'reason', quality
                    FROM t_business_metric_projections
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                row = cursor.fetchone()
        self.assertEqual(
            row,
            ("provisional", "SOURCE_CONTRACT_MISMATCH", int(TrunkQuality.BAD)),
        )

    def test_l2_contract_joins_the_bound_output_of_a_multi_output_revision(self) -> None:
        decoy_output = UUID("92000000-0000-0000-0000-000000000008")
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_outputs
                      (id, revision_id, output_key, entity_definition_id,
                       data_type, unit, freshness_seconds)
                    VALUES (%s, %s, 'aaa-decoy', 'pv.decoy', 'FLOAT', 'MW', 1)
                    """,
                    (decoy_output, self.SOURCE_REVISION_ID),
                )
        first = UUID("92000000-0000-0000-0000-000000000024")
        second = UUID("92000000-0000-0000-0000-000000000025")
        self._seed_source(event_id=first, observed_at=self.DAY_START, value=10.0)
        self._seed_source(
            event_id=second,
            observed_at=self.DAY_START + timedelta(hours=12),
            value=20.0,
        )

        now = self.DAY_START + timedelta(hours=13)
        self._runtime(now=now).advance(now=now)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state ->> 'value', state ->> 'reason', quality
                    FROM t_business_metric_projections
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                row = cursor.fetchone()
        self.assertEqual(row, ("15.0", None, int(TrunkQuality.GOOD)))

    def test_restart_replay_is_idempotent(self) -> None:
        self._seed_closable_day()
        after_close = self.DAY_START + timedelta(days=1, minutes=2)

        first = self._runtime(now=after_close).advance(now=after_close)
        replay = self._runtime(now=after_close).advance(now=after_close)

        self.assertEqual(first.completed_count, 1)
        self.assertEqual(replay.completed_count, 0)
        self.assertEqual(replay.corrected_count, 0)
        self.assertEqual(self._metric_counts(), (1, 1, 1, 2, 1))

    def test_restart_with_new_runtime_identity_does_not_create_correction(self) -> None:
        from app.services import data_trunk_postgres, metric_projection_postgres

        self._seed_closable_day()
        after_close = self.DAY_START + timedelta(days=1, minutes=2)
        self._runtime(now=after_close).advance(now=after_close)
        restarted_runtime_id = UUID("92000000-0000-0000-0000-000000000099")

        with patch.object(
            metric_projection_postgres,
            "RUNTIME_INSTANCE_ID",
            restarted_runtime_id,
        ), patch.object(
            data_trunk_postgres,
            "RUNTIME_INSTANCE_ID",
            restarted_runtime_id,
        ):
            replay = self._runtime(now=after_close).advance(now=after_close)

        self.assertEqual(replay.corrected_count, 0)
        self.assertEqual(self._metric_counts(), (1, 1, 1, 2, 1))

    def test_concurrent_ticks_share_the_installation_advisory_lock(self) -> None:
        self._seed_closable_day()
        now = self.DAY_START + timedelta(days=1, minutes=2)
        start = Event()
        outcomes: list[object] = []

        def advance() -> None:
            start.wait()
            try:
                outcomes.append(self._runtime(now=now).advance(now=now))
            except Exception as exc:  # captured for an assertion in the main thread
                outcomes.append(exc)

        threads = [Thread(target=advance), Thread(target=advance)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(isinstance(item, ProjectionReceipt) for item in outcomes))
        self.assertEqual(sum(item.completed_count for item in outcomes), 1)
        self.assertEqual(self._metric_counts(), (1, 1, 1, 2, 1))

    def test_projection_checkpoint_rolls_to_the_next_daily_window(self) -> None:
        first = UUID("92000000-0000-0000-0000-000000000091")
        last = UUID("92000000-0000-0000-0000-000000000092")
        watermark = UUID("92000000-0000-0000-0000-000000000093")
        self._seed_source(event_id=first, observed_at=self.DAY_START, value=10.0)
        self._seed_source(
            event_id=last,
            observed_at=self.DAY_START + timedelta(hours=23),
            value=20.0,
        )
        self._runtime(now=self.DAY_START + timedelta(hours=23)).advance(
            now=self.DAY_START + timedelta(hours=23)
        )
        self._seed_source(
            event_id=watermark,
            observed_at=self.DAY_START + timedelta(days=1, minutes=1, seconds=1),
            value=20.0,
        )
        after_close = self.DAY_START + timedelta(days=1, minutes=2)

        self._runtime(now=after_close).advance(now=after_close)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT window_started_at, window_ended_at
                    FROM t_business_metric_projections
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                row = cursor.fetchone()
        self.assertEqual(
            row,
            (
                self.DAY_START + timedelta(days=1),
                self.DAY_START + timedelta(days=2),
            ),
        )

    def test_late_event_adds_corrected_revision_and_replay_adds_nothing(self) -> None:
        self._seed_closable_day()
        after_close = self.DAY_START + timedelta(days=1, minutes=2)
        runtime = self._runtime(now=after_close)
        runtime.advance(now=after_close)
        late_id = UUID("92000000-0000-0000-0000-000000000104")
        self._seed_source(
            event_id=late_id,
            observed_at=self.DAY_START + timedelta(hours=11, minutes=30),
            value=30.0,
            received_at=after_close + timedelta(seconds=1),
        )

        runtime.observe_committed((late_id,))
        corrected = runtime.advance(now=after_close + timedelta(seconds=2))
        runtime.observe_committed((late_id,))
        replay = runtime.advance(now=after_close + timedelta(seconds=3))

        self.assertEqual(corrected.corrected_count, 1)
        self.assertEqual(replay.corrected_count, 0)
        self.assertEqual(self._metric_counts(), (1, 2, 2, 5, 2))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT result.revision, result.lifecycle, observation.value_float
                    FROM t_business_metric_window_results AS result
                    JOIN t_l2_observations AS observation
                      ON observation.event_id = result.result_event_id
                     AND observation.observed_at = result.result_observed_at
                    WHERE result.installed_metric_id = %s
                    ORDER BY result.revision
                    """,
                    (self.installed.id,),
                )
                rows = cursor.fetchall()
        self.assertEqual(rows, [(1, "completed", 15.0), (2, "corrected", 22.5)])

    def test_restart_scans_received_watermark_when_late_receive_precedes_last_tick(self) -> None:
        self._seed_closable_day()
        after_close = self.DAY_START + timedelta(days=1, minutes=2)
        self._runtime(now=after_close).advance(now=after_close)
        late_id = UUID("92000000-0000-0000-0000-000000000105")
        self._seed_source(
            event_id=late_id,
            observed_at=self.DAY_START + timedelta(hours=11, minutes=30),
            value=30.0,
            received_at=after_close - timedelta(seconds=10),
        )

        receipt = self._runtime(now=after_close + timedelta(seconds=1)).advance(
            now=after_close + timedelta(seconds=1)
        )

        self.assertEqual(receipt.corrected_count, 1)
        self.assertEqual(receipt.error_count, 0)
        self.assertEqual(self._metric_counts(), (1, 2, 2, 5, 2))

    def test_untrusted_observed_at_falls_back_to_received_at_and_records_basis(self) -> None:
        first = UUID("92000000-0000-0000-0000-000000000021")
        second = UUID("92000000-0000-0000-0000-000000000022")
        watermark = UUID("92000000-0000-0000-0000-000000000023")
        self._seed_source(event_id=first, observed_at=self.DAY_START, value=10.0)
        received = self.DAY_START + timedelta(hours=12)
        self._seed_source(
            event_id=second,
            observed_at=received,
            received_at=received,
            value=20.0,
            event_time_basis="received_at",
        )
        self._seed_source(
            event_id=watermark,
            observed_at=self.DAY_START + timedelta(days=1, minutes=1, seconds=1),
            value=20.0,
        )
        after_close = self.DAY_START + timedelta(days=1, minutes=2)

        self._runtime(now=after_close).advance(now=after_close)

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source_summary -> 'timeBasis'
                    FROM t_business_metric_window_results
                    WHERE installed_metric_id = %s
                    ORDER BY revision DESC LIMIT 1
                    """,
                    (self.installed.id,),
                )
                time_basis = cursor.fetchone()[0]
        self.assertEqual(
            time_basis,
            {str(first): "observed_at", str(second): "received_at"},
        )

    def test_future_untrusted_original_time_does_not_reject_effective_source_order(self) -> None:
        first = UUID("92000000-0000-0000-0000-000000000026")
        last = UUID("92000000-0000-0000-0000-000000000027")
        watermark = UUID("92000000-0000-0000-0000-000000000028")
        first_effective = self.DAY_START + timedelta(hours=1)
        self._seed_source(
            event_id=first,
            observed_at=datetime(2036, 8, 23, tzinfo=UTC),
            received_at=first_effective,
            value=10.0,
            event_time_basis="received_at",
        )
        self._seed_source(
            event_id=last,
            observed_at=self.DAY_START + timedelta(hours=12),
            value=20.0,
        )
        self._seed_source(
            event_id=watermark,
            observed_at=self.DAY_START + timedelta(days=1, minutes=2),
            value=30.0,
        )
        now = self.DAY_START + timedelta(days=1, minutes=3)

        receipt = self._runtime(now=now).advance(now=now)

        self.assertEqual(receipt.completed_count, 1)
        self.assertEqual(receipt.error_count, 0)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT first_source_observed_at, first_source_effective_at,
                           last_source_observed_at, last_source_effective_at
                    FROM t_business_metric_window_results
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                row = cursor.fetchone()
        self.assertEqual(row[0], datetime(2036, 8, 23, tzinfo=UTC))
        self.assertEqual(row[1], first_effective)
        self.assertEqual(row[2], self.DAY_START + timedelta(hours=12))
        self.assertEqual(row[3], self.DAY_START + timedelta(hours=12))

    def test_frozen_counter_failure_does_not_switch_to_unbound_power(self) -> None:
        self._reset_and_install(method="counter_delta")
        frozen = UUID("92000000-0000-0000-0000-000000000031")
        self._seed_source(
            event_id=frozen,
            observed_at=self.DAY_START,
            value=None,
            quality=TrunkQuality.BAD,
        )
        unrelated_entity = uuid4()
        unrelated_event = UUID("92000000-0000-0000-0000-000000000032")
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_entity_instances
                      (id, device_instance_id, definition_id, display_name,
                       data_type, unit, direction, freshness_seconds, source_kind)
                    SELECT %s, device_instance_id, 'pv.active_power', 'PV power',
                           'FLOAT', 'kW', 'R', 5, 'legacy_tag'
                    FROM t_entity_instances WHERE id = %s
                    """,
                    (unrelated_entity, self.source_entity_id),
                )
        self._seed_source(
            event_id=unrelated_event,
            observed_at=self.DAY_START + timedelta(hours=12),
            value=99.0,
            entity_id=unrelated_entity,
        )

        self._runtime(now=self.DAY_START + timedelta(hours=13)).advance(
            now=self.DAY_START + timedelta(hours=13)
        )

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT quality, state ->> 'lifecycle', state ->> 'reason',
                           state -> 'sourceEventIds'
                    FROM t_business_metric_projections
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                row = cursor.fetchone()
        self.assertEqual(
            row[:3],
            (int(TrunkQuality.BAD), "provisional", "SOURCE_BAD"),
        )
        self.assertEqual(row[3], [str(frozen)])
        self.assertNotIn(str(unrelated_event), row[3])
        self.assertEqual(self._metric_counts()[2], 0)

    def test_frozen_counter_contracts_apply_16_32_64_bit_and_reset_rules(self) -> None:
        cases = (
            (16, "65535", False, True, 65530, 3, "9"),
            (32, "4294967295", False, True, 4294967290, 4, "10"),
            (64, "18446744073709551615", False, True, 10, 20, "10"),
            (16, "65535", True, False, 100, 5, "5"),
        )
        for index, (
            bit_width,
            maximum,
            reset_on_decrease,
            rollover_on_decrease,
            baseline,
            endpoint,
            expected,
        ) in enumerate(cases, start=1):
            with self.subTest(
                bit_width=bit_width,
                reset_on_decrease=reset_on_decrease,
            ):
                self._reset_and_install(
                    method="counter_delta",
                    source_data_type="INT",
                    counter_contract={
                        "maximum": maximum,
                        "bitWidth": bit_width,
                        "resetOnDecrease": reset_on_decrease,
                        "rolloverOnDecrease": rollover_on_decrease,
                    },
                )
                self._seed_source(
                    event_id=UUID(int=200 + index * 2),
                    observed_at=self.DAY_START - timedelta(seconds=1),
                    value=baseline,
                )
                self._seed_source(
                    event_id=UUID(int=201 + index * 2),
                    observed_at=self.DAY_START + timedelta(hours=12),
                    value=endpoint,
                )

                now = self.DAY_START + timedelta(hours=13)
                self._runtime(now=now).advance(now=now)

                with psycopg2.connect(**self.connection_kwargs) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT projection.state ->> 'value',
                                   binding.counter_bit_width,
                                   binding.counter_maximum::text,
                                   binding.counter_reset_on_decrease,
                                   binding.counter_rollover_on_decrease
                            FROM t_business_metric_projections AS projection
                            JOIN t_business_metric_source_bindings AS binding
                              ON binding.installed_metric_id = projection.installed_metric_id
                            WHERE projection.installed_metric_id = %s
                            """,
                            (self.installed.id,),
                        )
                        row = cursor.fetchone()
                self.assertEqual(row[0], expected)
                self.assertEqual(row[1], bit_width)
                self.assertEqual(row[2], maximum)
                self.assertEqual(row[3], reset_on_decrease)
                self.assertEqual(row[4], rollover_on_decrease)

    def test_invalid_closed_window_writes_ledger_without_l2_fact(self) -> None:
        self._reset_and_install(method="counter_delta")
        bad = UUID("92000000-0000-0000-0000-000000000041")
        watermark = UUID("92000000-0000-0000-0000-000000000042")
        self._seed_source(
            event_id=bad,
            observed_at=self.DAY_START,
            value=None,
            quality=TrunkQuality.BAD,
        )
        self._seed_source(
            event_id=watermark,
            observed_at=self.DAY_START + timedelta(days=1, minutes=1, seconds=1),
            value=100.0,
        )
        after_close = self.DAY_START + timedelta(days=1, minutes=2)

        receipt = self._runtime(now=after_close).advance(now=after_close)

        self.assertEqual(receipt.invalid_count, 1)
        self.assertEqual(self._metric_counts(), (1, 1, 0, 0, 0))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT lifecycle, quality, result_event_id,
                           source_summary -> 'sourceEventIds'
                    FROM t_business_metric_window_results
                    WHERE installed_metric_id = %s
                    """,
                    (self.installed.id,),
                )
                row = cursor.fetchone()
        self.assertEqual(row[:3], ("invalid", int(TrunkQuality.BAD), None))
        self.assertEqual(row[3], [str(bad)])

    def test_close_failure_at_each_stage_rolls_back_the_whole_transaction(self) -> None:
        for failed_stage in ("l2", "result", "source", "outbox", "checkpoint"):
            with self.subTest(failed_stage=failed_stage):
                self._reset_and_install()
                self._seed_closable_day()
                after_close = self.DAY_START + timedelta(days=1, minutes=2)

                receipt = self._runtime(
                    now=after_close,
                    failed_stage=failed_stage,
                ).advance(now=after_close)

                self.assertEqual(receipt.error_count, 1)
                self.assertEqual(self._metric_counts(), (0, 0, 0, 0, 0))
                with psycopg2.connect(**self.connection_kwargs) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT count(*) FROM t_l2_latest WHERE entity_instance_id = %s",
                            (self.output_entity_id,),
                        )
                        self.assertEqual(cursor.fetchone()[0], 0)

    def test_one_installation_failure_does_not_block_the_second_installation(self) -> None:
        self.fixture._seed_second_metric_template()
        second_plan = self.fixture._preview_template("ems.pv-energy-yesterday")
        second = self.fixture._delivery().apply(
            self.fixture._apply_command(second_plan, key="metric-second")
        )
        self._seed_closable_day()
        failures = 0

        def fail_first_l2(stage: str) -> None:
            nonlocal failures
            if stage == "l2" and failures == 0:
                failures += 1
                raise RuntimeError("first installation only")

        now = self.DAY_START + timedelta(days=1, minutes=2)
        receipt = MetricProjection(
            connection_factory=self._connection,
            clock=lambda: now,
            fault_hook=fail_first_l2,
        ).advance(now=now)

        self.assertEqual(receipt.error_count, 1)
        self.assertEqual(receipt.completed_count, 1)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT installed_metric_id, count(*)
                    FROM t_business_metric_window_results
                    WHERE installed_metric_id = ANY(%s::uuid[])
                    GROUP BY installed_metric_id
                    """,
                    ([str(self.installed.id), str(second.id)],),
                )
                rows = cursor.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0][0], {self.installed.id, second.id})
        self.assertEqual(rows[0][1], 1)


if __name__ == "__main__":
    unittest.main()
