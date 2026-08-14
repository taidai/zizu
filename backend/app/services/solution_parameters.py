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
    "device_instances",
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
    "device_instances": {"minimumItems", "maximumItems"},
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
        if parameter_type == "device_instances":
            minimum_items = raw.get("minimumItems", 1)
            maximum_items = raw.get("maximumItems", 64)
            if (
                isinstance(minimum_items, bool)
                or isinstance(maximum_items, bool)
                or not isinstance(minimum_items, int)
                or not isinstance(maximum_items, int)
                or not 1 <= minimum_items <= maximum_items <= 64
            ):
                raise DeliveryError("MANIFEST_INVALID", "Device instance limits are invalid")
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


def upgrade_parameter_conflicts(
    previous_contracts: tuple[dict[str, Any], ...],
    next_contracts: tuple[dict[str, Any], ...],
    *,
    current_parameters: dict[str, Any],
    current_metadata: dict[str, dict[str, str]],
    submitted: dict[str, Any],
    secret_references: dict[str, str],
) -> tuple[dict[str, str], ...]:
    """Return explicit three-way conflicts for inherited site overrides.

    An explicit value in the upgrade request is the engineer's individual
    resolution. Otherwise, preserve an existing override only when the package
    did not also change the previous default beneath it.
    """
    previous_by_id = {contract["id"]: contract for contract in previous_contracts}
    conflicts: list[dict[str, str]] = []
    for contract in next_contracts:
        parameter_id = contract["id"]
        if parameter_id in submitted or parameter_id in secret_references:
            continue
        previous = previous_by_id.get(parameter_id)
        if (
            previous is None
            or previous.get("type") == "secret"
            or contract.get("type") == "secret"
            or "default" not in previous
            or "default" not in contract
            or previous["default"] == contract["default"]
            or current_metadata.get(parameter_id, {}).get("source")
            != "engineer_input"
            or current_parameters.get(parameter_id) == previous["default"]
        ):
            continue
        conflicts.append(
            _blocker(
                "UPGRADE_PARAMETER_CONFLICT",
                parameter_id,
                "Package and site override both changed this parameter",
            )
        )
    return tuple(conflicts)


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
    if parameter_type == "device_instances":
        if not isinstance(value, list):
            raise _ParameterValueError(
                "PARAMETER_TYPE_INVALID",
                "Expected a device instance list",
            )
        minimum_items = contract.get("minimumItems", 1)
        maximum_items = contract.get("maximumItems", 64)
        if not minimum_items <= len(value) <= maximum_items:
            raise _ParameterValueError(
                "PARAMETER_RANGE_INVALID",
                "Device instance count is outside the allowed range",
            )
        normalized: list[dict[str, str]] = []
        instance_keys: set[str] = set()
        device_keys: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or not {"instance_key", "device_key"} <= set(item) \
                    or set(item) - {
                        "instance_key", "device_key", "standby_device_key", "display_name"
                    }:
                raise _ParameterValueError(
                    "PARAMETER_DEVICE_INSTANCE_INVALID",
                    "Device instance fields are invalid",
                )
            instance_key = item.get("instance_key")
            device_key = item.get("device_key")
            display_name = item.get("display_name")
            standby_device_key = item.get("standby_device_key")
            if (
                not _valid_instance_text(instance_key)
                or not _valid_instance_text(device_key)
                or (display_name is not None and not _valid_instance_text(display_name))
                or (
                    standby_device_key is not None
                    and (
                        not _valid_instance_text(standby_device_key)
                        or standby_device_key == device_key
                    )
                )
                or instance_key in instance_keys
                or device_key in device_keys
                or standby_device_key in device_keys
            ):
                raise _ParameterValueError(
                    "PARAMETER_DEVICE_INSTANCE_INVALID",
                    "Device instance keys must be unique, non-empty strings",
                )
            normalized.append(
                {
                    "instance_key": instance_key,
                    "device_key": device_key,
                    **({"display_name": display_name} if display_name is not None else {}),
                    **(
                        {"standby_device_key": standby_device_key}
                        if standby_device_key is not None
                        else {}
                    ),
                }
            )
            instance_keys.add(instance_key)
            device_keys.add(device_key)
            if standby_device_key is not None:
                device_keys.add(standby_device_key)
        return sorted(normalized, key=lambda item: item["instance_key"])
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


def _valid_instance_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and len(value) <= 128
        and all(character.isprintable() for character in value)
    )


def _is_safe_pattern(pattern: str) -> bool:
    """Reject constructs that make manifest-provided regex matching unbounded."""
    if re.search(r"\\[1-9]|\(\?P=|\(\?=|\(\?!|\(\?<=|\(\?<!", pattern):
        return False
    if re.search(r"\([^()]*(?:[+*]|\{\d+(?:,\d*)?\})[^()]*\)(?:[+*]|\{)", pattern):
        return False
    return True


def _blocker(code: str, parameter_id: str, message: str) -> dict[str, str]:
    return {"code": code, "parameter_id": parameter_id, "message": message}
