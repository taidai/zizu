from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    build_minimal_package,
)


TAG_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_TAG_ID = UUID("20000000-0000-0000-0000-000000000002")
BACKUP_TAG_ID = UUID("20000000-0000-0000-0000-000000000003")
GRID_TAG_ID = UUID("20000000-0000-0000-0000-000000000004")


def build_entity_package(
    *,
    package_version: str = "1.0.0",
    multiple_devices: bool = False,
    manual_failover: bool = False,
    workbench_definition: str | None = None,
    entity_direction: str = "R",
    history_acceptance: bool = False,
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
        f"direction: {entity_direction}\n"
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
    history = (
        "schemaVersion: zizu.acceptance/v1alpha1\n"
        "id: acceptance.pcs-history\n"
        "kind: history_readiness\n"
        "required: true\n"
        "slot: slot.pcs-primary\n"
        "definition: pcs.activePower\n"
        "range: 24h\n"
        "minimumSamples: 2\n"
        "timeout: 5s\n"
    ).encode() if history_acceptance else None
    acceptance_digest = hashlib.sha256(acceptance).hexdigest()
    definition_digest = hashlib.sha256(entity_definition).hexdigest()
    entity_digest = hashlib.sha256(entity_slot).hexdigest()
    entity_acceptance_digest = hashlib.sha256(entity_acceptance).hexdigest()
    history_digest = hashlib.sha256(history).hexdigest() if history is not None else None
    workbench = workbench_definition.encode() if workbench_definition is not None else None
    workbench_digest = hashlib.sha256(workbench).hexdigest() if workbench else None
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
        + (
            "  - id: acceptance.pcs-history\n"
            "    kind: acceptance\n"
            "    path: acceptance/pcs-history.yaml\n"
            f"    sha256: \"{history_digest}\"\n"
            if history is not None
            else ""
        )
        + (
            "  - id: workbench.ems\n"
            "    kind: ems_workbench\n"
            "    path: workbench/ems.yaml\n"
            f"    sha256: \"{workbench_digest}\"\n"
            if workbench is not None
            else ""
        )
        +
        "acceptance:\n"
        "  - acceptance.platform-liveness\n"
        "  - acceptance.pcs-active-power\n"
        + ("  - acceptance.pcs-history\n" if history is not None else "")
    ).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("solution.yaml", manifest)
        package.writestr("acceptance/liveness.yaml", acceptance)
        package.writestr("entities/pcs-primary.yaml", entity_slot)
        package.writestr("entities/pcs-active-power.yaml", entity_definition)
        package.writestr("acceptance/pcs-active-power.yaml", entity_acceptance)
        if history is not None:
            package.writestr("acceptance/pcs-history.yaml", history)
        if workbench is not None:
            package.writestr("workbench/ems.yaml", workbench)
    return archive.getvalue()


def build_policy_package(
    policy_definition: str,
    *,
    base_archive: bytes | None = None,
    acceptance_definition: str | None = None,
) -> bytes:
    """Append one declarative EMS policy asset to the minimal public package."""
    policy = policy_definition.encode()
    source = base_archive or build_entity_package()
    with zipfile.ZipFile(io.BytesIO(source)) as existing:
        files = {
            info.filename: existing.read(info)
            for info in existing.infolist()
            if not info.is_dir()
        }
    manifest = files["solution.yaml"].decode()
    asset = (
        "  - id: policy.grid-import-cap\n"
        "    kind: ems_policy\n"
        "    path: policies/grid-import-cap.yaml\n"
        f"    sha256: \"{hashlib.sha256(policy).hexdigest()}\"\n"
    )
    if acceptance_definition is not None:
        acceptance = acceptance_definition.encode()
        asset += (
            "  - id: acceptance.policy-grid-import-cap\n"
            "    kind: acceptance\n"
            "    path: acceptance/policy-grid-import-cap.yaml\n"
            f"    sha256: \"{hashlib.sha256(acceptance).hexdigest()}\"\n"
        )
        manifest = manifest.replace(
            "  - acceptance.platform-liveness\n",
            "  - acceptance.platform-liveness\n  - acceptance.policy-grid-import-cap\n",
        )
        files["acceptance/policy-grid-import-cap.yaml"] = acceptance
    files["solution.yaml"] = manifest.replace("acceptance:\n", asset + "acceptance:\n").encode()
    files["policies/grid-import-cap.yaml"] = policy
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path, content in files.items():
            package.writestr(path, content)
    return archive.getvalue()


