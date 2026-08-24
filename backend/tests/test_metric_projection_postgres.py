from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import register_uuid

from app.services.data_trunk_contracts import TrunkQuality
from app.services.metric_projection_postgres import MetricProjection
from tests import test_data_trunk_migration_postgres as migration_support
from tests import test_business_metrics_postgres as business_metric_support


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL integration tests",
)
class MetricProjectionPostgresTest(unittest.TestCase):
    SOURCE_RUNTIME_ID = UUID("92000000-0000-0000-0000-000000000001")
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

    def _reset_and_install(self, *, method: str = "average") -> None:
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
            "window": {"kind": "aligned_daily"},
            "sources": [
                {
                    "method": method,
                    "entityDefinition": (
                        "pv.energy_total" if method == "counter_delta" else "pv.active_power"
                    ),
                    "priority": 1,
                }
            ],
            "quality": {"goodCoverage": 0.40, "minimumUsableCoverage": 0.20},
            "allowedLateness": "1m",
            "correction": {"automaticHorizon": "7d"},
            "capabilities": {"controlEligible": False},
        }
        fixture._seed_site_and_template()
        if method != "counter_delta":
            with psycopg2.connect(**self.connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE t_entity_instances
                        SET definition_id = 'pv.active_power', unit = 'kW',
                            freshness_seconds = 86400
                        WHERE id = %s
                        """,
                        (fixture.COUNTER_ID,),
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
    ) -> None:
        received = received_at or observed_at + timedelta(seconds=1)
        digest = hashlib.sha256(str(event_id).encode("ascii")).hexdigest()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_l2_observations
                      (observed_at, event_id, entity_instance_id, received_at,
                       calculated_at, value_float, quality, reason,
                       processing_revision_id, site_configuration_version,
                       source_digest, source_order_key,
                       producing_runtime_instance_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s)
                    """,
                    (
                        observed_at,
                        event_id,
                        entity_id or self.source_entity_id,
                        received,
                        max(received, observed_at),
                        value,
                        int(quality),
                        None if quality is TrunkQuality.GOOD else "SOURCE_BAD",
                        self.installed.processing_revision_id,
                        self.installed.site_configuration_version,
                        digest,
                        f"S:{event_id.int:032d}",
                        self.SOURCE_RUNTIME_ID,
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

        receipt = runtime.observe_committed((first, second))

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

        corrected = runtime.observe_committed((late_id,))
        replay = runtime.observe_committed((late_id,))

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
        self.assertEqual(row[:3], (int(TrunkQuality.BAD), "invalid", "SOURCE_BAD"))
        self.assertEqual(row[3], [str(frozen)])
        self.assertNotIn(str(unrelated_event), row[3])
        self.assertEqual(self._metric_counts()[2], 0)

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

                with self.assertRaises(RuntimeError):
                    self._runtime(
                        now=after_close,
                        failed_stage=failed_stage,
                    ).advance(now=after_close)

                self.assertEqual(self._metric_counts(), (0, 0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
