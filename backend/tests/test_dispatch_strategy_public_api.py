from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import os
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

from fastapi import FastAPI

from app.api import dispatch_strategies
from app.services.dispatch_strategies import (
    ControlIntentDraft,
    EvaluationResult,
    StrategyBindingDraft,
    StrategyEvaluation,
    StrategyInput,
    StrategyRevision,
    StrategySnapshot,
    StrategyView,
)
from tests.api_test_client import AuthenticatedApiClient


NOW = datetime(2026, 9, 4, 2, 30, tzinfo=UTC)
STRATEGY_ID = UUID("74000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("74000000-0000-0000-0000-000000000002")
INPUT_ID = UUID("74000000-0000-0000-0000-000000000003")
OUTPUT_ID = UUID("74000000-0000-0000-0000-000000000004")


def _revision(lifecycle="DRAFT"):
    return StrategyRevision(
        REVISION_ID,
        STRATEGY_ID,
        1,
        lifecycle,
        "FIXED_TICK",
        "Asia/Shanghai",
        {"nodes": [{"id": "input", "type": "inputNode"}], "edges": []},
        "a" * 64,
        7,
        (
            StrategyBindingDraft("INPUT", "soc", 0, INPUT_ID, "FLOAT", "%", 10),
            StrategyBindingDraft("OUTPUT", "power-target", 0, OUTPUT_ID, "FLOAT", "kW", 10),
        ),
        "engineer",
        NOW,
        None if lifecycle == "DRAFT" else "engineer",
        None if lifecycle == "DRAFT" else NOW,
    )


def _view(draft=True, enabled=False):
    revision = _revision("DRAFT" if draft else "PUBLISHED")
    return StrategyView(
        STRATEGY_ID,
        "2充2放",
        None,
        REVISION_ID if enabled else None,
        enabled,
        "READY",
        None,
        None,
        None,
        None,
        None,
        None,
        NOW,
        NOW,
        revision if draft else None,
        revision if enabled else None,
        revision if not draft else None,
    )


class _Repository:
    def __init__(self) -> None:
        self.view = _view()
        self.calls = []

    def current_configuration_revision(self):
        return 7

    def list_strategies(self):
        return (self.view,)

    def get_strategy(self, strategy_id):
        return self.view

    def create_strategy(self, draft, actor):
        self.calls.append(("create", draft, actor))
        return self.view

    def save_draft(self, strategy_id, draft, expected_digest, actor):
        self.calls.append(("save", expected_digest, draft, actor))
        return self.view

    def publish(self, strategy_id, expected_digest, expected_revision, actor):
        self.calls.append(("publish", expected_digest, expected_revision, actor))
        return replace(_revision("PUBLISHED"), published_by=actor, published_at=NOW)

    def enable(self, strategy_id, revision_id, actor):
        self.calls.append(("enable", revision_id, actor))
        self.view = _view(draft=False, enabled=True)
        return self.view

    def disable(self, strategy_id, actor):
        self.calls.append(("disable", actor))
        self.view = _view(draft=False, enabled=False)
        return self.view

    def clear_failure(self, strategy_id, actor):
        self.calls.append(("clear", actor))
        return self.view

    def list_events(self, strategy_id, before_at, before_id, limit):
        self.calls.append(("events", before_at, before_id, limit))
        return (
            (
                {
                    "id": UUID("74000000-0000-0000-0000-000000000010"),
                    "occurred_at": NOW,
                    "event_kind": "DECISION_CHANGED",
                    "trigger_kind": "FIXED_TICK",
                    "trigger_key": "tick:1",
                    "frame_sequence": 42,
                    "configuration_revision": 7,
                    "snapshot_evidence": {},
                    "decision": {"target": 156.7},
                    "intent_summary": [],
                    "control_command_id": None,
                    "control_status": None,
                    "reason_code": None,
                },
            ),
            False,
        )


class _Runtime:
    def __init__(self) -> None:
        self.calls = []

    def simulate(self, revision_id, overrides, evaluated_at):
        self.calls.append((revision_id, overrides, evaluated_at))
        input_value = overrides.get("soc", 49.0)
        sample = StrategyInput(
            "soc", INPUT_ID, input_value, "FLOAT", "%", "GOOD", NOW, 40, 7
        )
        intent = ControlIntentDraft("power-target", OUTPUT_ID, 156.7, 0)
        return EvaluationResult(
            "EVALUATED",
            None,
            StrategySnapshot(42, 7, NOW, (sample,)),
            {"soc": input_value, "site_local_minute": 630},
            StrategyEvaluation(
                ("discharge-1",),
                {"action_id": "power-target", "target": 156.7},
                (intent,),
            ),
            {"power-target": 156.7},
            {"power-target": 156.8},
            (intent,),
            (),
            False,
        )


class DispatchStrategyPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = _Repository()
        self.runtime = _Runtime()
        app = FastAPI()
        app.include_router(dispatch_strategies.router, prefix="/api/v1")
        app.dependency_overrides[
            dispatch_strategies.get_dispatch_strategy_repository
        ] = lambda: self.repository
        app.dependency_overrides[
            dispatch_strategies.get_dispatch_strategy_runtime
        ] = lambda: self.runtime
        self.app = app

    async def test_operator_can_read_and_simulate_but_cannot_mutate(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            listed = await client.get("/api/v1/dispatch-strategies")
            token = await client._bearer("operator")
            simulated = await client._client.post(
                f"/api/v1/dispatch-strategies/{STRATEGY_ID}/simulate",
                json={"overrides": {"soc": 51.0}},
                headers={"Authorization": token},
            )
            forbidden = await client._client.post(
                "/api/v1/dispatch-strategies",
                json={"name": "blocked"},
                headers={"Authorization": token},
            )

        self.assertEqual(200, listed.status_code, listed.text)
        self.assertEqual(200, simulated.status_code, simulated.text)
        self.assertEqual(403, forbidden.status_code, forbidden.text)
        self.assertEqual(51.0, simulated.json()["snapshot"]["soc"]["value"])
        self.assertEqual("discharge-1", simulated.json()["matched_rules"][0])
        self.assertEqual([], self.repository.calls)

    async def test_engineer_can_create_update_publish_enable_disable_and_clear(self) -> None:
        draft_body = {
            "expected_digest": "a" * 64,
            "name": "2充2放",
            "trigger_kind": "FIXED_TICK",
            "site_timezone": "Asia/Shanghai",
            "base_configuration_revision": 7,
            "jdm_content": _revision().jdm_content,
            "bindings": [
                {
                    "direction": "INPUT",
                    "binding_key": "soc",
                    "ordinal": 0,
                    "entity_instance_id": str(INPUT_ID),
                    "expected_data_type": "FLOAT",
                    "unit": "%",
                    "freshness_seconds": 10,
                },
                {
                    "direction": "OUTPUT",
                    "binding_key": "power-target",
                    "ordinal": 0,
                    "entity_instance_id": str(OUTPUT_ID),
                    "expected_data_type": "FLOAT",
                    "unit": "kW",
                    "freshness_seconds": 10,
                },
            ],
        }
        async with AuthenticatedApiClient(self.app) as client:
            created = await client.post(
                "/api/v1/dispatch-strategies", json={"name": "2充2放"}
            )
            saved = await client._request(
                "PUT", f"/api/v1/dispatch-strategies/{STRATEGY_ID}/draft", json=draft_body
            )
            published = await client.post(
                f"/api/v1/dispatch-strategies/{STRATEGY_ID}/publish",
                json={"expected_digest": "a" * 64, "configuration_revision": 7},
            )
            enabled = await client.post(
                f"/api/v1/dispatch-strategies/{STRATEGY_ID}/enable",
                json={"revision_id": str(REVISION_ID)},
            )
            disabled = await client.post(
                f"/api/v1/dispatch-strategies/{STRATEGY_ID}/disable"
            )
            cleared = await client.post(
                f"/api/v1/dispatch-strategies/{STRATEGY_ID}/failure-latch/clear"
            )

        self.assertTrue(all(response.status_code == 200 for response in (
            created, saved, published, enabled, disabled, cleared
        )), (created.text, saved.text, published.text, enabled.text, disabled.text, cleared.text))
        self.assertEqual(
            ["create", "save", "publish", "enable", "disable", "clear"],
            [item[0] for item in self.repository.calls],
        )

    async def test_events_are_bounded_and_legacy_routes_are_absent(self) -> None:
        async with AuthenticatedApiClient(self.app) as client:
            events = await client.get(
                f"/api/v1/dispatch-strategies/{STRATEGY_ID}/events?limit=10"
            )
            rules = await client.get("/api/v1/rules")
            templates = await client.get("/api/v1/rule-templates")

        self.assertEqual(200, events.status_code, events.text)
        self.assertEqual("DECISION_CHANGED", events.json()["items"][0]["event_kind"])
        self.assertEqual(404, rules.status_code)
        self.assertEqual(404, templates.status_code)

    async def test_disabled_published_revision_remains_visible_for_later_enable(self) -> None:
        self.repository.view = _view(draft=False, enabled=False)
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.get(f"/api/v1/dispatch-strategies/{STRATEGY_ID}")

        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(response.json()["active_revision"])
        self.assertEqual(str(REVISION_ID), response.json()["published_revision"]["id"])


if __name__ == "__main__":
    unittest.main()
