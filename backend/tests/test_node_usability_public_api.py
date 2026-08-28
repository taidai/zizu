from __future__ import annotations

import os
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

from fastapi import FastAPI

from app.api import nodes
from tests.api_test_client import AuthenticatedApiClient


NODE_ID = UUID("00000000-0000-0000-0000-000000000101")


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def current_revision(self) -> int:
        return 7

    def list_active(self):
        return [{"id": str(NODE_ID), "name": "PCS", "parent_id": None, "layer": 1}]

    def get_active(self, node_id):
        return self.list_active()[0] if str(node_id) == str(NODE_ID) else None

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"node": self.list_active()[0], "configuration_revision": 8}

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return {"node": self.list_active()[0], "configuration_revision": 8}

    def retire(self, **kwargs):
        self.calls.append(("retire", kwargs))
        return {"retired": str(NODE_ID), "retired_nodes": 1, "configuration_revision": 8}


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

    async def reload_rules_now(self) -> None:
        pass


class NodeUsabilityPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = _Repository()
        app = FastAPI()
        app.include_router(nodes.router, prefix="/api/v1")
        app.dependency_overrides[nodes.get_node_tree_repository] = lambda: self.repository
        app.dependency_overrides[nodes.get_node_tree_runtime] = _Runtime
        self.app = app

    async def test_server_computes_layer_and_explicit_null_moves_to_root(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            created = await client.post(
                "/api/v1/nodes",
                json={"name": "PCS", "node_type": "DEVICE", "parent_id": None},
            )
            moved = await client._request(
                "PUT",
                f"/api/v1/nodes/{NODE_ID}",
                json={"parent_id": None},
            )

        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual(200, moved.status_code, moved.text)
        self.assertNotIn("layer", self.repository.calls[0][1])
        self.assertEqual({"parent_id": None}, self.repository.calls[1][1]["changes"])

    async def test_delete_is_a_safe_runtime_retirement(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client._request(
                "DELETE",
                f"/api/v1/nodes/{NODE_ID}",
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, response.json()["retired_nodes"])
        self.assertEqual("retire", self.repository.calls[0][0])


if __name__ == "__main__":
    unittest.main()
