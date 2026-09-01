from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("DB_PASSWORD", "test-postgres-secret")
os.environ.setdefault("NEURON_PASSWORD", "test-neuron-secret")
os.environ.setdefault("NANOMQ_API_PASSWORD", "test-nanomq-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value-that-is-long-enough")

from app.api.tags import get_tag_history


class _Cursor:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, _params) -> None:
        self.queries.append(query)

    def fetchone(self):
        return ("FaultBit", "故障位")

    def fetchall(self):
        now = datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)
        return [
            (now, None, 0, None, None, 192, None),
            (now, None, None, False, None, 192, None),
            (now, None, None, None, "RUNNING", 192, None),
        ]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


class TagHistoryApiTest(unittest.TestCase):
    def test_long_range_downsamples_real_typed_samples_without_averaging(self) -> None:
        cursor = _Cursor()

        with patch(
            "app.services.telemetry_store.get_connection",
            return_value=_Connection(cursor),
        ):
            result = asyncio.run(get_tag_history(uuid4(), range="24h"))

        history_sql = cursor.queries[-1]
        self.assertIn("DISTINCT ON", history_sql.upper())
        self.assertNotIn("AVG(", history_sql.upper())
        self.assertEqual([0, False, "RUNNING"], [item["raw_value"] for item in result["points"]])
        self.assertIs(type(result["points"][0]["raw_value"]), int)
        self.assertIs(type(result["points"][1]["raw_value"]), bool)


if __name__ == "__main__":
    unittest.main()
