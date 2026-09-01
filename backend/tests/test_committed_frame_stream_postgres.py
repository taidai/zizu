from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
from types import MappingProxyType
import unittest
from uuid import UUID, uuid4

import psycopg2

from app.services.committed_frame_stream import FrameScope, FrameStreamError
from app.services.committed_frame_stream_postgres import (
    PostgresCommittedFrameStreamRepository,
    _l0_snapshot_value,
)
from app.services.data_trunk_contracts import (
    FramedRawObservation,
    FrozenFrameCandidate,
    RawObservation,
    SourceOrder,
    TrunkQuality,
    TypedValue,
)
from app.services.data_trunk_freshness import effective_l0_quality
from app.services.data_trunk_postgres import PostgresFrameRepository
from tests import test_data_frames_migration_postgres as frame_migration
from tests import test_committed_frame_payload_migration_postgres as payload_migration


NOW = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)
MIGRATION_059 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_059_l0_raw_bit_semantics.sql"
)


class _SnapshotCursor:
    def __init__(self) -> None:
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql, _parameters=None) -> None:
        self.sql = " ".join(str(sql).split())

    def fetchone(self):
        if self.sql.startswith("SELECT 1 FROM t_nodes"):
            return (1,)
        if self.sql.startswith("SELECT current_revision"):
            return (0,)
        return None

    def fetchall(self):
        return []


class _ReusableConnection:
    def __init__(self) -> None:
        self.readonly = False
        self.commits = 0
        self.rollbacks = 0

    def set_session(self, *, isolation_level, readonly) -> None:
        del isolation_level
        self.readonly = readonly

    def cursor(self):
        return _SnapshotCursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _StaleSnapshotCursor:
    def __init__(
        self,
        *,
        head_beat: int = 10,
        accepted_beat: int = 1,
        source_quality: int = 192,
        quality_reason: str | None = None,
    ) -> None:
        self.sql = ""
        self.head_beat = head_beat
        self.accepted_beat = accepted_beat
        self.source_quality = source_quality
        self.quality_reason = quality_reason
        self.tag_id = uuid4()
        self.node_id = uuid4()
        self.entity_id = uuid4()
        self.observation_id = uuid4()
        self.event_id = uuid4()
        self.processing_revision_id = uuid4()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql, _parameters=None) -> None:
        self.sql = " ".join(str(sql).split())

    def fetchone(self):
        if self.sql.startswith("SELECT 1 FROM t_nodes"):
            return (1,)
        if self.sql.startswith("SELECT frame_sequence"):
            return (
                10,
                NOW + timedelta(seconds=10),
                7,
                self.head_beat,
                NOW + timedelta(seconds=10),
                "FAILED",
                "FRAME_PROCESSING_FAILED",
            )
        if self.sql.startswith("SELECT count(*) FROM t_data_frames"):
            return (3,)
        return None

    def fetchall(self):
        if "FROM t_tags AS tag" in self.sql:
            return [
                (
                    self.tag_id,
                    "pcs.power",
                    "PCS power",
                    "FLOAT",
                    "kW",
                    "pcs/data/power",
                    "neuron",
                    self.observation_id,
                    5.0,
                    None,
                    None,
                    None,
                    self.source_quality,
                    NOW + timedelta(seconds=1),
                    NOW + timedelta(seconds=1),
                    "a" * 64,
                    1,
                    self.source_quality,
                    self.accepted_beat,
                    None,
                    None,
                    None,
                    None,
                    self.quality_reason,
                    "FLOAT",
                )
            ]
        if "FROM t_entity_instances AS entity" in self.sql:
            return [
                (
                    self.entity_id,
                    "pcs.activePower",
                    "PCS power",
                    "FLOAT",
                    "kW",
                    self.event_id,
                    5.0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    192,
                    None,
                    NOW,
                    NOW,
                    NOW,
                    self.processing_revision_id,
                    7,
                    "b" * 64,
                    1,
                    3.0,
                )
            ]
        return []


class _StaleSnapshotConnection(_ReusableConnection):
    def __init__(
        self,
        *,
        head_beat: int = 10,
        accepted_beat: int = 1,
        source_quality: int = 192,
        quality_reason: str | None = None,
    ) -> None:
        super().__init__()
        self.snapshot_cursor = _StaleSnapshotCursor(
            head_beat=head_beat,
            accepted_beat=accepted_beat,
            source_quality=source_quality,
            quality_reason=quality_reason,
        )

    def cursor(self):
        return self.snapshot_cursor


