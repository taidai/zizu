from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4

from app.services.dispatch_strategies import StrategyTrigger
from app.services.dispatch_strategy_workers import (
    ControlIntent,
    ControlIntentDispatcher,
    FixedMinuteTickWorker,
    next_minute_boundary,
)


NOW = datetime(2026, 9, 4, 2, 30, tzinfo=UTC)
STRATEGY_ID = UUID("73000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("73000000-0000-0000-0000-000000000002")
ENTITY_ID = UUID("73000000-0000-0000-0000-000000000003")


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, StrategyTrigger]] = []

    def evaluate(self, strategy_id, trigger):
        self.calls.append((strategy_id, trigger))
        return SimpleNamespace(status="EVALUATED")


class _TickRepository:
    def fixed_tick_strategy_ids(self):
        return (STRATEGY_ID,)


class _FlakyRuntime:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.calls = 0

    def evaluate(self, strategy_id, trigger):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary tick failure")
        self.stop.set()
        return SimpleNamespace(status="EVALUATED")


class _FlakyIntentRepository:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.calls = 0

    def claim_next(self, now):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary dispatcher failure")
        self.stop.set()
        return None


@dataclass
class _IntentState:
    item: ControlIntent


class _IntentRepository:
    def __init__(self, intents: list[ControlIntent]) -> None:
        self.rows = [_IntentState(item) for item in intents]
        self.strategy_health = "READY"
        self.failures = []

    def claim_next(self, now):
        for row in self.rows:
            item = row.item
            if item.status == "PENDING" and item.next_attempt_at <= now:
                earlier = [
                    candidate.item
                    for candidate in self.rows
                    if candidate.item.evaluation_key == item.evaluation_key
                    and candidate.item.ordinal < item.ordinal
                    and candidate.item.status != "CONFIRMED"
                ]
                if earlier:
                    continue
                row.item = replace(
                    item,
                    status="IN_FLIGHT",
                    attempt_count=item.attempt_count + 1,
                )
                return row.item
            if item.status == "IN_FLIGHT":
                return item
        return None

    def attach_command(self, intent_id, attempt_number, command_id):
        row = self._row(intent_id)
        row.item = replace(row.item, control_command_id=command_id)

    def mark_confirmed(self, intent_id, command_id, now):
        row = self._row(intent_id)
        row.item = replace(row.item, status="CONFIRMED", control_command_id=command_id)

    def schedule_retry(self, intent_id, command_id, code, next_attempt_at):
        row = self._row(intent_id)
        row.item = replace(
            row.item,
            status="PENDING",
            control_command_id=None,
            next_attempt_at=next_attempt_at,
        )

    def mark_failed(self, intent, command_id, code, now):
        row = self._row(intent.id)
        row.item = replace(row.item, status="FAILED", control_command_id=command_id)
        self.strategy_health = "FAILED"
        self.failures.append((intent.id, code))
        for candidate in self.rows:
            if candidate.item.status == "PENDING":
                candidate.item = replace(candidate.item, status="CANCELLED")

    def recoverable_count(self):
        return sum(item.item.status == "IN_FLIGHT" for item in self.rows)

    def _row(self, intent_id):
        return next(row for row in self.rows if row.item.id == intent_id)


class _Control:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = list(statuses)
        self.requests = []
        self.commands = {}

    def submit(self, request):
        self.requests.append(request)
        status = self.statuses.pop(0)
        command = self._command(status)
        self.commands[command.id] = command
        return command

    def reconcile(self, command_id):
        return self.commands[command_id]

    def _command(self, status):
        return SimpleNamespace(
            id=uuid4(),
            status=status,
            code=f"CONTROL_{status.upper()}",
            timeout_at=NOW + timedelta(seconds=10),
            policy_snapshot={"cooldown_seconds": 1},
        )


def _intent(ordinal: int = 0, *, evaluation_key: str = "tick:1") -> ControlIntent:
    return ControlIntent(
        id=uuid4(),
        strategy_id=STRATEGY_ID,
        revision_id=REVISION_ID,
        revision_number=4,
        evaluation_key=evaluation_key,
        action_id=f"power-{ordinal}",
        ordinal=ordinal,
        entity_instance_id=ENTITY_ID,
        expected_value=156.7,
        status="PENDING",
        attempt_count=0,
        control_command_id=None,
        snapshot_evidence={"frame_sequence": 42, "configuration_revision": 7},
        next_attempt_at=NOW,
    )


