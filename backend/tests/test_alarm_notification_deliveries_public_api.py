from __future__ import annotations

import os
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

from fastapi import FastAPI

from tests.api_test_client import AuthenticatedApiClient


DELIVERY_ID = UUID("00000000-0000-0000-0000-000000000301")
EVENT_ID = UUID("00000000-0000-0000-0000-000000000302")


class _FakeHttpNotifications:
    def __init__(self) -> None:
        self.item = {
            "id": str(DELIVERY_ID),
            "event_id": str(EVENT_ID),
            "event_type": "ALARM_ACTIVATED",
            "alarm_name": "PCS 故障",
            "severity": "MAJOR",
            "node_name": "1# PCS",
            "entity_name": "故障状态",
            "configuration_name": "值班群",
            "configuration_exists": True,
            "target_display": "https://receiver.invalid/***",
            "status": "failed",
            "attempt_count": 4,
            "last_http_status": 500,
            "last_error_code": "HTTP_NOTIFICATION_DELIVERY_REJECTED",
            "last_error_detail": "Remote endpoint rejected the request",
            "last_response_excerpt": "failure",
            "created_at": "2026-09-02T10:00:00+00:00",
            "delivered_at": None,
            "cancelled_at": None,
            "attempts": [],
        }
        self.last_retry = None
        self.last_delete = None

    def list_deliveries(self, *, page: int, page_size: int):
        return {
            "items": [self.item],
            "total": 1,
            "page": page,
            "page_size": page_size,
            "total_pages": 1,
        }

    def retry(self, notification_id: UUID, actor: str, idempotency_key: str):
        self.last_retry = (notification_id, actor, idempotency_key)
        return {**self.item, "status": "pending"}

    def delete_deliveries(self, notification_ids: tuple[UUID, ...], actor: str):
        self.last_delete = (notification_ids, actor)
        return len(notification_ids)


class AlarmNotificationDeliveriesPublicApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from app.api.alarm_http_notifications import get_alarm_http_notifications
        from app.api.alarm_notification_deliveries import router

        self.service = _FakeHttpNotifications()
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.dependency_overrides[get_alarm_http_notifications] = (
            lambda: self.service
        )

    async def test_operator_can_list_redacted_delivery_history(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.get(
                "/api/v1/alarms/notification-deliveries?page=1&page_size=20"
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("failed", response.json()["items"][0]["status"])
        self.assertNotIn("secret-token", response.text)

    async def test_engineer_can_retry_failed_delivery_idempotently(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.post(
                f"/api/v1/alarms/notification-deliveries/{DELIVERY_ID}/retry",
                headers={"Idempotency-Key": "retry-once"},
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("pending", response.json()["status"])
        self.assertEqual(DELIVERY_ID, self.service.last_retry[0])
        self.assertEqual(
            "user:00000000-0000-0000-0000-000000000002",
            self.service.last_retry[1],
        )
        self.assertEqual("retry-once", self.service.last_retry[2])

    async def test_operator_cannot_retry_delivery(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            headers = {
                "Authorization": await client._bearer("operator"),
                "Idempotency-Key": "operator-denied",
            }
            response = await client._client.post(
                f"/api/v1/alarms/notification-deliveries/{DELIVERY_ID}/retry",
                headers=headers,
            )

        self.assertEqual(403, response.status_code, response.text)
        self.assertIsNone(self.service.last_retry)

    async def test_engineer_can_delete_one_terminal_delivery(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client._client.delete(
                f"/api/v1/alarms/notification-deliveries/{DELIVERY_ID}",
                headers={"Authorization": await client._bearer("engineer")},
            )

        self.assertEqual(204, response.status_code, response.text)
        self.assertEqual((DELIVERY_ID,), self.service.last_delete[0])
        self.assertEqual(
            "user:00000000-0000-0000-0000-000000000002",
            self.service.last_delete[1],
        )

    async def test_engineer_can_delete_selected_deliveries_in_one_request(self) -> None:
        second_id = UUID("00000000-0000-0000-0000-000000000303")
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.post(
                "/api/v1/alarms/notification-deliveries/deletions",
                json={"delivery_ids": [str(DELIVERY_ID), str(second_id)]},
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"deleted": 2}, response.json())
        self.assertEqual((DELIVERY_ID, second_id), self.service.last_delete[0])

    async def test_batch_delete_rejects_more_than_two_hundred_ids(self) -> None:
        ids = [str(UUID(int=value)) for value in range(1, 202)]
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.post(
                "/api/v1/alarms/notification-deliveries/deletions",
                json={"delivery_ids": ids},
            )

        self.assertEqual(422, response.status_code, response.text)
        self.assertIsNone(self.service.last_delete)

    async def test_operator_cannot_delete_delivery_history(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            headers = {"Authorization": await client._bearer("operator")}
            response = await client._client.delete(
                f"/api/v1/alarms/notification-deliveries/{DELIVERY_ID}",
                headers=headers,
            )

        self.assertEqual(403, response.status_code, response.text)
        self.assertIsNone(self.service.last_delete)


if __name__ == "__main__":
    unittest.main()
