from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import unittest
from uuid import UUID, uuid4

from app.services.alarm_configuration import (
    AlarmConfigurationError,
    AlarmConfiguration,
    AlarmRule,
    ApplyAlarmConfigurationPlan,
    EntitySelection,
    InMemoryAlarmConfigurationRepository,
    LegacyAlarmSource,
    PlanAlarmConfiguration,
    ResolvedAlarmEntity,
    compile_legacy_migration_plan,
)


class UncheckedRepository(InMemoryAlarmConfigurationRepository):
    def save_rule_set_revision(self, *, key, name, rules, actor):
        raise AssertionError("service must reject invalid rules before repository")


def rule(rule_id: str, severity: str, trigger_operator: str, trigger_value: float, recovery_operator: str, recovery_value: float, unit: str | None = None) -> AlarmRule:
    return AlarmRule(
        id=rule_id,
        name=rule_id,
        severity=severity,
        trigger={"operator": trigger_operator, "value": trigger_value},
        trigger_duration_seconds=0,
        recovery={"operator": recovery_operator, "value": recovery_value},
        recovery_duration_seconds=0,
        notification_throttle_seconds=0,
        unit=unit,
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

    def test_rule_set_revisions_are_listed_in_stable_immutable_order(self) -> None:
        service, repository = configured_service(entity_count=1)
        first = service.create_rule_set(
            key="visible-rules",
            name="Visible rules",
            rules=(rule("major", "MAJOR", "gt", 80, "lte", 70),),
            actor="user:engineer",
        )
        second = service.create_rule_set_revision(
            rule_set_id=first.rule_set_id,
            rules=(rule("critical", "CRITICAL", "gt", 90, "lte", 80),),
            actor="user:engineer",
        )

        revisions = service.list_rule_set_revisions()

        self.assertEqual(revisions, (first, second))
        with self.assertRaises(TypeError):
            revisions[0].rules[0].trigger["value"] = 999

    def test_postgres_driver_integrity_failures_are_normalized(self) -> None:
        import psycopg2

        from app.services.alarm_configuration_postgres import (
            PostgresAlarmConfigurationRepository,
        )

        def failed_connection():
            raise psycopg2.IntegrityError("unexpected driver failure")

        repository = PostgresAlarmConfigurationRepository(
            connection_factory=failed_connection
        )
        with self.assertRaisesRegex(
            AlarmConfigurationError,
            "ALARM_CONFIGURATION_PERSISTENCE_FAILED",
        ):
            repository.get_plan(uuid4())
        with self.assertRaisesRegex(
            AlarmConfigurationError,
            "ALARM_RULE_SET_PERSISTENCE_FAILED",
        ):
            repository.save_rule_set_revision(
                key="broken",
                name="Broken",
                rules=(),
                actor="user:engineer",
            )

    def test_plan_requires_nonblank_planner_and_binds_it_to_the_digest(self) -> None:
        service, repository, revision = self._configuration(entity_count=1)
        with self.assertRaisesRegex(
            AlarmConfigurationError,
            "ALARM_PLAN_ACTOR_INVALID",
        ):
            service.plan(
                PlanAlarmConfiguration(
                    installation_id=repository.current_installation_id,
                    selection=EntitySelection(),
                    rule_set_id=revision.rule_set_id,
                    rule_set_revision=revision.revision,
                    planned_by=" ",
                )
            )
        first = service.plan(
            PlanAlarmConfiguration(
                installation_id=repository.current_installation_id,
                selection=EntitySelection(),
                rule_set_id=revision.rule_set_id,
                rule_set_revision=revision.revision,
                planned_by="user:planner-a",
            )
        )
        second = service.plan(
            PlanAlarmConfiguration(
                installation_id=repository.current_installation_id,
                selection=EntitySelection(),
                rule_set_id=revision.rule_set_id,
                rule_set_revision=revision.revision,
                planned_by="user:planner-b",
            )
        )
        self.assertEqual("user:planner-a", first.planned_by)
        self.assertNotEqual(first.digest, second.digest)

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
                planned_by="user:engineer",
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
                planned_by="user:engineer",
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
                planned_by="user:engineer",
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
            service.plan(PlanAlarmConfiguration(repository.current_installation_id, EntitySelection(entity_instance_ids=repository.entity_ids), revision.rule_set_id, revision.revision, "user:engineer"))

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
            service.plan(PlanAlarmConfiguration(repository.current_installation_id, EntitySelection(entity_instance_ids=repository.entity_ids), revision.rule_set_id, revision.revision, "user:engineer"))

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
        plan = service.plan(PlanAlarmConfiguration(repository.current_installation_id, EntitySelection(), revision.rule_set_id, revision.revision, "user:engineer"))
        self.assertEqual("blocked", plan.status)
        self.assertEqual(3, len(plan.items))
        self.assertTrue(any(blocker["code"] == "ALARM_ENTITY_UNRESOLVED" for blocker in plan.blockers))

    def test_plan_sorts_entities_even_when_repository_returns_them_reversed(self) -> None:
        service, repository, revision = self._configuration()
        original = repository.resolve_entities
        repository.resolve_entities = lambda installation_id, selection: tuple(reversed(original(installation_id, selection)))
        plan = service.plan(PlanAlarmConfiguration(repository.current_installation_id, EntitySelection(), revision.rule_set_id, revision.revision, "user:engineer"))
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
        with self.assertRaises(TypeError):
            revision.rules[0].trigger.__ior__({"value": 999})


class AlarmConfigurationValidationTest(unittest.TestCase):
    def test_legacy_entity_candidates_compile_every_rule_and_block_incompatible_choices(self) -> None:
        installation_id = UUID(int=700)
        numeric = ResolvedAlarmEntity(
            id=UUID(int=701), device_instance_id=UUID(int=711),
            definition_id="pcs.activePower", display_name="PCS 一号有功功率",
            data_type="number", unit="kW", confirmation_id=UUID(int=721),
        )
        text = ResolvedAlarmEntity(
            id=UUID(int=702), device_instance_id=UUID(int=712),
            definition_id="pcs.statusText", display_name="PCS 二号状态文本",
            data_type="text", unit=None, confirmation_id=UUID(int=722),
        )
        source = LegacyAlarmSource(
            source_kind="entity_alarm_binding",
            source_key="legacy-high-power",
            display_name="旧版 PCS 高功率告警",
            entity_candidates=(numeric, text),
            level_code="error1",
            stored_severity=None,
            trigger_rules=(
                {"op": "active"},
                {"op": "gte", "threshold": 500},
            ),
        )

        preview = compile_legacy_migration_plan(
            installation_id=installation_id,
            sources=(source,),
            selections={},
            actor="user:engineer",
        ).items[0]
        proposals = {
            proposal.entity_instance_id: proposal
            for proposal in preview.proposed_rules
        }

        self.assertEqual(2, len(proposals[numeric.id].proposed_definitions))
        self.assertEqual((), proposals[numeric.id].blockers)
        self.assertEqual(
            ["旧版 PCS 高功率告警（规则 1）", "旧版 PCS 高功率告警（规则 2）"],
            [item.name for item in proposals[numeric.id].proposed_definitions],
        )
        self.assertEqual(2, len(proposals[text.id].proposed_definitions))
        self.assertIn(
            "ALARM_LEGACY_RULE_UNSUPPORTED",
            {blocker["code"] for blocker in proposals[text.id].blockers},
        )

        selected_bad = compile_legacy_migration_plan(
            installation_id=installation_id,
            sources=(source,),
            selections={(source.source_kind, source.source_key): text.id},
            actor="user:engineer",
        ).items[0]
        self.assertEqual("blocked", selected_bad.status)
        self.assertIn(
            "ALARM_LEGACY_RULE_UNSUPPORTED",
            {blocker["code"] for blocker in selected_bad.blockers},
        )
        self.assertEqual((), selected_bad.definitions)

    def test_invalid_entity_and_rule_inputs_block_the_plan_without_applying(self) -> None:
        def entity(*, data_type: str = "number", unit: str | None = "kW", confirmed: bool = True) -> ResolvedAlarmEntity:
            return ResolvedAlarmEntity(
                id=UUID(int=1), device_instance_id=UUID(int=101), definition_id="pcs.activePower",
                display_name="PCS-1", data_type=data_type, unit=unit,
                confirmation_id=UUID(int=1001) if confirmed else None,
            )

        def unconfirmed_entity() -> ResolvedAlarmEntity:
            return entity(confirmed=False)

        def unsafe_hysteresis_rule() -> AlarmRule:
            return rule("x", "MAJOR", "gt", 10, "lte", 10, unit="kW")

        cases = (
            ((unconfirmed_entity(),), rule("x", "MAJOR", "gt", 10, "lte", 9, unit="kW"), "ALARM_ENTITY_UNRESOLVED"),
            ((entity(data_type="STRING"),), rule("x", "MAJOR", "gt", 10, "lte", 9, unit="kW"), "ALARM_DATA_TYPE_UNSUPPORTED"),
            ((entity(unit="kW"),), rule("x", "MAJOR", "gt", 10, "lte", 9, unit="degC"), "ALARM_UNIT_MISMATCH"),
            ((entity(),), unsafe_hysteresis_rule(), "ALARM_THRESHOLD_INVALID"),
        )
        for entities, alarm_rule, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                installation_id = uuid4()
                repository = InMemoryAlarmConfigurationRepository(installation_id=installation_id, entities=entities)
                service = AlarmConfiguration(repository)
                revision = service.create_rule_set(key="validation", name="Validation", rules=(alarm_rule,), actor="user:engineer")
                plan = service.plan(PlanAlarmConfiguration(installation_id, EntitySelection(), revision.rule_set_id, revision.revision, "user:engineer"))
                self.assertEqual("blocked", plan.status)
                self.assertIn(expected_code, [blocker["code"] for blocker in plan.blockers])
                self.assertEqual(0, repository.applied_count)


class AlarmConfigurationApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.repository = configured_service(entity_count=1)
        self.revision = self.service.create_rule_set(
            key="pcs-power-limits", name="PCS 功率分级",
            rules=(rule("warning", "WARNING", "gt", 450, "lte", 430, unit="kW"),), actor="user:engineer",
        )

    def ready_plan(self):
        return self.service.plan(PlanAlarmConfiguration(
            self.repository.current_installation_id, EntitySelection(), self.revision.rule_set_id, self.revision.revision, "user:engineer",
        ))

    def test_apply_is_atomic_and_same_key_returns_same_derived_installation(self) -> None:
        plan = self.ready_plan()
        first = self.service.apply(ApplyAlarmConfigurationPlan(
            plan_id=plan.id, plan_digest=plan.digest, idempotency_key="alarm-plan-1", actor="user:engineer"))
        replay = self.service.apply(ApplyAlarmConfigurationPlan(
            plan_id=plan.id, plan_digest=plan.digest, idempotency_key="alarm-plan-1", actor="user:engineer"))
        self.assertEqual(first, replay)
        self.assertEqual(1, self.repository.applied_count)
        self.assertEqual(plan.base_site_configuration_version + 1, first.site_configuration_version)

    def test_apply_persists_plan_lifecycle_without_changing_planner(self) -> None:
        plan = self.ready_plan()
        result = self.service.apply(
            ApplyAlarmConfigurationPlan(
                plan.id,
                plan.digest,
                "lifecycle",
                "user:applier",
            )
        )
        persisted = self.repository.get_plan(plan.id)
        self.assertIsNotNone(persisted)
        self.assertEqual("applied", persisted.status)
        self.assertEqual(result, persisted.applied_result)
        self.assertEqual("user:engineer", persisted.planned_by)

    def test_repository_rejects_blank_apply_actor_before_any_write(self) -> None:
        plan = self.ready_plan()
        with self.assertRaisesRegex(
            AlarmConfigurationError,
            "ALARM_APPLY_COMMAND_INVALID",
        ):
            self.repository.apply_plan(
                plan,
                idempotency_key="blank-actor",
                actor=" ",
            )
        self.assertEqual(0, self.repository.applied_count)

    def test_same_idempotency_key_is_scoped_by_actor(self) -> None:
        first_plan = self.ready_plan()
        first = self.service.apply(
            ApplyAlarmConfigurationPlan(
                first_plan.id,
                first_plan.digest,
                "shared-key",
                "user:applier-a",
            )
        )
        second_revision = self.service.create_rule_set_revision(
            rule_set_id=self.revision.rule_set_id,
            rules=(rule("major", "MAJOR", "gt", 500, "lte", 470, unit="kW"),),
            actor="user:engineer",
        )
        second_plan = self.service.plan(
            PlanAlarmConfiguration(
                self.repository.current_installation_id,
                EntitySelection(),
                second_revision.rule_set_id,
                second_revision.revision,
                "user:planner-b",
            )
        )
        second = self.service.apply(
            ApplyAlarmConfigurationPlan(
                second_plan.id,
                second_plan.digest,
                "shared-key",
                "user:applier-b",
            )
        )
        self.assertNotEqual(first.installation_id, second.installation_id)
        self.assertEqual(
            first,
            self.repository.find_idempotency("user:applier-a", "shared-key")[3],
        )
        self.assertEqual(
            second,
            self.repository.find_idempotency("user:applier-b", "shared-key")[3],
        )

    def test_apply_rejects_a_reused_key_for_a_different_request(self) -> None:
        plan = self.ready_plan()
        self.service.apply(ApplyAlarmConfigurationPlan(plan.id, plan.digest, "alarm-plan-1", "user:engineer"))
        replacement_revision = self.service.create_rule_set_revision(
            rule_set_id=self.revision.rule_set_id,
            rules=(rule("major", "MAJOR", "gt", 500, "lte", 470, unit="kW"),), actor="user:engineer",
        )
        replacement = self.service.plan(PlanAlarmConfiguration(
            self.repository.current_installation_id, EntitySelection(), replacement_revision.rule_set_id, replacement_revision.revision, "user:engineer",
        ))
        with self.assertRaisesRegex(AlarmConfigurationError, "IDEMPOTENCY_KEY_REUSED"):
            self.service.apply(ApplyAlarmConfigurationPlan(replacement.id, replacement.digest, "alarm-plan-1", "user:engineer"))

    def test_repository_apply_plan_is_the_public_atomic_apply_seam(self) -> None:
        plan = self.ready_plan()
        first = self.repository.apply_plan(plan, idempotency_key="repository-key", actor="user:engineer")
        replay = self.repository.apply_plan(plan, idempotency_key="repository-key", actor="user:engineer")
        self.assertEqual(first, replay)
        self.assertEqual(1, self.repository.applied_count)

    def test_apply_rejects_stale_plan_without_writes(self) -> None:
        plan = self.ready_plan()
        self.repository._site_version += 1
        with self.assertRaisesRegex(AlarmConfigurationError, "ALARM_PLAN_STALE"):
            self.service.apply(ApplyAlarmConfigurationPlan(plan.id, plan.digest, "stale-plan", "user:engineer"))
        self.assertEqual(0, self.repository.applied_count)

    def test_apply_rejects_wrong_digest_without_writes(self) -> None:
        plan = self.ready_plan()
        with self.assertRaisesRegex(AlarmConfigurationError, "ALARM_PLAN_DIGEST_MISMATCH"):
            self.service.apply(ApplyAlarmConfigurationPlan(plan.id, "wrong", "wrong-digest", "user:engineer"))
        self.assertEqual(0, self.repository.applied_count)

    def test_audit_failure_rolls_back_all_apply_mutations(self) -> None:
        plan = self.ready_plan()
        before = {
            "definitions": deepcopy(self.repository._definitions),
            "current_pointers": deepcopy(self.repository._current_pointers),
            "site_version": self.repository._site_version,
            "audit_events": deepcopy(self.repository._audit_events),
            "idempotency": deepcopy(self.repository._idempotency),
            "derived_installation_id": self.repository._derived_installation_id,
        }
        self.repository.fail_audit = True
        with self.assertRaisesRegex(AlarmConfigurationError, "ALARM_AUDIT_FAILED"):
            self.service.apply(ApplyAlarmConfigurationPlan(plan.id, plan.digest, "audit-failure", "user:engineer"))
        self.assertEqual(before["definitions"], self.repository._definitions)
        self.assertEqual(before["current_pointers"], self.repository._current_pointers)
        self.assertEqual(before["site_version"], self.repository._site_version)
        self.assertEqual(before["audit_events"], self.repository._audit_events)
        self.assertEqual(before["idempotency"], self.repository._idempotency)
        self.assertEqual(before["derived_installation_id"], self.repository._derived_installation_id)
        self.assertEqual(0, self.repository.applied_count)

    def test_revision_removal_is_a_stably_sorted_delete_candidate_preview(self) -> None:
        first = self.ready_plan()
        self.service.apply(ApplyAlarmConfigurationPlan(first.id, first.digest, "first-revision", "user:engineer"))
        second_revision = self.service.create_rule_set_revision(
            rule_set_id=self.revision.rule_set_id,
            rules=(rule("major", "MAJOR", "gt", 500, "lte", 470, unit="kW"),),
            actor="user:engineer",
        )
        second = self.service.plan(PlanAlarmConfiguration(
            self.repository.current_installation_id, EntitySelection(), second_revision.rule_set_id, second_revision.revision, "user:engineer",
        ))
        deletes = [item for item in second.items if item.action == "delete_candidate"]
        self.assertEqual(1, len(deletes))
        self.assertEqual("warning", deletes[0].rule_id)
        self.assertIsNotNone(deletes[0].before)
        self.assertIsNone(deletes[0].after)
        self.assertEqual([item.definition_key for item in second.items], sorted(item.definition_key for item in second.items))
        self.assertNotEqual(first.digest, second.digest)

    def test_concurrent_same_key_replays_one_atomic_apply(self) -> None:
        class RacingRepository(InMemoryAlarmConfigurationRepository):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.barrier = Barrier(2)

            def apply_plan(self, plan, *, idempotency_key, actor):
                self.barrier.wait(timeout=2)
                return super().apply_plan(plan, idempotency_key=idempotency_key, actor=actor)

        installation_id = uuid4()
        entity = ResolvedAlarmEntity(UUID(int=1), UUID(int=101), "pcs.activePower", "PCS-1", "number", "kW", UUID(int=1001))
        repository = RacingRepository(installation_id=installation_id, entities=(entity,), site_version=7)
        service = AlarmConfiguration(repository)
        revision = service.create_rule_set(
            key="pcs-power-limits", name="PCS 功率分级",
            rules=(rule("warning", "WARNING", "gt", 450, "lte", 430, unit="kW"),), actor="user:engineer",
        )
        plan = service.plan(PlanAlarmConfiguration(installation_id, EntitySelection(), revision.rule_set_id, revision.revision, "user:engineer"))
        command = ApplyAlarmConfigurationPlan(plan.id, plan.digest, "concurrent-key", "user:engineer")
        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = list(workers.map(lambda _: service.apply(command), range(2)))
        self.assertEqual(first, second)
        self.assertEqual(1, repository.applied_count)
        self.assertEqual(8, repository._site_version)
        self.assertEqual(1, len(repository._audit_events))
        self.assertEqual(1, len(repository._idempotency))
        self.assertEqual(1, len({first.installation_id, second.installation_id}))


if __name__ == "__main__":
    unittest.main()
