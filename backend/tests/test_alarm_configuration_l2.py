from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import os
from threading import Event
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
    AppliedAlarmConfiguration,
    EntitySelection,
    ApplyAlarmConfigurationPlan,
    PlanAlarmConfiguration,
    ResolvedAlarmEntity,
    canonical_digest,
)
from app.services.data_trunk_contracts import DataTrunkError


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
        self.http_notification_states = {}

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

    def http_notification_status(self, config_id):
        return self.http_notification_states.get(config_id)


class _RuntimeGate:
    def __init__(self) -> None:
        self.calls = []

    def begin_configuration_publish(self, revision):
        self.calls.append(("begin", revision))

    def cancel_configuration_publish(self):
        self.calls.append(("cancel",))

    def reconcile_configuration_runtime(self):
        self.calls.append(("reconcile",))


class _AwaitingRuntimeGate(_RuntimeGate):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def begin_configuration_publish(self, revision):
        super().begin_configuration_publish(revision)
        self.entered.set()
        if not self.release.wait(timeout=0.2):
            raise DataTrunkError(
                "CONFIGURATION_RUNTIME_DRAIN_TIMEOUT",
                "CONFIGURATION_RUNTIME_DRAIN_TIMEOUT",
            )


class AlarmConfigurationL2Test(unittest.TestCase):
    def test_old_ready_plan_cannot_bypass_new_condition_validation(self) -> None:
        repository = _Repository()
        service = AlarmConfiguration(repository)
        plan = service.plan(PlanAlarmConfiguration(
            EntitySelection(entity_instance_ids=(repository.entity.id,)),
            repository.rule_set.rule_set_id, 1, "operator:test",
        ))
        legacy_rule = replace(plan.rule_set_revision.rules[0], recovery={"operator": "gt", "value": 90})
        repository.saved = replace(
            plan,
            rule_set_revision=replace(plan.rule_set_revision, rules=(legacy_rule,)),
            items=tuple(replace(item, after={**item.after, "rule": {
                **item.after["rule"], "recovery": legacy_rule.recovery,
            }}) for item in plan.items),
            digest=canonical_digest(legacy_rule),
        )
        command = ApplyAlarmConfigurationPlan(plan.id, repository.saved.digest, "legacy-plan", "operator:test")
        with self.assertRaisesRegex(AlarmConfigurationError, "ALARM_PLAN_BLOCKED"):
            service.apply(command)
        self.assertEqual(0, repository.apply_calls)

        # An already applied historical plan must still replay its saved result.
        repository.saved = replace(repository.saved, status="applied")
        self.assertIs(repository.applied, service.apply(command))
        self.assertEqual(1, repository.apply_calls)

    def test_identical_state_conditions_block_trial_and_publication(self) -> None:
        for value in (False, True):
            with self.subTest(value=value):
                repository = _Repository()
                repository.entity = replace(repository.entity, data_type="BOOL", unit=None)
                rule = replace(
                    repository.rule_set.rules[0],
                    trigger={"operator": "eq", "value": value},
                    recovery={"operator": "eq", "value": value},
                    unit=None,
                )
                repository.rule_set = replace(repository.rule_set, rules=(rule,))
                service = AlarmConfiguration(repository)

                with self.assertRaisesRegex(AlarmConfigurationError, "ALARM_CONDITIONS_IDENTICAL"):
                    service.trial(entity_instance_id=repository.entity.id, rule=rule, value=value, quality=192)
                plan = service.plan(PlanAlarmConfiguration(
                    EntitySelection(entity_instance_ids=(repository.entity.id,)),
                    repository.rule_set.rule_set_id, 1, "operator:test",
                ))
                self.assertEqual("blocked", plan.status)
                self.assertIn("ALARM_CONDITIONS_IDENTICAL", {item["code"] for item in plan.blockers})
                with self.assertRaisesRegex(AlarmConfigurationError, "ALARM_PLAN_BLOCKED"):
                    service.apply(ApplyAlarmConfigurationPlan(plan.id, plan.digest, "conflict-test", "operator:test"))
                self.assertEqual(0, repository.apply_calls)

    def test_opposite_state_conditions_remain_publishable_and_trialable(self) -> None:
        repository = _Repository()
        repository.entity = replace(repository.entity, data_type="BOOL", unit=None)
        rule = replace(
            repository.rule_set.rules[0],
            trigger={"operator": "eq", "value": True},
            recovery={"operator": "eq", "value": False},
            unit=None,
        )
        repository.rule_set = replace(repository.rule_set, rules=(rule,))
        service = AlarmConfiguration(repository)
        for value in (False, True):
            result = service.trial(entity_instance_id=repository.entity.id, rule=rule, value=value, quality=192)
            self.assertEqual(value, result.trigger_matches)
            self.assertEqual(not value, result.recovery_matches)
        plan = service.plan(PlanAlarmConfiguration(
            EntitySelection(entity_instance_ids=(repository.entity.id,)),
            repository.rule_set.rule_set_id, 1, "operator:test",
        ))
        self.assertEqual("ready", plan.status)

    def test_public_rule_contract_preserves_http_notification_binding(self) -> None:
        from app.api.alarm_configurations import AlarmRuleRequest, _error, _rule

        config_id = uuid4()
        request = AlarmRuleRequest.model_validate(
            {
                "id": "high",
                "name": "功率越限",
                "severity": "MAJOR",
                "trigger": {"operator": "gt", "value": 90},
                "trigger_duration_seconds": 1,
                "recovery": {"operator": "lt", "value": 85},
                "recovery_duration_seconds": 1,
                "notification_throttle_seconds": 60,
                "unit": "kW",
                "http_notification_config_id": str(config_id),
            }
        )

        rule = request.domain()
        self.assertEqual(config_id, rule.http_notification_config_id)
        self.assertEqual(str(config_id), _rule(rule)["http_notification_config_id"])
        self.assertEqual(
            409,
            _error(AlarmConfigurationError("HTTP_NOTIFICATION_DISABLED")).status_code,
        )

    def test_rule_without_http_notification_remains_valid(self) -> None:
        repository = _Repository()

        plan = AlarmConfiguration(repository).plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(repository.entity.id,)),
                repository.rule_set.rule_set_id,
                1,
                "operator:test",
            )
        )

        self.assertEqual("ready", plan.status)

    def test_plan_blocks_missing_disabled_or_stale_http_notification(self) -> None:
        for state, expected_code in (
            (None, "HTTP_NOTIFICATION_NOT_FOUND"),
            ((False, True), "HTTP_NOTIFICATION_DISABLED"),
            ((True, False), "HTTP_NOTIFICATION_TEST_STALE"),
        ):
            with self.subTest(state=state):
                repository = _Repository()
                config_id = uuid4()
                repository.http_notification_states[config_id] = state
                bound_rule = replace(
                    repository.rule_set.rules[0],
                    http_notification_config_id=config_id,
                )
                repository.rule_set = replace(
                    repository.rule_set,
                    rules=(bound_rule,),
                    digest=canonical_digest(bound_rule),
                )

                plan = AlarmConfiguration(repository).plan(
                    PlanAlarmConfiguration(
                        EntitySelection(
                            entity_instance_ids=(repository.entity.id,)
                        ),
                        repository.rule_set.rule_set_id,
                        1,
                        "operator:test",
                    )
                )

                self.assertEqual("blocked", plan.status)
                self.assertIn(
                    expected_code,
                    {item["code"] for item in plan.blockers},
                )
    def test_rule_group_summary_survives_an_empty_disable_revision(self) -> None:
        from app.services.alarm_configuration_postgres import _rule_groups

        repository = _Repository()
        enabled_revision = AlarmRuleSetRevision(
            repository.rule_set.rule_set_id,
            "pcs-fault-codes",
            "PCS 故障码",
            1,
            (
                AlarmRule(
                    "e30",
                    "压缩机故障",
                    "CRITICAL",
                    {"operator": "contains", "value": "E30"},
                    0,
                    {"operator": "not_contains", "value": "E30"},
                    3,
                    60,
                ),
            ),
            "digest-1",
        )
        disabled_revision = AlarmRuleSetRevision(
            repository.rule_set.rule_set_id,
            "pcs-fault-codes",
            "PCS 故障码",
            2,
            (),
            "digest-2",
        )

        groups = _rule_groups(
            (enabled_revision, disabled_revision),
            (
                (
                    f"alarm.pcs-fault-codes.{repository.entity.id}.e30",
                    repository.entity.id,
                    repository.node_id,
                    False,
                ),
            ),
        )

        self.assertEqual(1, len(groups))
        self.assertEqual(2, groups[0].latest_revision)
        self.assertEqual(1, groups[0].last_non_empty_revision)
        self.assertEqual((repository.entity.id,), groups[0].entity_instance_ids)
        self.assertEqual((), groups[0].enabled_entity_instance_ids)
        self.assertEqual(1, groups[0].device_count)
        self.assertEqual(1, groups[0].rule_count)
        self.assertEqual("CRITICAL", groups[0].highest_severity)

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
    async def test_apply_endpoint_keeps_event_loop_running_while_runtime_drains(self) -> None:
        from app.api.alarm_configurations import get_alarm_configuration, router

        repository = _Repository()
        gate = _AwaitingRuntimeGate()
        configuration = AlarmConfiguration(repository, runtime_gate=gate)
        plan = configuration.plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(repository.entity.id,)),
                repository.rule_set.rule_set_id,
                1,
                "operator:test",
            )
        )
        repository.applied = AppliedAlarmConfiguration(
            uuid4(),
            plan.id,
            8,
            (uuid4(),),
            uuid4(),
            datetime.now(UTC),
            plan.items,
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_alarm_configuration] = lambda: configuration

        async def finish_runtime_drain() -> None:
            await asyncio.to_thread(gate.entered.wait)
            gate.release.set()

        async with AuthenticatedApiClient(app) as client:
            bearer = await client._bearer("engineer")
            release_task = asyncio.create_task(finish_runtime_drain())
            response = await client._client.post(
                f"/api/v1/alarm-configuration-plans/{plan.id}/apply",
                json={"plan_digest": plan.digest},
                headers={
                    "Authorization": bearer,
                    "Idempotency-Key": "alarm-apply-event-loop-test",
                },
            )
        await release_task

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(8, response.json()["configuration_revision"])

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

    async def test_rule_group_endpoint_returns_the_minimal_list_contract(self) -> None:
        from app.api.alarm_configurations import get_alarm_configuration, router
        from app.services.alarm_configuration import AlarmRuleGroup

        repository = _Repository()
        repository.list_rule_groups = lambda: (
            AlarmRuleGroup(
                repository.rule_set.rule_set_id,
                "pcs-power",
                "PCS 功率",
                2,
                1,
                (repository.entity.id,),
                (repository.entity.id,),
                1,
                1,
                "MAJOR",
            ),
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_alarm_configuration] = lambda: AlarmConfiguration(repository)

        async with AuthenticatedApiClient(app) as client:
            response = await client._client.get(
                "/api/v1/alarm-rule-groups",
                headers={"Authorization": await client._bearer("engineer")},
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("PCS 功率", response.json()["items"][0]["name"])
        self.assertEqual(1, response.json()["items"][0]["device_count"])
        self.assertEqual("MAJOR", response.json()["items"][0]["highest_severity"])


if __name__ == "__main__":
    unittest.main()
