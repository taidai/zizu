"""Domain model for user-facing dispatch strategies."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from typing import Callable, Literal, Mapping, Protocol, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from app.services.gorules_adapter import StandardJdmError, evaluate_standard_jdm


ALLOWED_DISPATCH_ACTIONS = frozenset({"CHARGE", "DISCHARGE", "HOLD"})
_VALUE_UNSET = object()


class StrategyModelError(ValueError):
    """Stable machine-readable strategy model failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class DispatchWindow:
    key: str
    start: str
    end: str
    action: str
    target: Decimal
    soc_min: Decimal
    soc_max: Decimal


@dataclass(frozen=True)
class StrategyInput:
    field_key: str
    entity_instance_id: UUID
    value: object
    data_type: str
    unit: str | None
    quality: str
    observed_at: datetime
    frame_sequence: int
    configuration_revision: int
    definition_id: str | None = None


@dataclass(frozen=True)
class StrategyBindingDraft:
    direction: str
    binding_key: str
    ordinal: int
    entity_instance_id: UUID
    expected_data_type: str
    unit: str | None
    freshness_seconds: float


@dataclass(frozen=True)
class StrategyDraft:
    name: str
    description: str | None
    trigger_kind: str
    site_timezone: str
    jdm_content: Mapping[str, object]
    base_configuration_revision: int
    bindings: tuple[StrategyBindingDraft, ...]


@dataclass(frozen=True)
class StrategyRevision:
    id: UUID
    strategy_id: UUID
    revision: int
    lifecycle: str
    trigger_kind: str
    site_timezone: str
    jdm_content: Mapping[str, object]
    content_digest: str
    base_configuration_revision: int
    bindings: tuple[StrategyBindingDraft, ...]
    created_by: str
    created_at: datetime
    published_by: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class StrategyView:
    id: UUID
    name: str
    description: str | None
    active_revision_id: UUID | None
    enabled: bool
    runtime_health: str
    last_trigger_key: str | None
    last_evaluated_at: datetime | None
    last_desired: object | None
    last_actual: object | None
    last_evidence: Mapping[str, object] | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    draft: StrategyRevision | None
    active_revision: StrategyRevision | None
    published_revision: StrategyRevision | None = None


@dataclass(frozen=True)
class EntityBindingContract:
    active: bool
    data_type: str
    unit: str | None
    direction: str
    confirmed_write_points: int
    minimum: float | None
    maximum: float | None
    definition_id: str | None = None


@dataclass(frozen=True)
class OutputBinding:
    action_id: str
    entity_instance_id: UUID
    data_type: str
    unit: str | None
    controllable: bool
    confirmed_write_point: bool


@dataclass(frozen=True)
class ControlIntentDraft:
    action_id: str
    entity_instance_id: UUID
    value: object
    ordinal: int


@dataclass(frozen=True)
class StrategyEvaluation:
    matched_rules: tuple[str, ...]
    decision: Mapping[str, object]
    intents: tuple[ControlIntentDraft, ...]


@dataclass(frozen=True)
class StrategyTrigger:
    kind: Literal["DATA_CHANGE", "FIXED_TICK"]
    trigger_key: str
    evaluated_at: datetime
    frame_sequence: int


@dataclass(frozen=True)
class StrategySnapshot:
    frame_sequence: int
    configuration_revision: int
    evaluated_at: datetime
    inputs: tuple[StrategyInput, ...]


@dataclass(frozen=True)
class StrategyRuntimeState:
    runtime_health: str = "READY"
    last_trigger_key: str | None = None
    last_desired: Mapping[str, object] | None = None
    last_actual: Mapping[str, object] | None = None
    block_reason: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class StrategyEventDraft:
    kind: str
    reason_code: str | None = None


