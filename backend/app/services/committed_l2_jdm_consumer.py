"""Thin async adapter for the committed-L2 JDM runtime."""
from __future__ import annotations

import asyncio

from app.services.data_trunk_outbox import FrameOutboxEvent
from app.services.jdm_runtime import JdmRuntime


class CommittedL2JdmConsumer:
    def __init__(self, runtime: JdmRuntime) -> None:
        self._runtime = runtime

    async def publish(self, event: FrameOutboxEvent) -> None:
        await asyncio.to_thread(self._runtime.submit_frame, event)


def build_postgres_committed_l2_jdm_consumer() -> CommittedL2JdmConsumer:
    from app.services.jdm_postgres import PostgresJdmRepository

    return CommittedL2JdmConsumer(JdmRuntime(PostgresJdmRepository()))


__all__ = [
    "CommittedL2JdmConsumer",
    "build_postgres_committed_l2_jdm_consumer",
]
