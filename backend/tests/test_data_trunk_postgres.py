from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
import unittest
from uuid import UUID

import psycopg2

from app.services.data_trunk import DataTrunk, _FreshnessScheduler
from app.services.data_trunk_contracts import (
    DataTrunkError,
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.data_trunk_postgres import PostgresDataTrunkRepository
from tests.test_alarm_configuration_postgres import (
    _PostgresAlarmConfigurationTestBase,
)
from tests import test_data_trunk_migration_postgres as migration_support


NODE_ID = UUID("00000000-0000-0000-0000-000000000001")
TAG_ID = UUID("00000000-0000-0000-0000-000000000011")
CONVERSION_ID = UUID("00000000-0000-0000-0000-000000000201")
REVISION_ID = UUID("00000000-0000-0000-0000-000000000202")
ENTITY_ID = UUID("00000000-0000-0000-0000-000000000301")
STATE_TAG_ID = UUID("00000000-0000-0000-0000-000000000012")
FAULT_TAG_ID = UUID("00000000-0000-0000-0000-000000000013")
STATE_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000303")
FAULT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000304")
EXPECTED_EVENT_ID = UUID("c5320566-2b3d-50c5-b320-bf082d7533f3")


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL integration tests",
)
class DataTrunkPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Data trunk tests require a *_test database")
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
                migration_support.DataTrunkMigrationPostgresTest._reset_through_037(
                    cursor
                )
                migration_support.DataTrunkMigrationPostgresTest._apply_038(cursor)
                self._seed_installed_numeric_conversion(cursor)
        self.repository = PostgresDataTrunkRepository(
            connection_factory=self._connection,
            clock=lambda: datetime(2026, 8, 17, 1, tzinfo=UTC),
        )
        self.trunk = DataTrunk(self.repository)

    @contextmanager
    def _connection(self):
        connection = psycopg2.connect(**self.connection_kwargs)
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _seed_installed_numeric_conversion(cursor) -> None:
        installation_id, _ = (
            _PostgresAlarmConfigurationTestBase._insert_installed_site(cursor)
        )
        cursor.execute(
            """
            INSERT INTO t_nodes (id, name, source_catalog_key)
            VALUES (%s, 'PCS-01', 'PCS-01');
            INSERT INTO t_tags
              (id, node_id, name, data_type, unit, read_write, enabled)
            VALUES (%s, %s, 'ActivePowerRaw', 'FLOAT', 'W', 'R', TRUE);
            INSERT INTO t_device_instances
              (id, identity_installation_id, slot_id, instance_key,
               device_category, display_name, node_id)
            VALUES (
              '00000000-0000-0000-0000-000000000302',
              %s,
              'pcs.primary',
              'PCS-01',
              'pcs',
              'PCS 01',
              %s
            );
            INSERT INTO t_entity_instances
              (id, device_instance_id, definition_id, display_name,
               data_type, unit, direction, freshness_seconds, source_kind)
            VALUES (
              %s,
              '00000000-0000-0000-0000-000000000302',
              'pcs.active_power',
              'PCS 01 有功功率',
              'FLOAT',
              'kW',
              'R',
              30,
              'point_conversion'
            );
            INSERT INTO t_point_conversion_templates
              (id, asset_id, device_category, brand, model,
               display_name, status)
            VALUES (
              '00000000-0000-0000-0000-000000000205',
              'pcs.brand-a',
              'pcs',
              'Brand A',
              'PCS-A',
              'Brand A PCS',
              'active'
            );
            INSERT INTO t_point_conversion_revisions
              (id, template_id, revision, content_digest, published_at)
            VALUES (
              %s,
              '00000000-0000-0000-0000-000000000205',
              1,
              %s,
              '2026-08-17T00:00:00Z'
            );
            INSERT INTO t_point_conversion_inputs
              (id, revision_id, input_key, source_kind, data_type, unit,
               required, stable_source_key, aliases)
            VALUES (
              '00000000-0000-0000-0000-000000000207',
              %s,
              'active_power_raw',
              'l0',
              'FLOAT',
              'W',
              TRUE,
              'ActivePowerRaw',
              ARRAY['ActivePower']
            );
            INSERT INTO t_point_conversion_outputs
              (id, revision_id, output_key, entity_definition_id,
               data_type, unit, freshness_seconds)
            VALUES (
              '00000000-0000-0000-0000-000000000208',
              %s,
              'active_power',
              'pcs.active_power',
              'FLOAT',
              'kW',
              30
            );
            INSERT INTO t_numeric_transform_rules
              (output_id, input_id, scale, "offset", minimum, maximum)
            VALUES (
              '00000000-0000-0000-0000-000000000208',
              '00000000-0000-0000-0000-000000000207',
              0.001,
              0,
              -500,
              500
            );
            INSERT INTO t_point_conversion_plans
              (id, node_id, template_revision_id,
               entity_identity_installation_id, solution_installation_id,
               base_site_configuration_version, source_catalog_digest,
               status, items, blockers, digest, planned_by)
            VALUES (
              '00000000-0000-0000-0000-000000000206',
              %s,
              %s,
              %s,
              %s,
              0,
              %s,
              'applied',
              '[]',
              '[]',
              %s,
              'user:installer'
            );
            INSERT INTO t_installed_point_conversions
              (id, node_id, revision_id, source_plan_id,
               solution_installation_id, site_configuration_version,
               installed_by, current)
            VALUES (
              %s,
              %s,
              %s,
              '00000000-0000-0000-0000-000000000206',
              %s,
              1,
              'user:installer',
              TRUE
            );
            INSERT INTO t_conversion_input_bindings
              (installed_conversion_id, input_id, source_kind, l0_tag_id,
               confirmed_by)
            VALUES (
              %s,
              '00000000-0000-0000-0000-000000000207',
              'l0',
              %s,
              'user:installer'
            );
            INSERT INTO t_conversion_output_bindings
              (installed_conversion_id, output_id, entity_instance_id)
            VALUES (
              %s,
              '00000000-0000-0000-0000-000000000208',
              %s
            )
            """,
            (
                str(NODE_ID),
                str(TAG_ID),
                str(NODE_ID),
                installation_id,
                str(NODE_ID),
                str(ENTITY_ID),
                str(REVISION_ID),
                "c" * 64,
                str(REVISION_ID),
                str(REVISION_ID),
                str(NODE_ID),
                str(REVISION_ID),
                installation_id,
                installation_id,
                "d" * 64,
                "e" * 64,
                str(CONVERSION_ID),
                str(NODE_ID),
                str(REVISION_ID),
                installation_id,
                str(CONVERSION_ID),
                str(TAG_ID),
                str(CONVERSION_ID),
                str(ENTITY_ID),
            ),
        )

    @staticmethod
    def raw_power(
        value: float,
        *,
        sequence: int,
        observed_at: datetime | None = None,
    ) -> RawObservation:
        observed_at = observed_at or datetime(2026, 8, 17, tzinfo=UTC)
        digest_character = chr(ord("a") + sequence - 1)
        return RawObservation(
            observation_id=UUID(int=0x100 + sequence),
            node_id=NODE_ID,
            tag_id=TAG_ID,
            source_key="ActivePowerRaw",
            value=TypedValue.float(value),
            raw_unit="W",
            quality=TrunkQuality.GOOD,
            source_timestamp=observed_at,
            received_at=observed_at + timedelta(seconds=1),
            source_message_id=f"message-{sequence}",
            source_sequence=sequence,
            source_digest=digest_character * 64,
        )

    @staticmethod
    def raw_text(
        *,
        tag_id: UUID,
        source_key: str,
        value: str,
        sequence: int,
    ) -> RawObservation:
        observed_at = datetime(2026, 8, 17, tzinfo=UTC)
        digest_character = chr(ord("a") + sequence - 1)
        return RawObservation(
            observation_id=UUID(int=0x200 + sequence),
            node_id=NODE_ID,
            tag_id=tag_id,
            source_key=source_key,
            value=TypedValue(ValueKind.STRING, value),
            raw_unit=None,
            quality=TrunkQuality.GOOD,
            source_timestamp=observed_at,
            received_at=observed_at + timedelta(seconds=1),
            source_message_id=f"message-{sequence}",
            source_sequence=sequence,
            source_digest=digest_character * 64,
        )

    @staticmethod
    def _seed_enum_and_fault_conversions(cursor) -> None:
        cursor.execute(
            """
            INSERT INTO t_tags
              (id, node_id, name, data_type, unit, read_write, enabled)
            VALUES
              (%s, %s, 'OperatingStateRaw', 'STRING', NULL, 'R', TRUE),
              (%s, %s, 'FaultCodesRaw', 'STRING', NULL, 'R', TRUE);
            INSERT INTO t_entity_instances
              (id, device_instance_id, definition_id, display_name,
               data_type, unit, direction, freshness_seconds, source_kind)
            VALUES
              (%s, '00000000-0000-0000-0000-000000000302',
               'pcs.operating_state', 'PCS 01 运行状态',
               'ENUM', NULL, 'R', 30, 'point_conversion'),
              (%s, '00000000-0000-0000-0000-000000000302',
               'pcs.fault_codes', 'PCS 01 故障码',
               'CODE_SET', NULL, 'R', 30, 'point_conversion');
            INSERT INTO t_point_conversion_inputs
              (id, revision_id, input_key, source_kind, data_type, unit,
               required, stable_source_key, aliases)
            VALUES
              ('00000000-0000-0000-0000-000000000209', %s,
               'operating_state_raw', 'l0', 'STRING', NULL, TRUE,
               'OperatingStateRaw', '{}'),
              ('00000000-0000-0000-0000-000000000210', %s,
               'fault_codes_raw', 'l0', 'STRING', NULL, TRUE,
               'FaultCodesRaw', '{}');
            INSERT INTO t_point_conversion_outputs
              (id, revision_id, output_key, entity_definition_id,
               data_type, unit, freshness_seconds)
            VALUES
              ('00000000-0000-0000-0000-000000000211', %s,
               'operating_state', 'pcs.operating_state',
               'ENUM', NULL, 30),
              ('00000000-0000-0000-0000-000000000212', %s,
               'fault_codes', 'pcs.fault_codes',
               'CODE_SET', NULL, 30);
            INSERT INTO t_enum_transform_rules (output_id, input_id)
            VALUES (
              '00000000-0000-0000-0000-000000000211',
              '00000000-0000-0000-0000-000000000209'
            );
            INSERT INTO t_fault_code_transform_rules
              (output_id, input_id, delimiter)
            VALUES (
              '00000000-0000-0000-0000-000000000212',
              '00000000-0000-0000-0000-000000000210',
              'semicolon'
            );
            INSERT INTO t_enum_mapping_entries
              (output_id, raw_value, canonical_value)
            VALUES
              ('00000000-0000-0000-0000-000000000211', '0', 'STOPPED'),
              ('00000000-0000-0000-0000-000000000211', '2', 'RUNNING');
            INSERT INTO t_fault_code_mapping_entries
              (output_id, raw_code, canonical_code, display_name,
               default_severity)
            VALUES
              ('00000000-0000-0000-0000-000000000212', 'E30',
               'COMPRESSOR_FAULT', '压缩机故障', 'MAJOR'),
              ('00000000-0000-0000-0000-000000000212', 'E11',
               'DC_OVERVOLTAGE', '直流过压', 'MAJOR');
            INSERT INTO t_conversion_input_bindings
              (installed_conversion_id, input_id, source_kind, l0_tag_id,
               confirmed_by)
            VALUES
              (%s, '00000000-0000-0000-0000-000000000209', 'l0', %s,
               'user:installer'),
              (%s, '00000000-0000-0000-0000-000000000210', 'l0', %s,
               'user:installer');
            INSERT INTO t_conversion_output_bindings
              (installed_conversion_id, output_id, entity_instance_id)
            VALUES
              (%s, '00000000-0000-0000-0000-000000000211', %s),
              (%s, '00000000-0000-0000-0000-000000000212', %s);
            """,
            (
                str(STATE_TAG_ID),
                str(NODE_ID),
                str(FAULT_TAG_ID),
                str(NODE_ID),
                str(STATE_ENTITY_ID),
                str(FAULT_ENTITY_ID),
                str(REVISION_ID),
                str(REVISION_ID),
                str(REVISION_ID),
                str(REVISION_ID),
                str(CONVERSION_ID),
                str(STATE_TAG_ID),
                str(CONVERSION_ID),
                str(FAULT_TAG_ID),
                str(CONVERSION_ID),
                str(STATE_ENTITY_ID),
                str(CONVERSION_ID),
                str(FAULT_ENTITY_ID),
            ),
        )

    def assert_counts(
        self,
        *,
        l0: int,
        l0_latest: int,
        l2: int,
        l2_latest: int,
        sources: int,
        outbox: int,
    ) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                actual = []
                for table in (
                    "t_telemetry",
                    "t_telemetry_latest",
                    "t_l2_observations",
                    "t_l2_latest",
                    "t_l2_observation_sources",
                    "t_l2_stream_outbox",
                ):
                    cursor.execute(f"SELECT count(*) FROM {table}")
                    actual.append(cursor.fetchone()[0])
        self.assertEqual(actual, [l0, l0_latest, l2, l2_latest, sources, outbox])

    def test_numeric_ingest_commits_all_facts_together(self) -> None:
        receipt = self.trunk.ingest((self.raw_power(12345.0, sequence=1),))

        self.assertEqual(receipt.accepted_l0_count, 1)
        self.assertEqual(receipt.duplicate_l0_count, 0)
        self.assertEqual(receipt.l2_event_ids, (EXPECTED_EVENT_ID,))
        self.assert_counts(
            l0=1,
            l0_latest=1,
            l2=1,
            l2_latest=1,
            sources=1,
            outbox=1,
        )

    def test_injected_source_or_outbox_failure_rolls_back_every_write(self) -> None:
        for failed_stage in ("source", "outbox"):
            with self.subTest(failed_stage=failed_stage):
                repository = PostgresDataTrunkRepository(
                    connection_factory=self._connection,
                    clock=lambda: datetime(2026, 8, 17, 1, tzinfo=UTC),
                    fault_hook=lambda stage: (
                        (_ for _ in ()).throw(
                            RuntimeError(f"injected {failed_stage} failure")
                        )
                        if stage == failed_stage
                        else None
                    ),
                )

                with self.assertRaises(DataTrunkError) as raised:
                    DataTrunk(repository).ingest(
                        (self.raw_power(12345.0, sequence=1),)
                    )

                self.assertEqual(raised.exception.code, "DATA_TRUNK_UNAVAILABLE")
                self.assert_counts(
                    l0=0,
                    l0_latest=0,
                    l2=0,
                    l2_latest=0,
                    sources=0,
                    outbox=0,
                )

    def test_duplicate_source_digest_is_idempotent(self) -> None:
        raw = self.raw_power(12345.0, sequence=1)

        first = self.trunk.ingest((raw,))
        second = self.trunk.ingest((raw,))

        self.assertEqual(first.accepted_l0_count, 1)
        self.assertEqual(second.accepted_l0_count, 0)
        self.assertEqual(second.duplicate_l0_count, 1)
        self.assertEqual(second.l2_event_ids, ())
        self.assert_counts(
            l0=1,
            l0_latest=1,
            l2=1,
            l2_latest=1,
            sources=1,
            outbox=1,
        )

    def test_late_observation_adds_history_without_moving_latest_or_outbox(self) -> None:
        newer = self.raw_power(
            13000.0,
            sequence=2,
            observed_at=datetime(2026, 8, 17, 0, 0, 10, tzinfo=UTC),
        )
        late = self.raw_power(
            12000.0,
            sequence=1,
            observed_at=datetime(2026, 8, 17, 0, 0, 5, tzinfo=UTC),
        )

        self.trunk.ingest((newer,))
        receipt = self.trunk.ingest((late,))

        self.assertEqual(receipt.late_observation_count, 1)
        self.assert_counts(
            l0=2,
            l0_latest=1,
            l2=2,
            l2_latest=1,
            sources=2,
            outbox=1,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT raw_value_float FROM t_telemetry_latest WHERE tag_id = %s",
                    (str(TAG_ID),),
                )
                self.assertEqual(cursor.fetchone(), (13000.0,))
                cursor.execute(
                    "SELECT value_float FROM t_l2_latest WHERE entity_instance_id = %s",
                    (str(ENTITY_ID),),
                )
                self.assertEqual(cursor.fetchone(), (13.0,))

    def test_batch_preserves_every_l2_history_observation(self) -> None:
        first = self.raw_power(
            10000.0,
            sequence=1,
            observed_at=datetime(2026, 8, 17, 0, 0, 1, tzinfo=UTC),
        )
        second = self.raw_power(
            20000.0,
            sequence=2,
            observed_at=datetime(2026, 8, 17, 0, 0, 2, tzinfo=UTC),
        )

        receipt = self.trunk.ingest((first, second))

        self.assertEqual(receipt.accepted_l0_count, 2)
        self.assertEqual(len(receipt.l2_event_ids), 2)
        self.assert_counts(
            l0=2,
            l0_latest=1,
            l2=2,
            l2_latest=1,
            sources=2,
            outbox=2,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT value_float
                    FROM t_l2_observations
                    ORDER BY observed_at
                    """
                )
                self.assertEqual(cursor.fetchall(), [(10.0,), (20.0,)])

    def test_same_timestamp_uses_source_order_key_as_tie_breaker(self) -> None:
        observed_at = datetime(2026, 8, 17, tzinfo=UTC)
        later_sequence = self.raw_power(
            20000.0,
            sequence=2,
            observed_at=observed_at,
        )
        earlier_sequence = self.raw_power(
            10000.0,
            sequence=1,
            observed_at=observed_at,
        )

        self.trunk.ingest((later_sequence,))
        receipt = self.trunk.ingest((earlier_sequence,))

        self.assertEqual(receipt.late_observation_count, 1)
        self.assert_counts(
            l0=2,
            l0_latest=1,
            l2=2,
            l2_latest=1,
            sources=2,
            outbox=1,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT raw_value_float, source_order_key
                    FROM t_telemetry_latest
                    WHERE tag_id = %s
                    """,
                    (str(TAG_ID),),
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (20000.0, f"S:00000000000000000002:{'b' * 64}"),
                )

    def test_runtime_type_mismatch_keeps_l0_and_writes_bad_l2(self) -> None:
        raw = replace(
            self.raw_power(12345.0, sequence=1),
            value=TypedValue(ValueKind.STRING, "not-a-number"),
        )

        receipt = self.trunk.ingest((raw,))

        self.assertEqual(receipt.accepted_l0_count, 1)
        self.assert_counts(
            l0=1,
            l0_latest=1,
            l2=1,
            l2_latest=1,
            sources=1,
            outbox=1,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT raw_value_text FROM t_telemetry WHERE tag_id = %s",
                    (str(TAG_ID),),
                )
                self.assertEqual(cursor.fetchone(), ("not-a-number",))
                cursor.execute(
                    """
                    SELECT value_float, quality, reason
                    FROM t_l2_latest
                    WHERE entity_instance_id = %s
                    """,
                    (str(ENTITY_ID),),
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (None, int(TrunkQuality.BAD), "TYPE_MISMATCH"),
                )

    def test_relational_enum_and_fault_rules_produce_typed_l2(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                self._seed_enum_and_fault_conversions(cursor)

        receipt = self.trunk.ingest(
            (
                self.raw_text(
                    tag_id=STATE_TAG_ID,
                    source_key="OperatingStateRaw",
                    value="2",
                    sequence=2,
                ),
                self.raw_text(
                    tag_id=FAULT_TAG_ID,
                    source_key="FaultCodesRaw",
                    value="E30; e11;E30;X99",
                    sequence=3,
                ),
            )
        )

        self.assertEqual(receipt.accepted_l0_count, 2)
        self.assertEqual(len(receipt.l2_event_ids), 2)
        self.assert_counts(
            l0=2,
            l0_latest=2,
            l2=2,
            l2_latest=2,
            sources=2,
            outbox=2,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT entity_instance_id, value_text, value_codes,
                           quality, reason
                    FROM t_l2_latest
                    ORDER BY entity_instance_id
                    """
                )
                self.assertEqual(
                    cursor.fetchall(),
                    [
                        (str(STATE_ENTITY_ID), "RUNNING", None, 192, None),
                        (
                            str(FAULT_ENTITY_ID),
                            None,
                            [
                                "COMPRESSOR_FAULT",
                                "DC_OVERVOLTAGE",
                                "X99",
                            ],
                            64,
                            "UNMAPPED_FAULT_CODE",
                        ),
                    ],
                )

    def test_freshness_scheduler_writes_stale_state_atomically_and_idempotently(
        self,
    ) -> None:
        raw = self.raw_power(20000.0, sequence=1)
        self.trunk.ingest((raw,))
        scheduler = _FreshnessScheduler(
            self.repository,
            clock=lambda: raw.source_timestamp + timedelta(seconds=31),
        )

        first = scheduler.run_once()
        second = scheduler.run_once()

        self.assertEqual((first, second), (1, 0))
        self.assert_counts(
            l0=1,
            l0_latest=1,
            l2=2,
            l2_latest=1,
            sources=2,
            outbox=2,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT value_float, quality, reason, observed_at
                    FROM t_l2_latest
                    WHERE entity_instance_id = %s
                    """,
                    (str(ENTITY_ID),),
                )
                value, quality, reason, observed_at = cursor.fetchone()
                self.assertIsNone(value)
                self.assertEqual(
                    (quality, reason),
                    (int(TrunkQuality.STALE), "FRESHNESS_EXPIRED"),
                )
                self.assertEqual(
                    observed_at,
                    raw.source_timestamp + timedelta(seconds=30),
                )
                cursor.execute(
                    """
                    SELECT source_kind
                    FROM t_l2_observation_sources
                    ORDER BY source_kind
                    """
                )
                self.assertEqual(
                    cursor.fetchall(),
                    [("freshness",), ("l0",)],
                )

    def test_freshness_source_or_outbox_failure_rolls_back_stale_state(self) -> None:
        raw = self.raw_power(20000.0, sequence=1)
        self.trunk.ingest((raw,))

        for failed_stage in ("source", "outbox"):
            with self.subTest(failed_stage=failed_stage):
                repository = PostgresDataTrunkRepository(
                    connection_factory=self._connection,
                    clock=lambda: datetime(2026, 8, 17, 1, tzinfo=UTC),
                    fault_hook=lambda stage: (
                        (_ for _ in ()).throw(
                            RuntimeError(f"injected {failed_stage} failure")
                        )
                        if stage == failed_stage
                        else None
                    ),
                )
                scheduler = _FreshnessScheduler(
                    repository,
                    clock=lambda: raw.source_timestamp + timedelta(seconds=31),
                )

                with self.assertRaises(DataTrunkError) as raised:
                    scheduler.run_once()

                self.assertEqual(raised.exception.code, "DATA_TRUNK_UNAVAILABLE")
                self.assert_counts(
                    l0=1,
                    l0_latest=1,
                    l2=1,
                    l2_latest=1,
                    sources=1,
                    outbox=1,
                )
                with psycopg2.connect(**self.connection_kwargs) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT quality
                            FROM t_l2_latest
                            WHERE entity_instance_id = %s
                            """,
                            (str(ENTITY_ID),),
                        )
                        self.assertEqual(
                            cursor.fetchone(),
                            (int(TrunkQuality.GOOD),),
                        )

    def test_real_observation_without_sequence_wins_at_freshness_deadline(self) -> None:
        first = self.raw_power(20000.0, sequence=1)
        self.trunk.ingest((first,))
        deadline = first.source_timestamp + timedelta(seconds=30)
        _FreshnessScheduler(
            self.repository,
            clock=lambda: deadline,
        ).run_once()
        real_at_deadline = replace(
            self.raw_power(21000.0, sequence=2, observed_at=deadline),
            source_sequence=None,
            source_digest="0" * 64,
        )

        self.trunk.ingest((real_at_deadline,))

        self.assert_counts(
            l0=2,
            l0_latest=1,
            l2=3,
            l2_latest=1,
            sources=3,
            outbox=3,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT value_float, quality, source_order_key
                    FROM t_l2_latest
                    WHERE entity_instance_id = %s
                    """,
                    (str(ENTITY_ID),),
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (21.0, int(TrunkQuality.GOOD), f"D:{'0' * 64}"),
                )


if __name__ == "__main__":
    unittest.main()
