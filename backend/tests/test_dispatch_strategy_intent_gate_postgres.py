from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import os
import unittest
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

from app.services.dispatch_strategy_postgres import _json_safe
from app.services.dispatch_strategy_workers import ControlIntentDispatcher
from tests import test_dispatch_strategy_postgres as fixtures


@unittest.skipUnless(os.environ.get("ZIZU_POSTGRES_TEST") == "1", "requires isolated PostgreSQL")
class DispatchIntentGatePostgresTest(fixtures.DispatchStrategyPostgresFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        strategy = self.repository.create_strategy(self._draft(), "test:intent-gate")
        self.revision = self.repository.publish(strategy.id, strategy.draft.content_digest,
                                                self.configuration_revision, "test:intent-gate")
        self.repository.enable(strategy.id, self.revision.id, "test:intent-gate")
        frame, self.now = self._commit_samples()
        snapshot = self.repository.load_snapshot(self.revision, frame, self.now)
        self.evidence = _json_safe(asdict(snapshot))
        for sample in self.evidence["inputs"]:
            sample["binding_key"] = sample.pop("field_key")

    def _queue(self, evidence=None, *, status="PENDING", attempts=0):
        intent_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_dispatch_control_intents"
                "(id,strategy_id,revision_id,evaluation_key,action_id,ordinal,entity_instance_id,"
                "expected_value,status,attempt_count,snapshot_evidence,next_attempt_at) "
                "VALUES(%s,%s,%s,%s,'power-target',0,%s,'50.0',%s,%s,%s,%s)",
                (intent_id, self.revision.strategy_id, self.revision.id, str(intent_id),
                 self.output_id, status, attempts, Json(evidence if evidence is not None else self.evidence), self.now),
            )
        return intent_id

    def _state(self, intent_id):
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status,last_error_code,attempt_count FROM t_dispatch_control_intents WHERE id=%s", (intent_id,))
            return cursor.fetchone()

    def _sent(self):
        intent_id = self._queue(status="IN_FLIGHT", attempts=1)
        command_id, audit_id = uuid4(), uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO t_audit_events(id,event,outcome) VALUES(%s,'test:sent','dispatched')", (audit_id,))
            cursor.execute(
                "INSERT INTO t_control_commands(id,actor,source_type,capability,entity_instance_id,expected_value,"
                "data_type,policy_snapshot,status,code,idempotency_key,request_digest,audit_event_id) "
                "VALUES(%s,'test:intent-gate','strategy','control.write',%s,'50.0','FLOAT','{}',"
                "'dispatched','DISPATCHED',%s,%s,%s)",
                (command_id, self.output_id, str(command_id), 'a' * 64, audit_id),
            )
        self.repository.attach_command(intent_id, 1, command_id)
        return intent_id, command_id

    def test_old_incomplete_evidence_is_cancelled_before_new_write(self):
        intent_id = self._queue({"configuration_revision": self.configuration_revision})
        self.assertIsNone(self.repository.claim_next(self.now))
        self.assertEqual(("CANCELLED", "STRATEGY_EVIDENCE_INVALID", 0), self._state(intent_id))

    def test_old_wrong_soc_value_is_rejected_even_with_current_good_soc(self):
        self.evidence["inputs"][0]["value"] = 150.0
        intent_id = self._queue()
        self.assertIsNone(self.repository.claim_next(self.now))
        self.assertEqual(("CANCELLED", "SOC_VALUE_INVALID", 0), self._state(intent_id))

    def test_good_old_evidence_cannot_bypass_bad_current_sample(self):
        intent_id = self._queue()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE t_l2_latest SET quality=0 WHERE entity_instance_id=%s", (self.input_id,))
        self.assertIsNone(self.repository.claim_next(self.now))
        self.assertEqual(("CANCELLED", "L2_QUALITY_NOT_GOOD", 0), self._state(intent_id))

    def test_legacy_complete_evidence_without_definition_is_checked_against_catalog(self):
        for sample in self.evidence["inputs"]:
            sample.pop("definition_id")
        self._queue()
        self.assertIsNotNone(self.repository.claim_next(self.now))

    def test_malformed_evidence_is_cancelled_without_crashing_worker(self):
        self.evidence["inputs"][0]["frame_sequence"] = "not-a-frame"
        intent_id = self._queue()
        self.assertIsNone(self.repository.claim_next(self.now))
        self.assertEqual(("CANCELLED", "STRATEGY_EVIDENCE_INVALID", 0), self._state(intent_id))

    def test_future_source_timestamp_is_not_good_authority_to_write(self):
        self.evidence["inputs"][0]["observed_at"] = (self.now + timedelta(minutes=1)).isoformat()
        intent_id = self._queue()
        self.assertIsNone(self.repository.claim_next(self.now))
        self.assertEqual(("CANCELLED", "STRATEGY_EVIDENCE_INVALID", 0), self._state(intent_id))

    def test_blocked_strategy_cannot_issue_old_pending_intent(self):
        intent_id = self._queue()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE t_dispatch_strategies SET runtime_health='BLOCKED' WHERE id=%s", (self.revision.strategy_id,))
        self.assertIsNone(self.repository.claim_next(self.now))
        self.assertEqual(("CANCELLED", "STRATEGY_FENCE_CHANGED", 0), self._state(intent_id))

    def test_old_retry_cannot_submit_after_source_contract_changes(self):
        intent_id = self._queue(attempts=1)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE t_entity_instances SET definition_id='room.humidity' WHERE id=%s", (self.input_id,))
        self.assertIsNone(self.repository.claim_next(self.now))
        self.assertEqual(("CANCELLED", "SOC_BINDING_DEFINITION_INVALID", 1), self._state(intent_id))

    def test_unsent_inflight_intent_after_restart_is_revalidated(self):
        intent_id = self._queue({"configuration_revision": self.configuration_revision}, status="IN_FLIGHT", attempts=1)
        self.assertIsNone(self.repository.claim_next(self.now))
        self.assertEqual(("CANCELLED", "STRATEGY_EVIDENCE_INVALID", 1), self._state(intent_id))

    def test_old_evidence_expiry_prevents_new_write_even_if_it_was_good(self):
        intent_id = self._queue()
        self.assertIsNone(self.repository.claim_next(self.now + timedelta(seconds=11)))
        self.assertEqual("CANCELLED", self._state(intent_id)[0])

    def test_valid_intent_can_be_claimed_once_and_recovered_without_extra_attempt(self):
        intent_id = self._queue()
        first = self.repository.claim_next(self.now)
        self.assertIsNotNone(first)
        self.assertEqual((intent_id, 1, None), (first.id, first.attempt_count, first.control_command_id))
        recovered = self.repository.claim_next(self.now + timedelta(seconds=1))
        self.assertEqual((intent_id, 1), (recovered.id, recovered.attempt_count))

    def test_disable_between_claim_and_submit_still_prevents_device_write(self):
        intent_id = self._queue()
        original_claim = self.repository.claim_next

        def claim_then_disable(now):
            intent = original_claim(now)
            self.repository.disable(self.revision.strategy_id, "test:race")
            return intent

        self.repository.claim_next = claim_then_disable
        worker = ControlIntentDispatcher(self.repository, _ReadbackOnly(uuid4(), "dispatched"))
        self.assertIsNone(worker.run_once(self.now))
        self.assertEqual("CANCELLED", self._state(intent_id)[0])

    def test_submission_guard_holds_final_authority_until_submit_finishes(self):
        self._queue()
        intent = self.repository.claim_next(self.now)
        with self.repository.submission_guard(intent, self.now) as allowed:
            self.assertTrue(allowed)
            for statement, params in (
                ("UPDATE t_configuration_state SET current_revision=current_revision", ()),
                ("UPDATE t_dispatch_strategies SET enabled=FALSE WHERE id=%s", (self.revision.strategy_id,)),
                ("UPDATE t_l2_latest SET quality=0 WHERE entity_instance_id=%s", (self.input_id,)),
                ("UPDATE t_entity_instances SET active=FALSE WHERE id=%s", (self.input_id,)),
                ("UPDATE t_nodes SET enabled=FALSE WHERE id=%s", (self.node_id,)),
                ("UPDATE t_installed_point_processings SET current=FALSE WHERE node_id=%s", (self.node_id,)),
            ):
                with self.subTest(statement=statement), self._connection() as other, other.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout='100ms'")
                    with self.assertRaises(psycopg2.errors.LockNotAvailable):
                        cursor.execute(statement, params)
                    other.rollback()
        # No lock remains while waiting for a later physical readback.
        disabled = self.repository.disable(self.revision.strategy_id, "test:after-submit")
        self.assertFalse(disabled.enabled)

    def test_real_control_cooldown_can_submit_under_guard_without_self_deadlock(self):
        from app.services.automated_control_commands import AutomatedControlCommands
        from app.services.control_commands import ControlCommandRuntime, PostgresControlCommandRepository
        from app.services.entity_instance_postgres import PostgresEntityInstanceRepository, PostgresSourceCatalog, PostgresObservationCatalog
        from app.services.entity_instance_registry import EntityInstanceRegistry
        from app.services.entity_instance_runtime import EntityInstanceRuntime
        from app.services.configuration_revision_postgres import PostgresConfigurationRevisions
        from tests.test_control_command_runtime import RecordingDispatcher

        registry = EntityInstanceRegistry(PostgresEntityInstanceRepository(), PostgresSourceCatalog(), PostgresConfigurationRevisions().current)
        commands = PostgresControlCommandRepository()

        def bounded_connection():
            connection = self._connection()
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout='200ms'")
            return connection

        commands._connection = bounded_connection
        device = RecordingDispatcher()
        control = AutomatedControlCommands(ControlCommandRuntime(
            registry=registry, policies=PostgresEntityInstanceRepository(),
            readback=EntityInstanceRuntime(registry, PostgresObservationCatalog()),
            dispatcher=device, repository=commands, clock=lambda: self.now + timedelta(milliseconds=1),
        ))
        self._queue()
        result = ControlIntentDispatcher(self.repository, control, clock=lambda: self.now).run_once(self.now)
        self.assertEqual("IN_FLIGHT", result.status)
        self.assertEqual(1, len(device.requests))
        self.assertEqual(self.tag_id, device.requests[0].tag_id)

    def test_same_target_concurrent_cooldown_claims_have_only_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from app.services.control_commands import PostgresControlCommandRepository

        command_ids = [self._sent()[1], self._sent()[1]]
        commands = PostgresControlCommandRepository()
        barrier = Barrier(2)

        def reserve(command_id):
            barrier.wait(timeout=2)
            return commands.reserve_cooldown(self.output_id, command_id, self.now + timedelta(seconds=10), self.now)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(reserve, command_ids))
        self.assertEqual([False, True], sorted(results))

    def test_already_sent_command_is_read_back_after_disable_without_new_submission(self):
        intent_id, command_id = self._sent()
        self.repository.disable(self.revision.strategy_id, "test:intent-gate")
        control = _ReadbackOnly(command_id, "readback_confirmed")
        result = ControlIntentDispatcher(self.repository, control).run_once(self.now)
        self.assertIsNotNone(result)
        self.assertEqual("CONFIRMED", result.status)
        self.assertEqual([command_id], control.reads)
        self.assertEqual(("CONFIRMED", None, 1), self._state(intent_id))

    def test_already_sent_command_finishes_readback_after_configuration_changes(self):
        intent_id, command_id = self._sent()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_configuration_revisions(revision,previous_revision,actor,action,resource_kind,resource_id,after_digest) "
                "VALUES(%s,%s,'test','test:change','site','test',%s)",
                (self.configuration_revision + 1, self.configuration_revision, 'b' * 64),
            )
            cursor.execute("UPDATE t_configuration_state SET current_revision=current_revision+1")
        control = _ReadbackOnly(command_id, "readback_confirmed")
        result = ControlIntentDispatcher(self.repository, control).run_once(self.now + timedelta(minutes=1))
        self.assertEqual("CONFIRMED", result.status)
        self.assertEqual([command_id], control.reads)
        self.assertEqual(("CONFIRMED", None, 1), self._state(intent_id))

    def test_failed_readback_after_disable_does_not_resubmit(self):
        intent_id, command_id = self._sent()
        self.repository.disable(self.revision.strategy_id, "test:intent-gate")
        control = _ReadbackOnly(command_id, "timeout")
        worker = ControlIntentDispatcher(self.repository, control)
        self.assertIsNotNone(worker.run_once(self.now))
        self.assertEqual([command_id], control.reads)
        self.assertIsNone(worker.run_once(self.now + timedelta(seconds=2)))
        self.assertEqual(("CANCELLED", "STRATEGY_FENCE_CHANGED", 1), self._state(intent_id))


class _ReadbackOnly:
    def __init__(self, command_id, status):
        self.command_id, self.status, self.reads = command_id, status, []

    def submit(self, _request):
        raise AssertionError("Already submitted command must never be submitted again")

    def reconcile(self, command_id):
        self.reads.append(command_id)
        return SimpleNamespace(id=command_id, status=self.status, code=self.status.upper(),
                               timeout_at=None, policy_snapshot={"cooldown_seconds": 1})
