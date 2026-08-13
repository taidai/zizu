from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import unittest
import zipfile
from uuid import UUID

from fastapi import FastAPI

from tests.test_delivery_public_api import (
    AsgiPublicApiProbe,
    AuthenticatedDeliveryClient,
)


TAG_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_TAG_ID = UUID("20000000-0000-0000-0000-000000000002")


def build_entity_package(*, package_version: str = "1.0.0") -> bytes:
    acceptance = (
        "schemaVersion: zizu.acceptance/v1alpha1\n"
        "id: acceptance.platform-liveness\n"
        "kind: platform_liveness\n"
        "required: true\n"
        "timeout: 5s\n"
    ).encode()
    entity_definition = (
        "schemaVersion: zizu.entity-definition/v1alpha1\n"
        "id: pcs.activePower\n"
        "kind: entity_definition\n"
        "displayName: Active power\n"
        "deviceCategory: pcs\n"
        "dataType: FLOAT\n"
        "unit: kW\n"
        "direction: R\n"
    ).encode()
    entity_slot = (
        "schemaVersion: zizu.entity-instance-slot/v1alpha1\n"
        "id: slot.pcs-primary\n"
        "kind: entity_instance_slot\n"
        "deviceCategory: pcs\n"
        "count: 1\n"
        "instanceKeyParameter: pcs.instance_key\n"
        "displayName: Primary PCS\n"
        "freshness: 30s\n"
        "requiredEntities:\n"
        "  - definition: pcs.activePower\n"
        "    matcher:\n"
        "      id: matcher.pcs-active-power\n"
        "      deviceKeyParameter: pcs.device_key\n"
        "      tagName: ActivePower\n"
    ).encode()
    entity_acceptance = (
        "schemaVersion: zizu.acceptance/v1alpha1\n"
        "id: acceptance.pcs-active-power\n"
        "kind: entity_readiness\n"
        "required: true\n"
        "slot: slot.pcs-primary\n"
        "definition: pcs.activePower\n"
        "freshness: 30s\n"
        "timeout: 5s\n"
    ).encode()
    acceptance_digest = hashlib.sha256(acceptance).hexdigest()
    definition_digest = hashlib.sha256(entity_definition).hexdigest()
    entity_digest = hashlib.sha256(entity_slot).hexdigest()
    entity_acceptance_digest = hashlib.sha256(entity_acceptance).hexdigest()
    manifest = (
        "schemaVersion: zizu.solution/v1alpha1\n"
        "id: org.zizu.single-pcs\n"
        f"version: {package_version}\n"
        "displayName: Single PCS EMS\n"
        "platform:\n"
        "  version: \">=0.4.77,<0.5.0\"\n"
        "parameters:\n"
        "  - id: pcs.instance_key\n"
        "    type: string\n"
        "    required: true\n"
        "    description: Stable site-local PCS key\n"
        "  - id: pcs.device_key\n"
        "    type: string\n"
        "    required: true\n"
        "    description: Source catalog device key\n"
        "assets:\n"
        "  - id: acceptance.platform-liveness\n"
        "    kind: acceptance\n"
        "    path: acceptance/liveness.yaml\n"
        f"    sha256: \"{acceptance_digest}\"\n"
        "  - id: slot.pcs-primary\n"
        "    kind: entity_instance_slot\n"
        "    path: entities/pcs-primary.yaml\n"
        f"    sha256: \"{entity_digest}\"\n"
        "  - id: pcs.activePower\n"
        "    kind: entity_definition\n"
        "    path: entities/pcs-active-power.yaml\n"
        f"    sha256: \"{definition_digest}\"\n"
        "  - id: acceptance.pcs-active-power\n"
        "    kind: acceptance\n"
        "    path: acceptance/pcs-active-power.yaml\n"
        f"    sha256: \"{entity_acceptance_digest}\"\n"
        "acceptance:\n"
        "  - acceptance.platform-liveness\n"
        "  - acceptance.pcs-active-power\n"
    ).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("solution.yaml", manifest)
        package.writestr("acceptance/liveness.yaml", acceptance)
        package.writestr("entities/pcs-primary.yaml", entity_slot)
        package.writestr("entities/pcs-active-power.yaml", entity_definition)
        package.writestr("acceptance/pcs-active-power.yaml", entity_acceptance)
    return archive.getvalue()


class EntityDeliveryPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def build_app(*, sources: tuple) -> FastAPI:
        from app.api.entity_instances import (
            get_entity_instance_runtime,
            router as entity_instance_router,
        )
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.api.health import router as health_router
        from app.services.entity_instance_registry import (
            EntityInstanceRegistry,
            InMemoryEntityInstanceRepository,
            InMemorySourceCatalog,
            SourceDescriptor,
        )
        from app.services.entity_instance_runtime import (
            EntityInstanceRuntime,
            InMemoryObservationCatalog,
            InMemoryNeuronProtocolSimulator,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        delivery_repository = InMemoryDeliveryRepository()
        source_catalog = InMemorySourceCatalog(sources)
        registry = EntityInstanceRegistry(
            InMemoryEntityInstanceRepository(),
            source_catalog,
            delivery_repository.site_configuration_version,
        )
        observations = InMemoryObservationCatalog()
        simulator = InMemoryNeuronProtocolSimulator(source_catalog, observations)
        runtime = EntityInstanceRuntime(registry, observations)
        app = FastAPI()

        @app.post("/protocol-simulator/neuron")
        async def publish_neuron(payload: dict) -> dict:
            message = payload["message"]
            published = simulator.publish(
                topic=payload.get("topic", "neuron/PCS-01/telemetry"),
                payload=json.dumps(message, separators=(",", ":")).encode("utf-8"),
                quality=int(payload.get("quality", 192)),
            )
            return {"published": published}

        app.include_router(health_router, prefix="/api/v1")
        app.include_router(delivery_router, prefix="/api/v1")
        app.include_router(entity_instance_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            delivery_repository,
            platform_version="0.4.77",
            public_api_probe=AsgiPublicApiProbe(app),
            entity_instance_registry=registry,
            entity_instance_runtime=runtime,
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        app.dependency_overrides[get_entity_instance_runtime] = lambda: runtime
        return app

    @staticmethod
    def source(tag_id: UUID = TAG_ID):
        from app.services.entity_instance_registry import SourceDescriptor

        return SourceDescriptor(
            tag_id=tag_id,
            device_key="PCS-01",
            device_name="PCS-01",
            tag_name="ActivePower",
            data_type="FLOAT",
            unit="kW",
            direction="R",
            enabled=True,
        )

    @staticmethod
    async def import_and_plan(client: AuthenticatedDeliveryClient, **body):
        imported = await client.post(
            "/api/v1/solution-packages/import",
            files={
                "archive": (
                    "single-pcs.zizu.zip",
                    build_entity_package(),
                    "application/zip",
                )
            },
        )
        if imported.status_code != 201:
            raise AssertionError(imported.text)
        planned = await client.post(
            f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
            json={
                "parameters": {
                    "pcs.instance_key": "PCS-01",
                    "pcs.device_key": "PCS-01",
                },
                **body,
            },
        )
        return imported, planned

    async def test_package_to_confirmed_fresh_entity_delivery_report(self) -> None:
        app = self.build_app(
            sources=(self.source(),),
        )

        async with AuthenticatedDeliveryClient(app) as client:
            imported, planned = await self.import_and_plan(client)
            self.assertEqual(["slot.pcs-primary"], imported.json()["entity_slot_ids"])
            self.assertEqual(201, planned.status_code, planned.text)
            plan = planned.json()
            binding = next(item for item in plan["items"] if item["kind"] == "entity_binding")
            self.assertEqual("ENTITY_BINDING_READY", binding["code"])
            self.assertEqual(str(TAG_ID), binding["selected_tag_id"])
            self.assertIn("device_key=PCS-01", binding["candidates"][0]["reason"])

            installed = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json={"plan_digest": plan["digest"]},
                headers={"Idempotency-Key": "install-single-pcs"},
            )
            self.assertEqual(201, installed.status_code, installed.text)
            entity_instance_id = installed.json()["entity_instance_ids"][0]

            published = await client.post(
                "/protocol-simulator/neuron",
                json={
                    "message": {
                        "node": "PCS-01",
                        "timestamp": round(datetime.now(timezone.utc).timestamp() * 1000),
                        "values": {"ActivePower": 125.5},
                    }
                },
            )
            self.assertEqual(200, published.status_code, published.text)
            self.assertEqual(1, published.json()["published"])

            realtime = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime"
            )
            self.assertEqual(200, realtime.status_code, realtime.text)
            self.assertEqual(125.5, realtime.json()["value"])

            accepted = await client.post(
                f"/api/v1/solution-installations/{installed.json()['id']}/acceptance-runs",
                headers={"Idempotency-Key": "accept-single-pcs"},
            )
            self.assertEqual(201, accepted.status_code, accepted.text)
            report = accepted.json()
            self.assertEqual("passed", report["status"])
            entity_evidence = next(
                item for item in report["items"] if item["code"] == "ENTITY_BINDING_FRESH"
            )
            self.assertEqual(entity_instance_id, entity_evidence["evidence"]["entity_instance_id"])
            self.assertEqual(1, entity_evidence["evidence"]["primary_source_count"])

            upgraded_import = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "single-pcs-v1.0.1.zizu.zip",
                        build_entity_package(package_version="1.0.1"),
                        "application/zip",
                    )
                },
            )
            upgraded_plan = await client.post(
                f"/api/v1/solution-packages/{upgraded_import.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instance_key": "PCS-01",
                        "pcs.device_key": "PCS-01",
                    }
                },
            )
            upgraded = await client.post(
                f"/api/v1/install-plans/{upgraded_plan.json()['id']}/apply",
                json={"plan_digest": upgraded_plan.json()["digest"]},
                headers={"Idempotency-Key": "upgrade-single-pcs"},
            )
            self.assertEqual(201, upgraded.status_code, upgraded.text)
            self.assertEqual(
                [entity_instance_id],
                upgraded.json()["entity_instance_ids"],
            )
            self.assertEqual(
                plan["entity_identity_installation_id"],
                upgraded_plan.json()["entity_identity_installation_id"],
            )

    async def test_ambiguous_sources_require_explicit_selection_and_stale_plan_is_zero_write(
        self,
    ) -> None:
        app = self.build_app(
            sources=(self.source(), self.source(OTHER_TAG_ID)),
        )
        key = "slot.pcs-primary/PCS-01/pcs.activePower"
        async with AuthenticatedDeliveryClient(app) as client:
            imported, blocked = await self.import_and_plan(client)
            self.assertEqual("blocked", blocked.json()["status"])
            self.assertEqual(
                "ENTITY_BINDING_AMBIGUOUS",
                blocked.json()["blockers"][0]["code"],
            )
            rejected = await client.post(
                f"/api/v1/install-plans/{blocked.json()['id']}/apply",
                json={"plan_digest": blocked.json()["digest"]},
                headers={"Idempotency-Key": "blocked-binding"},
            )
            self.assertEqual(409, rejected.status_code)

            plans = []
            for selected in (TAG_ID, OTHER_TAG_ID):
                response = await client.post(
                    f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                    json={
                        "parameters": {
                            "pcs.instance_key": "PCS-01",
                            "pcs.device_key": "PCS-01",
                        },
                        "binding_selections": {key: str(selected)},
                    },
                )
                self.assertEqual(201, response.status_code, response.text)
                plans.append(response.json())
            self.assertEqual(
                plans[0]["entity_identity_installation_id"],
                plans[1]["entity_identity_installation_id"],
            )
            self.assertNotEqual(
                plans[0]["target_installation_id"],
                plans[1]["target_installation_id"],
            )

            installed = await client.post(
                f"/api/v1/install-plans/{plans[0]['id']}/apply",
                json={"plan_digest": plans[0]["digest"]},
                headers={"Idempotency-Key": "selected-binding"},
            )
            self.assertEqual(201, installed.status_code, installed.text)
            stale = await client.post(
                f"/api/v1/install-plans/{plans[1]['id']}/apply",
                json={"plan_digest": plans[1]["digest"]},
                headers={"Idempotency-Key": "stale-binding"},
            )
            self.assertEqual(409, stale.status_code)
            self.assertEqual(
                "INSTALL_PLAN_STALE",
                stale.json()["detail"]["code"],
            )
            installations = await client.get("/api/v1/solution-installations")
            self.assertEqual(1, installations.json()["total"])

    async def test_stale_entity_observation_fails_the_public_delivery_report(self) -> None:
        from datetime import timedelta

        app = self.build_app(sources=(self.source(),))
        async with AuthenticatedDeliveryClient(app) as client:
            _, planned = await self.import_and_plan(client)
            installed = await client.post(
                f"/api/v1/install-plans/{planned.json()['id']}/apply",
                json={"plan_digest": planned.json()["digest"]},
                headers={"Idempotency-Key": "install-stale-data"},
            )
            entity_instance_id = installed.json()["entity_instance_ids"][0]
            published = await client.post(
                "/protocol-simulator/neuron",
                json={
                    "message": {
                        "node": "PCS-01",
                        "timestamp": round(
                            (datetime.now(timezone.utc) - timedelta(minutes=2)).timestamp()
                            * 1000
                        ),
                        "values": {"ActivePower": 125.5},
                    }
                },
            )
            self.assertEqual(1, published.json()["published"])
            realtime = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime"
            )
            self.assertEqual(409, realtime.status_code, realtime.text)
            self.assertEqual("ENTITY_DATA_STALE", realtime.json()["detail"]["code"])
            accepted = await client.post(
                f"/api/v1/solution-installations/{installed.json()['id']}/acceptance-runs",
                headers={"Idempotency-Key": "accept-stale-data"},
            )
            self.assertEqual(201, accepted.status_code, accepted.text)
            self.assertEqual("failed", accepted.json()["status"])
            self.assertIn(
                "ENTITY_DATA_STALE",
                [item["code"] for item in accepted.json()["items"]],
            )

    async def test_bad_quality_fails_realtime_and_delivery_report(self) -> None:
        app = self.build_app(sources=(self.source(),))
        async with AuthenticatedDeliveryClient(app) as client:
            _, planned = await self.import_and_plan(client)
            installed = await client.post(
                f"/api/v1/install-plans/{planned.json()['id']}/apply",
                json={"plan_digest": planned.json()["digest"]},
                headers={"Idempotency-Key": "install-bad-quality"},
            )
            entity_instance_id = installed.json()["entity_instance_ids"][0]
            published = await client.post(
                "/protocol-simulator/neuron",
                json={
                    "quality": 0,
                    "message": {
                        "node": "PCS-01",
                        "timestamp": round(datetime.now(timezone.utc).timestamp() * 1000),
                        "values": {"ActivePower": 125.5},
                    },
                },
            )
            self.assertEqual(1, published.json()["published"])
            realtime = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime"
            )
            self.assertEqual(409, realtime.status_code, realtime.text)
            self.assertEqual(
                "ENTITY_DATA_QUALITY_BAD",
                realtime.json()["detail"]["code"],
            )
            accepted = await client.post(
                f"/api/v1/solution-installations/{installed.json()['id']}/acceptance-runs",
                headers={"Idempotency-Key": "accept-bad-quality"},
            )
            self.assertEqual("failed", accepted.json()["status"])
            self.assertIn(
                "ENTITY_DATA_QUALITY_BAD",
                [item["code"] for item in accepted.json()["items"]],
            )


if __name__ == "__main__":
    unittest.main()
