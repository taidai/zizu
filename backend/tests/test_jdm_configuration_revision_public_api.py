"""Public API contract for revision-bound JDM configuration and simulation."""
from __future__ import annotations

from datetime import UTC, datetime
import os
import unittest
from uuid import UUID, uuid4

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

from fastapi import FastAPI

from app.api import rules
from app.services.data_trunk_contracts import DataTrunkError
from app.services.entity_instance_catalog import (
    EntityInstanceCatalog,
    EntityInstanceDescriptor,
)
from tests.api_test_client import AuthenticatedApiClient


SOURCE_ID = UUID("a3000000-0000-0000-0000-000000000001")
TARGET_ID = UUID("a3000000-0000-0000-0000-000000000002")
RULE_ID = UUID("a3000000-0000-0000-0000-000000000003")
NOW = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)


def _content(*, physical_source: bool = False) -> dict:
    config = {
        "sourceEntityInstanceIds": [str(SOURCE_ID)],
        "inputMappings": {"power": str(SOURCE_ID)},
        "actions": [
            {
                "id": "set-limit",
                "type": "control",
                "entity_instance_id": str(TARGET_ID),
                "value": 5,
            }
        ],
    }
    if physical_source:
        config["sourceNodeIds"] = [str(uuid4())]
    return {"when": "power > 10", "_config": config}


class _CatalogRepository:
    def list_instances(self):
        return tuple(
            EntityInstanceDescriptor(
                id=entity_id,
                node_id=uuid4(),
                node_type="PCS",
                node_display_name="PCS-01",
                definition_id=f"pcs.{name}",
                display_name=name,
                data_type="FLOAT",
                unit="kW",
                direction=direction,
                freshness_seconds=3.0,
                confirmed=True,
            )
            for entity_id, name, direction in (
                (SOURCE_ID, "activePower", "R"),
                (TARGET_ID, "powerSetpoint", "RW"),
            )
        )

    def preview_legacy(self):
        return ()


