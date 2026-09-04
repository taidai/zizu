"""Domain model for user-facing dispatch strategies."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math
from typing import Mapping, Sequence
from uuid import UUID


ALLOWED_DISPATCH_ACTIONS = frozenset({"CHARGE", "DISCHARGE", "HOLD"})


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
