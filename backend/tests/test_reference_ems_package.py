"""The public PV/storage/charging reference package must remain importable."""
from __future__ import annotations

import importlib.util
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from uuid import UUID

from app.services.solution_delivery import InMemoryDeliveryRepository, SolutionDelivery
from tests.test_delivery_public_api import (
    AuthenticatedDeliveryClient,
    CURRENT_PLATFORM_VERSION,
)
from tests.test_control_command_public_api import ControlCommandPublicApiTest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_reference_delivery.py"
SPEC = importlib.util.spec_from_file_location("build_reference_delivery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ReferenceEmsPackageTest(unittest.IsolatedAsyncioTestCase):
    def test_public_reference_package_is_reproducible_and_importable(self) -> None:
        first = builder.build_archive()
        second = builder.build_archive()
        self.assertEqual(first, second)
        imported = SolutionDelivery(
            InMemoryDeliveryRepository(), platform_version="0.4.77"
        ).import_package(first, "user:test-engineer")
        self.assertEqual(imported.package_id, "org.zizu.pv-storage-charging-ems")
        self.assertEqual(imported.version, "1.0.0")
        self.assertEqual(len(imported.manifest["_entity_slots"]), 5)
        self.assertEqual(len(imported.manifest["_alarm_assets"]), 3)
        self.assertEqual(len(imported.manifest["_policy_assets"]), 1)
        self.assertEqual(
            set(imported.acceptance_ids),
            {
                "acceptance.platform-liveness", "acceptance.neuron-gateway", "acceptance.pcs", "acceptance.bms",
                "acceptance.pv", "acceptance.evse", "acceptance.meter", "acceptance.meter-history",
                "acceptance.operation-audit", "acceptance.operator-configuration-denied", "acceptance.manual-pcs-setpoint",
                "acceptance.grid-import-lifecycle", "acceptance.policy-grid-import-cap",
                "acceptance.release-lock", "acceptance.pcs-data-trunk",
            },
        )

    async def test_reference_package_completes_the_public_ems_delivery_trial(self) -> None:
        """The published package, not a private fixture, drives every delivery seam."""
        from app.services.entity_instance_registry import (
            InMemoryEntityInstanceRepository,
            SourceDescriptor,
        )
        from app.services.entity_instance_runtime import (
            InMemoryObservationCatalog,
            SourceObservation,
        )
        from app.services.data_trunk import (
            DataTrunk,
            InMemoryDataTrunkRepository,
            TagMetadata,
        )
        from app.services.point_processing import (
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PointProcessingDelivery,
            PointProcessingSource,
        )
        from app.services.solution_point_processings import (
            point_processing_assets,
            point_processing_revision_id,
        )

        release_lock = {"status": "missing"}
        sources = (
            SourceDescriptor(UUID("70000000-0000-0000-0000-000000000001"), "PCS-01", "PCS-01", "ActivePower", "FLOAT", "kW", "R", True),
            SourceDescriptor(UUID("70000000-0000-0000-0000-000000000002"), "PCS-01", "PCS-01", "ActivePowerSetpoint", "FLOAT", "kW", "RW", True),
            SourceDescriptor(UUID("70000000-0000-0000-0000-000000000003"), "PCS-01", "PCS-01", "ActivePowerReadback", "FLOAT", "kW", "R", True),
            SourceDescriptor(UUID("70000000-0000-0000-0000-000000000004"), "PCS-01", "PCS-01", "BmsReady", "BOOL", None, "R", True),
            SourceDescriptor(UUID("70000000-0000-0000-0000-000000000005"), "BMS-01", "BMS-01", "StateOfCharge", "FLOAT", "%", "R", True),
            SourceDescriptor(UUID("70000000-0000-0000-0000-000000000006"), "PV-01", "PV-01", "ActivePower", "FLOAT", "kW", "R", True),
            SourceDescriptor(UUID("70000000-0000-0000-0000-000000000007"), "EVSE-01", "EVSE-01", "ActivePower", "FLOAT", "kW", "R", True),
            SourceDescriptor(UUID("70000000-0000-0000-0000-000000000008"), "METER-01", "METER-01", "ActivePower", "FLOAT", "kW", "R", True),
        )
        pcs_node_id = UUID("70000000-0000-0000-0001-000000000001")
        point_sources = (
            PointProcessingSource(UUID("70000000-0000-0000-0000-000000000009"), "l0", pcs_node_id, "ActivePowerRaw", "FLOAT", "W", True),
            PointProcessingSource(UUID("70000000-0000-0000-0000-000000000010"), "l0", pcs_node_id, "RunningState", "STRING", None, True),
            PointProcessingSource(UUID("70000000-0000-0000-0000-000000000011"), "l0", pcs_node_id, "FaultCodeText", "STRING", None, True),
        )
        parsed_package = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        ).import_package(builder.build_archive(), "user:test-engineer")
        assets = point_processing_assets(parsed_package)
        templates = {point_processing_revision_id(asset): asset for asset in assets}
        brand_a_revision = next(
            revision_id
            for revision_id, asset in templates.items()
            if asset.asset_id == "pcs.brand-a"
        )
        entity_repository = InMemoryEntityInstanceRepository()
        point_repository = InMemoryPointProcessingRepository(
            on_applied=lambda application: entity_repository.activate_point_processing_outputs(
                application.revision_id,
                application.site_configuration_version,
                application.output_entity_instance_ids,
            )
        )
        point_catalog = InMemoryPointProcessingCatalog(
            templates=templates,
            sources=point_sources,
            node_source_keys={pcs_node_id: "PCS-01"},
        )
        point_processings = PointProcessingDelivery(point_repository, point_catalog)
        observations = InMemoryObservationCatalog()

        def publish_l2(committed) -> None:
            for item in committed:
                observations.publish(
                    SourceObservation(
                        tag_id=item.entity_instance_id,
                        observed_at=item.observed_at,
                        value=item.value.value,
                        quality=int(item.quality),
                        event_id=item.event_id,
                        reason=item.reason,
                        received_at=item.received_at,
                        calculated_at=item.calculated_at,
                        processing_revision_id=item.processing_revision_id,
                        site_configuration_version=(
                            item.site_configuration_version
                        ),
                        source_digest=item.source_digest,
                    )
                )

        data_trunk = DataTrunk(
            InMemoryDataTrunkRepository(
                installed_provider=lambda: point_repository.installed_processings(
                    point_catalog
                ),
                site_configuration_version=(
                    point_repository.site_configuration_version
                ),
                on_l2_committed=publish_l2,
                clock=lambda: datetime.now(timezone.utc),
            )
        )
        point_tag_catalog = {
            item.stable_source_key: TagMetadata(
                node_id=pcs_node_id,
                tag_id=item.source_id,
                stable_source_key=item.stable_source_key,
                data_type=item.data_type,
                unit=item.unit,
            )
            for item in point_sources
        }
        app, dispatcher = ControlCommandPublicApiTest.build_app(
            high_risk=True,
            sources=sources,
            release_lock_reader=lambda: release_lock,
            point_processings=point_processings,
            entity_repository=entity_repository,
            observations=observations,
            data_trunk=data_trunk,
            point_tag_catalog=point_tag_catalog,
        )
        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={"archive": ("pv-storage-charging-ems.zizu.zip", builder.build_archive(), "application/zip")},
            )
            self.assertEqual(201, imported.status_code, imported.text)
            plan = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instances": [{"instance_key": "PCS-01", "device_key": "PCS-01"}],
                        "bms.instances": [{"instance_key": "BMS-01", "device_key": "BMS-01"}],
                        "pv.instances": [{"instance_key": "PV-01", "device_key": "PV-01"}],
                        "evse.instances": [{"instance_key": "EVSE-01", "device_key": "EVSE-01"}],
                        "meter.instance_key": "METER-01",
                        "meter.device_key": "METER-01",
                    },
                    "secret_references": {"gateway.credentials": "secret://reference/gateway"},
                    "point_processings": [
                        {
                            "node_id": str(pcs_node_id),
                            "template_revision_id": str(brand_a_revision),
                        }
                    ],
                },
            )
            self.assertEqual(201, plan.status_code, plan.text)
            installed = await client.post(
                f"/api/v1/install-plans/{plan.json()['id']}/apply",
                json={"plan_digest": plan.json()["digest"]},
                headers={"Idempotency-Key": "reference-ems-install"},
            )
            self.assertEqual(201, installed.status_code, installed.text)
            entity_ids = {
                item["definition_id"]: item["entity_instance_id"]
                for item in plan.json()["items"]
                if item["kind"] == "entity_binding"
            }

            async def publish(
                node: str,
                values: dict[str, object],
                observed_at: datetime,
            ) -> dict:
                response = await client.post(
                    "/protocol-simulator/neuron",
                    json={
                        "message": {
                            "node": node,
                            "timestamp": round(observed_at.timestamp() * 1000),
                            "values": values,
                        }
                    },
                )
                self.assertEqual(200, response.status_code, response.text)
                return response.json()

            initial_at = datetime.now(timezone.utc)
            await publish(
                "PCS-01",
                {
                    "ActivePowerRaw": 20_000.0,
                    "RunningState": "2",
                    "FaultCodeText": "E30",
                    "ActivePowerSetpoint": 0.0,
                    "ActivePowerReadback": 0.0,
                    "BmsReady": True,
                },
                initial_at,
            )
            await publish("BMS-01", {"StateOfCharge": 65.0}, initial_at)
            await publish("PV-01", {"ActivePower": 80.0}, initial_at)
            await publish("EVSE-01", {"ActivePower": 25.0}, initial_at)
            await publish("METER-01", {"ActivePower": 100.0}, initial_at)
            await publish("METER-01", {"ActivePower": 101.0}, initial_at + timedelta(seconds=1))

            workbench = await client.get("/api/v1/ems-workbench")
            self.assertEqual(200, workbench.status_code, workbench.text)
            self.assertEqual("workbench.ems", workbench.json()["workbench_id"])

            operator_headers = {
                "Authorization": await client._bearer("operator"),
                "Idempotency-Key": "reference-manual-confirmation",
            }
            confirmation = await client._client.post(
                f"/api/v1/entity-instances/{entity_ids['pcs.setpoint']}/control-confirmations",
                headers=operator_headers,
                json={"value": 5.0},
            )
            self.assertEqual(201, confirmation.status_code, confirmation.text)
            manual = await client._client.post(
                f"/api/v1/entity-instances/{entity_ids['pcs.setpoint']}/control-commands",
                headers={
                    "Authorization": await client._bearer("operator"),
                    "Idempotency-Key": "reference-manual-command",
                },
                json={"value": 5.0, "confirmation_id": confirmation.json()["id"]},
            )
            self.assertEqual(201, manual.status_code, manual.text)
            await publish("PCS-01", {"ActivePowerReadback": 5.0}, datetime.now(timezone.utc))
            manual_done = await client._client.post(
                f"/api/v1/control-commands/{manual.json()['id']}/reconcile",
                headers={"Authorization": await client._bearer("operator")},
            )
            self.assertEqual("readback_confirmed", manual_done.json()["status"])

            denied = await client._client.post(
                "/api/v1/ems-policies/policy.grid-import-cap/enable",
                headers={"Authorization": await client._bearer("operator")},
            )
            self.assertEqual(403, denied.status_code, denied.text)
            self.assertEqual("PERMISSION_DENIED", denied.json()["detail"]["code"])
            denied_audit_id = denied.headers.get("X-ZiZu-Audit-Event-ID")
            self.assertIsNotNone(denied_audit_id)

            # The configured 5-second device cooldown is intentionally observed,
            # rather than bypassed by a test-only clock.
            await asyncio.sleep(5.1)
            alarm_started_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            triggered = await publish("METER-01", {"ActivePower": 550.0}, alarm_started_at)
            # One input fan-outs to three independently declared severities.  Their
            # execution order is not part of the public protocol: at 550 kW the
            # WARNING and MAJOR definitions enter pending while CRITICAL remains
            # normal.  Do not accidentally make the catalogue's sort order a
            # delivery contract.
            self.assertCountEqual(
                [outcome["code"] for outcome in triggered["alarm_outcomes"]],
                ["ALARM_TRIGGER_PENDING", "ALARM_TRIGGER_PENDING", "ALARM_NORMAL"],
            )
            activated = await publish(
                "METER-01", {"ActivePower": 550.0}, alarm_started_at + timedelta(seconds=11)
            )
            self.assertCountEqual(
                [outcome["code"] for outcome in activated["alarm_outcomes"]],
                ["ALARM_ACTIVATED", "ALARM_ACTIVATED", "ALARM_NORMAL"],
            )
            events = await client.get("/api/v1/alarm-events?state=open")
            self.assertEqual(2, events.json()["total"], events.text)
            self.assertEqual(1, events.json()["summary"]["by_severity"]["WARNING"])
            self.assertEqual(1, events.json()["summary"]["by_severity"]["MAJOR"])
            self.assertEqual(0, events.json()["summary"]["by_severity"]["CRITICAL"])
            event_id = next(
                item["id"] for item in events.json()["items"] if item["severity"] == "MAJOR"
            )
            acknowledged = await client._client.post(
                f"/api/v1/alarm-events/{event_id}/acknowledgements",
                headers={"Authorization": await client._bearer("operator")},
                json={"note": "reference trial"},
            )
            self.assertEqual("active_acknowledged", acknowledged.json()["state"])

            engineer_headers = {"Authorization": await client._bearer("engineer")}
            enabled = await client._client.post(
                "/api/v1/ems-policies/policy.grid-import-cap/enable", headers=engineer_headers
            )
            self.assertEqual(200, enabled.status_code, enabled.text)
            policy = await client._client.post(
                "/api/v1/ems-policies/policy.grid-import-cap/evaluate", headers=engineer_headers
            )
            self.assertEqual(200, policy.status_code, policy.text)
            policy_command = policy.json()["command"]
            self.assertEqual(10, policy_command["expected_value"])
            await publish(
                "PCS-01", {"ActivePowerReadback": 10.0}, alarm_started_at + timedelta(seconds=12)
            )
            policy_done = await client._client.post(
                f"/api/v1/control-commands/{policy_command['id']}/reconcile", headers=engineer_headers
            )
            self.assertEqual("readback_confirmed", policy_done.json()["status"])
            self.assertEqual(2, len(dispatcher.requests))

            recovery_pending = await publish(
                "METER-01", {"ActivePower": 200.0}, alarm_started_at + timedelta(seconds=13)
            )
            self.assertCountEqual(
                [outcome["code"] for outcome in recovery_pending["alarm_outcomes"]],
                ["ALARM_RECOVERY_PENDING", "ALARM_RECOVERY_PENDING", "ALARM_NORMAL"],
            )
            recovered = await publish(
                "METER-01", {"ActivePower": 200.0}, alarm_started_at + timedelta(seconds=24)
            )
            self.assertCountEqual(
                [outcome["code"] for outcome in recovered["alarm_outcomes"]],
                ["ALARM_RECOVERED", "ALARM_RECOVERED", "ALARM_NORMAL"],
            )

            release_lock.update(
                {
                    "status": "locked",
                    "id": "70000000-0000-0000-0000-000000000099",
                    "platform_version": CURRENT_PLATFORM_VERSION,
                    "architecture": "linux/amd64",
                    "site_configuration_version": installed.json()["site_configuration_version"],
                    "package": {
                        "id": imported.json()["package_id"],
                        "version": imported.json()["version"],
                        "digest": imported.json()["digest"],
                    },
                }
            )
            report = await client.post(
                f"/api/v1/solution-installations/{installed.json()['id']}/acceptance-runs",
                json={
                    "manual_commands": {"acceptance.manual-pcs-setpoint": manual_done.json()["id"]},
                    "policy_commands": {"acceptance.policy-grid-import-cap": policy_command["id"]},
                    "authorization_denials": {
                        "acceptance.operator-configuration-denied": denied_audit_id
                    },
                },
                headers={"Idempotency-Key": "reference-ems-acceptance"},
            )
            missing_manual = await client.post(
                f"/api/v1/solution-installations/{installed.json()['id']}/acceptance-runs",
                json={"policy_commands": {"acceptance.policy-grid-import-cap": policy_command["id"]}},
                headers={"Idempotency-Key": "reference-ems-acceptance-missing-manual"},
            )
            from app.services.gateway_readiness import GatewayReadinessResult

            class UnavailableGateway:
                async def check(self) -> GatewayReadinessResult:
                    return GatewayReadinessResult("neuron", "unavailable", "GATEWAY_UNAVAILABLE")

            app.state.solution_delivery.set_gateway_readiness(UnavailableGateway())
            gateway_down = await client.post(
                f"/api/v1/solution-installations/{installed.json()['id']}/acceptance-runs",
                json={
                    "manual_commands": {"acceptance.manual-pcs-setpoint": manual_done.json()["id"]},
                    "policy_commands": {"acceptance.policy-grid-import-cap": policy_command["id"]},
                },
                headers={"Idempotency-Key": "reference-ems-acceptance-gateway-down"},
            )

        self.assertEqual(201, report.status_code, report.text)
        self.assertEqual("passed", report.json()["status"], report.text)
        self.assertTrue(all(item["status"] == "passed" for item in report.json()["items"]))
        operation_audit = next(
            item for item in report.json()["items"]
            if item["acceptance_id"] == "acceptance.operation-audit"
        )
        self.assertEqual(
            {
                "installation",
                "manual_control",
                "policy_control",
                "alarm_acknowledgement",
                "authorization_denial",
            },
            set(operation_audit["evidence"]["required_evidence"]),
        )
        self.assertTrue(all(operation_audit["evidence"]["coverage"].values()))
        alarm_item = next(
            item for item in report.json()["items"]
            if item["acceptance_id"] == "acceptance.grid-import-lifecycle"
        )
        self.assertTrue(
            all(event["acknowledgement_audit_event_ids"] for event in alarm_item["evidence"]["events"])
        )
        self.assertEqual(201, missing_manual.status_code, missing_manual.text)
        self.assertEqual("failed", missing_manual.json()["status"])
        manual_item = next(
            item for item in missing_manual.json()["items"]
            if item["acceptance_id"] == "acceptance.manual-pcs-setpoint"
        )
        self.assertEqual("MANUAL_CONTROL_COMMAND_REQUIRED", manual_item["code"])
        denial_item = next(
            item for item in missing_manual.json()["items"]
            if item["acceptance_id"] == "acceptance.operator-configuration-denied"
        )
        self.assertEqual("AUTHORIZATION_DENIAL_EVIDENCE_REQUIRED", denial_item["code"])
        self.assertEqual(201, gateway_down.status_code, gateway_down.text)
        self.assertEqual("failed", gateway_down.json()["status"])
        gateway_item = next(
            item for item in gateway_down.json()["items"]
            if item["acceptance_id"] == "acceptance.neuron-gateway"
        )
        self.assertEqual("GATEWAY_UNAVAILABLE", gateway_item["code"])


if __name__ == "__main__":
    unittest.main()
