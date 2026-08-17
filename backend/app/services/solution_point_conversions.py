"""Versioned solution-package assets for L1 point conversions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

from app.services.solution_delivery_contracts import DeliveryError

if TYPE_CHECKING:
    from app.services.solution_delivery_contracts import PackageImport


_SCHEMA_VERSION = "zizu.point-conversion/v1alpha1"
_DATA_TYPES = {"FLOAT", "INT", "BOOL", "STRING", "ENUM", "CODE_SET"}
_OUTPUT_TYPES = {
    "numeric": "FLOAT",
    "enum": "ENUM",
    "fault_codes": "CODE_SET",
}
_SEVERITIES = {"INFO", "WARNING", "MAJOR", "CRITICAL"}


@dataclass(frozen=True)
class PointConversionInput:
    input_id: str
    source_kind: str
    source_key: str
    aliases: tuple[str, ...]
    data_type: str
    unit: str | None
    required: bool


@dataclass(frozen=True)
class PointConversionOutput:
    output_id: str
    entity_definition_id: str
    data_type: str
    unit: str | None
    freshness_seconds: float
    transform: Mapping[str, Any]


@dataclass(frozen=True)
class PointConversionAsset:
    asset_id: str
    device_category: str
    brand: str
    model: str
    revision: int
    status: str
    content_digest: str
    inputs: tuple[PointConversionInput, ...]
    outputs: tuple[PointConversionOutput, ...]


class PointConversionAssetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_point_conversion_asset(
    raw: Mapping[str, Any],
    *,
    entity_definitions: Mapping[str, Mapping[str, Any]] | None = None,
) -> PointConversionAsset:
    fields = {
        "schemaVersion",
        "id",
        "kind",
        "displayName",
        "deviceCategory",
        "brand",
        "model",
        "revision",
        "status",
        "inputs",
        "outputs",
    }
    if set(raw) != fields or raw.get("schemaVersion") != _SCHEMA_VERSION:
        raise PointConversionAssetError(
            "POINT_CONVERSION_ASSET_INVALID",
            "Point conversion asset schema is invalid",
        )
    if raw.get("kind") != "point_conversion_template":
        raise PointConversionAssetError(
            "POINT_CONVERSION_ASSET_INVALID",
            "Point conversion asset kind is invalid",
        )
    for field in ("id", "displayName", "deviceCategory", "brand", "model"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise PointConversionAssetError(
                "POINT_CONVERSION_ASSET_INVALID",
                "Point conversion asset identity is invalid",
            )
    if raw["deviceCategory"] != "PCS":
        raise PointConversionAssetError(
            "POINT_CONVERSION_DEVICE_CATEGORY_UNSUPPORTED",
            "Only PCS point conversion assets are supported",
        )
    if (
        not isinstance(raw.get("revision"), int)
        or isinstance(raw["revision"], bool)
        or raw["revision"] < 1
        or raw.get("status") not in {"active", "retired"}
    ):
        raise PointConversionAssetError(
            "POINT_CONVERSION_ASSET_INVALID",
            "Point conversion revision or status is invalid",
        )

    inputs = _parse_inputs(raw.get("inputs"))
    outputs = _parse_outputs(raw.get("outputs"), inputs, entity_definitions or {})
    canonical = json.dumps(
        _plain(raw),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return PointConversionAsset(
        asset_id=raw["id"],
        device_category=raw["deviceCategory"],
        brand=raw["brand"],
        model=raw["model"],
        revision=raw["revision"],
        status=raw["status"],
        content_digest=hashlib.sha256(canonical).hexdigest(),
        inputs=inputs,
        outputs=outputs,
    )


def validate_point_conversion_assets(
    manifest: Mapping[str, Any],
    assets: Mapping[str, bytes],
    load_mapping,
) -> tuple[dict[str, Any], ...]:
    declarations = tuple(manifest.get("assets", ()))
    definitions: dict[str, Mapping[str, Any]] = {}
    for declaration in declarations:
        if declaration.get("kind") != "entity_definition":
            continue
        definition = load_mapping(
            assets.get(declaration["path"]),
            "ASSET_REFERENCE_INVALID",
        )
        definitions[declaration["id"]] = definition

    normalized = []
    for declaration in declarations:
        if declaration.get("kind") != "point_conversion_template":
            continue
        raw = load_mapping(
            assets.get(declaration["path"]),
            "ASSET_REFERENCE_INVALID",
        )
        if raw.get("id") != declaration["id"]:
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Point conversion asset identity does not match its declaration",
            )
        try:
            parsed = parse_point_conversion_asset(
                raw,
                entity_definitions=definitions,
            )
        except PointConversionAssetError as exc:
            raise DeliveryError(exc.code, f"{exc.code}: {exc}") from exc
        normalized.append(_asset_dict(parsed))
    return tuple(sorted(normalized, key=lambda item: (item["asset_id"], item["revision"])))


def point_conversion_assets(package: "PackageImport") -> tuple[PointConversionAsset, ...]:
    return tuple(
        _asset_from_dict(raw)
        for raw in package.manifest.get("_point_conversion_assets", ())
    )


def _parse_inputs(raw_inputs: Any) -> tuple[PointConversionInput, ...]:
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise PointConversionAssetError(
            "POINT_CONVERSION_INPUT_INVALID",
            "Point conversion inputs must be a non-empty list",
        )
    parsed = []
    seen: set[str] = set()
    for raw in raw_inputs:
        if not isinstance(raw, Mapping):
            raise PointConversionAssetError(
                "POINT_CONVERSION_INPUT_INVALID",
                "Point conversion input is invalid",
            )
        required_fields = {"id", "sourceKind", "sourceKey", "aliases", "dataType", "required"}
        if set(raw) - (required_fields | {"unit"}) or not required_fields.issubset(raw):
            raise PointConversionAssetError(
                "POINT_CONVERSION_INPUT_INVALID",
                "Point conversion input fields are invalid",
            )
        input_id = raw.get("id")
        aliases = raw.get("aliases")
        if (
            not isinstance(input_id, str)
            or not input_id.strip()
            or input_id in seen
            or raw.get("sourceKind") not in {"l0", "l2"}
            or not isinstance(raw.get("sourceKey"), str)
            or not raw["sourceKey"].strip()
            or not isinstance(aliases, list)
            or any(not isinstance(item, str) or not item.strip() for item in aliases)
            or len(set(aliases)) != len(aliases)
            or raw.get("dataType") not in _DATA_TYPES
            or not isinstance(raw.get("required"), bool)
            or not isinstance(raw.get("unit"), (str, type(None)))
        ):
            raise PointConversionAssetError(
                "POINT_CONVERSION_INPUT_INVALID",
                "Point conversion input contract is invalid",
            )
        seen.add(input_id)
        parsed.append(
            PointConversionInput(
                input_id=input_id,
                source_kind=raw["sourceKind"],
                source_key=raw["sourceKey"],
                aliases=tuple(aliases),
                data_type=raw["dataType"],
                unit=raw.get("unit"),
                required=raw["required"],
            )
        )
    return tuple(sorted(parsed, key=lambda item: item.input_id))


def _parse_outputs(
    raw_outputs: Any,
    inputs: tuple[PointConversionInput, ...],
    entity_definitions: Mapping[str, Mapping[str, Any]],
) -> tuple[PointConversionOutput, ...]:
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise PointConversionAssetError(
            "POINT_CONVERSION_OUTPUT_INVALID",
            "Point conversion outputs must be a non-empty list",
        )
    input_by_id = {item.input_id: item for item in inputs}
    parsed = []
    seen: set[str] = set()
    for raw in raw_outputs:
        if not isinstance(raw, Mapping) or set(raw) != {
            "id", "entityDefinition", "dataType", "unit", "freshness", "transform"
        }:
            raise PointConversionAssetError(
                "POINT_CONVERSION_OUTPUT_INVALID",
                "Point conversion output fields are invalid",
            )
        output_id = raw.get("id")
        entity_id = raw.get("entityDefinition")
        data_type = raw.get("dataType")
        unit = raw.get("unit")
        if (
            not isinstance(output_id, str)
            or not output_id.strip()
            or output_id in seen
            or not isinstance(entity_id, str)
            or not entity_id.strip()
            or data_type not in _DATA_TYPES
            or not isinstance(unit, (str, type(None)))
        ):
            raise PointConversionAssetError(
                "POINT_CONVERSION_OUTPUT_INVALID",
                "Point conversion output contract is invalid",
            )
        freshness = _duration_seconds(raw.get("freshness"))
        transform = _parse_transform(raw.get("transform"), input_by_id)
        if _OUTPUT_TYPES[transform["kind"]] != data_type:
            raise PointConversionAssetError(
                "POINT_CONVERSION_OUTPUT_INVALID",
                "Transform kind and output data type do not match",
            )
        definition = entity_definitions.get(entity_id)
        if definition is not None and (
            definition.get("dataType") != data_type
            or (definition.get("unit") or None) != (unit or None)
            or str(definition.get("deviceCategory", "")).casefold() != "pcs"
            or definition.get("direction") not in {"R", "RW"}
        ):
            raise PointConversionAssetError(
                "POINT_CONVERSION_OUTPUT_INCOMPATIBLE",
                "Point conversion output does not match its entity definition",
            )
        if entity_definitions and definition is None:
            raise PointConversionAssetError(
                "POINT_CONVERSION_OUTPUT_INCOMPATIBLE",
                "Point conversion output entity definition is missing",
            )
        seen.add(output_id)
        parsed.append(
            PointConversionOutput(
                output_id=output_id,
                entity_definition_id=entity_id,
                data_type=data_type,
                unit=unit,
                freshness_seconds=freshness,
                transform=_freeze(transform),
            )
        )
    return tuple(sorted(parsed, key=lambda item: item.entity_definition_id))


def _parse_transform(
    raw: Any,
    inputs: Mapping[str, PointConversionInput],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("kind") not in _OUTPUT_TYPES:
        raise PointConversionAssetError(
            "POINT_CONVERSION_RULE_INVALID",
            "Point conversion transform kind is invalid",
        )
    kind = raw["kind"]
    input_id = raw.get("input")
    if not isinstance(input_id, str) or input_id not in inputs:
        raise PointConversionAssetError(
            "POINT_CONVERSION_RULE_INVALID",
            "Point conversion transform input is invalid",
        )
    if kind == "numeric":
        if set(raw) != {"kind", "input", "scale", "offset", "minimum", "maximum"}:
            raise PointConversionAssetError(
                "POINT_CONVERSION_RULE_INVALID",
                "Numeric transform fields are invalid",
            )
        numbers = (raw["scale"], raw["offset"], raw["minimum"], raw["maximum"])
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            for value in numbers
        ) or inputs[input_id].data_type not in {"FLOAT", "INT"} or (
            float(raw["minimum"]) > float(raw["maximum"])
        ):
            raise PointConversionAssetError(
                "POINT_CONVERSION_RULE_INVALID",
                "Numeric transform values are invalid",
            )
        return {**raw, **{field: float(raw[field]) for field in ("scale", "offset", "minimum", "maximum")}}
    if kind == "enum":
        if set(raw) != {"kind", "input", "entries"}:
            raise PointConversionAssetError(
                "POINT_CONVERSION_RULE_INVALID",
                "Enum transform fields are invalid",
            )
        entries = raw.get("entries")
        if (
            not isinstance(entries, Mapping)
            or not entries
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in entries.items()
            )
        ):
            raise PointConversionAssetError(
                "POINT_CONVERSION_RULE_INVALID",
                "Enum transform entries are invalid",
            )
        return {"kind": kind, "input": input_id, "entries": dict(sorted(entries.items()))}

    if set(raw) != {"kind", "input", "delimiter", "entries"} or raw.get("delimiter") not in {
        "semicolon", "comma", "pipe", "whitespace"
    }:
        raise PointConversionAssetError(
            "POINT_CONVERSION_RULE_INVALID",
            "Fault-code transform fields are invalid",
        )
    entries = raw.get("entries")
    if not isinstance(entries, Mapping) or not entries:
        raise PointConversionAssetError(
            "POINT_CONVERSION_RULE_INVALID",
            "Fault-code transform entries are invalid",
        )
    normalized_entries: dict[str, dict[str, str]] = {}
    for raw_code, value in entries.items():
        normalized_raw_code = raw_code.strip().upper() if isinstance(raw_code, str) else ""
        if (
            not isinstance(raw_code, str)
            or not raw_code.strip()
            or normalized_raw_code in normalized_entries
            or not isinstance(value, Mapping)
            or set(value) != {"code", "name", "defaultSeverity"}
            or not isinstance(value.get("code"), str)
            or not value["code"].strip()
            or not isinstance(value.get("name"), str)
            or not value["name"].strip()
            or value.get("defaultSeverity") not in _SEVERITIES
        ):
            raise PointConversionAssetError(
                "POINT_CONVERSION_RULE_INVALID",
                "Fault-code transform entry is invalid",
            )
        normalized_entries[normalized_raw_code] = {
            "code": value["code"],
            "name": value["name"],
            "defaultSeverity": value["defaultSeverity"],
        }
    return {
        "kind": kind,
        "input": input_id,
        "delimiter": raw["delimiter"],
        "entries": dict(sorted(normalized_entries.items())),
    }


def _duration_seconds(value: Any) -> float:
    match = re.fullmatch(r"([1-9]\d*)s", value) if isinstance(value, str) else None
    if match is None:
        raise PointConversionAssetError(
            "POINT_CONVERSION_OUTPUT_INVALID",
            "Point conversion freshness is invalid",
        )
    return float(match.group(1))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in sorted(value.items())})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _asset_dict(asset: PointConversionAsset) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "device_category": asset.device_category,
        "brand": asset.brand,
        "model": asset.model,
        "revision": asset.revision,
        "status": asset.status,
        "content_digest": asset.content_digest,
        "inputs": [
            {
                "input_id": item.input_id,
                "source_kind": item.source_kind,
                "source_key": item.source_key,
                "aliases": list(item.aliases),
                "data_type": item.data_type,
                "unit": item.unit,
                "required": item.required,
            }
            for item in asset.inputs
        ],
        "outputs": [
            {
                "output_id": item.output_id,
                "entity_definition_id": item.entity_definition_id,
                "data_type": item.data_type,
                "unit": item.unit,
                "freshness_seconds": item.freshness_seconds,
                "transform": _plain(item.transform),
            }
            for item in asset.outputs
        ],
    }


def _asset_from_dict(raw: Mapping[str, Any]) -> PointConversionAsset:
    return PointConversionAsset(
        asset_id=raw["asset_id"],
        device_category=raw["device_category"],
        brand=raw["brand"],
        model=raw["model"],
        revision=raw["revision"],
        status=raw["status"],
        content_digest=raw["content_digest"],
        inputs=tuple(
            PointConversionInput(
                input_id=item["input_id"],
                source_kind=item["source_kind"],
                source_key=item["source_key"],
                aliases=tuple(item["aliases"]),
                data_type=item["data_type"],
                unit=item["unit"],
                required=item["required"],
            )
            for item in raw["inputs"]
        ),
        outputs=tuple(
            PointConversionOutput(
                output_id=item["output_id"],
                entity_definition_id=item["entity_definition_id"],
                data_type=item["data_type"],
                unit=item["unit"],
                freshness_seconds=item["freshness_seconds"],
                transform=_freeze(item["transform"]),
            )
            for item in raw["outputs"]
        ),
    )
