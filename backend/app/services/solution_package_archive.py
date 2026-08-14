"""解决方案包归档的安全读取、清单校验与规范摘要。"""
from __future__ import annotations

import hashlib
import io
import math
import re
import stat
from typing import Any
import zipfile

import yaml

from app.services.solution_delivery_contracts import (
    DeliveryError,
    MAX_PACKAGE_ARCHIVE_BYTES,
)
from app.services.solution_parameters import validate_parameter_contracts


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARCHIVE_FILES = 256
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_EXPANDED_BYTES = 20 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_EXECUTABLE_SUFFIXES = {
    ".bat",
    ".cjs",
    ".cmd",
    ".com",
    ".dll",
    ".dylib",
    ".exe",
    ".jar",
    ".js",
    ".mjs",
    ".ps1",
    ".py",
    ".pyc",
    ".sh",
    ".so",
    ".sql",
    ".wasm",
}


def _read_archive(archive: bytes) -> dict[str, bytes]:
    if len(archive) > MAX_PACKAGE_ARCHIVE_BYTES:
        raise DeliveryError("PACKAGE_LIMIT_EXCEEDED", "ZIP archive exceeds 10 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            entries = [info for info in package.infolist() if not info.is_dir()]
            if len(entries) > _MAX_ARCHIVE_FILES:
                raise DeliveryError(
                    "PACKAGE_LIMIT_EXCEEDED",
                    "ZIP archive contains more than 256 files",
                )
            if sum(info.file_size for info in entries) > _MAX_EXPANDED_BYTES:
                raise DeliveryError(
                    "PACKAGE_LIMIT_EXCEEDED",
                    "ZIP archive expands beyond 20 MiB",
                )

            files: dict[str, bytes] = {}
            casefold_paths: set[str] = set()
            for info in entries:
                header = info.header_offset
                if archive[header : header + 4] != b"PK\x03\x04":
                    raise DeliveryError(
                        "PACKAGE_ARCHIVE_UNSAFE",
                        "ZIP local header is invalid",
                    )
                raw_name_length = int.from_bytes(
                    archive[header + 26 : header + 28],
                    "little",
                )
                raw_name = archive[header + 30 : header + 30 + raw_name_length]
                if b"\\" in raw_name or b"\0" in raw_name:
                    raise DeliveryError(
                        "PACKAGE_ARCHIVE_UNSAFE",
                        "ZIP entry uses an ambiguous raw path",
                    )
                path = info.filename
                parts = path.split("/")
                if (
                    not path
                    or "\0" in path
                    or "\\" in path
                    or path.startswith("/")
                    or ".." in parts
                    or any(part in ("", ".") for part in parts)
                    or ":" in parts[0]
                ):
                    raise DeliveryError(
                        "PACKAGE_ARCHIVE_UNSAFE",
                        "ZIP entry has an unsafe or ambiguous path",
                    )
                folded = path.casefold()
                if path in files or folded in casefold_paths:
                    raise DeliveryError(
                        "PACKAGE_ARCHIVE_UNSAFE",
                        "ZIP entry path is duplicated or case-ambiguous",
                    )
                if info.flag_bits & 0x1:
                    raise DeliveryError(
                        "PACKAGE_ARCHIVE_UNSAFE",
                        "Encrypted ZIP entries are not allowed",
                    )
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                if file_type not in (0, stat.S_IFREG):
                    raise DeliveryError(
                        "PACKAGE_ARCHIVE_UNSAFE",
                        "Links and device entries are not allowed",
                    )
                if info.file_size > _MAX_FILE_BYTES:
                    raise DeliveryError(
                        "PACKAGE_LIMIT_EXCEEDED",
                        "ZIP entry expands beyond 2 MiB",
                    )
                if info.file_size and (
                    info.compress_size == 0
                    or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
                ):
                    raise DeliveryError(
                        "PACKAGE_LIMIT_EXCEEDED",
                        "ZIP entry compression ratio exceeds 100:1",
                    )
                suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
                if suffix in _EXECUTABLE_SUFFIXES:
                    raise DeliveryError(
                        "PACKAGE_ARCHIVE_UNSAFE",
                        "Executable content is not allowed in a solution package",
                    )
                files[path] = package.read(info)
                casefold_paths.add(folded)
            return files
    except DeliveryError:
        raise
    except (zipfile.BadZipFile, NotImplementedError, OSError, RuntimeError) as exc:
        raise DeliveryError("PACKAGE_ARCHIVE_UNSAFE", "Invalid ZIP archive") from exc


def _load_mapping(content: bytes | None, code: str) -> dict[str, Any]:
    if content is None:
        raise DeliveryError(code, "Required YAML document is missing")
    try:
        value = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise DeliveryError(code, "Invalid YAML document") from exc
    if not isinstance(value, dict):
        raise DeliveryError(code, "YAML document must be a mapping")
    return value


def _validate_manifest(manifest: dict[str, Any]) -> None:
    scalar_fields = ("id", "version", "displayName")
    if manifest.get("schemaVersion") != "zizu.solution/v1alpha1" or any(
        not isinstance(manifest.get(field), str) or not manifest[field]
        for field in scalar_fields
    ):
        raise DeliveryError("MANIFEST_INVALID", "Manifest identity is invalid")
    platform = manifest.get("platform")
    if not isinstance(platform, dict) or not isinstance(platform.get("version"), str):
        raise DeliveryError("MANIFEST_INVALID", "Platform range is required")
    assets = manifest.get("assets")
    acceptance = manifest.get("acceptance")
    if not isinstance(assets, list) or not isinstance(acceptance, list):
        raise DeliveryError("MANIFEST_INVALID", "Assets and acceptance must be lists")
    asset_ids: set[str] = set()
    asset_paths: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise DeliveryError("MANIFEST_INVALID", "Asset must be a mapping")
        if asset.get("kind") not in {
            "acceptance",
            "entity_definition",
            "entity_instance_slot",
            "alarm_definition",
            "ems_workbench",
            "ems_policy",
        } or any(
            not isinstance(asset.get(field), str) or not asset[field]
            for field in ("id", "path", "sha256")
        ):
            raise DeliveryError("MANIFEST_INVALID", "Acceptance asset is invalid")
        if not _SHA256.fullmatch(asset["sha256"]):
            raise DeliveryError("MANIFEST_INVALID", "Asset sha256 is invalid")
        if asset["id"] in asset_ids or asset["path"] in asset_paths:
            raise DeliveryError(
                "MANIFEST_INVALID",
                "Asset ids and paths must be unique",
            )
        asset_ids.add(asset["id"])
        asset_paths.add(asset["path"])
    validate_parameter_contracts(manifest.get("parameters"))


def _validate_entity_assets(
    manifest: dict[str, Any],
    assets: dict[str, bytes],
) -> tuple[dict[str, Any], ...]:
    """校验定义/槽位/实体验收并返回 Registry 使用的规范槽位。"""
    declarations = {item["id"]: item for item in manifest["assets"]}
    definitions: dict[str, dict[str, Any]] = {}
    slots: list[dict[str, Any]] = []
    parameter_contracts = {
        item["id"]: item for item in validate_parameter_contracts(manifest.get("parameters"))
    }
    for declaration in declarations.values():
        if declaration["kind"] != "entity_definition":
            continue
        definition = _load_mapping(
            assets[declaration["path"]],
            "ASSET_REFERENCE_INVALID",
        )
        fields = {
            "schemaVersion",
            "id",
            "kind",
            "displayName",
            "deviceCategory",
            "dataType",
            "unit",
            "direction",
            "control",
        }
        if (
            set(definition) - fields
            or definition.get("schemaVersion") != "zizu.entity-definition/v1alpha1"
            or definition.get("id") != declaration["id"]
            or definition.get("kind") != "entity_definition"
            or any(
                not isinstance(definition.get(field), str) or not definition[field]
                for field in ("displayName", "deviceCategory", "dataType", "direction")
            )
            or definition["dataType"] not in {"FLOAT", "INT", "BOOL", "STRING", "ENUM"}
            or definition["direction"] not in {"R", "W", "RW"}
            or not isinstance(definition.get("unit"), (str, type(None)))
        ):
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Entity definition is invalid",
            )
        definitions[definition["id"]] = definition

    for definition_id, definition in tuple(definitions.items()):
        control = _validate_control_policy(definition, definitions)
        definitions[definition_id] = {
            **definition,
            **({"control": control} if control is not None else {}),
        }

    slot_ids: set[str] = set()
    slot_definition_ids: dict[str, set[str]] = {}
    for declaration in declarations.values():
        if declaration["kind"] != "entity_instance_slot":
            continue
        raw = _load_mapping(assets[declaration["path"]], "ASSET_REFERENCE_INVALID")
        common_fields = {
            "schemaVersion",
            "id",
            "kind",
            "deviceCategory",
            "displayName",
            "freshness",
            "requiredEntities",
        }
        legacy_fields = common_fields | {"count", "instanceKeyParameter"}
        multi_fields = common_fields | {"instancesParameter"}
        multi_device = set(raw) == multi_fields
        parameter_id = raw.get("instanceKeyParameter")
        instances_parameter_id = raw.get("instancesParameter")
        parameter = parameter_contracts.get(
            instances_parameter_id if multi_device else parameter_id
        )
        required_entities = raw.get("requiredEntities")
        if (
            set(raw) not in (legacy_fields, multi_fields)
            or raw.get("schemaVersion") != "zizu.entity-instance-slot/v1alpha1"
            or raw.get("id") != declaration["id"]
            or raw.get("kind") != "entity_instance_slot"
            or (not multi_device and raw.get("count") != 1)
            or any(
                not isinstance(raw.get(field), str) or not raw[field]
                for field in ("deviceCategory", "displayName", "freshness")
            )
            or parameter is None
            or parameter["type"] != ("device_instances" if multi_device else "string")
            or not isinstance(required_entities, list)
            or not required_entities
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "Entity slot is invalid")
        freshness = _duration_seconds(raw["freshness"])
        normalized_definitions: list[dict[str, Any]] = []
        required_definition_ids: set[str] = set()
        matcher_ids: set[str] = set()
        for required in required_entities:
            if not isinstance(required, dict) or set(required) != {"definition", "matcher"}:
                raise DeliveryError("ASSET_REFERENCE_INVALID", "Entity slot reference is invalid")
            definition = definitions.get(required["definition"])
            matcher = required.get("matcher")
            if (
                definition is None
                or definition["id"] in required_definition_ids
                or not isinstance(matcher, dict)
                or set(matcher) not in (
                    ({"id", "tagName"}, {"id", "tagName", "failoverPolicy"})
                    if multi_device
                    else ({"id", "deviceKeyParameter", "tagName"},)
                )
                or any(
                    not isinstance(matcher.get(field), str) or not matcher[field]
                    for field in (
                        ("id", "tagName")
                        if multi_device
                        else ("id", "deviceKeyParameter", "tagName")
                    )
                )
                or matcher["id"] in matcher_ids
                or matcher.get("failoverPolicy") not in (None, "manual")
                or (
                    not multi_device
                    and (
                        matcher["deviceKeyParameter"] not in parameter_contracts
                        or parameter_contracts[matcher["deviceKeyParameter"]]["type"]
                        != "string"
                    )
                )
                or definition["deviceCategory"] != raw["deviceCategory"]
            ):
                raise DeliveryError("ASSET_REFERENCE_INVALID", "Entity slot reference is invalid")
            required_definition_ids.add(definition["id"])
            matcher_ids.add(matcher["id"])
            normalized_definitions.append(
                {
                    "id": definition["id"],
                    "display_name": definition["displayName"],
                    "data_type": definition["dataType"],
                    "unit": definition.get("unit"),
                    "direction": definition["direction"],
                    **(
                        {"control": definition["control"]}
                        if definition.get("control") is not None
                        else {}
                    ),
                    "matcher": {
                        "id": matcher["id"],
                        "tag_name": matcher["tagName"],
                        **(
                            {}
                            if multi_device
                            else {"device_key_parameter": matcher["deviceKeyParameter"]}
                        ),
                        **(
                            {"failover_policy": "manual"}
                            if matcher.get("failoverPolicy") == "manual"
                            else {}
                        ),
                    },
                }
            )
        for definition in normalized_definitions:
            control = definition.get("control")
            if control is None:
                continue
            referenced = {
                control["readback_definition"],
                *(item["definition_id"] for item in control["interlocks"]),
            }
            if not referenced.issubset(required_definition_ids):
                raise DeliveryError(
                    "ASSET_REFERENCE_INVALID",
                    "Control policy references must belong to the same entity slot",
                )
        slot_ids.add(raw["id"])
        slot_definition_ids[raw["id"]] = required_definition_ids
        slots.append(
            {
                "id": raw["id"],
                "device_category": raw["deviceCategory"],
                **(
                    {"instances_parameter": instances_parameter_id}
                    if multi_device
                    else {"instance_key_parameter": parameter_id}
                ),
                "display_name": raw["displayName"],
                "freshness_seconds": freshness,
                "definitions": normalized_definitions,
            }
        )

    for acceptance_id in manifest["acceptance"]:
        declaration = declarations[acceptance_id]
        definition = _load_mapping(assets[declaration["path"]], "ASSET_REFERENCE_INVALID")
        if definition.get("kind") not in {"entity_readiness", "history_readiness"}:
            continue
        entity_fields = {
            "schemaVersion", "id", "kind", "required", "slot",
            "definition", "freshness", "timeout",
        }
        history_fields = {
            "schemaVersion", "id", "kind", "required", "slot",
            "definition", "range", "minimumSamples", "timeout",
        }
        if (
            (set(definition) != entity_fields and set(definition) != history_fields)
            or definition.get("schemaVersion") != "zizu.acceptance/v1alpha1"
            or definition.get("id") != acceptance_id
            or not isinstance(definition.get("required"), bool)
            or definition.get("slot") not in slot_ids
            or definition.get("definition")
            not in slot_definition_ids.get(definition.get("slot"), set())
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "Entity acceptance is invalid")
        if definition["kind"] == "entity_readiness":
            _duration_seconds(definition.get("freshness"))
        elif (
            definition.get("range") not in {"1h", "24h", "7d", "30d"}
            or not isinstance(definition.get("minimumSamples"), int)
            or isinstance(definition.get("minimumSamples"), bool)
            or definition["minimumSamples"] < 1
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "History acceptance is invalid")
        _validate_timeout(definition.get("timeout"))
    return tuple(slots)


