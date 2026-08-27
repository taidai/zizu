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

from app.services.data_trunk_contracts import (
    DataTrunkError,
    FrameFailure,
    FramedRawObservation,
    FrozenFrameCandidate,
    InputReference,
    RawObservation,
    SourceOrder,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.data_trunk_postgres import PostgresFrameRepository
from app.services.data_trunk_conversion import evaluate_processing
from app.services.frame_processor import FrameProcessor
from tests import test_data_frames_migration_postgres as frame_migration


NOW = datetime.now(UTC)


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
        migration_test = frame_migration.DataFramesMigrationPostgresTest
        migration_test.connection_kwargs = cls.connection_kwargs
        migration_test().setUp()
        with psycopg2.connect(**cls.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                migration_test._apply_046(cursor)
            connection.commit()

    def setUp(self) -> None:
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
                "INSERT INTO t_nodes(id,name,node_type,enabled) "
                "VALUES(%s,%s,'DEVICE',TRUE)",
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
            source_timestamp=NOW,
            received_at=NOW,
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
            shot_at=NOW,
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
                quality=TrunkQuality.GOOD, source_timestamp=NOW,
                received_at=NOW, source_message_id=f"multi-{capture_beat}",
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
            shot_at=NOW,
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
            cursor.execute(
                "SELECT count(*) FROM t_data_frame_outbox WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual((0,), cursor.fetchone())

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
        self.assertEqual({}, dict(recovered.active_input_contracts))
        self.assertEqual(frozenset(), recovered.required_tag_ids)
        self.assertEqual((), recovered.observations)

    def test_transaction_b_atomically_advances_l0_and_completes_frame(self) -> None:
        pending = self.repository.commit_pending(self._candidate(capture_beat=106))
        terminal = FrameProcessor(
            self.repository,
            evaluator=evaluate_processing,
            clock=lambda: NOW,
        ).process_next(NOW)

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
                "SELECT terminal_status FROM t_data_frame_outbox WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual(("COMPLETE",), cursor.fetchone())

    def test_transaction_b_rolls_back_all_publication_on_fault(self) -> None:
        def fault(stage: str) -> None:
            if stage == "source":
                raise RuntimeError("injected source failure")

        repository = PostgresFrameRepository(
            connection_factory=self._connection_factory(), fault_hook=fault
        )
        pending = repository.commit_pending(self._candidate(capture_beat=107))
        with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_COMPLETE_FAILED"):
            FrameProcessor(
                repository,
                evaluator=evaluate_processing,
                clock=lambda: NOW,
            ).process_next(NOW)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM t_data_frames WHERE frame_id=%s",
                (str(pending.frame_id),),
            )
            self.assertEqual(("PROCESSING",), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_telemetry_latest WHERE frame_sequence=%s",
                (pending.frame_sequence,),
            )
            self.assertEqual((0,), cursor.fetchone())

    def test_expired_processing_lease_is_reclaimed_and_old_token_is_fenced(self) -> None:
        self.repository.commit_pending(self._candidate(capture_beat=109))
        first = self.repository.claim_next(NOW)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "UPDATE t_data_frames SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE frame_id=%s",
                (str(first.frame_id),),
            )
            cursor.execute("SET session_replication_role=origin")
        other = PostgresFrameRepository(connection_factory=self._connection_factory())
        second = other.claim_next(datetime.now(UTC))
        self.assertEqual(first.frame_id, second.frame_id)
        self.assertEqual(2, second.attempt_count)
        self.assertNotEqual(first.processing_token, second.processing_token)
        with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_CLAIM_LOST"):
            self.repository.complete(
                first,
                self.repository.load_processing_snapshot(first),
                (),
            )

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
                    NOW,
                    datetime.now(UTC) - timedelta(seconds=61),
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
                "INSERT INTO t_nodes(id,name,node_type,enabled) "
                "VALUES(%s,'FRAME-SITE','SITE',TRUE)",
                (str(site_node_id),),
            )
            cursor.execute(
                "UPDATE t_nodes SET node_type='PCS',parent_id=%s WHERE id=%s",
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
            clock=lambda: NOW,
        ).process_next(NOW)
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