@dataclass(frozen=True)
class StrategyEvaluationMutation:
    strategy_id: UUID
    revision_id: UUID
    trigger: StrategyTrigger
    snapshot: StrategySnapshot
    engine_inputs: Mapping[str, object]
    decision: Mapping[str, object] | None
    desired: Mapping[str, object] | None
    actual: Mapping[str, object] | None
    intents: tuple[ControlIntentDraft, ...]
    events: tuple[StrategyEventDraft, ...]
    runtime_health: str
    reason_code: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    status: str
    reason_code: str | None
    snapshot: StrategySnapshot | None
    engine_inputs: Mapping[str, object]
    evaluation: StrategyEvaluation | None
    desired: Mapping[str, object] | None
    actual: Mapping[str, object] | None
    intents: tuple[ControlIntentDraft, ...]
    events: tuple[StrategyEventDraft, ...]
    persisted: bool


class StrategyRuntimeRepository(Protocol):
    def affected_strategy_ids(
        self, entity_ids: Sequence[UUID], trigger_kind: str
    ) -> tuple[UUID, ...]: ...

    def active_revision(self, strategy_id: UUID) -> StrategyRevision | None: ...

    def get_revision(self, revision_id: UUID) -> StrategyRevision | None: ...

    def load_snapshot(
        self,
        revision: StrategyRevision,
        frame_sequence: int | None,
        evaluated_at: datetime,
    ) -> StrategySnapshot: ...

    def runtime_state(self, strategy_id: UUID) -> StrategyRuntimeState: ...

    def has_open_intent(self, strategy_id: UUID, entity_id: UUID) -> bool: ...

    def commit_evaluation(self, mutation: StrategyEvaluationMutation) -> bool: ...


JdmEvaluator = Callable[
    [Mapping[str, object], Mapping[str, object]], dict[str, object]
]


