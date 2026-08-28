"""Authenticated public HTTP seam for point-processing planning and apply."""
from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

import httpx
from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.security import get_identity
from app.services.identity import (
    Identity,
    InMemoryIdentityRepository,
    UserIdentity,
    hash_password,
)
from app.services.point_processing import (
    ApplyPointProcessingPlan,
    InMemoryPointProcessingCatalog,
    InMemoryPointProcessingRepository,
    PreviewPointProcessing,
    PointProcessingService,
    PointProcessingSource,
)
from app.services.point_processing_templates import InMemoryPointProcessingTemplates

NODE_ID = UUID("84000000-0000-0000-0000-000000000001")
ENTITY_IDENTITY_INSTALLATION_ID = UUID("84000000-0000-0000-0000-000000000002")
SOLUTION_INSTALLATION_ID = UUID("84000000-0000-0000-0000-000000000003")
BRAND_A_REVISION_ID = UUID("84000000-0000-0000-0000-00000000000a")
BRAND_B_REVISION_ID = UUID("84000000-0000-0000-0000-00000000000b")
SITE_FORMULA_REVISION_ID = UUID("84000000-0000-0000-0000-00000000000c")
PCS_POWER_1 = UUID("85000000-0000-0000-0000-000000000101")
PCS_POWER_2 = UUID("85000000-0000-0000-0000-000000000102")


class PointProcessingPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(
            cls.password,
            salt=b"point-conv-auth",
        )

    def build_app(self) -> tuple[FastAPI, InMemoryIdentityRepository, InMemoryPointProcessingRepository]:
        from tests.test_point_processing import _assets, _site_formula_asset

        assets = _assets()
        repository = InMemoryPointProcessingRepository()
        service = PointProcessingService(
            repository,
            InMemoryPointProcessingCatalog(
                templates={
                    BRAND_A_REVISION_ID: assets["pcs.brand-a"],
                    BRAND_B_REVISION_ID: assets["pcs.brand-b"],
                    SITE_FORMULA_REVISION_ID: _site_formula_asset(),
                },
                sources=(
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000001"), "l0", NODE_ID, "ActivePowerRaw", "FLOAT", "W", True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000002"), "l0", NODE_ID, "RunningState", "STRING", None, True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000003"), "l0", NODE_ID, "FaultCodeText", "STRING", None, True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000011"), "l0", NODE_ID, "PActKw", "FLOAT", "kW", True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000012"), "l0", NODE_ID, "ModeCode", "STRING", None, True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000013"), "l0", NODE_ID, "AlarmList", "STRING", None, True),
                ),
                selector_members={
                    (NODE_ID, "PCS", "pcs.active_power"): (
                        PCS_POWER_2,
                        PCS_POWER_1,
                    ),
                },
            ),
        )
        initial_plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=BRAND_A_REVISION_ID,
                input_selections={},
                actor="user:seed-engineer",
            )
        )
        service.apply(
            ApplyPointProcessingPlan(
                initial_plan.id,
                initial_plan.digest,
                "initial-brand-a",
                "user:seed-engineer",
            )
        )
        identity_repository = InMemoryIdentityRepository(
            [
                UserIdentity(UUID("00000000-0000-0000-0000-000000000001"), "admin", self.password_hash, "admin", "active"),
                UserIdentity(UUID("00000000-0000-0000-0000-000000000002"), "engineer", self.password_hash, "engineer", "active"),
                UserIdentity(UUID("00000000-0000-0000-0000-000000000003"), "operator", self.password_hash, "operator", "active"),
            ]
        )
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        try:
            from app.api.point_processings import (
                get_point_processing_templates,
                get_point_processings,
                router,
            )
        except ImportError:
            pass
        else:
            app.include_router(router, prefix="/api/v1")
            app.dependency_overrides[get_point_processings] = lambda: service
            templates = InMemoryPointProcessingTemplates(configuration_revision=0)
            app.dependency_overrides[get_point_processing_templates] = lambda: templates
            app.state.point_processing_templates = templates
        app.dependency_overrides[get_identity] = lambda: Identity(identity_repository)
        return app, identity_repository, repository

    async def login(self, client: httpx.AsyncClient, username: str) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": self.password},
        )
        self.assertEqual(200, response.status_code, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def test_formula_preview_is_read_only_typed_and_engineer_only(self) -> None:
        app, _, repository = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            operator_headers = await self.login(client, "operator")
            engineer_headers = await self.login(client, "engineer")
            body = {
                "template_revision_id": str(SITE_FORMULA_REVISION_ID),
                "expression": "sum(pcs_power)",
            }
            templates = await client.get(
                "/api/v1/point-processing-templates?device_category=SITE",
                headers=engineer_headers,
            )
            denied = await client.post(
                f"/api/v1/nodes/{NODE_ID}/point-processing-formula-preview",
                headers=operator_headers,
                json=body,
            )
            response = await client.post(
                f"/api/v1/nodes/{NODE_ID}/point-processing-formula-preview",
                headers=engineer_headers,
                json=body,
            )

        self.assertEqual(403, denied.status_code, denied.text)
        self.assertEqual(200, templates.status_code, templates.text)
        template = templates.json()["items"][0]
        self.assertEqual("many", template["inputs"][0]["cardinality"])
        self.assertEqual("PCS", template["inputs"][0]["selector"]["nodeType"])
        self.assertEqual("formula", template["outputs"][0]["transform"]["kind"])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("FLOAT", response.json()["result_type"])
        self.assertEqual("kW", response.json()["result_unit"])
        self.assertEqual(2, response.json()["member_count"])
        self.assertEqual(2, response.json()["dag_summary"]["edge_count"])
        self.assertEqual(1, repository.application_count())

    async def test_standalone_json_import_export_is_immutable(self) -> None:
        from tests.test_point_processing_templates import template_json

        app, _, _ = self.build_app()
        registry = app.state.point_processing_templates
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            admin_headers = await self.login(client, "admin")
            engineer_headers = await self.login(client, "engineer")
            validated = await client.post(
                "/api/v1/point-processing-templates/validate",
                headers=admin_headers,
                json=template_json(),
            )
            validation_was_not_persisted = await client.get(
                f"/api/v1/point-processing-templates/{validated.json()['revision_id']}/export",
                headers=admin_headers,
            )
            denied_engineer_validate = await client.post(
                "/api/v1/point-processing-templates/validate",
                headers=engineer_headers,
                json=template_json(),
            )
            denied_engineer_import = await client.post(
                "/api/v1/point-processing-templates/import",
                headers=engineer_headers,
                json=template_json(),
            )
            created = await client.post(
                "/api/v1/point-processing-templates/import",
                headers=admin_headers,
                json=template_json(),
            )
            changed = template_json()
            changed["displayName"] = "篡改名称"
            conflict = await client.post(
                "/api/v1/point-processing-templates/import",
                headers=admin_headers,
                json=changed,
            )
            anonymous_export = await client.get(
                f"/api/v1/point-processing-templates/{created.json()['revision_id']}/export"
            )
            exported = await client.get(
                f"/api/v1/point-processing-templates/{created.json()['revision_id']}/export",
                headers=engineer_headers,
            )

        self.assertEqual(200, validated.status_code, validated.text)
        self.assertEqual(template_json(), validated.json()["content"])
        self.assertEqual(404, validation_was_not_persisted.status_code)
        self.assertEqual(0, registry.configuration_revision)
        self.assertEqual(403, denied_engineer_validate.status_code)
        self.assertEqual(403, denied_engineer_import.status_code)
        self.assertEqual(201, created.status_code, created.text)
        self.assertEqual(0, registry.configuration_revision)
        self.assertEqual(409, conflict.status_code, conflict.text)
        self.assertEqual(
            "POINT_PROCESSING_REVISION_IMMUTABLE",
            conflict.json()["detail"]["code"],
        )
        self.assertEqual(401, anonymous_export.status_code)
        self.assertEqual(200, exported.status_code, exported.text)
        self.assertEqual(template_json(), exported.json())
        self.assertEqual(
            f'"{created.json()["content_digest"]}"',
            exported.headers["etag"],
        )

    async def test_public_role_matrix_plan_apply_and_operator_projection(self) -> None:
        app, identity_repository, repository = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            anonymous = await client.get(
                "/api/v1/point-processing-templates?device_category=PCS"
            )
            legacy_route = await client.get(
                "/api/v1/point-conversion-templates"
            )
            operator_headers = await self.login(client, "operator")
            engineer_headers = await self.login(client, "engineer")
            operator_templates = await client.get(
                "/api/v1/point-processing-templates?device_category=PCS",
                headers=operator_headers,
            )
            templates = await client.get(
                "/api/v1/point-processing-templates?device_category=PCS",
                headers=engineer_headers,
            )
            planned = await client.post(
                f"/api/v1/nodes/{NODE_ID}/point-processing-plans",
                headers=engineer_headers,
                json={
                    "template_revision_id": str(BRAND_B_REVISION_ID),
                    "input_selections": {},
                },
            )
            self.assertEqual(201, planned.status_code, planned.text)
            operator_apply = await client.post(
                f"/api/v1/point-processing-plans/{planned.json()['id']}/apply",
                headers={**operator_headers, "Idempotency-Key": "operator-denied"},
                json={"plan_digest": planned.json()["digest"]},
            )
            count_after_denied = repository.application_count()
            applied = await client.post(
                f"/api/v1/point-processing-plans/{planned.json()['id']}/apply",
                headers={**engineer_headers, "Idempotency-Key": "engineer-apply"},
                json={"plan_digest": planned.json()["digest"]},
            )
            operator_trunk = await client.get(
                f"/api/v1/nodes/{NODE_ID}/data-trunk",
                headers=operator_headers,
            )
            engineer_trunk = await client.get(
                f"/api/v1/nodes/{NODE_ID}/data-trunk",
                headers=engineer_headers,
            )

        self.assertEqual(401, anonymous.status_code, anonymous.text)
        self.assertEqual(404, legacy_route.status_code, legacy_route.text)
        self.assertEqual(403, operator_templates.status_code, operator_templates.text)
        self.assertEqual(200, templates.status_code, templates.text)
        self.assertEqual(2, templates.json()["total"])
        brand_a = next(
            item for item in templates.json()["items"]
            if item["asset_id"] == "pcs.brand-a"
        )
        self.assertEqual(
            ["active_power_raw", "fault_codes_raw", "operating_state_raw"],
            sorted(item["input_id"] for item in brand_a["inputs"]),
        )
        self.assertTrue(all("source_key" in item for item in brand_a["inputs"]))
        self.assertEqual(201, planned.status_code, planned.text)
        self.assertEqual("ready", planned.json()["status"])
        self.assertEqual(403, operator_apply.status_code, operator_apply.text)
        self.assertEqual(1, count_after_denied)
        self.assertEqual(201, applied.status_code, applied.text)
        self.assertEqual(2, repository.application_count())
        self.assertEqual(200, operator_trunk.status_code, operator_trunk.text)
        self.assertTrue(operator_trunk.json()["l2"])
        self.assertEqual([], operator_trunk.json()["l0"])
        self.assertNotIn("input_bindings", operator_trunk.json()["l1_summary"])
        self.assertTrue(engineer_trunk.json()["l0"])
        self.assertIn("input_bindings", engineer_trunk.json()["l1_summary"])
        self.assertTrue(
            all("requires_scan" in item for item in templates.json()["items"])
        )
        self.assertTrue(
            all(
                set(item) == {"input_id", "source_kind", "source_key"}
                for item in operator_trunk.json()["l1_summary"]["source_summary"]
            )
        )
        self.assertTrue(
            all("processing_kind" in item for item in operator_trunk.json()["l2"])
        )
        sources_by_output = {
            item["output_key"]: [source["input_id"] for source in item["source_summary"]]
            for item in operator_trunk.json()["l2"]
        }
        self.assertEqual(["active_power_raw"], sources_by_output["active_power"])
        self.assertEqual(["operating_state_raw"], sources_by_output["operating_state"])
        self.assertEqual(["fault_codes_raw"], sources_by_output["fault_codes"])
        self.assertNotIn(
            "source_id",
            operator_trunk.json()["l1_summary"]["source_summary"][0],
        )
        self.assertTrue(
            any(
                event.event == "authorization.decision"
                and event.outcome == "denied"
                for event in identity_repository.audits
            )
        )

if __name__ == "__main__":
    unittest.main()