class CommittedFrameStreamConnectionContractTest(unittest.TestCase):
    def test_snapshot_does_not_return_a_read_only_connection_to_the_pool(self) -> None:
        connection = _ReusableConnection()

        @contextmanager
        def reusable_connection():
            yield connection

        repository = PostgresCommittedFrameStreamRepository(
            connection_factory=reusable_connection
        )

        repository.read_snapshot(FrameScope.for_node(uuid4()))

        self.assertFalse(connection.readonly)
        self.assertEqual(1, connection.commits)

    def test_resnapshot_marks_l0_stale_from_the_terminal_head_beat(self) -> None:
        connection = _StaleSnapshotConnection()

        @contextmanager
        def reusable_connection():
            yield connection

        snapshot = PostgresCommittedFrameStreamRepository(
            connection_factory=reusable_connection
        ).read_snapshot(FrameScope.for_node(connection.snapshot_cursor.node_id))

        self.assertEqual(
            int(TrunkQuality.STALE),
            snapshot.l0[0]["effective_quality"],
        )

    def test_snapshot_exposes_frame_failure_and_backlog_for_link_diagnosis(self) -> None:
        connection = _StaleSnapshotConnection()

        @contextmanager
        def reusable_connection():
            yield connection

        snapshot = PostgresCommittedFrameStreamRepository(
            connection_factory=reusable_connection
        ).read_snapshot(FrameScope.for_node(connection.snapshot_cursor.node_id))

        self.assertEqual("FAILED", snapshot.frame_status)
        self.assertEqual(
            {"code": "FRAME_PROCESSING_FAILED"},
            snapshot.failure,
        )
        self.assertEqual(3, snapshot.backlog_frames)
        self.assertEqual("STALE", snapshot.l0[0]["reason"])

    def test_resnapshot_fails_closed_while_runtime_is_warming(self) -> None:
        connection = _StaleSnapshotConnection(head_beat=1, accepted_beat=1)

        @contextmanager
        def reusable_connection():
            yield connection

        snapshot = PostgresCommittedFrameStreamRepository(
            connection_factory=reusable_connection
        ).read_snapshot(FrameScope.for_node(connection.snapshot_cursor.node_id))

        self.assertEqual(
            int(TrunkQuality.STALE),
            snapshot.l0[0]["effective_quality"],
        )

    def test_resnapshot_exposes_the_stored_bad_raw_value_reason(self) -> None:
        connection = _StaleSnapshotConnection(
            head_beat=1,
            accepted_beat=1,
            source_quality=int(TrunkQuality.BAD),
            quality_reason="BIT_VALUE_OUT_OF_RANGE",
        )

        @contextmanager
        def reusable_connection():
            yield connection

        snapshot = PostgresCommittedFrameStreamRepository(
            connection_factory=reusable_connection
        ).read_snapshot(FrameScope.for_node(connection.snapshot_cursor.node_id))

        self.assertEqual(5.0, snapshot.l0[0]["value"])
        self.assertEqual(int(TrunkQuality.BAD), snapshot.l0[0]["effective_quality"])
        self.assertEqual("BIT_VALUE_OUT_OF_RANGE", snapshot.l0[0]["reason"])

    def test_resnapshot_marks_expired_l2_stale_using_entity_freshness(self) -> None:
        connection = _StaleSnapshotConnection()

        @contextmanager
        def reusable_connection():
            yield connection

        snapshot = PostgresCommittedFrameStreamRepository(
            connection_factory=reusable_connection
        ).read_snapshot(FrameScope.for_node(connection.snapshot_cursor.node_id))

        self.assertEqual(int(TrunkQuality.STALE), snapshot.l2[0]["quality"])
        self.assertEqual("STALE", snapshot.l2[0]["reason"])