class StrategyRuntime:
    """The sole seam for snapshot validation, JDM evaluation and intent creation."""

    def __init__(
        self,
        repository: StrategyRuntimeRepository,
        *,
        evaluator: JdmEvaluator = evaluate_standard_jdm,
    ) -> None:
        self._repository = repository
        self._evaluator = evaluator

    def simulate(
        self,
        revision_id: UUID,
        overrides: Mapping[str, object],
        evaluated_at: datetime,
    ) -> EvaluationResult:
        revision = self._repository.get_revision(revision_id)
        if revision is None:
            raise StrategyModelError("STRATEGY_REVISION_NOT_FOUND", "revision does not exist")
        snapshot = self._repository.load_snapshot(revision, None, evaluated_at)
        try:
            engine_inputs, actual = validate_strategy_snapshot(revision, snapshot)
        except StrategyModelError as error:
            return EvaluationResult(
                "BLOCKED", error.code, snapshot, {}, None, None, None,
                (), (), False,
            )
        input_bindings = {
            item.binding_key: item
            for item in revision.bindings
            if item.direction == "INPUT"
        }
        for key, value in overrides.items():
            binding = input_bindings.get(key)
            if binding is None:
                raise StrategyModelError(
                    "SIMULATION_OVERRIDE_UNKNOWN", f"unknown input {key}"
                )
            sample = next(
                item for item in snapshot.inputs
                if item.entity_instance_id == binding.entity_instance_id
            )
            validate_strategy_binding_role(binding, sample, value=value)
            try:
                engine_inputs[key] = _typed_value(value, binding.expected_data_type)
            except StrategyModelError as error:
                raise StrategyModelError(
                    "SIMULATION_OVERRIDE_TYPE_MISMATCH", str(error)
                ) from error
        evaluation = self._run_jdm(revision, engine_inputs)
        desired = {item.action_id: item.value for item in evaluation.intents}
        return EvaluationResult(
            "EVALUATED", None, snapshot, engine_inputs, evaluation, desired, actual,
            evaluation.intents, (), False,
        )

    def evaluate(self, strategy_id: UUID, trigger: StrategyTrigger) -> EvaluationResult:
        if trigger.kind not in {"DATA_CHANGE", "FIXED_TICK"}:
            raise StrategyModelError("STRATEGY_TRIGGER_INVALID", "unknown trigger kind")
        if not trigger.trigger_key.strip():
            raise StrategyModelError("STRATEGY_TRIGGER_KEY_REQUIRED", "trigger key is required")
        state = self._repository.runtime_state(strategy_id)
        if state.last_trigger_key == trigger.trigger_key:
            return EvaluationResult(
                state.runtime_health,
                state.block_reason or state.failure_code,
                None,
                {},
                None,
                state.last_desired,
                state.last_actual,
                (),
                (),
                False,
            )
        revision = self._repository.active_revision(strategy_id)
        if revision is None:
            return EvaluationResult(
                "SKIPPED", "STRATEGY_NOT_ACTIVE", None, {}, None, None, None, (), (), False
            )
        snapshot = self._repository.load_snapshot(
            revision,
            None if trigger.kind == "FIXED_TICK" else trigger.frame_sequence,
            trigger.evaluated_at,
        )
        if state.failure_code is not None or state.runtime_health == "FAILED":
            return EvaluationResult(
                "FAILED", state.failure_code or "STRATEGY_FAILURE_LATCHED", snapshot,
                {}, None, state.last_desired, state.last_actual, (), (), False,
            )
        try:
            engine_inputs, actual = validate_strategy_snapshot(revision, snapshot)
        except StrategyModelError as error:
            return self._commit_blocked(strategy_id, revision, trigger, snapshot, state, error.code)
        try:
            evaluation = self._run_jdm(revision, engine_inputs)
        except (StrategyModelError, StandardJdmError) as error:
            code = getattr(error, "code", "JDM_EVALUATION_FAILED")
            events = () if state.failure_code == code else (StrategyEventDraft("FAILED", code),)
            mutation = StrategyEvaluationMutation(
                strategy_id, revision.id, trigger, snapshot, engine_inputs, None,
                state.last_desired, actual, (), events, "FAILED", code, code,
            )
            persisted = self._repository.commit_evaluation(mutation)
            return EvaluationResult(
                "FAILED", code, snapshot, engine_inputs, None, state.last_desired,
                actual, (), events if persisted else (), persisted,
            )

        desired = {item.action_id: item.value for item in evaluation.intents}
        intents = tuple(
            item
            for item in evaluation.intents
            if not _values_equal(actual.get(item.action_id), item.value)
            and not self._repository.has_open_intent(strategy_id, item.entity_instance_id)
        )
        events: list[StrategyEventDraft] = []
        if state.runtime_health == "BLOCKED":
            events.append(StrategyEventDraft("RECOVERED"))
        if state.last_desired != desired:
            events.append(StrategyEventDraft("DECISION_CHANGED"))
        if intents:
            events.append(StrategyEventDraft("INTENT_CREATED"))
        mutation = StrategyEvaluationMutation(
            strategy_id, revision.id, trigger, snapshot, engine_inputs,
            evaluation.decision, desired, actual, intents, tuple(events), "READY",
        )
        persisted = self._repository.commit_evaluation(mutation)
        return EvaluationResult(
            "EVALUATED", None, snapshot, engine_inputs, evaluation, desired, actual,
            intents if persisted else (), tuple(events) if persisted else (), persisted,
        )

    def evaluate_data_change(
        self,
        changed_entity_ids: Sequence[UUID],
        trigger: StrategyTrigger,
    ) -> tuple[EvaluationResult, ...]:
        strategy_ids = self._repository.affected_strategy_ids(
            changed_entity_ids, "DATA_CHANGE"
        )
        return tuple(self.evaluate(strategy_id, trigger) for strategy_id in strategy_ids)

    def _run_jdm(
        self,
        revision: StrategyRevision,
        engine_inputs: Mapping[str, object],
    ) -> StrategyEvaluation:
        outputs = self._evaluator(revision.jdm_content, engine_inputs)
        bindings = {
            item.binding_key: OutputBinding(
                item.binding_key,
                item.entity_instance_id,
                item.expected_data_type,
                item.unit,
                True,
                True,
            )
            for item in revision.bindings
            if item.direction == "OUTPUT"
        }
        intents = extract_control_intents(outputs, bindings)
        decision = outputs.get("result", outputs)
        if not isinstance(decision, Mapping):
            raise StrategyModelError("JDM_RESULT_INVALID", "decision result must be an object")
        raw_rule = decision.get("matched_rule")
        matched_rules = (str(raw_rule),) if raw_rule is not None else ()
        return StrategyEvaluation(matched_rules, dict(decision), intents)

    def _commit_blocked(
        self,
        strategy_id: UUID,
        revision: StrategyRevision,
        trigger: StrategyTrigger,
        snapshot: StrategySnapshot,
        state: StrategyRuntimeState,
        reason_code: str,
    ) -> EvaluationResult:
        if state.runtime_health != "BLOCKED":
            events = (StrategyEventDraft("BLOCKED", reason_code),)
        elif state.block_reason != reason_code:
            events = (StrategyEventDraft("BLOCK_REASON_CHANGED", reason_code),)
        else:
            events = ()
        mutation = StrategyEvaluationMutation(
            strategy_id, revision.id, trigger, snapshot, {}, None,
            state.last_desired, state.last_actual, (), events, "BLOCKED", reason_code,
        )
        persisted = self._repository.commit_evaluation(mutation)
        return EvaluationResult(
            "BLOCKED", reason_code, snapshot, {}, None, state.last_desired,
            state.last_actual, (), events if persisted else (), persisted,
        )


