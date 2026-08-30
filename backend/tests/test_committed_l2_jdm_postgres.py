"""PostgreSQL evidence for committed-L2 JDM schema and transactions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import unittest
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json, register_uuid

from tests import test_data_frames_postgres as frame_runtime
from app.services.data_trunk_contracts import FrameStatus, TrunkQuality, TypedValue
from app.services.data_trunk_outbox import CommittedL2Change, FrameOutboxEvent


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_052 = ROOT / "init-db" / "migration_052_committed_l2_jdm.sql"


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run committed-L2 JDM PostgreSQL tests",
)
class JdmSchemaPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_uuid()
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Committed-L2 JDM tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        frame_runtime.DataFramesPostgresTest.connection_kwargs = (
            self.connection_kwargs
        )
        frame_runtime.DataFramesPostgresTest.setUpClass()

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    def test_schema_052_is_replayable_and_complete(self) -> None:
        sql = MIGRATION_052.read_text(encoding="utf-8")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(sql)
            connection.commit()
            cursor.execute(sql)
            connection.commit()

            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_rules' "
                "AND column_name='configuration_revision'"
            )
            self.assertEqual(("NO",), cursor.fetchone())

            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_jdm_executions'"
            )
            self.assertEqual(
                {
                    "id",
                    "rule_id",
                    "rule_version",
                    "frame_id",
                    "frame_sequence",
                    "configuration_revision",
                    "model_digest",
                    "status",
                    "reason_code",
                    "inputs",
                    "outputs",
                    "actions",
                    "executed_at",
                },
                {row[0] for row in cursor.fetchall()},
            )

            cursor.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='public.t_jdm_executions'::regclass"
            )
            self.assertTrue(
                {
                    "t_jdm_executions_pkey",
                    "uq_jdm_execution_rule_frame",
                    "fk_jdm_execution_frame",
                    "fk_jdm_execution_configuration_revision",
                    "chk_jdm_execution_rule_version",
                    "chk_jdm_execution_frame_sequence",
                    "chk_jdm_execution_model_digest",
                    "chk_jdm_execution_status",
                    "chk_jdm_execution_reason",
                }.issubset({row[0] for row in cursor.fetchall()})
            )

    def test_schema_052_rejects_an_unknown_partial_execution_table(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE public.t_jdm_executions "
                "(id UUID PRIMARY KEY, status TEXT NOT NULL)"
            )
            connection.commit()
            with self.assertRaisesRegex(
                psycopg2.DatabaseError,
                "SCHEMA_052_PARTIAL_STRUCTURE",
            ):
                cursor.execute(MIGRATION_052.read_text(encoding="utf-8"))


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run committed-L2 JDM PostgreSQL tests",
)
class JdmRuntimePostgresTest(unittest.TestCase):
    SOURCE_ID = UUID("a2000000-0000-0000-0000-000000000001")
    TARGET_ID = UUID("a2000000-0000-0000-0000-000000000002")
    NOW = datetime(2026, 8, 30, 2, 0, tzinfo=UTC)

    @classmethod
    def setUpClass(cls) -> None:
        register_uuid()
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Committed-L2 JDM tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        frame_runtime.DataFramesPostgresTest.connection_kwargs = (
            self.connection_kwargs
        )
        frame_runtime.DataFramesPostgresTest.setUpClass()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_052.read_text(encoding="utf-8"))
            cursor.execute("SELECT current_revision FROM t_configuration_state")
            self.configuration_revision = int(cursor.fetchone()[0])
            self.rule_ids = (uuid4(), uuid4())
            for index, rule_id in enumerate(self.rule_ids, start=1):
                cursor.execute(
                    """
                    INSERT INTO t_rules
                      (id,name,rule_type,jdm_content,version,enabled,
                       configuration_revision)
                    VALUES(%s,%s,'control',%s,2,TRUE,%s)
                    """,
                    (
                        rule_id,
                        f"jdm-frame-rule-{rule_id}",
                        Json(self._content(index)),
                        self.configuration_revision,
                    ),
                )
            self.event = self._insert_frame(cursor, capture_beat=520001)

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

    def _content(self, index: int) -> dict:
        return {
            "when": "power > 10",
            "_config": {
                "inputMappings": {"power": str(self.SOURCE_ID)},
                "sourceEntityInstanceIds": [str(self.SOURCE_ID)],
                "actions": [
                    {
                        "id": f"set-limit-{index}",
                        "type": "control",
                        "entity_instance_id": str(self.TARGET_ID),
                        "value": index,
                    }
                ],
            },
        }

    def _insert_frame(
        self,
        cursor,
        *,
        capture_beat: int,
        configuration_revision: int | None = None,
        frame_sequence: int | None = None,
    ) -> FrameOutboxEvent:
        revision = (
            self.configuration_revision
            if configuration_revision is None
            else configuration_revision
        )
        cursor.execute("SET session_replication_role=replica")
        cursor.execute(
            """
            INSERT INTO t_data_frames
              (frame_id,candidate_digest,capture_beat,shot_at,
               configuration_revision,status,attempt_count,finished_at)
            VALUES(%s,%s,%s,%s,%s,'COMPLETE',1,%s)
            RETURNING frame_id,frame_sequence
            """,
            (uuid4(), f"{capture_beat:064x}"[-64:], capture_beat, self.NOW, revision, self.NOW),
        )
        frame_id, generated_sequence = cursor.fetchone()
        cursor.execute("SET session_replication_role=origin")
        sequence = int(generated_sequence)
        if frame_sequence is not None:
            cursor.execute(
                "UPDATE t_data_frames SET frame_sequence=%s WHERE frame_id=%s",
                (frame_sequence, frame_id),
            )
            sequence = frame_sequence
        return FrameOutboxEvent(
            frame_id=frame_id,
            frame_sequence=sequence,
            status=FrameStatus.COMPLETE,
            configuration_revision=revision,
            l0_changes=(),
            l2_changes=(
                CommittedL2Change(
                    entity_instance_id=self.SOURCE_ID,
                    event_id=uuid4(),
                    value=TypedValue.float(12.0),
                    quality=TrunkQuality.GOOD,
                    reason=None,
                    unit="kW",
                    observed_at=self.NOW,
                    received_at=self.NOW,
                    calculated_at=self.NOW,
                    source_digest="b" * 64,
                ),
            ),
            failure_id=None,
            failure_code=None,
            frame_time=self.NOW,
        )

    def _runtime(self):
        from app.services.jdm_postgres import PostgresJdmRepository
        from app.services.jdm_runtime import JdmRuntime

        return JdmRuntime(
            PostgresJdmRepository(connection_factory=self._connection_factory())
        )

    def _count(self, where: str, parameters: tuple = ()) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {where}", parameters)
            return int(cursor.fetchone()[0])

    def test_receipt_and_all_model_executions_commit_together(self) -> None:
        executions = self._runtime().submit_frame(self.event)

        self.assertEqual(2, len(executions))
        self.assertEqual(
            1,
            self._count(
                "t_committed_frame_consumers "
                "WHERE consumer_key='jdm' AND frame_id=%s",
                (self.event.frame_id,),
            ),
        )
        self.assertEqual(
            2,
            self._count(
                "t_jdm_executions WHERE frame_id=%s",
                (self.event.frame_id,),
            ),
        )

    def test_second_execution_failure_rolls_back_receipt_and_first_execution(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE FUNCTION reject_second_jdm_execution() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.rule_id=%s THEN
                    RAISE EXCEPTION 'SECOND_JDM_EXECUTION_REJECTED';
                  END IF;
                  RETURN NEW;
                END $$
                """,
                (self.rule_ids[1],),
            )
            cursor.execute(
                "CREATE TRIGGER reject_second_jdm_execution "
                "BEFORE INSERT ON t_jdm_executions FOR EACH ROW "
                "EXECUTE FUNCTION reject_second_jdm_execution()"
            )

        with self.assertRaisesRegex(Exception, "SECOND_JDM_EXECUTION_REJECTED"):
            self._runtime().submit_frame(self.event)

        self.assertEqual(
            0,
            self._count(
                "t_committed_frame_consumers "
                "WHERE consumer_key='jdm' AND frame_id=%s",
                (self.event.frame_id,),
            ),
        )
        self.assertEqual(
            0,
            self._count(
                "t_jdm_executions WHERE frame_id=%s",
                (self.event.frame_id,),
            ),
        )

    def test_same_frame_replay_is_a_noop(self) -> None:
        runtime = self._runtime()
        first = runtime.submit_frame(self.event)
        replay = runtime.submit_frame(self.event)

        self.assertEqual(2, len(first))
        self.assertEqual((), replay)
        self.assertEqual(
            2,
            self._count(
                "t_jdm_executions WHERE frame_id=%s",
                (self.event.frame_id,),
            ),
        )

    def test_no_active_models_does_not_persist_an_empty_receipt(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM t_rules")

        executions = self._runtime().submit_frame(self.event)

        self.assertEqual((), executions)
        self.assertEqual(
            0,
            self._count(
                "t_committed_frame_consumers "
                "WHERE consumer_key='jdm' AND frame_id=%s",
                (self.event.frame_id,),
            ),
        )

    def test_active_revision_mismatch_writes_nothing(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_configuration_revisions"
                "(revision,previous_revision,actor,action,resource_kind,"
                "resource_id,after_digest) "
                "VALUES(1,0,'test','test.publish','test','jdm',%s)",
                ("c" * 64,),
            )
            event = self._insert_frame(
                cursor,
                capture_beat=520002,
                configuration_revision=1,
            )

        with self.assertRaisesRegex(
            Exception,
            "JDM_FRAME_CONFIGURATION_MISMATCH",
        ):
            self._runtime().submit_frame(event)

        self.assertEqual(
            0,
            self._count(
                "t_committed_frame_consumers "
                "WHERE consumer_key='jdm' AND frame_id=%s",
                (event.frame_id,),
            ),
        )

    def test_no_models_still_rejects_active_revision_mismatch(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM t_rules")
            cursor.execute(
                "INSERT INTO t_configuration_revisions"
                "(revision,previous_revision,actor,action,resource_kind,"
                "resource_id,after_digest) "
                "VALUES(1,0,'test','test.publish','test','jdm',%s)",
                ("d" * 64,),
            )
            event = self._insert_frame(
                cursor,
                capture_beat=520003,
                configuration_revision=1,
            )

        with self.assertRaisesRegex(
            Exception,
            "JDM_FRAME_CONFIGURATION_MISMATCH",
        ):
            self._runtime().submit_frame(event)

        self.assertEqual(
            0,
            self._count(
                "t_committed_frame_consumers "
                "WHERE consumer_key='jdm' AND frame_id=%s",
                (event.frame_id,),
            ),
        )


if __name__ == "__main__":
    unittest.main()
