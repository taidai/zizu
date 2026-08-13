"""解决方案包参数契约校验与站点值解析。"""
from __future__ import annotations

import ipaddress
import math
import re
from typing import Any

from app.services.solution_delivery_contracts import DeliveryError


_PARAMETER_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_DURATION = re.compile(r"^([1-9]\d*)(ms|s|m|h)$")
_SECRET_REFERENCE = re.compile(r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_PARAMETER_TYPES = {
    "string",
    "integer",
    "number",
    "boolean",
    "enum",
    "address",
    "port",
    "duration",
    "secret",
}
_COMMON_FIELDS = {"id", "type", "required", "description", "default"}
_TYPE_FIELDS = {
    "string": {"pattern"},
    "integer": {"unit", "minimum", "maximum"},
    "number": {"unit", "minimum", "maximum"},
    "boolean": set(),
    "enum": {"values"},
    "address": {"pattern"},
    "port": {"minimum", "maximum"},
    "duration": set(),
    "secret": set(),
}


class _ParameterValueError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_parameter_contracts(value: Any) -> tuple[dict[str, Any], ...]:
    """Validate and return an immutable-order parameter contract list."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DeliveryError("MANIFEST_INVALID", "Parameters must be a list")
    parameter_ids: set[str] = set()
    contracts: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise DeliveryError("MANIFEST_INVALID", "Parameter contract must be a mapping")
        parameter_id = raw.get("id")
        parameter_type = raw.get("type")
        required = raw.get("required")
        description = raw.get("description")
        if (
            not isinstance(parameter_id, str)
            or _PARAMETER_ID.fullmatch(parameter_id) is None
            or parameter_id in parameter_ids
            or not isinstance(parameter_type, str)
            or parameter_type not in _PARAMETER_TYPES
            or not isinstance(required, bool)
            or not isinstance(description, str)
            or not description.strip()
            or set(raw) - (_COMMON_FIELDS | _TYPE_FIELDS[parameter_type])
        ):
            raise DeliveryError("MANIFEST_INVALID", "Parameter contract is invalid")
        if parameter_type == "secret" and "default" in raw:
            raise DeliveryError("MANIFEST_INVALID", "Secret parameters cannot have defaults")
        if "unit" in raw and (
            not isinstance(raw["unit"], str) or not raw["unit"].strip()
        ):
            raise DeliveryError("MANIFEST_INVALID", "Parameter unit is invalid")
        minimum = raw.get("minimum")
        maximum = raw.get("maximum")
        for limit in (minimum, maximum):
            if limit is not None and (
                isinstance(limit, bool)
                or not isinstance(limit, (int, float))
                or not math.isfinite(float(limit))
            ):
                raise DeliveryError("MANIFEST_INVALID", "Parameter range is invalid")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise DeliveryError("MANIFEST_INVALID", "Parameter range is invalid")
        if "pattern" in raw:
            pattern = raw["pattern"]
            if not isinstance(pattern, str) or len(pattern) > 256:
                raise DeliveryError("MANIFEST_INVALID", "Parameter pattern is invalid")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise DeliveryError("MANIFEST_INVALID", "Parameter pattern is invalid") from exc
            if not _is_safe_pattern(pattern):
                raise DeliveryError("MANIFEST_INVALID", "Parameter pattern is unsafe")
        if parameter_type == "enum":
            values = raw.get("values")
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(item, str) or not item for item in values)
                or len(values) != len(set(values))
            ):
                raise DeliveryError("MANIFEST_INVALID", "Enum values are invalid")
        if "default" in raw:
            try:
                _normalize_value(raw, raw["default"])
            except _ParameterValueError as exc:
                raise DeliveryError("MANIFEST_INVALID", "Parameter default is invalid") from exc
        parameter_ids.add(parameter_id)
        contracts.append(dict(raw))
    return tuple(contracts)


def resolve_site_parameters(
    contracts: tuple[dict[str, Any], ...],
    submitted: dict[str, Any],
    secret_references: dict[str, str],
    current_parameters: dict[str, Any] | None = None,
    current_secret_references: dict[str, str] | None = None,
    current_metadata: dict[str, dict[str, str]] | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, str],
    dict[str, str],
    tuple[dict[str, str], ...],
]:
    """Normalize site values and return deterministic, non-secret blockers."""
    normalized: dict[str, Any] = {}
    references: dict[str, str] = {}
    sources: dict[str, str] = {}
    blockers: list[dict[str, str]] = []
    contract_by_id = {contract["id"]: contract for contract in contracts}
    current_parameters = current_parameters or {}
    current_secret_references = current_secret_references or {}
    current_metadata = current_metadata or {}

    for parameter_id in sorted(set(submitted) - set(contract_by_id)):
        blockers.append(_blocker("PARAMETER_UNKNOWN", parameter_id, "Site parameter is not declared"))
    for parameter_id in sorted(set(secret_references) - set(contract_by_id)):
        blockers.append(
            _blocker("SECRET_REFERENCE_UNKNOWN", parameter_id, "Secret reference is not declared")
        )

    for contract in contracts:
        parameter_id = contract["id"]
        parameter_type = contract["type"]
        if parameter_type == "secret":
            if parameter_id in submitted:
                blockers.append(
                    _blocker(
                        "SECRET_VALUE_FORBIDDEN",
                        parameter_id,
                        "Secret values are forbidden; submit a Secret reference",
                    )
                )
            reference = secret_references.get(parameter_id)
            current_source = current_metadata.get(parameter_id, {}).get("source")
            if reference is None and current_source == "engineer_input":
                reference = current_secret_references.get(parameter_id)
            if reference is None:
                if contract["required"]:
                    blockers.append(
                        _blocker(
                            "SECRET_REFERENCE_REQUIRED",
                            parameter_id,
                            "Required Secret reference is missing",
                        )
                    )
            elif not isinstance(reference, str) or _SECRET_REFERENCE.fullmatch(reference) is None:
                blockers.append(
                    _blocker(
                        "SECRET_REFERENCE_INVALID",
                        parameter_id,
                        "Secret reference must use the secret:// scheme",
                    )
                )
            else:
                references[parameter_id] = reference
                sources[parameter_id] = (
                    "engineer_input"
                    if parameter_id in secret_references
                    else current_source or "engineer_input"
                )
            continue

        if parameter_id in secret_references:
            blockers.append(
                _blocker(
                    "PARAMETER_VALUE_REQUIRED",
                    parameter_id,
                    "Non-secret parameters cannot use Secret references",
                )
            )
        if parameter_id in submitted:
            raw_value = submitted[parameter_id]
            source = "engineer_input"
        elif current_metadata.get(parameter_id, {}).get("source") == "engineer_input":
            raw_value = current_parameters.get(parameter_id)
            source = "engineer_input"
        elif "default" in contract:
            raw_value = contract["default"]
            source = "package_default"
        elif contract["required"]:
            blockers.append(
                _blocker(
                    "PARAMETER_REQUIRED",
                    parameter_id,
                    "Required site parameter is missing",
                )
            )
            continue
        else:
            continue
        try:
            normalized[parameter_id] = _normalize_value(contract, raw_value)
            sources[parameter_id] = source
        except _ParameterValueError as exc:
            blockers.append(_blocker(exc.code, parameter_id, str(exc)))

    return normalized, references, sources, tuple(blockers)


def _normalize_value(contract: dict[str, Any], value: Any) -> Any:
    parameter_type = contract["type"]
    if parameter_type == "string":
        if not isinstance(value, str):
            raise _ParameterValueError("PARAMETER_TYPE_INVALID", "Expected a string")
        pattern = contract.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise _ParameterValueError("PARAMETER_PATTERN_INVALID", "String does not match pattern")
        return value
    if parameter_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _ParameterValueError("PARAMETER_TYPE_INVALID", "Expected an integer")
        _check_range(contract, value)
        return value
    if parameter_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise _ParameterValueError("PARAMETER_TYPE_INVALID", "Expected a finite number")
        _check_range(contract, value)
        return value
    if parameter_type == "boolean":
        if not isinstance(value, bool):
            raise _ParameterValueError("PARAMETER_TYPE_INVALID", "Expected a boolean")
        return value
    if parameter_type == "enum":
        if not isinstance(value, str) or value not in contract["values"]:
            raise _ParameterValueError("PARAMETER_ENUM_INVALID", "Value is not an allowed enum member")
        return value
    if parameter_type == "address":
        if not isinstance(value, str) or not _valid_address(value):
            raise _ParameterValueError("PARAMETER_ADDRESS_INVALID", "Expected an IP address or hostname")
        pattern = contract.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise _ParameterValueError("PARAMETER_PATTERN_INVALID", "Address does not match pattern")
        return value
    if parameter_type == "port":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise _ParameterValueError("PARAMETER_PORT_INVALID", "Port must be between 1 and 65535")
        _check_range(contract, value)
        return value
    if parameter_type == "duration":
        if not isinstance(value, str) or _DURATION.fullmatch(value) is None:
            raise _ParameterValueError("PARAMETER_DURATION_INVALID", "Duration must use ms, s, m, or h")
        return value
    raise _ParameterValueError("PARAMETER_TYPE_INVALID", "Unsupported parameter type")


def _check_range(contract: dict[str, Any], value: int | float) -> None:
    if ("minimum" in contract and value < contract["minimum"]) or (
        "maximum" in contract and value > contract["maximum"]
    ):
        raise _ParameterValueError("PARAMETER_RANGE_INVALID", "Value is outside the allowed range")


def _valid_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return _HOSTNAME.fullmatch(value) is not None


def _is_safe_pattern(pattern: str) -> bool:
    """Reject constructs that make manifest-provided regex matching unbounded."""
    if re.search(r"\\[1-9]|\(\?P=|\(\?=|\(\?!|\(\?<=|\(\?<!", pattern):
        return False
    if re.search(r"\([^()]*(?:[+*]|\{\d+(?:,\d*)?\})[^()]*\)(?:[+*]|\{)", pattern):
        return False
    return True


def _blocker(code: str, parameter_id: str, message: str) -> dict[str, str]:
    return {"code": code, "parameter_id": parameter_id, "message": message}
