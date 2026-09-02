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


CONFIG_ID = UUID("00000000-0000-0000-0000-000000000201")


class _FakeHttpNotifications:
    def __init__(self) -> None:
        self.item = {
            "id": str(CONFIG_ID),
            "name": "值班群",
            "description": None,
            "method": "POST",
            "url_display": "https://receiver.invalid/***?token=%2A%2A%2A",
            "query_params": [],
            "headers": [
                {"key": "Authorization", "sensitive": True, "configured": True}
            ],
            "content_type": "application/json",
            "body_template": '{"type":{{event.type}}}',
            "timeout_seconds": 5,
            "current_digest": "a" * 64,
            "tested_digest": None,
            "tested_at": None,
            "last_test_status": None,
            "enabled": False,
        }
        self.deleted = False

    def list(self):
        return (self.item,)

    def create(self, draft, actor):
        self.last_actor = actor
        self.last_draft = draft
        return self.item

    def update(self, config_id, draft, actor):
        self.last_actor = actor
        self.last_draft = draft
        assert config_id == CONFIG_ID
        return self.item

    async def test(self, config_id, actor):
        assert config_id == CONFIG_ID
        return {**self.item, "last_test_status": {"delivered": True, "http_status": 204}}

    def enable(self, config_id, actor):
        assert config_id == CONFIG_ID
        return {**self.item, "enabled": True}

    def disable(self, config_id, actor):
        assert config_id == CONFIG_ID
        return self.item

    def delete(self, config_id, actor):
        assert config_id == CONFIG_ID
        self.deleted = True


class AlarmHttpNotificationPublicApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from app.api.alarm_http_notifications import (
            get_alarm_http_notifications,
            router,
        )

        self.service = _FakeHttpNotifications()
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.dependency_overrides[get_alarm_http_notifications] = (
            lambda: self.service
        )
        self.payload = {
            "name": "值班群",
            "description": None,
            "method": "POST",
            "url": "https://receiver.invalid/hook?token=hidden",
            "query_params": [],
            "headers": [
                {
                    "key": "Authorization",
                    "value": "Bearer hidden",
                    "sensitive": True,
                }
            ],
            "content_type": "application/json",
            "body_template": '{"type":{{event.type}}}',
            "timeout_seconds": 5,
        }

    async def test_admin_can_use_all_configuration_lifecycle_endpoints(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            bearer = await client._bearer("admin")
            headers = {"Authorization": bearer}
            created = await client._client.post(
                "/api/v1/admin/alarm-http-notifications",
                headers=headers,
                json=self.payload,
            )
            listed = await client._client.get(
                "/api/v1/admin/alarm-http-notifications",
                headers=headers,
            )
            updated = await client._client.put(
                f"/api/v1/admin/alarm-http-notifications/{CONFIG_ID}",
                headers=headers,
                json={**self.payload, "description": "主通道"},
            )
            tested = await client._client.post(
                f"/api/v1/admin/alarm-http-notifications/{CONFIG_ID}/test",
                headers=headers,
            )
            enabled = await client._client.post(
                f"/api/v1/admin/alarm-http-notifications/{CONFIG_ID}/enable",
                headers=headers,
            )
            disabled = await client._client.post(
                f"/api/v1/admin/alarm-http-notifications/{CONFIG_ID}/disable",
                headers=headers,
            )
            deleted = await client._client.delete(
                f"/api/v1/admin/alarm-http-notifications/{CONFIG_ID}",
                headers=headers,
            )

        self.assertEqual(201, created.status_code, created.text)
        self.assertEqual(200, listed.status_code, listed.text)
        self.assertEqual(200, updated.status_code, updated.text)
        self.assertEqual(204, tested.json()["last_test_status"]["http_status"])
        self.assertTrue(enabled.json()["enabled"])
        self.assertFalse(disabled.json()["enabled"])
        self.assertEqual(204, deleted.status_code, deleted.text)
        self.assertTrue(self.service.deleted)
        for response in (created, listed, updated, tested, enabled, disabled):
            self.assertNotIn("hidden", response.text)

    async def test_non_admin_cannot_read_or_change_http_notification_configs(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            bearer = await client._bearer("engineer")
            headers = {"Authorization": bearer}
            read = await client._client.get(
                "/api/v1/admin/alarm-http-notifications",
                headers=headers,
            )
            write = await client._client.post(
                "/api/v1/admin/alarm-http-notifications",
                headers=headers,
                json=self.payload,
            )

        self.assertEqual(403, read.status_code, read.text)
        self.assertEqual(403, write.status_code, write.text)

    async def test_stable_service_error_is_preserved_in_public_response(self) -> None:
        from app.services.alarm_http_notifications import HttpNotificationError

        def fail(config_id, actor):
            raise HttpNotificationError(
                "HTTP_NOTIFICATION_NOT_TESTED",
                "Send a successful test before enabling this configuration",
            )

        self.service.enable = fail
        async with AuthenticatedApiClient(self.app) as client:
            bearer = await client._bearer("admin")
            response = await client._client.post(
                f"/api/v1/admin/alarm-http-notifications/{CONFIG_ID}/enable",
                headers={"Authorization": bearer},
            )

        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(
            "HTTP_NOTIFICATION_NOT_TESTED",
            response.json()["detail"]["code"],
        )


if __name__ == "__main__":
    unittest.main()
