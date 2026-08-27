from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import os
from types import MappingProxyType
import unittest
from uuid import UUID, uuid4

import psycopg2

from app.services.data_trunk_contracts import (
    DataTrunkError,
    FramedRawObservation,
    FrozenFrameCandidate,
    RawObservation,
    SourceOrder,
    TrunkQuality,
    TypedValue,
)
from app.services.data_trunk_postgres import PostgresFrameRepository
from tests import test_data_frames_migration_postgres as frame_migration


NOW = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)


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


if __name__ == "__main__":
    unittest.main()
