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


class UncheckedRepository(InMemoryAlarmConfigurationRepository):
    def save_rule_set_revision(self, *, key, name, rules, actor):
        raise AssertionError("service must reject invalid rules before repository")


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
    def _configuration(self, *, entity_count: int = 4):
        service, repository = configured_service(entity_count=entity_count)
        revision = repository.save_rule_set_revision(
            key="pcs-power-limits",
            name="PCS 功率分级",
            rules=tuple(rule(f"rule-{index}", "WARNING", "gt", index, "lte", index - 1) for index in range(3)),
            actor="user:engineer",
        )
        return service, repository, revision

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

    def test_rejects_more_than_two_hundred_entities(self) -> None:
        service, repository, revision = self._configuration(entity_count=201)
        with self.assertRaisesRegex(ValueError, "200"):
            service.plan(PlanAlarmConfiguration(repository.current_installation_id, EntitySelection(entity_instance_ids=repository.entity_ids), revision.rule_set_id, revision.revision))

    def test_rejects_more_than_twenty_rules(self) -> None:
        service, repository = configured_service()
        rules = tuple(rule(f"rule-{index}", "WARNING", "gt", index, "lte", index - 1) for index in range(21))
        with self.assertRaisesRegex(ValueError, "20"):
            service.create_rule_set(key="too-many", name="Too many", rules=rules, actor="user:engineer")

    def test_rejects_more_than_two_thousand_expanded_definitions(self) -> None:
        service, repository = configured_service(entity_count=200)
        rules = tuple(rule(f"rule-{index}", "WARNING", "gt", index, "lte", index - 1) for index in range(11))
        revision = repository.save_rule_set_revision(key="too-many-expanded", name="Too many", rules=rules, actor="user:engineer")
        with self.assertRaisesRegex(ValueError, "2000"):
            service.plan(PlanAlarmConfiguration(repository.current_installation_id, EntitySelection(entity_instance_ids=repository.entity_ids), revision.rule_set_id, revision.revision))

    def test_rejects_empty_duplicate_and_invalid_rule_values(self) -> None:
        service, _ = configured_service()
        cases = (
            (rule("", "WARNING", "gt", 1, "lte", 0), "non-empty"),
            ((rule("same", "WARNING", "gt", 1, "lte", 0), rule("same", "MAJOR", "gt", 2, "lte", 1)), "unique"),
            (rule("bad", "INVALID", "gt", 1, "lte", 0), "severity"),
        )
        for invalid_rules, expected in cases:
            rules = invalid_rules if isinstance(invalid_rules, tuple) else (invalid_rules,)
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                service.create_rule_set(key=f"invalid-{expected}", name="Invalid", rules=rules, actor="user:engineer")

    def test_blocks_unconfirmed_entities_without_expanding_them(self) -> None:
        service, repository, revision = self._configuration()
        repository._entities = (repository._entities[0], ResolvedAlarmEntity(
            id=UUID(int=999), device_instance_id=UUID(int=999), definition_id="pcs.activePower",
            display_name="unconfirmed", data_type="number", unit="kW", confirmation_id=None,
        ))
        plan = service.plan(PlanAlarmConfiguration(repository.current_installation_id, EntitySelection(), revision.rule_set_id, revision.revision))
        self.assertEqual("blocked", plan.status)
        self.assertEqual(3, len(plan.items))
        self.assertTrue(any(blocker["code"] == "UNCONFIRMED_ENTITY" for blocker in plan.blockers))

    def test_plan_sorts_entities_even_when_repository_returns_them_reversed(self) -> None:
        service, repository, revision = self._configuration()
        original = repository.resolve_entities
        repository.resolve_entities = lambda installation_id, selection: tuple(reversed(original(installation_id, selection)))
        plan = service.plan(PlanAlarmConfiguration(repository.current_installation_id, EntitySelection(), revision.rule_set_id, revision.revision))
        self.assertEqual(sorted(item.entity_instance_id for item in plan.items), [item.entity_instance_id for item in plan.items])

    def test_rule_revision_defensively_copies_nested_conditions(self) -> None:
        service, _ = configured_service()
        trigger = {"operator": "gt", "value": 450}
        created = service.create_rule_set(key="immutable", name="Immutable", rules=(AlarmRule(
            id="warning", name="Warning", severity="WARNING", trigger=trigger,
            trigger_duration_seconds=0, recovery={"operator": "lte", "value": 430},
            recovery_duration_seconds=0, notification_throttle_seconds=0,
        ),), actor="user:engineer")
        trigger["value"] = 999
        self.assertEqual(450, created.rules[0].trigger["value"])

    def test_service_seam_validates_rules_even_when_repository_does_not(self) -> None:
        service, _ = configured_service()
        service.repository = UncheckedRepository(installation_id=uuid4(), entities=())
        invalid = (
            (rule("", "WARNING", "gt", 1, "lte", 0),),
            (rule("same", "WARNING", "gt", 1, "lte", 0), rule("same", "MAJOR", "gt", 2, "lte", 1)),
            (rule("bad", "INVALID", "gt", 1, "lte", 0),),
        )
        for rules in invalid:
            with self.assertRaises(ValueError):
                service.create_rule_set(key="service-check", name="Invalid", rules=rules, actor="user:engineer")

    def test_nested_revision_conditions_are_truly_immutable(self) -> None:
        service, _ = configured_service()
        revision = service.create_rule_set(
            key="frozen", name="Frozen", rules=(rule("warning", "WARNING", "gt", 450, "lte", 430),), actor="user:engineer"
        )
        with self.assertRaises(TypeError):
            revision.rules[0].trigger["value"] = 999


if __name__ == "__main__":
    unittest.main()