class CommittedFrameLegacyProjectionTest(unittest.TestCase):
    def test_zero_accepted_beat_is_immediately_stale(self) -> None:
        self.assertEqual(
            int(TrunkQuality.STALE),
            effective_l0_quality(
                1,
                has_value=True,
                stored_quality=TrunkQuality.GOOD,
                capture_beat=1,
                accepted_beat=0,
                received_at=NOW,
                evaluated_at=NOW + timedelta(seconds=1),
            ),
        )

    def test_legacy_int_point_uses_its_nonempty_float_column_for_diagnostics(self) -> None:
        self.assertEqual(
            34.1,
            _l0_snapshot_value(
                "INT",
                0,
                raw_float=None,
                raw_int=None,
                raw_bool=None,
                raw_text=None,
                legacy_float=34.1,
                legacy_int=None,
                legacy_bool=None,
                legacy_text=None,
            ),
        )
        self.assertEqual(
            int(TrunkQuality.STALE),
            effective_l0_quality(0, has_value=True, stored_quality=192),
        )

    def test_committed_frame_projects_the_actual_raw_typed_column(self) -> None:
        self.assertEqual(
            34.1,
            _l0_snapshot_value(
                "INT",
                1,
                raw_float=34.1,
                raw_int=None,
                raw_bool=None,
                raw_text=None,
                legacy_float=34.1,
                legacy_int=None,
                legacy_bool=None,
                legacy_text=None,
            ),
        )

    def test_ambiguous_legacy_value_columns_fail_closed(self) -> None:
        self.assertIsNone(
            _l0_snapshot_value(
                "INT",
                0,
                raw_float=None,
                raw_int=None,
                raw_bool=None,
                raw_text=None,
                legacy_float=34.1,
                legacy_int=34,
                legacy_bool=None,
                legacy_text=None,
            )
        )


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run committed-frame stream tests",
)
class CommittedFrameStreamPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Committed-frame tests require a *_test database")
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
                payload_migration.CommittedFramePayloadMigrationPostgresTest._apply_047(
                    cursor
                )
                cursor.execute(
                    frame_migration.MIGRATION_054.read_text(encoding="utf-8")
                )
                cursor.execute(MIGRATION_059.read_text(encoding="utf-8"))
            connection.commit()

    def setUp(self) -> None:
        self.node_a, self.node_b = uuid4(), uuid4()
        self.tag_a, self.tag_b = uuid4(), uuid4()
        self.entity_a = uuid4()
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
            for node_id, name in ((self.node_a, "A"), (self.node_b, "B")):
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled) "
                    "VALUES(%s,%s,'DEVICE',TRUE)",
                    (str(node_id), f"stream-node-{name}-{node_id}"),
                )
            for tag_id, node_id, name in (
                (self.tag_a, self.node_a, "a.power"),
                (self.tag_b, self.node_b, "b.power"),
            ):
                cursor.execute(
                    "INSERT INTO t_tags"
                    "(id,node_id,name,data_type,unit,enabled,timestamp_trusted,"
                    " source_sequence_trusted,source_path) "
                    "VALUES(%s,%s,%s,'FLOAT','kW',TRUE,FALSE,TRUE,%s)",
                    (str(tag_id), str(node_id), name, f"mqtt/{name}"),
                )
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_entity_instances"
                "(id,node_id,definition_id,display_name,data_type,unit,direction,"
                " freshness_seconds,active,source_kind) "
                "VALUES(%s,%s,'site.netPower','Net power','FLOAT','kW','R',30,"
                " TRUE,'point_processing')",
                (str(self.entity_a), str(self.node_a)),
            )
            cursor.execute("SET session_replication_role=origin")
        self.frame_repository = PostgresFrameRepository(
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

    def _commit_frame(self, beat: int, value_a: float, value_b: float):
        cells = {}
        for tag_id, node_id, value in (
            (self.tag_a, self.node_a, value_a),
            (self.tag_b, self.node_b, value_b),
        ):
            digest = hashlib.sha256(f"{tag_id}:{beat}:{value}".encode()).hexdigest()
            raw = RawObservation(
                observation_id=uuid4(),
                node_id=node_id,
                tag_id=tag_id,
                source_key=str(tag_id),
                value=TypedValue.float(value),
                raw_unit="kW",
                quality=TrunkQuality.GOOD,
                source_timestamp=NOW + timedelta(seconds=beat),
                received_at=NOW + timedelta(seconds=beat),
                source_message_id=f"stream-{beat}",
                source_sequence=beat,
                source_digest=digest,
                event_time_basis="received_at",
                source_order=SourceOrder.sequence(beat),
            )
            cells[tag_id] = FramedRawObservation(
                observation=raw,
                accepted_beat=beat,
                effective_quality=TrunkQuality.GOOD,
            )
        candidate = FrozenFrameCandidate(
            frame_id=uuid4(),
            candidate_digest=hashlib.sha256(f"frame:{beat}".encode()).hexdigest(),
            generation=beat,
            capture_beat=beat,
            shot_at=NOW + timedelta(seconds=beat),
            configuration_revision=0,
            cells=MappingProxyType(cells),
            changed_l0=tuple(cells.values()),
        )
        pending = self.frame_repository.commit_pending(candidate)
        claimed = self.frame_repository.claim_next(datetime.now(UTC))
        snapshot = self.frame_repository.load_processing_snapshot(claimed)
        self.frame_repository.complete(claimed, snapshot, ())
        return pending

    def _mark_published(self, frame_id: UUID) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_data_frame_outbox SET published_at=clock_timestamp() "
                "WHERE frame_id=%s",
                (str(frame_id),),
            )

    def test_snapshot_uses_one_terminal_database_cut(self) -> None:
        before = self._commit_frame(1, 1.0, 10.0)
        committed_during_read = False

        def commit_next() -> None:
            nonlocal committed_during_read
            if not committed_during_read:
                committed_during_read = True
                self._commit_frame(2, 2.0, 20.0)

        repository = PostgresCommittedFrameStreamRepository(
            connection_factory=self._connection_factory(),
            on_snapshot_head_read=commit_next,
        )
        snapshot = repository.read_snapshot(FrameScope.for_node(self.node_a))

        self.assertEqual(before.frame_sequence, snapshot.frame_sequence)
        self.assertEqual(1.0, snapshot.l0[0]["value"])
        self.assertTrue(
            all(item["frame_sequence"] <= before.frame_sequence for item in snapshot.l0)
        )

    def test_empty_active_item_is_visible_as_waiting_stale(self) -> None:
        repository = PostgresCommittedFrameStreamRepository(
            connection_factory=self._connection_factory()
        )
        snapshot = repository.read_snapshot(FrameScope.for_node(self.node_a))

        self.assertEqual(0, snapshot.frame_sequence)
        self.assertEqual(1, len(snapshot.l0))
        self.assertIsNone(snapshot.l0[0]["value"])
        self.assertEqual(int(TrunkQuality.STALE), snapshot.l0[0]["effective_quality"])
        self.assertEqual(1, len(snapshot.l2))
        self.assertIsNone(snapshot.l2[0]["value"])
        self.assertEqual("WAITING_DATA", snapshot.l2[0]["reason"])

    def test_legacy_latest_value_is_visible_but_stale(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_tags SET data_type='INT' WHERE id=%s",
                (str(self.tag_a),),
            )
            cursor.execute(
                """
                INSERT INTO t_telemetry_latest(
                  node_id,tag_id,ts,value_float,quality,frame_sequence,
                  accepted_beat,event_time_basis,event_received_at
                ) VALUES(%s,%s,%s,34.1,192,0,0,'received_at',%s)
                """,
                (str(self.node_a), str(self.tag_a), NOW, NOW),
            )

        snapshot = PostgresCommittedFrameStreamRepository(
            connection_factory=self._connection_factory()
        ).read_snapshot(FrameScope.for_node(self.node_a))

        point = next(item for item in snapshot.l0 if item["tag_id"] == str(self.tag_a))
        self.assertEqual(34.1, point["value"])
        self.assertEqual(int(TrunkQuality.STALE), point["effective_quality"])

    def test_replay_filters_payload_without_leaking_other_node(self) -> None:
        frame = self._commit_frame(3, 3.0, 30.0)
        self._mark_published(frame.frame_id)
        repository = PostgresCommittedFrameStreamRepository(
            connection_factory=self._connection_factory()
        )

        rows = repository.replay_after(
            0, frame.frame_sequence, FrameScope.for_node(self.node_a)
        )

        self.assertEqual(1, len(rows))
        self.assertEqual(
            {str(self.tag_a)}, {item["tag_id"] for item in rows[0].l0_changes}
        )
        self.assertEqual(frame.frame_sequence, repository.replay_window().latest_sequence)

    def test_replay_rejects_more_than_fixed_5000_frame_window(self) -> None:
        repository = PostgresCommittedFrameStreamRepository(
            connection_factory=self._connection_factory()
        )
        with self.assertRaisesRegex(FrameStreamError, "FRAME_CURSOR_TOO_OLD"):
            repository.replay_after(1, 5002, FrameScope.for_node(self.node_a))


if __name__ == "__main__":
    unittest.main()
