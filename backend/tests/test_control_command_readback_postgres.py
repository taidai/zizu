from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from threading import Barrier, Event
import unittest

import psycopg2

from app.services.control_commands import ControlCommandRuntime, PostgresControlCommandRepository, SubmitControlCommand
from app.services.configuration_revision_postgres import PostgresConfigurationRevisions
from app.services.entity_instance_postgres import (
    PostgresEntityInstanceRepository, PostgresObservationCatalog, PostgresSourceCatalog,
)
from app.services.entity_instance_registry import EntityInstanceRegistry
from app.services.entity_instance_runtime import EntityInstanceRuntime
from tests.test_control_command_runtime import MutableClock, RecordingDispatcher
from tests.test_dispatch_strategy_postgres import DispatchStrategyPostgresFixture


@unittest.skipUnless(os.environ.get("ZIZU_POSTGRES_TEST") == "1", "requires isolated PostgreSQL")
class ControlReadbackPostgresTest(DispatchStrategyPostgresFixture, unittest.TestCase):
    """Real committed L2 and command/audit persistence; device dispatch stays a recorder."""

    def setUp(self):
        super().setUp()
        self._commit_samples()
        self.clock = MutableClock(self.now)
        self.registry = EntityInstanceRegistry(
            PostgresEntityInstanceRepository(), PostgresSourceCatalog(),
            PostgresConfigurationRevisions().current,
        )
        self.reader = EntityInstanceRuntime(self.registry, PostgresObservationCatalog())
        self.device = RecordingDispatcher()
        self.commands = PostgresControlCommandRepository()
        self.commands._connection = self._connection
        self.runtime = self._runtime(self.commands)

    def _runtime(self, commands, reader=None):
        return ControlCommandRuntime(
            registry=self.registry, policies=PostgresEntityInstanceRepository(),
            readback=reader or self.reader, dispatcher=self.device,
            repository=commands, clock=self.clock.now,
        )

    def _request(self):
        return SubmitControlCommand(
            actor="test:readback", source_type="manual", entity_instance_id=self.output_id,
            value=100.0, idempotency_key="readback-once",
        )

    def _new_sample(self, value, capture_beat=2):
        from app.services.data_trunk_contracts import TypedValue
        from app.services.data_trunk_conversion import evaluate_processing
        from app.services.data_trunk_postgres import PostgresFrameRepository
        from app.services.frame_processor import FrameProcessor
        from tests.test_data_frames_postgres import DataFramesPostgresTest

        candidate = DataFramesPostgresTest._multi_candidate(
            self, capture_beat=capture_beat, configuration_revision=self.configuration_revision,
            tag_specs=((self.soc_tag_id, "soc", TypedValue.float(50.0), "%"),
                       (self.tag_id, "limit", TypedValue.float(value), "kW")),
        )
        frames = PostgresFrameRepository(connection_factory=self._connection)
        frames.commit_pending(candidate)
        result = FrameProcessor(frames, evaluator=evaluate_processing, clock=lambda: self.now).process_next(self.now)
        self.assertEqual("COMPLETE", result.status.value)

    def test_waiting_evidence_is_durable_once_and_new_matching_l2_confirms_after_restart(self):
        first_sample = self.reader.read(self.output_id)
        command = self.runtime.submit(self._request())
        self.assertEqual("dispatched", command.status)
        self.assertEqual("CONTROL_READBACK_PENDING_MISMATCH", command.code)
        restarted_commands = PostgresControlCommandRepository()
        restarted_commands._connection = self._connection
        restarted = self._runtime(restarted_commands)
        self.assertEqual("dispatched", restarted.recover()[0].status)
        evidence = [event for event in restarted_commands.events(command.id) if event.readback_evidence]
        self.assertEqual(1, len(evidence))
        self.assertEqual({
            "entity_instance_id": str(self.output_id), "value": 156.8, "quality": 192,
            "observed_at": first_sample.observed_at.isoformat(), "event_id": str(first_sample.event_id),
        }, evidence[0].readback_evidence)
        self.now += timedelta(seconds=1)
        self.clock.advance(1)
        self._new_sample(100.0)
        confirmed = restarted.reconcile(command.id)
        self.assertEqual("readback_confirmed", confirmed.status)
        self.assertEqual(command.timeout_at, confirmed.timeout_at)
        self.assertEqual(command.origin_evidence, confirmed.origin_evidence)
        self.assertEqual(command.policy_snapshot, confirmed.policy_snapshot)
        self.assertEqual(command.id, restarted.submit(self._request()).id)
        self.assertEqual(1, len(self.device.requests))

    def test_deadline_uses_durable_evidence_and_terminal_cannot_be_reopened(self):
        command = self.runtime.submit(self._request())
        restarted_commands = PostgresControlCommandRepository()
        restarted_commands._connection = self._connection
        restarted = self._runtime(restarted_commands)
        self.clock.advance(10)
        terminal = restarted.recover()[0]
        self.assertEqual(("mismatch", "CONTROL_READBACK_MISMATCH"), (terminal.status, terminal.code))
        self.assertEqual(command.timeout_at, terminal.timeout_at)
        self.now += timedelta(seconds=1)
        self._new_sample(100.0)
        self.assertEqual("mismatch", restarted.reconcile(command.id).status)
        self.assertEqual(1, len(self.device.requests))

    def test_concurrent_first_mismatches_record_only_one_audited_observation(self):
        # Initial sample predates acceptance, so the command begins with no evidence.
        self.clock.advance(1)
        command = self.runtime.submit(self._request())
        self.assertEqual("CONTROL_DISPATCHED", command.code)
        self.now += timedelta(seconds=1)
        self._new_sample(150.0)
        barrier = Barrier(2)
        read = self.reader.read

        class SynchronizedReader:
            def read(self, entity_id):
                observation = read(entity_id)
                barrier.wait(timeout=5)
                return observation

        concurrent = self._runtime(self.commands, SynchronizedReader())
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: concurrent.reconcile(command.id), range(2)))
        self.assertEqual(["dispatched", "dispatched"], [item.status for item in results])
        evidence = [event for event in self.commands.events(command.id) if event.readback_evidence]
        self.assertEqual(1, len(evidence))
        self.assertEqual(150.0, evidence[0].readback_evidence["value"])
        self.assertEqual(1, len(self.device.requests))

    def test_audit_failure_rolls_back_waiting_marker(self):
        self.clock.advance(1)
        command = self.runtime.submit(self._request())
        self.now += timedelta(seconds=1)
        self._new_sample(150.0)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE t_audit_events ADD CONSTRAINT test_readback_audit_failure "
                "CHECK (NOT (details ? 'readback')) NOT VALID"
            )
        try:
            with self.assertRaises(psycopg2.errors.CheckViolation):
                self.runtime.reconcile(command.id)
            self.assertEqual("CONTROL_DISPATCHED", self.runtime.get(command.id).code)
            self.assertFalse(any(event.readback_evidence for event in self.commands.events(command.id)))
        finally:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute("ALTER TABLE t_audit_events DROP CONSTRAINT test_readback_audit_failure")
        self.assertEqual("CONTROL_READBACK_PENDING_MISMATCH", self.runtime.reconcile(command.id).code)
        self.assertEqual(1, len(self.device.requests))

    def test_delayed_mismatch_cannot_overwrite_confirmation_from_another_runtime(self):
        self.clock.advance(1)
        command = self.runtime.submit(self._request())
        self.now += timedelta(seconds=1)
        self._new_sample(150.0)
        captured, release = Event(), Event()
        read = self.reader.read

        class DelayedReader:
            def read(self, entity_id):
                observation = read(entity_id)
                captured.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("test did not release readback")
                return observation

        other = self._runtime(self.commands, DelayedReader())
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(other.reconcile, command.id)
            try:
                self.assertTrue(captured.wait(timeout=5))
                self.now += timedelta(seconds=1)
                self._new_sample(100.0, capture_beat=3)
                self.assertEqual("readback_confirmed", self.runtime.reconcile(command.id).status)
            finally:
                release.set()
            self.assertEqual("readback_confirmed", pending.result(timeout=5).status)
        self.assertFalse(any(event.readback_evidence for event in self.commands.events(command.id)))
        self.assertEqual(1, len(self.device.requests))
