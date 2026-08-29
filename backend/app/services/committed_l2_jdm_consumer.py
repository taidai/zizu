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
