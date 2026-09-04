"""Thin async adapter from committed L2 frame events to StrategyRuntime."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.services.data_trunk_outbox import FrameOutboxEvent
from app.services.dispatch_strategies import StrategyRuntime, StrategyTrigger


class CommittedL2JdmConsumer:
    def __init__(self, runtime: StrategyRuntime) -> None:
        self._runtime = runtime

    async def publish(self, event: FrameOutboxEvent) -> None:
        trigger = StrategyTrigger(
            kind="DATA_CHANGE",
            trigger_key=f"frame:{event.frame_id}:{event.frame_sequence}",
            evaluated_at=event.frame_time or datetime.now(UTC),
            frame_sequence=event.frame_sequence,
        )
        changed_ids = tuple(
            dict.fromkeys(item.entity_instance_id for item in event.l2_changes)
        )
        await asyncio.to_thread(
            self._runtime.evaluate_data_change,
            changed_ids,
            trigger,
        )


def build_postgres_committed_l2_jdm_consumer() -> CommittedL2JdmConsumer:
    from app.services.dispatch_strategy_postgres import PostgresStrategyRepository

    repository = PostgresStrategyRepository()
    return CommittedL2JdmConsumer(StrategyRuntime(repository))


__all__ = [
    "CommittedL2JdmConsumer",
    "build_postgres_committed_l2_jdm_consumer",
]
