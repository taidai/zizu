from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import register_uuid

from app.services.alarm_configuration import canonical_digest
from app.services.configuration_revision import ConfigurationRevisionError
from app.services.ems_workbench_slots import EmsWorkbenchSlotError
from app.services.ems_workbench_slots_postgres import PostgresWorkbenchSlotRepository
from tests import test_dispatch_strategy_migration_postgres as strategy_migration


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_062 = ROOT / "init-db" / "migration_062_dispatch_strategies.sql"
MIGRATION_063 = ROOT / "init-db" / "migration_063_ems_workbench_slots.sql"


class WorkbenchSlotMigrationContractTest(unittest.TestCase):
    def test_schema_063_declares_only_the_five_fixed_revisioned_slots(self) -> None:
        sql = MIGRATION_063.read_text(encoding="utf-8")

        self.assertIn("t_ems_workbench_slot_bindings", sql)
        self.assertIn("t_ems_workbench_slot_idempotency", sql)
        self.assertIn("REFERENCES public.t_configuration_revisions(revision)", sql)
        for slot_key in (
            "site-power",
            "pv-power",
            "storage-power",
            "storage-soc",
            "charging-power",
        ):
            self.assertEqual(1, sql.count(f"'{slot_key}'"))


class _ReplayCursor:
    def __init__(self, replay: tuple[str, dict] | None) -> None:
        self.replay = replay
        self.statements: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, statement: str, params: tuple | None = None) -> None:
        self.statements.append((statement, params))

    def fetchone(self):
        return self.replay


class _ReplayConnection:
    def __init__(self, replay: tuple[str, dict] | None) -> None:
        self.cursor_instance = _ReplayCursor(replay)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _ReplayCursor:
        return self.cursor_instance


class _WriteCursor(_ReplayCursor):
    def __init__(self) -> None:
        super().__init__(None)

    def fetchone(self):
        statement = self.statements[-1][0]
        if "FROM t_entity_instances AS entity" in statement:
            return ("FLOAT", "kW")
        return None


class _WriteConnection(_ReplayConnection):
    def __init__(self) -> None:
        self.cursor_instance = _WriteCursor()
        self.commit_count = 0
        self.rollback_count = 0

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class _RevisionPublisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish(self, **kwargs) -> int:
        self.calls.append(kwargs)
        return kwargs["base_revision"] + 1


