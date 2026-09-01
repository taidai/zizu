from __future__ import annotations

import os
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

from fastapi import FastAPI

from app.api import tags
from tests.api_test_client import AuthenticatedApiClient


TAG_ID = UUID("00000000-0000-0000-0000-000000000201")


class _Repository:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def current_revision(self) -> int:
        return 7

    def update(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "updated": 1,
            "configuration_revision": 8,
            "items": [{"id": str(TAG_ID), "display_name": "PCS 有功功率", "enabled": False}],
        }

    def delete(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "deleted": 1,
            "configuration_revision": 8,
            "deleted_ids": [str(TAG_ID)],
        }


class _Gate:
    def begin_configuration_publish(self, revision: int) -> None:
        self.revision = revision

    def cancel_configuration_publish(self) -> None:
        self.cancelled = True

    def reconcile_configuration_runtime(self) -> None:
        self.reconciled = True


class _Runtime:
    def __init__(self) -> None:
        self.data_trunk = type("Trunk", (), {"configuration_gate": _Gate()})()
        self.reloaded = False

    async def reload_rules_now(self) -> None:
        self.reloaded = True


class RawPointMaintenancePublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = _Repository()
        self.runtime = _Runtime()
        app = FastAPI()
        app.include_router(tags.router, prefix="/api/v1")
        app.dependency_overrides[tags.get_raw_point_maintenance] = lambda: self.repository
        app.dependency_overrides[tags.get_raw_point_maintenance_runtime] = lambda: self.runtime
        self.app = app

    async def test_update_uses_the_configuration_gate_and_returns_the_new_revision(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client._request(
                "PUT",
                "/api/v1/tags/maintenance",
                json={
                    "tag_ids": [str(TAG_ID)],
                    "display_name": "  PCS 有功功率  ",
                    "enabled": False,
                },
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(8, response.json()["configuration_revision"])
        self.assertEqual((TAG_ID,), self.repository.calls[0]["tag_ids"])
        self.assertEqual(
            {"display_name": "PCS 有功功率", "enabled": False},
            self.repository.calls[0]["changes"],
        )
        self.assertEqual(7, self.runtime.data_trunk.configuration_gate.revision)
        self.assertTrue(self.runtime.data_trunk.configuration_gate.reconciled)
        self.assertTrue(self.runtime.reloaded)

    async def test_delete_uses_the_configuration_gate_and_returns_deleted_ids(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client._request(
                "DELETE",
                "/api/v1/tags/maintenance",
                json={"tag_ids": [str(TAG_ID)]},
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([str(TAG_ID)], response.json()["deleted_ids"])
        self.assertEqual((TAG_ID,), self.repository.calls[0]["tag_ids"])
        self.assertEqual(7, self.repository.calls[0]["base_revision"])
        self.assertEqual(7, self.runtime.data_trunk.configuration_gate.revision)
        self.assertTrue(self.runtime.data_trunk.configuration_gate.reconciled)
        self.assertTrue(self.runtime.reloaded)


if __name__ == "__main__":
    unittest.main()