def _validate_alarm_assets(
    manifest: dict[str, Any],
    assets: dict[str, bytes],
    slots: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Normalize the deliberately small v1 alarm-definition grammar."""
    definitions_by_id = {
        definition["id"]: definition
        for slot in slots
        for definition in slot["definitions"]
    }
    slots_by_id = {slot["id"]: slot for slot in slots}
    normalized: list[dict[str, Any]] = []
    seen_asset_ids: set[str] = set()
    for declaration in manifest["assets"]:
        if declaration["kind"] != "alarm_definition":
            continue
        raw = _load_mapping(assets[declaration["path"]], "ASSET_REFERENCE_INVALID")
        fields = {
            "schemaVersion", "id", "kind", "version", "slot", "entityDefinition",
            "trigger", "triggerDuration", "recovery", "recoveryDuration", "severity",
            "notificationThrottle",
        }
        if (
            set(raw) != fields
            or raw.get("schemaVersion") != "zizu.alarm-definition/v1alpha1"
            or raw.get("id") != declaration["id"]
            or raw.get("kind") != "alarm_definition"
            or not isinstance(raw.get("version"), str)
            or not raw["version"]
            or raw.get("id") in seen_asset_ids
            or raw.get("slot") not in slots_by_id
            or raw.get("entityDefinition") not in definitions_by_id
            or raw.get("entityDefinition")
            not in {item["id"] for item in slots_by_id[raw["slot"]]["definitions"]}
            or raw.get("severity") not in {"INFO", "WARNING", "MAJOR", "CRITICAL"}
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "Alarm definition is invalid")
        trigger = _validate_alarm_condition(raw.get("trigger"))
        recovery = _validate_alarm_condition(raw.get("recovery"))
        normalized.append(
            {
                "id": raw["id"],
                "version": raw["version"],
                "slot": raw["slot"],
                "entity_definition": raw["entityDefinition"],
                "trigger": trigger,
                "trigger_duration_seconds": _duration_seconds(raw["triggerDuration"]),
                "recovery": recovery,
                "recovery_duration_seconds": _duration_seconds(raw["recoveryDuration"]),
                "severity": raw["severity"],
                "notification_throttle_seconds": _duration_seconds(raw["notificationThrottle"]),
            }
        )
        seen_asset_ids.add(raw["id"])
    return tuple(normalized)


def _validate_alarm_lifecycle_acceptances(
    manifest: dict[str, Any],
    assets: dict[str, bytes],
    alarm_assets: tuple[dict[str, Any], ...],
) -> None:
    """Validate report-only alarm lifecycle acceptance declarations."""
    declarations = {item["id"]: item for item in manifest["assets"]}
    known_alarm_ids = {item["id"] for item in alarm_assets}
    for acceptance_id in manifest["acceptance"]:
        declaration = declarations[acceptance_id]
        definition = _load_mapping(
            assets[declaration["path"]],
            "ASSET_REFERENCE_INVALID",
        )
        if definition.get("kind") != "alarm_lifecycle":
            continue
        required_fields = {
            "schemaVersion",
            "id",
            "kind",
            "required",
            "alarmDefinition",
            "expectedState",
            "timeout",
        }
        if (
            set(definition) != required_fields
            or definition.get("schemaVersion") != "zizu.acceptance/v1alpha1"
            or definition.get("id") != acceptance_id
            or not isinstance(definition.get("required"), bool)
            or definition.get("alarmDefinition") not in known_alarm_ids
            or definition.get("expectedState") not in {"recovered"}
        ):
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Alarm lifecycle acceptance is invalid",
            )
        _validate_timeout(definition.get("timeout"))


def _validate_alarm_condition(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"op", "value"}:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Alarm condition is invalid")
    operator = raw.get("op")
    value = raw.get("value")
    if operator not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Alarm condition is invalid")
    if operator in {"gt", "gte", "lt", "lte"} and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Alarm numeric condition is invalid")
    if not isinstance(value, (str, int, float, bool)) or value is None:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Alarm condition value is invalid")
    return {"op": operator, "value": value}


def _duration_seconds(value: Any) -> float:
    if not isinstance(value, str):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Duration is invalid")
    match = re.fullmatch(r"([1-9]\d*)s", value)
    if match is None:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Duration is invalid")
    return float(match.group(1))


def _validate_control_policy(
    definition: dict[str, Any],
    definitions: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Normalize the intentionally small, declarative control policy grammar."""
    raw = definition.get("control")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Control policy is invalid")
    fields = {"minimum", "maximum", "cooldown", "readback", "interlocks", "highRisk"}
    if (
        set(raw) - fields
        or any(field not in raw for field in ("cooldown", "readback", "interlocks", "highRisk"))
        or definition["direction"] not in {"W", "RW"}
        or not isinstance(raw["highRisk"], bool)
        or not isinstance(raw["interlocks"], list)
    ):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Control policy is invalid")
    numeric = definition["dataType"] in {"FLOAT", "INT"}
    minimum = raw.get("minimum")
    maximum = raw.get("maximum")
    if numeric:
        if (
            not isinstance(minimum, (int, float))
            or isinstance(minimum, bool)
            or not isinstance(maximum, (int, float))
            or isinstance(maximum, bool)
            or float(minimum) > float(maximum)
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "Control numeric limits are invalid")
    elif minimum is not None or maximum is not None:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Control limits require a numeric entity")
    readback = raw["readback"]
    allowed_readback = {"definition", "timeout", "tolerance"} if numeric else {"definition", "timeout"}
    if (
        not isinstance(readback, dict)
        or set(readback) != allowed_readback
        or not isinstance(readback.get("definition"), str)
        or not readback["definition"]
        or readback["definition"] not in definitions
    ):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Control readback is invalid")
    readback_definition = definitions[readback["definition"]]
    if (
        readback_definition["deviceCategory"] != definition["deviceCategory"]
        or readback_definition["dataType"] != definition["dataType"]
        or (readback_definition.get("unit") or None) != (definition.get("unit") or None)
        or readback_definition["direction"] not in {"R", "RW"}
    ):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Control readback is incompatible")
    try:
        cooldown = _duration_seconds(raw["cooldown"])
        timeout = _duration_seconds(readback["timeout"])
    except DeliveryError as exc:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Control duration is invalid") from exc
    tolerance = readback.get("tolerance")
    if numeric and (
        not isinstance(tolerance, (int, float))
        or isinstance(tolerance, bool)
        or float(tolerance) < 0
    ):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Control tolerance is invalid")
    interlocks: list[dict[str, Any]] = []
    seen_interlocks: set[str] = set()
    for item in raw["interlocks"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"definition", "equals"}
            or not isinstance(item.get("definition"), str)
            or item["definition"] not in definitions
            or item["definition"] in seen_interlocks
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "Control interlock is invalid")
        referenced = definitions[item["definition"]]
        if (
            referenced["deviceCategory"] != definition["deviceCategory"]
            or referenced["direction"] not in {"R", "RW"}
            or not _control_value_matches_type(item["equals"], referenced["dataType"])
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "Control interlock is incompatible")
        seen_interlocks.add(item["definition"])
        interlocks.append({"definition_id": item["definition"], "equals": item["equals"]})
    return {
        "minimum": float(minimum) if numeric else None,
        "maximum": float(maximum) if numeric else None,
        "cooldown_seconds": int(cooldown),
        "readback_definition": readback["definition"],
        "tolerance": float(tolerance) if numeric else None,
        "timeout_seconds": int(timeout),
        "interlocks": interlocks,
        "high_risk": raw["highRisk"],
    }


