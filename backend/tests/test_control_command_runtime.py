from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import io
import json
import os
import unittest
from unittest.mock import patch
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")


TARGET_ID = UUID("60000000-0000-0000-0000-000000000001")
READBACK_ID = UUID("60000000-0000-0000-0000-000000000002")
INTERLOCK_ID = UUID("60000000-0000-0000-0000-000000000003")


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeRegistry:
    def __init__(self, sources: dict[UUID, object]) -> None:
        self.sources = sources

    def resolve(self, entity_instance_id: UUID) -> object:
        return self.sources[entity_instance_id]


class FakePolicyCatalog:
    def __init__(self, policy: object) -> None:
        self.policy = policy
        self.instances = {
            (UUID("50000000-0000-0000-0000-000000000001"), "pcs.setpoint"): TARGET_ID,
            (UUID("50000000-0000-0000-0000-000000000001"), "pcs.readback"): READBACK_ID,
            (UUID("50000000-0000-0000-0000-000000000001"), "bms.ready"): INTERLOCK_ID,
        }

    def control_policy(self, entity_instance_id: UUID) -> object | None:
        return self.policy if entity_instance_id == TARGET_ID else None

    def entity_instance_for_definition(
        self,
        device_instance_id: UUID,
        definition_id: str,
    ) -> UUID | None:
        return self.instances.get((device_instance_id, definition_id))


class FakeReadback:
    def __init__(self) -> None:
        self.observations: dict[UUID, object] = {}

    def read(self, entity_instance_id: UUID) -> object:
        return self.observations[entity_instance_id]


class RecordingDispatcher:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.requests: list[object] = []

    def dispatch(self, request: object) -> None:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure


class ControlCommandRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        from app.services.control_commands import (
            ControlCommandRuntime,
            ControlInterlock,
            ControlPolicy,
            EntityInstanceObservation,
            InMemoryControlCommandRepository,
            ResolvedEntitySource,
        )

        self.clock = MutableClock(datetime(2026, 8, 14, 8, tzinfo=timezone.utc))
        node_id = UUID("50000000-0000-0000-0000-000000000001")
        control_tag_id = UUID("50000000-0000-0000-0000-000000000003")
        self.source = ResolvedEntitySource(
            entity_instance_id=TARGET_ID,
            definition_id="pcs.setpoint",
            node_key="PCS-01",
            node_id=node_id,
            data_type="FLOAT",
            unit="kW",
            direction="RW",
            freshness_seconds=30,
            control_tag_id=control_tag_id,
        )
        self.readback_source = ResolvedEntitySource(
            entity_instance_id=READBACK_ID,
            definition_id="pcs.readback",
            node_key="PCS-01",
            node_id=node_id,
            data_type="FLOAT",
            unit="kW",
            direction="R",
            freshness_seconds=30,
        )
        self.interlock_source = ResolvedEntitySource(
            entity_instance_id=INTERLOCK_ID,
            definition_id="bms.ready",
            node_key="PCS-01",
            node_id=node_id,
            data_type="BOOL",
            unit=None,
            direction="R",
            freshness_seconds=30,
        )
        self.policy = ControlPolicy(
            minimum=-100.0,
            maximum=100.0,
            cooldown_seconds=5,
            readback_definition="pcs.readback",
            tolerance=0.1,
            timeout_seconds=10,
            interlocks=(ControlInterlock("bms.ready", True),),
            high_risk=False,
        )
        self.repository = InMemoryControlCommandRepository()
        self.readback = FakeReadback()
        self.dispatcher = RecordingDispatcher()
        self.runtime = ControlCommandRuntime(
            registry=FakeRegistry(
                {
                    TARGET_ID: self.source,
                    READBACK_ID: self.readback_source,
                    INTERLOCK_ID: self.interlock_source,
                }
            ),
            policies=FakePolicyCatalog(self.policy),
            readback=self.readback,
            dispatcher=self.dispatcher,
            repository=self.repository,
            clock=self.clock.now,
        )

    def _observe(self, entity_instance_id: UUID, value: object, *, after_seconds: int = 1) -> None:
        from app.services.control_commands import EntityInstanceObservation

        source = {
            READBACK_ID: self.readback_source,
            INTERLOCK_ID: self.interlock_source,
        }[entity_instance_id]
        self.readback.observations[entity_instance_id] = EntityInstanceObservation(
            entity_instance_id=entity_instance_id,
            definition_id=source.definition_id,
            node_id=source.node_id,
            node_key=source.node_key,
            value=value,
            data_type=source.data_type,
            unit=source.unit,
            observed_at=self.clock.now() + timedelta(seconds=after_seconds),
            quality=192,
            age_ms=0,
            fresh=True,
            quality_good=True,
        )

    def _request(self, *, value: object = 20.0, key: str = "setpoint-1", confirmation_id=None):
        from app.services.control_commands import SubmitControlCommand

        return SubmitControlCommand(
            actor="user:operator-1",
            source_type="manual",
            entity_instance_id=TARGET_ID,
            value=value,
            idempotency_key=key,
            confirmation_id=confirmation_id,
        )

    def test_confirmed_command_is_idempotent_and_records_monotonic_states(self) -> None:
        self._observe(INTERLOCK_ID, True)
        self._observe(READBACK_ID, 20.05)

        command = self.runtime.submit(self._request())
        repeated = self.runtime.submit(self._request())

        self.assertEqual("readback_confirmed", command.status)
        self.assertEqual(command.id, repeated.id)
        self.assertEqual(1, len(self.dispatcher.requests))
        self.assertEqual(self.source.control_tag_id, self.dispatcher.requests[0].tag_id)
        self.assertEqual(
            ["accepted", "validated", "dispatched", "readback_confirmed"],
            [event.to_status for event in self.repository.events(command.id)],
        )
        self.assertEqual("control.write", command.capability)

    def test_unmapped_l2_control_target_is_rejected_without_device_write(self) -> None:
        from dataclasses import replace

        self.runtime._registry.sources[TARGET_ID] = replace(
            self.source,
            control_tag_id=None,
        )
        self._observe(INTERLOCK_ID, True)

        command = self.runtime.submit(self._request(key="unmapped-l2"))

        self.assertEqual(("rejected", "CONTROL_TARGET_UNMAPPED"), (command.status, command.code))
        self.assertEqual([], self.dispatcher.requests)

    def test_limit_and_interlock_rejections_never_dispatch(self) -> None:
        self._observe(INTERLOCK_ID, False)

        interlocked = self.runtime.submit(self._request(value=20.0, key="interlocked"))
        out_of_range = self.runtime.submit(self._request(value=101.0, key="out-of-range"))

        self.assertEqual(("rejected", "CONTROL_INTERLOCK_UNSATISFIED"), (interlocked.status, interlocked.code))
        self.assertEqual(("rejected", "CONTROL_VALUE_OUT_OF_RANGE"), (out_of_range.status, out_of_range.code))
        self.assertEqual([], self.dispatcher.requests)

    def test_retained_true_interlock_with_bad_quality_never_dispatches(self) -> None:
        self._observe(INTERLOCK_ID, True)
        current = self.readback.observations[INTERLOCK_ID]
        self.readback.observations[INTERLOCK_ID] = replace(
            current,
            quality=0,
            quality_good=False,
        )

        rejected = self.runtime.submit(self._request(key="bad-retained-interlock"))

        self.assertEqual(
            ("rejected", "CONTROL_INTERLOCK_UNAVAILABLE"),
            (rejected.status, rejected.code),
        )
        self.assertEqual([], self.dispatcher.requests)

    def test_fresh_readback_mismatch_waits_and_later_match_confirms_without_resend(self) -> None:
        self._observe(INTERLOCK_ID, True)
        self._observe(READBACK_ID, 10.0)

        command = self.runtime.submit(self._request())

        self.assertEqual(("dispatched", "CONTROL_READBACK_PENDING_MISMATCH"), (command.status, command.code))
        self.clock.advance(4)
        self._observe(READBACK_ID, 20.05, after_seconds=0)
        confirmed = self.runtime.reconcile(command.id)
        self.assertEqual("readback_confirmed", confirmed.status)
        self.assertEqual(command.timeout_at, confirmed.timeout_at)
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_first_mismatch_evidence_survives_restart_until_original_deadline(self) -> None:
        self._observe(INTERLOCK_ID, True, after_seconds=0)
        self._observe(READBACK_ID, 10.0, after_seconds=0)
        event_id = UUID("70000000-0000-0000-0000-000000000001")
        self.readback.observations[READBACK_ID] = replace(
            self.readback.observations[READBACK_ID], event_id=event_id,
        )
        command = self.runtime.submit(self._request())
        first_observed_at = self.clock.now()
        restarted = type(self.runtime)(
            registry=self.runtime._registry, policies=self.runtime._policies,
            readback=self.readback, dispatcher=self.dispatcher,
            repository=self.repository, clock=self.clock.now,
        )
        self.clock.advance(9)
        self._observe(READBACK_ID, 12.0, after_seconds=0)
        self.assertEqual("dispatched", restarted.recover()[0].status)
        pending = [event for event in self.repository.events(command.id)
                   if event.code == "CONTROL_READBACK_PENDING_MISMATCH"]
        self.assertEqual(1, len(pending))
        self.assertEqual({
            "entity_instance_id": str(READBACK_ID), "value": 10.0,
            "observed_at": first_observed_at.isoformat(), "quality": 192,
            "event_id": str(event_id),
        }, pending[0].readback_evidence)
        self.clock.advance(1)
        failed = restarted.recover()[0]
        self.assertEqual(("mismatch", "CONTROL_READBACK_MISMATCH"), (failed.status, failed.code))
        self.assertEqual(command.timeout_at, failed.timeout_at)
        self.assertEqual(command.origin_evidence, failed.origin_evidence)
        self.assertEqual(command.policy_snapshot, failed.policy_snapshot)
        self.assertEqual(1, len(self.dispatcher.requests))

    def _submit_with_delayed_dispatch(self, old_value: float):
        self._observe(INTERLOCK_ID, True, after_seconds=0)
        original_dispatch = self.dispatcher.dispatch

        def delayed_dispatch(request: object) -> None:
            self.clock.advance(1)
            self._observe(READBACK_ID, old_value, after_seconds=0)
            self.clock.advance(2)
            original_dispatch(request)

        self.dispatcher.dispatch = delayed_dispatch
        return self.runtime.submit(self._request())

    def test_pre_dispatch_matching_sample_cannot_confirm_after_delayed_acceptance(self) -> None:
        started_at = self.clock.now()
        command = self._submit_with_delayed_dispatch(20.0)

        self.assertEqual(("dispatched", "CONTROL_DISPATCHED"), (command.status, command.code))
        self.assertEqual(started_at, command.created_at)
        self.assertEqual(started_at + timedelta(seconds=3), command.dispatched_at)
        dispatched_event = self.repository.events(command.id)[-1]
        self.assertEqual(command.dispatched_at, dispatched_event.at)
        restarted = type(self.runtime)(
            registry=self.runtime._registry,
            policies=self.runtime._policies,
            readback=self.readback,
            dispatcher=self.dispatcher,
            repository=self.repository,
            clock=self.clock.now,
        )
        self.assertEqual("dispatched", restarted.recover()[0].status)
        self.assertEqual(command.id, restarted.submit(self._request()).id)
        self.clock.advance(1)
        self._observe(READBACK_ID, 20.05, after_seconds=0)
        self.assertEqual("readback_confirmed", restarted.reconcile(command.id).status)
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_pre_dispatch_different_sample_cannot_fail_after_delayed_acceptance(self) -> None:
        command = self._submit_with_delayed_dispatch(10.0)

        self.assertEqual(("dispatched", "CONTROL_DISPATCHED"), (command.status, command.code))
        self.clock.advance(1)
        self._observe(READBACK_ID, 10.0, after_seconds=0)
        mismatch = self.runtime.reconcile(command.id)
        self.assertEqual(("dispatched", "CONTROL_READBACK_PENDING_MISMATCH"), (mismatch.status, mismatch.code))
        self.clock.advance(1)
        self._observe(READBACK_ID, 20.0, after_seconds=0)
        self.assertEqual("readback_confirmed", self.runtime.reconcile(command.id).status)

    def test_invalid_readback_never_counts_as_confirmation_or_mismatch_evidence(self) -> None:
        for index, changes in enumerate((
            {"fresh": False},
            {"quality": 0, "quality_good": False},
            {"quality": 1, "quality_good": False},
            {"quality": 0, "quality_good": True},
            {"observed_at": self.clock.now() - timedelta(seconds=1)},
        )):
            for value in (10.0, 20.0):
                with self.subTest(changes=changes, value=value):
                    self.setUp()
                    self._observe(INTERLOCK_ID, True, after_seconds=0)
                    self._observe(READBACK_ID, value, after_seconds=0)
                    self.readback.observations[READBACK_ID] = replace(
                        self.readback.observations[READBACK_ID], **changes,
                    )
                    command = self.runtime.submit(self._request(key=f"invalid-readback-{index}-{value}"))
                    self.assertEqual(("dispatched", "CONTROL_DISPATCHED"), (command.status, command.code))
                    self.clock.advance(10)
                    result = self.runtime.reconcile(command.id)
                    self.assertEqual(("timeout", "CONTROL_READBACK_TIMEOUT"), (result.status, result.code))
                    self.assertFalse(any(event.readback_evidence for event in self.repository.events(command.id)))
                    self.assertEqual(1, len(self.dispatcher.requests))

    def test_delayed_dispatch_does_not_extend_original_timeout(self) -> None:
        started_at = self.clock.now()
        command = self._submit_with_delayed_dispatch(10.0)

        self.assertEqual("dispatched", command.status)
        self.assertEqual(started_at + timedelta(seconds=10), command.timeout_at)
        self.clock.advance(7)
        timed_out = self.runtime.reconcile(command.id)
        self.assertEqual(("timeout", "CONTROL_READBACK_TIMEOUT"), (timed_out.status, timed_out.code))
        self.assertEqual(1, len(self.dispatcher.requests))

    def _reconcile_after_slow_read(self, *, observed_after_deadline=False, failure=False):
        self._observe(INTERLOCK_ID, True, after_seconds=0)
        command = self.runtime.submit(self._request())
        self.assertEqual("dispatched", command.status)
        self.clock.advance(9)
        self._observe(READBACK_ID, 20.0, after_seconds=0)
        original_read = self.readback.read

        def slow_read(entity_id):
            self.clock.advance(2)
            if failure:
                raise RuntimeError("readback unavailable")
            if observed_after_deadline:
                self._observe(READBACK_ID, 20.0, after_seconds=0)
            return original_read(entity_id)

        self.readback.read = slow_read
        result = self.runtime.reconcile(command.id)
        self.assertEqual(("timeout", "CONTROL_READBACK_TIMEOUT"), (result.status, result.code))
        self.assertEqual(command.timeout_at, result.timeout_at)
        self.assertEqual(self.clock.now(), self.repository.events(command.id)[-1].at)
        self.assertEqual("timeout", self.runtime.reconcile(command.id).status)
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_readback_observed_after_deadline_cannot_confirm_during_slow_query(self) -> None:
        self._reconcile_after_slow_read(observed_after_deadline=True)

    def test_readback_query_finishing_after_deadline_cannot_confirm_earlier_sample(self) -> None:
        self._reconcile_after_slow_read()

    def test_readback_error_after_deadline_finishes_as_timeout_immediately(self) -> None:
        self._reconcile_after_slow_read(failure=True)

    def test_slow_read_after_a_timely_mismatch_expires_as_mismatch_not_success(self) -> None:
        self._observe(INTERLOCK_ID, True, after_seconds=0)
        self._observe(READBACK_ID, 10.0, after_seconds=0)
        command = self.runtime.submit(self._request())
        self.clock.advance(9)
        self._observe(READBACK_ID, 20.0, after_seconds=0)
        original_read = self.readback.read

        def slow_read(entity_id):
            self.clock.advance(2)
            return original_read(entity_id)

        self.readback.read = slow_read
        result = self.runtime.reconcile(command.id)
        self.assertEqual(("mismatch", "CONTROL_READBACK_MISMATCH"), (result.status, result.code))
        self.assertEqual(command.timeout_at, result.timeout_at)
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_first_different_sample_returned_after_deadline_is_not_timely_evidence(self) -> None:
        self._observe(INTERLOCK_ID, True, after_seconds=0)
        command = self.runtime.submit(self._request())
        self.clock.advance(9)
        self._observe(READBACK_ID, 10.0, after_seconds=0)
        original_read = self.readback.read

        def slow_read(entity_id):
            self.clock.advance(2)
            return original_read(entity_id)

        self.readback.read = slow_read
        result = self.runtime.reconcile(command.id)
        self.assertEqual(("timeout", "CONTROL_READBACK_TIMEOUT"), (result.status, result.code))
        self.assertFalse(any(event.readback_evidence for event in self.repository.events(command.id)))

    def test_expiry_uses_mismatch_persisted_by_another_reconcile(self) -> None:
        self._observe(INTERLOCK_ID, True, after_seconds=0)
        command = self.runtime.submit(self._request())
        self._observe(READBACK_ID, 10.0, after_seconds=0)
        original_read = self.readback.read

        def overlapping_read(entity_id):
            self.readback.read = original_read
            self.runtime.reconcile(command.id)
            self.clock.advance(10)
            return original_read(entity_id)

        self.readback.read = overlapping_read
        result = self.runtime.reconcile(command.id)
        self.assertEqual(("mismatch", "CONTROL_READBACK_MISMATCH"), (result.status, result.code))
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_inflight_mismatch_cannot_overwrite_concurrent_confirmation(self) -> None:
        self._observe(INTERLOCK_ID, True, after_seconds=0)
        command = self.runtime.submit(self._request())
        self._observe(READBACK_ID, 10.0, after_seconds=0)
        different = self.readback.observations[READBACK_ID]
        original_read = self.readback.read

        def overlapping_read(entity_id):
            self.readback.read = original_read
            self._observe(READBACK_ID, 20.0, after_seconds=0)
            self.runtime.reconcile(command.id)
            return different

        self.readback.read = overlapping_read
        result = self.runtime.reconcile(command.id)
        self.assertEqual("readback_confirmed", result.status)
        self.assertFalse(any(event.readback_evidence for event in self.repository.events(command.id)))
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_existing_terminal_mismatch_is_not_reopened_by_matching_readback(self) -> None:
        self._observe(INTERLOCK_ID, True, after_seconds=0)
        command = self.runtime.submit(self._request())
        # A pre-upgrade terminal command is an immutable historical fact.
        self.repository.update(
            replace(command, status="mismatch", code="CONTROL_READBACK_MISMATCH"),
            occurred_at=self.clock.now(),
        )
        self._observe(READBACK_ID, 20.0, after_seconds=0)
        self.assertEqual("mismatch", self.runtime.reconcile(command.id).status)
        self.assertEqual(command.id, self.runtime.submit(self._request()).id)
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_missing_readback_times_out_after_restart(self) -> None:
        self._observe(INTERLOCK_ID, True)
        dispatched = self.runtime.submit(self._request())

        restarted = type(self.runtime)(
            registry=self.runtime._registry,
            policies=self.runtime._policies,
            readback=self.readback,
            dispatcher=self.dispatcher,
            repository=self.repository,
            clock=self.clock.now,
        )
        self.clock.advance(11)
        timed_out = restarted.recover()[0]

        self.assertEqual("dispatched", dispatched.status)
        self.assertEqual(("timeout", "CONTROL_READBACK_TIMEOUT"), (timed_out.status, timed_out.code))

    def test_strategy_requires_valid_process_local_source_deadline_before_dispatch(self) -> None:
        for index, deadline in enumerate((None, True, "2099-01-01T00:00:00Z", self.clock.now().replace(tzinfo=None))):
            with self.subTest(deadline=deadline):
                self._observe(INTERLOCK_ID, True)
                request = replace(
                    self._request(key=f"invalid-deadline-{index}"), source_type="strategy",
                    source_fresh_until=deadline,
                    origin_evidence={"source_fresh_until": "2099-01-01T00:00:00Z"},
                )
                result = self.runtime.submit(request)
                self.assertEqual(("rejected", "CONTROL_INPUT_DEADLINE_INVALID"), (result.status, result.code))
        self.assertEqual([], self.dispatcher.requests)

    def test_slow_command_persistence_cannot_dispatch_expired_strategy_input(self) -> None:
        self._observe(INTERLOCK_ID, True)
        deadline = self.clock.now() + timedelta(seconds=5)
        original_save = self.repository.save

        def slow_save(command, *, idempotent):
            saved = original_save(command, idempotent=idempotent)
            self.clock.advance(6)
            return saved

        self.repository.save = slow_save
        request = replace(self._request(), source_type="strategy", source_fresh_until=deadline)
        result = self.runtime.submit(request)
        self.assertEqual(("rejected", "CONTROL_INPUT_STALE"), (result.status, result.code))
        self.assertEqual([], self.dispatcher.requests)

    def test_strategy_deadline_remains_inclusive_without_allowing_later_dispatch(self) -> None:
        self._observe(INTERLOCK_ID, True)
        deadline = self.clock.now()
        request = replace(self._request(), source_type="strategy", source_fresh_until=deadline)
        result = self.runtime.submit(request)
        self.assertEqual("dispatched", result.status)
        self.assertEqual(deadline, self.dispatcher.requests[0].source_fresh_until)
        self.clock.value += timedelta(microseconds=1)
        expired = self.runtime.submit(replace(request, idempotency_key="expired-input"))
        self.assertEqual(("rejected", "CONTROL_INPUT_STALE"), (expired.status, expired.code))
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_slow_neuron_target_lookup_or_login_cannot_send_expired_strategy_write(self) -> None:
        from app.core.config import settings
        from app.services.control_commands import NeuronControlDispatcher

        for index, stage in enumerate(("target", "login")):
            with self.subTest(stage=stage):
                self.clock.advance(6)
                self._observe(INTERLOCK_ID, True)
                deadline = self.clock.now() + timedelta(seconds=5)
                self.runtime._dispatcher = NeuronControlDispatcher(clock=self.clock.now)
                requests = []

                def target_row():
                    if stage == "target":
                        self.clock.advance(6)
                    return ("PCS-01", "setpoint", "NEURON", "driver/cmd/setpoint")

                def http_response(request, **kwargs):
                    requests.append(request)
                    if request.full_url.endswith("/api/v2/login"):
                        if stage == "login":
                            self.clock.advance(6)
                        return io.BytesIO(b'{"token":"test-neuron-token"}')
                    return io.BytesIO(b'{"error":0}')

                with (
                    patch("app.services.telemetry_store.get_connection") as connection,
                    patch.object(settings, "neuron_password", "test-neuron-write-secret"),
                    patch("urllib.request.urlopen", side_effect=http_response),
                ):
                    cursor = connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
                    cursor.fetchone.side_effect = target_row
                    result = self.runtime.submit(replace(
                        self._request(key=f"slow-neuron-{index}"), source_type="strategy",
                        source_fresh_until=deadline,
                    ))
                self.assertEqual(("rejected", "CONTROL_INPUT_STALE"), (result.status, result.code))
                self.assertFalse(any(request.full_url.endswith("/api/v2/write") for request in requests))

    def test_dispatch_failure_is_not_reported_as_success(self) -> None:
        self._observe(INTERLOCK_ID, True)
        self.runtime._dispatcher.failure = RuntimeError("gateway rejected")

        command = self.runtime.submit(self._request())

        self.assertEqual(("failed", "CONTROL_DISPATCH_FAILED"), (command.status, command.code))

    def _submit_via_neuron(self, response: bytes, *, key: str, source_fresh_until=None):
        from app.core.config import settings
        from app.services.control_commands import NeuronControlDispatcher

        self.runtime._dispatcher = NeuronControlDispatcher(clock=self.clock.now)
        self._observe(INTERLOCK_ID, True)
        with (
            patch("app.services.telemetry_store.get_connection") as connection,
            patch.object(settings, "neuron_password", "test-neuron-write-secret"),
            patch("urllib.request.urlopen", side_effect=[
                io.BytesIO(b'{"token":"test-neuron-token"}'),
                io.BytesIO(response),
            ]) as http,
        ):
            cursor = connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = ("PCS-01", "setpoint", "NEURON", "driver/cmd/setpoint")
            request = self._request(key=key)
            if source_fresh_until is not None:
                request = replace(request, source_type="strategy", source_fresh_until=source_fresh_until)
            command = self.runtime.submit(request)
            repeated = self.runtime.submit(request)

        self.assertEqual(command.id, repeated.id)
        self.assertEqual(2, http.call_count)  # One login and one write; no replay write.
        write = http.call_args_list[1].args[0]
        self.assertTrue(write.full_url.endswith("/api/v2/write"))
        self.assertEqual("POST", write.method)
        self.assertEqual(
            {"node": "driver", "group": "cmd", "tag": "setpoint", "value": 20.0},
            json.loads(write.data),
        )
        return command

    def test_neuron_requires_explicit_integer_zero_write_ack(self) -> None:
        responses = (
            b"", b"null", b"[]", b"true", b"0", b'"accepted"', b"{}",
            b'{"error":null}', b'{"error":false}', b'{"error":0.0}',
            b'{"error":"0"}', b'{"error":3004}', b'{"error":',
        )
        for index, response in enumerate(responses):
            with self.subTest(response=response):
                self.clock.advance(6)
                command = self._submit_via_neuron(response, key=f"neuron-invalid-ack-{index}")
                self.assertEqual(("failed", "CONTROL_DISPATCH_FAILED"), (command.status, command.code))
                self.assertIsNone(command.dispatched_at)
                self.assertEqual(
                    ["accepted", "validated", "failed"],
                    [event.to_status for event in self.repository.events(command.id)],
                )

    def test_neuron_write_ack_still_requires_new_l2_readback(self) -> None:
        self._observe(READBACK_ID, 20.0, after_seconds=-1)
        command = self._submit_via_neuron(b'{"error":0}', key="neuron-valid-ack")

        self.assertEqual(("dispatched", "CONTROL_DISPATCHED"), (command.status, command.code))
        self.clock.advance(1)
        self._observe(READBACK_ID, 20.0, after_seconds=0)
        confirmed = self.runtime.reconcile(command.id)
        self.assertEqual("readback_confirmed", confirmed.status)
        self.assertEqual(
            ["accepted", "validated", "dispatched", "readback_confirmed"],
            [event.to_status for event in self.repository.events(command.id)],
        )

    def test_valid_strategy_deadline_neuron_ack_still_needs_new_l2_readback(self) -> None:
        self._observe(READBACK_ID, 20.0, after_seconds=-1)
        command = self._submit_via_neuron(
            b'{"error":0}', key="strategy-valid-ack",
            source_fresh_until=self.clock.now() + timedelta(seconds=5),
        )
        self.assertEqual("dispatched", command.status)
        self.assertNotIn("source_fresh_until", command.public_dict())
        self.clock.advance(1)
        self._observe(READBACK_ID, 20.0, after_seconds=0)
        self.assertEqual("readback_confirmed", self.runtime.reconcile(command.id).status)

    def test_persistent_cooldown_and_reused_key_protect_after_restart(self) -> None:
        self._observe(INTERLOCK_ID, True)
        self._observe(READBACK_ID, 20.0)
        accepted = self.runtime.submit(self._request())
        restarted = type(self.runtime)(
            registry=self.runtime._registry,
            policies=self.runtime._policies,
            readback=self.readback,
            dispatcher=self.dispatcher,
            repository=self.repository,
            clock=self.clock.now,
        )

        repeated = restarted.submit(self._request())
        cooling_down = restarted.submit(self._request(value=21.0, key="setpoint-2"))

        self.assertEqual(accepted.id, repeated.id)
        self.assertEqual(("rejected", "CONTROL_COOLDOWN_ACTIVE"), (cooling_down.status, cooling_down.code))

    def test_recovery_never_redispatches_an_already_saved_command(self) -> None:
        self._observe(INTERLOCK_ID, True)
        original_dispatch = self.runtime._dispatcher.dispatch

        def restart_during_dispatch(request: object) -> None:
            original_dispatch(request)
            raise KeyboardInterrupt("process interrupted after persistence")

        self.runtime._dispatcher.dispatch = restart_during_dispatch
        with self.assertRaises(KeyboardInterrupt):
            self.runtime.submit(self._request())
        restarted_dispatcher = RecordingDispatcher()
        restarted = type(self.runtime)(
            registry=self.runtime._registry,
            policies=self.runtime._policies,
            readback=self.readback,
            dispatcher=restarted_dispatcher,
            repository=self.repository,
            clock=self.clock.now,
        )

        recovered = restarted.recover()

        self.assertEqual([], restarted_dispatcher.requests)
        self.assertEqual(1, len(recovered))
        self.assertEqual("failed", recovered[0].status)
        self.assertEqual("CONTROL_DISPATCH_INTERRUPTED", recovered[0].code)

    def test_command_keeps_readback_contract_when_current_policy_changes(self) -> None:
        self._observe(INTERLOCK_ID, True)
        command = self.runtime.submit(self._request())
        self.runtime._policies.policy = type(self.policy)(
            minimum=-1.0,
            maximum=1.0,
            cooldown_seconds=0,
            readback_definition="bms.ready",
            tolerance=None,
            timeout_seconds=1,
            interlocks=(),
            high_risk=False,
        )
        self._observe(READBACK_ID, 20.05)

        confirmed = self.runtime.reconcile(command.id)

        self.assertEqual("readback_confirmed", confirmed.status)

    def test_high_risk_confirmation_is_bound_to_actor_and_command_content(self) -> None:
        from app.services.control_commands import ControlPolicy

        self.policy = ControlPolicy(
            minimum=-100.0,
            maximum=100.0,
            cooldown_seconds=0,
            readback_definition="pcs.readback",
            tolerance=0.1,
            timeout_seconds=10,
            interlocks=(self.policy.interlocks[0],),
            high_risk=True,
        )
        self.runtime._policies.policy = self.policy
        self._observe(INTERLOCK_ID, True)
        self._observe(READBACK_ID, 20.0)

        missing = self.runtime.submit(self._request())
        confirmation = self.runtime.request_confirmation(self._request())
        changed = self.runtime.submit(self._request(value=21.0, key="changed", confirmation_id=confirmation.id))
        confirmed = self.runtime.submit(self._request(confirmation_id=confirmation.id))

        self.assertEqual(("rejected", "CONTROL_CONFIRMATION_REQUIRED"), (missing.status, missing.code))
        self.assertEqual(("rejected", "CONTROL_CONFIRMATION_INVALID"), (changed.status, changed.code))
        self.assertEqual("readback_confirmed", confirmed.status)

    def test_rule_trigger_records_evidence_and_uses_persistent_command_cooldown(self) -> None:
        """A rule is an automation subject, never a second device-write path."""
        from app.services.automated_control_commands import (
            AutomatedControlCommandRequest,
            AutomatedControlCommands,
        )

        rule_id = UUID("70000000-0000-0000-0000-000000000001")
        self._observe(INTERLOCK_ID, True)
        self._observe(READBACK_ID, 20.0)
        request = AutomatedControlCommandRequest(
            source_type="rule",
            subject_id=rule_id,
            subject_version=3,
            action_key="output.setpoint",
            entity_instance_id=TARGET_ID,
            value=20.0,
            trigger_evidence={
                "inputs": [{"entity_instance_id": str(INTERLOCK_ID), "value": True}],
                "outputs": {
                    "setpoint": 20.0,
                    "command": {"node": "must-not-persist", "tag": "must-not-persist"},
                    "command.node": "must-not-persist",
                },
            },
        )

        commands = AutomatedControlCommands(self.runtime)
        submitted = commands.submit(request)
        replayed = commands.submit(request)
        restarted = AutomatedControlCommands(
            type(self.runtime)(
                registry=self.runtime._registry,
                policies=self.runtime._policies,
                readback=self.readback,
                dispatcher=self.dispatcher,
                repository=self.repository,
                clock=self.clock.now,
            )
        )
        cooling_down = restarted.submit(
            AutomatedControlCommandRequest(
                source_type="rule",
                subject_id=rule_id,
                subject_version=3,
                action_key="output.setpoint",
                entity_instance_id=TARGET_ID,
                value=21.0,
                trigger_evidence={
                    "inputs": [{"entity_instance_id": str(INTERLOCK_ID), "value": True}],
                    "outputs": {"setpoint": 21.0},
                },
            )
        )

        self.assertEqual("rule", submitted.source_type)
        self.assertEqual(20.0, submitted.origin_evidence["trigger"]["outputs"]["setpoint"])
        self.assertNotIn("command", submitted.origin_evidence["trigger"]["outputs"])
        self.assertNotIn("command.node", submitted.origin_evidence["trigger"]["outputs"])
        self.assertEqual(f"rule:{rule_id}", submitted.actor)
        self.assertEqual(rule_id, UUID(submitted.origin_evidence["subject"]["id"]))
        self.assertEqual(3, submitted.origin_evidence["subject"]["version"])
        self.assertEqual("output.setpoint", submitted.origin_evidence["action_key"])
        self.assertEqual(
            {
                "inputs": [{"entity_instance_id": str(INTERLOCK_ID), "value": True}],
                "outputs": {"setpoint": 20.0},
            },
            submitted.origin_evidence["trigger"],
        )
        self.assertEqual(submitted.id, replayed.id)
        self.assertEqual(("rejected", "CONTROL_COOLDOWN_ACTIVE"), (cooling_down.status, cooling_down.code))
        self.assertEqual(1, len(self.dispatcher.requests))

    def test_strategy_attempt_uses_the_worker_owned_exact_idempotency_key(self) -> None:
        from app.services.automated_control_commands import (
            AutomatedControlCommandRequest,
            AutomatedControlCommands,
        )

        strategy_id = UUID("70000000-0000-0000-0000-000000000011")
        attempt_key = "e" * 64
        self._observe(INTERLOCK_ID, True)
        self._observe(READBACK_ID, 20.0)
        request = AutomatedControlCommandRequest(
            source_type="strategy",
            subject_id=strategy_id,
            subject_version=4,
            action_key="power-target",
            entity_instance_id=TARGET_ID,
            value=20.0,
            trigger_evidence={"frame_sequence": 42},
            attempt_idempotency_key=attempt_key,
            source_fresh_until=self.clock.now() + timedelta(seconds=10),
        )

        commands = AutomatedControlCommands(self.runtime)
        submitted = commands.submit(request)
        replayed = commands.submit(request)

        self.assertEqual("strategy", submitted.source_type)
        self.assertEqual(f"strategy:{strategy_id}", submitted.actor)
        self.assertEqual(attempt_key, submitted.idempotency_key)
        self.assertEqual(submitted.id, replayed.id)
        self.assertEqual(1, len(self.dispatcher.requests))


if __name__ == "__main__":
    unittest.main()
