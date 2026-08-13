from __future__ import annotations

from datetime import datetime, timezone
import unittest

from fastapi import FastAPI

from tests.test_delivery_public_api import AuthenticatedDeliveryClient
from tests import test_entity_delivery_public_api as entity_delivery_test


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def dispatch(self, request: object) -> None:
        self.requests.append(request)


class ControlCommandPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def build_app(*, high_risk: bool = False) -> tuple[FastAPI, RecordingDispatcher]:
        from app.api.control_commands import (
            get_default_control_commands,
            router as control_router,
        )
        from app.services.control_commands import (
            ControlCommandRuntime,
            InMemoryControlCommandRepository,
        )
        from app.services.entity_instance_registry import SourceDescriptor

        sources = (
            SourceDescriptor(entity_delivery_test.TAG_ID, "PCS-01", "PCS-01", "Setpoint", "FLOAT", "kW", "RW", True),
            SourceDescriptor(entity_delivery_test.OTHER_TAG_ID, "PCS-01", "PCS-01", "Readback", "FLOAT", "kW", "R", True),
            SourceDescriptor(entity_delivery_test.BACKUP_TAG_ID, "PCS-01", "PCS-01", "Ready", "BOOL", None, "R", True),
        )
        app = entity_delivery_test.EntityDeliveryPublicApiTest.build_app(sources=sources)
        dispatcher = RecordingDispatcher()
        runtime = ControlCommandRuntime(
            registry=app.state.entity_instance_registry,
            policies=app.state.entity_instance_repository,
            readback=app.state.entity_instance_runtime,
            dispatcher=dispatcher,
            repository=InMemoryControlCommandRepository(),
        )
        app.include_router(control_router, prefix="/api/v1")
        app.dependency_overrides[get_default_control_commands] = lambda: runtime
        return app, dispatcher

    @staticmethod
    async def install(
        client: AuthenticatedDeliveryClient,
        *,
        high_risk: bool = False,
    ) -> dict[str, str]:
        imported = await client.post(
            "/api/v1/solution-packages/import",
            files={"archive": ("control-pcs.zizu.zip", entity_delivery_test.build_control_entity_package(high_risk=high_risk), "application/zip")},
        )
        if imported.status_code != 201:
            raise AssertionError(imported.text)
        planned = await client.post(
            f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
            json={"parameters": {"pcs.instance_key": "PCS-01", "pcs.device_key": "PCS-01"}},
        )
        if planned.status_code != 201:
            raise AssertionError(planned.text)
        applied = await client.post(
            f"/api/v1/install-plans/{planned.json()['id']}/apply",
            json={"plan_digest": planned.json()["digest"]},
            headers={"Idempotency-Key": "install-controllable-pcs"},
        )
        if applied.status_code != 201:
            raise AssertionError(applied.text)
        return {
            item["definition_id"]: item["entity_instance_id"]
            for item in planned.json()["items"]
            if item["kind"] == "entity_binding"
        }

    @staticmethod
    async def publish(client: AuthenticatedDeliveryClient, **values: object) -> None:
        response = await client._client.post(
            "/protocol-simulator/neuron",
            json={
                "message": {
                    "node": "PCS-01",
                    "timestamp": round(datetime.now(timezone.utc).timestamp() * 1000),
                    "values": values,
                }
            },
        )
        if response.status_code != 200:
            raise AssertionError(response.text)

    async def test_operator_control_moves_from_dispatch_to_readback_confirmation(self) -> None:
        app, dispatcher = self.build_app()
        async with AuthenticatedDeliveryClient(app) as client:
            ids = await self.install(client)
            await self.publish(client, Ready=True, Readback=0.0)
            headers = {
                "Authorization": await client._bearer("operator"),
                "Idempotency-Key": "operator-setpoint-20",
            }
            submitted = await client._client.post(
                f"/api/v1/entity-instances/{ids['pcs.setpoint']}/control-commands",
                headers=headers,
                json={"value": 20.0},
            )
            repeated = await client._client.post(
                f"/api/v1/entity-instances/{ids['pcs.setpoint']}/control-commands",
                headers=headers,
                json={"value": 20.0},
            )
            await self.publish(client, Readback=20.05)
            reconciled = await client._client.post(
                f"/api/v1/control-commands/{submitted.json()['id']}/reconcile",
                headers={"Authorization": await client._bearer("operator")},
            )

        self.assertEqual(201, submitted.status_code, submitted.text)
        self.assertEqual("dispatched", submitted.json()["status"])
        self.assertEqual(201, repeated.status_code, repeated.text)
        self.assertEqual(submitted.json()["id"], repeated.json()["id"])
        self.assertEqual(1, len(dispatcher.requests))
        self.assertEqual(200, reconciled.status_code, reconciled.text)
        self.assertEqual("readback_confirmed", reconciled.json()["status"])

    async def test_operator_cannot_bypass_interlock_or_limit(self) -> None:
        app, dispatcher = self.build_app()
        async with AuthenticatedDeliveryClient(app) as client:
            ids = await self.install(client)
            await self.publish(client, Ready=False, Readback=0.0)
            headers = {
                "Authorization": await client._bearer("operator"),
                "Idempotency-Key": "blocked-interlock",
            }
            blocked = await client._client.post(
                f"/api/v1/entity-instances/{ids['pcs.setpoint']}/control-commands",
                headers=headers,
                json={"value": 20.0},
            )
            too_high = await client._client.post(
                f"/api/v1/entity-instances/{ids['pcs.setpoint']}/control-commands",
                headers={**headers, "Idempotency-Key": "blocked-limit"},
                json={"value": 101.0},
            )

        self.assertEqual(409, blocked.status_code, blocked.text)
        self.assertEqual("CONTROL_INTERLOCK_UNSATISFIED", blocked.json()["detail"]["code"])
        self.assertEqual(409, too_high.status_code, too_high.text)
        self.assertEqual("CONTROL_VALUE_OUT_OF_RANGE", too_high.json()["detail"]["code"])
        self.assertEqual([], dispatcher.requests)

    async def test_high_risk_confirmation_is_content_bound_and_single_use(self) -> None:
        app, dispatcher = self.build_app(high_risk=True)
        async with AuthenticatedDeliveryClient(app) as client:
            ids = await self.install(client, high_risk=True)
            await self.publish(client, Ready=True, Readback=20.0)
            headers = {
                "Authorization": await client._bearer("operator"),
                "Idempotency-Key": "high-risk-setpoint-20",
            }
            missing = await client._client.post(
                f"/api/v1/entity-instances/{ids['pcs.setpoint']}/control-commands",
                headers=headers,
                json={"value": 20.0},
            )
            confirmation = await client._client.post(
                f"/api/v1/entity-instances/{ids['pcs.setpoint']}/control-confirmations",
                headers=headers,
                json={"value": 20.0},
            )
            changed = await client._client.post(
                f"/api/v1/entity-instances/{ids['pcs.setpoint']}/control-commands",
                headers={**headers, "Idempotency-Key": "high-risk-setpoint-21"},
                json={"value": 21.0, "confirmation_id": confirmation.json()["id"]},
            )
            confirmed = await client._client.post(
                f"/api/v1/entity-instances/{ids['pcs.setpoint']}/control-commands",
                headers=headers,
                json={"value": 20.0, "confirmation_id": confirmation.json()["id"]},
            )
            await self.publish(client, Readback=20.0)
            reconciled = await client._client.post(
                f"/api/v1/control-commands/{confirmed.json()['id']}/reconcile",
                headers={"Authorization": await client._bearer("operator")},
            )

        self.assertEqual(409, missing.status_code, missing.text)
        self.assertEqual("CONTROL_CONFIRMATION_REQUIRED", missing.json()["detail"]["code"])
        self.assertEqual(201, confirmation.status_code, confirmation.text)
        self.assertEqual(409, changed.status_code, changed.text)
        self.assertEqual("CONTROL_CONFIRMATION_INVALID", changed.json()["detail"]["code"])
        self.assertEqual(201, confirmed.status_code, confirmed.text)
        self.assertEqual("dispatched", confirmed.json()["status"])
        self.assertEqual(200, reconciled.status_code, reconciled.text)
        self.assertEqual("readback_confirmed", reconciled.json()["status"])
        self.assertEqual(1, len(dispatcher.requests))

    def test_every_control_route_declares_bearer_and_control_capability(self) -> None:
        from app.main import create_app

        schema = create_app().openapi()
        expected = {
            ("post", "/api/v1/entity-instances/{entity_instance_id}/control-confirmations"),
            ("post", "/api/v1/entity-instances/{entity_instance_id}/control-commands"),
            ("get", "/api/v1/control-commands/{command_id}"),
            ("post", "/api/v1/control-commands/{command_id}/reconcile"),
        }
        for method, path in expected:
            with self.subTest(method=method, path=path):
                operation = schema["paths"][path][method]
                self.assertEqual(operation.get("x-zizu-capability"), "control.write")
                self.assertEqual(operation.get("security"), [{"HTTPBearer": []}])


if __name__ == "__main__":
    unittest.main()
