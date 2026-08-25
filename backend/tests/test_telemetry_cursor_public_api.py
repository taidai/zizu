from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI

from app.api.telemetry import router as telemetry_router
from tests.api_test_client import AuthenticatedApiClient


class CursorTelemetryDatabase:
    def __init__(self) -> None:
        base = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)
        self.pages = (
            [
                self.row(base, "00000000-0000-0000-0000-000000000004", 4.0),
                self.row(base, "00000000-0000-0000-0000-000000000003", 3.0),
                self.row(base - timedelta(seconds=1), "00000000-0000-0000-0000-000000000002", 2.0),
            ],
            [
                self.row(base - timedelta(seconds=1), "00000000-0000-0000-0000-000000000002", 2.0),
                self.row(base - timedelta(seconds=2), "00000000-0000-0000-0000-000000000001", 1.0),
            ],
        )
        self.page_index = 0
        self.queries: list[str] = []

    @staticmethod
    def row(ts: datetime, tag_id: str, value: float) -> tuple:
        return (
            ts,
            UUID(tag_id),
            f"tag-{tag_id[-1]}",
            None,
            "PCS-01",
            value,
            value,
            192,
        )

    def connection(self) -> "CursorTelemetryConnection":
        return CursorTelemetryConnection(self)


class CursorTelemetryConnection:
    def __init__(self, database: CursorTelemetryDatabase) -> None:
        self.database = database

    def __enter__(self) -> "CursorTelemetryConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> "CursorTelemetryCursor":
        return CursorTelemetryCursor(self.database)


class CursorTelemetryCursor:
    def __init__(self, database: CursorTelemetryDatabase) -> None:
        self.database = database
        self.description = [
            (name,)
            for name in (
                "ts",
                "tag_id",
                "tag_name",
                "display_name",
                "node_name",
                "raw_value",
                "eng_value",
                "quality",
            )
        ]
        self.rows: list[tuple] = []

    def __enter__(self) -> "CursorTelemetryCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _params: object = None) -> None:
        normalized = " ".join(query.split())
        self.database.queries.append(normalized)
        if "COUNT(*)" in normalized or " OFFSET " in f" {normalized} ":
            raise AssertionError("interactive telemetry must not scan counts or use offsets")
        self.rows = self.database.pages[self.database.page_index]
        self.database.page_index += 1

    def fetchall(self) -> list[tuple]:
        return self.rows


class TelemetryCursorPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_pages_without_exact_count_or_duplicate_points(self) -> None:
        database = CursorTelemetryDatabase()
        app = FastAPI()
        app.include_router(telemetry_router, prefix="/api/v1")

        with patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with AuthenticatedApiClient(app) as client:
                first = await client.get(
                    "/api/v1/telemetry?range=all&page_size=2"
                )
                self.assertEqual(200, first.status_code, first.text)
                self.assertEqual([4.0, 3.0], [item["eng_value"] for item in first.json()["points"]])
                self.assertTrue(first.json()["has_more"])
                self.assertIsNotNone(first.json()["next_cursor"])
                self.assertIsNone(first.json()["total"])

                wrong_scope = await client.get(
                    "/api/v1/telemetry",
                    params={
                        "range": "1h",
                        "page_size": 2,
                        "cursor": first.json()["next_cursor"],
                    },
                )
                self.assertEqual(422, wrong_scope.status_code, wrong_scope.text)
                self.assertEqual(
                    "TELEMETRY_CURSOR_INVALID",
                    wrong_scope.json()["detail"]["code"],
                )

                second = await client.get(
                    "/api/v1/telemetry",
                    params={
                        "range": "all",
                        "page_size": 2,
                        "cursor": first.json()["next_cursor"],
                    },
                )
                self.assertEqual(200, second.status_code, second.text)
                self.assertEqual([2.0, 1.0], [item["eng_value"] for item in second.json()["points"]])
                self.assertFalse(second.json()["has_more"])
                self.assertIsNone(second.json()["next_cursor"])
                self.assertIsNone(second.json()["total"])

        first_ids = {item["tag_id"] for item in first.json()["points"]}
        second_ids = {item["tag_id"] for item in second.json()["points"]}
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.assertEqual(2, len(database.queries))
        self.assertTrue(all("COUNT(*)" not in query for query in database.queries))
        self.assertTrue(all(" OFFSET " not in f" {query} " for query in database.queries))


if __name__ == "__main__":
    unittest.main()
