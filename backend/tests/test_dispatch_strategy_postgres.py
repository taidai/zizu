from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
import json
from pathlib import Path
from queue import Queue
import time
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
    def _advance_configuration(self) -> int:
        next_revision = self.configuration_revision + 1
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_configuration_revisions"
                "(revision,previous_revision,actor,action,resource_kind,resource_id,after_digest) "
                "VALUES(%s,%s,'test:strategy','test:change','site','test',%s)",
                (next_revision, self.configuration_revision, "e" * 64),
            )
            cursor.execute(
                "UPDATE t_configuration_state SET current_revision=%s WHERE singleton=TRUE",
                (next_revision,),
            )
        self.configuration_revision = next_revision
        return next_revision

    def test_save_rebases_missing_units_to_current_contract_without_mutating_published(self) -> None:
        original = replace(
            self._draft(),
            jdm_content={
                "nodes": [
                    {"id": "input", "type": "inputNode", "name": "Input"},
                    {"id": "custom", "type": "expressionNode", "content": {"expressions": []}},
                    {"id": "output", "type": "outputNode", "name": "Output"},
                ],
                "edges": [
                    {"id": "input-custom", "sourceId": "input", "targetId": "custom"},
                    {"id": "custom-output", "sourceId": "custom", "targetId": "output"},
                ],
                "metadata": {"preserve": {"layout": [3, 1, 2]}},
            },
        )
        original = replace(
            original,
            bindings=(
                original.bindings[0],
                StrategyBindingDraft("INPUT", "reserve-soc", 1, self.input_id, "FLOAT", "%", 37),
                original.bindings[1],
            ),
        )
        created = self.repository.create_strategy(original, "engineer:test")
        published = self.repository.publish(
            created.id, created.draft.content_digest,
            self.configuration_revision, "engineer:test",
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_dispatch_strategy_bindings SET unit=NULL "
                "WHERE revision_id=%s AND binding_key IN ('soc','power-target')",
                (published.id,),
            )
        legacy = self.repository.get_strategy(created.id).published_revision
        self.assertEqual([None, "%", None], [item.unit for item in legacy.bindings])
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_dispatch_strategies "
                "SET runtime_health='FAILED',failure_code='CONTROL_READBACK_MISMATCH' "
                "WHERE id=%s",
                (created.id,),
            )
        current_revision = self._advance_configuration()
        _, evaluated_at = self._commit_samples()
        runtime = StrategyRuntime(
            self.repository,
            evaluator=lambda _content, _inputs: {
                "result": {"action_id": "power-target", "target": 50.0}
            },
        )
        blocked = runtime.simulate(published.id, {}, evaluated_at)
        self.assertEqual(("BLOCKED", "L2_CONFIGURATION_MISMATCH", ()), (
            blocked.status, blocked.reason_code, blocked.intents,
        ))
        incoming = replace(
            original,
            base_configuration_revision=legacy.base_configuration_revision,
            bindings=legacy.bindings,
        )

        saved = self.repository.save_draft(
            created.id, incoming, legacy.content_digest, "engineer:test",
        )

        self.assertEqual(current_revision, saved.draft.base_configuration_revision)
        self.assertEqual(legacy.jdm_content, saved.draft.jdm_content)
        self.assertEqual(
            [
                ("INPUT", "soc", 0, self.input_id, "FLOAT", "%", 10),
                ("INPUT", "reserve-soc", 1, self.input_id, "FLOAT", "%", 37),
                ("OUTPUT", "power-target", 0, self.output_id, "FLOAT", "kW", 10),
            ],
            [
                (item.direction, item.binding_key, item.ordinal,
                 item.entity_instance_id, item.expected_data_type,
                 item.unit, item.freshness_seconds)
                for item in saved.draft.bindings
            ],
        )
        unchanged = self.repository.get_strategy(created.id).published_revision
        self.assertEqual(legacy, unchanged)
        self.assertFalse(saved.enabled)
        self.assertIsNone(saved.active_revision_id)
        self.assertEqual("FAILED", saved.runtime_health)
        self.assertEqual("CONTROL_READBACK_MISMATCH", saved.failure_code)
        evaluated = runtime.simulate(saved.draft.id, {}, evaluated_at)
        self.assertEqual("EVALUATED", evaluated.status)
        self.assertIsNone(evaluated.reason_code)

    def test_save_rebase_rejects_known_unit_change_for_same_entity(self) -> None:
        created = self.repository.create_strategy(self._draft(), "engineer:test")
        published = self.repository.publish(
            created.id, created.draft.content_digest,
            self.configuration_revision, "engineer:test",
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_entity_instances SET unit='W' WHERE id=%s",
                (self.output_id,),
            )
        self._advance_configuration()
        incoming = replace(
            self._draft(),
            base_configuration_revision=published.base_configuration_revision,
            bindings=published.bindings,
        )

        with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_UNIT_MISMATCH"):
            self.repository.save_draft(
                created.id, incoming, published.content_digest, "engineer:test",
            )

        view = self.repository.get_strategy(created.id)
        self.assertIsNone(view.draft)
        self.assertEqual(published, view.published_revision)

    def test_save_rebase_rejects_known_type_change_for_same_entity(self) -> None:
        created = self.repository.create_strategy(self._draft(), "engineer:test")
        published = self.repository.publish(
            created.id, created.draft.content_digest,
            self.configuration_revision, "engineer:test",
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_dispatch_strategy_bindings SET expected_data_type='INT' "
                "WHERE revision_id=%s AND binding_key='soc'",
                (published.id,),
            )
        legacy = self.repository.get_strategy(created.id).published_revision
        self._advance_configuration()
        incoming = replace(
            self._draft(),
            base_configuration_revision=legacy.base_configuration_revision,
            bindings=legacy.bindings,
        )

        with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_TYPE_MISMATCH"):
            self.repository.save_draft(
                created.id, incoming, legacy.content_digest, "engineer:test",
            )

        view = self.repository.get_strategy(created.id)
        self.assertIsNone(view.draft)
        self.assertEqual(legacy, view.published_revision)

    def test_relabeling_binding_cannot_hide_known_unit_change_for_same_entity(self) -> None:
        strategies = []
        for name in ("changed-key", "changed-ordinal", "changed-direction"):
            created = self.repository.create_strategy(self._draft(name), "engineer:test")
            published = self.repository.publish(
                created.id, created.draft.content_digest,
                self.configuration_revision, "engineer:test",
            )
            strategies.append((created.id, published))
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_entity_instances SET unit='W' WHERE id=%s",
                (self.output_id,),
            )
        self._advance_configuration()

        changes = (
            {"binding_key": "renamed-target", "ordinal": 0, "direction": "OUTPUT"},
            {"binding_key": "power-target", "ordinal": 1, "direction": "OUTPUT"},
            {"binding_key": "renamed-target", "ordinal": 1, "direction": "INPUT"},
        )
        for (strategy_id, published), changed in zip(strategies, changes, strict=True):
            with self.subTest(change=changed):
                output = next(
                    item for item in published.bindings
                    if item.entity_instance_id == self.output_id
                )
                relabeled = replace(output, unit="W", **changed)
                incoming = replace(
                    self._draft(),
                    base_configuration_revision=published.base_configuration_revision,
                    bindings=(published.bindings[0], relabeled),
                )
                with self.assertRaisesRegex(
                    StrategyModelError, "L2_BINDING_UNIT_MISMATCH",
                ):
                    self.repository.save_draft(
                        strategy_id, incoming, published.content_digest,
                        "engineer:test",
                    )
                self.assertIsNone(self.repository.get_strategy(strategy_id).draft)

    def test_relabeling_binding_cannot_hide_known_type_change_for_same_entity(self) -> None:
        created = self.repository.create_strategy(self._draft(), "engineer:test")
        published = self.repository.publish(
            created.id, created.draft.content_digest,
            self.configuration_revision, "engineer:test",
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_dispatch_strategy_bindings SET expected_data_type='INT' "
                "WHERE revision_id=%s AND entity_instance_id=%s",
                (published.id, self.output_id),
            )
        legacy = self.repository.get_strategy(created.id).published_revision
        self._advance_configuration()
        output = next(
            item for item in legacy.bindings
            if item.entity_instance_id == self.output_id
        )
        relabeled = replace(
            output,
            binding_key="renamed-target",
            expected_data_type="FLOAT",
        )
        incoming = replace(
            self._draft(),
            base_configuration_revision=legacy.base_configuration_revision,
            bindings=(legacy.bindings[0], relabeled),
        )

        with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_TYPE_MISMATCH"):
            self.repository.save_draft(
                created.id, incoming, legacy.content_digest, "engineer:test",
            )

        self.assertIsNone(self.repository.get_strategy(created.id).draft)

    def test_rebase_checks_every_historical_contract_for_reused_entity(self) -> None:
        draft = self._draft()
        draft = replace(
            draft,
            jdm_content={"nodes": [], "edges": [], "metadata": {"kind": "custom"}},
            bindings=(
                draft.bindings[0],
                StrategyBindingDraft(
                    "OUTPUT", "alias-target", 0, self.output_id, "FLOAT", "kW", 20,
                ),
                replace(draft.bindings[1], ordinal=1),
            ),
        )
        created = self.repository.create_strategy(draft, "engineer:test")
        published = self.repository.publish(
            created.id, created.draft.content_digest,
            self.configuration_revision, "engineer:test",
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_dispatch_strategy_bindings SET unit=NULL "
                "WHERE revision_id=%s AND binding_key='alias-target'",
                (published.id,),
            )
            cursor.execute(
                "UPDATE t_entity_instances SET unit='W' WHERE id=%s",
                (self.output_id,),
            )
        legacy = self.repository.get_strategy(created.id).published_revision
        self.assertEqual(
            [None, "kW"],
            [item.unit for item in legacy.bindings if item.entity_instance_id == self.output_id],
        )
        self._advance_configuration()
        alias = next(item for item in legacy.bindings if item.binding_key == "alias-target")
        incoming = replace(
            draft,
            base_configuration_revision=legacy.base_configuration_revision,
            bindings=(legacy.bindings[0], replace(alias, binding_key="renamed", unit="W")),
        )

        with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_UNIT_MISMATCH"):
            self.repository.save_draft(
                created.id, incoming, legacy.content_digest, "engineer:test",
            )

        self.assertIsNone(self.repository.get_strategy(created.id).draft)

    def test_concurrent_rebase_keeps_compare_and_swap_exclusive(self) -> None:
        created = self.repository.create_strategy(self._draft(), "engineer:test")
        published = self.repository.publish(
            created.id, created.draft.content_digest,
            self.configuration_revision, "engineer:test",
        )
        current_revision = self._advance_configuration()
        incoming = replace(
            self._draft(),
            description="rebased",
            base_configuration_revision=published.base_configuration_revision,
            bindings=published.bindings,
        )

        def save_once(actor: str):
            try:
                saved = self.repository.save_draft(
                    created.id, incoming, published.content_digest, actor,
                )
                return ("saved", saved.draft.content_digest)
            except StrategyRepositoryError as error:
                return (error.code, None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save_once, ("engineer:first", "engineer:second")))

        self.assertEqual(["STRATEGY_DRAFT_STALE", "saved"], sorted(item[0] for item in outcomes))
        view = self.repository.get_strategy(created.id)
        self.assertEqual(current_revision, view.draft.base_configuration_revision)
        self.assertEqual(published, view.published_revision)

    def test_save_locks_configuration_before_waiting_for_strategy(self) -> None:
        created = self.repository.create_strategy(self._draft(), "engineer:test")
        worker_pids: Queue[int] = Queue()

        def tracked_connection():
            connection = self._connection()
            worker_pids.put(connection.get_backend_pid())
            return connection

        repository = PostgresStrategyRepository(
            connection_factory=tracked_connection,
            compiler=lambda _content: "f" * 64,
        )
        holder = self._connection()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            with holder.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM t_dispatch_strategies WHERE id=%s FOR UPDATE",
                    (created.id,),
                )
            future = pool.submit(
                repository.save_draft,
                created.id,
                replace(self._draft(), description="wait for strategy"),
                created.draft.content_digest,
                "engineer:worker",
            )
            worker_pid = worker_pids.get(timeout=2)
            waiting = False
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self._connection() as observer, observer.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (worker_pid,),
                    )
                    row = cursor.fetchone()
                if row is not None and row[0] == "Lock":
                    waiting = True
                    break
                time.sleep(0.02)
            self.assertTrue(waiting, "save worker did not reach the strategy lock barrier")
            configuration_was_locked = False
            with self._connection() as challenger, challenger.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout='200ms'")
                try:
                    cursor.execute(
                        "UPDATE t_configuration_state "
                        "SET current_revision=current_revision WHERE singleton=TRUE"
                    )
                except psycopg2.errors.LockNotAvailable:
                    configuration_was_locked = True
            holder.rollback()
            saved = future.result(timeout=2)
            self.assertEqual("wait for strategy", saved.description)
            self.assertTrue(
                configuration_was_locked,
                "save must hold the configuration row before waiting for the strategy",
            )
        finally:
            holder.rollback()
            holder.close()
            pool.shutdown(wait=True)

    def test_old_good_sample_reports_input_stale_not_quality_failure(self) -> None:
        strategy = self.repository.create_strategy(self._draft(), "engineer:test")
        _, observed_at = self._commit_samples()
        runtime = StrategyRuntime(
            self.repository,
            evaluator=lambda _content, _inputs: {
                "result": {"action_id": "power-target", "target": 50.0}
            },
        )

        result = runtime.simulate(strategy.draft.id, {}, observed_at + timedelta(seconds=11))

        self.assertEqual(("BLOCKED", "L2_INPUT_STALE", ()), (
            result.status, result.reason_code, result.intents,
        ))

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