class WorkbenchSlotRepositoryUnitTest(unittest.TestCase):
    def test_write_commits_binding_idempotency_and_formal_audit_in_one_connection(self) -> None:
        connection = _WriteConnection()
        revisions = _RevisionPublisher()
        repository = PostgresWorkbenchSlotRepository(lambda: connection)
        repository._revisions = revisions
        entity_id = UUID("91000000-0000-0000-0000-000000000101")

        receipt = repository.set_binding(
            slot_key="storage-power",
            entity_instance_id=entity_id,
            base_configuration_revision=7,
            actor="user:engineer",
            idempotency_key="bind-storage-power-v1",
        )

        statements = [item[0] for item in connection.cursor_instance.statements]
        self.assertEqual(8, receipt.configuration_revision)
        self.assertEqual((1, 0), (connection.commit_count, connection.rollback_count))
        self.assertEqual(1, len(revisions.calls))
        self.assertIs(connection, revisions.calls[0]["transaction"])
        self.assertEqual("ems_workbench_slot.bind", revisions.calls[0]["action"])
        self.assertEqual("ems_workbench_slot", revisions.calls[0]["resource_kind"])
        self.assertTrue(
            any("INSERT INTO t_ems_workbench_slot_bindings" in item for item in statements)
        )
        self.assertTrue(
            any("INSERT INTO t_ems_workbench_slot_idempotency" in item for item in statements)
        )

    def test_replay_lookup_uses_the_persisted_receipt_without_allocating_revision(self) -> None:
        entity_id = UUID("91000000-0000-0000-0000-000000000101")
        request = {
            "slot_key": "storage-power",
            "entity_instance_id": str(entity_id),
            "base_configuration_revision": 7,
        }
        connection = _ReplayConnection(
            (
                canonical_digest(request),
                {
                    **request,
                    "configuration_revision": 8,
                },
            )
        )
        repository = PostgresWorkbenchSlotRepository(lambda: connection)

        receipt = repository.find_replay(
            slot_key="storage-power",
            entity_instance_id=entity_id,
            base_configuration_revision=7,
            actor="user:engineer",
            idempotency_key="bind-storage-power-v1",
        )

        self.assertTrue(receipt.replayed)
        self.assertEqual(8, receipt.configuration_revision)
        self.assertEqual(1, len(connection.cursor_instance.statements))

    def test_replay_lookup_rejects_the_same_key_for_a_changed_request(self) -> None:
        connection = _ReplayConnection(
            (
                canonical_digest(
                    {
                        "slot_key": "storage-power",
                        "entity_instance_id": None,
                        "base_configuration_revision": 7,
                    }
                ),
                {
                    "slot_key": "storage-power",
                    "entity_instance_id": None,
                    "configuration_revision": 8,
                },
            )
        )
        repository = PostgresWorkbenchSlotRepository(lambda: connection)

        with self.assertRaises(EmsWorkbenchSlotError) as raised:
            repository.find_replay(
                slot_key="storage-power",
                entity_instance_id=UUID("91000000-0000-0000-0000-000000000101"),
                base_configuration_revision=7,
                actor="user:engineer",
                idempotency_key="bind-storage-power-v1",
            )

        self.assertEqual("WORKBENCH_SLOT_IDEMPOTENCY_CONFLICT", raised.exception.code)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run EMS workbench slot repository tests",
)
class WorkbenchSlotPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("EMS workbench slot tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        migration_test = strategy_migration.DispatchStrategyMigrationPostgresTest
        migration_test.connection_kwargs = self.connection_kwargs
        migration_test().setUp()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_062.read_text(encoding="utf-8"))
            cursor.execute(MIGRATION_063.read_text(encoding="utf-8"))
            # Both migrations own and commit their transactions. Start the fixture
            # transaction explicitly so deferred source checks see the final graph.
            cursor.execute("BEGIN")
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            self.node_id = uuid4()
            self.entity_id = uuid4()
            cursor.execute(
                "INSERT INTO t_nodes(id,name,layer,node_type,enabled) "
                "VALUES(%s,'workbench-test',1,'PCS',TRUE)",
                (self.node_id,),
            )
            cursor.execute(
                "INSERT INTO t_entity_instances"
                "(id,node_id,definition_id,display_name,data_type,unit,direction,"
                "freshness_seconds,source_kind,active) "
                "VALUES(%s,%s,'pcs.active_power','PCS power','FLOAT','kW','R',"
                "10,'point_processing',TRUE)",
                (self.entity_id, self.node_id),
            )
            installation_id = uuid4()
            cursor.execute("SET session_replication_role=replica")
            cursor.execute(
                "INSERT INTO t_installed_point_processings"
                "(id,node_id,revision_id,source_plan_id,configuration_revision,"
                " installed_by,current) VALUES(%s,%s,%s,%s,0,'test:workbench',TRUE)",
                (installation_id, self.node_id, uuid4(), uuid4()),
            )
            cursor.execute(
                "INSERT INTO t_point_processing_output_bindings"
                "(installed_processing_id,output_id,entity_instance_id) "
                "VALUES(%s,%s,%s)",
                (installation_id, uuid4(), self.entity_id),
            )
            cursor.execute("SET session_replication_role=origin")
            cursor.execute("SELECT current_revision FROM t_configuration_state")
            self.base_revision = int(cursor.fetchone()[0])
            connection.commit()
        self.repository = PostgresWorkbenchSlotRepository(
            connection_factory=self._connection
        )

    def _connection(self):
        connection = psycopg2.connect(**self.connection_kwargs)
        register_uuid(conn_or_curs=connection)
        return connection

    def test_migration_is_replayable_and_installs_one_current_row_contract(self) -> None:
        # Break caught: a partial/replayed migration allowing duplicate current
        # bindings or losing the persistent idempotency receipt.
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_063.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(MIGRATION_063.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(
                "SELECT to_regclass('t_ems_workbench_slot_bindings'), "
                "to_regclass('t_ems_workbench_slot_idempotency')"
            )
            self.assertEqual(
                (
                    "t_ems_workbench_slot_bindings",
                    "t_ems_workbench_slot_idempotency",
                ),
                cursor.fetchone(),
            )
            cursor.execute(
                "SELECT indisprimary FROM pg_index "
                "WHERE indrelid='t_ems_workbench_slot_bindings'::regclass"
            )
            self.assertIn((True,), cursor.fetchall())

    def test_save_is_revisioned_audited_and_same_request_replays(self) -> None:
        # Break caught: retrying after an unknown HTTP result allocating a second
        # configuration revision or duplicate audit event.
        first = self.repository.set_binding(
            slot_key="storage-power",
            entity_instance_id=self.entity_id,
            base_configuration_revision=self.base_revision,
            actor="user:engineer",
            idempotency_key="bind-storage-power-v1",
        )
        replay = self.repository.set_binding(
            slot_key="storage-power",
            entity_instance_id=self.entity_id,
            base_configuration_revision=self.base_revision,
            actor="user:engineer",
            idempotency_key="bind-storage-power-v1",
        )
        preflight_replay = self.repository.find_replay(
            slot_key="storage-power",
            entity_instance_id=self.entity_id,
            base_configuration_revision=self.base_revision,
            actor="user:engineer",
            idempotency_key="bind-storage-power-v1",
        )

        self.assertEqual(self.base_revision + 1, first.configuration_revision)
        self.assertFalse(first.replayed)
        self.assertEqual(first.configuration_revision, replay.configuration_revision)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay, preflight_replay)
        self.assertEqual(
            {"storage-power": self.entity_id},
            self.repository.list_manual_bindings(),
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_configuration_audit "
                "WHERE resource_kind='ems_workbench_slot'"
            )
            self.assertEqual(1, cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM t_ems_workbench_slot_idempotency")
            self.assertEqual(1, cursor.fetchone()[0])

    def test_same_idempotency_key_with_different_request_is_rejected(self) -> None:
        # Break caught: silently treating a changed payload as a replay.
        self.repository.set_binding(
            slot_key="storage-power",
            entity_instance_id=self.entity_id,
            base_configuration_revision=self.base_revision,
            actor="user:engineer",
            idempotency_key="one-key",
        )

        with self.assertRaises(EmsWorkbenchSlotError) as raised:
            self.repository.set_binding(
                slot_key="storage-power",
                entity_instance_id=None,
                base_configuration_revision=self.base_revision + 1,
                actor="user:engineer",
                idempotency_key="one-key",
            )

        self.assertEqual("WORKBENCH_SLOT_IDEMPOTENCY_CONFLICT", raised.exception.code)

    def test_stale_revision_is_zero_write(self) -> None:
        # Break caught: changing the binding despite another configuration
        # publisher winning the base-revision race.
        with self.assertRaises(ConfigurationRevisionError) as raised:
            self.repository.set_binding(
                slot_key="storage-power",
                entity_instance_id=self.entity_id,
                base_configuration_revision=self.base_revision + 1,
                actor="user:engineer",
                idempotency_key="stale-bind",
            )

        self.assertEqual("CONFIGURATION_REVISION_STALE", raised.exception.code)
        self.assertEqual({}, self.repository.list_manual_bindings())
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_ems_workbench_slot_idempotency")
            self.assertEqual(0, cursor.fetchone()[0])

    def test_clear_removes_manual_row_and_publishes_a_second_revision(self) -> None:
        # Break caught: persisting NULL as a manual binding, which would prevent
        # deterministic automatic matching from resuming after clear.
        bound = self.repository.set_binding(
            slot_key="storage-power",
            entity_instance_id=self.entity_id,
            base_configuration_revision=self.base_revision,
            actor="user:engineer",
            idempotency_key="bind-before-clear",
        )
        cleared = self.repository.set_binding(
            slot_key="storage-power",
            entity_instance_id=None,
            base_configuration_revision=bound.configuration_revision,
            actor="user:engineer",
            idempotency_key="clear-after-bind",
        )

        self.assertEqual(bound.configuration_revision + 1, cleared.configuration_revision)
        self.assertIsNone(cleared.entity_instance_id)
        self.assertEqual({}, self.repository.list_manual_bindings())


if __name__ == "__main__":
    unittest.main()
