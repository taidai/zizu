"""Dispatch must use the same confirmed L2 sources as the entity catalog."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import os
import unittest

from app.services.dispatch_strategies import StrategyModelError, StrategyRuntime, StrategyTrigger
from app.services.entity_instance_postgres import PostgresEntityInstanceRepository
from tests.test_dispatch_strategy_postgres import DispatchStrategyPostgresFixture


UNAVAILABLE_SOURCES = ("unconfirmed", "non_current", "disabled_node")


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run confirmed dispatch-source tests",
)
class DispatchStrategyConfirmedPostgresTest(DispatchStrategyPostgresFixture, unittest.TestCase):
    @contextmanager
    def _unavailable_source(self, state):
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT installed_processing_id,output_id,entity_instance_id "
                "FROM t_point_processing_output_bindings WHERE entity_instance_id=%s",
                (self.input_id,),
            )
            binding = cursor.fetchone()
            if state == "disabled_node":
                cursor.execute("UPDATE t_nodes SET enabled=FALSE WHERE id=%s", (self.node_id,))
            else:
                # Preserve a legal legacy-active row without bypassing constraints.
                # Modern publication normally deactivates these entities; legacy
                # or incomplete records must still fail closed at the consumer.
                cursor.execute(
                    "UPDATE t_entity_instances SET source_kind='legacy_tag' "
                    "WHERE id=ANY(%s::uuid[])",
                    ([self.input_id, self.output_id],),
                )
                if state == "unconfirmed":
                    cursor.execute(
                        "DELETE FROM t_point_processing_output_bindings WHERE entity_instance_id=%s",
                        (self.input_id,),
                    )
                else:
                    cursor.execute(
                        "UPDATE t_installed_point_processings SET current=FALSE WHERE id=%s",
                        (binding[0],),
                    )
            cursor.execute("SELECT active FROM t_entity_instances WHERE id=%s", (self.input_id,))
            self.assertTrue(cursor.fetchone()[0])
        try:
            catalog_ids = {item.id for item in PostgresEntityInstanceRepository().list_instances()}
            self.assertNotIn(self.input_id, catalog_ids)
            yield
        finally:
            with self._connection() as connection, connection.cursor() as cursor:
                if state == "disabled_node":
                    cursor.execute("UPDATE t_nodes SET enabled=TRUE WHERE id=%s", (self.node_id,))
                else:
                    if state == "unconfirmed":
                        cursor.execute(
                            "INSERT INTO t_point_processing_output_bindings"
                            "(installed_processing_id,output_id,entity_instance_id) VALUES(%s,%s,%s)",
                            binding,
                        )
                    else:
                        cursor.execute(
                            "UPDATE t_installed_point_processings SET current=TRUE WHERE id=%s",
                            (binding[0],),
                        )
                    cursor.execute(
                        "UPDATE t_entity_instances SET source_kind='point_processing' "
                        "WHERE id=ANY(%s::uuid[])",
                        ([self.input_id, self.output_id],),
                    )

    def _published_strategy(self):
        strategy = self.repository.create_strategy(self._draft(), "test:engineer")
        revision = self.repository.publish(
            strategy.id, strategy.draft.content_digest,
            self.configuration_revision, "test:engineer",
        )
        return strategy, revision

    def _runtime(self):
        calls = []

        def evaluator(_content, inputs):
            calls.append(inputs)
            return {"result": {"action_id": "power-target", "target": 50.0}}

        return StrategyRuntime(self.repository, evaluator=evaluator), calls

    def test_current_l1_sources_support_complete_lifecycle_and_committed_runtime(self):
        catalog = {item.id: item for item in PostgresEntityInstanceRepository().list_instances()}
        self.assertTrue(catalog[self.input_id].confirmed)
        self.assertTrue(catalog[self.output_id].confirmed)
        strategy, revision = self._published_strategy()
        enabled = self.repository.enable(strategy.id, revision.id, "test:engineer")
        self.assertTrue(enabled.enabled)
        frame, now = self._commit_samples()
        runtime, calls = self._runtime()
        trial = runtime.simulate(revision.id, {}, now)
        self.assertEqual("EVALUATED", trial.status)
        result = runtime.evaluate(strategy.id, StrategyTrigger("FIXED_TICK", "confirmed", now, frame))
        self.assertEqual("EVALUATED", result.status)
        self.assertEqual(1, len(result.intents))
        self.assertEqual(2, len(calls))

    def test_create_rejects_unconfirmed_active_soc(self):
        for state in UNAVAILABLE_SOURCES:
            with self.subTest(state=state), self._unavailable_source(state):
                with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_UNAVAILABLE"):
                    self.repository.create_strategy(self._draft(), "test:engineer")
                self.assertEqual((), self.repository.list_strategies())

    def test_save_rejects_unconfirmed_soc_without_changing_empty_draft(self):
        for state in UNAVAILABLE_SOURCES:
            strategy = self.repository.create_strategy(replace(self._draft(), bindings=()), "test:engineer")
            with self.subTest(state=state), self._unavailable_source(state):
                with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_UNAVAILABLE"):
                    self.repository.save_draft(
                        strategy.id, self._draft(), strategy.draft.content_digest, "test:engineer",
                    )
                current = self.repository.get_strategy(strategy.id)
                self.assertEqual(strategy.draft.content_digest, current.draft.content_digest)
                self.assertEqual((), current.draft.bindings)

    def test_publish_rechecks_current_source_confirmation(self):
        for state in UNAVAILABLE_SOURCES:
            with self.subTest(state=state):
                strategy = self.repository.create_strategy(self._draft(), "test:engineer")
                with self._unavailable_source(state):
                    with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_UNAVAILABLE"):
                        self.repository.publish(
                            strategy.id, strategy.draft.content_digest,
                            self.configuration_revision, "test:engineer",
                        )
                    self.assertEqual("DRAFT", self.repository.get_strategy(strategy.id).draft.lifecycle)

    def test_enable_rechecks_confirmation_before_acquiring_ownership(self):
        strategy, revision = self._published_strategy()
        for state in UNAVAILABLE_SOURCES:
            with self.subTest(state=state), self._unavailable_source(state):
                with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_UNAVAILABLE"):
                    self.repository.enable(strategy.id, revision.id, "test:engineer")
                self.assertFalse(self.repository.get_strategy(strategy.id).enabled)
                with self._connection() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM t_dispatch_strategy_owners WHERE strategy_id=%s", (strategy.id,))
                    self.assertEqual(0, cursor.fetchone()[0])

    def test_simulation_excludes_unconfirmed_soc_before_jdm_even_with_override(self):
        strategy, revision = self._published_strategy()
        _, now = self._commit_samples()
        runtime, calls = self._runtime()
        for state in UNAVAILABLE_SOURCES:
            with self.subTest(state=state), self._unavailable_source(state):
                result = runtime.simulate(revision.id, {"soc": 50.0}, now)
                self.assertEqual(("BLOCKED", "L2_INPUT_MISSING", ()),
                                 (result.status, result.reason_code, result.intents))
                self.assertEqual([], calls)

    def test_enabled_runtime_excludes_unconfirmed_soc_and_persists_zero_intents(self):
        strategy, revision = self._published_strategy()
        self.repository.enable(strategy.id, revision.id, "test:engineer")
        frame, now = self._commit_samples()
        runtime, calls = self._runtime()
        for state in UNAVAILABLE_SOURCES:
            with self.subTest(state=state), self._unavailable_source(state):
                result = runtime.evaluate(strategy.id, StrategyTrigger("FIXED_TICK", state, now, frame))
                self.assertEqual(("BLOCKED", "L2_INPUT_MISSING", ()),
                                 (result.status, result.reason_code, result.intents))
                self.assertEqual([], calls)
                with self._connection() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM t_dispatch_control_intents WHERE strategy_id=%s", (strategy.id,))
                    self.assertEqual(0, cursor.fetchone()[0])