def strategy_draft_digest(draft: StrategyDraft) -> str:
    """Digest every executable field; presentation order of bindings is irrelevant."""
    bindings = sorted(
        (
            {
                "direction": item.direction,
                "binding_key": item.binding_key,
                "ordinal": item.ordinal,
                "entity_instance_id": str(item.entity_instance_id),
                "expected_data_type": item.expected_data_type,
                "unit": item.unit,
                "freshness_seconds": item.freshness_seconds,
            }
            for item in draft.bindings
        ),
        key=lambda item: (item["direction"], item["ordinal"], item["binding_key"]),
    )
    document = {
        "name": draft.name.strip(),
        "description": draft.description,
        "trigger_kind": draft.trigger_kind,
        "site_timezone": draft.site_timezone,
        "jdm_content": draft.jdm_content,
        "base_configuration_revision": draft.base_configuration_revision,
        "bindings": bindings,
    }
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def validate_publish_bindings(
    bindings: Sequence[StrategyBindingDraft],
    contracts: Mapping[UUID, EntityBindingContract],
    *,
    static_targets: Sequence[object],
    require_complete: bool = True,
) -> None:
    """Fail closed before a strategy revision can own or control L2 outputs."""
    inputs = tuple(item for item in bindings if item.direction == "INPUT")
    outputs = tuple(item for item in bindings if item.direction == "OUTPUT")
    if require_complete and not inputs:
        raise StrategyModelError("STRATEGY_INPUT_REQUIRED", "at least one L2 input is required")
    if require_complete and not outputs:
        raise StrategyModelError("STRATEGY_OUTPUT_REQUIRED", "at least one L2 output is required")
    if {item.entity_instance_id for item in inputs} & {
        item.entity_instance_id for item in outputs
    }:
        raise StrategyModelError(
            "STRATEGY_SELF_TRIGGER_FORBIDDEN",
            "an output cannot also be an input",
        )
    seen_keys: set[tuple[str, str]] = set()
    seen_ordinals: set[tuple[str, int]] = set()
    for binding in bindings:
        if binding.direction not in {"INPUT", "OUTPUT"}:
            raise StrategyModelError("BINDING_DIRECTION_INVALID", "binding direction is invalid")
        if not binding.binding_key.strip():
            raise StrategyModelError("BINDING_KEY_INVALID", "binding key is required")
        if binding.freshness_seconds <= 0:
            raise StrategyModelError("BINDING_FRESHNESS_INVALID", "freshness must be positive")
        key = (binding.direction, binding.binding_key)
        ordinal = (binding.direction, binding.ordinal)
        if key in seen_keys or ordinal in seen_ordinals:
            raise StrategyModelError("BINDING_DUPLICATED", "binding keys and ordinals must be unique")
        seen_keys.add(key)
        seen_ordinals.add(ordinal)
        contract = contracts.get(binding.entity_instance_id)
        if contract is None or not contract.active:
            raise StrategyModelError("L2_BINDING_UNAVAILABLE", "bound L2 entity is unavailable")
        if contract.data_type != binding.expected_data_type:
            raise StrategyModelError("L2_BINDING_TYPE_MISMATCH", "bound L2 type changed")
        if _normalized_unit(contract.unit) != _normalized_unit(binding.unit):
            raise StrategyModelError("L2_BINDING_UNIT_MISMATCH", "bound L2 unit changed")
        validate_strategy_binding_role(binding, contract)
        if binding.direction == "INPUT" and contract.direction not in {"R", "RW"}:
            raise StrategyModelError("INPUT_NOT_READABLE", "bound L2 cannot be read")
        if binding.direction == "OUTPUT":
            if contract.direction not in {"W", "RW"}:
                raise StrategyModelError("OUTPUT_NOT_CONTROLLABLE", "bound L2 is read-only")
            if contract.confirmed_write_points != 1:
                raise StrategyModelError(
                    "OUTPUT_WRITE_POINT_UNCONFIRMED",
                    "output needs exactly one confirmed write point",
                )
    if len(outputs) != 1 and static_targets:
        raise StrategyModelError(
            "OUTPUT_TARGET_AMBIGUOUS",
            "static targets require one output binding",
        )
    if static_targets:
        output_contract = contracts[outputs[0].entity_instance_id]
        for raw_target in static_targets:
            target = _finite_decimal(raw_target, "OUTPUT_TARGET_INVALID")
            if output_contract.minimum is not None and target < Decimal(str(output_contract.minimum)):
                raise StrategyModelError("OUTPUT_LIMIT_VIOLATION", "target is below the configured minimum")
            if output_contract.maximum is not None and target > Decimal(str(output_contract.maximum)):
                raise StrategyModelError("OUTPUT_LIMIT_VIOLATION", "target is above the configured maximum")


