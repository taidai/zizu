from __future__ import annotations

import os
import unittest
from uuid import uuid4

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

from fastapi import FastAPI

from tests.api_test_client import AuthenticatedApiClient

from app.services.alarm_configuration import (
    AlarmConfiguration,
    AlarmConfigurationError,
    AlarmRule,
    AlarmRuleSetRevision,
    EntitySelection,
    ApplyAlarmConfigurationPlan,
    PlanAlarmConfiguration,
    ResolvedAlarmEntity,
    canonical_digest,
)


class _Repository:
    def __init__(self) -> None:
        self.node_id = uuid4()
        self.entity = ResolvedAlarmEntity(
            uuid4(), self.node_id, "pcs.activePower", "有功功率", "FLOAT", "kW"
        )
        rule = AlarmRule(
            "high", "功率越限", "MAJOR", {"operator": "gt", "value": 90}, 1,
            {"operator": "lt", "value": 85}, 1, 60, "kW",
        )
        self.rule_set = AlarmRuleSetRevision(
            uuid4(), "pcs-power", "PCS 功率", 1, (rule,), canonical_digest(rule)
        )
        self.saved = None
        self.applied = object()
        self.apply_calls = 0

    def get_rule_set_revision(self, rule_set_id, revision):
        return self.rule_set if (rule_set_id, revision) == (self.rule_set.rule_set_id, 1) else None

    def resolve_entities(self, selection):
        if selection.entity_instance_ids and self.entity.id not in selection.entity_instance_ids:
            return ()
        if selection.node_ids and self.node_id not in selection.node_ids:
            return ()
        return (self.entity,)

    def current_configuration_revision(self):
        return 7

    def current_configuration(self):
        return {"configuration_revision": 7, "definitions": {}}

    def save_plan(self, plan):
        self.saved = plan
        return plan

    def get_plan(self, plan_id):
        return self.saved if self.saved is not None and self.saved.id == plan_id else None

    def apply_plan(self, plan, *, idempotency_key, actor):
        self.apply_calls += 1
        return self.applied


class _RuntimeGate:
    def __init__(self) -> None:
        self.calls = []

    def begin_configuration_publish(self, revision):
        self.calls.append(("begin", revision))

    def cancel_configuration_publish(self):
        self.calls.append(("cancel",))

    def reconcile_configuration_runtime(self):
        self.calls.append(("reconcile",))


