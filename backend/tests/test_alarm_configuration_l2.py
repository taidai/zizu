from __future__ import annotations

import unittest
from uuid import uuid4

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


if __name__ == "__main__":
    unittest.main()
