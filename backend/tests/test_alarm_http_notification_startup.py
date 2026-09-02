from __future__ import annotations

import asyncio
import os
import unittest

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")


class _Dispatcher:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.calls = 0

    async def run_once(self) -> int:
        self.calls += 1
        self.stop.set()
        return 1


class AlarmHttpNotificationStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_loop_runs_dispatcher_and_honours_shutdown(self) -> None:
        from app.main import run_alarm_http_notification_loop

        stop = asyncio.Event()
        dispatcher = _Dispatcher(stop)

        await run_alarm_http_notification_loop(dispatcher, stop)

        self.assertEqual(1, dispatcher.calls)
        self.assertTrue(stop.is_set())


if __name__ == "__main__":
    unittest.main()