def build_alarm_entity_package(
    *,
    package_version: str = "1.0.0",
    multiple_devices: bool = False,
    manual_failover: bool = False,
    recovery_value: int = 90,
) -> bytes:
    """为最小 PCS 包增加一个只引用实体实例的告警定义。"""
    alarm_definition = (
        "schemaVersion: zizu.alarm-definition/v1alpha1\n"
        "id: alarm.pcs.overpower\n"
        "kind: alarm_definition\n"
        "version: 1.0.0\n"
        "slot: slot.pcs-primary\n"
        "entityDefinition: pcs.activePower\n"
        "trigger:\n  op: gt\n  value: 100\n"
        "triggerDuration: 1s\n"
        f"recovery:\n  op: lte\n  value: {recovery_value}\n"
        "recoveryDuration: 1s\n"
        "severity: MAJOR\n"
        "notificationThrottle: 60s\n"
    ).encode()
    alarm_acceptance = (
        "schemaVersion: zizu.acceptance/v1alpha1\n"
        "id: acceptance.pcs-overpower-lifecycle\n"
        "kind: alarm_lifecycle\n"
        "required: true\n"
        "alarmDefinition: alarm.pcs.overpower\n"
        "expectedState: recovered\n"
        "timeout: 5s\n"
    ).encode()
    source = io.BytesIO(
        build_entity_package(
            package_version=package_version,
            multiple_devices=multiple_devices,
            manual_failover=manual_failover,
        )
    )
    with zipfile.ZipFile(source) as package:
        files = {
            item.filename: package.read(item.filename)
            for item in package.infolist()
            if not item.is_dir()
        }
    declaration = (
        "  - id: alarm.pcs.overpower\n"
        "    kind: alarm_definition\n"
        "    path: alarms/pcs-overpower.yaml\n"
        f"    sha256: \"{hashlib.sha256(alarm_definition).hexdigest()}\"\n"
    ).encode()
    acceptance_declaration = (
        "  - id: acceptance.pcs-overpower-lifecycle\n"
        "    kind: acceptance\n"
        "    path: acceptance/pcs-overpower-lifecycle.yaml\n"
        f"    sha256: \"{hashlib.sha256(alarm_acceptance).hexdigest()}\"\n"
    ).encode()
    files["solution.yaml"] = files["solution.yaml"].replace(
        b"acceptance:\n",
        declaration
        + acceptance_declaration
        + b"acceptance:\n  - acceptance.pcs-overpower-lifecycle\n",
    )
    files["alarms/pcs-overpower.yaml"] = alarm_definition
    files["acceptance/pcs-overpower-lifecycle.yaml"] = alarm_acceptance
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path, content in files.items():
            package.writestr(path, content)
    return archive.getvalue()


