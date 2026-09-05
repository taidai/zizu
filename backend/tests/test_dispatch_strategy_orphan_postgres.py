"""Recover a submitted attempt when the process dies before intent attachment."""
from __future__ import annotations

from datetime import timedelta
import os
from types import SimpleNamespace
import unittest
from uuid import uuid4

from psycopg2.extras import Json

from app.services.dispatch_strategies import StrategyRuntime, StrategyTrigger
from app.services.dispatch_strategy_workers import ControlIntentDispatcher, _attempt_key
from tests.test_dispatch_strategy_postgres import DispatchStrategyPostgresFixture


@unittest.skipUnless(os.environ.get("ZIZU_POSTGRES_TEST") == "1", "requires isolated PostgreSQL")
class DispatchOrphanPostgresTest(DispatchStrategyPostgresFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        strategy = self.repository.create_strategy(self._draft(), "test:orphan")
        revision = self.repository.publish(
            strategy.id, strategy.draft.content_digest, self.configuration_revision, "test:orphan",
        )
        self.repository.enable(strategy.id, revision.id, "test:orphan")
        frame, self.now = self._commit_samples()
        runtime = StrategyRuntime(
            self.repository,
            evaluator=lambda _content, _inputs: {"result": {"action_id": "power-target", "target": 50.0}},
        )
        result = runtime.evaluate(strategy.id, StrategyTrigger("FIXED_TICK", "orphan", self.now, frame))
        self.assertEqual("EVALUATED", result.status)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT next_attempt_at FROM t_dispatch_control_intents WHERE strategy_id=%s",
                (strategy.id,),
            )
            ready_at = cursor.fetchone()[0]
        self.intent = self.repository.claim_next(ready_at)
        self.assertIsNotNone(self.intent)
        self.assertEqual(("IN_FLIGHT", 1, None),
                         (self.intent.status, self.intent.attempt_count, self.intent.control_command_id))

    def _persist_submitted_command(self, *, actor=None, attempt=None):
        actor = actor or f"strategy:{self.intent.strategy_id}"
        key = _attempt_key(self.intent.id, self.intent.attempt_count if attempt is None else attempt)
        command_id, audit_id = uuid4(), uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_audit_events(id,event,outcome) VALUES(%s,'test:orphan','dispatched')",
                (audit_id,),
            )
            cursor.execute(
                "INSERT INTO t_control_commands"
                "(id,actor,source_type,capability,entity_instance_id,expected_value,data_type,"
                "policy_snapshot,status,code,idempotency_key,request_digest,audit_event_id) "
                "VALUES(%s,%s,'strategy','control.write',%s,%s,'FLOAT','{}',"
                "'dispatched','DISPATCHED',%s,%s,%s)",
                (command_id, actor, self.output_id, Json(self.intent.expected_value), key, "a" * 64, audit_id),
            )
            cursor.execute(
                "INSERT INTO t_control_command_idempotency(actor,idempotency_key,request_digest,command_id) "
                "VALUES(%s,%s,%s,%s)",
                (actor, key, "a" * 64, command_id),
            )
        # This is the crash boundary: command/idempotency committed, attach absent.
        self.assertEqual(("IN_FLIGHT", 1, None), self._intent_state())
        return command_id

    def _intent_state(self):
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,attempt_count,control_command_id FROM t_dispatch_control_intents WHERE id=%s",
                (self.intent.id,),
            )
            return cursor.fetchone()

    def test_submitted_orphan_is_attached_and_read_back_after_disable_and_expiry(self):
        command_id = self._persist_submitted_command()
        self.repository.disable(self.intent.strategy_id, "test:orphan")
        control = _ReadbackOnly(command_id)
        restarted = ControlIntentDispatcher(self.repository, control)

        result = restarted.run_once(self.now + timedelta(minutes=1))

        self.assertIsNotNone(result)
        self.assertEqual(("CONFIRMED", 1, command_id),
                         (result.status, result.attempt_count, result.control_command_id))
        self.assertEqual([command_id], control.reads)
        self.assertEqual(("CONFIRMED", 1, command_id), self._intent_state())
        self.assertIsNone(restarted.run_once(self.now + timedelta(minutes=2)))
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_control_commands")
            self.assertEqual(1, cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM t_control_command_idempotency")
            self.assertEqual(1, cursor.fetchone()[0])

    def test_orphan_lookup_does_not_attach_another_strategy_command(self):
        command_id = self._persist_submitted_command(actor=f"strategy:{uuid4()}")
        self._assert_unmatched_command_is_not_reconciled(command_id)

    def test_orphan_lookup_does_not_attach_another_attempt_command(self):
        command_id = self._persist_submitted_command(attempt=2)
        self._assert_unmatched_command_is_not_reconciled(command_id)

    def _assert_unmatched_command_is_not_reconciled(self, command_id):
        self.repository.disable(self.intent.strategy_id, "test:orphan")
        control = _ReadbackOnly(command_id)
        result = ControlIntentDispatcher(self.repository, control).run_once(self.now + timedelta(minutes=1))
        self.assertIsNone(result)
        self.assertEqual([], control.reads)
        self.assertEqual(("CANCELLED", 1, None), self._intent_state())


class _ReadbackOnly:
    def __init__(self, command_id):
        self.command_id = command_id
        self.reads = []

    def submit(self, _request):
        raise AssertionError("Recovery must not submit an already-persisted attempt")

    def reconcile(self, command_id):
        if command_id != self.command_id:
            raise AssertionError("Recovery attached an unrelated command")
        self.reads.append(command_id)
        return SimpleNamespace(id=command_id, status="readback_confirmed", code="CONTROL_READBACK_CONFIRMED",
                               timeout_at=None, policy_snapshot={})
