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
from app.services.neuron_point_processing_catalog import ScannedPoint, ScannedPointCatalog
from app.services.neuron_tag_import import plan_neuron_tag_import
from tests.api_test_client import AuthenticatedApiClient


NODE_ID = UUID("00000000-0000-0000-0000-000000000101")


class _Catalog:
    def scan_selected(self, node_name: str, groups: tuple[str, ...]) -> ScannedPointCatalog:
        points = tuple(
            ScannedPoint(
                group=group,
                group_interval_ms=1000,
                name=f"{group}-point",
                address=f"1!{index}",
                wire_data_type="INT16",
                value_data_type="INT",
                decimal=0.0,
                read_only=True,
            )
            for index, group in enumerate(sorted(groups), start=1)
        )
        return ScannedPointCatalog(node_name, 1000, "c" * 64, points, ())


class _Repository:
    def __init__(self) -> None:
        self.applied = 0

    def preview(self, *, node_id, neuron_node, selected_groups, points):
        return plan_neuron_tag_import(
            node_id=node_id,
            neuron_node=neuron_node,
            selected_groups=selected_groups,
            points=points,
            existing=(),
            base_configuration_revision=7,
        )

    def apply(self, preview, *, actor: str):
        self.applied += 1
        return {
            "status": "applied",
            "configuration_revision": 8,
            "counts": preview.counts,
        }


class _Gate:
    def begin_configuration_publish(self, revision: int) -> None:
        self.revision = revision

    def cancel_configuration_publish(self) -> None:
        self.cancelled = True

    def reconcile_configuration_runtime(self) -> None:
        self.reconciled = True


class _Trunk:
    def __init__(self) -> None:
        self.configuration_gate = _Gate()


class _Runtime:
    def __init__(self) -> None:
        self.data_trunk = _Trunk()
        self.reloaded = 0

    async def reload_rules_now(self) -> None:
        self.reloaded += 1


class NeuronTagImportPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = _Repository()
        self.runtime = _Runtime()
        app = FastAPI()
        app.include_router(tags.router, prefix="/api/v1")
        app.dependency_overrides[tags.get_neuron_tag_imports] = lambda: self.repository
        app.dependency_overrides[tags.get_neuron_import_catalog] = _Catalog
        app.dependency_overrides[tags.get_neuron_import_runtime] = lambda: self.runtime
        self.app = app

    async def test_preview_and_digest_confirmed_apply_use_multiple_groups(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            preview_response = await client.post(
                "/api/v1/tags/import-neuron/preview",
                json={
                    "node_id": str(NODE_ID),
                    "neuron_node": "EN9-PCS",
                    "neuron_groups": ["status", "data"],
                },
            )
            self.assertEqual(200, preview_response.status_code, preview_response.text)
            preview = preview_response.json()
            self.assertEqual(["data", "status"], preview["selected_groups"])
            self.assertEqual({"create": 2}, preview["counts"])

            apply_response = await client.post(
                "/api/v1/tags/import-neuron",
                json={
                    "node_id": str(NODE_ID),
                    "neuron_node": "EN9-PCS",
                    "neuron_groups": ["status", "data"],
                    "preview_digest": preview["preview_digest"],
                },
            )

        self.assertEqual(200, apply_response.status_code, apply_response.text)
        self.assertEqual(8, apply_response.json()["configuration_revision"])
        self.assertEqual(1, self.repository.applied)
        self.assertEqual(1, self.runtime.reloaded)

    async def test_old_single_group_wire_shape_is_rejected(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.post(
                "/api/v1/tags/import-neuron",
                json={
                    "node_id": str(NODE_ID),
                    "neuron_node": "EN9-PCS",
                    "neuron_group": "data",
                    "preview_digest": "a" * 64,
                },
            )

        self.assertEqual(422, response.status_code)


if __name__ == "__main__":
    unittest.main()
