"""In-process minute and committed control-intent workers for dispatch strategies."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from loguru import logger

from app.services.automated_control_commands import (
    AutomatedControlCommandRequest,
    AutomatedControlCommands,
)
from app.services.dispatch_strategies import StrategyRuntime, StrategyTrigger


@dataclass(frozen=True)
class ControlIntent:
    id: UUID
    strategy_id: UUID
    revision_id: UUID
    revision_number: int
    evaluation_key: str
    action_id: str
    ordinal: int
    entity_instance_id: UUID
    expected_value: object
    status: str
    attempt_count: int
    control_command_id: UUID | None
    snapshot_evidence: dict[str, object]
    next_attempt_at: datetime


@dataclass(frozen=True)
class DispatchResult:
    intent_id: UUID
    status: str
    attempt_count: int
    control_command_id: UUID | None
    code: str | None


class StrategyWorkerRepository(Protocol):
    def fixed_tick_strategy_ids(self) -> tuple[UUID, ...]: ...

    def claim_next(self, now: datetime) -> ControlIntent | None: ...

    def attach_command(
        self, intent_id: UUID, attempt_number: int, command_id: UUID
    ) -> None: ...

    def mark_confirmed(
        self, intent_id: UUID, command_id: UUID, now: datetime
    ) -> None: ...

    def schedule_retry(
        self,
        intent_id: UUID,
        command_id: UUID,
        code: str,
        next_attempt_at: datetime,
    ) -> None: ...

    def mark_failed(
        self,
        intent: ControlIntent,
        command_id: UUID,
        code: str,
        now: datetime,
    ) -> None: ...

    def recoverable_count(self) -> int: ...


def next_minute_boundary(value: datetime) -> datetime:
    """Return the next wall-clock minute; missed minutes are intentionally ignored."""
    return value.replace(second=0, microsecond=0) + timedelta(minutes=1)


class FixedMinuteTickWorker:
    def __init__(
        self,
        repository: StrategyWorkerRepository,
        runtime: StrategyRuntime,
        *,
        clock=None,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_once(self, tick_at: datetime) -> int:
        tick = tick_at.replace(second=0, microsecond=0)
        trigger = StrategyTrigger(
            "FIXED_TICK",
            f"tick:{tick.isoformat()}",
            tick,
            0,
        )
        strategy_ids = self._repository.fixed_tick_strategy_ids()
        for strategy_id in strategy_ids:
            self._runtime.evaluate(strategy_id, trigger)
        return len(strategy_ids)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            boundary = next_minute_boundary(self._clock())
            delay = max(0.0, (boundary - self._clock()).total_seconds())
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                continue
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(self.run_once, boundary)
            except Exception as error:
                logger.error(
                    "[DispatchStrategy] fixed minute tick failed: {}",
                    type(error).__name__,
                )


class ControlIntentDispatcher:
    def __init__(
        self,
        repository: StrategyWorkerRepository,
        control: AutomatedControlCommands,
        *,
        clock=None,
    ) -> None:
        self._repository = repository
        self._control = control
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_once(self, now: datetime | None = None) -> DispatchResult | None:
        attempted_at = now or self._clock()
        intent = self._repository.claim_next(attempted_at)
        if intent is None:
            return None
        if intent.control_command_id is None:
            request = AutomatedControlCommandRequest(
                source_type="strategy",
                subject_id=intent.strategy_id,
                subject_version=intent.revision_number,
                action_key=intent.action_id,
                entity_instance_id=intent.entity_instance_id,
                value=intent.expected_value,
                trigger_evidence={
                    **dict(intent.snapshot_evidence),
                    "intent_id": str(intent.id),
                    "attempt_number": intent.attempt_count,
                },
                attempt_idempotency_key=_attempt_key(intent.id, intent.attempt_count),
            )
            command = self._control.submit(request)
            self._repository.attach_command(
                intent.id, intent.attempt_count, command.id
            )
        else:
            command = self._control.reconcile(intent.control_command_id)
        if command.status == "readback_confirmed":
            self._repository.mark_confirmed(intent.id, command.id, attempted_at)
            return DispatchResult(
                intent.id, "CONFIRMED", intent.attempt_count, command.id, command.code
            )
        if command.status in {"rejected", "timeout", "failed", "mismatch"}:
            if intent.attempt_count >= 3:
                self._repository.mark_failed(
                    intent, command.id, command.code, attempted_at
                )
                return DispatchResult(
                    intent.id, "FAILED", intent.attempt_count, command.id, command.code
                )
            cooldown = max(
                0,
                int(command.policy_snapshot.get("cooldown_seconds", 0)),
            )
            self._repository.schedule_retry(
                intent.id,
                command.id,
                command.code,
                attempted_at + timedelta(seconds=cooldown),
            )
            return DispatchResult(
                intent.id, "PENDING", intent.attempt_count, command.id, command.code
            )
        return DispatchResult(
            intent.id, "IN_FLIGHT", intent.attempt_count, command.id, command.code
        )

    def recover(self, now: datetime | None = None) -> int:
        attempted_at = now or self._clock()
        count = self._repository.recoverable_count()
        for _ in range(count):
            self.run_once(attempted_at)
        return count

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await asyncio.to_thread(self.run_once, self._clock())
            except Exception as error:
                logger.error(
                    "[DispatchStrategy] intent dispatcher tick failed: {}",
                    type(error).__name__,
                )
                result = None
            if result is None or result.status == "IN_FLIGHT":
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass


def _attempt_key(intent_id: UUID, attempt_number: int) -> str:
    return sha256(f"{intent_id}:{attempt_number}".encode("utf-8")).hexdigest()


__all__ = [
    "ControlIntent",
    "ControlIntentDispatcher",
    "DispatchResult",
    "FixedMinuteTickWorker",
    "next_minute_boundary",
]
