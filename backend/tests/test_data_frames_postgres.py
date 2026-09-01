from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
import unittest
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg2

os.environ.setdefault("DB_PASSWORD", "test-postgres-secret")
os.environ.setdefault("NEURON_PASSWORD", "test-neuron-secret")
os.environ.setdefault("NANOMQ_API_PASSWORD", "test-nanomq-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value-that-is-long-enough")

from app.api.tags import _coerce_latest_value
from app.services.data_trunk_contracts import (
    DataTrunkError,
    FrameFailure,
    FramedRawObservation,
    FrozenFrameCandidate,
    InputReference,
    L2Observation,
    RawObservation,
    SourceOrder,
    SourceOrderMode,
    TrunkQuality,
    TypedValue,
    ValueKind,
    typed_raw_value_from_columns,
)
from app.services.data_trunk_postgres import PostgresFrameRepository
from app.services.data_trunk_outbox import PostgresFrameOutboxRepository
from app.services.data_trunk_conversion import evaluate_processing
from app.services.frame_processor import FrameProcessor
from tests import test_data_frames_migration_postgres as frame_migration
from tests import test_committed_frame_payload_migration_postgres as payload_migration
from tests import test_frame_retention_migration_postgres as retention_migration
from tests import test_committed_frame_consumers_migration_postgres as consumer_migration


MIGRATION_050 = Path(__file__).resolve().parents[2] / "init-db" / "migration_050_node_l0_usability.sql"
MIGRATION_051 = Path(__file__).resolve().parents[2] / "init-db" / "migration_051_node_private_point_processing.sql"
MIGRATION_059 = Path(__file__).resolve().parents[2] / "init-db" / "migration_059_l0_raw_bit_semantics.sql"


