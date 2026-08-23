"""Authenticated public HTTP seam for point-processing planning and apply."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
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
    PointProcessingDelivery,
    PointProcessingSource,
)
from app.services.solution_delivery import InMemoryDeliveryRepository, SolutionDelivery
from app.services.solution_point_processings import point_processing_assets


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_reference_delivery.py"
SPEC = importlib.util.spec_from_file_location("build_reference_delivery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

NODE_ID = UUID("84000000-0000-0000-0000-000000000001")
ENTITY_IDENTITY_INSTALLATION_ID = UUID("84000000-0000-0000-0000-000000000002")
SOLUTION_INSTALLATION_ID = UUID("84000000-0000-0000-0000-000000000003")
BRAND_A_REVISION_ID = UUID("84000000-0000-0000-0000-00000000000a")
BRAND_B_REVISION_ID = UUID("84000000-0000-0000-0000-00000000000b")


class PointProcessingPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(
            cls.password,
            salt=b"point-conv-auth",
        )

    def build_app(self) -> tuple[FastAPI, InMemoryIdentityRepository, InMemoryPointProcessingRepository]:
        package = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        ).import_package(builder.build_archive(), "user:test-engineer")
        assets = {item.asset_id: item for item in point_processing_assets(package)}
        repository = InMemoryPointProcessingRepository()
        service = PointProcessingDelivery(
            repository,
            InMemoryPointProcessingCatalog(
                templates={
                    BRAND_A_REVISION_ID: assets["pcs.brand-a"],
                    BRAND_B_REVISION_ID: assets["pcs.brand-b"],
                },
                sources=(
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000001"), "l0", NODE_ID, "ActivePowerRaw", "FLOAT", "W", True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000002"), "l0", NODE_ID, "RunningState", "STRING", None, True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000003"), "l0", NODE_ID, "FaultCodeText", "STRING", None, True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000011"), "l0", NODE_ID, "PActKw", "FLOAT", "kW", True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000012"), "l0", NODE_ID, "ModeCode", "STRING", None, True),
                    PointProcessingSource(UUID("85000000-0000-0000-0000-000000000013"), "l0", NODE_ID, "AlarmList", "STRING", None, True),
                ),
            ),
        )
        initial_plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=BRAND_A_REVISION_ID,
                input_selections={},
                actor="user:seed-engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
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
            from app.api.point_processings import get_point_processings, router
        except ImportError:
            pass
        else:
            app.include_router(router, prefix="/api/v1")
            app.dependency_overrides[get_point_processings] = lambda: service
        app.dependency_overrides[get_identity] = lambda: Identity(identity_repository)
        return app, identity_repository, repository

    async def login(self, client: httpx.AsyncClient, username: str) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": self.password},
        )
        self.assertEqual(200, response.status_code, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

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
            any(
                event.event == "authorization.decision"
                and event.outcome == "denied"
                for event in identity_repository.audits
            )
        )

    async def test_acceptance_report_is_authenticated_and_persistently_addressable(self) -> None:
        app, _, _ = self.build_app()
        application_id = UUID("84000000-0000-0000-0000-000000000101")
        report_id = UUID("84000000-0000-0000-0000-000000000102")
        report_body = {
            "id": str(report_id),
            "application_id": str(application_id),
            "passed": True,
            "checks": [],
        }
        report = Mock()
        report.public_dict.return_value = report_body
        transport = httpx.ASGITransport(app=app)
        with (
            patch(
                "app.services.en9_point_processing_acceptance.run_en9_acceptance",
                return_value=report,
            ) as run_acceptance,
            patch(
                "app.services.en9_point_processing_acceptance.get_en9_acceptance_report",
                return_value=report_body,
            ) as get_report,
            patch(
                "app.services.en9_point_processing_acceptance.get_latest_en9_acceptance_state",
                return_value={
                    "application": {"id": str(application_id)},
                    "latest_report": report_body,
                },
            ) as get_state,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                anonymous = await client.post(
                    f"/api/v1/point-processing-applications/{application_id}/acceptance",
                    json={"observed_for_seconds": 1800},
                )
                operator_headers = await self.login(client, "operator")
                engineer_headers = await self.login(client, "engineer")
                operator = await client.post(
                    f"/api/v1/point-processing-applications/{application_id}/acceptance",
                    headers=operator_headers,
                    json={"observed_for_seconds": 1800},
                )
                created = await client.post(
                    f"/api/v1/point-processing-applications/{application_id}/acceptance",
                    headers=engineer_headers,
                    json={"observed_for_seconds": 1800},
                )
                fetched = await client.get(
                    f"/api/v1/point-processing-acceptance-reports/{report_id}",
                    headers=engineer_headers,
                )
                restored = await client.get(
                    f"/api/v1/nodes/{NODE_ID}/point-processing-acceptance-state",
                    headers=engineer_headers,
                )

        self.assertEqual(401, anonymous.status_code, anonymous.text)
        self.assertEqual(403, operator.status_code, operator.text)
        self.assertEqual(201, created.status_code, created.text)
        self.assertEqual(report_body, created.json())
        self.assertEqual(200, fetched.status_code, fetched.text)
        self.assertEqual(report_body, fetched.json())
        self.assertEqual(200, restored.status_code, restored.text)
        self.assertEqual(str(application_id), restored.json()["application"]["id"])
        run_acceptance.assert_called_once_with(application_id, 1800.0)
        get_report.assert_called_once_with(report_id)
        get_state.assert_called_once_with(NODE_ID)


if __name__ == "__main__":
    unittest.main()