class _Gate:
    def __init__(self, *, fail_begin: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail_begin = fail_begin

    def begin_configuration_publish(self, revision: int) -> None:
        self.calls.append(("begin", revision))
        if self.fail_begin:
            raise DataTrunkError(
                "CONFIGURATION_RUNTIME_BUSY",
                "configuration runtime is busy",
            )

    def cancel_configuration_publish(self) -> None:
        self.calls.append(("cancel",))

    def reconcile_configuration_runtime(self) -> None:
        self.calls.append(("reconcile",))


class _Runtime:
    def __init__(self, *, fail_begin: bool = False) -> None:
        self.gate = _Gate(fail_begin=fail_begin)
        self.data_trunk = type("Trunk", (), {"configuration_gate": self.gate})()

    async def reload_rules_now(self) -> None:
        self.gate.calls.append(("reload",))


class _Rules:
    def __init__(self) -> None:
        self.rows: dict[UUID, dict] = {
            RULE_ID: {
                "id": RULE_ID,
                "name": "limit power",
                "rule_type": "control",
                "jdm_content": _content(),
                "version": 1,
                "enabled": True,
                "configuration_revision": 7,
                "created_at": NOW,
                "updated_at": NOW,
            }
        }
        self.mutations: list[tuple] = []
        self.execution_rows = [
            {
                "id": uuid4(),
                "rule_id": RULE_ID,
                "rule_version": 1,
                "frame_id": uuid4(),
                "frame_sequence": 7001,
                "configuration_revision": 7,
                "model_digest": "d" * 64,
                "status": "rejected",
                "reason_code": "JDM_INPUT_QUALITY_NOT_GOOD",
                "inputs": {},
                "outputs": {},
                "actions": [],
                "executed_at": NOW,
            }
        ]

    def current_revision(self) -> int:
        return 7

    def list(self, enabled=None):
        rows = tuple(self.rows.values())
        return rows if enabled is None else tuple(row for row in rows if row["enabled"] is enabled)

    def get(self, rule_id: UUID):
        return self.rows.get(rule_id)

    def create(self, *, base_revision: int, **values):
        self.mutations.append(("create", base_revision, values))
        row = {
            "id": uuid4(),
            "version": 1,
            "configuration_revision": 8,
            "created_at": NOW,
            "updated_at": NOW,
            **{key: value for key, value in values.items() if key not in {"actor", "references"}},
        }
        self.rows[row["id"]] = row
        return row

    def update(self, *, rule_id: UUID, base_revision: int, changes: dict, **values):
        self.mutations.append(("update", base_revision, rule_id, changes, values))
        row = {
            **self.rows[rule_id],
            **changes,
            "version": self.rows[rule_id]["version"] + 1,
            "configuration_revision": 8,
            "updated_at": NOW,
        }
        self.rows[rule_id] = row
        return row

    def delete(self, *, rule_id: UUID, base_revision: int, **values):
        self.mutations.append(("delete", base_revision, rule_id, values))
        self.rows.pop(rule_id)
        return {"status": "deleted", "id": str(rule_id), "configuration_revision": 8}

    def executions(self, rule_id: UUID, limit: int):
        return tuple(row for row in self.execution_rows if row["rule_id"] == rule_id)[:limit]


class JdmConfigurationRevisionPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = _Rules()
        self.runtime = _Runtime()
        app = FastAPI()
        app.include_router(rules.router, prefix="/api/v1")
        app.dependency_overrides[rules.get_jdm_rules] = lambda: self.repository
        app.dependency_overrides[rules.get_jdm_runtime] = lambda: self.runtime
        app.dependency_overrides[rules.get_entity_instance_catalog] = lambda: EntityInstanceCatalog(_CatalogRepository())
        self.app = app

    async def test_create_drains_runtime_and_binds_new_configuration_revision(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.post(
                "/api/v1/rules",
                json={
                    "name": "new limit",
                    "rule_type": "control",
                    "jdm_content": _content(),
                    "enabled": True,
                },
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(8, response.json()["configuration_revision"])
        self.assertEqual(
            [("begin", 7), ("reload",), ("reconcile",)],
            self.runtime.gate.calls,
        )
        self.assertEqual("create", self.repository.mutations[0][0])

    async def test_simulation_uses_runtime_adapter_without_persisting_a_fact(self) -> None:
        before = (len(self.repository.mutations), len(self.repository.execution_rows))
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.post(
                f"/api/v1/rules/{RULE_ID}/simulate",
                json={"context": {"power": 12}},
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()["evaluation"]["triggered"])
        self.assertEqual(before, (len(self.repository.mutations), len(self.repository.execution_rows)))

    async def test_busy_gate_returns_stable_conflict_without_rule_write(self) -> None:
        busy_runtime = _Runtime(fail_begin=True)
        self.app.dependency_overrides[rules.get_jdm_runtime] = lambda: busy_runtime
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.post(
                "/api/v1/rules",
                json={
                    "name": "blocked",
                    "rule_type": "control",
                    "jdm_content": _content(),
                },
            )

        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("CONFIGURATION_RUNTIME_BUSY", response.json()["detail"]["code"])
        self.assertEqual([], self.repository.mutations)

    async def test_legacy_rule_types_and_physical_inputs_are_rejected(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            alarm = await client.post(
                "/api/v1/rules",
                json={"name": "alarm", "rule_type": "alarm", "jdm_content": _content()},
            )
            physical = await client.post(
                "/api/v1/rules",
                json={"name": "physical", "rule_type": "control", "jdm_content": _content(physical_source=True)},
            )

        self.assertEqual(409, alarm.status_code, alarm.text)
        self.assertEqual("JDM_RULE_TYPE_UNSUPPORTED", alarm.json()["detail"]["code"])
        self.assertEqual(409, physical.status_code, physical.text)
        self.assertEqual("JDM_L2_INPUT_REQUIRED", physical.json()["detail"]["code"])
        self.assertEqual([], self.repository.mutations)

    async def test_execution_history_is_read_only_and_sanitized(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.get(f"/api/v1/rules/{RULE_ID}/executions?limit=50")

        self.assertEqual(200, response.status_code, response.text)
        execution = response.json()["executions"][0]
        self.assertEqual(7001, execution["frame_sequence"])
        self.assertEqual("JDM_INPUT_QUALITY_NOT_GOOD", execution["reason_code"])
        self.assertNotIn("source_path", execution)
        self.assertNotIn("topic", execution)


if __name__ == "__main__":
    unittest.main()
