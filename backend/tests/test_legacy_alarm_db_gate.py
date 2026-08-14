"""Ticket 14 production gate: web DB role cannot mutate legacy alarm history."""
from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import patch


class LegacyAlarmDatabaseGateTest(unittest.TestCase):
    def _connection(self, row: tuple[bool, bool, bool, bool] | None):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, _query):
                return None

            def fetchone(self):
                return row

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return Cursor()

        @contextmanager
        def connection():
            yield Connection()

        return connection

    def test_owner_or_writer_is_rejected(self) -> None:
        from app.services.telemetry_store import verify_legacy_alarm_history_gate

        for row in (
            (True, True, False, False),
            (False, True, True, False),
            (False, False, False, False),
            (False, True, False, True),
        ):
            with self.subTest(row=row), patch(
                "app.services.telemetry_store.get_connection",
                self._connection(row),
            ):
                with self.assertRaisesRegex(RuntimeError, "non-owner without public schema CREATE"):
                    verify_legacy_alarm_history_gate()

    def test_read_only_non_owner_is_accepted(self) -> None:
        from app.services.telemetry_store import verify_legacy_alarm_history_gate

        with patch(
            "app.services.telemetry_store.get_connection",
            self._connection((False, True, False, False)),
        ):
            verify_legacy_alarm_history_gate()


if __name__ == "__main__":
    unittest.main()
