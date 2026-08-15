from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from app.services.alarm_configuration import (
    AlarmConfiguration,
    AlarmRule,
    EntitySelection,
    InMemoryAlarmConfigurationRepository,
    PlanAlarmConfiguration,
    ResolvedAlarmEntity,
)


def rule(rule_id: str, severity: str, trigger_operator: str, trigger_value: float, recovery_operator: str, recovery_value: float) -> AlarmRule:
    return AlarmRule(
        id=rule_id,
        name=rule_id,
        severity=severity,
        trigger={"operator": trigger_operator, "value": trigger_value},
        trigger_duration_seconds=0,
        recovery={"operator": recovery_operator, "value": recovery_value},
        recovery_duration_seconds=0,
        notification_throttle_seconds=0,
    )


def configured_service(entity_count: int = 4) -> tuple[AlarmConfiguration, InMemoryAlarmConfigurationRepository]:
    installation_id = uuid4()
    entities = tuple(
        ResolvedAlarmEntity(
            id=UUID(int=index + 1),
            device_instance_id=UUID(int=100 + index),
            definition_id="pcs.activePower",
            display_name=f"PCS-{index + 1}",
            data_type="number",
            unit="kW",
            confirmation_id=UUID(int=1000 + index),
        )
        for index in range(entity_count)
    )
    repository = InMemoryAlarmConfigurationRepository(
        installation_id=installation_id,
        entities=entities,
        site_version=7,
    )
    return AlarmConfiguration(repository), repository


class AlarmConfigurationPlanTest(unittest.TestCase):
    def test_four_entities_and_three_rules_expand_to_twelve_stable_definitions(self) -> None:
        service, repository = configured_service(entity_count=4)
        revision = repository.save_rule_set_revision(
            key="pcs-power-limits",
            name="PCS 功率分级",
            rules=(
                rule("warning", "WARNING", "gt", 450, "lte", 430),
                rule("major", "MAJOR", "gt", 500, "lte", 470),
                rule("critical", "CRITICAL", "gt", 550, "lte", 500),
            ),
            actor="user:engineer",
        )

        plan = service.plan(
            PlanAlarmConfiguration(
                installation_id=repository.current_installation_id,
                selection=EntitySelection(
                    entity_instance_ids=repository.entity_ids,
                    device_instance_ids=(),
                    entity_definition_ids=(),
                ),
                rule_set_id=revision.rule_set_id,
                rule_set_revision=revision.revision,
            )
        )

        self.assertEqual(12, len(plan.items))
        self.assertEqual(12, len({item.definition_key for item in plan.items}))
        self.assertEqual("ready", plan.status)
        self.assertEqual([], list(plan.blockers))
        self.assertTrue(all(item.action == "add" for item in plan.items))

    def test_reordered_entities_and_rules_have_same_digest_and_keys(self) -> None:
        service, repository = configured_service(entity_count=4)
        revision = repository.save_rule_set_revision(
            key="pcs-power-limits",
            name="PCS 功率分级",
            rules=(
                rule("warning", "WARNING", "gt", 450, "lte", 430),
                rule("major", "MAJOR", "gt", 500, "lte", 470),
                rule("critical", "CRITICAL", "gt", 550, "lte", 500),
            ),
            actor="user:engineer",
        )
        first = service.plan(
            PlanAlarmConfiguration(
                installation_id=repository.current_installation_id,
                selection=EntitySelection(entity_instance_ids=repository.entity_ids),
                rule_set_id=revision.rule_set_id,
                rule_set_revision=revision.revision,
            )
        )
        reversed_revision = repository.save_rule_set_revision(
            key="pcs-power-limits",
            name="PCS 功率分级",
            rules=tuple(reversed(revision.rules)),
            actor="user:engineer",
        )
        second = service.plan(
            PlanAlarmConfiguration(
                installation_id=repository.current_installation_id,
                selection=EntitySelection(entity_instance_ids=tuple(reversed(repository.entity_ids))),
                rule_set_id=reversed_revision.rule_set_id,
                rule_set_revision=reversed_revision.revision,
            )
        )
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(
            [item.definition_key for item in first.items],
            [item.definition_key for item in second.items],
        )


if __name__ == "__main__":
    unittest.main()