def validate_strategy_binding_role(
    binding: StrategyBindingDraft,
    contract: EntityBindingContract | StrategyInput,
    *,
    value: object = _VALUE_UNSET,
) -> None:
    """Enforce the starter's reserved roles without restricting generic JDM bindings."""
    role = (binding.direction, binding.binding_key)
    if role == ("INPUT", "soc"):
        prefix, unit = "SOC", "%"
        if contract.definition_id not in {"bms.soc", "storage.soc"}:
            raise StrategyModelError(
                "SOC_BINDING_DEFINITION_INVALID", "SOC requires bms.soc or storage.soc"
            )
    elif role == ("OUTPUT", "power-target"):
        prefix, unit = "POWER_TARGET", "kW"
    else:
        return
    if contract.data_type not in {"INT", "FLOAT"}:
        raise StrategyModelError(f"{prefix}_BINDING_TYPE_INVALID", "role requires numeric L2")
    if _normalized_unit(contract.unit) != unit:
        raise StrategyModelError(f"{prefix}_BINDING_UNIT_INVALID", f"role requires {unit}")
    if value is _VALUE_UNSET:
        return
    code = f"{prefix}_VALUE_INVALID"
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float, Decimal))
        or (contract.data_type == "INT" and type(value) is not int)
    ):
        raise StrategyModelError(code, "role value must match its numeric type")
    number = _finite_decimal(value, code)
    if prefix == "SOC" and not 0 <= number <= 100:
        raise StrategyModelError(code, "SOC value must be within 0..100 percent")


