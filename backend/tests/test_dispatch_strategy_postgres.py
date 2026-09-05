from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import os
import json
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg2
from psycopg2.extras import register_uuid

from app.services.dispatch_strategies import (
    DispatchWindow,
    StrategyBindingDraft,
    StrategyDraft,
    StrategyModelError,
    StrategyRuntime,
    build_two_charge_two_discharge_jdm,
)
from app.services.dispatch_strategy_postgres import (
    PostgresStrategyRepository,
    StrategyRepositoryError,
    _json_safe,
)
from tests import test_dispatch_strategy_migration_postgres as strategy_migration


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_062 = ROOT / "init-db" / "migration_062_dispatch_strategies.sql"


class DispatchStrategyEvidenceSerializationTest(unittest.TestCase):
    def test_nonfinite_blocked_sample_evidence_is_valid_json_not_a_number(self) -> None:
        evidence = _json_safe({"samples": [float("nan"), float("inf"), Decimal("-Infinity")]})
        try:
            encoded = json.dumps(evidence, allow_nan=False)
        except ValueError as error:
            self.fail(f"Blocked evidence is not valid JSON: {error}")
        self.assertEqual({"samples": ["nan", "inf", "-inf"]}, json.loads(encoded))

    def test_mapping_evidence_is_converted_to_json_safe_values(self) -> None:
        observed_at = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
        entity_id = uuid4()

        evidence = _json_safe(
            {
                "observed_at": observed_at,
                "entity_instance_id": entity_id,
                "value": Decimal("156.8"),
            }
        )

        self.assertEqual(
            {
                "observed_at": "2026-09-05T08:00:00+00:00",
                "entity_instance_id": str(entity_id),
                "value": 156.8,
            },
            evidence,
        )


class DispatchStrategyPostgresFixture:
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
        migration_test = strategy_migration.DispatchStrategyMigrationPostgresTest
        migration_test.connection_kwargs = self.connection_kwargs
        migration_test().setUp()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION_062.read_text(encoding="utf-8"))
            cursor.execute("SELECT current_revision FROM t_configuration_state")
            self.configuration_revision = int(cursor.fetchone()[0])
            self.node_id = uuid4()
            self.soc_tag_id = uuid4()
            self.tag_id = uuid4()
            cursor.execute(
                "INSERT INTO t_nodes(id,name,layer,node_type,enabled) "
                "VALUES(%s,'strategy-test',1,'PCS',TRUE)",
                (self.node_id,),
            )
            cursor.execute(
                "INSERT INTO t_tags"
                "(id,node_id,data_type,name,display_name,unit,read_write,enabled,"
                "read_only,source_type,source_path) "
                "VALUES(%s,%s,'FLOAT','limit','Limit','kW','RW',TRUE,FALSE,"
                "'neuron','strategy-test/group0/limit')",
                (self.tag_id, self.node_id),
            )
            cursor.execute(
                "INSERT INTO t_tags"
                "(id,node_id,data_type,name,display_name,unit,read_write,enabled) "
                "VALUES(%s,%s,'FLOAT','soc','SOC','%%','R',TRUE)",
                (self.soc_tag_id, self.node_id),
            )
            connection.commit()
        self._publish_confirmed_sources()
        self.repository = PostgresStrategyRepository(
            connection_factory=self._connection,
            compiler=lambda _content: "f" * 64,
        )

    def _publish_confirmed_sources(self) -> None:
        from app.services.point_processing import (
            ApplyPointProcessingPlan, PointProcessingService, PreviewPointProcessing,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog, PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )
        from app.services.telemetry_store import close_db_pool, init_db_pool
        from tests.test_point_processing_templates import template_json

        init_db_pool(1, 4)
        self.addCleanup(close_db_pool)
        template = template_json()
        template["id"] = "dispatch-confirmed-sources"
        template["inputs"] = [
            {"id": key, "sourceKind": "l0", "sourceKey": key, "aliases": [],
             "dataType": "FLOAT", "unit": unit, "required": True}
            for key, unit in (("soc", "%"), ("limit", "kW"))
        ]
        template["outputs"] = [
            {"id": key, "entityDefinition": definition, "dataType": "FLOAT",
             "unit": unit, "freshness": "10s",
             "transform": {"kind": "passthrough", "input": key}}
            for key, definition, unit in (("soc", "bms.soc", "%"), ("limit", "limit", "kW"))
        ]
        template["outputs"][1]["control"] = {
            "minimum": 0, "maximum": 200, "tolerance": 0.01,
            "cooldownSeconds": 1, "timeoutSeconds": 10, "highRisk": False,
        }
        registered = PostgresPointProcessingTemplates().import_template(template, actor="test:strategy")
        service = PointProcessingService(PostgresPointProcessingRepository(), PostgresPointProcessingCatalog())
        plan = service.preview(PreviewPointProcessing(
            node_id=self.node_id, template_revision_id=registered.revision_id,
            input_selections={}, actor="test:strategy",
        ))
        self.assertEqual("ready", plan.status, plan.blockers)
        application = service.apply(ApplyPointProcessingPlan(
            plan.id, plan.digest, "dispatch-confirmed-sources", "test:strategy",
        ))
        self.configuration_revision = application.configuration_revision
        self.processing_revision_id = registered.revision_id
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT definition_id,id FROM t_entity_instances WHERE id=ANY(%s::uuid[])",
                (list(application.output_entity_instance_ids),),
            )
            entities = dict(cursor.fetchall())
        self.input_id, self.output_id = entities["bms.soc"], entities["limit"]

    def _commit_samples(self):
        from app.services.data_trunk_contracts import TypedValue
        from app.services.data_trunk_conversion import evaluate_processing
        from app.services.data_trunk_postgres import PostgresFrameRepository
        from app.services.frame_processor import FrameProcessor
        from tests.test_data_frames_postgres import DataFramesPostgresTest

        self.now = datetime.now(UTC)
        candidate = DataFramesPostgresTest._multi_candidate(
            self, capture_beat=1, configuration_revision=self.configuration_revision,
            tag_specs=((self.soc_tag_id, "soc", TypedValue.float(50.0), "%"),
                       (self.tag_id, "limit", TypedValue.float(156.8), "kW")),
        )
        frames = PostgresFrameRepository(connection_factory=self._connection)
        pending = frames.commit_pending(candidate)
        terminal = FrameProcessor(frames, evaluator=evaluate_processing, clock=lambda: self.now).process_next(self.now)
        self.assertIsNotNone(terminal)
        self.assertEqual("COMPLETE", terminal.status.value)
        return pending.frame_sequence, self.now

    def _connection(self):
        connection = psycopg2.connect(**self.connection_kwargs)
        register_uuid(conn_or_curs=connection)
        return connection

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