def build_control_entity_package(
    *,
    package_version: str = "1.0.0",
    high_risk: bool = False,
    maximum: int = 100,
) -> bytes:
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
            f"control:\n  minimum: -100\n  maximum: {maximum}\n  cooldown: 5s\n"
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
        "grid.activePower": (
            "schemaVersion: zizu.entity-definition/v1alpha1\n"
            "id: grid.activePower\nkind: entity_definition\ndisplayName: Grid power\n"
            "deviceCategory: pcs\ndataType: FLOAT\nunit: kW\ndirection: R\n"
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
        "  - definition: grid.activePower\n    matcher:\n      id: matcher.grid-power\n"
        "      deviceKeyParameter: pcs.device_key\n      tagName: GridPower\n"
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
        "schemaVersion: zizu.solution/v1alpha1\nid: org.zizu.control-pcs\n"
        f"version: {package_version}\n"
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
    def build_app(
        *,
        sources: tuple,
        legacy_entities: tuple = (),
        release_lock_reader=None,
    ) -> FastAPI:
        from app.api.entity_instances import (
            get_entity_instance_catalog,
            get_entity_instance_failover,
            get_entity_instance_runtime,
            router as entity_instance_router,
        )
        from app.api.solution_delivery import (
            get_default_ems_workbench,
            get_solution_delivery,
            router as delivery_router,
        )
        from app.api.ems_workbench import router as ems_workbench_router
        from app.api.alarm_events import get_alarm_runtime, router as alarm_event_router
        from app.api.health import router as health_router
        from app.api.rules import (
            get_rule_alarm_adapter,
            router as rules_router,
        )
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
        from app.services.alarm_runtime import (
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )
        from app.services.entity_alarm_adapter import EntityAlarmAdapter
        from app.services.rule_alarm_adapter import RuleAlarmAdapter
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )
        from app.services.ems_workbench import EmsWorkbench

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
        alarm_definitions = InMemoryAlarmDefinitionCatalog()
        alarm_runtime = AlarmRuntime(alarm_definitions, InMemoryAlarmRepository())
        alarm_adapter = EntityAlarmAdapter(alarm_definitions, runtime, alarm_runtime)
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
            alarm_outcomes = alarm_adapter.submit_all()
            return {
                "published": published,
                "alarm_outcomes": [
                    {"event_id": str(item.event_id) if item.event_id else None, "state": item.state, "code": item.code}
                    for item in alarm_outcomes
                ],
            }

        app.include_router(health_router, prefix="/api/v1")
        app.include_router(delivery_router, prefix="/api/v1")
        app.include_router(ems_workbench_router, prefix="/api/v1")
        app.include_router(entity_instance_router, prefix="/api/v1")
        app.include_router(alarm_event_router, prefix="/api/v1")
        app.include_router(rules_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            delivery_repository,
            platform_version="0.4.78",
            public_api_probe=AsgiPublicApiProbe(app),
            entity_instance_registry=registry,
            entity_instance_runtime=runtime,
            alarm_definitions=alarm_definitions,
            alarm_runtime=alarm_runtime,
            release_lock_reader=release_lock_reader,
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        app.state.solution_delivery = delivery
        app.state.delivery_repository = delivery_repository
        app.state.entity_instance_catalog = catalog
        app.state.entity_instance_runtime = runtime
        app.dependency_overrides[get_default_ems_workbench] = lambda: EmsWorkbench(
            delivery_repository,
            catalog,
            runtime,
        )
        app.dependency_overrides[get_entity_instance_runtime] = lambda: runtime
        app.dependency_overrides[get_entity_instance_catalog] = lambda: catalog
        app.dependency_overrides[get_entity_instance_failover] = lambda: failover
        app.dependency_overrides[get_alarm_runtime] = lambda: alarm_runtime
        app.dependency_overrides[get_rule_alarm_adapter] = lambda: RuleAlarmAdapter(
            alarm_definitions,
            alarm_runtime,
        )
        app.state.entity_instance_registry = registry
        app.state.entity_instance_repository = entity_repository
        app.state.entity_instance_runtime = runtime
        app.state.alarm_runtime = alarm_runtime
        return app

    @staticmethod
    def source(
        tag_id: UUID = TAG_ID,
        *,
        device_key: str = "PCS-01",
        direction: str = "R",
    ):
        from app.services.entity_instance_registry import SourceDescriptor

        return SourceDescriptor(
            tag_id=tag_id,
            device_key=device_key,
            device_name=device_key,
            tag_name="ActivePower",
            data_type="FLOAT",
            unit="kW",
            direction=direction,
            enabled=True,
        )

    @staticmethod
    async def import_and_plan(
        client: AuthenticatedDeliveryClient,
        *,
        archive: bytes | None = None,
        **body,
    ):
        imported = await client.post(
            "/api/v1/solution-packages/import",
            files={
                "archive": (
                    "single-pcs.zizu.zip",
                    archive or build_entity_package(),
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

    async def test_import_rejects_workbench_reference_outside_declared_entity_slots(self) -> None:
        """A package must fail before persistence when its operator UI cannot resolve an entity."""
        app = self.build_app(sources=(self.source(),))
        invalid_workbench = (
            "schemaVersion: zizu.ems-workbench/v1alpha1\n"
            "id: workbench.ems\n"
            "kind: ems_workbench\n"
            "navigation:\n"
            "  - id: overview\n"
            "    label: 概览\n"
            "groups:\n"
            "  - id: pcs\n"
            "    label: PCS\n"
            "    entities:\n"
            "      - slot: slot.pcs-primary\n"
            "        definition: pcs.unknown\n"
            "kpis: []\n"
            "trends: []\n"
            "alarms:\n"
            "  visible: true\n"
            "controls:\n"
            "  visible: true\n"
        )
        async with AuthenticatedDeliveryClient(app) as client:
            rejected = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "invalid-workbench.zizu.zip",
                        build_entity_package(workbench_definition=invalid_workbench),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(422, rejected.status_code, rejected.text)
            self.assertEqual("ASSET_REFERENCE_INVALID", rejected.json()["detail"]["code"])
            packages = await client.get("/api/v1/solution-packages")
            self.assertEqual(0, packages.json()["total"])

    async def test_import_rejects_policy_target_that_is_not_a_writable_confirmed_entity(self) -> None:
        """A policy cannot turn a read-only measurement into a control bypass."""
        app = self.build_app(sources=(self.source(),))
        policy = (
            "schemaVersion: zizu.ems-policy/v1alpha1\n"
            "id: policy.grid-import-cap\n"
            "kind: ems_policy\n"
            "revision: 1\n"
            "input:\n"
            "  slot: slot.pcs-primary\n"
            "  definition: pcs.activePower\n"
            "  unit: kW\n"
            "condition:\n"
            "  operator: gt\n"
            "  threshold: 100\n"
            "action:\n"
            "  id: cap-import\n"
            "  target:\n"
            "    slot: slot.pcs-primary\n"
            "    definition: pcs.activePower\n"
            "  value: 50\n"
            "  unit: kW\n"
            "simulation:\n  input:\n    value: 120\n    unit: kW\n  expected:\n    triggered: true\n    actionValue: 50\n"
        )
        async with AuthenticatedDeliveryClient(app) as client:
            response = await client.post(
                "/api/v1/solution-packages/import",
                files={"archive": ("invalid-policy.zizu.zip", build_policy_package(policy), "application/zip")},
            )
            self.assertEqual(422, response.status_code, response.text)
            self.assertEqual("POLICY_TARGET_NOT_WRITABLE", response.json()["detail"]["code"])
            packages = await client.get("/api/v1/solution-packages")
            self.assertEqual(0, packages.json()["total"])

    async def test_valid_policy_is_a_reviewable_installation_plan_item(self) -> None:
        """A policy is versioned package data, not an untracked rule-engine edit."""
        from app.services.entity_instance_registry import SourceDescriptor

        app = self.build_app(sources=(
            SourceDescriptor(TAG_ID, "PCS-01", "PCS-01", "Setpoint", "FLOAT", "kW", "RW", True),
            SourceDescriptor(OTHER_TAG_ID, "PCS-01", "PCS-01", "Readback", "FLOAT", "kW", "R", True),
            SourceDescriptor(BACKUP_TAG_ID, "PCS-01", "PCS-01", "Ready", "BOOL", None, "R", True),
        ))
        policy = (
            "schemaVersion: zizu.ems-policy/v1alpha1\n"
            "id: policy.grid-import-cap\n"
            "kind: ems_policy\n"
            "revision: 1\n"
            "input:\n  slot: slot.pcs-primary\n  definition: pcs.readback\n  unit: kW\n"
            "condition:\n  operator: gt\n  threshold: 100\n"
            "action:\n  id: cap-import\n  target:\n    slot: slot.pcs-primary\n    definition: pcs.setpoint\n  value: 50\n  unit: kW\n"
            "simulation:\n  input:\n    value: 120\n    unit: kW\n  expected:\n    triggered: true\n    actionValue: 50\n"
        )
        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={"archive": ("policy-pcs.zizu.zip", build_policy_package(policy, base_archive=build_control_entity_package()), "application/zip")},
            )
            self.assertEqual(201, imported.status_code, imported.text)
            self.assertEqual(["policy.grid-import-cap"], imported.json()["policy_asset_ids"])
            plan = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={"parameters": {"pcs.instance_key": "PCS-01", "pcs.device_key": "PCS-01"}},
            )
        self.assertEqual(201, plan.status_code, plan.text)
        self.assertIn(
            {"asset_id": "policy.grid-import-cap", "kind": "ems_policy", "action": "add", "revision": 1},
            plan.json()["items"],
        )

    async def test_operator_reads_package_configured_workbench_from_confirmed_entity_instances(self) -> None:
        app = self.build_app(sources=(self.source(),))
        workbench = (
            "schemaVersion: zizu.ems-workbench/v1alpha1\n"
            "id: workbench.ems\n"
            "kind: ems_workbench\n"
            "navigation:\n"
            "  - id: overview\n"
            "    label: 场站概览\n"
            "  - id: trends\n"
            "    label: 运行趋势\n"
            "  - id: alarms\n"
            "    label: 告警\n"
            "groups:\n"
            "  - id: pcs\n"
            "    label: PCS\n"
            "    entities:\n"
            "      - slot: slot.pcs-primary\n"
            "        definition: pcs.activePower\n"
            "kpis:\n"
            "  - id: pcs-power\n"
            "    label: PCS 功率\n"
            "    entity:\n"
            "      slot: slot.pcs-primary\n"
            "      definition: pcs.activePower\n"
            "trends:\n"
            "  - id: pcs-power-trend\n"
            "    label: PCS 功率趋势\n"
            "    defaultRange: 24h\n"
            "    entities:\n"
            "      - slot: slot.pcs-primary\n"
            "        definition: pcs.activePower\n"
            "alarms:\n"
            "  visible: true\n"
            "controls:\n"
            "  visible: true\n"
        )
        async with AuthenticatedDeliveryClient(app) as client:
            anonymous = await client._client.get("/api/v1/ems-workbench")
            self.assertEqual(401, anonymous.status_code, anonymous.text)
            _, planned = await self.import_and_plan(
                client,
                archive=build_entity_package(workbench_definition=workbench),
            )
            self.assertEqual(201, planned.status_code, planned.text)
            installed = await client.post(
                f"/api/v1/install-plans/{planned.json()['id']}/apply",
                json={"plan_digest": planned.json()["digest"]},
                headers={"Idempotency-Key": "install-workbench"},
            )
            self.assertEqual(201, installed.status_code, installed.text)
            now = datetime.now(timezone.utc)
            published = await client.post(
                "/protocol-simulator/neuron",
                json={
                    "message": {
                        "node": "PCS-01",
                        "timestamp": round(now.timestamp() * 1000),
                        "values": {"ActivePower": 42.5},
                    }
                },
            )
            self.assertEqual(200, published.status_code, published.text)
            operator_headers = {"Authorization": await client._bearer("operator")}
            response = await client._client.get(
                "/api/v1/ems-workbench",
                headers=operator_headers,
            )
            self.assertEqual(200, response.status_code, response.text)
            payload = response.json()
            self.assertEqual("workbench.ems", payload["workbench_id"])
            self.assertEqual("场站概览", payload["navigation"][0]["label"])
            self.assertEqual("PCS", payload["groups"][0]["label"])
            self.assertEqual(42.5, payload["kpis"][0]["entities"][0]["value"])
            self.assertEqual("available", payload["kpis"][0]["entities"][0]["status"])
            self.assertEqual("24h", payload["trends"][0]["default_range"])
            trend = await client._client.get(
                "/api/v1/ems-workbench/trends/pcs-power-trend?range=24h",
                headers=operator_headers,
            )
            self.assertEqual(200, trend.status_code, trend.text)
            self.assertEqual("pcs-power-trend", trend.json()["id"])
            self.assertEqual(42.5, trend.json()["series"][0]["points"][0]["value"])

    async def test_protocol_observation_drives_auditable_entity_alarm_lifecycle(self) -> None:
        app = self.build_app(sources=(self.source(),))
        started_at = datetime.now(timezone.utc)

        async with AuthenticatedDeliveryClient(app) as client:
            _, planned = await self.import_and_plan(
                client,
                archive=build_alarm_entity_package(),
            )
            self.assertEqual(201, planned.status_code, planned.text)
            self.assertEqual(
                "alarm_definition",
                next(
                    item["kind"]
                    for item in planned.json()["items"]
                    if item["kind"] == "alarm_definition"
                ),
            )
            installed = await client.post(
                f"/api/v1/install-plans/{planned.json()['id']}/apply",
                json={"plan_digest": planned.json()["digest"]},
                headers={"Idempotency-Key": "install-entity-alarm"},
            )
            self.assertEqual(201, installed.status_code, installed.text)

            async def publish(
                value: float,
                after_seconds: float,
                *,
                quality: int = 192,
            ) -> dict:
                response = await client.post(
                    "/protocol-simulator/neuron",
                    json={
                        "message": {
                            "node": "PCS-01",
                            "timestamp": round(
                                (started_at + timedelta(seconds=after_seconds)).timestamp()
                                * 1000
                            ),
                            "values": {"ActivePower": value},
                        },
                        "quality": quality,
                    },
                )
                self.assertEqual(200, response.status_code, response.text)
                return response.json()

            self.assertEqual("ALARM_TRIGGER_PENDING", (await publish(101, 0))["alarm_outcomes"][0]["code"])
            activated = await publish(101, 1.1)
            self.assertEqual("ALARM_ACTIVATED", activated["alarm_outcomes"][0]["code"])

            listed = await client.get("/api/v1/alarm-events")
            self.assertEqual(200, listed.status_code, listed.text)
            event_id = listed.json()["items"][0]["id"]
            self.assertEqual("active_unacknowledged", listed.json()["items"][0]["state"])

            operator_headers = {"Authorization": await client._bearer("operator")}
            acknowledged = await client._client.post(
                f"/api/v1/alarm-events/{event_id}/acknowledgements",
                headers=operator_headers,
                json={"note": "已知悉"},
            )
            self.assertEqual(200, acknowledged.status_code, acknowledged.text)
            self.assertEqual("active_acknowledged", acknowledged.json()["state"])

            self.assertEqual("ALARM_RECOVERY_PENDING", (await publish(90, 2.0))["alarm_outcomes"][0]["code"])
            self.assertEqual(
                "ALARM_STILL_ACTIVE",
                (await publish(90, 2.5, quality=0))["alarm_outcomes"][0]["code"],
            )
            self.assertEqual("ALARM_RECOVERY_PENDING", (await publish(90, 3.1))["alarm_outcomes"][0]["code"])
            self.assertEqual("ALARM_STILL_ACTIVE", (await publish(90, 34.2))["alarm_outcomes"][0]["code"])
            self.assertEqual("ALARM_RECOVERY_PENDING", (await publish(90, 35.0))["alarm_outcomes"][0]["code"])
            recovered = await publish(90, 36.1)
            self.assertEqual("ALARM_RECOVERED", recovered["alarm_outcomes"][0]["code"])
            event = await client.get(f"/api/v1/alarm-events/{event_id}")
            self.assertEqual(200, event.status_code, event.text)
            self.assertEqual("recovered", event.json()["state"])
            self.assertEqual(
                "user:00000000-0000-0000-0000-000000000003",
                event.json()["acknowledged_by"],
            )

            accepted = await client.post(
                f"/api/v1/solution-installations/{installed.json()['id']}/acceptance-runs",
                headers={"Idempotency-Key": "accept-entity-alarm-lifecycle"},
            )
            self.assertEqual(201, accepted.status_code, accepted.text)
            lifecycle_item = next(
                item
                for item in accepted.json()["items"]
                if item["acceptance_id"] == "acceptance.pcs-overpower-lifecycle"
            )
            self.assertEqual("passed", lifecycle_item["status"], accepted.text)
            self.assertEqual("ALARM_LIFECYCLE_CONFIRMED", lifecycle_item["code"])
            self.assertEqual("recovered", lifecycle_item["evidence"]["events"][0]["state"])
            self.assertEqual(
                [],
                lifecycle_item["evidence"]["events"][0]["missing_transition_codes"],
            )

    async def test_alarm_lifecycle_acceptance_requires_operator_acknowledgement(
        self,
    ) -> None:
        app = self.build_app(sources=(self.source(),))
        started_at = datetime.now(timezone.utc)

        async with AuthenticatedDeliveryClient(app) as client:
            _, planned = await self.import_and_plan(
                client,
                archive=build_alarm_entity_package(),
            )
            self.assertEqual(201, planned.status_code, planned.text)
            installed = await client.post(
                f"/api/v1/install-plans/{planned.json()['id']}/apply",
                json={"plan_digest": planned.json()["digest"]},
                headers={"Idempotency-Key": "install-unacknowledged-alarm"},
            )
            self.assertEqual(201, installed.status_code, installed.text)

            async def publish(value: float, after_seconds: float) -> None:
                response = await client.post(
                    "/protocol-simulator/neuron",
                    json={
                        "message": {
                            "node": "PCS-01",
                            "timestamp": round(
                                (started_at + timedelta(seconds=after_seconds)).timestamp()
                                * 1000
                            ),
                            "values": {"ActivePower": value},
                        }
                    },
                )
                self.assertEqual(200, response.status_code, response.text)

            await publish(101, 0)
            await publish(101, 1.1)
            await publish(90, 2.0)
            await publish(90, 3.1)
            report = await client.post(
                f"/api/v1/solution-installations/{installed.json()['id']}/acceptance-runs",
                headers={"Idempotency-Key": "accept-unacknowledged-alarm"},
            )

        self.assertEqual(201, report.status_code, report.text)
        lifecycle_item = next(
            item
            for item in report.json()["items"]
            if item["acceptance_id"] == "acceptance.pcs-overpower-lifecycle"
        )
        self.assertEqual("failed", lifecycle_item["status"], report.text)
        self.assertEqual("ALARM_LIFECYCLE_INCOMPLETE", lifecycle_item["code"])
        self.assertEqual(
            ["ALARM_ACKNOWLEDGED"],
            lifecycle_item["evidence"]["events"][0]["missing_transition_codes"],
        )

    async def test_entity_direction_change_blocks_an_automatic_upgrade(self) -> None:
        """A source compatible with both versions cannot hide a direction change."""
        app = self.build_app(sources=(self.source(direction="RW"),))

        async with AuthenticatedDeliveryClient(app) as client:
            imported_v1, plan_v1 = await self.import_and_plan(
                client,
                archive=build_entity_package(package_version="1.0.0"),
            )
            installed_v1 = await client.post(
                f"/api/v1/install-plans/{plan_v1.json()['id']}/apply",
                json={"plan_digest": plan_v1.json()["digest"]},
                headers={"Idempotency-Key": "install-direction-v1"},
            )
            imported_v2 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "single-pcs-direction-v2.zizu.zip",
                        build_entity_package(
                            package_version="1.1.0",
                            entity_direction="RW",
                        ),
                        "application/zip",
                    )
                },
            )
            plan_v2 = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instance_key": "PCS-01",
                        "pcs.device_key": "PCS-01",
                    }
                },
            )
            blocked_apply = await client.post(
                f"/api/v1/install-plans/{plan_v2.json()['id']}/apply",
                json={"plan_digest": plan_v2.json()["digest"]},
                headers={"Idempotency-Key": "install-direction-v2"},
            )
            reviewed_plan = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instance_key": "PCS-01",
                        "pcs.device_key": "PCS-01",
                    },
                    "upgrade_risk_resolutions": {
                        "UPGRADE_ENTITY_SEMANTICS_CHANGED:pcs.activePower": (
                            "Verified the source and downstream unit conversion"
                        )
                    },
                },
            )
            reviewed_install = await client.post(
                f"/api/v1/install-plans/{reviewed_plan.json()['id']}/apply",
                json={"plan_digest": reviewed_plan.json()["digest"]},
                headers={"Idempotency-Key": "install-direction-v2-reviewed"},
            )
            invalid_review = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instance_key": "PCS-01",
                        "pcs.device_key": "PCS-01",
                    },
                    "upgrade_risk_resolutions": {"unknown:risk": "not accepted"},
                },
            )
            original_configuration = await client.get(
                "/api/v1/site-configuration-versions/"
                f"{installed_v1.json()['site_configuration_version']}"
            )

        self.assertEqual(201, imported_v1.status_code, imported_v1.text)
        self.assertEqual(201, plan_v1.status_code, plan_v1.text)
        self.assertEqual(201, installed_v1.status_code, installed_v1.text)
        self.assertEqual(201, imported_v2.status_code, imported_v2.text)
        self.assertEqual(201, plan_v2.status_code, plan_v2.text)
        self.assertEqual("blocked", plan_v2.json()["status"])
        self.assertIn(
            {
                "code": "UPGRADE_ENTITY_SEMANTICS_CHANGED",
                "asset_id": "pcs.activePower",
                "message": "Entity unit or direction changed",
            },
            plan_v2.json()["blockers"],
        )
        blocked_item = next(
            item
            for item in plan_v2.json()["items"]
            if item.get("kind") == "upgrade_safety"
        )
        self.assertEqual("block", blocked_item["action"])
        self.assertEqual(
            "UPGRADE_ENTITY_SEMANTICS_CHANGED:pcs.activePower",
            blocked_item["risk_key"],
        )
        self.assertEqual(409, blocked_apply.status_code, blocked_apply.text)
        self.assertEqual(
            "INSTALL_PLAN_BLOCKED",
            blocked_apply.json()["detail"]["code"],
        )
        self.assertEqual(201, reviewed_plan.status_code, reviewed_plan.text)
        self.assertEqual("ready", reviewed_plan.json()["status"])
        self.assertEqual(201, reviewed_install.status_code, reviewed_install.text)
        self.assertEqual(422, invalid_review.status_code, invalid_review.text)
        self.assertEqual(
            "UPGRADE_RISK_RESOLUTION_INVALID",
            invalid_review.json()["detail"]["code"],
        )
        self.assertIn(
            {
                "asset_id": "pcs.activePower",
                "kind": "upgrade_safety",
                "action": "update",
                "code": "UPGRADE_ENTITY_SEMANTICS_CHANGED",
                "message": "Entity unit or direction changed",
                "risk_key": "UPGRADE_ENTITY_SEMANTICS_CHANGED:pcs.activePower",
                "resolution": "Verified the source and downstream unit conversion",
                "reviewed_by": "user:00000000-0000-0000-0000-000000000002",
            },
            reviewed_plan.json()["items"],
        )
        self.assertEqual(200, original_configuration.status_code, original_configuration.text)
        self.assertEqual(1, original_configuration.json()["version"])

    async def test_removing_a_running_entity_reference_blocks_an_automatic_upgrade(
        self,
    ) -> None:
        app = self.build_app(sources=(self.source(),))

        async with AuthenticatedDeliveryClient(app) as client:
            _, plan_v1 = await self.import_and_plan(
                client,
                archive=build_entity_package(package_version="1.0.0"),
            )
            installed_v1 = await client.post(
                f"/api/v1/install-plans/{plan_v1.json()['id']}/apply",
                json={"plan_digest": plan_v1.json()["digest"]},
                headers={"Idempotency-Key": "install-removal-v1"},
            )
            imported_v2 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "single-pcs-removal-v2.zizu.zip",
                        build_minimal_package(
                            package_id="org.zizu.single-pcs",
                            package_version="1.1.0",
                        ),
                        "application/zip",
                    )
                },
            )
            plan_v2 = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={},
            )
            blocked_apply = await client.post(
                f"/api/v1/install-plans/{plan_v2.json()['id']}/apply",
                json={"plan_digest": plan_v2.json()["digest"]},
                headers={"Idempotency-Key": "install-removal-v2"},
            )
            original_configuration = await client.get(
                "/api/v1/site-configuration-versions/"
                f"{installed_v1.json()['site_configuration_version']}"
            )

        self.assertEqual(201, installed_v1.status_code, installed_v1.text)
        self.assertEqual(201, imported_v2.status_code, imported_v2.text)
        self.assertEqual(201, plan_v2.status_code, plan_v2.text)
        self.assertEqual("blocked", plan_v2.json()["status"])
        self.assertIn(
            {
                "code": "UPGRADE_RUNNING_REFERENCE_REMOVAL",
                "asset_id": "pcs.activePower",
                "message": "Upgrade removes a running entity definition",
            },
            plan_v2.json()["blockers"],
        )
        self.assertEqual(409, blocked_apply.status_code, blocked_apply.text)
        self.assertEqual(
            "INSTALL_PLAN_BLOCKED",
            blocked_apply.json()["detail"]["code"],
        )
        self.assertEqual(200, original_configuration.status_code, original_configuration.text)
        self.assertEqual(1, original_configuration.json()["version"])

    async def test_alarm_recovery_change_blocks_an_automatic_upgrade(self) -> None:
        app = self.build_app(sources=(self.source(),))

        async with AuthenticatedDeliveryClient(app) as client:
            _, plan_v1 = await self.import_and_plan(
                client,
                archive=build_alarm_entity_package(package_version="1.0.0"),
            )
            installed_v1 = await client.post(
                f"/api/v1/install-plans/{plan_v1.json()['id']}/apply",
                json={"plan_digest": plan_v1.json()["digest"]},
                headers={"Idempotency-Key": "install-alarm-v1"},
            )
            imported_v2 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "single-pcs-alarm-v2.zizu.zip",
                        build_alarm_entity_package(
                            package_version="1.1.0",
                            recovery_value=80,
                        ),
                        "application/zip",
                    )
                },
            )
            plan_v2 = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.instance_key": "PCS-01",
                        "pcs.device_key": "PCS-01",
                    }
                },
            )
            blocked_apply = await client.post(
                f"/api/v1/install-plans/{plan_v2.json()['id']}/apply",
                json={"plan_digest": plan_v2.json()["digest"]},
                headers={"Idempotency-Key": "install-alarm-v2"},
            )

        self.assertEqual(201, installed_v1.status_code, installed_v1.text)
        self.assertEqual(201, imported_v2.status_code, imported_v2.text)
        self.assertEqual(201, plan_v2.status_code, plan_v2.text)
        self.assertEqual("blocked", plan_v2.json()["status"])
        self.assertIn(
            {
                "code": "UPGRADE_ALARM_RECOVERY_CHANGED",
                "asset_id": "alarm.pcs.overpower",
                "message": "Alarm recovery behavior changed",
            },
            plan_v2.json()["blockers"],
        )
        self.assertEqual(409, blocked_apply.status_code, blocked_apply.text)
        self.assertEqual(
            "INSTALL_PLAN_BLOCKED",
            blocked_apply.json()["detail"]["code"],
        )

    async def test_control_policy_change_blocks_an_automatic_upgrade(self) -> None:
        from app.services.entity_instance_registry import SourceDescriptor

        sources = (
            SourceDescriptor(
                tag_id=UUID("20000000-0000-0000-0000-000000000010"),
                device_key="PCS-01",
                device_name="PCS-01",
                tag_name="Setpoint",
                data_type="FLOAT",
                unit="kW",
                direction="RW",
                enabled=True,
            ),
            SourceDescriptor(
                tag_id=UUID("20000000-0000-0000-0000-000000000011"),
                device_key="PCS-01",
                device_name="PCS-01",
                tag_name="Readback",
                data_type="FLOAT",
                unit="kW",
                direction="R",
                enabled=True,
            ),
            SourceDescriptor(
                tag_id=UUID("20000000-0000-0000-0000-000000000012"),
                device_key="PCS-01",
                device_name="PCS-01",
                tag_name="Ready",
                data_type="BOOL",
                unit=None,
                direction="R",
                enabled=True,
            ),
            SourceDescriptor(
                tag_id=UUID("20000000-0000-0000-0000-000000000013"),
                device_key="PCS-01",
                device_name="PCS-01",
                tag_name="GridPower",
                data_type="FLOAT",
                unit="kW",
                direction="R",
                enabled=True,
            ),
        )
        app = self.build_app(sources=sources)
        parameters = {
            "pcs.instance_key": "PCS-01",
            "pcs.device_key": "PCS-01",
        }

        async with AuthenticatedDeliveryClient(app) as client:
            imported_v1 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "control-pcs-v1.zizu.zip",
                        build_control_entity_package(package_version="1.0.0"),
                        "application/zip",
                    )
                },
            )
            plan_v1 = await client.post(
                f"/api/v1/solution-packages/{imported_v1.json()['id']}/install-plans",
                json={"parameters": parameters},
            )
            installed_v1 = await client.post(
                f"/api/v1/install-plans/{plan_v1.json()['id']}/apply",
                json={"plan_digest": plan_v1.json()["digest"]},
                headers={"Idempotency-Key": "install-control-v1"},
            )
            imported_v2 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "control-pcs-v2.zizu.zip",
                        build_control_entity_package(
                            package_version="1.1.0",
                            maximum=200,
                        ),
                        "application/zip",
                    )
                },
            )
            plan_v2 = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={"parameters": parameters},
            )
            blocked_apply = await client.post(
                f"/api/v1/install-plans/{plan_v2.json()['id']}/apply",
                json={"plan_digest": plan_v2.json()["digest"]},
                headers={"Idempotency-Key": "install-control-v2"},
            )

        self.assertEqual(201, installed_v1.status_code, installed_v1.text)
        self.assertEqual(201, imported_v2.status_code, imported_v2.text)
        self.assertEqual(201, plan_v2.status_code, plan_v2.text)
        self.assertEqual("blocked", plan_v2.json()["status"])
        self.assertIn(
            {
                "code": "UPGRADE_CONTROL_POLICY_CHANGED",
                "asset_id": "pcs.setpoint",
                "message": "Upgrade broadens control permissions and requires review",
            },
            plan_v2.json()["blockers"],
        )
        self.assertEqual(409, blocked_apply.status_code, blocked_apply.text)
        self.assertEqual(
            "INSTALL_PLAN_BLOCKED",
            blocked_apply.json()["detail"]["code"],
        )

    async def test_package_to_confirmed_fresh_entity_delivery_report(self) -> None:
        app = self.build_app(
            sources=(self.source(),),
        )

        async with AuthenticatedDeliveryClient(app) as client:
            imported, planned = await self.import_and_plan(
                client,
                archive=build_entity_package(history_acceptance=True),
            )
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

            second_published = await client.post(
                "/protocol-simulator/neuron",
                json={
                    "message": {
                        "node": "PCS-01",
                        "timestamp": round(datetime.now(timezone.utc).timestamp() * 1000) + 1,
                        "values": {"ActivePower": 126.0},
                    }
                },
            )
            self.assertEqual(200, second_published.status_code, second_published.text)
            self.assertEqual(1, second_published.json()["published"])

            realtime = await client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime"
            )
            self.assertEqual(200, realtime.status_code, realtime.text)
            self.assertEqual(126.0, realtime.json()["value"])

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
            history_evidence = next(
                item for item in report["items"] if item["code"] == "HISTORY_AVAILABLE"
            )
            self.assertEqual(2, history_evidence["evidence"]["sample_count"])
            self.assertEqual(2, history_evidence["evidence"]["good_sample_count"])

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

    def test_rule_instance_context_reuses_registry_resolution_and_alarm_safe_runtime_read(self) -> None:
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
        runtime.read_for_alarm.return_value = SimpleNamespace(
            value=125.5,
            observed_at=datetime.now(timezone.utc),
            quality=192,
            fresh=True,
            max_observation_gap_seconds=30,
        )
        with patch(
            "app.api.solution_delivery.get_default_entity_instance_registry",
            return_value=registry,
        ), patch(
            "app.api.solution_delivery.get_default_entity_instance_runtime",
            return_value=runtime,
        ):
            context = _entity_instance_context({str(entity_id)})

        registry.resolve.assert_called_once_with(entity_id)
        runtime.read_for_alarm.assert_called_once_with(entity_id)
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