class DispatchStrategyWorkersTest(unittest.TestCase):
    def test_minute_tick_uses_same_runtime_and_never_backfills_old_minutes(self) -> None:
        runtime = _Runtime()
        worker = FixedMinuteTickWorker(_TickRepository(), runtime)
        tick = datetime(2026, 9, 4, 2, 31, tzinfo=UTC)

        count = worker.run_once(tick)

        self.assertEqual(1, count)
        strategy_id, trigger = runtime.calls[0]
        self.assertEqual(STRATEGY_ID, strategy_id)
        self.assertEqual("FIXED_TICK", trigger.kind)
        self.assertEqual("tick:2026-09-04T02:31:00+00:00", trigger.trigger_key)
        self.assertEqual(0, trigger.frame_sequence)
        self.assertEqual(
            datetime(2026, 9, 4, 2, 31, tzinfo=UTC),
            next_minute_boundary(datetime(2026, 9, 4, 2, 30, 59, tzinfo=UTC)),
        )

    def test_later_ordinal_waits_for_confirmed_readback(self) -> None:
        first = _intent(0)
        second = _intent(1)
        repository = _IntentRepository([first, second])
        control = _Control(["dispatched"])
        dispatcher = ControlIntentDispatcher(repository, control)

        pending = dispatcher.run_once(NOW)

        self.assertEqual(first.id, pending.intent_id)
        self.assertEqual("IN_FLIGHT", repository.rows[0].item.status)
        self.assertEqual("PENDING", repository.rows[1].item.status)
        self.assertEqual(1, len(control.requests))

    def test_existing_inflight_command_is_reconciled_without_resubmit(self) -> None:
        intent = replace(
            _intent(),
            status="IN_FLIGHT",
            attempt_count=1,
            control_command_id=uuid4(),
        )
        repository = _IntentRepository([intent])
        control = _Control([])
        command = control._command("readback_confirmed")
        control.commands[intent.control_command_id] = replace_namespace_id(
            command, intent.control_command_id
        )

        result = ControlIntentDispatcher(repository, control).run_once(NOW)

        self.assertEqual("CONFIRMED", result.status)
        self.assertEqual("CONFIRMED", repository.rows[0].item.status)
        self.assertEqual([], control.requests)

    def test_attempt_key_is_exact_and_restart_reuses_the_inflight_attempt(self) -> None:
        intent = _intent()
        repository = _IntentRepository([intent])
        control = _Control(["dispatched"])
        dispatcher = ControlIntentDispatcher(repository, control)

        dispatcher.run_once(NOW)
        key = sha256(f"{intent.id}:1".encode("utf-8")).hexdigest()
        dispatcher.recover(NOW)

        self.assertEqual(key, control.requests[0].attempt_idempotency_key)
        self.assertEqual(1, len(control.requests))

    def test_third_failed_attempt_latches_strategy_and_cancels_sequence(self) -> None:
        first = _intent(0)
        second = _intent(1)
        repository = _IntentRepository([first, second])
        control = _Control(["failed", "failed", "failed"])
        dispatcher = ControlIntentDispatcher(repository, control)

        for offset in (0, 2, 4):
            dispatcher.run_once(NOW + timedelta(seconds=offset))

        self.assertEqual("FAILED", repository.rows[0].item.status)
        self.assertEqual("CANCELLED", repository.rows[1].item.status)
        self.assertEqual("FAILED", repository.strategy_health)
        self.assertEqual(3, len(control.requests))
        self.assertEqual(1, len(repository.failures))

    def test_confirmed_first_action_releases_next_ordinal(self) -> None:
        first = _intent(0)
        second = _intent(1)
        repository = _IntentRepository([first, second])
        control = _Control(["readback_confirmed", "readback_confirmed"])
        dispatcher = ControlIntentDispatcher(repository, control)

        one = dispatcher.run_once(NOW)
        two = dispatcher.run_once(NOW)

        self.assertEqual(("CONFIRMED", "CONFIRMED"), (one.status, two.status))
        self.assertEqual(2, len(control.requests))


class DispatchStrategyWorkerLoopsTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_tick_loop_survives_one_evaluation_failure(self) -> None:
        stop = asyncio.Event()
        runtime = _FlakyRuntime(stop)
        clock_values = iter(
            (
                NOW,
                NOW + timedelta(minutes=1),
                NOW + timedelta(minutes=1),
                NOW + timedelta(minutes=2),
            )
        )
        worker = FixedMinuteTickWorker(
            _TickRepository(),
            runtime,
            clock=lambda: next(clock_values),
        )

        await asyncio.wait_for(worker.run(stop), timeout=1)

        self.assertEqual(2, runtime.calls)

    async def test_intent_dispatcher_loop_survives_one_repository_failure(self) -> None:
        stop = asyncio.Event()
        repository = _FlakyIntentRepository(stop)
        dispatcher = ControlIntentDispatcher(repository, _Control([]), clock=lambda: NOW)

        await asyncio.wait_for(dispatcher.run(stop), timeout=1)

        self.assertEqual(2, repository.calls)


def replace_namespace_id(value, command_id):
    return SimpleNamespace(
        id=command_id,
        status=value.status,
        code=value.code,
        timeout_at=value.timeout_at,
        policy_snapshot=value.policy_snapshot,
    )


if __name__ == "__main__":
    unittest.main()
