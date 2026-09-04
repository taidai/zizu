from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import os
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

from app.services.dispatch_strategies import (
    DispatchWindow,
    StrategyBindingDraft,
    StrategyDraft,
    build_two_charge_two_discharge_jdm,
)
from app.services.dispatch_strategy_postgres import (
    PostgresStrategyRepository,
    StrategyRepositoryError,
)
from tests import test_data_frames_postgres as frame_runtime


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_062 = ROOT / "init-db" / "migration_062_dispatch_strategies.sql"


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run dispatch-strategy repository tests",
)
class DispatchStrategyPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Dispatch-strategy tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        frame_runtime.DataFramesPostgresTest.connection_kwargs = self.connection_kwargs
        frame_runtime.DataFramesPostgresTest.setUpClass()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_062.read_text(encoding="utf-8"))
            cursor.execute("SELECT current_revision FROM t_configuration_state")
            self.configuration_revision = int(cursor.fetchone()[0])
            self.node_id = uuid4()
            self.input_id = uuid4()
            self.output_id = uuid4()
            self.tag_id = uuid4()
            cursor.execute(
                "INSERT INTO t_nodes(id,name,layer,node_type,enabled) "
                "VALUES(%s,'strategy-test',1,'Site',TRUE)",
                (self.node_id,),
            )
            cursor.execute(
                "INSERT INTO t_tags"
                "(id,node_id,tag_type,data_type,name,display_name,unit,read_write,enabled) "
                "VALUES(%s,%s,'PHYSICAL','FLOAT','limit','Limit','kW','RW',TRUE)",
                (self.tag_id, self.node_id),
            )
            cursor.execute(
                "INSERT INTO t_entity_instances"
                "(id,node_id,definition_id,display_name,data_type,unit,direction,"
                "freshness_seconds,active,control_policy) VALUES"
                "(%s,%s,'soc','SOC','FLOAT','%%','R',10,TRUE,NULL),"
                "(%s,%s,'limit','Limit','FLOAT','kW','RW',10,TRUE,%s)",
                (
                    self.input_id,
                    self.node_id,
                    self.output_id,
                    self.node_id,
                    Json(
                        {
                            "minimum": 0,
                            "maximum": 200,
                            "cooldown_seconds": 1,
                            "readback_definition": "limit",
                            "timeout_seconds": 10,
                            "high_risk": False,
                        }
                    ),
                ),
            )
            cursor.execute(
                "INSERT INTO t_l2_control_bindings(entity_instance_id,l0_tag_id) VALUES(%s,%s)",
                (self.output_id, self.tag_id),
            )
            connection.commit()
        self.repository = PostgresStrategyRepository(
            connection_factory=self._connection,
            compiler=lambda _content: "f" * 64,
        )

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    def _draft(self, name: str = "2充2放") -> StrategyDraft:
        model = build_two_charge_two_discharge_jdm(
            (
                DispatchWindow(
                    "charge-1", "01:00", "03:00", "CHARGE",
                    Decimal("50"), Decimal("10"), Decimal("90"),
                ),
            ),
            Decimal("0"),
        )
        return StrategyDraft(
            name=name,
            description=None,
            trigger_kind="FIXED_TICK",
            site_timezone="Asia/Shanghai",
            jdm_content=model,
            base_configuration_revision=self.configuration_revision,
            bindings=(
                StrategyBindingDraft("INPUT", "soc", 0, self.input_id, "FLOAT", "%", 10),
                StrategyBindingDraft("OUTPUT", "power-target", 0, self.output_id, "FLOAT", "kW", 10),
            ),
        )

    def test_draft_compare_and_swap_and_published_revision_are_immutable(self) -> None:
        created = self.repository.create_strategy(self._draft(), "engineer:test")
        self.assertIsNotNone(created.draft)
        original_digest = created.draft.content_digest

        updated = self.repository.save_draft(
            created.id,
            replace(self._draft(), description="new"),
            original_digest,
            "engineer:test",
        )
        self.assertNotEqual(original_digest, updated.draft.content_digest)
        with self.assertRaisesRegex(StrategyRepositoryError, "STRATEGY_DRAFT_STALE"):
            self.repository.save_draft(
                created.id,
                self._draft(),
                original_digest,
                "engineer:test",
            )

        published = self.repository.publish(
            created.id,
            updated.draft.content_digest,
            self.configuration_revision,
            "engineer:test",
        )
        with self._connection() as connection, connection.cursor() as cursor:
            with self.assertRaises(psycopg2.DatabaseError):
                cursor.execute(
                    "UPDATE t_dispatch_strategy_revisions SET site_timezone='UTC' WHERE id=%s",
                    (published.id,),
                )

    def test_second_strategy_cannot_partially_acquire_owned_output(self) -> None:
        first = self.repository.create_strategy(self._draft("first"), "engineer:test")
        first_revision = self.repository.publish(
            first.id, first.draft.content_digest, self.configuration_revision, "engineer:test"
        )
        self.repository.enable(first.id, first_revision.id, "engineer:test")

        second = self.repository.create_strategy(self._draft("second"), "engineer:test")
        second_revision = self.repository.publish(
            second.id, second.draft.content_digest, self.configuration_revision, "engineer:test"
        )
        with self.assertRaisesRegex(StrategyRepositoryError, "OUTPUT_ALREADY_OWNED"):
            self.repository.enable(second.id, second_revision.id, "engineer:test")

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT strategy_id FROM t_dispatch_strategy_owners WHERE entity_instance_id=%s",
                (self.output_id,),
            )
            self.assertEqual(str(first.id), str(cursor.fetchone()[0]))

    def test_disable_cancels_pending_but_leaves_inflight_for_reconciliation(self) -> None:
        strategy = self.repository.create_strategy(self._draft(), "engineer:test")
        revision = self.repository.publish(
            strategy.id, strategy.draft.content_digest,
            self.configuration_revision, "engineer:test",
        )
        self.repository.enable(strategy.id, revision.id, "engineer:test")
        pending_id = uuid4()
        inflight_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            for intent_id, status in ((pending_id, "PENDING"), (inflight_id, "IN_FLIGHT")):
                cursor.execute(
                    "INSERT INTO t_dispatch_control_intents"
                    "(id,strategy_id,revision_id,evaluation_key,action_id,ordinal,"
                    "entity_instance_id,expected_value,status,snapshot_evidence) "
                    "VALUES(%s,%s,%s,%s,'power-target',0,%s,'156.7',%s,'{}')",
                    (intent_id, strategy.id, revision.id, str(intent_id), self.output_id, status),
                )
            connection.commit()

        disabled = self.repository.disable(strategy.id, "engineer:test")

        self.assertFalse(disabled.enabled)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM t_dispatch_control_intents ORDER BY id"
            )
            self.assertEqual({"CANCELLED", "IN_FLIGHT"}, {row[0] for row in cursor.fetchall()})


if __name__ == "__main__":
    unittest.main()