@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run dispatch-strategy repository tests",
)
class DispatchStrategyPostgresTest(DispatchStrategyPostgresFixture, unittest.TestCase):
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

    def test_saving_assigned_wrong_soc_is_rejected_and_empty_draft_stays_valid(self) -> None:
        empty = replace(self._draft(), bindings=())
        strategy = self.repository.create_strategy(empty, "engineer:test")
        for definition_id, unit, code in (
            ("room.humidity", "%", "SOC_BINDING_DEFINITION_INVALID"),
            ("bms.soc", "kW", "SOC_BINDING_UNIT_INVALID"),
            ("bms.soc", "degC", "SOC_BINDING_UNIT_INVALID"),
        ):
            with self.subTest(definition=definition_id, unit=unit):
                with self._connection() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE t_entity_instances SET definition_id=%s,unit=%s WHERE id=%s",
                        (definition_id, unit, self.input_id),
                    )
                draft = self._draft()
                draft = replace(draft, bindings=(replace(draft.bindings[0], unit=unit), draft.bindings[1]))
                with self.assertRaisesRegex(StrategyModelError, code):
                    self.repository.create_strategy(draft, "engineer:test")
                with self.assertRaisesRegex(StrategyModelError, code):
                    self.repository.save_draft(strategy.id, draft, strategy.draft.content_digest, "engineer:test")
                current = self.repository.get_strategy(strategy.id)
                self.assertEqual(strategy.draft.content_digest, current.draft.content_digest)
                self.assertEqual((), current.draft.bindings)

    def test_partial_draft_can_be_saved_but_cannot_be_published(self) -> None:
        empty = self.repository.create_strategy(replace(self._draft(), bindings=()), "engineer:test")
        draft = self._draft()
        saved = self.repository.save_draft(
            empty.id, replace(draft, bindings=(draft.bindings[0],)),
            empty.draft.content_digest, "engineer:test",
        )
        self.assertEqual(1, len(saved.draft.bindings))
        with self.assertRaisesRegex(StrategyModelError, "STRATEGY_OUTPUT_REQUIRED"):
            self.repository.publish(saved.id, saved.draft.content_digest, self.configuration_revision, "engineer:test")

    def test_publish_rechecks_soc_definition_against_current_catalog(self) -> None:
        strategy = self.repository.create_strategy(self._draft(), "engineer:test")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE t_entity_instances SET definition_id='room.humidity' WHERE id=%s", (self.input_id,))
        with self.assertRaisesRegex(StrategyModelError, "SOC_BINDING_DEFINITION_INVALID"):
            self.repository.publish(strategy.id, strategy.draft.content_digest, self.configuration_revision, "engineer:test")
        self.assertEqual("DRAFT", self.repository.get_strategy(strategy.id).draft.lifecycle)

    def test_enabling_old_revision_rechecks_soc_before_acquiring_ownership(self) -> None:
        strategy = self.repository.create_strategy(self._draft(), "engineer:test")
        published = self.repository.publish(strategy.id, strategy.draft.content_digest, self.configuration_revision, "engineer:test")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE t_entity_instances SET definition_id='room.humidity' WHERE id=%s", (self.input_id,))
        with self.assertRaisesRegex(StrategyModelError, "SOC_BINDING_DEFINITION_INVALID"):
            self.repository.enable(strategy.id, published.id, "engineer:test")
        self.assertFalse(self.repository.get_strategy(strategy.id).enabled)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_dispatch_strategy_owners WHERE strategy_id=%s", (strategy.id,))
            self.assertEqual(0, cursor.fetchone()[0])

    def test_snapshot_retains_catalog_definition_for_runtime_semantic_check(self) -> None:
        strategy = self.repository.create_strategy(self._draft(), "engineer:test")
        _, now = self._commit_samples()
        snapshot = self.repository.load_snapshot(strategy.draft, None, now)
        soc = next(item for item in snapshot.inputs if item.entity_instance_id == self.input_id)
        self.assertEqual("bms.soc", getattr(soc, "definition_id", None))
        runtime = StrategyRuntime(self.repository, evaluator=lambda _content, _inputs: {"result": {"action_id": "power-target", "target": 50.0}})
        self.assertEqual("EVALUATED", runtime.simulate(strategy.draft.id, {}, now).status)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE t_entity_instances SET definition_id='room.humidity' WHERE id=%s", (self.input_id,))
        result = runtime.simulate(strategy.draft.id, {}, now)
        self.assertEqual(("BLOCKED", "SOC_BINDING_DEFINITION_INVALID", ()), (result.status, result.reason_code, result.intents))

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
