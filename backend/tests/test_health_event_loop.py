from __future__ import annotations

import asyncio
import os
from threading import Event
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DB_PASSWORD", "test-postgres-secret")
os.environ.setdefault("NEURON_PASSWORD", "test-neuron-secret")
os.environ.setdefault("NANOMQ_API_PASSWORD", "test-nanomq-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value-that-is-long-enough")

from app.api import health


class HealthEventLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_database_health_wait_does_not_pause_the_sampling_event_loop(self):
        tick = Event()
        progressed_while_waiting = []

        def database_check():
            # The database boundary may wait for a connection or a response.
            # Its wait must allow the existing event loop to process MQTT/ticks.
            progressed_while_waiting.append(tick.wait(timeout=0.5))
            return True

        asyncio.get_running_loop().call_soon(tick.set)
        with (
            patch.object(health, "_pipeline", None),
            patch.object(health, "_check_tsdb", database_check),
            patch.object(health, "_check_neuron", AsyncMock(return_value="connected")),
            patch.object(health, "_release_lock_summary", return_value={}),
        ):
            result = await health.health_check()

        self.assertEqual([True], progressed_while_waiting)
        self.assertEqual("connected", result["components"]["timescaledb"]["status"])


if __name__ == "__main__":
    unittest.main()
