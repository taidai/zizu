from __future__ import annotations

import unittest
from uuid import uuid4

from app.services.alarm_configuration import (
    AlarmConfiguration,
    AlarmConfigurationError,
    AlarmRule,
    AlarmRuleSetRevision,
    EntitySelection,
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


if __name__ == "__main__":
    unittest.main()
