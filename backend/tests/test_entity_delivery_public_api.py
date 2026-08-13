from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
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
BACKUP_TAG_ID = UUID("20000000-0000-0000-0000-000000000003")


def build_entity_package(
    *,
    package_version: str = "1.0.0",
    multiple_devices: bool = False,
    manual_failover: bool = False,
) -> bytes:
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
        (
            "schemaVersion: zizu.entity-instance-slot/v1alpha1\n"
            "id: slot.pcs-primary\n"
            "kind: entity_instance_slot\n"
            "deviceCategory: pcs\n"
            "instancesParameter: pcs.instances\n"
            "displayName: PCS\n"
            "freshness: 30s\n"
            "requiredEntities:\n"
            "  - definition: pcs.activePower\n"
            "    matcher:\n"
            "      id: matcher.pcs-active-power\n"
            "      tagName: ActivePower\n"
            + ("      failoverPolicy: manual\n" if manual_failover else "")
        )
        if multiple_devices
        else (
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
        )
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
        + (
            "  - id: pcs.instances\n"
            "    type: device_instances\n"
            "    required: true\n"
            "    description: Stable PCS instance and source keys\n"
            "    minimumItems: 1\n"
            "    maximumItems: 16\n"
            if multiple_devices
            else (
                "  - id: pcs.instance_key\n"
                "    type: string\n"
                "    required: true\n"
                "    description: Stable site-local PCS key\n"
                "  - id: pcs.device_key\n"
                "    type: string\n"
                "    required: true\n"
                "    description: Source catalog device key\n"
            )
        )
        +
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


def build_control_entity_package(*, high_risk: bool = False) -> bytes:
    """一个可配置控制、回读和联锁的最小 EMS 解决方案资产。"""
    acceptance = (
        "schemaVersion: zizu.acceptance/v1alpha1\n"
        "id: acceptance.platform-liveness\n"
        "kind: platform_liveness\n"
        "required: true\n"
        "timeout: 5s\n"
    ).encode()
    entities = {
        "pcs.setpoint": (
            "schemaVersion: zizu.entity-definition/v1alpha1\n"
            "id: pcs.setpoint\nkind: entity_definition\ndisplayName: Setpoint\n"
            "deviceCategory: pcs\ndataType: FLOAT\nunit: kW\ndirection: RW\n"
            "control:\n  minimum: -100\n  maximum: 100\n  cooldown: 5s\n"
            "  readback:\n    definition: pcs.readback\n    tolerance: 0.1\n    timeout: 10s\n"
            "  interlocks:\n    - definition: bms.ready\n      equals: true\n"
            f"  highRisk: {'true' if high_risk else 'false'}\n"
        ).encode(),
        "pcs.readback": (
            "schemaVersion: zizu.entity-definition/v1alpha1\n"
            "id: pcs.readback\nkind: entity_definition\ndisplayName: Readback\n"
            "deviceCategory: pcs\ndataType: FLOAT\nunit: kW\ndirection: R\n"
        ).encode(),
        "bms.ready": (
            "schemaVersion: zizu.entity-definition/v1alpha1\n"
            "id: bms.ready\nkind: entity_definition\ndisplayName: BMS ready\n"
            "deviceCategory: pcs\ndataType: BOOL\nunit: null\ndirection: R\n"
        ).encode(),
    }
    slot = (
        "schemaVersion: zizu.entity-instance-slot/v1alpha1\n"
        "id: slot.pcs-primary\nkind: entity_instance_slot\ndeviceCategory: pcs\n"
        "count: 1\ninstanceKeyParameter: pcs.instance_key\ndisplayName: Primary PCS\n"
        "freshness: 30s\nrequiredEntities:\n"
        "  - definition: pcs.setpoint\n    matcher:\n      id: matcher.setpoint\n"
        "      deviceKeyParameter: pcs.device_key\n      tagName: Setpoint\n"
        "  - definition: pcs.readback\n    matcher:\n      id: matcher.readback\n"
        "      deviceKeyParameter: pcs.device_key\n      tagName: Readback\n"
        "  - definition: bms.ready\n    matcher:\n      id: matcher.ready\n"
        "      deviceKeyParameter: pcs.device_key\n      tagName: Ready\n"
    ).encode()
    declarations = [
        ("acceptance.platform-liveness", "acceptance", "acceptance/liveness.yaml", acceptance),
        ("slot.pcs-primary", "entity_instance_slot", "entities/pcs-primary.yaml", slot),
        *(
            (definition_id, "entity_definition", f"entities/{definition_id.replace('.', '-')}.yaml", content)
            for definition_id, content in entities.items()
        ),
    ]
    manifest = (
        "schemaVersion: zizu.solution/v1alpha1\nid: org.zizu.control-pcs\nversion: 1.0.0\n"
        "displayName: Controllable PCS EMS\nplatform:\n  version: \">=0.4.77,<0.5.0\"\n"
        "parameters:\n"
        "  - id: pcs.instance_key\n    type: string\n    required: true\n"
        "    description: Stable PCS key\n"
        "  - id: pcs.device_key\n    type: string\n    required: true\n"
        "    description: Source catalog device key\nassets:\n"
        + "".join(
            f"  - id: {item_id}\n    kind: {kind}\n    path: {path}\n"
            f"    sha256: \"{hashlib.sha256(content).hexdigest()}\"\n"
            for item_id, kind, path, content in declarations
        )
        + "acceptance:\n  - acceptance.platform-liveness\n"
    ).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("solution.yaml", manifest)
        for _item_id, _kind, path, content in declarations:
            package.writestr(path, content)
    return archive.getvalue()


class EntityDeliveryPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def build_app(*, sources: tuple, legacy_entities: tuple = ()) -> FastAPI:
        from app.api.entity_instances import (
            get_entity_instance_catalog,
            get_entity_instance_failover,
            get_entity_instance_runtime,
            router as entity_instance_router,
        )
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.api.health import router as health_router
        from app.api.rules import router as rules_router
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
        from app.services.entity_instance_catalog import EntityInstanceCatalog
        from app.services.entity_instance_failover import EntityFailoverPolicy
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        delivery_repository = InMemoryDeliveryRepository()
        source_catalog = InMemorySourceCatalog(sources)
        entity_repository = InMemoryEntityInstanceRepository(legacy_entities=legacy_entities)
        registry = EntityInstanceRegistry(
            entity_repository,
            source_catalog,
            delivery_repository.site_configuration_version,
        )
        observations = InMemoryObservationCatalog()
        simulator = InMemoryNeuronProtocolSimulator(source_catalog, observations)
        runtime = EntityInstanceRuntime(registry, observations)
        catalog = EntityInstanceCatalog(entity_repository)
        failover = EntityFailoverPolicy(entity_repository)
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
        app.include_router(rules_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            delivery_repository,
            platform_version="0.4.77",
            public_api_probe=AsgiPublicApiProbe(app),
            entity_instance_registry=registry,
            entity_instance_runtime=runtime,
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        app.dependency_overrides[get_entity_instance_runtime] = lambda: runtime
        app.dependency_overrides[get_entity_instance_catalog] = lambda: catalog
        app.dependency_overrides[get_entity_instance_failover] = lambda: failover
        app.state.entity_instance_registry = registry
        app.state.entity_instance_repository = entity_repository
        app.state.entity_instance_runtime = runtime
        return app

    @staticmethod
    def source(tag_id: UUID = TAG_ID, *, device_key: str = "PCS-01"):
        from app.services.entity_instance_registry import SourceDescriptor

        return SourceDescriptor(
            tag_id=tag_id,
            device_key=device_key,
            device_name=device_key,
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

    async def test_two_same_definition_devices_have_stable_non_crossed_instances(self) -> None:
        legacy_unique_id = UUID("30000000-0000-0000-0000-000000000001")
        legacy_ambiguous_id = UUID("30000000-0000-0000-0000-000000000002")
        legacy_missing_id = UUID("30000000-0000-0000-0000-000000000003")
        app = self.build_app(
            sources=(
                self.source(TAG_ID, device_key="PCS-01"),
                self.source(OTHER_TAG_ID, device_key="PCS-02"),
            ),
            legacy_entities=(
                (legacy_unique_id, "legacy.pcs-a-power", (TAG_ID,)),
                (
                    legacy_ambiguous_id,
                    "legacy.global-pcs-power",
                    (TAG_ID, OTHER_TAG_ID),
                ),
                (
                    legacy_missing_id,
                    "legacy.retired-meter-power",
                    (UUID("20000000-0000-0000-0000-000000000099"),),
                ),
            ),
        )
        parameters = {
            "pcs.instances": [
                {
                    "instance_key": "PCS-01",
                    "device_key": "PCS-01",
                    "display_name": "PCS A",
                },
                {
                    "instance_key": "PCS-02",
                    "device_key": "PCS-02",
                    "display_name": "PCS B",
                },
            ]
        }
        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "multi-pcs.zizu.zip",
                        build_entity_package(multiple_devices=True),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(201, imported.status_code, imported.text)
            planned = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={"parameters": parameters},
            )
            self.assertEqual(201, planned.status_code, planned.text)
            entity_items = [
                item for item in planned.json()["items"] if item["kind"] == "entity_binding"
            ]
            self.assertEqual(2, len(entity_items))
            self.assertEqual(
                {"PCS-01": str(TAG_ID), "PCS-02": str(OTHER_TAG_ID)},
                {item["instance_key"]: item["selected_tag_id"] for item in entity_items},
            )

            installed = await client.post(
                f"/api/v1/install-plans/{planned.json()['id']}/apply",
                json={"plan_digest": planned.json()["digest"]},
                headers={"Idempotency-Key": "install-multi-pcs"},
            )
            self.assertEqual(201, installed.status_code, installed.text)
            ids_by_key = {
                item["instance_key"]: item["entity_instance_id"] for item in entity_items
            }
            self.assertEqual(2, len(set(ids_by_key.values())))
            catalog = await client.get("/api/v1/entity-instances")
            self.assertEqual(200, catalog.status_code, catalog.text)
            self.assertEqual(2, catalog.json()["total"])
            self.assertEqual(
                {"PCS-01", "PCS-02"},
                {item["instance_key"] for item in catalog.json()["items"]},
            )
            migration = await client.get(
                "/api/v1/entity-instances/legacy-migration-preview"
            )
            self.assertEqual(200, migration.status_code, migration.text)
            self.assertEqual(
                {"unique": 1, "missing": 1, "ambiguous": 1},
                migration.json()["counts"],
            )
            preview_by_id = {
                item["legacy_entity_id"]: item for item in migration.json()["items"]
            }
            self.assertEqual(
                [ids_by_key["PCS-01"]],
                preview_by_id[str(legacy_unique_id)]["candidate_entity_instance_ids"],
            )
            self.assertEqual(
                {ids_by_key["PCS-01"], ids_by_key["PCS-02"]},
                set(
                    preview_by_id[str(legacy_ambiguous_id)][
                        "candidate_entity_instance_ids"
                    ]
                ),
            )
            self.assertEqual(
                [],
                preview_by_id[str(legacy_missing_id)]["candidate_entity_instance_ids"],
            )

            legacy_rule = await client.post(
                "/api/v1/rules",
                json={
                    "name": "legacy ambiguous input",
                    "rule_type": "alarm",
                    "jdm_content": {
                        "_config": {"sourceEntityIds": [str(legacy_ambiguous_id)]}
                    },
                },
            )
            self.assertEqual(409, legacy_rule.status_code, legacy_rule.text)
            self.assertEqual(
                "ENTITY_REFERENCE_LEGACY_FORBIDDEN",
                legacy_rule.json()["detail"]["code"],
            )

            rule_id = UUID("40000000-0000-0000-0000-000000000001")

            class RuleCursor:
                description = [
                    (name,) for name in (
                        "id", "name", "rule_type", "jdm_content", "version",
                        "enabled", "created_at", "updated_at",
                    )
                ]

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def execute(self, query, params):
                    self.params = params

                def fetchone(self):
                    now = datetime.now(timezone.utc)
                    return (
                        rule_id,
                        "PCS A input",
                        "alarm",
                        json.dumps({
                            "_config": {
                                "sourceEntityInstanceIds": [ids_by_key["PCS-01"]],
                                "inputMappings": {"power": ids_by_key["PCS-01"]},
                            }
                        }),
                        1,
                        True,
                        now,
                        now,
                    )

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

            from unittest.mock import patch
            with patch(
                "app.services.telemetry_store.get_connection",
                rule_connection,
            ):
                stable_rule = await client.post(
                    "/api/v1/rules",
                    json={
                        "name": "PCS A input",
                        "rule_type": "alarm",
                        "jdm_content": {
                            "_config": {
                                "sourceEntityInstanceIds": [ids_by_key["PCS-01"]],
                                "inputMappings": {"power": ids_by_key["PCS-01"]},
                            }
                        },
                    },
                )
            self.assertEqual(200, stable_rule.status_code, stable_rule.text)
            self.assertEqual(str(rule_id), stable_rule.json()["id"])

            for device_key, value in (("PCS-01", 101.5), ("PCS-02", 202.5)):
                published = await client.post(
                    "/protocol-simulator/neuron",
                    json={
                        "message": {
                            "node": device_key,
                            "timestamp": round(datetime.now(timezone.utc).timestamp() * 1000),
                            "values": {"ActivePower": value},
                        }
                    },
                )
                self.assertEqual(1, published.json()["published"])
                realtime = await client.get(
                    f"/api/v1/entity-instances/{ids_by_key[device_key]}/realtime"
                )
                self.assertEqual(value, realtime.json()["value"])

            renamed_parameters = {
                "pcs.instances": [
                    {**parameters["pcs.instances"][0], "display_name": "East PCS"},
                    {**parameters["pcs.instances"][1], "display_name": "West PCS"},
                ]
            }
            renamed_plan = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={"parameters": renamed_parameters},
            )
            renamed = await client.post(
                f"/api/v1/install-plans/{renamed_plan.json()['id']}/apply",
                json={"plan_digest": renamed_plan.json()["digest"]},
                headers={"Idempotency-Key": "rename-multi-pcs"},
            )
            self.assertEqual(201, renamed.status_code, renamed.text)
            self.assertEqual(
                set(ids_by_key.values()),
                set(renamed.json()["entity_instance_ids"]),
            )

    async def test_manual_source_failover_is_explicit_and_audited(self) -> None:
        legacy_standby_id = UUID("30000000-0000-0000-0000-000000000004")
        app = self.build_app(
            sources=(
                self.source(TAG_ID, device_key="PCS-01"),
                self.source(BACKUP_TAG_ID, device_key="PCS-01-BACKUP"),
            ),
            legacy_entities=((legacy_standby_id, "legacy.standby", (BACKUP_TAG_ID,)),),
        )
        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "pcs-failover.zizu.zip",
                        build_entity_package(
                            multiple_devices=True,
                            manual_failover=True,
                        ),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(201, imported.status_code, imported.text)
            planned = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instances": [
                            {
                                "instance_key": "PCS-01",
                                "device_key": "PCS-01",
                                "standby_device_key": "PCS-01-BACKUP",
                            }
                        ]
                    }
                },
            )
            self.assertEqual(201, planned.status_code, planned.text)
            binding = next(
                item for item in planned.json()["items"]
                if item["kind"] == "entity_binding"
            )
            self.assertEqual("manual", binding["failover_policy"])
            self.assertEqual(str(BACKUP_TAG_ID), binding["standby_tag_id"])
            installed = await client.post(
                f"/api/v1/install-plans/{planned.json()['id']}/apply",
                json={"plan_digest": planned.json()["digest"]},
                headers={"Idempotency-Key": "install-manual-failover"},
            )
            entity_instance_id = installed.json()["entity_instance_ids"][0]

            published_backup = await client.post(
                "/protocol-simulator/neuron",
                json={
                    "message": {
                        "node": "PCS-01-BACKUP",
                        "timestamp": round(datetime.now(timezone.utc).timestamp() * 1000),
                        "values": {"ActivePower": 77.5},
                    }
                },
            )
            self.assertEqual(1, published_backup.json()["published"])
            before_switch = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime"
            )
            self.assertEqual(404, before_switch.status_code, before_switch.text)
            self.assertEqual("ENTITY_DATA_MISSING", before_switch.json()["detail"]["code"])

            switched = await client.post(
                f"/api/v1/entity-instances/{entity_instance_id}/source-failover",
                json={
                    "expected_current_role": "primary",
                    "target_role": "standby",
                    "reason": "Primary gateway maintenance",
                },
            )
            self.assertEqual(200, switched.status_code, switched.text)
            self.assertEqual("standby", switched.json()["current_role"])
            self.assertEqual(1, switched.json()["switch_count"])
            self.assertEqual("user:00000000-0000-0000-0000-000000000002", switched.json()["actor"])
            self.assertEqual("Primary gateway maintenance", switched.json()["reason"])
            after_switch = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime"
            )
            self.assertEqual(200, after_switch.status_code, after_switch.text)
            self.assertEqual(77.5, after_switch.json()["value"])
            preview = await client.get(
                "/api/v1/entity-instances/legacy-migration-preview"
            )
            standby_preview = next(
                item for item in preview.json()["items"]
                if item["legacy_entity_id"] == str(legacy_standby_id)
            )
            self.assertEqual("unique", standby_preview["classification"])
            self.assertEqual(
                [entity_instance_id],
                standby_preview["candidate_entity_instance_ids"],
            )

            renamed_plan = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instances": [
                            {
                                "instance_key": "PCS-01",
                                "device_key": "PCS-01",
                                "standby_device_key": "PCS-01-BACKUP",
                                "display_name": "Renamed PCS",
                            }
                        ]
                    }
                },
            )
            upgraded = await client.post(
                f"/api/v1/install-plans/{renamed_plan.json()['id']}/apply",
                json={"plan_digest": renamed_plan.json()["digest"]},
                headers={"Idempotency-Key": "upgrade-manual-failover"},
            )
            self.assertEqual(201, upgraded.status_code, upgraded.text)
            self.assertEqual([entity_instance_id], upgraded.json()["entity_instance_ids"])
            after_upgrade = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime"
            )
            self.assertEqual(77.5, after_upgrade.json()["value"])

            stale_retry = await client.post(
                f"/api/v1/entity-instances/{entity_instance_id}/source-failover",
                json={
                    "expected_current_role": "primary",
                    "target_role": "standby",
                    "reason": "Unsafe duplicate retry",
                },
            )
            self.assertEqual(409, stale_retry.status_code, stale_retry.text)
            self.assertEqual(
                "ENTITY_FAILOVER_STATE_CHANGED",
                stale_retry.json()["detail"]["code"],
            )

            status_response = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/source-failover"
            )
            self.assertEqual("standby", status_response.json()["current_role"])
            self.assertEqual(1, len(status_response.json()["audit"]), status_response.text)

            without_policy = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "pcs-no-failover-v2.zizu.zip",
                        build_entity_package(
                            package_version="2.0.0",
                            multiple_devices=True,
                        ),
                        "application/zip",
                    )
                },
            )
            removal_plan = await client.post(
                f"/api/v1/solution-packages/{without_policy.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instances": [
                            {"instance_key": "PCS-01", "device_key": "PCS-01"}
                        ]
                    }
                },
            )
            blocked_removal = await client.post(
                f"/api/v1/install-plans/{removal_plan.json()['id']}/apply",
                json={"plan_digest": removal_plan.json()["digest"]},
                headers={"Idempotency-Key": "remove-active-standby-policy"},
            )
            self.assertEqual(409, blocked_removal.status_code, blocked_removal.text)
            self.assertEqual(
                "ENTITY_FAILOVER_POLICY_CHANGE_REQUIRES_PRIMARY",
                blocked_removal.json()["detail"]["code"],
            )
            returned_primary = await client.post(
                f"/api/v1/entity-instances/{entity_instance_id}/source-failover",
                json={
                    "expected_current_role": "standby",
                    "target_role": "primary",
                    "reason": "Remove retired standby policy",
                },
            )
            self.assertEqual(200, returned_primary.status_code, returned_primary.text)
            removed = await client.post(
                f"/api/v1/install-plans/{removal_plan.json()['id']}/apply",
                json={"plan_digest": removal_plan.json()["digest"]},
                headers={"Idempotency-Key": "remove-primary-policy"},
            )
            self.assertEqual(201, removed.status_code, removed.text)
            missing_policy = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/source-failover"
            )
            self.assertEqual(404, missing_policy.status_code, missing_policy.text)
            self.assertEqual(
                "ENTITY_FAILOVER_NOT_CONFIGURED",
                missing_policy.json()["detail"]["code"],
            )

    def test_rule_instance_context_reuses_registry_resolution_and_runtime_read(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from app.services.rule_engine import _entity_instance_context

        entity_id = UUID("50000000-0000-0000-0000-000000000001")
        registry = Mock()
        registry.resolve.return_value = SimpleNamespace(
            tag_id=TAG_ID,
            device_instance_id=UUID("50000000-0000-0000-0000-000000000002"),
            entity_instance_id=entity_id,
        )
        runtime = Mock()
        runtime.read.return_value = SimpleNamespace(value=125.5)
        with patch(
            "app.api.solution_delivery.get_default_entity_instance_registry",
            return_value=registry,
        ), patch(
            "app.api.solution_delivery.get_default_entity_instance_runtime",
            return_value=runtime,
        ):
            context = _entity_instance_context({str(entity_id)})

        registry.resolve.assert_called_once_with(entity_id)
        runtime.read.assert_called_once_with(entity_id)
        self.assertEqual(125.5, context[str(entity_id)]["value"])

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
