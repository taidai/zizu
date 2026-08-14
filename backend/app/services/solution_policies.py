"""Validate the small, data-only EMS policy asset grammar.

Policies describe an operational decision, not executable code.  They may
refer only to entity definitions declared in the same solution package; the
runtime later resolves those definitions through the confirmed installation.
"""
from __future__ import annotations

import re
import math
from typing import Any, Callable

from app.services.solution_delivery_contracts import DeliveryError


def validate_ems_policy_assets(
    manifest: dict[str, Any],
    assets: dict[str, bytes],
    slots: tuple[dict[str, Any], ...],
    load_mapping: Callable[[bytes | None, str], dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    declarations = [item for item in manifest["assets"] if item["kind"] == "ems_policy"]
    known = {
        slot["id"]: {definition["id"]: definition for definition in slot["definitions"]}
        for slot in slots
    }
    normalized: list[dict[str, Any]] = []
    for declaration in declarations:
        raw = load_mapping(assets.get(declaration["path"]), "POLICY_ASSET_INVALID")
        fields = {"schemaVersion", "id", "kind", "revision", "input", "condition", "action", "simulation"}
        if (
            set(raw) != fields
            or raw.get("schemaVersion") != "zizu.ems-policy/v1alpha1"
            or raw.get("id") != declaration["id"]
            or raw.get("kind") != "ems_policy"
            or not isinstance(raw.get("revision"), int)
            or raw["revision"] < 1
        ):
            raise DeliveryError("POLICY_ASSET_INVALID", "EMS policy identity is invalid")
        input_reference, input_definition = _reference(raw.get("input"), known, "POLICY_INPUT_INVALID")
        if input_definition["data_type"] not in {"FLOAT", "INT"}:
            raise DeliveryError("POLICY_INPUT_INVALID", "EMS policy input must be numeric")
        if raw["input"].get("unit") != input_definition.get("unit"):
            raise DeliveryError("POLICY_UNIT_INCOMPATIBLE", "EMS policy input unit must match its entity")
        condition = raw.get("condition")
        if (
            not isinstance(condition, dict)
            or set(condition) != {"operator", "threshold"}
            or condition.get("operator") not in {"gt", "gte", "lt", "lte"}
            or not _finite_number(condition.get("threshold"))
        ):
            raise DeliveryError("POLICY_CONDITION_INVALID", "EMS policy condition is invalid")
        action = raw.get("action")
        if (
            not isinstance(action, dict)
            or set(action) - {"id", "target", "value", "unit", "highRiskAuthorization"}
            or not {"id", "target", "value", "unit"}.issubset(action)
            or not isinstance(action.get("id"), str)
            or not action["id"].strip()
            or not _finite_number(action.get("value"))
        ):
            raise DeliveryError("POLICY_ACTION_INVALID", "EMS policy action is invalid")
        target_reference, target_definition = _reference(action.get("target"), known, "POLICY_TARGET_INVALID")
        if target_definition["direction"] not in {"W", "RW"}:
            raise DeliveryError("POLICY_TARGET_NOT_WRITABLE", "EMS policy target must be writable")
        if target_definition.get("control") is None:
            raise DeliveryError("POLICY_TARGET_NOT_CONFIGURED", "EMS policy target needs a declared control policy")
        if target_definition["data_type"] not in {"FLOAT", "INT"}:
            raise DeliveryError("POLICY_TARGET_INVALID", "EMS policy target must be numeric")
        if action.get("unit") != target_definition.get("unit"):
            raise DeliveryError("POLICY_UNIT_INCOMPATIBLE", "EMS policy action unit must match its target")
        authorization = _high_risk_authorization(
            action.get("highRiskAuthorization"),
            target_definition,
            action["value"],
        )
        simulation = _simulation(raw.get("simulation"), raw["input"], condition, action)
        normalized.append(
            {
                "id": declaration["id"],
                "revision": raw["revision"],
                "input": {**input_reference, "unit": raw["input"]["unit"]},
                "condition": {"operator": condition["operator"], "threshold": condition["threshold"]},
                "action": {
                    "id": action["id"].strip(),
                    "target": target_reference,
                    "value": action["value"],
                    "unit": action["unit"],
                    "high_risk_authorization": authorization,
                },
                "simulation": simulation,
            }
        )
    if len({item["id"] for item in normalized}) != len(normalized):
        raise DeliveryError("POLICY_ASSET_INVALID", "EMS policy asset IDs must be unique")
    return tuple(normalized)


def _high_risk_authorization(
    raw: Any,
    target_definition: dict[str, Any],
    action_value: float | int,
) -> dict[str, float] | None:
    """Keep the only automated high-risk exception explicit and value-bounded."""
    control = target_definition.get("control")
    high_risk = isinstance(control, dict) and control.get("high_risk") is True
    if raw is None:
        return None
    if not high_risk:
        raise DeliveryError(
            "POLICY_HIGH_RISK_AUTHORIZATION_INVALID",
            "Only a high-risk target may declare an automation authorization",
        )
    if (
        not isinstance(raw, dict)
        or set(raw) != {"maximumAbsoluteValue"}
        or not _finite_number(raw.get("maximumAbsoluteValue"))
        or float(raw["maximumAbsoluteValue"]) <= 0
        or abs(float(action_value)) > float(raw["maximumAbsoluteValue"])
    ):
        raise DeliveryError(
            "POLICY_HIGH_RISK_AUTHORIZATION_INVALID",
            "High-risk policy authorization must bound the declared action value",
        )
    return {"maximum_absolute_value": float(raw["maximumAbsoluteValue"])}


def validate_policy_execution_acceptances(
    manifest: dict[str, Any],
    assets: dict[str, bytes],
    policies: tuple[dict[str, Any], ...],
    load_mapping: Callable[[bytes | None, str], dict[str, Any]],
) -> None:
    """Validate the narrowly scoped acceptance that exercises one declared policy."""
    policy_actions = {item["id"]: item["action"]["id"] for item in policies}
    declarations = {item["id"]: item for item in manifest["assets"]}
    for acceptance_id in manifest["acceptance"]:
        raw = load_mapping(assets.get(declarations[acceptance_id]["path"]), "ASSET_REFERENCE_INVALID")
        if raw.get("kind") != "policy_execution":
            continue
        fields = {"schemaVersion", "id", "kind", "required", "policy", "expectedAction", "timeout"}
        if (
            set(raw) != fields
            or raw.get("schemaVersion") != "zizu.acceptance/v1alpha1"
            or raw.get("id") != acceptance_id
            or not isinstance(raw.get("required"), bool)
            or raw.get("policy") not in policy_actions
            or raw.get("expectedAction") != policy_actions[raw["policy"]]
            or not isinstance(raw.get("timeout"), str)
            or re.fullmatch(r"[1-9]\d*s", raw["timeout"]) is None
        ):
            raise DeliveryError("POLICY_ACCEPTANCE_INVALID", "Policy execution acceptance is invalid")


def _simulation(
    raw: Any,
    policy_input: dict[str, Any],
    condition: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"input", "expected"}:
        raise DeliveryError("POLICY_SIMULATION_INVALID", "EMS policy needs one fixed simulation")
    input_value = raw.get("input")
    expected = raw.get("expected")
    if (
        not isinstance(input_value, dict)
        or set(input_value) != {"value", "unit"}
        or not _finite_number(input_value.get("value"))
        or input_value.get("unit") != policy_input.get("unit")
        or not isinstance(expected, dict)
        or set(expected) != {"triggered", "actionValue"}
        or not isinstance(expected.get("triggered"), bool)
        or (expected.get("actionValue") is not None and not _finite_number(expected.get("actionValue")))
    ):
        raise DeliveryError("POLICY_SIMULATION_INVALID", "EMS policy simulation is invalid")
    triggered = _condition_matches(float(input_value["value"]), condition["operator"], float(condition["threshold"]))
    action_value = action["value"] if triggered else None
    if expected["triggered"] != triggered or expected["actionValue"] != action_value:
        raise DeliveryError("POLICY_SIMULATION_FAILED", "EMS policy simulation expectation does not match its declaration")
    return {
        "input": {"value": input_value["value"], "unit": input_value["unit"]},
        "expected": {"triggered": triggered, "actionValue": action_value},
    }


def _condition_matches(value: float, operator: str, threshold: float) -> bool:
    return {
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
    }[operator]


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _reference(
    raw: Any,
    known: dict[str, dict[str, dict[str, Any]]],
    code: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) - {"slot", "definition", "unit"} or set(raw) & {"slot", "definition"} != {"slot", "definition"}:
        raise DeliveryError(code, "EMS policy entity reference is invalid")
    slot = raw.get("slot")
    definition = raw.get("definition")
    if not isinstance(slot, str) or not isinstance(definition, str):
        raise DeliveryError(code, "EMS policy entity reference is invalid")
    item = known.get(slot, {}).get(definition)
    if item is None:
        raise DeliveryError(code, "EMS policy references an unknown entity")
    return {"slot": slot, "definition": definition}, item
