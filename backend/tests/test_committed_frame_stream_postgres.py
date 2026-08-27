from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import os
from types import MappingProxyType
import unittest
from uuid import UUID, uuid4

import psycopg2

from app.services.committed_frame_stream import FrameScope, FrameStreamError
from app.services.committed_frame_stream_postgres import (
    PostgresCommittedFrameStreamRepository,
)
from app.services.data_trunk_contracts import (
    FramedRawObservation,
    FrozenFrameCandidate,
    RawObservation,
    SourceOrder,
    TrunkQuality,
    TypedValue,
)
from app.services.data_trunk_postgres import PostgresFrameRepository
from tests import test_data_frames_migration_postgres as frame_migration
from tests import test_committed_frame_payload_migration_postgres as payload_migration


NOW = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)


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