def static_jdm_targets(content: Mapping[str, object]) -> tuple[Decimal, ...]:
    """Read literal targets from decision tables for publish-time limit checks."""
    targets: list[Decimal] = []
    nodes = content.get("nodes")
    if not isinstance(nodes, list):
        return ()
    for node in nodes:
        if not isinstance(node, Mapping) or node.get("type") != "decisionTableNode":
            continue
        table = node.get("content")
        rules = table.get("rules") if isinstance(table, Mapping) else None
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, Mapping) or "target" not in rule:
                continue
            raw = rule["target"]
            if not isinstance(raw, (str, int, float, Decimal)) or isinstance(raw, bool):
                raise StrategyModelError(
                    "OUTPUT_TARGET_NOT_STATIC",
                    "dispatch targets must be literal numbers",
                )
            targets.append(_finite_decimal(raw, "OUTPUT_TARGET_INVALID"))
    return tuple(targets)


def split_cross_midnight(window: DispatchWindow) -> tuple[DispatchWindow, ...]:
    """Split one cross-midnight business window into explicit day segments."""
    start = _minute(window.start, allow_24=False)
    end = _minute(window.end, allow_24=True)
    if start == end:
        raise StrategyModelError("DISPATCH_WINDOW_EMPTY", "start and end differ")
    if start < end:
        return (window,)
    return (
        replace(window, key=f"{window.key}:late", end="24:00"),
        replace(window, key=f"{window.key}:early", start="00:00"),
    )


def build_two_charge_two_discharge_jdm(
    rows: Sequence[DispatchWindow],
    safe_target: Decimal | None,
) -> dict[str, object]:
    """Compile the easy decision-table form into its sole standard JDM graph."""
    if safe_target is None:
        raise StrategyModelError("SAFE_TARGET_REQUIRED", "other-time target is required")
    safe = _finite_decimal(safe_target, "SAFE_TARGET_INVALID")
    validated: list[tuple[int, int, DispatchWindow]] = []
    seen_keys: set[str] = set()
    for row in rows:
        if not row.key.strip() or row.key in seen_keys:
            raise StrategyModelError("DISPATCH_WINDOW_KEY_INVALID", "window keys must be unique")
        seen_keys.add(row.key)
        start = _minute(row.start, allow_24=False)
        end = _minute(row.end, allow_24=True)
        if start >= end:
            code = "CROSS_MIDNIGHT_MUST_BE_SPLIT" if start > end else "DISPATCH_WINDOW_EMPTY"
            raise StrategyModelError(code, "windows must be explicit within one day")
        if row.action not in ALLOWED_DISPATCH_ACTIONS:
            raise StrategyModelError("DISPATCH_ACTION_INVALID", "unknown dispatch action")
        target = _finite_decimal(row.target, "DISPATCH_TARGET_INVALID")
        soc_min = _finite_decimal(row.soc_min, "SOC_RANGE_INVALID")
        soc_max = _finite_decimal(row.soc_max, "SOC_RANGE_INVALID")
        if soc_min > soc_max or soc_min < 0 or soc_max > 100:
            raise StrategyModelError("SOC_RANGE_INVALID", "SOC range must be within 0..100")
        validated.append(
            (
                start,
                end,
                replace(row, target=target, soc_min=soc_min, soc_max=soc_max),
            )
        )
    validated.sort(key=lambda item: (item[0], item[1], item[2].key))
    for previous, current in zip(validated, validated[1:]):
        if current[0] < previous[1]:
            raise StrategyModelError(
                "DISPATCH_WINDOWS_OVERLAP",
                f"{previous[2].key} overlaps {current[2].key}",
            )
    table_rules = [
        {
            "_id": row.key,
            "site_local_minute": (
                f"site_local_minute >= {start} && site_local_minute < {end}"
            ),
            "soc": f"soc >= {_decimal(row.soc_min)} && soc <= {_decimal(row.soc_max)}",
            "action_id": json.dumps("power-target"),
            "target": _decimal(row.target),
            "matched_rule": json.dumps(row.key),
            "_description": row.action,
        }
        for start, end, row in validated
    ]
    table_rules.append(
        {
            "_id": "other-time",
            "site_local_minute": "1 == 1",
            "soc": "1 == 1",
            "action_id": json.dumps("power-target"),
            "target": _decimal(safe),
            "matched_rule": json.dumps("other-time"),
            "_description": "HOLD",
        }
    )
    table = {
        "hitPolicy": "first",
        "inputs": [
            {
                "id": "site_local_minute",
                "name": "场站本地分钟",
                "type": "expression",
                "field": "site_local_minute",
            },
            {"id": "soc", "name": "SOC", "type": "expression", "field": "soc"},
        ],
        "outputs": [
            {"id": "action_id", "name": "动作标识", "type": "expression", "field": "action_id"},
            {"id": "target", "name": "功率目标", "type": "expression", "field": "target"},
            {"id": "matched_rule", "name": "命中行", "type": "expression", "field": "matched_rule"},
        ],
        "rules": table_rules,
    }
    return {
        "nodes": [
            {"id": "input", "type": "inputNode", "name": "Input"},
            {"id": "schedule", "type": "decisionTableNode", "name": "2充2放", "content": table},
            {"id": "output", "type": "outputNode", "name": "Output"},
        ],
        "edges": [
            {"id": "input-schedule", "sourceId": "input", "targetId": "schedule", "type": "edge"},
            {"id": "schedule-output", "sourceId": "schedule", "targetId": "output", "type": "edge"},
        ],
    }


