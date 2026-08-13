from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI

from tests.test_delivery_public_api import AuthenticatedDeliveryClient
from tests import test_entity_delivery_public_api as entity_delivery_test


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def dispatch(self, request: object) -> None:
        self.requests.append(request)


class FailingDispatcher(RecordingDispatcher):
    def dispatch(self, request: object) -> None:
        super().dispatch(request)
        raise RuntimeError("simulated Neuron 403")


class ControlCommandPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def build_app(
        *, high_risk: bool = False, dispatcher_fails: bool = False
    ) -> tuple[FastAPI, RecordingDispatcher]:
        from app.api.control_commands import (
            get_default_control_commands,
            get_control_compatibility,
            router as control_router,
        )
        from app.api.neuron import router as neuron_router
        from app.api.rpc import router as rpc_router
        from app.services.control_commands import (
            ControlCommandCompatibility,
            ControlCommandRuntime,
            InMemoryControlTargetResolver,
            InMemoryControlCommandRepository,
        )
        from app.services.entity_instance_registry import SourceDescriptor

        sources = (
            SourceDescriptor(entity_delivery_test.TAG_ID, "PCS-01", "PCS-01", "Setpoint", "FLOAT", "kW", "RW", True),
            SourceDescriptor(entity_delivery_test.OTHER_TAG_ID, "PCS-01", "PCS-01", "Readback", "FLOAT", "kW", "R", True),
            SourceDescriptor(entity_delivery_test.BACKUP_TAG_ID, "PCS-01", "PCS-01", "Ready", "BOOL", None, "R", True),
        )
        app = entity_delivery_test.EntityDeliveryPublicApiTest.build_app(sources=sources)
        dispatcher = FailingDispatcher() if dispatcher_fails else RecordingDispatcher()
        runtime = ControlCommandRuntime(
            registry=app.state.entity_instance_registry,
            policies=app.state.entity_instance_repository,
            readback=app.state.entity_instance_runtime,
            dispatcher=dispatcher,
            repository=InMemoryControlCommandRepository(),
        )
        compatibility_targets = InMemoryControlTargetResolver()
        compatibility = ControlCommandCompatibility(runtime, compatibility_targets)
        app.include_router(control_router, prefix="/api/v1")
        app.include_router(neuron_router, prefix="/api/v1")
        app.include_router(rpc_router, prefix="/api/v1")
        app.dependency_overrides[get_default_control_commands] = lambda: runtime
        app.dependency_overrides[get_control_compatibility] = lambda: compatibility
        app.state.control_compatibility_targets = compatibility_targets
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

    async def test_neuron_and_rpc_compatibility_routes_create_commands_not_direct_writes(self) -> None:
        app, dispatcher = self.build_app()
        node_id = UUID("30000000-0000-0000-0000-000000000001")
        async with AuthenticatedDeliveryClient(app) as client:
            ids = await self.install(client)
            targets = app.state.control_compatibility_targets
            targets.register_neuron(
                node="PCS-01",
                group="default",
                tag="Setpoint",
                entity_instance_id=UUID(ids["pcs.setpoint"]),
            )
            targets.register_rpc(node_id, UUID(ids["pcs.setpoint"]))
            targets.register_legacy_rpc(
                node_id=node_id,
                command="pcs.setpoint",
                entity_instance_id=UUID(ids["pcs.setpoint"]),
            )
            await self.publish(client, Ready=True, Readback=0.0)
            headers = {
                "Authorization": await client._bearer("operator"),
                "Idempotency-Key": "legacy-neuron-setpoint-20",
            }
            neuron = await client._client.post(
                "/api/v1/neuron/write",
                headers=headers,
                json={"node": "PCS-01", "group": "default", "tag": "Setpoint", "value": 20.0},
            )
            unknown_neuron = await client._client.post(
                "/api/v1/neuron/write",
                headers={**headers, "Idempotency-Key": "legacy-neuron-unknown"},
                json={"node": "PCS-01", "group": "default", "tag": "Unknown", "value": 20.0},
            )
            legacy_rpc = await client._client.post(
                f"/api/v1/devices/{node_id}/rpc",
                headers=headers,
                json={
                    "command": "pcs.setpoint",
                    "payload": {"value": 20.0},
                    "topic": "ignored/arbitrary/topic",
                },
            )
            unknown_rpc = await client._client.post(
                f"/api/v1/devices/{node_id}/rpc",
                headers={**headers, "Idempotency-Key": "legacy-rpc-unknown"},
                json={"command": "unknown.command", "payload": {"value": 20.0}},
            )
            rpc = await client._client.post(
                f"/api/v1/devices/{node_id}/rpc",
                headers={**headers, "Idempotency-Key": "legacy-neuron-setpoint-20"},
                json={"entity_instance_id": ids["pcs.setpoint"], "value": 20.0},
            )
            queried = await client._client.get(
                neuron.json()["links"]["command"],
                headers={"Authorization": await client._bearer("operator")},
            )

        self.assertEqual(201, neuron.status_code, neuron.text)
        self.assertEqual("compatibility", neuron.json()["source_type"])
        self.assertEqual("dispatched", neuron.json()["status"])
        self.assertEqual("/api/v1/entity-instances/{id}/control-commands", neuron.json()["migration"]["replacement"])
        self.assertEqual(409, unknown_neuron.status_code, unknown_neuron.text)
        self.assertEqual("CONTROL_COMPATIBILITY_TARGET_UNRESOLVED", unknown_neuron.json()["detail"]["code"])
        self.assertIsNone(
            unknown_neuron.json()["detail"]["command"]["entity_instance_id"],
        )
        self.assertEqual(201, legacy_rpc.status_code, legacy_rpc.text)
        self.assertEqual(neuron.json()["id"], legacy_rpc.json()["id"])
        self.assertEqual(409, unknown_rpc.status_code, unknown_rpc.text)
        self.assertEqual(
            "CONTROL_COMPATIBILITY_TARGET_UNRESOLVED",
            unknown_rpc.json()["detail"]["code"],
        )
        self.assertEqual(201, rpc.status_code, rpc.text)
        self.assertEqual("compatibility", rpc.json()["source_type"])
        self.assertEqual(neuron.json()["id"], rpc.json()["id"])
        self.assertEqual(
            f"/api/v1/control-commands/{neuron.json()['id']}",
            neuron.json()["links"]["command"],
        )
        self.assertEqual(200, queried.status_code, queried.text)
        self.assertEqual(neuron.json()["id"], queried.json()["id"])
        self.assertEqual(1, len(dispatcher.requests))

    async def test_compatibility_neuron_failure_is_a_failed_command_not_device_success(self) -> None:
        app, dispatcher = self.build_app(dispatcher_fails=True)
        async with AuthenticatedDeliveryClient(app) as client:
            ids = await self.install(client)
            targets = app.state.control_compatibility_targets
            targets.register_neuron(
                node="PCS-01",
                group="default",
                tag="Setpoint",
                entity_instance_id=UUID(ids["pcs.setpoint"]),
            )
            await self.publish(client, Ready=True, Readback=0.0)
            failed = await client._client.post(
                "/api/v1/neuron/write",
                headers={
                    "Authorization": await client._bearer("operator"),
                    "Idempotency-Key": "legacy-neuron-unavailable",
                },
                json={"node": "PCS-01", "group": "default", "tag": "Setpoint", "value": 20.0},
            )

        self.assertEqual(201, failed.status_code, failed.text)
        self.assertEqual("failed", failed.json()["status"])
        self.assertEqual("CONTROL_DISPATCH_FAILED", failed.json()["code"])
        self.assertEqual("compatibility", failed.json()["source_type"])
        self.assertEqual(1, len(dispatcher.requests))

    async def test_rule_trigger_replays_as_one_command_then_confirms_via_protocol_readback(self) -> None:
        """The rule execution seam shares command audit, cooldown, and readback semantics."""
        from app.api.control_commands import get_default_control_commands
        from app.services.automated_control_commands import AutomatedControlCommands
        from app.services.rule_engine import run_rule_tick

        app, dispatcher = self.build_app()
        rule_id = UUID("80000000-0000-0000-0000-000000000001")
        async with AuthenticatedDeliveryClient(app) as client:
            ids = await self.install(client)
            await self.publish(client, Ready=1)
            rule_content = {
                "when": "ready == 1",
                "_config": {
                    "sourceEntityInstanceIds": [ids["bms.ready"]],
                    "inputMappings": {"ready": ids["bms.ready"]},
                    "actions": [{
                        "id": "setpoint-from-ready",
                        "type": "control",
                        "entity_instance_id": ids["pcs.setpoint"],
                        "value": 20.0,
                    }],
                },
            }

            class RuleCursor:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def execute(self, query, params=()):
                    self.query = query
                    self.params = params

                def fetchall(self):
                    return [
                        (
                            rule_id,
                            "control",
                            json.dumps(rule_content),
                            True,
                            4,
                        )
                    ]

            class RuleConnection:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def cursor(self):
                    return RuleCursor()

                def commit(self):
                    return None

            @contextmanager
            def rule_connection():
                yield RuleConnection()

            commands = app.dependency_overrides[get_default_control_commands]()
            with patch("app.services.telemetry_store.get_connection", rule_connection), patch(
                "app.api.solution_delivery.get_default_entity_instance_registry",
                return_value=app.state.entity_instance_registry,
            ), patch(
                "app.api.solution_delivery.get_default_entity_instance_runtime",
                return_value=app.state.entity_instance_runtime,
            ), patch(
                "app.api.solution_delivery.get_default_automated_control_commands",
                return_value=AutomatedControlCommands(commands),
            ):
                first_tick = run_rule_tick()
                replay_tick = run_rule_tick()

            self.assertEqual({"evaluated": 1, "alarms": 0, "controls": 1, "errors": 0}, first_tick)
            self.assertEqual({"evaluated": 1, "alarms": 0, "controls": 1, "errors": 0}, replay_tick)
            command_id = dispatcher.requests[0].command_id
            first_read = await client._client.get(
                f"/api/v1/control-commands/{command_id}",
                headers={"Authorization": await client._bearer("operator")},
            )
            await self.publish(client, Readback=20.05)
            confirmed = await client._client.post(
                f"/api/v1/control-commands/{command_id}/reconcile",
                headers={"Authorization": await client._bearer("operator")},
            )

        self.assertEqual(1, len(dispatcher.requests))
        self.assertEqual(200, first_read.status_code, first_read.text)
        self.assertEqual("rule", first_read.json()["source_type"])
        self.assertEqual(f"rule:{rule_id}", first_read.json()["actor"])
        self.assertEqual(str(rule_id), first_read.json()["origin_evidence"]["subject"]["id"])
        self.assertEqual(4, first_read.json()["origin_evidence"]["subject"]["version"])
        self.assertEqual("setpoint-from-ready", first_read.json()["origin_evidence"]["action_key"])
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual("readback_confirmed", confirmed.json()["status"])

    async def test_rule_api_rejects_legacy_physical_control_addresses_before_write(self) -> None:
        app, _dispatcher = self.build_app()
        async with AuthenticatedDeliveryClient(app) as client:
            legacy_neuron = await client.post(
                "/api/v1/rules",
                json={
                    "name": "Legacy physical action",
                    "rule_type": "control",
                    "jdm_content": {
                        "_config": {
                            "actions": [{
                                "type": "neuron_write",
                                "node": "any-node",
                                "group": "any-group",
                                "tag": "any-tag",
                                "value": 1,
                            }]
                        }
                    },
                },
            )
            legacy_mqtt = await client.post(
                "/api/v1/rules",
                json={
                    "name": "Legacy MQTT action",
                    "rule_type": "control",
                    "jdm_content": {
                        "_config": {
                            "actions": [{
                                "type": "control",
                                "command": {"topic": "arbitrary/unsafe", "payload": {"value": 1}},
                                "value": 1,
                            }]
                        }
                    },
                },
            )

            missing_action_id = await client.post(
                "/api/v1/rules",
                json={
                    "name": "Unstable automatic action",
                    "rule_type": "control",
                    "jdm_content": {
                        "_config": {
                            "sourceEntityInstanceIds": ["90000000-0000-0000-0000-000000000010"],
                            "actions": [{
                                "type": "control",
                                "entity_instance_id": "90000000-0000-0000-0000-000000000011",
                                "value": 1,
                            }],
                        },
                    },
                },
            )
            missing_inputs = await client.post(
                "/api/v1/rules",
                json={
                    "name": "Automatic action without input evidence",
                    "rule_type": "control",
                    "jdm_content": {
                        "_config": {
                            "actions": [{
                                "id": "stable-action",
                                "type": "control",
                                "entity_instance_id": "90000000-0000-0000-0000-000000000011",
                                "value": 1,
                            }],
                        },
                    },
                },
            )

        self.assertEqual(409, legacy_neuron.status_code, legacy_neuron.text)
        self.assertEqual("RULE_CONTROL_LEGACY_FORBIDDEN", legacy_neuron.json()["detail"]["code"])
        self.assertEqual(409, legacy_mqtt.status_code, legacy_mqtt.text)
        self.assertEqual("RULE_CONTROL_LEGACY_FORBIDDEN", legacy_mqtt.json()["detail"]["code"])
        self.assertEqual(409, missing_action_id.status_code, missing_action_id.text)
        self.assertEqual("RULE_CONTROL_ACTION_INVALID", missing_action_id.json()["detail"]["code"])
        self.assertEqual(409, missing_inputs.status_code, missing_inputs.text)
        self.assertEqual("RULE_CONTROL_INPUTS_REQUIRED", missing_inputs.json()["detail"]["code"])

    def test_gorules_control_outputs_cannot_define_runtime_control_targets(self) -> None:
        from app.services.gorules_adapter import _extract_actions

        actions = _extract_actions(
            {
                "command": {
                    "node": "arbitrary-node",
                    "group": "arbitrary-group",
                    "tag": "arbitrary-tag",
                    "value": 1,
                },
                "command.entity_instance_id": "90000000-0000-0000-0000-000000000001",
                "command.value": 20.0,
            },
            {},
        )

        self.assertEqual([], actions)

    def test_every_control_route_declares_bearer_and_control_capability(self) -> None:
        from app.main import create_app

        schema = create_app().openapi()
        expected = {
            ("post", "/api/v1/entity-instances/{entity_instance_id}/control-confirmations"),
            ("post", "/api/v1/entity-instances/{entity_instance_id}/control-commands"),
            ("get", "/api/v1/control-commands/{command_id}"),
            ("post", "/api/v1/control-commands/{command_id}/reconcile"),
            ("post", "/api/v1/neuron/write"),
            ("post", "/api/v1/devices/{node_id}/rpc"),
        }
        for method, path in expected:
            with self.subTest(method=method, path=path):
                operation = schema["paths"][path][method]
                self.assertEqual(operation.get("x-zizu-capability"), "control.write")
                self.assertEqual(operation.get("security"), [{"HTTPBearer": []}])


if __name__ == "__main__":
    unittest.main()
