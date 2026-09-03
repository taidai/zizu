from __future__ import annotations

import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
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


class AlarmHttpNotificationOptionsApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from app.api.alarm_http_notifications import get_alarm_http_notifications, router
        from app.services.alarm_http_notifications import AlarmHttpNotifications, StoredHttpNotificationConfig

        config = StoredHttpNotificationConfig(
            id=CONFIG_ID, name="值班群", description="private description",
            method="POST", url_display="https://receiver.invalid/***",
            public_query_params=(), secret_query_param_names=("token",),
            public_headers=(), secret_header_names=("Authorization",),
            content_type="application/json", body_template='{"private":"do not expose"}',
            timeout_seconds=5, current_digest="a" * 64, tested_digest="a" * 64,
            tested_at=None, last_test_status={"delivered": True, "response_excerpt": "private"},
            enabled=False,
        )
        self.configs = [config]
        self.repository = SimpleNamespace(list_configs=lambda: self.configs)
        self.service = AlarmHttpNotifications(self.repository)
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.dependency_overrides[get_alarm_http_notifications] = lambda: self.service

    async def test_admin_and_engineer_can_read_safe_availability_without_enabling(self) -> None:
        self.configs += [
            replace(self.configs[0], id=UUID(int=202), name="可用", enabled=True),
            replace(self.configs[0], id=UUID(int=203), name="需重测", enabled=True, tested_digest="old"),
            replace(self.configs[0], id=UUID(int=204), name="未测试", tested_digest=None),
        ]
        async with AuthenticatedApiClient(self.app) as client:
            for role in ("admin", "engineer"):
                response = await client._client.get(
                    "/api/v1/alarm-http-notification-options",
                    headers={"Authorization": await client._bearer(role)},
                )
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([
                    {"id": str(CONFIG_ID), "name": "值班群", "status": "disabled"},
                    {"id": str(UUID(int=202)), "name": "可用", "status": "available"},
                    {"id": str(UUID(int=203)), "name": "需重测", "status": "needs_test"},
                    {"id": str(UUID(int=204)), "name": "未测试", "status": "needs_test"},
                ], response.json())
        self.assertFalse(self.configs[0].enabled)

    async def test_options_require_configuration_read_permission(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            for headers, expected in (({}, 401), ({"Authorization": await client._bearer("operator")}, 403)):
                response = await client._client.get("/api/v1/alarm-http-notification-options", headers=headers)
                self.assertEqual(expected, response.status_code, response.text)

    async def test_unavailable_storage_is_not_reported_as_an_empty_list(self) -> None:
        from app.services.alarm_http_notifications import HttpNotificationError

        def fail():
            raise HttpNotificationError("HTTP_NOTIFICATION_PERSISTENCE_UNAVAILABLE", "Unavailable")

        self.repository.list_configs = fail
        async with AuthenticatedApiClient(self.app) as client:
            response = await client._client.get(
                "/api/v1/alarm-http-notification-options",
                headers={"Authorization": await client._bearer("engineer")},
            )
        self.assertEqual(503, response.status_code, response.text)
        self.assertEqual("HTTP_NOTIFICATION_PERSISTENCE_UNAVAILABLE", response.json()["detail"]["code"])


if __name__ == "__main__":
    unittest.main()