def _control_value_matches_type(value: Any, data_type: str) -> bool:
    return {
        "FLOAT": isinstance(value, (int, float)) and not isinstance(value, bool),
        "INT": isinstance(value, int) and not isinstance(value, bool),
        "BOOL": isinstance(value, bool),
        "STRING": isinstance(value, str),
        "ENUM": isinstance(value, str),
    }.get(data_type, False)


def _validate_acceptance_definition(
    definition: dict[str, Any],
    acceptance_id: str,
) -> None:
    if definition.get("kind") == "entity_readiness":
        return
    if definition.get("kind") == "history_readiness":
        return
    if definition.get("kind") == "alarm_lifecycle":
        return
    if definition.get("kind") == "policy_execution":
        return
    if definition.get("kind") == "manual_control_execution":
        allowed_fields = {
            "schemaVersion", "id", "kind", "required", "entityDefinition",
            "expectedValue", "actorRole", "timeout",
        }
        expected_value = definition.get("expectedValue")
        if (
            set(definition) != allowed_fields
            or definition.get("schemaVersion") != "zizu.acceptance/v1alpha1"
            or definition.get("id") != acceptance_id
            or not isinstance(definition.get("required"), bool)
            or not isinstance(definition.get("entityDefinition"), str)
            or not definition["entityDefinition"].strip()
            or not isinstance(definition.get("actorRole"), str)
            or definition.get("actorRole") != "operator"
            or isinstance(expected_value, bool)
            or not isinstance(expected_value, (int, float))
            or not math.isfinite(float(expected_value))
        ):
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Manual control execution acceptance is invalid",
            )
        _validate_timeout(definition.get("timeout"))
        return
    if definition.get("kind") == "release_lock":
        allowed_fields = {"schemaVersion", "id", "kind", "required", "timeout"}
        if (
            set(definition) != allowed_fields
            or definition.get("schemaVersion") != "zizu.acceptance/v1alpha1"
            or definition.get("id") != acceptance_id
            or not isinstance(definition.get("required"), bool)
        ):
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Release lock acceptance is invalid",
            )
        _validate_timeout(definition.get("timeout"))
        return
    if definition.get("kind") == "operation_audit":
        allowed_fields = {"schemaVersion", "id", "kind", "required", "timeout"}
        if (
            set(definition) != allowed_fields
            or definition.get("schemaVersion") != "zizu.acceptance/v1alpha1"
            or definition.get("id") != acceptance_id
            or not isinstance(definition.get("required"), bool)
        ):
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Operation audit acceptance is invalid",
            )
        _validate_timeout(definition.get("timeout"))
        return
    allowed_fields = {"schemaVersion", "id", "kind", "required", "timeout"}
    if set(definition) != allowed_fields:
        raise DeliveryError(
            "ASSET_REFERENCE_INVALID",
            "Acceptance definition fields are invalid",
        )
    if (
        definition.get("schemaVersion") != "zizu.acceptance/v1alpha1"
        or definition.get("id") != acceptance_id
        or definition.get("kind") != "platform_liveness"
        or not isinstance(definition.get("required"), bool)
    ):
        raise DeliveryError(
            "ASSET_REFERENCE_INVALID",
            "Unsupported acceptance definition",
        )
    _validate_timeout(definition.get("timeout"))