def extract_control_intents(
    outputs: Mapping[str, object],
    bindings: Mapping[str, OutputBinding],
) -> tuple[ControlIntentDraft, ...]:
    """Turn JDM result values into statically bound, ordered SET intents."""
    result = outputs.get("result", outputs)
    if isinstance(result, Mapping) and isinstance(result.get("intents"), list):
        raw_intents = result["intents"]
    elif isinstance(result, list):
        raw_intents = result
    elif isinstance(result, Mapping):
        raw_intents = [result]
    else:
        raise StrategyModelError("JDM_RESULT_INVALID", "decision result must be an object or list")
    intents: list[ControlIntentDraft] = []
    for ordinal, raw in enumerate(raw_intents):
        if not isinstance(raw, Mapping):
            raise StrategyModelError("JDM_RESULT_INVALID", "every intent result must be an object")
        action_id = raw.get("action_id")
        if not isinstance(action_id, str) or action_id not in bindings:
            raise StrategyModelError("OUTPUT_BINDING_MISSING", "action output has no static binding")
        if "target" not in raw:
            raise StrategyModelError("OUTPUT_TARGET_MISSING", "SET target is required")
        binding = bindings[action_id]
        if not binding.controllable:
            raise StrategyModelError("OUTPUT_NOT_CONTROLLABLE", "target L2 is read-only")
        if not binding.confirmed_write_point:
            raise StrategyModelError("OUTPUT_WRITE_POINT_UNCONFIRMED", "target has no confirmed write point")
        value = _typed_value(raw["target"], binding.data_type)
        intents.append(
            ControlIntentDraft(
                action_id=action_id,
                entity_instance_id=binding.entity_instance_id,
                value=value,
                ordinal=ordinal,
            )
        )
    return tuple(intents)


