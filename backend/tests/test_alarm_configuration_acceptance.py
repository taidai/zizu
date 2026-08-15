from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from uuid import UUID, uuid4

from app.services.alarm_configuration import (
    AlarmConfigurationPlanItem,
    AppliedAlarmConfiguration,
)
from app.services.alarm_configuration_acceptance import (
    AlarmConfigurationAcceptance,
    AlarmConfigurationAcceptanceError,
    InMemoryAlarmConfigurationAcceptanceRepository,
    RunAlarmConfigurationAcceptance,
)
from app.services.alarm_runtime import (
    AcknowledgeAlarm,
    AlarmDefinition,
    AlarmObservation,
    AlarmRuntime,
    InMemoryAlarmDefinitionCatalog,
    InMemoryAlarmRepository,
)


APPLICATION_ID = UUID("81000000-0000-0000-0000-000000000003")
INSTALLATION_ID = UUID("81000000-0000-0000-0000-000000000002")
APPLIED_ID = UUID("81000000-0000-0000-0000-000000000003")
AUDIT_EVENT_ID = UUID("81000000-0000-0000-0000-000000000004")
DEFINITION_IDS = (
    UUID("81000000-0000-0000-0000-000000000011"),
    UUID("81000000-0000-0000-0000-000000000012"),
)
ENTITY_IDS = (
    UUID("81000000-0000-0000-0000-000000000021"),
    UUID("81000000-0000-0000-0000-000000000022"),
)


class AlarmConfigurationAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.started_at = datetime(2026, 8, 16, 9, tzinfo=timezone.utc)
        definitions = tuple(
            AlarmDefinition(
                id=definition_id,
                asset_id=f"site.alarm.pcs.{index}",
                version="1",
                entity_instance_id=ENTITY_IDS[index],
                entity_definition_id="pcs.activePower",
                trigger={"op": "gt", "value": 100},
                trigger_duration_seconds=1,
                recovery={"op": "lte", "value": 90},
                recovery_duration_seconds=0,
                severity="MAJOR",
                notification_throttle_seconds=0,
            )
            for index, definition_id in enumerate(DEFINITION_IDS)
        )
        self.runtime = AlarmRuntime(
            InMemoryAlarmDefinitionCatalog(definitions),
            InMemoryAlarmRepository(),
        )
        self.plan_items = tuple(
            AlarmConfigurationPlanItem(
                definition_key=f"site.alarm.pcs.{index}",
                entity_instance_id=ENTITY_IDS[index],
                rule_id="overpower",
                action=action,
                before=None if action == "add" else {"version": "old"},
                after={"version": "new"},
                blockers=(),
            )
            for index, action in enumerate(("add", "update"))
        )
        self.applied = AppliedAlarmConfiguration(
            id=APPLIED_ID,
            plan_id=UUID("81000000-0000-0000-0000-000000000005"),
            installation_id=INSTALLATION_ID,
            site_configuration_version=8,
            definition_ids=DEFINITION_IDS,
            audit_event_id=AUDIT_EVENT_ID,
            applied_at=self.started_at,
            items=self.plan_items,
        )

    def observe(self, definition_id: UUID, entity_id: UUID, *, value: int, after_seconds: int):
        return self.runtime.submit(AlarmObservation(
            definition_id=definition_id,
            entity_instance_id=entity_id,
            observed_at=self.started_at + timedelta(seconds=after_seconds),
            value=value,
            quality=192,
            source_kind="acceptance",
            source_ref="PCS-01",
            evidence={"sample": after_seconds},
        ))

    def complete_lifecycle(self, definition_id: UUID, entity_id: UUID, *, offset: int = 0) -> None:
        self.observe(definition_id, entity_id, value=101, after_seconds=offset)
        active = self.observe(definition_id, entity_id, value=101, after_seconds=offset + 1)
        self.runtime.acknowledge(AcknowledgeAlarm(
            event_id=active.event_id,
            actor="user:operator",
            acknowledged_at=self.started_at + timedelta(seconds=offset + 2),
            note="acceptance",
        ))
        self.observe(definition_id, entity_id, value=90, after_seconds=offset + 3)

    def acceptance(self, runtime=None, repository=None) -> AlarmConfigurationAcceptance:
        return AlarmConfigurationAcceptance(
            runtime=runtime or self.runtime,
            repository=repository or InMemoryAlarmConfigurationAcceptanceRepository(),
        )

    def applied_for(self, definition_id: UUID, item: AlarmConfigurationPlanItem) -> AppliedAlarmConfiguration:
        return AppliedAlarmConfiguration(
            id=APPLIED_ID,
            plan_id=UUID("81000000-0000-0000-0000-000000000005"),
            installation_id=INSTALLATION_ID,
            site_configuration_version=8,
            definition_ids=(definition_id,),
            audit_event_id=AUDIT_EVENT_ID,
            applied_at=self.started_at,
            items=(item,),
        )

    def run_acceptance(self, acceptance: AlarmConfigurationAcceptance, applied: AppliedAlarmConfiguration, *, key: str = "acceptance"):
        return acceptance.run(RunAlarmConfigurationAcceptance(
            application_id=APPLICATION_ID,
            actor="user:engineer",
            idempotency_key=key,
        ), applied)

    def test_complete_lifecycle_passes(self) -> None:
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])
        self.complete_lifecycle(DEFINITION_IDS[1], ENTITY_IDS[1], offset=10)

        report = AlarmConfigurationAcceptance(
            runtime=self.runtime,
            repository=InMemoryAlarmConfigurationAcceptanceRepository(),
        ).run(RunAlarmConfigurationAcceptance(
            application_id=APPLICATION_ID,
            actor="user:engineer",
            idempotency_key="complete-lifecycle",
        ), self.applied)

        self.assertEqual("passed", report.status)
        self.assertEqual(2, len(report.items))
        self.assertEqual(
            {"ALARM_ACTIVATED", "ALARM_ACKNOWLEDGED", "ALARM_RECOVERED"},
            set(report.items[0].transition_codes),
        )
        self.assertEqual("recovered", report.items[0].event_state)
        self.assertIsNotNone(report.items[0].acknowledgement_audit_event_id)

    def test_progress_observes_four_guided_stages_without_saving_report(self) -> None:
        repository = InMemoryAlarmConfigurationAcceptanceRepository()
        acceptance = self.acceptance(repository=repository)
        item = replace(
            self.plan_items[0],
            after={"rule": {"name": "有功功率越限"}},
        )
        applied = self.applied_for(DEFINITION_IDS[0], item)

        waiting_trigger = acceptance.progress(applied)
        self.observe(DEFINITION_IDS[0], ENTITY_IDS[0], value=101, after_seconds=0)
        still_waiting_trigger = acceptance.progress(applied)
        active = self.observe(DEFINITION_IDS[0], ENTITY_IDS[0], value=101, after_seconds=1)
        waiting_acknowledgement = acceptance.progress(applied)
        self.runtime.acknowledge(AcknowledgeAlarm(
            event_id=active.event_id,
            actor="user:operator",
            acknowledged_at=self.started_at + timedelta(seconds=2),
        ))
        waiting_recovery = acceptance.progress(applied)
        self.observe(DEFINITION_IDS[0], ENTITY_IDS[0], value=90, after_seconds=3)
        passed = acceptance.progress(applied)

        self.assertEqual(
            ["waiting_trigger", "waiting_trigger", "waiting_acknowledgement", "waiting_recovery", "passed"],
            [
                waiting_trigger.items[0].stage,
                still_waiting_trigger.items[0].stage,
                waiting_acknowledgement.items[0].stage,
                waiting_recovery.items[0].stage,
                passed.items[0].stage,
            ],
        )
        self.assertEqual("有功功率越限", passed.items[0].rule_name)
        self.assertTrue(passed.ready_to_report)
        self.assertIsNone(repository.find_idempotency("user:engineer", "acceptance"))

    def test_stale_application_is_rejected_without_report_or_idempotency_write(self) -> None:
        repository = InMemoryAlarmConfigurationAcceptanceRepository()
        acceptance = self.acceptance(repository=repository)
        latest_application_id = uuid4()

        with self.assertRaisesRegex(
            AlarmConfigurationAcceptanceError,
            "ALARM_ACCEPTANCE_APPLICATION_STALE",
        ):
            acceptance.run(
                RunAlarmConfigurationAcceptance(
                    self.applied.id,
                    "user:engineer",
                    "stale-application",
                ),
                self.applied,
                latest_application_id=latest_application_id,
            )

        self.assertIsNone(
            repository.find_idempotency("user:engineer", "stale-application")
        )

    def test_missing_event_fails_with_literal_code(self) -> None:
        report = self.run_acceptance(self.acceptance(), self.applied)

        self.assertEqual("failed", report.status)
        self.assertEqual("ALARM_ACCEPTANCE_EVENT_MISSING", report.items[0].code)

    def test_unacknowledged_active_event_fails_with_literal_code(self) -> None:
        self.observe(DEFINITION_IDS[0], ENTITY_IDS[0], value=101, after_seconds=0)
        self.observe(DEFINITION_IDS[0], ENTITY_IDS[0], value=101, after_seconds=1)

        report = self.run_acceptance(self.acceptance(), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]))

        self.assertEqual("ALARM_ACCEPTANCE_ACKNOWLEDGEMENT_MISSING", report.items[0].code)

    def test_acknowledged_unrecovered_event_fails_with_literal_code(self) -> None:
        self.observe(DEFINITION_IDS[0], ENTITY_IDS[0], value=101, after_seconds=0)
        active = self.observe(DEFINITION_IDS[0], ENTITY_IDS[0], value=101, after_seconds=1)
        self.runtime.acknowledge(AcknowledgeAlarm(
            event_id=active.event_id,
            actor="user:operator",
            acknowledged_at=self.started_at + timedelta(seconds=2),
        ))

        report = self.run_acceptance(self.acceptance(), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]))

        self.assertEqual("ALARM_ACCEPTANCE_RECOVERY_MISSING", report.items[0].code)

    def test_recovered_event_with_missing_required_transition_fails(self) -> None:
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])

        class IncompleteTimeline:
            def list(_self):
                return self.runtime.list()

            def timeline(_self, event_id):
                return tuple(
                    transition for transition in self.runtime.timeline(event_id)
                    if transition.code != "ALARM_ACTIVATED"
                )

        report = self.run_acceptance(
            self.acceptance(runtime=IncompleteTimeline()),
            self.applied_for(DEFINITION_IDS[0], self.plan_items[0]),
        )

        self.assertEqual("ALARM_ACCEPTANCE_TIMELINE_INCOMPLETE", report.items[0].code)

    def test_preserve_requires_evidence_for_the_same_immutable_definition(self) -> None:
        repository = InMemoryAlarmConfigurationAcceptanceRepository()
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])
        self.run_acceptance(self.acceptance(repository=repository), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]))
        preserve = AlarmConfigurationPlanItem(
            definition_key=self.plan_items[1].definition_key,
            entity_instance_id=ENTITY_IDS[1],
            rule_id="overpower",
            action="preserve",
            before={"version": "old"},
            after={"version": "old"},
            blockers=(),
        )

        report = self.run_acceptance(
            self.acceptance(repository=repository),
            self.applied_for(DEFINITION_IDS[1], preserve),
            key="preserve-different-definition",
        )

        self.assertEqual("failed", report.status)
        self.assertEqual("ALARM_ACCEPTANCE_PRESERVE_EVIDENCE_MISSING", report.items[0].code)

    def test_preserve_passes_and_references_prior_report_for_same_definition(self) -> None:
        repository = InMemoryAlarmConfigurationAcceptanceRepository()
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])
        prior = self.run_acceptance(self.acceptance(repository=repository), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]))
        preserve = AlarmConfigurationPlanItem(
            definition_key=self.plan_items[0].definition_key,
            entity_instance_id=ENTITY_IDS[0],
            rule_id="overpower",
            action="preserve",
            before={"version": "new"},
            after={"version": "new"},
            blockers=(),
        )

        report = self.run_acceptance(
            self.acceptance(repository=repository),
            self.applied_for(DEFINITION_IDS[0], preserve),
            key="preserve-same-definition",
        )

        self.assertEqual("passed", report.status)
        self.assertEqual("ALARM_ACCEPTANCE_PRESERVED", report.items[0].code)
        self.assertEqual(str(prior.id), report.items[0].evidence["prior_report_id"])

    def test_same_command_replays_the_saved_report(self) -> None:
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])
        repository = InMemoryAlarmConfigurationAcceptanceRepository()
        acceptance = self.acceptance(repository=repository)
        applied = self.applied_for(DEFINITION_IDS[0], self.plan_items[0])

        first = self.run_acceptance(acceptance, applied, key="same-command")
        replay = self.run_acceptance(acceptance, applied, key="same-command")

        self.assertEqual(first, replay)

    def test_acknowledgement_without_audit_evidence_fails(self) -> None:
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])

        class MissingAuditTimeline:
            def list(_self): return self.runtime.list()
            def timeline(_self, event_id):
                return tuple(replace(item, audit_event_id=None) if item.code == "ALARM_ACKNOWLEDGED" else item for item in self.runtime.timeline(event_id))

        report = self.run_acceptance(self.acceptance(runtime=MissingAuditTimeline()), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]))
        self.assertEqual("ALARM_ACCEPTANCE_ACKNOWLEDGEMENT_MISSING", report.items[0].code)

    def test_same_actor_key_with_different_request_is_rejected(self) -> None:
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])
        acceptance = self.acceptance(repository=InMemoryAlarmConfigurationAcceptanceRepository())
        self.run_acceptance(acceptance, self.applied_for(DEFINITION_IDS[0], self.plan_items[0]), key="collision")
        changed = replace(self.applied_for(DEFINITION_IDS[0], self.plan_items[0]), site_configuration_version=9)
        with self.assertRaisesRegex(AlarmConfigurationAcceptanceError, "ALARM_ACCEPTANCE_IDEMPOTENCY_KEY_REUSED"):
            self.run_acceptance(acceptance, changed, key="collision")

    def test_application_must_match_applied_identity(self) -> None:
        with self.assertRaisesRegex(AlarmConfigurationAcceptanceError, "ALARM_ACCEPTANCE_APPLICATION_MISMATCH"):
            self.acceptance().run(RunAlarmConfigurationAcceptance(UUID(int=1), "user:engineer", "bad-app"), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]))

    def test_evidence_is_immutable_and_report_digest_binds_identity_and_times(self) -> None:
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])
        report = self.run_acceptance(self.acceptance(), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]), key="immutable")
        with self.assertRaises(TypeError):
            report.items[0].evidence["tamper"] = True
        second = self.run_acceptance(self.acceptance(), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]), key="immutable-2")
        self.assertNotEqual(report.digest, second.digest)

    def test_repository_rejects_overwrite_of_existing_report_id(self) -> None:
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])
        repository = InMemoryAlarmConfigurationAcceptanceRepository()
        report = self.run_acceptance(self.acceptance(repository=repository), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]))
        with self.assertRaisesRegex(AlarmConfigurationAcceptanceError, "ALARM_ACCEPTANCE_REPORT_EXISTS"):
            repository.save(replace(report, status="failed"), idempotency_key="other", request_digest="other")

    def test_mismatched_or_empty_applied_items_are_rejected(self) -> None:
        for applied in (replace(self.applied, items=self.plan_items[:1]), replace(self.applied, definition_ids=(), items=())):
            with self.subTest(applied=applied):
                with self.assertRaisesRegex(AlarmConfigurationAcceptanceError, "ALARM_ACCEPTANCE_APPLIED_ITEMS_INVALID"):
                    self.run_acceptance(self.acceptance(), applied)

    def test_report_digest_is_recomputable_from_public_report_content(self) -> None:
        self.complete_lifecycle(DEFINITION_IDS[0], ENTITY_IDS[0])
        report = self.run_acceptance(self.acceptance(), self.applied_for(DEFINITION_IDS[0], self.plan_items[0]), key="request-only")

        def value(item):
            if isinstance(item, UUID): return str(item)
            if isinstance(item, datetime): return item.astimezone(timezone.utc).isoformat()
            if hasattr(item, "__dataclass_fields__"):
                return {name: value(getattr(item, name)) for name in item.__dataclass_fields__ if name != "digest"}
            if isinstance(item, dict): return {str(key): value(value_) for key, value_ in sorted(item.items())}
            if isinstance(item, (tuple, list)): return [value(value_) for value_ in item]
            return item

        payload = value(report)
        recomputed = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(report.digest, recomputed)


if __name__ == "__main__":
    unittest.main()