class RawTypedUnionContractTest(unittest.TestCase):
    def test_actual_nonempty_column_defines_the_raw_value_type(self) -> None:
        integer = typed_raw_value_from_columns(
            raw_float=None,
            raw_int=0,
            raw_bool=None,
            raw_text=None,
        )
        boolean = typed_raw_value_from_columns(
            raw_float=None,
            raw_int=None,
            raw_bool=False,
            raw_text=None,
        )

        self.assertEqual(TypedValue.integer(0), integer)
        self.assertEqual(TypedValue.boolean(False), boolean)

    def test_zero_or_multiple_raw_columns_are_invalid_evidence(self) -> None:
        with self.assertRaisesRegex(DataTrunkError, "RECOVERY_EVIDENCE_INVALID"):
            typed_raw_value_from_columns(
                raw_float=None,
                raw_int=None,
                raw_bool=None,
                raw_text=None,
            )
        with self.assertRaisesRegex(DataTrunkError, "RECOVERY_EVIDENCE_INVALID"):
            typed_raw_value_from_columns(
                raw_float=0.0,
                raw_int=0,
                raw_bool=None,
                raw_text=None,
            )

    def test_tags_api_prefers_the_actual_framed_raw_scalar_and_reason(self) -> None:
        tag = {
            "data_type": "BOOL",
            "frame_sequence": 9,
            "raw_value_float": None,
            "raw_value_int": 0,
            "raw_value_bool": None,
            "raw_value_text": None,
            "value_float": None,
            "value_int": 0,
            "value_bool": None,
            "value_str": None,
            "quality": 0,
            "quality_reason": "TYPE_MISMATCH",
        }

        _coerce_latest_value(tag)

        self.assertEqual(0, tag["raw_value"])
        self.assertIs(type(tag["raw_value"]), int)
        self.assertIsNone(tag["eng_value"])
        self.assertEqual("TYPE_MISMATCH", tag["quality_reason"])


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run data-frame PostgreSQL tests",
)
class DataFramesPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Data-frame tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        with psycopg2.connect(**cls.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA IF EXISTS zizu_internal CASCADE")
            connection.commit()
        migration_test = frame_migration.DataFramesMigrationPostgresTest
        migration_test.connection_kwargs = cls.connection_kwargs
        migration_test().setUp()
        with psycopg2.connect(**cls.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                migration_test._apply_046(cursor)
                payload_migration.CommittedFramePayloadMigrationPostgresTest._apply_047(cursor)
                cursor.execute(
                    retention_migration.MIGRATION_048.read_text(encoding="utf-8")
                )
                cursor.execute(
                    consumer_migration.MIGRATION_049.read_text(encoding="utf-8")
                )
                cursor.execute(MIGRATION_050.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_051.read_text(encoding="utf-8"))
                cursor.execute(
                    frame_migration.MIGRATION_054.read_text(encoding="utf-8")
                )
                cursor.execute(MIGRATION_059.read_text(encoding="utf-8"))
            connection.commit()

    def setUp(self) -> None:
        self.now = datetime.now(UTC)
        self.node_id = uuid4()
        self.tag_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            for table in (
                "t_data_frame_outbox",
                "t_l2_observation_sources",
                "t_l2_latest",
                "t_l2_observations",
                "t_telemetry_latest",
                "t_telemetry",
                "t_l0_observation_dedup",
                "t_ingestion_failures",
                "t_data_frames",
            ):
                cursor.execute(f"DELETE FROM {table}")
            cursor.execute("SET session_replication_role=origin")
            cursor.execute(
                "INSERT INTO t_nodes(id,name,node_type,enabled,layer) "
                "VALUES(%s,%s,'DEVICE',TRUE,1)",
                (str(self.node_id), f"frame-node-{self.node_id}"),
            )
            cursor.execute(
                "INSERT INTO t_tags"
                "(id,node_id,name,data_type,enabled,timestamp_trusted,source_sequence_trusted) "
                "VALUES(%s,%s,%s,'FLOAT',TRUE,FALSE,TRUE)",
                (str(self.tag_id), str(self.node_id), f"tag-{self.tag_id}"),
            )
        self.repository = PostgresFrameRepository(
            connection_factory=self._connection_factory()
        )

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    def _connection_factory(self):
        kwargs = dict(self.connection_kwargs)

        @contextmanager
        def factory():
            connection = psycopg2.connect(**kwargs)
            try:
                yield connection
            finally:
                connection.close()

        return factory

    def _candidate(
        self,
        *,
        capture_beat: int,
        digest_suffix: str = "",
        frame_id: UUID | None = None,
        configuration_revision: int = 0,
    ) -> FrozenFrameCandidate:
        source_digest = hashlib.sha256(
            f"{self.tag_id}:{capture_beat}:{digest_suffix}".encode()
        ).hexdigest()
        observation = RawObservation(
            observation_id=uuid4(),
            node_id=self.node_id,
            tag_id=self.tag_id,
            source_key=f"node/{self.node_id}/power",
            value=TypedValue.float(float(capture_beat)),
            raw_unit="kW",
            quality=TrunkQuality.GOOD,
            source_timestamp=self.now,
            received_at=self.now,
            source_message_id=f"message-{capture_beat}",
            source_sequence=capture_beat,
            source_digest=source_digest,
            event_time_basis="received_at",
            source_order=SourceOrder.sequence(capture_beat),
        )
        framed = FramedRawObservation(
            observation=observation,
            accepted_beat=capture_beat,
            effective_quality=TrunkQuality.GOOD,
        )
        candidate_digest = hashlib.sha256(
            f"frame:{capture_beat}:{digest_suffix}".encode()
        ).hexdigest()
        return FrozenFrameCandidate(
            frame_id=frame_id or uuid4(),
            candidate_digest=candidate_digest,
            generation=capture_beat,
            capture_beat=capture_beat,
            shot_at=self.now,
            configuration_revision=configuration_revision,
            cells=MappingProxyType({self.tag_id: framed}),
            changed_l0=(framed,),
        )

    def _multi_candidate(
        self,
        *,
        capture_beat: int,
        configuration_revision: int,
        tag_specs: tuple[tuple[UUID, str, TypedValue, str | None], ...],
    ) -> FrozenFrameCandidate:
        cells = []
        for tag_id, source_key, value, unit in tag_specs:
            digest = hashlib.sha256(
                f"{tag_id}:{capture_beat}:{value.value}".encode()
            ).hexdigest()
            raw = RawObservation(
                observation_id=uuid4(), node_id=self.node_id, tag_id=tag_id,
                source_key=source_key, value=value, raw_unit=unit,
                quality=TrunkQuality.GOOD, source_timestamp=self.now,
                received_at=self.now, source_message_id=f"multi-{capture_beat}",
                source_sequence=capture_beat, source_digest=digest,
                event_time_basis="received_at",
                source_order=SourceOrder.sequence(capture_beat),
            )
            cells.append(
                FramedRawObservation(
                    observation=raw,
                    accepted_beat=capture_beat,
                    effective_quality=TrunkQuality.GOOD,
                )
            )
        return FrozenFrameCandidate(
            frame_id=uuid4(),
            candidate_digest=hashlib.sha256(
                f"multi-frame:{capture_beat}".encode()
            ).hexdigest(),
            generation=capture_beat,
            capture_beat=capture_beat,
            shot_at=self.now,
            configuration_revision=configuration_revision,
            cells=MappingProxyType(
                {cell.observation.tag_id: cell for cell in cells}
            ),
            changed_l0=tuple(cells),
        )

    def test_transaction_a_persists_pending_frame_and_changed_l0_only(self) -> None:
        candidate = self._candidate(capture_beat=101)
        pending = self.repository.commit_pending(candidate)
        self.assertEqual("PENDING", pending.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,capture_beat FROM t_data_frames WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual(("PENDING", 101), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_telemetry WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((1,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_telemetry_latest WHERE frame_sequence=%s",
                (pending.frame_sequence,),
            )
            self.assertEqual((0,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_data_frame_outbox WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((0,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_l2_observations WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((0,), cursor.fetchone())

    def test_bad_raw_integer_round_trips_through_history_latest_and_outbox(self) -> None:
        candidate = self._candidate(capture_beat=102)
        original = candidate.changed_l0[0]
        bad_observation = replace(
            original.observation,
            value=TypedValue.integer(2),
            quality=TrunkQuality.BAD,
            quality_reason="BIT_VALUE_OUT_OF_RANGE",
        )
        bad = replace(
            original,
            observation=bad_observation,
            effective_quality=TrunkQuality.BAD,
        )
        candidate = replace(
            candidate,
            cells=MappingProxyType({self.tag_id: bad}),
            changed_l0=(bad,),
        )

        pending = self.repository.commit_pending(candidate)
        claimed = self.repository.claim_next(datetime.now(UTC))
        snapshot = self.repository.load_processing_snapshot(claimed)
        restored = snapshot.l0_by_tag[self.tag_id].observation
        self.assertEqual(TypedValue.integer(2), restored.value)
        self.assertEqual(
            "BIT_VALUE_OUT_OF_RANGE",
            restored.quality_reason,
        )
        self.repository.complete(claimed, snapshot, ())

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT raw_value_int,raw_value_bool,quality,quality_reason "
                "FROM t_telemetry WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((2, None, 0, "BIT_VALUE_OUT_OF_RANGE"), cursor.fetchone())
            cursor.execute(
                "SELECT raw_value_int,raw_value_bool,quality,quality_reason "
                "FROM t_telemetry_latest WHERE tag_id=%s",
                (str(self.tag_id),),
            )
            self.assertEqual((2, None, 0, "BIT_VALUE_OUT_OF_RANGE"), cursor.fetchone())
            cursor.execute(
                "SELECT payload->'l0_changes' FROM t_data_frame_outbox WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            change = cursor.fetchone()[0][0]
            self.assertEqual("INT", change["data_type"])
            self.assertEqual(2, change["value"])
            self.assertEqual("BIT_VALUE_OUT_OF_RANGE", change["quality_reason"])

    def test_stale_l0_remains_stale_after_latest_advances_without_new_sample(self) -> None:
        installation_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_installed_point_processings"
                "(id,node_id,revision_id,source_plan_id,configuration_revision,"
                " installed_by,current) VALUES(%s,%s,%s,%s,0,'test:frame',TRUE)",
                (
                    str(installation_id),
                    str(self.node_id),
                    str(uuid4()),
                    str(uuid4()),
                ),
            )
            cursor.execute(
                "INSERT INTO t_point_processing_input_bindings"
                "(installed_processing_id,input_id,source_kind,l0_tag_id,confirmed_by) "
                "VALUES(%s,%s,'l0',%s,'test:frame')",
                (str(installation_id), str(uuid4()), str(self.tag_id)),
            )
            cursor.execute("SET session_replication_role=origin")

        first = self._candidate(capture_beat=1)
        self.repository.commit_pending(first)
        first_claim = self.repository.claim_next(datetime.now(UTC))
        first_snapshot = self.repository.load_processing_snapshot(first_claim)
        self.repository.complete(first_claim, first_snapshot, ())

        for beat in (2, 3):
            unchanged = replace(
                first,
                frame_id=uuid4(),
                candidate_digest=hashlib.sha256(
                    f"unchanged-beat-{beat}".encode()
                ).hexdigest(),
                generation=beat,
                capture_beat=beat,
                changed_l0=(),
            )
            self.repository.commit_pending(unchanged)
            claim = self.repository.claim_next(datetime.now(UTC))
            snapshot = self.repository.load_processing_snapshot(claim)
            self.assertIs(
                TrunkQuality.GOOD,
                snapshot.l0_by_tag[self.tag_id].effective_quality,
            )
            self.repository.complete(claim, snapshot, ())
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT accepted_beat FROM t_telemetry_latest WHERE tag_id=%s",
                    (str(self.tag_id),),
                )
                self.assertEqual((1,), cursor.fetchone())
                cursor.execute(
                    "SELECT payload->'l0_changes' FROM t_data_frame_outbox "
                    "WHERE frame_id=%s",
                    (str(claim.frame_id),),
                )
                self.assertEqual(([],), cursor.fetchone())

        stale = replace(
            first,
            frame_id=uuid4(),
            candidate_digest=hashlib.sha256(b"unchanged-beat-4").hexdigest(),
            generation=4,
            capture_beat=4,
            changed_l0=(),
        )
        self.repository.commit_pending(stale)
        stale_claim = self.repository.claim_next(datetime.now(UTC))
        stale_snapshot = self.repository.load_processing_snapshot(stale_claim)

        self.assertIs(
            TrunkQuality.STALE,
            stale_snapshot.l0_by_tag[self.tag_id].effective_quality,
        )
        self.repository.complete(stale_claim, stale_snapshot, ())
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload->'l0_changes' FROM t_data_frame_outbox "
                "WHERE frame_id=%s",
                (str(stale_claim.frame_id),),
            )
            changes = cursor.fetchone()[0]
        self.assertEqual(1, len(changes))
        self.assertEqual(1, changes[0]["accepted_beat"])
        self.assertEqual(int(TrunkQuality.STALE), changes[0]["effective_quality"])

    def test_legacy_zero_accepted_beat_is_stale_in_the_first_frame(self) -> None:
        installation_id = uuid4()
        observation_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_installed_point_processings"
                "(id,node_id,revision_id,source_plan_id,configuration_revision,"
                " installed_by,current) VALUES(%s,%s,%s,%s,0,'test:frame',TRUE)",
                (
                    str(installation_id),
                    str(self.node_id),
                    str(uuid4()),
                    str(uuid4()),
                ),
            )
            cursor.execute(
                "INSERT INTO t_point_processing_input_bindings"
                "(installed_processing_id,input_id,source_kind,l0_tag_id,confirmed_by) "
                "VALUES(%s,%s,'l0',%s,'test:frame')",
                (str(installation_id), str(uuid4()), str(self.tag_id)),
            )
            cursor.execute(
                "INSERT INTO t_telemetry_latest"
                "(node_id,tag_id,ts,value_float,raw_value_float,quality,"
                " observation_id,source_message_id,source_sequence,source_digest,"
                " event_time_basis,event_received_at,source_order_key,"
                " frame_sequence,accepted_beat,source_order_mode) "
                "VALUES(%s,%s,%s,1.0,1.0,192,%s,'legacy',1,%s,'received_at',"
                " %s,'legacy',1,0,'sequence')",
                (
                    str(self.node_id),
                    str(self.tag_id),
                    self.now,
                    str(observation_id),
                    "a" * 64,
                    self.now,
                ),
            )
            cursor.execute("SET session_replication_role=origin")

        empty = replace(
            self._candidate(capture_beat=1),
            candidate_digest=hashlib.sha256(b"legacy-first-frame").hexdigest(),
            changed_l0=(),
        )
        self.repository.commit_pending(empty)
        claim = self.repository.claim_next(datetime.now(UTC))
        snapshot = self.repository.load_processing_snapshot(claim)

        self.assertIs(
            TrunkQuality.STALE,
            snapshot.l0_by_tag[self.tag_id].effective_quality,
        )

    def test_second_writer_is_rejected_while_first_lease_is_held(self) -> None:
        first = self.repository.acquire_writer()
        self.addCleanup(first.close)
        first.close()
        first.close()
        first = self.repository.acquire_writer()
        self.addCleanup(first.close)
        other = PostgresFrameRepository(connection_factory=self._connection_factory())
        with self.assertRaisesRegex(
            DataTrunkError, "DATA_FRAME_WRITER_ALREADY_ACTIVE"
        ):
            other.acquire_writer()

    def test_transaction_a_commit_response_loss_returns_original_frame(self) -> None:
        raised = False

        def fault(stage: str) -> None:
            nonlocal raised
            if stage == "frame_commit" and not raised:
                raised = True
                raise RuntimeError("simulated response loss")

        repository = PostgresFrameRepository(
            connection_factory=self._connection_factory(), fault_hook=fault
        )
        candidate = self._candidate(capture_beat=102)
        with self.assertRaisesRegex(
            DataTrunkError, "DATA_FRAME_COMMIT_RESULT_UNKNOWN"
        ):
            repository.commit_pending(candidate)
        recovered = repository.commit_pending(candidate)
        self.assertEqual(candidate.frame_id, recovered.frame_id)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_data_frames WHERE capture_beat=102"
            )
            self.assertEqual((1,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_telemetry WHERE frame_id=%s",
                (str(recovered.frame_id),),
            )
            self.assertEqual((1,), cursor.fetchone())

    def test_transaction_a_rejects_capture_identity_with_different_digest(self) -> None:
        original = self._candidate(capture_beat=103)
        self.repository.commit_pending(original)
        changed = self._candidate(
            capture_beat=103,
            digest_suffix="changed",
            frame_id=original.frame_id,
        )
        with self.assertRaisesRegex(
            DataTrunkError, "DATA_FRAME_CANDIDATE_CONFLICT"
        ):
            self.repository.commit_pending(changed)

    def test_transaction_a_rejects_stale_configuration_without_any_write(self) -> None:
        candidate = self._candidate(capture_beat=104, configuration_revision=9)
        with self.assertRaisesRegex(
            DataTrunkError, "DATA_FRAME_CONFIGURATION_STALE"
        ):
            self.repository.commit_pending(candidate)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_data_frames WHERE capture_beat=104")
            self.assertEqual((0,), cursor.fetchone())

    def test_restore_blackboard_uses_last_capture_and_current_configuration(self) -> None:
        self.repository.commit_pending(self._candidate(capture_beat=105))
        recovered = self.repository.restore_blackboard()
        self.assertGreaterEqual(recovered.capture_beat, 105)
        self.assertEqual(0, recovered.configuration_revision)
        self.assertEqual(
            SourceOrderMode.SEQUENCE,
            recovered.active_input_contracts[self.tag_id],
        )
        self.assertEqual(frozenset(), recovered.required_tag_ids)
        self.assertEqual(1, len(recovered.observations))
        self.assertEqual(
            self.tag_id,
            recovered.observations[0].observation.tag_id,
        )

    def test_restore_blackboard_preserves_actual_raw_type_after_tag_metadata_changes(self) -> None:
        self.repository.commit_pending(self._candidate(capture_beat=105))
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_tags SET data_type='BOOL',value_data_type='BOOL' "
                "WHERE id=%s",
                (str(self.tag_id),),
            )

        recovered = self.repository.restore_blackboard()

        self.assertIn(self.tag_id, recovered.active_input_contracts)
        self.assertEqual(1, len(recovered.observations))
        observation = recovered.observations[0].observation
        self.assertEqual(TypedValue.float(105.0), observation.value)
        self.assertEqual(TrunkQuality.GOOD, observation.quality)

    def test_transaction_b_atomically_advances_l0_and_completes_frame(self) -> None:
        pending = self.repository.commit_pending(self._candidate(capture_beat=106))
        terminal = FrameProcessor(
            self.repository,
            evaluator=evaluate_processing,
            clock=lambda: datetime.now(UTC),
        ).process_next(datetime.now(UTC))

        self.assertEqual(pending.frame_id, terminal.frame_id)
        self.assertEqual("COMPLETE", terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM t_data_frames WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual(("COMPLETE",), cursor.fetchone())
            cursor.execute(
                "SELECT frame_sequence,quality FROM t_telemetry_latest "
                "WHERE tag_id=%s",
                (str(self.tag_id),),
            )
            self.assertEqual(
                (pending.frame_sequence, int(TrunkQuality.GOOD)),
                cursor.fetchone(),
            )
            cursor.execute(
                "SELECT terminal_status,payload_version,payload "
                "FROM t_data_frame_outbox WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            terminal_status, payload_version, payload = cursor.fetchone()
            self.assertEqual("COMPLETE", terminal_status)
            self.assertEqual(1, payload_version)
            self.assertEqual(pending.frame_sequence, payload["frame_sequence"])
            self.assertEqual(str(self.node_id), payload["l0_changes"][0]["node_id"])

    def test_l0_outbox_resolves_latest_source_by_frame_sequence(self) -> None:
        pending = self.repository.commit_pending(self._candidate(capture_beat=107))
        terminal = FrameProcessor(
            self.repository,
            evaluator=evaluate_processing,
            clock=lambda: datetime.now(UTC),
        ).process_next(datetime.now(UTC))

        self.assertEqual("COMPLETE", terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "UPDATE t_l0_observation_dedup "
                "SET created_at=created_at + INTERVAL '1 second' "
                "WHERE observation_id IN ("
                "SELECT observation_id FROM t_telemetry WHERE frame_id=%s)",
                (str(pending.frame_id),),
            )
            cursor.execute("SET session_replication_role=origin")
            latest_state = PostgresFrameOutboxRepository._load_l0_latest_state(
                cursor,
                capture_beat=107,
            )

        self.assertEqual((self.tag_id,), tuple(latest_state))
        self.assertEqual(107.0, latest_state[self.tag_id].value.value)

    def test_l2_source_evidence_uses_retained_l0_latest_after_pruning(
        self,
    ) -> None:
        pending = self.repository.commit_pending(self._candidate(capture_beat=107))
        claim = self.repository.claim_next(datetime.now(UTC))
        snapshot = self.repository.load_processing_snapshot(claim)
        self.repository.complete(claim, snapshot, ())
        source = snapshot.l0_by_tag[self.tag_id].observation
        observation = L2Observation(
            event_id=uuid4(),
            entity_instance_id=uuid4(),
            definition_id="test.retained_latest",
            value=TypedValue.float(107.0),
            unit="kW",
            quality=TrunkQuality.GOOD,
            reason=None,
            observed_at=self.now,
            received_at=self.now,
            calculated_at=self.now,
            processing_revision_id=uuid4(),
            configuration_revision=0,
            source_observation_ids=(source.observation_id,),
            source_digest=source.source_digest,
            source_order_key="retained-latest",
            event_time_basis="received_at",
            frame_id=pending.frame_id,
            frame_sequence=pending.frame_sequence,
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            self.repository._insert_frame_l2(cursor, (observation,))
            cursor.execute(
                "DELETE FROM t_l0_observation_dedup WHERE observation_id=%s",
                (str(source.observation_id),),
            )
            cursor.execute("SET session_replication_role=origin")
            self.repository._insert_sources(cursor, (observation,))

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT l0_observation_id FROM t_l2_observation_sources "
                "WHERE l2_event_id=%s",
                (str(observation.event_id),),
            )
            self.assertEqual((str(source.observation_id),), cursor.fetchone())

    def test_l2_latest_retains_last_good_value_while_current_quality_is_bad(self) -> None:
        entity_id = uuid4()
        template_id = uuid4()
        processing_revision_id = uuid4()
        good_at = self.now
        bad_at = self.now + timedelta(seconds=1)
        recovered_at = self.now + timedelta(seconds=2)

        def observation(
            *,
            value: bool | None,
            quality: TrunkQuality,
            reason: str | None,
            observed_at: datetime,
            sequence: int,
        ) -> L2Observation:
            return L2Observation(
                event_id=uuid4(),
                entity_instance_id=entity_id,
                definition_id="test.boolean_status",
                value=TypedValue.boolean(value),
                unit=None,
                quality=quality,
                reason=reason,
                observed_at=observed_at,
                received_at=observed_at,
                calculated_at=observed_at,
                processing_revision_id=processing_revision_id,
                configuration_revision=0,
                source_observation_ids=(),
                source_digest=hashlib.sha256(
                    f"l2-{sequence}".encode()
                ).hexdigest(),
                source_order_key=f"l2-{sequence}",
                event_time_basis="received_at",
                frame_id=uuid4(),
                frame_sequence=sequence,
            )

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_point_processing_templates"
                "(id,asset_id,device_category,brand,model,display_name,status) "
                "VALUES(%s,%s,'DEVICE','test','last-good','Last good test','active')",
                (str(template_id), f"test.last-good.{template_id}"),
            )
            cursor.execute(
                "INSERT INTO t_point_processing_revisions"
                "(id,template_id,revision,content_digest,published_at) "
                "VALUES(%s,%s,1,%s,%s)",
                (
                    str(processing_revision_id),
                    str(template_id),
                    hashlib.sha256(str(processing_revision_id).encode()).hexdigest(),
                    self.now,
                ),
            )
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_entity_instances"
                "(id,node_id,definition_id,display_name,data_type,direction,"
                " freshness_seconds,active,source_kind) "
                "VALUES(%s,%s,'test.boolean_status','Boolean status','BOOL','R',"
                " 30,TRUE,'point_processing')",
                (str(entity_id), str(self.node_id)),
            )
            cursor.execute("SET session_replication_role=origin")
            self.repository._ensure_runtime(cursor)
            self.repository._advance_frame_l2_latest(
                cursor,
                (observation(
                    value=False,
                    quality=TrunkQuality.GOOD,
                    reason=None,
                    observed_at=good_at,
                    sequence=1,
                ),),
            )
            self.repository._advance_frame_l2_latest(
                cursor,
                (observation(
                    value=None,
                    quality=TrunkQuality.BAD,
                    reason="TYPE_MISMATCH",
                    observed_at=bad_at,
                    sequence=2,
                ),),
            )
            cursor.execute(
                "SELECT value_bool,value_observed_at,quality,observed_at,reason "
                "FROM t_l2_latest WHERE entity_instance_id=%s",
                (str(entity_id),),
            )
            self.assertEqual(
                (False, good_at, 0, bad_at, "TYPE_MISMATCH"),
                cursor.fetchone(),
            )
            self.repository._advance_frame_l2_latest(
                cursor,
                (observation(
                    value=True,
                    quality=TrunkQuality.GOOD,
                    reason=None,
                    observed_at=recovered_at,
                    sequence=3,
                ),),
            )
            cursor.execute(
                "SELECT value_bool,value_observed_at,quality,observed_at,reason "
                "FROM t_l2_latest WHERE entity_instance_id=%s",
                (str(entity_id),),
            )
            self.assertEqual(
                (True, recovered_at, 192, recovered_at, None),
                cursor.fetchone(),
            )

    def test_first_bad_l2_latest_does_not_invent_a_retained_value(self) -> None:
        entity_id = uuid4()
        template_id = uuid4()
        processing_revision_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_point_processing_templates"
                "(id,asset_id,device_category,brand,model,display_name,status) "
                "VALUES(%s,%s,'DEVICE','test','first-bad','First bad test','active')",
                (str(template_id), f"test.first-bad.{template_id}"),
            )
            cursor.execute(
                "INSERT INTO t_point_processing_revisions"
                "(id,template_id,revision,content_digest,published_at) "
                "VALUES(%s,%s,1,%s,%s)",
                (
                    str(processing_revision_id),
                    str(template_id),
                    hashlib.sha256(str(processing_revision_id).encode()).hexdigest(),
                    self.now,
                ),
            )
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_entity_instances"
                "(id,node_id,definition_id,display_name,data_type,direction,"
                " freshness_seconds,active,source_kind) "
                "VALUES(%s,%s,'test.first_bad','First bad','BOOL','R',"
                " 30,TRUE,'point_processing')",
                (str(entity_id), str(self.node_id)),
            )
            cursor.execute("SET session_replication_role=origin")
            self.repository._ensure_runtime(cursor)
            bad = L2Observation(
                event_id=uuid4(),
                entity_instance_id=entity_id,
                definition_id="test.first_bad",
                value=TypedValue.boolean(None),
                unit=None,
                quality=TrunkQuality.BAD,
                reason="TYPE_MISMATCH",
                observed_at=self.now,
                received_at=self.now,
                calculated_at=self.now,
                processing_revision_id=processing_revision_id,
                configuration_revision=0,
                source_observation_ids=(),
                source_digest=hashlib.sha256(b"first-bad-l2").hexdigest(),
                source_order_key="first-bad-l2",
                event_time_basis="received_at",
                frame_id=uuid4(),
                frame_sequence=1,
            )
            self.repository._advance_frame_l2_latest(cursor, (bad,))
            cursor.execute(
                "SELECT value_bool,value_observed_at,quality "
                "FROM t_l2_latest WHERE entity_instance_id=%s",
                (str(entity_id),),
            )
            self.assertEqual((None, None, 0), cursor.fetchone())

    def test_terminal_frame_outbox_rebuilds_atomic_event_and_acks_token(self) -> None:
        pending = self.repository.commit_pending(self._candidate(capture_beat=108))
        terminal = FrameProcessor(
            self.repository,
            evaluator=evaluate_processing,
            clock=lambda: datetime.now(UTC),
        ).process_next(datetime.now(UTC))
        self.assertIsNotNone(terminal)
        self.assertEqual("COMPLETE", terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute("DELETE FROM t_l2_observation_sources")
            cursor.execute("DELETE FROM t_l2_observations")
            cursor.execute("DELETE FROM t_telemetry")
            cursor.execute("SET session_replication_role=origin")
        outbox = PostgresFrameOutboxRepository(
            connection_factory=self._connection_factory(),
        )

        claim_at = datetime.now(UTC) + timedelta(seconds=10)
        claim = outbox.claim_unpublished(claim_at)

        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(pending.frame_id, claim.event.frame_id)
        self.assertEqual("COMPLETE", claim.event.status.value)
        self.assertEqual(1, len(claim.event.l0_changes))
        self.assertEqual(self.tag_id, claim.event.l0_changes[0].tag_id)
        self.assertEqual(108.0, claim.event.l0_changes[0].value.value)
        outbox.mark_published(claim.event.frame_id, claim.claim_token)
        self.assertIsNone(outbox.claim_unpublished(claim_at))

    def test_transaction_b_rolls_back_and_retries_immediately_on_fault(self) -> None:
        def fault(stage: str) -> None:
            if stage == "source":
                raise RuntimeError("injected source failure")

        repository = PostgresFrameRepository(
            connection_factory=self._connection_factory(), fault_hook=fault
        )
        pending = repository.commit_pending(self._candidate(capture_beat=107))
        terminal = FrameProcessor(
            repository,
            evaluator=evaluate_processing,
            clock=lambda: datetime.now(UTC),
        ).process_next(datetime.now(UTC))
        self.assertIsNone(terminal)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM t_data_frames WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual(("PENDING",), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_telemetry_latest WHERE frame_sequence=%s",
                (pending.frame_sequence,),
            )
            self.assertEqual((0,), cursor.fetchone())

    def test_expired_processing_lease_is_reclaimed_and_old_token_is_fenced(self) -> None:
        self.repository.commit_pending(self._candidate(capture_beat=109))
        first = self.repository.claim_next(self.now)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "UPDATE t_data_frames SET lease_until=%s "
                "WHERE frame_id=%s",
                (datetime.now(UTC) - timedelta(minutes=5), str(first.frame_id)),
            )
            cursor.execute("SET session_replication_role=origin")
        other = PostgresFrameRepository(connection_factory=self._connection_factory())
        second = other.claim_next(datetime.now(UTC))
        self.assertEqual(first.frame_id, second.frame_id)
        self.assertEqual(1, second.attempt_count)
        self.assertNotEqual(first.processing_token, second.processing_token)
        with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_CLAIM_LOST"):
            self.repository.complete(
                first,
                self.repository.load_processing_snapshot(first),
                (),
            )

    def test_expired_processing_lease_can_be_reclaimed_by_same_repository(self) -> None:
        self.repository.commit_pending(self._candidate(capture_beat=119))
        first = self.repository.claim_next(self.now)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "UPDATE t_data_frames SET lease_until=%s "
                "WHERE frame_id=%s",
                (datetime.now(UTC) - timedelta(minutes=5), str(first.frame_id)),
            )
            cursor.execute("SET session_replication_role=origin")
        second = self.repository.claim_next(datetime.now(UTC))
        self.assertEqual(first.frame_id, second.frame_id)
        self.assertNotEqual(first.processing_owner, second.processing_owner)
        self.assertNotEqual(first.processing_token, second.processing_token)

    def test_retry_clears_claim_and_third_failure_is_durable(self) -> None:
        pending = self.repository.commit_pending(self._candidate(capture_beat=110))
        failure = FrameFailure("FRAME_PROCESSING_FAILED", frozenset())
        for expected_attempt in (1, 2):
            claimed = self.repository.claim_next(datetime.now(UTC))
            self.assertEqual(expected_attempt, claimed.attempt_count)
            self.assertIsNone(
                self.repository.retry_or_fail(claimed, failure, datetime.now(UTC))
            )
        third = self.repository.claim_next(datetime.now(UTC))
        terminal = self.repository.retry_or_fail(
            third, failure, datetime.now(UTC)
        )
        self.assertEqual("FAILED", terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,attempt_count,processing_owner,processing_token,lease_until "
                "FROM t_data_frames WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual(("FAILED", 3, None, None, None), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_ingestion_failures WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((1,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_data_frame_outbox WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((1,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_telemetry WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((1,), cursor.fetchone())

    def test_old_pending_head_is_terminalized_before_next_frame(self) -> None:
        old_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_data_frames
                  (frame_id,candidate_digest,capture_beat,shot_at,
                   configuration_revision,status,attempt_count,created_at)
                VALUES(%s,%s,120,%s,0,'PENDING',1,%s)
                """,
                (
                    str(old_id),
                    "e" * 64,
                    self.now,
                    datetime.now(UTC) - timedelta(seconds=70),
                ),
            )
        next_pending = self.repository.commit_pending(
            self._candidate(capture_beat=121)
        )
        budget = self.repository.claim_next(datetime.now(UTC))
        self.assertEqual(old_id, budget.frame_id)
        self.assertEqual(1, budget.attempt_count)
        terminal = self.repository.fail_budget(budget, datetime.now(UTC))
        self.assertEqual("FAILED", terminal.status.value)
        next_claim = self.repository.claim_next(datetime.now(UTC))
        self.assertEqual(next_pending.frame_id, next_claim.frame_id)

    def test_expired_frame_is_failed_without_promoting_late_l0(self) -> None:
        pending = self.repository.commit_pending(
            self._candidate(capture_beat=123)
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            expired_at = datetime.now(UTC) - timedelta(seconds=70)
            cursor.execute(
                "UPDATE t_data_frames SET created_at=%s WHERE frame_id=%s",
                (expired_at, str(pending.frame_id)),
            )
            cursor.execute(
                "UPDATE t_l0_observation_dedup SET created_at=%s "
                "WHERE observation_id IN ("
                "SELECT observation_id FROM t_telemetry WHERE frame_id=%s)",
                (expired_at, str(pending.frame_id)),
            )
            cursor.execute("SET session_replication_role=origin")

        budget = self.repository.claim_next(datetime.now(UTC))
        terminal = self.repository.fail_budget(budget, datetime.now(UTC))

        self.assertEqual("FAILED", terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_telemetry_latest WHERE frame_sequence=%s",
                (pending.frame_sequence,),
            )
            self.assertEqual((0,), cursor.fetchone())

    def test_legacy_zero_attempt_processing_head_records_one_failure_attempt(self) -> None:
        old_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_data_frames
                  (frame_id,candidate_digest,capture_beat,shot_at,
                   configuration_revision,status,attempt_count,processing_owner,
                   processing_token,lease_until,created_at)
                VALUES(%s,%s,122,%s,0,'PROCESSING',0,%s,%s,%s,%s)
                """,
                (
                    str(old_id),
                    "f" * 64,
                    self.now,
                    str(uuid4()),
                    str(uuid4()),
                    datetime.now(UTC) - timedelta(seconds=1),
                    datetime.now(UTC) - timedelta(seconds=70),
                ),
            )
        budget = self.repository.claim_next(datetime.now(UTC))
        self.assertEqual(old_id, budget.frame_id)
        self.assertEqual(0, budget.attempt_count)
        terminal = self.repository.fail_budget(budget, datetime.now(UTC))
        self.assertEqual("FAILED", terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT attempts FROM t_ingestion_failures WHERE frame_id=%s",
                (str(old_id),),
            )
            self.assertEqual((1,), cursor.fetchone())

    def test_z_transaction_b_persists_every_installed_l1_output_once(self) -> None:
        reference_root = (
            Path(__file__).resolve().parents[2]
            / "reference-point-processings"
        )
        raw_template = json.loads(
            (reference_root / "pcs-brand-a.zizu-point-processing.json").read_text(
                encoding="utf-8"
            )
        )
        site_template = json.loads(
            (reference_root / "site-total-pcs-power.zizu-point-processing.json")
            .read_text(encoding="utf-8")
        )
        site_node_id = uuid4()
        tag_specs = (
            (
                uuid5(NAMESPACE_URL, "frame/ActivePowerRaw"),
                "ActivePowerRaw", TypedValue.float(1000.0), "W",
            ),
            (
                uuid5(NAMESPACE_URL, "frame/RunningState"),
                "RunningState", TypedValue(ValueKind.STRING, "2"), None,
            ),
            (
                uuid5(NAMESPACE_URL, "frame/FaultCodeText"),
                "FaultCodeText", TypedValue(ValueKind.STRING, "E30"), None,
            ),
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_nodes(id,name,node_type,enabled,layer) "
                "VALUES(%s,'FRAME-SITE','SITE',TRUE,1)",
                (str(site_node_id),),
            )
            cursor.execute(
                "UPDATE t_nodes SET node_type='PCS',parent_id=%s,layer=2 WHERE id=%s",
                (str(site_node_id), str(self.node_id)),
            )
            for tag_id, source_key, value, unit in tag_specs:
                cursor.execute(
                    "INSERT INTO t_tags(id,node_id,name,data_type,unit,enabled) "
                    "VALUES(%s,%s,%s,%s,%s,TRUE)",
                    (
                        str(tag_id), str(self.node_id), source_key,
                        value.kind.value, unit,
                    ),
                )
        from app.services.telemetry_store import close_db_pool, init_db_pool
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingService,
            PreviewPointProcessing,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )

        init_db_pool(1, 4)
        try:
            registered = PostgresPointProcessingTemplates().import_template(
                raw_template, actor="test:frame"
            )
            processing_repository = PostgresPointProcessingRepository()
            service = PointProcessingService(
                processing_repository, PostgresPointProcessingCatalog()
            )
            plan = service.preview(
                PreviewPointProcessing(
                    node_id=self.node_id,
                    template_revision_id=registered.revision_id,
                    input_selections={},
                    actor="test:frame",
                )
            )
            applied = service.apply(
                ApplyPointProcessingPlan(
                    plan.id, plan.digest, "frame-install", "test:frame"
                )
            )
            site_registered = PostgresPointProcessingTemplates().import_template(
                site_template, actor="test:frame"
            )
            site_plan = service.preview(
                PreviewPointProcessing(
                    node_id=site_node_id,
                    template_revision_id=site_registered.revision_id,
                    input_selections={},
                    actor="test:frame",
                )
            )
            applied = service.apply(
                ApplyPointProcessingPlan(
                    site_plan.id,
                    site_plan.digest,
                    "frame-site-install",
                    "test:frame",
                )
            )
        finally:
            close_db_pool()

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT output_binding.entity_instance_id "
                "FROM t_point_processing_output_bindings AS output_binding "
                "JOIN t_installed_point_processings AS installed "
                "ON installed.id=output_binding.installed_processing_id "
                "WHERE installed.current=TRUE ORDER BY 1"
            )
            affected = frozenset(UUID(str(row[0])) for row in cursor.fetchall())

        # A committed BAD state is a real latest event, but it is not a typed
        # value that a later failed frame can preserve as its safety baseline.
        missing_pending = self.repository.commit_pending(
            self._multi_candidate(
                capture_beat=107,
                configuration_revision=applied.configuration_revision,
                tag_specs=(),
            )
        )
        missing_terminal = FrameProcessor(
            self.repository,
            evaluator=evaluate_processing,
            clock=lambda: datetime.now(UTC),
        ).process_next(datetime.now(UTC))
        self.assertEqual("COMPLETE", missing_terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*),count(*) FILTER (WHERE "
                "num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes)=0) "
                "FROM t_l2_observations WHERE frame_id=%s AND quality=0",
                (str(missing_pending.frame_id),),
            )
            self.assertEqual((1, 1), cursor.fetchone())

        first_failed = self.repository.commit_pending(
            self._multi_candidate(
                capture_beat=108,
                configuration_revision=applied.configuration_revision,
                tag_specs=tag_specs,
            )
        )
        failure = FrameFailure("FRAME_PROCESSING_FAILED", affected)
        for _ in range(2):
            claim = self.repository.claim_next(datetime.now(UTC))
            self.repository.retry_or_fail(claim, failure, datetime.now(UTC))
        claim = self.repository.claim_next(datetime.now(UTC))
        failed_terminal = self.repository.retry_or_fail(
            claim, failure, datetime.now(UTC)
        )
        self.assertEqual("FAILED", failed_terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*),count(*) FILTER (WHERE "
                "num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes)=0) "
                "FROM t_l2_observations WHERE frame_id=%s AND quality=1",
                (str(first_failed.frame_id),),
            )
            self.assertEqual((4, 4), cursor.fetchone())

        candidate = self._multi_candidate(
            capture_beat=111,
            configuration_revision=applied.configuration_revision,
            tag_specs=tag_specs,
        )
        pending = self.repository.commit_pending(candidate)
        terminal = FrameProcessor(
            self.repository,
            evaluator=evaluate_processing,
            clock=lambda: datetime.now(UTC),
        ).process_next(datetime.now(UTC))
        self.assertEqual("COMPLETE", terminal.status.value)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*),count(DISTINCT entity_instance_id) "
                "FROM t_l2_observations WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((4, 4), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_l2_latest WHERE frame_sequence=%s",
                (pending.frame_sequence,),
            )
            self.assertEqual((4,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_data_frame_outbox WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((1,), cursor.fetchone())

        # The first expired recovery frame makes the affected entities STALE.
        # A later expired frame still needs its frame failure and outbox, but it
        # must not append an identical L2 state transition for every old beat.
        expired_frames = []
        for capture_beat in (112, 113):
            expired = self.repository.commit_pending(
                self._multi_candidate(
                    capture_beat=capture_beat,
                    configuration_revision=applied.configuration_revision,
                    tag_specs=tag_specs,
                )
            )
            expired_frames.append(expired)
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute("SET session_replication_role=replica")
                cursor.execute(
                    "UPDATE t_data_frames SET created_at=%s WHERE frame_id=%s",
                    (
                        datetime.now(UTC) - timedelta(seconds=70),
                        str(expired.frame_id),
                    ),
                )
                cursor.execute("SET session_replication_role=origin")
            budget = self.repository.claim_next(datetime.now(UTC))
            self.repository.fail_budget(budget, datetime.now(UTC))

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_l2_observations WHERE frame_id=%s",
                (str(expired_frames[0].frame_id),),
            )
            self.assertEqual((4,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_l2_observations WHERE frame_id=%s",
                (str(expired_frames[1].frame_id),),
            )
            self.assertEqual((0,), cursor.fetchone())

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_telemetry")
            history_before_stale = cursor.fetchone()[0]
        stale_candidate = replace(
            candidate,
            frame_id=uuid4(),
            candidate_digest=hashlib.sha256(b"pure-stale-frame").hexdigest(),
            generation=114,
            capture_beat=114,
            changed_l0=(),
        )
        stale_pending = self.repository.commit_pending(stale_candidate)
        stale_claim = self.repository.claim_next(datetime.now(UTC))
        stale_snapshot = self.repository.load_processing_snapshot(stale_claim)
        self.assertTrue(
            all(
                cell.effective_quality is TrunkQuality.STALE
                for cell in stale_snapshot.l0_by_tag.values()
            )
        )
        # The frame is already claimed for snapshot evidence; commit that exact claim.
        stale_outputs = []
        current_inputs = stale_snapshot.current_inputs()
        for entity_id in stale_snapshot.topological_output_ids:
            installed = stale_snapshot.installed_by_entity_id[entity_id]
            output = evaluate_processing(
                installed=(installed,),
                current_inputs=current_inputs,
                configuration_revision=stale_claim.configuration_revision,
                calculated_at=datetime.now(UTC),
                frame_id=stale_claim.frame_id,
                frame_sequence=stale_claim.frame_sequence,
            )[0]
            stale_outputs.append(output)
            current_inputs[InputReference.l2(entity_id)] = output
        stale_terminal = self.repository.complete(
            stale_claim, stale_snapshot, tuple(stale_outputs)
        )
        self.assertEqual(stale_pending.frame_id, stale_terminal.frame_id)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_telemetry")
            self.assertEqual(history_before_stale, cursor.fetchone()[0])
            cursor.execute(
                "SELECT count(*) FROM t_telemetry_latest "
                "WHERE frame_sequence=%s AND quality=1",
                (stale_pending.frame_sequence,),
            )
            self.assertEqual((3,), cursor.fetchone())

        preserved_failure = self.repository.commit_pending(
            self._multi_candidate(
                capture_beat=115,
                configuration_revision=applied.configuration_revision,
                tag_specs=tag_specs,
            )
        )
        for _ in range(2):
            claim = self.repository.claim_next(datetime.now(UTC))
            self.repository.retry_or_fail(claim, failure, datetime.now(UTC))
        claim = self.repository.claim_next(datetime.now(UTC))
        self.repository.retry_or_fail(claim, failure, datetime.now(UTC))
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*),count(*) FILTER (WHERE "
                "num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes)=1) "
                "FROM t_l2_observations WHERE frame_id=%s AND quality=1",
                (str(preserved_failure.frame_id),),
            )
            self.assertEqual((4, 4), cursor.fetchone())


if __name__ == "__main__":
    unittest.main()