def _typed_value(value: object, data_type: str) -> object:
    if data_type == "BOOL":
        if type(value) is not bool:
            raise StrategyModelError("OUTPUT_TYPE_MISMATCH", "BOOL target must be boolean")
        return value
    if data_type == "INT":
        if type(value) is not int:
            raise StrategyModelError("OUTPUT_TYPE_MISMATCH", "INT target must be integer")
        return value
    if data_type == "FLOAT":
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise StrategyModelError("OUTPUT_TYPE_MISMATCH", "FLOAT target must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise StrategyModelError("OUTPUT_TYPE_MISMATCH", "FLOAT target must be finite")
        return number
    if data_type in {"STRING", "ENUM"}:
        if not isinstance(value, str):
            raise StrategyModelError("OUTPUT_TYPE_MISMATCH", "text target must be a string")
        return value
    if data_type == "CODE_SET":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise StrategyModelError("OUTPUT_TYPE_MISMATCH", "CODE_SET target must be text list")
        return tuple(value)
    raise StrategyModelError("OUTPUT_TYPE_UNSUPPORTED", f"unsupported output type {data_type}")


def validate_strategy_snapshot(
    revision: StrategyRevision,
    snapshot: StrategySnapshot,
) -> tuple[dict[str, object], dict[str, object]]:
    if snapshot.configuration_revision != revision.base_configuration_revision:
        raise StrategyModelError(
            "L2_CONFIGURATION_MISMATCH", "snapshot configuration changed"
        )
    samples = {item.entity_instance_id: item for item in snapshot.inputs}
    engine_inputs: dict[str, object] = {}
    actual: dict[str, object] = {}
    for binding in revision.bindings:
        sample = samples.get(binding.entity_instance_id)
        if sample is None:
            kind = "INPUT" if binding.direction == "INPUT" else "OUTPUT"
            raise StrategyModelError(f"L2_{kind}_MISSING", "bound L2 value is missing")
        if sample.frame_sequence > snapshot.frame_sequence:
            raise StrategyModelError("L2_AFTER_SNAPSHOT_HEAD", "L2 is newer than snapshot head")
        if sample.configuration_revision != snapshot.configuration_revision:
            raise StrategyModelError(
                "L2_CONFIGURATION_MISMATCH", "L2 configuration revision changed"
            )
        if sample.data_type != binding.expected_data_type:
            raise StrategyModelError("L2_TYPE_MISMATCH", "L2 data type changed")
        if _normalized_unit(sample.unit) != _normalized_unit(binding.unit):
            raise StrategyModelError("L2_UNIT_MISMATCH", "L2 unit changed")
        if sample.quality != "GOOD":
            raise StrategyModelError("L2_QUALITY_NOT_GOOD", "L2 quality is not GOOD")
        if sample.observed_at is None:
            raise StrategyModelError("L2_TIMESTAMP_MISSING", "L2 timestamp is missing")
        if (snapshot.evaluated_at - sample.observed_at).total_seconds() > binding.freshness_seconds:
            raise StrategyModelError("L2_INPUT_STALE", "bound L2 value exceeded freshness")
        validate_strategy_binding_role(binding, sample, value=sample.value)
        if binding.direction == "INPUT":
            engine_inputs[binding.binding_key] = sample.value
        else:
            actual[binding.binding_key] = sample.value
    local_time = snapshot.evaluated_at.astimezone(ZoneInfo(revision.site_timezone))
    engine_inputs["site_local_minute"] = local_time.hour * 60 + local_time.minute
    engine_inputs["site_local_time"] = local_time.isoformat()
    return engine_inputs, actual


def _values_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except InvalidOperation:
            return False
    return left == right


def _minute(value: str, *, allow_24: bool) -> int:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (AttributeError, TypeError, ValueError) as error:
        raise StrategyModelError("DISPATCH_TIME_INVALID", "time must use HH:MM") from error
    if hour == 24 and minute == 0 and allow_24:
        return 1440
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise StrategyModelError("DISPATCH_TIME_INVALID", "time is outside one day")
    return hour * 60 + minute


def _finite_decimal(value: object, code: str) -> Decimal:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise StrategyModelError(code, "value must be numeric") from error
    if not number.is_finite():
        raise StrategyModelError(code, "value must be finite")
    return number


def _decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _normalized_unit(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
