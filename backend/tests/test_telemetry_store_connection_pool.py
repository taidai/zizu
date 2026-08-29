from __future__ import annotations

import os
import threading
import unittest

from psycopg2.pool import PoolError

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")

from app.services import telemetry_store


class _OneConnectionPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._checked_out = False
        self.connection = object()

    def getconn(self):
        with self._lock:
            if self._checked_out:
                raise PoolError("connection pool exhausted")
            self._checked_out = True
            return self.connection

    def putconn(self, connection) -> None:
        if connection is not self.connection:
            raise AssertionError("unexpected connection")
        with self._lock:
            self._checked_out = False


class TelemetryStoreConnectionPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_pool = telemetry_store._pool
        self.original_slots = getattr(telemetry_store, "_pool_slots", None)
        self.pool = _OneConnectionPool()
        telemetry_store._pool = self.pool
        telemetry_store._pool_slots = threading.BoundedSemaphore(1)

    def tearDown(self) -> None:
        telemetry_store._pool = self.original_pool
        telemetry_store._pool_slots = self.original_slots

    def test_short_pool_saturation_waits_for_returned_connection(self) -> None:
        first_acquired = threading.Event()
        release_first = threading.Event()
        second_acquired = threading.Event()
        second_done = threading.Event()
        errors: list[BaseException] = []

        def first_borrower() -> None:
            with telemetry_store.get_connection():
                first_acquired.set()
                release_first.wait(timeout=2)

        def second_borrower() -> None:
            try:
                with telemetry_store.get_connection():
                    second_acquired.set()
            except BaseException as exc:  # pragma: no cover - assertion reports it
                errors.append(exc)
            finally:
                second_done.set()

        first = threading.Thread(target=first_borrower)
        second = threading.Thread(target=second_borrower)
        first.start()
        self.assertTrue(first_acquired.wait(timeout=1))
        second.start()

        waited_for_connection = not second_done.wait(timeout=0.1)
        release_first.set()
        first.join(timeout=1)
        second.join(timeout=1)

        self.assertTrue(
            waited_for_connection,
            "a short burst must wait instead of immediately exhausting the pool",
        )
        self.assertEqual([], errors)
        self.assertTrue(second_acquired.is_set())


if __name__ == "__main__":
    unittest.main()
