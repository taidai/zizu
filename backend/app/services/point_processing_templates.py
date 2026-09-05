"""Standalone immutable JSON templates for L1 point processing."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.data_trunk_contracts import FormulaSource, ValueKind
from app.services.point_processing_formula import FormulaCompileError, compile_formula
_SCHEMA_VERSION = "zizu.point-processing/v1alpha1"
_DATA_TYPES = {"FLOAT", "INT", "BOOL", "STRING", "ENUM", "CODE_SET"}
_OUTPUT_TYPES = {
    "passthrough": None,
    "boolean_map": "BOOL",
    "numeric": "FLOAT",
    "enum": "ENUM",
    "fault_codes": "CODE_SET",
    "boolean_set": "CODE_SET",
    "formula": None,
}


@dataclass(frozen=True)
class PointProcessingInput:
    input_id: str
    source_kind: str
    source_key: str
    aliases: tuple[str, ...]
    data_type: str
    unit: str | None
    required: bool
    source_contract: Mapping[str, Any] | None = None
    cardinality: str = "one"
    selector: Mapping[str, Any] | None = None
    default_value: float | int | bool | None = None


@dataclass(frozen=True)
class PointProcessingOutput:
    output_id: str
    entity_definition_id: str
    data_type: str
    unit: str | None
    freshness_seconds: float
    transform: Mapping[str, Any]
    control: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PointProcessingTemplate:
    asset_id: str
    display_name: str
    device_category: str
    brand: str
    model: str
    revision: int
    status: str
    content_digest: str
    inputs: tuple[PointProcessingInput, ...]
    outputs: tuple[PointProcessingOutput, ...]
    content: Mapping[str, Any] | None = None


class PointProcessingTemplateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RegisteredPointProcessingTemplate:
    revision_id: UUID
    template: PointProcessingTemplate
    reuse_scope: Literal["node", "shared"] = "shared"
    owner_node_id: UUID | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "revision_id": str(self.revision_id),
            "content_digest": self.template.content_digest,
            "content": canonical_point_processing_content(self.template),
        }


class InMemoryPointProcessingTemplates:
    """Small test/reference registry with the same immutability rule as PostgreSQL."""

    def __init__(self, *, configuration_revision: int = 0) -> None:
        self.configuration_revision = configuration_revision
        self._by_revision: dict[UUID, PointProcessingTemplate] = {}

    def import_template(
        self,
        raw: Mapping[str, Any],
        *,
        actor: str,
    ) -> RegisteredPointProcessingTemplate:
        if not actor.strip():
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_ACTOR_INVALID",
                "Template import actor is required",
            )
        template = parse_point_processing_template(raw)
        revision_id = point_processing_revision_id(template)
        existing = self._by_revision.get(revision_id)
        if existing is not None and existing.content_digest != template.content_digest:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_REVISION_IMMUTABLE",
                "Immutable point-processing revision has different content",
            )
        self._by_revision.setdefault(revision_id, template)
        return RegisteredPointProcessingTemplate(revision_id, self._by_revision[revision_id])

    def export_template(self, revision_id: UUID) -> dict[str, Any]:
        template = self._by_revision.get(revision_id)
        if template is None:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_TEMPLATE_NOT_FOUND",
                "Point-processing template revision was not found",
            )
        return canonical_point_processing_content(template)


def point_processing_template_id(asset: PointProcessingTemplate) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        (
            "zizu/point-processing-template/"
            f"{asset.asset_id}/{asset.brand}/{asset.model}"
        ),
    )


def point_processing_revision_id(asset: PointProcessingTemplate) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"zizu/point-processing-revision/{point_processing_template_id(asset)}/{asset.revision}",
    )


def canonical_point_processing_content(
    template: PointProcessingTemplate,
) -> dict[str, Any]:
    if template.content is not None:
        return _plain(template.content)

    inputs: list[dict[str, Any]] = []
    for item in template.inputs:
        raw_input: dict[str, Any] = {
            "id": item.input_id,
            "sourceKind": item.source_kind,
            "sourceKey": item.source_key,
            "aliases": list(item.aliases),
            "dataType": item.data_type,
            "unit": item.unit,
            "required": item.required,
        }
        if item.source_contract is not None:
            raw_input["sourceContract"] = _plain(item.source_contract)
        if item.cardinality != "one":
            raw_input["cardinality"] = item.cardinality
        if item.selector is not None:
            raw_input["selector"] = _plain(item.selector)
        if item.default_value is not None:
            raw_input["defaultValue"] = item.default_value
        inputs.append(raw_input)

    outputs: list[dict[str, Any]] = []
    for item in template.outputs:
        transform = _plain(item.transform)
        if transform.get("kind") in {"formula", "boolean_map"}:
            transform.pop("canonicalAst", None)
            transform.pop("astDigest", None)
        output = {
            "id": item.output_id,
            "entityDefinition": item.entity_definition_id,
            "dataType": item.data_type,
            "unit": item.unit,
            "freshness": f"{int(item.freshness_seconds)}s",
            "transform": transform,
        }
        if item.control is not None:
            output["control"] = _plain(item.control)
        outputs.append(output)
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "id": template.asset_id,
        "kind": "point_processing_template",
        "displayName": template.display_name,
        "deviceCategory": template.device_category,
        "brand": template.brand,
        "model": template.model,
        "revision": template.revision,
        "status": template.status,
        "inputs": inputs,
        "outputs": outputs,
    }


def parse_point_processing_template(
    raw: Mapping[str, Any],
    *,
    entity_definitions: Mapping[str, Mapping[str, Any]] | None = None,
) -> PointProcessingTemplate:
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
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_ASSET_INVALID",
            "Point processing asset schema is invalid",
        )
    if raw.get("kind") != "point_processing_template":
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_ASSET_INVALID",
            "Point processing asset kind is invalid",
        )
    for field in ("id", "displayName", "deviceCategory", "brand", "model"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_ASSET_INVALID",
                "Point processing asset identity is invalid",
            )
    if (
        not isinstance(raw.get("revision"), int)
        or isinstance(raw["revision"], bool)
        or raw["revision"] < 1
        or raw.get("status") not in {"active", "retired"}
    ):
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_ASSET_INVALID",
            "Point processing revision or status is invalid",
        )

    inputs = _parse_inputs(raw.get("inputs"))
    outputs = _parse_outputs(
        raw.get("outputs"),
        inputs,
        entity_definitions or {},
        raw["deviceCategory"],
    )
    canonical = json.dumps(
        _plain(raw),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return PointProcessingTemplate(
        asset_id=raw["id"],
        display_name=raw["displayName"],
        device_category=raw["deviceCategory"],
        brand=raw["brand"],
        model=raw["model"],
        revision=raw["revision"],
        status=raw["status"],
        content_digest=hashlib.sha256(canonical).hexdigest(),
        inputs=inputs,
        outputs=outputs,
        content=_freeze(_plain(raw)),
    )


def _parse_inputs(raw_inputs: Any) -> tuple[PointProcessingInput, ...]:
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_INPUT_INVALID",
            "Point processing inputs must be a non-empty list",
        )
    parsed = []
    seen: set[str] = set()
    for raw in raw_inputs:
        if not isinstance(raw, Mapping):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_INPUT_INVALID",
                "Point processing input is invalid",
            )
        required_fields = {"id", "sourceKind", "sourceKey", "aliases", "dataType", "required"}
        optional_fields = {"unit", "sourceContract", "cardinality", "selector", "defaultValue"}
        if set(raw) - (required_fields | optional_fields) or not required_fields.issubset(raw):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_INPUT_INVALID",
                "Point processing input fields are invalid",
            )
        input_id = raw.get("id")
        aliases = raw.get("aliases")
        source_contract = raw.get("sourceContract")
        cardinality = raw.get("cardinality", "one")
        selector = raw.get("selector")
        default_value = raw.get("defaultValue")
        if source_contract is not None and (
            not isinstance(source_contract, Mapping)
            or set(source_contract) != {
                "group", "address", "wireDataType", "decimal", "readOnly"
            }
            or not isinstance(source_contract.get("group"), str)
            or not source_contract["group"].strip()
            or not isinstance(source_contract.get("address"), str)
            or not source_contract["address"].strip()
            or not isinstance(source_contract.get("wireDataType"), str)
            or not source_contract["wireDataType"].strip()
            or not isinstance(source_contract.get("decimal"), (int, float, type(None)))
            or isinstance(source_contract.get("decimal"), bool)
            or (
                source_contract.get("decimal") is not None
                and not math.isfinite(float(source_contract["decimal"]))
            )
            or source_contract.get("readOnly") is not True
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_INPUT_INVALID",
                "Point processing source contract is invalid",
            )
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
            or cardinality not in {"one", "many"}
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_INPUT_INVALID",
                "Point processing input contract is invalid",
            )
        if selector is not None and (
            raw["sourceKind"] != "l2"
            or not isinstance(selector, Mapping)
            or set(selector) != {"scope", "nodeType", "entityDefinition"}
            or selector.get("scope") != "descendants"
            or not isinstance(selector.get("nodeType"), str)
            or not selector["nodeType"].strip()
            or not isinstance(selector.get("entityDefinition"), str)
            or selector.get("entityDefinition") != raw["sourceKey"]
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_SELECTOR_INVALID",
                "Point processing selector contract is invalid",
            )
        if (
            (cardinality == "many" and selector is None)
            or (source_contract is not None and raw["sourceKind"] != "l0")
            or (selector is not None and source_contract is not None)
            or (default_value is not None and (raw["required"] or cardinality != "one"))
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_INPUT_INVALID",
                "Point processing input source mode is invalid",
            )
        if default_value is not None:
            if raw["dataType"] == "BOOL":
                valid_default = isinstance(default_value, bool)
            elif raw["dataType"] == "INT":
                valid_default = isinstance(default_value, int) and not isinstance(
                    default_value, bool
                )
            elif raw["dataType"] == "FLOAT":
                valid_default = (
                    isinstance(default_value, (int, float))
                    and not isinstance(default_value, bool)
                    and math.isfinite(float(default_value))
                )
            else:
                valid_default = False
            if not valid_default:
                raise PointProcessingTemplateError(
                    "POINT_PROCESSING_INPUT_INVALID",
                    "Point processing input default is invalid",
                )
        if cardinality == "many" and raw["dataType"] not in {"FLOAT", "INT", "BOOL"}:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_INPUT_INVALID",
                "Point processing collection input data type is invalid",
            )
        seen.add(input_id)
        parsed.append(
            PointProcessingInput(
                input_id=input_id,
                source_kind=raw["sourceKind"],
                source_key=raw["sourceKey"],
                aliases=tuple(aliases),
                data_type=raw["dataType"],
                unit=raw.get("unit"),
                required=raw["required"],
                source_contract=(
                    None if source_contract is None else _freeze(source_contract)
                ),
                cardinality=cardinality,
                selector=None if selector is None else _freeze(selector),
                default_value=default_value,
            )
        )
    return tuple(sorted(parsed, key=lambda item: item.input_id))


def _parse_outputs(
    raw_outputs: Any,
    inputs: tuple[PointProcessingInput, ...],
    entity_definitions: Mapping[str, Mapping[str, Any]],
    device_category: str,
) -> tuple[PointProcessingOutput, ...]:
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_OUTPUT_INVALID",
            "Point processing outputs must be a non-empty list",
        )
    input_by_id = {item.input_id: item for item in inputs}
    parsed = []
    seen: set[str] = set()
    for raw in raw_outputs:
        required_fields = {
            "id", "entityDefinition", "dataType", "unit", "freshness", "transform"
        }
        if (
            not isinstance(raw, Mapping)
            or not required_fields.issubset(raw)
            or set(raw) - (required_fields | {"control"})
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_OUTPUT_INVALID",
                "Point processing output fields are invalid",
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
            or (isinstance(unit, str) and (not unit.strip() or unit != unit.strip()))
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_OUTPUT_INVALID",
                "Point processing output contract is invalid",
            )
        freshness = _duration_seconds(raw.get("freshness"))
        transform = _parse_transform(
            raw.get("transform"),
            input_by_id,
            output_data_type=data_type,
            output_unit=unit,
        )
        control = _parse_control(
            raw.get("control"),
            transform=transform,
            inputs=input_by_id,
            output_data_type=data_type,
        )
        expected_type = _OUTPUT_TYPES[transform["kind"]]
        if expected_type is not None and expected_type != data_type:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_OUTPUT_INVALID",
                "Transform kind and output data type do not match",
            )
        definition = entity_definitions.get(entity_id)
        if definition is not None and (
            definition.get("dataType") != data_type
            or (definition.get("unit") or None) != (unit or None)
            or str(definition.get("deviceCategory", "")).casefold()
            != device_category.casefold()
            or definition.get("direction") not in {"R", "RW"}
            or (control is not None and definition.get("direction") != "RW")
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_OUTPUT_INCOMPATIBLE",
                "Point processing output does not match its entity definition",
            )
        if entity_definitions and definition is None:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_OUTPUT_INCOMPATIBLE",
                "Point processing output entity definition is missing",
            )
        seen.add(output_id)
        parsed.append(
            PointProcessingOutput(
                output_id=output_id,
                entity_definition_id=entity_id,
                data_type=data_type,
                unit=unit,
                freshness_seconds=freshness,
                transform=_freeze(transform),
                control=None if control is None else _freeze(control),
            )
        )
    return tuple(sorted(parsed, key=lambda item: item.entity_definition_id))


def _parse_control(
    raw: Any,
    *,
    transform: Mapping[str, Any],
    inputs: Mapping[str, PointProcessingInput],
    output_data_type: str,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    fields = {
        "minimum", "maximum", "tolerance", "cooldownSeconds",
        "timeoutSeconds", "highRisk",
    }
    source = inputs.get(str(transform.get("input", "")))
    numeric_fields = ("minimum", "maximum", "tolerance")
    valid_numbers = all(
        isinstance(raw.get(field), (int, float))
        and not isinstance(raw.get(field), bool)
        and math.isfinite(float(raw[field]))
        for field in numeric_fields
    ) if isinstance(raw, Mapping) else False
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or transform.get("kind") != "passthrough"
        or source is None
        or source.source_kind != "l0"
        or output_data_type not in {"FLOAT", "INT"}
        or not valid_numbers
        or float(raw["minimum"]) > float(raw["maximum"])
        or float(raw["tolerance"]) < 0
        or not isinstance(raw.get("cooldownSeconds"), int)
        or isinstance(raw.get("cooldownSeconds"), bool)
        or not 1 <= raw["cooldownSeconds"] <= 3600
        or not isinstance(raw.get("timeoutSeconds"), int)
        or isinstance(raw.get("timeoutSeconds"), bool)
        or not 1 <= raw["timeoutSeconds"] <= 300
        or not isinstance(raw.get("highRisk"), bool)
    ):
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_CONTROL_INVALID",
            "Controllable output requires one bounded numeric L0 passthrough",
        )
    return {field: raw[field] for field in sorted(fields)}


def _parse_transform(
    raw: Any,
    inputs: Mapping[str, PointProcessingInput],
    *,
    output_data_type: str,
    output_unit: str | None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("kind") not in _OUTPUT_TYPES:
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_RULE_INVALID",
            "Point processing transform kind is invalid",
        )
    kind = raw["kind"]
    if kind == "boolean_map":
        if set(raw) != {"kind", "input", "trueWhen"}:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Boolean-map transform fields are invalid",
            )
        input_id = raw.get("input")
        true_when = raw.get("trueWhen")
        source = inputs.get(input_id) if isinstance(input_id, str) else None
        if (
            source is None
            or source.source_kind != "l0"
            or not source.required
            or source.cardinality != "one"
            or source.default_value is not None
            or source.data_type != "INT"
            or source.unit is not None
            or output_data_type != "BOOL"
            or output_unit is not None
            or not isinstance(true_when, int)
            or isinstance(true_when, bool)
            or true_when not in {0, 1}
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Boolean-map requires one unitless INT input and BOOL output",
            )
        try:
            compiled = compile_formula(
                f"input == {true_when}",
                sources=(
                    FormulaSource(
                        "input",
                        ValueKind.INT,
                        None,
                        "one",
                        True,
                        None,
                    ),
                ),
                result_type=ValueKind.BOOL,
                result_unit=None,
            )
        except (FormulaCompileError, ValueError) as exc:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Boolean-map rule could not be compiled",
            ) from exc
        return {
            "kind": "boolean_map",
            "input": input_id,
            "trueWhen": true_when,
            "canonicalAst": _plain(compiled.ast),
            "astDigest": compiled.digest,
        }
    if kind == "passthrough":
        if set(raw) != {"kind", "input"}:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Passthrough transform fields are invalid",
            )
        input_id = raw.get("input")
        source = inputs.get(input_id) if isinstance(input_id, str) else None
        if (
            source is None
            or not source.required
            or source.cardinality != "one"
            or source.default_value is not None
            or source.data_type != output_data_type
            or (
                source.unit != output_unit
                and not (
                    source.source_kind == "l0"
                    and source.data_type in {"FLOAT", "INT"}
                    and source.unit is None
                    and output_unit is not None
                )
            )
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Passthrough input and output contracts must match",
            )
        return {"kind": "passthrough", "input": input_id}
    if kind == "formula":
        if set(raw) != {
            "kind", "expression", "scheduleSeconds", "controlEligible"
        } or not isinstance(raw.get("expression"), str) or not isinstance(
            raw.get("scheduleSeconds"), int
        ) or isinstance(raw.get("scheduleSeconds"), bool) or not (
            1 <= raw["scheduleSeconds"] <= 3600
        ) or not isinstance(raw.get("controlEligible"), bool):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_FORMULA_INVALID",
                "Point processing formula fields are invalid",
            )
        source_kinds = {item.source_kind for item in inputs.values()}
        if "l2" in source_kinds and source_kinds != {"l2"}:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_FORMULA_INVALID",
                "Cross-node formulas can only read standardized L2 inputs",
            )
        try:
            compiled = compile_formula(
                raw["expression"],
                sources=tuple(
                    FormulaSource(
                        item.input_id,
                        ValueKind(item.data_type),
                        item.unit,
                        item.cardinality,
                        item.required,
                        item.default_value,
                    )
                    for item in inputs.values()
                ),
                result_type=ValueKind(output_data_type),
                result_unit=output_unit,
            )
        except (FormulaCompileError, ValueError) as exc:
            code = getattr(exc, "code", "POINT_PROCESSING_FORMULA_INVALID")
            raise PointProcessingTemplateError(code, str(exc)) from exc
        return {
            "kind": "formula",
            "expression": compiled.text,
            "canonicalAst": _plain(compiled.ast),
            "astDigest": compiled.digest,
            "scheduleSeconds": raw["scheduleSeconds"],
            "controlEligible": raw["controlEligible"],
        }
    if kind == "boolean_set":
        if set(raw) != {"kind", "entries"}:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Boolean-set transform fields are invalid",
            )
        entries = raw.get("entries")
        if not isinstance(entries, list) or not entries:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Boolean-set transform entries are invalid",
            )
        normalized_entries = []
        seen_inputs: set[str] = set()
        seen_codes: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {
                "input", "code", "name", "category"
            }:
                raise PointProcessingTemplateError(
                    "POINT_PROCESSING_RULE_INVALID",
                    "Boolean-set transform entry fields are invalid",
                )
            entry_input = entry.get("input")
            code = entry.get("code")
            if (
                not isinstance(entry_input, str)
                or entry_input not in inputs
                or inputs[entry_input].source_kind != "l0"
                or inputs[entry_input].data_type != "INT"
                or inputs[entry_input].unit is not None
                or not inputs[entry_input].required
                or inputs[entry_input].cardinality != "one"
                or inputs[entry_input].default_value is not None
                or entry_input in seen_inputs
                or not isinstance(code, str)
                or not code.strip()
                or code in seen_codes
                or not isinstance(entry.get("name"), str)
                or not entry["name"].strip()
                or not isinstance(entry.get("category"), str)
                or not entry["category"].strip()
            ):
                raise PointProcessingTemplateError(
                    "POINT_PROCESSING_RULE_INVALID",
                    "Boolean-set requires one unitless INT 0/1 input per fault code",
                )
            seen_inputs.add(entry_input)
            seen_codes.add(code)
            normalized_entries.append(dict(entry))
        return {
            "kind": kind,
            "entries": sorted(normalized_entries, key=lambda item: item["code"]),
        }

    input_id = raw.get("input")
    if not isinstance(input_id, str) or input_id not in inputs:
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_RULE_INVALID",
            "Point processing transform input is invalid",
        )
    if kind == "numeric":
        if set(raw) != {"kind", "input", "scale", "offset", "minimum", "maximum"}:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
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
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Numeric transform values are invalid",
            )
        return {**raw, **{field: float(raw[field]) for field in ("scale", "offset", "minimum", "maximum")}}
    if kind == "enum":
        if set(raw) != {"kind", "input", "entries"}:
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
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
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Enum transform entries are invalid",
            )
        return {"kind": kind, "input": input_id, "entries": dict(sorted(entries.items()))}

    if set(raw) != {"kind", "input", "delimiter", "entries"} or raw.get("delimiter") not in {
        "semicolon", "comma", "pipe", "whitespace"
    }:
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_RULE_INVALID",
            "Fault-code transform fields are invalid",
        )
    entries = raw.get("entries")
    if not isinstance(entries, Mapping) or not entries:
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_RULE_INVALID",
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
            or set(value) != {"code", "name"}
            or not isinstance(value.get("code"), str)
            or not value["code"].strip()
            or not isinstance(value.get("name"), str)
            or not value["name"].strip()
        ):
            raise PointProcessingTemplateError(
                "POINT_PROCESSING_RULE_INVALID",
                "Fault-code transform entry is invalid",
            )
        normalized_entries[normalized_raw_code] = {
            "code": value["code"],
            "name": value["name"],
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
        raise PointProcessingTemplateError(
            "POINT_PROCESSING_OUTPUT_INVALID",
            "Point processing freshness is invalid",
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


def _template_record(asset: PointProcessingTemplate) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "display_name": asset.display_name,
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
                "source_contract": _plain(item.source_contract),
                "cardinality": item.cardinality,
                "selector": _plain(item.selector),
                "default_value": item.default_value,
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
                "control": _plain(item.control),
            }
            for item in asset.outputs
        ],
    }


def _template_from_record(raw: Mapping[str, Any]) -> PointProcessingTemplate:
    return PointProcessingTemplate(
        asset_id=raw["asset_id"],
        display_name=raw["display_name"],
        device_category=raw["device_category"],
        brand=raw["brand"],
        model=raw["model"],
        revision=raw["revision"],
        status=raw["status"],
        content_digest=raw["content_digest"],
        inputs=tuple(
            PointProcessingInput(
                input_id=item["input_id"],
                source_kind=item["source_kind"],
                source_key=item["source_key"],
                aliases=tuple(item["aliases"]),
                data_type=item["data_type"],
                unit=item["unit"],
                required=item["required"],
                source_contract=_freeze(item.get("source_contract")),
                cardinality=item.get("cardinality", "one"),
                selector=_freeze(item.get("selector")),
                default_value=item.get("default_value"),
            )
            for item in raw["inputs"]
        ),
        outputs=tuple(
            PointProcessingOutput(
                output_id=item["output_id"],
                entity_definition_id=item["entity_definition_id"],
                data_type=item["data_type"],
                unit=item["unit"],
                freshness_seconds=item["freshness_seconds"],
                transform=_freeze(item["transform"]),
                control=_freeze(item.get("control")),
            )
            for item in raw["outputs"]
        ),
    )
