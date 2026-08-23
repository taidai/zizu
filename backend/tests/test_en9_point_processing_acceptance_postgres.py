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
            stream_evidence.record_delivery(
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

        final_observations = tuple(
            replace(
                observation,
                observation_id=uuid5(
                    NAMESPACE_URL,
                    f"en9:final:{observation.tag_id}",
                ),
                source_timestamp=generated_at - timedelta(seconds=1),
                received_at=generated_at - timedelta(milliseconds=500),
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
        self.assertEqual(
            (90, 3),
            (final_receipt.accepted_l0_count, len(final_receipt.l2_event_ids)),
        )

        with self._connection() as connection:
            with connection.cursor() as cursor:
                for runtime_id in runtime_ids:
                    cursor.execute(
                        """
                        INSERT INTO t_runtime_instances
                          (id, started_at, platform_version)
                        VALUES (%s, %s, '0.4.82')
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (str(runtime_id), observed_at),
                    )
                for minute in range(1, 30):
                    point_at = observed_at + timedelta(minutes=minute)
                    for entity_id in (
                        UUID("85000000-0000-0000-0000-000000000101"),
                        UUID("85000000-0000-0000-0000-000000000102"),
                        UUID("85000000-0000-0000-0000-000000000103"),
                    ):
                        event_id = uuid5(
                            NAMESPACE_URL,
                            f"en9:history:{minute}:{entity_id}",
                        )
                        cursor.execute(
                            """
                            INSERT INTO t_l2_observations
                              (observed_at, event_id, entity_instance_id,
                               received_at, calculated_at, value_float,
                               value_int, value_bool, value_text, value_codes,
                               quality, reason, processing_revision_id,
                               site_configuration_version, source_digest,
                               source_order_key)
                            SELECT %s, %s, entity_instance_id, %s, %s,
                                   value_float, value_int, value_bool,
                                   value_text, value_codes, quality, reason,
                                   processing_revision_id,
                                   site_configuration_version,
                                   source_digest, %s
                            FROM t_l2_latest WHERE entity_instance_id = %s
                            """,
                            (
                                point_at, str(event_id), point_at, point_at,
                                f"acceptance:{minute:02d}:{entity_id}",
                                str(entity_id),
                            ),
                        )
                for minute in range(31):
                    delivered_at = observed_at + timedelta(minutes=minute)
                    runtime_id = runtime_ids[0]
                    for entity_id in (
                        UUID("85000000-0000-0000-0000-000000000101"),
                        UUID("85000000-0000-0000-0000-000000000102"),
                        UUID("85000000-0000-0000-0000-000000000103"),
                    ):
                        event_id = uuid5(
                            NAMESPACE_URL,
                            f"en9:receipt-event:{minute}:{entity_id}",
                        )
                        cursor.execute(
                            """
                            INSERT INTO t_en9_acceptance_ws_receipts
                              (id, application_id, event_id,
                               entity_instance_id, user_id, session_id,
                               runtime_instance_id, delivered_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                str(uuid5(NAMESPACE_URL, f"receipt:{event_id}")),
                                str(application.id), str(event_id), str(entity_id),
                                str(user_id), str(session_id), str(runtime_id),
                                delivered_at,
                            ),
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
        self.assertTrue(single_runtime_checks["EN9_AUTHENTICATED_WS_RECEIPTS"])
        self.assertFalse(single_runtime_checks["EN9_RESTART_CONTINUITY"])

        with self._connection() as connection:
            with connection.cursor() as cursor:
                for entity_id in (
                    UUID("85000000-0000-0000-0000-000000000101"),
                    UUID("85000000-0000-0000-0000-000000000102"),
                    UUID("85000000-0000-0000-0000-000000000103"),
                ):
                    event_id = uuid5(
                        NAMESPACE_URL,
                        f"en9:restart-proof:{entity_id}",
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_en9_acceptance_ws_receipts
                          (id, application_id, event_id, entity_instance_id,
                           user_id, session_id, runtime_instance_id,
                           delivered_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            str(uuid5(NAMESPACE_URL, f"receipt:{event_id}")),
                            str(application.id), str(event_id), str(entity_id),
                            str(user_id), str(session_id), str(runtime_ids[1]),
                            generated_at,
                        ),
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
