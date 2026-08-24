from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import os
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid5, NAMESPACE_URL

import psycopg2

from app.services.data_trunk import DataTrunk
from app.services.data_trunk_contracts import (
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.data_trunk_postgres import PostgresDataTrunkRepository
from tests.test_neuron_point_processing_catalog import FakeNeuron
from tests import test_data_trunk_migration_postgres as migration_support
from tests import test_point_processing_postgres as point_processing_support


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run EN9 acceptance tests",
)
class EN9PointProcessingAcceptancePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("EN9 acceptance tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        from app.services.telemetry_store import close_db_pool, init_db_pool

        close_db_pool()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration_support.DataTrunkMigrationPostgresTest._reset_through_037(cursor)
                migration_support.DataTrunkMigrationPostgresTest._apply_038(cursor)
                migration_support.DataTrunkMigrationPostgresTest._apply_039(cursor)
                migration_support.DataTrunkMigrationPostgresTest._apply_040(cursor)
                migration_support.DataTrunkMigrationPostgresTest._apply_041(cursor)
        init_db_pool(min_conn=1, max_conn=4)

    @classmethod
    def tearDownClass(cls) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    @contextmanager
    def _connection(self):
        connection = psycopg2.connect(**self.connection_kwargs)
        try:
            yield connection
        finally:
            connection.close()

    def test_en9_acceptance_rejects_a_client_declared_short_window(self) -> None:
        from app.services.en9_point_processing_acceptance import run_en9_acceptance

        with self.assertRaisesRegex(ValueError, "EN9_ACCEPTANCE_WINDOW_TOO_SHORT"):
            run_en9_acceptance(
                UUID("85000000-0000-0000-0000-000000000999"),
                0,
                connection_factory=self._connection,
            )

    def test_en9_report_requires_all_ninety_sources_and_three_l2_entities(self) -> None:
        from app.services.en9_point_processing_acceptance import run_en9_acceptance
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingDelivery,
            PreviewPointProcessing,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
        )

        helper = point_processing_support.PointProcessingPostgresTest(
            methodName="test_package_import_persists_complete_versioned_catalog_atomically"
        )
        helper.connection_kwargs = self.connection_kwargs
        package, brand_a_revision, _ = helper._import_reference_package()
        node_id = UUID("85000000-0000-0000-0000-000000000001")
        helper._seed_brand_a_site(
            package.id,
            package.digest,
            brand_a_revision,
            node_id,
            {
                "active_power": UUID("85000000-0000-0000-0000-000000000101"),
                "operating_state": UUID("85000000-0000-0000-0000-000000000102"),
                "fault_codes": UUID("85000000-0000-0000-0000-000000000103"),
            },
        )
        catalog = PostgresPointProcessingCatalog()
        en9_revision = next(
            item.revision_id for item in catalog.list_templates("PCS")
            if item.asset.asset_id == "pcs.en9"
        )
        service = PointProcessingDelivery(
            PostgresPointProcessingRepository(),
            catalog,
            point_scanner=NeuronPointCatalog(FakeNeuron()),
        )
        plan = service.preview(
            PreviewPointProcessing(node_id, en9_revision, {}, "user:acceptance")
        )
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id, plan.digest, "en9-acceptance", "user:acceptance"
            )
        )

        observed_at = datetime.now(UTC)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, name, value_data_type, unit, source_address
                    FROM t_tags
                    WHERE node_id = %s AND source_address IS NOT NULL
                    ORDER BY source_address
                    """,
                    (str(node_id),),
                )
                sources = cursor.fetchall()
        observations = []
        for sequence, (tag_id, name, data_type, unit, address) in enumerate(sources, 1):
            if address == "1!424634":
                value = TypedValue.float(25.0)
            elif address == "1!424669":
                value = TypedValue(ValueKind.INT, 2)
            else:
                value = TypedValue(ValueKind.BOOL, False)
            digest = hashlib.sha256(f"{address}:{sequence}".encode()).hexdigest()
            observations.append(
                RawObservation(
                    observation_id=uuid5(NAMESPACE_URL, f"en9:{address}"),
                    node_id=node_id,
                    tag_id=UUID(str(tag_id)),
                    source_key=name,
                    value=value,
                    raw_unit=unit,
                    quality=TrunkQuality.GOOD,
                    source_timestamp=observed_at,
                    received_at=observed_at + timedelta(milliseconds=100),
                    source_message_id="en9-acceptance-snapshot",
                    source_sequence=sequence,
                    source_digest=digest,
                    event_time_basis="observed_at",
                )
            )
        receipt = DataTrunk(
            PostgresDataTrunkRepository(
                clock=lambda: observed_at + timedelta(seconds=1)
            )
        ).ingest(tuple(observations))
        self.assertEqual((90, 3), (receipt.accepted_l0_count, len(receipt.l2_event_ids)))

        runtime_ids = (
            UUID("85000000-0000-0000-0000-000000000201"),
            UUID("85000000-0000-0000-0000-000000000202"),
        )
        user_id = UUID("85000000-0000-0000-0000-000000000203")
        session_id = UUID("85000000-0000-0000-0000-000000000204")
        from app.services.data_trunk_outbox import OutboxEvent
        from app.services.en9_point_processing_acceptance import (
            PostgresEN9StreamEvidence,
        )
        stream_evidence = PostgresEN9StreamEvidence(self._connection)
        stream_evidence.register_runtime(runtime_ids[0])
        binding = stream_evidence.bind(
            application.id,
            application.output_entity_instance_ids,
            SimpleNamespace(user_id=user_id, session_id=session_id),
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_id, entity_instance_id, payload
                    FROM t_l2_stream_outbox
                    WHERE event_id = ANY(%s::uuid[])
                    ORDER BY entity_instance_id
                    """,
                    ([str(item) for item in receipt.l2_event_ids],),
                )
                committed_events = cursor.fetchall()
        for event_id, entity_id, payload in committed_events:
            stream_evidence.record_acknowledgement(
                binding,
                OutboxEvent(UUID(str(event_id)), UUID(str(entity_id)), payload),
                runtime_ids[0],
            )

        generated_at = observed_at + timedelta(seconds=1800)
        premature = run_en9_acceptance(
            application.id,
            1800,
            connection_factory=self._connection,
            clock=lambda: generated_at,
        )
        self.assertFalse(premature.passed)
        premature_checks = {item.code: item.passed for item in premature.checks}
        self.assertFalse(premature_checks["EN9_L2_CONTINUOUS_HISTORY"])
        self.assertFalse(premature_checks["EN9_AUTHENTICATED_WS_RECEIPTS"])
        self.assertFalse(premature_checks["EN9_RESTART_CONTINUITY"])

        for minute in range(1, 30):
            point_at = observed_at + timedelta(minutes=minute)
            minute_observations = tuple(
                replace(
                    observation,
                    observation_id=uuid5(
                        NAMESPACE_URL,
                        f"en9:minute:{minute}:{observation.tag_id}",
                    ),
                    source_timestamp=point_at,
                    received_at=point_at,
                    source_message_id=f"en9-acceptance-minute-{minute}",
                    source_sequence=minute * 1000 + index,
                    source_digest=hashlib.sha256(
                        f"minute:{minute}:{observation.tag_id}".encode()
                    ).hexdigest(),
                )
                for index, observation in enumerate(observations, 1)
            )
            minute_receipt = DataTrunk(
                PostgresDataTrunkRepository(clock=lambda now=point_at: now)
            ).ingest(minute_observations)
            self.assertEqual(3, len(minute_receipt.l2_event_ids))

        final_observations = tuple(
            replace(
                observation,
                observation_id=uuid5(
                    NAMESPACE_URL,
                    f"en9:final:{observation.tag_id}",
                ),
                source_timestamp=generated_at,
                received_at=generated_at,
                source_message_id="en9-acceptance-final",
                source_sequence=1000 + index,
                source_digest=hashlib.sha256(
                    f"final:{observation.tag_id}".encode()
                ).hexdigest(),
            )
            for index, observation in enumerate(observations, 1)
        )
        final_receipt = DataTrunk(
            PostgresDataTrunkRepository(
                clock=lambda: generated_at,
            )
        ).ingest(final_observations)
        self.assertEqual(90, final_receipt.accepted_l0_count)
        self.assertEqual(3, len(final_receipt.l2_event_ids))

        with self._connection() as connection:
            with connection.cursor() as cursor:
                for runtime_id, started_at in (
                    (runtime_ids[0], observed_at),
                    (runtime_ids[1], observed_at + timedelta(minutes=15)),
                ):
                    cursor.execute(
                        """
                        INSERT INTO t_runtime_instances
                          (id, started_at, platform_version)
                        VALUES (%s, %s, '0.4.82')
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (str(runtime_id), started_at),
                    )
                cursor.execute("ALTER TABLE t_l2_observations DISABLE TRIGGER USER")
                cursor.execute("ALTER TABLE t_l2_latest DISABLE TRIGGER USER")
                cursor.execute(
                    """
                    UPDATE t_l2_observations
                    SET producing_runtime_instance_id = CASE
                      WHEN calculated_at < %s THEN %s::uuid ELSE %s::uuid END
                    WHERE calculated_at BETWEEN %s AND %s
                    """,
                    (
                        observed_at + timedelta(minutes=15),
                        str(runtime_ids[0]), str(runtime_ids[1]),
                        observed_at, generated_at,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE t_l2_latest
                    SET producing_runtime_instance_id = %s
                    WHERE processing_revision_id IS NOT NULL
                    """,
                    (str(runtime_ids[1]),),
                )
                cursor.execute("ALTER TABLE t_l2_observations ENABLE TRIGGER USER")
                cursor.execute("ALTER TABLE t_l2_latest ENABLE TRIGGER USER")
                cursor.execute(
                    """
                    SELECT event_id, entity_instance_id, calculated_at
                    FROM t_l2_observations
                    WHERE calculated_at >= %s AND calculated_at < %s
                    ORDER BY calculated_at, entity_instance_id
                    """,
                    (observed_at, observed_at + timedelta(minutes=15)),
                )
                first_runtime_events = cursor.fetchall()
                for event_id, entity_id, delivered_at in first_runtime_events:
                    receipt_id = uuid5(
                        NAMESPACE_URL,
                        f"receipt:{event_id}:{runtime_ids[0]}",
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_en9_acceptance_ws_receipts
                          (id, application_id, event_id, entity_instance_id,
                           user_id, session_id, runtime_instance_id, delivered_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            str(receipt_id), str(application.id), str(event_id),
                            str(entity_id), str(user_id), str(session_id),
                            str(runtime_ids[0]), delivered_at,
                        ),
                    )
                for minute in range(15):
                    delivered_at = observed_at + timedelta(minutes=minute)
                    cursor.execute(
                        """
                        INSERT INTO t_runtime_health_samples
                          (runtime_instance_id, sampled_at, pipeline_running,
                           mqtt_connected, last_message_at)
                        VALUES (%s, %s, TRUE, TRUE, %s)
                        """,
                        (str(runtime_ids[0]), delivered_at, delivered_at),
                    )
            connection.commit()

        single_runtime = run_en9_acceptance(
            application.id,
            1800,
            connection_factory=self._connection,
            clock=lambda: generated_at,
        )
        single_runtime_checks = {
            item.code: item.passed for item in single_runtime.checks
        }
        self.assertFalse(single_runtime.passed)
        self.assertTrue(single_runtime_checks["EN9_L2_CONTINUOUS_HISTORY"])
        self.assertFalse(single_runtime_checks["EN9_AUTHENTICATED_WS_RECEIPTS"])
        self.assertFalse(single_runtime_checks["EN9_RESTART_CONTINUITY"])

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_id, entity_instance_id, calculated_at
                    FROM t_l2_observations
                    WHERE calculated_at >= %s AND calculated_at <= %s
                    ORDER BY calculated_at, entity_instance_id
                    """,
                    (observed_at + timedelta(minutes=15), generated_at),
                )
                second_runtime_events = cursor.fetchall()
                for event_id, entity_id, delivered_at in second_runtime_events:
                    receipt_id = uuid5(
                        NAMESPACE_URL,
                        f"receipt:{event_id}:{runtime_ids[1]}",
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_en9_acceptance_ws_receipts
                          (id, application_id, event_id, entity_instance_id,
                           user_id, session_id, runtime_instance_id, delivered_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            str(receipt_id), str(application.id), str(event_id),
                            str(entity_id), str(user_id), str(session_id),
                            str(runtime_ids[1]), delivered_at,
                        ),
                    )
                for minute in range(15, 31):
                    delivered_at = observed_at + timedelta(minutes=minute)
                    cursor.execute(
                        """
                        INSERT INTO t_runtime_health_samples
                          (runtime_instance_id, sampled_at, pipeline_running,
                           mqtt_connected, last_message_at)
                        VALUES (%s, %s, TRUE, TRUE, %s)
                        """,
                        (str(runtime_ids[1]), delivered_at, delivered_at),
                    )
            connection.commit()

        report = run_en9_acceptance(
            application.id,
            1800,
            connection_factory=self._connection,
            clock=lambda: generated_at,
        )

        self.assertEqual(90, report.required_input_count)
        self.assertEqual(3, report.output_entity_count)
        self.assertTrue(report.passed, report.public_dict())
        from app.services.en9_point_processing_acceptance import (
            get_en9_acceptance_report,
        )
        persisted = get_en9_acceptance_report(
            report.id,
            connection_factory=self._connection,
        )
        self.assertEqual(report.digest, persisted["digest"])
        self.assertTrue(persisted["passed"])

        def check_passed(code: str) -> bool:
            checked = run_en9_acceptance(
                application.id,
                1800,
                connection_factory=self._connection,
                clock=lambda: generated_at,
            )
            return {item.code: item.passed for item in checked.checks}[code]

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (latest.event_id)
                           source.l0_observation_id, l0.tag_id,
                           (
                             SELECT binding.l0_tag_id
                             FROM t_point_processing_input_bindings AS binding
                             JOIN t_point_processing_inputs AS input
                               ON input.id = binding.input_id
                             WHERE binding.installed_processing_id = %s
                               AND input.input_key = 'active_power_raw'
                           )
                    FROM t_point_processing_output_bindings AS binding
                    JOIN t_point_processing_outputs AS output
                      ON output.id = binding.output_id
                    JOIN t_l2_observations AS latest
                      ON latest.entity_instance_id = binding.entity_instance_id
                    JOIN t_l2_observation_sources AS source
                      ON source.l2_event_id = latest.event_id
                     AND source.l2_observed_at = latest.observed_at
                    JOIN t_l0_observation_dedup AS l0
                      ON l0.observation_id = source.l0_observation_id
                    WHERE binding.installed_processing_id = %s
                      AND output.entity_definition_id = 'pcs.fault_codes'
                    ORDER BY latest.event_id, source.l0_observation_id
                    """,
                    (str(application.installed_processing_id),) * 2,
                )
                corrupted_sources = cursor.fetchall()
                repeated_fault_tag_id = corrupted_sources[0][1]
                cursor.execute(
                    "ALTER TABLE t_l0_observation_dedup DISABLE TRIGGER USER"
                )
                cursor.execute(
                    "ALTER TABLE t_telemetry DISABLE TRIGGER USER"
                )
                for source_id, _original_tag_id, _wrong_tag_id in corrupted_sources:
                    cursor.execute(
                        "UPDATE t_l0_observation_dedup SET tag_id = %s "
                        "WHERE observation_id = %s",
                        (str(repeated_fault_tag_id), str(source_id)),
                    )
                    cursor.execute(
                        "UPDATE t_telemetry SET tag_id = %s "
                        "WHERE observation_id = %s",
                        (str(repeated_fault_tag_id), str(source_id)),
                    )
            connection.commit()
        self.assertFalse(check_passed("EN9_FAULT_SOURCE_EVIDENCE"))
        self.assertFalse(check_passed("EN9_L2_CONTINUOUS_HISTORY"))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                for source_id, original_tag_id, _wrong_tag_id in corrupted_sources:
                    cursor.execute(
                        "UPDATE t_l0_observation_dedup SET tag_id = %s "
                        "WHERE observation_id = %s",
                        (str(original_tag_id), str(source_id)),
                    )
                    cursor.execute(
                        "UPDATE t_telemetry SET tag_id = %s "
                        "WHERE observation_id = %s",
                        (str(original_tag_id), str(source_id)),
                    )
                cursor.execute(
                    "ALTER TABLE t_telemetry ENABLE TRIGGER USER"
                )
                cursor.execute(
                    "ALTER TABLE t_l0_observation_dedup ENABLE TRIGGER USER"
                )
            connection.commit()

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE t_l2_observations DISABLE TRIGGER USER")
                cursor.execute(
                    "UPDATE t_l2_observations SET quality = 64 WHERE calculated_at <= %s",
                    (generated_at,),
                )
            connection.commit()
        self.assertFalse(check_passed("EN9_L2_CONTINUOUS_HISTORY"))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE t_l2_observations SET quality = 192 WHERE calculated_at <= %s",
                    (generated_at,),
                )
                cursor.execute("ALTER TABLE t_l2_observations ENABLE TRIGGER USER")
            connection.commit()

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE t_l2_observations DISABLE TRIGGER USER")
                cursor.execute(
                    "UPDATE t_l2_observations SET calculated_at = calculated_at - INTERVAL '1 day'"
                )
            connection.commit()
        self.assertFalse(check_passed("EN9_L2_CONTINUOUS_HISTORY"))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE t_l2_observations SET calculated_at = calculated_at + INTERVAL '1 day'"
                )
                cursor.execute("ALTER TABLE t_l2_observations ENABLE TRIGGER USER")
            connection.commit()

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE t_runtime_instances DISABLE TRIGGER USER")
                cursor.execute(
                    "UPDATE t_runtime_instances SET started_at = %s WHERE id = %s",
                    (observed_at, str(runtime_ids[1])),
                )
            connection.commit()
        self.assertFalse(check_passed("EN9_RESTART_CONTINUITY"))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE t_runtime_instances SET started_at = %s WHERE id = %s",
                    (observed_at + timedelta(minutes=15), str(runtime_ids[1])),
                )
                cursor.execute("ALTER TABLE t_runtime_instances ENABLE TRIGGER USER")
            connection.commit()

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE t_runtime_health_samples DISABLE TRIGGER USER")
                cursor.execute(
                    "UPDATE t_runtime_health_samples SET mqtt_connected = FALSE"
                )
            connection.commit()
        self.assertFalse(check_passed("EN9_RUNTIME_MQTT_COLLECTION_HEALTH"))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE t_runtime_health_samples SET mqtt_connected = TRUE"
                )
                cursor.execute("ALTER TABLE t_runtime_health_samples ENABLE TRIGGER USER")
            connection.commit()

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE t_l2_latest
                    SET value_float = value_float * 10
                    WHERE entity_instance_id = %s
                    """,
                    ("85000000-0000-0000-0000-000000000101",),
                )
                cursor.execute(
                    """
                    UPDATE t_l2_latest
                    SET value_text = 'STOPPED'
                    WHERE entity_instance_id = %s
                    """,
                    ("85000000-0000-0000-0000-000000000102",),
                )
            connection.commit()
        mismatched = run_en9_acceptance(
            application.id,
            1800,
            connection_factory=self._connection,
            clock=lambda: generated_at,
        )
        mismatch_checks = {item.code: item.passed for item in mismatched.checks}
        self.assertFalse(mismatched.passed)
        self.assertFalse(mismatch_checks["EN9_POWER_VALUE"])
        self.assertFalse(mismatch_checks["EN9_STATE_ENUM"])
        from app.services.en9_point_processing_acceptance import (
            get_latest_en9_acceptance_state,
        )
        restored = get_latest_en9_acceptance_state(
            node_id,
            connection_factory=self._connection,
        )
        self.assertEqual(str(application.id), restored["application"]["id"])
        self.assertEqual(mismatched.digest, restored["latest_report"]["digest"])


if __name__ == "__main__":
    unittest.main()
