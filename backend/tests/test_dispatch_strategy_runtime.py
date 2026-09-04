from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest
from uuid import UUID

from app.services.dispatch_strategies import (
    DispatchWindow,
    EvaluationResult,
    StrategyBindingDraft,
    StrategyInput,
    StrategyRevision,
    StrategyRuntime,
    StrategyRuntimeState,
    StrategySnapshot,
    StrategyTrigger,
    build_two_charge_two_discharge_jdm,
)


NOW = datetime(2026, 9, 4, 2, 30, tzinfo=UTC)
STRATEGY_ID = UUID("71000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("71000000-0000-0000-0000-000000000002")
SOC_ID = UUID("71000000-0000-0000-0000-000000000003")
OUTPUT_ID = UUID("71000000-0000-0000-0000-000000000004")


def _engine(_content, inputs):
    target = 156.7 if inputs["soc"] >= 40 else 100.0
    return {
        "result": {
            "action_id": "power-target",
            "target": target,
            "matched_rule": "discharge-1",
        }
    }


def _revision(trigger_kind: str = "DATA_CHANGE") -> StrategyRevision:
    content = build_two_charge_two_discharge_jdm(
        (
            DispatchWindow(
                "discharge-1",
                "10:00",
                "12:00",
                "DISCHARGE",
                Decimal("156.7"),
                Decimal("40"),
                Decimal("90"),
            ),
        ),
        Decimal("0"),
    )
    return StrategyRevision(
        id=REVISION_ID,
        strategy_id=STRATEGY_ID,
        revision=1,
        lifecycle="PUBLISHED",
        trigger_kind=trigger_kind,
        site_timezone="Asia/Shanghai",
        jdm_content=content,
        content_digest="a" * 64,
        base_configuration_revision=7,
        bindings=(
            StrategyBindingDraft("INPUT", "soc", 0, SOC_ID, "FLOAT", "%", 10.0),
            StrategyBindingDraft(
                "OUTPUT", "power-target", 0, OUTPUT_ID, "FLOAT", "kW", 10.0
            ),
        ),
        created_by="tester",
        created_at=NOW,
        published_by="tester",
        published_at=NOW,
    )


def _sample(
    key: str,
    entity_id: UUID,
    value: object,
    *,
    data_type: str = "FLOAT",
    unit: str | None = None,
    quality: str = "GOOD",
    observed_at: datetime = NOW,
    frame: int = 42,
    revision: int = 7,
) -> StrategyInput:
    return StrategyInput(
        field_key=key,
        entity_instance_id=entity_id,
        value=value,
        data_type=data_type,
        unit=unit,
        quality=quality,
        observed_at=observed_at,
        frame_sequence=frame,
        configuration_revision=revision,
    )


def _snapshot(*, soc: StrategyInput | None = None, output: StrategyInput | None = None):
    return StrategySnapshot(
        frame_sequence=42,
        configuration_revision=7,
        evaluated_at=NOW,
        inputs=(
            soc or _sample("soc", SOC_ID, 49.0, unit="%", frame=40),
            output or _sample("power-target", OUTPUT_ID, 156.8, unit="kW", frame=42),
        ),
    )


class _Repository:
    def __init__(self, snapshot: StrategySnapshot | None = None) -> None:
        self.model = _revision()
        self.snapshot = snapshot or _snapshot()
        self.state = StrategyRuntimeState()
        self.mutations = []
        self.open_intents: set[UUID] = set()
        self.fail_commit = False
        self.requested_frames = []

    def affected_strategy_ids(self, entity_ids, trigger_kind):
        if trigger_kind != "DATA_CHANGE" or SOC_ID not in entity_ids:
            return ()
        return (STRATEGY_ID,)

    def active_revision(self, strategy_id):
        return self.model if strategy_id == STRATEGY_ID else None

    def get_revision(self, revision_id):
        return self.model if revision_id == REVISION_ID else None

    def load_snapshot(self, revision, frame_sequence, evaluated_at):
        self.requested_frames.append(frame_sequence)
        return replace(
            self.snapshot,
            frame_sequence=self.snapshot.frame_sequence if frame_sequence is None else frame_sequence,
            evaluated_at=evaluated_at,
        )

    def runtime_state(self, strategy_id):
        return self.state

    def has_open_intent(self, strategy_id, entity_id):
        return entity_id in self.open_intents

    def commit_evaluation(self, mutation):
        if self.fail_commit:
            raise RuntimeError("simulated commit failure")
        if self.state.last_trigger_key == mutation.trigger.trigger_key:
            return False
        self.mutations.append(mutation)
        self.state = StrategyRuntimeState(
            runtime_health=mutation.runtime_health,
            last_trigger_key=mutation.trigger.trigger_key,
            last_desired=mutation.desired,
            last_actual=mutation.actual,
            block_reason=mutation.reason_code,
            failure_code=mutation.failure_code,
        )
        self.open_intents.update(item.entity_instance_id for item in mutation.intents)
        return True


class DispatchStrategyRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = _Repository()
        self.runtime = StrategyRuntime(self.repository, evaluator=_engine)

    def trigger(self, key: str = "frame:42", kind: str = "DATA_CHANGE"):
        return StrategyTrigger(kind, key, NOW, 42)

    def test_trigger_changes_locate_strategy_but_snapshot_supplies_all_inputs(self) -> None:
        results = self.runtime.evaluate_data_change(
            changed_entity_ids=(SOC_ID,),
            trigger=self.trigger(),
        )

        self.assertEqual(1, len(results))
        self.assertEqual(42, results[0].snapshot.frame_sequence)
        self.assertEqual(49.0, results[0].engine_inputs["soc"])
        self.assertEqual(630, results[0].engine_inputs["site_local_minute"])
        self.assertEqual(40, results[0].snapshot.inputs[0].frame_sequence)

    def test_invalid_bound_value_blocks_without_intent(self) -> None:
        cases = (
            (None, "L2_INPUT_MISSING"),
            (replace(_sample("soc", SOC_ID, 49.0, unit="%"), quality="BAD"), "L2_QUALITY_NOT_GOOD"),
            (replace(_sample("soc", SOC_ID, 49.0, unit="%"), quality="UNCERTAIN"), "L2_QUALITY_NOT_GOOD"),
            (replace(_sample("soc", SOC_ID, 49.0, unit="%"), quality="STALE"), "L2_QUALITY_NOT_GOOD"),
            (replace(_sample("soc", SOC_ID, 49.0, unit="%"), observed_at=NOW - timedelta(seconds=11)), "L2_INPUT_STALE"),
            (replace(_sample("soc", SOC_ID, 49.0, unit="%"), configuration_revision=6), "L2_CONFIGURATION_MISMATCH"),
            (replace(_sample("soc", SOC_ID, 49.0, unit="%"), data_type="INT"), "L2_TYPE_MISMATCH"),
            (replace(_sample("soc", SOC_ID, 49.0, unit="%"), unit="kWh"), "L2_UNIT_MISMATCH"),
        )
        for sample, reason in cases:
            with self.subTest(reason=reason):
                inputs = tuple(
                    item
                    for item in _snapshot().inputs
                    if item.entity_instance_id != SOC_ID
                )
                if sample is not None:
                    inputs = (sample, *inputs)
                repository = _Repository(replace(_snapshot(), inputs=inputs))
                result = StrategyRuntime(repository, evaluator=_engine).evaluate(
                    STRATEGY_ID, self.trigger()
                )
                self.assertEqual("BLOCKED", result.status)
                self.assertEqual(reason, result.reason_code)
                self.assertEqual((), result.intents)

    def test_block_event_is_written_only_for_first_reason_change_and_recovery(self) -> None:
        self.repository.snapshot = replace(
            _snapshot(),
            inputs=(
                replace(_snapshot().inputs[0], quality="BAD"),
                _snapshot().inputs[1],
            ),
        )
        first = self.runtime.evaluate(STRATEGY_ID, self.trigger("frame:42"))
        second = self.runtime.evaluate(STRATEGY_ID, self.trigger("frame:43"))
        self.repository.snapshot = replace(
            self.repository.snapshot,
            inputs=(
                replace(self.repository.snapshot.inputs[0], quality="GOOD", observed_at=NOW - timedelta(seconds=11)),
                self.repository.snapshot.inputs[1],
            ),
        )
        changed = self.runtime.evaluate(STRATEGY_ID, self.trigger("frame:44"))
        self.repository.snapshot = _snapshot()
        recovered = self.runtime.evaluate(STRATEGY_ID, self.trigger("frame:45"))

        self.assertEqual(("BLOCKED",), tuple(item.kind for item in first.events))
        self.assertEqual((), second.events)
        self.assertEqual(("BLOCK_REASON_CHANGED",), tuple(item.kind for item in changed.events))
        self.assertIn("RECOVERED", tuple(item.kind for item in recovered.events))

    def test_data_change_and_fixed_tick_share_one_decision_path(self) -> None:
        first = self.runtime.evaluate(STRATEGY_ID, self.trigger("data", "DATA_CHANGE"))
        self.repository.open_intents.clear()
        second = self.runtime.evaluate(STRATEGY_ID, self.trigger("tick", "FIXED_TICK"))

        self.assertEqual(first.evaluation.decision, second.evaluation.decision)
        self.assertEqual(first.engine_inputs, second.engine_inputs)
        self.assertEqual([42, None], self.repository.requested_frames)

    def test_repeated_trigger_is_idempotent(self) -> None:
        first = self.runtime.evaluate(STRATEGY_ID, self.trigger())
        second = self.runtime.evaluate(STRATEGY_ID, self.trigger())

        self.assertTrue(first.persisted)
        self.assertFalse(second.persisted)
        self.assertEqual(1, len(self.repository.mutations))
        self.assertEqual(1, len(self.repository.mutations[0].intents))

    def test_simulation_uses_same_snapshot_with_typed_override_and_no_write(self) -> None:
        result = self.runtime.simulate(REVISION_ID, {"soc": 35.0}, NOW)

        self.assertEqual(35.0, result.engine_inputs["soc"])
        self.assertEqual(100.0, result.evaluation.intents[0].value)
        self.assertEqual([], self.repository.mutations)

        with self.assertRaisesRegex(ValueError, "SIMULATION_OVERRIDE_TYPE_MISMATCH"):
            self.runtime.simulate(REVISION_ID, {"soc": "35"}, NOW)

    def test_actual_at_target_is_noop_but_drift_reconciles_once(self) -> None:
        self.repository.snapshot = _snapshot(
            output=_sample("power-target", OUTPUT_ID, 156.7, unit="kW")
        )
        at_target = self.runtime.evaluate(STRATEGY_ID, self.trigger("at-target"))
        self.repository.snapshot = _snapshot(
            output=_sample("power-target", OUTPUT_ID, 156.8, unit="kW")
        )
        drift = self.runtime.evaluate(STRATEGY_ID, self.trigger("drift"))
        still_drift = self.runtime.evaluate(STRATEGY_ID, self.trigger("drift-2"))

        self.assertEqual((), at_target.intents)
        self.assertEqual(1, len(drift.intents))
        self.assertEqual((), still_drift.intents)

    def test_failure_latch_and_transaction_failure_never_leak_an_intent(self) -> None:
        self.repository.state = replace(
            self.repository.state,
            runtime_health="FAILED",
            failure_code="CONTROL_FAILED",
        )
        latched = self.runtime.evaluate(STRATEGY_ID, self.trigger("latched"))
        self.assertEqual((), latched.intents)

        repository = _Repository()
        repository.fail_commit = True
        with self.assertRaisesRegex(RuntimeError, "commit failure"):
            StrategyRuntime(repository, evaluator=_engine).evaluate(
                STRATEGY_ID, self.trigger("db-fails")
            )
        self.assertEqual([], repository.mutations)


if __name__ == "__main__":
    unittest.main()
