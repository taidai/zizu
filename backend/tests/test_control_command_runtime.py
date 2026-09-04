from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import unittest
from uuid import UUID


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

    def test_fresh_readback_mismatch_is_terminal(self) -> None:
        self._observe(INTERLOCK_ID, True)
        self._observe(READBACK_ID, 10.0)

        command = self.runtime.submit(self._request())

        self.assertEqual(("mismatch", "CONTROL_READBACK_MISMATCH"), (command.status, command.code))
        self.assertEqual("mismatch", self.runtime.reconcile(command.id).status)

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

    def test_dispatch_failure_is_not_reported_as_success(self) -> None:
        self._observe(INTERLOCK_ID, True)
        self.runtime._dispatcher.failure = RuntimeError("gateway rejected")

        command = self.runtime.submit(self._request())

        self.assertEqual(("failed", "CONTROL_DISPATCH_FAILED"), (command.status, command.code))

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