def _validate_timeout(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"([1-9]\d*)s", value) is None:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "Acceptance timeout is invalid")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise DeliveryError("MANIFEST_INVALID", "Version must use major.minor.patch")
    return tuple(int(part) for part in match.groups())


def _validate_platform_range(constraint: str, current: str) -> None:
    version = _version_tuple(current)
    clauses = [item.strip() for item in constraint.split(",")]
    if not clauses or any(not item for item in clauses):
        raise DeliveryError("MANIFEST_INVALID", "Platform range is invalid")
    for clause in clauses:
        match = re.fullmatch(r"(>=|<=|>|<|==)(\d+\.\d+\.\d+)", clause)
        if match is None:
            raise DeliveryError("MANIFEST_INVALID", "Platform range is invalid")
        operator, target_text = match.groups()
        target = _version_tuple(target_text)
        accepted = {
            ">=": version >= target,
            "<=": version <= target,
            ">": version > target,
            "<": version < target,
            "==": version == target,
        }[operator]
        if not accepted:
            raise DeliveryError(
                "PLATFORM_INCOMPATIBLE",
                "Package does not support the running platform version",
            )


def _package_digest(files: dict[str, bytes]) -> str:
    canonical = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.encode("utf-8")):
        content = files[path]
        canonical.update(path.encode("utf-8"))
        canonical.update(b"\0")
        canonical.update(str(len(content)).encode("ascii"))
        canonical.update(b"\0")
        canonical.update(hashlib.sha256(content).digest())
    return canonical.hexdigest()