class AlarmConfigurationL2Test(unittest.TestCase):
    def test_plan_targets_active_l2_entity_and_current_revision(self) -> None:
        repository = _Repository()
        plan = AlarmConfiguration(repository).plan(
            PlanAlarmConfiguration(
                EntitySelection(node_ids=(repository.node_id,)),
                repository.rule_set.rule_set_id,
                1,
                "operator:test",
            )
        )
        self.assertEqual(plan.base_configuration_revision, 7)
        self.assertEqual(plan.items[0].entity_instance_id, repository.entity.id)
        self.assertEqual(plan.items[0].action, "add")

    def test_plan_rejects_selection_without_l2_entity(self) -> None:
        repository = _Repository()
        with self.assertRaisesRegex(AlarmConfigurationError, "ALARM_ENTITY_UNRESOLVED"):
            AlarmConfiguration(repository).plan(
                PlanAlarmConfiguration(
                    EntitySelection(entity_instance_ids=(uuid4(),)),
                    repository.rule_set.rule_set_id,
                    1,
                    "operator:test",
                )
            )

    def test_apply_drains_old_frames_then_reconciles_new_revision(self) -> None:
        repository = _Repository()
        plan = AlarmConfiguration(repository).plan(
            PlanAlarmConfiguration(
                EntitySelection(node_ids=(repository.node_id,)),
                repository.rule_set.rule_set_id,
                1,
                "operator:test",
            )
        )
        gate = _RuntimeGate()
        result = AlarmConfiguration(repository, runtime_gate=gate).apply(
            ApplyAlarmConfigurationPlan(
                plan.id,
                plan.digest,
                "alarm-apply-1",
                "operator:test",
            )
        )

        self.assertIs(repository.applied, result)
        self.assertEqual([("begin", 7), ("reconcile",)], gate.calls)

    def test_code_set_membership_rule_can_only_bind_a_code_set_entity(self) -> None:
        repository = _Repository()
        rule = AlarmRule(
            "e30",
            "压缩机故障",
            "MAJOR",
            {"operator": "contains", "value": "E30"},
            0,
            {"operator": "not_contains", "value": "E30"},
            3,
            60,
        )
        repository.rule_set = AlarmRuleSetRevision(
            repository.rule_set.rule_set_id,
            "pcs-fault-codes",
            "PCS 故障码",
            1,
            (rule,),
            canonical_digest(rule),
        )
        repository.entity = ResolvedAlarmEntity(
            repository.entity.id,
            repository.node_id,
            "pcs.faultCodes",
            "故障码",
            "CODE_SET",
            None,
        )

        compatible = AlarmConfiguration(repository).plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(repository.entity.id,)),
                repository.rule_set.rule_set_id,
                1,
                "operator:test",
            )
        )
        self.assertEqual("ready", compatible.status)
        self.assertEqual("add", compatible.items[0].action)

        repository.entity = ResolvedAlarmEntity(
            repository.entity.id,
            repository.node_id,
            "pcs.activePower",
            "有功功率",
            "FLOAT",
            "kW",
        )
        incompatible = AlarmConfiguration(repository).plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(repository.entity.id,)),
                repository.rule_set.rule_set_id,
                1,
                "operator:test",
            )
        )
        self.assertEqual("blocked", incompatible.status)
        self.assertEqual("ALARM_DATA_TYPE_UNSUPPORTED", incompatible.blockers[0]["code"])

    def test_trial_matches_a_good_value_without_persisting_configuration(self) -> None:
        repository = _Repository()
        rule = AlarmRule(
            "high",
            "功率越限",
            "MAJOR",
            {"operator": "gt", "value": 90},
            3,
            {"operator": "lte", "value": 85},
            3,
            60,
            "kW",
        )

        result = AlarmConfiguration(repository).trial(
            entity_instance_id=repository.entity.id,
            rule=rule,
            value=95,
            quality=192,
        )

        self.assertTrue(result.trigger_matches)
        self.assertFalse(result.recovery_matches)
        self.assertIn("命中触发条件", result.description)
        self.assertIsNone(repository.saved)
        self.assertEqual(0, repository.apply_calls)

    def test_trial_fails_closed_when_l2_quality_is_not_good(self) -> None:
        repository = _Repository()

        result = AlarmConfiguration(repository).trial(
            entity_instance_id=repository.entity.id,
            rule=repository.rule_set.rules[0],
            value=100,
            quality=64,
        )

        self.assertFalse(result.trigger_matches)
        self.assertFalse(result.recovery_matches)
        self.assertIn("质量非 GOOD", result.description)


class AlarmConfigurationPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_trial_endpoint_returns_a_result_without_creating_a_plan(self) -> None:
        from app.api.alarm_configurations import get_alarm_configuration, router

        repository = _Repository()
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_alarm_configuration] = lambda: AlarmConfiguration(repository)

        async with AuthenticatedApiClient(app) as client:
            response = await client.post(
                "/api/v1/alarm-configurations/trials",
                json={
                    "entity_instance_id": str(repository.entity.id),
                    "rule": {
                        "id": "high",
                        "name": "功率越限",
                        "severity": "MAJOR",
                        "trigger": {"operator": "gt", "value": 90},
                        "trigger_duration_seconds": 3,
                        "recovery": {"operator": "lte", "value": 85},
                        "recovery_duration_seconds": 3,
                        "notification_throttle_seconds": 60,
                        "unit": "kW",
                    },
                    "value": 95,
                    "quality": 192,
                },
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()["trigger_matches"])
        self.assertFalse(response.json()["recovery_matches"])
        self.assertIsNone(repository.saved)
        self.assertEqual(0, repository.apply_calls)


if __name__ == "__main__":
    unittest.main()
