"""Declarative business-metric package assets and deterministic internal compilation."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.business_metric_contracts import (
    BusinessMetricTemplate,
    CompiledMetricRevision,
    FlowDirection,
    MetricAggregator,
    MetricLifecycle,
    MetricQualityContract,
    MetricSourceOption,
    MetricSourceResolution,
    ResolvedMetricSource,
    WindowKind,
)
from app.services.solution_delivery_contracts import DeliveryError
from app.services.solution_point_processings import (
    PointProcessingAsset,
    PointProcessingInput,
    PointProcessingOutput,
    point_processing_revision_id,
)

if TYPE_CHECKING:
    from app.services.solution_delivery_contracts import PackageImport


_SCHEMA_VERSION = "zizu.business-metric/v1alpha1"
_DATA_TYPES = {"FLOAT", "INT", "BOOL", "STRING", "ENUM", "CODE_SET"}
_DURATION = re.compile(r"(\d+)([smhd])")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 60 * 60, "d": 24 * 60 * 60}


class BusinessMetricAssetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def parse_business_metric_asset(raw: Mapping[str, Any]) -> BusinessMetricTemplate:
    """Parse only the versioned declarative template language, never expressions."""
    fields = {
        "schemaVersion",
        "id",
        "revision",
        "displayName",
        "targetNodeType",
        "output",
        "window",
        "sources",
        "quality",
        "allowedLateness",
        "correction",
        "capabilities",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) not in (fields, fields | {"flow"})
        or raw.get("schemaVersion") != _SCHEMA_VERSION
    ):
        raise _invalid("Business metric template schema is invalid")
    for field in ("id", "displayName", "targetNodeType"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise _invalid("Business metric template identity is invalid")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise _invalid("Business metric template revision is invalid")

    output = _parse_output(raw.get("output"))
    window_kind, rolling_seconds = _parse_window(raw.get("window"))
    sources = _parse_sources(raw.get("sources"))
    quality = _parse_quality(raw.get("quality"))
    allowed_lateness_seconds = _parse_allowed_lateness(raw.get("allowedLateness"))
    correction_seconds = _parse_correction(raw.get("correction"), window_kind)
    flow_direction, normalize_flow_direction = _parse_flow(raw.get("flow"))
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, Mapping) or set(capabilities) != {"controlEligible"} or capabilities.get("controlEligible") is not False:
        raise _invalid("Business metric control capability must be false")

    content_digest = _template_content_digest(
        template_id=raw["id"],
        revision=revision,
        display_name=raw["displayName"],
        target_node_type=raw["targetNodeType"],
        output=output,
        window_kind=window_kind,
        rolling_window_seconds=rolling_seconds,
        sources=sources,
        quality=quality,
        allowed_lateness_seconds=allowed_lateness_seconds,
        automatic_correction_horizon_seconds=correction_seconds,
        flow_direction=flow_direction,
        normalize_flow_direction=normalize_flow_direction,
    )
    return BusinessMetricTemplate(
        template_id=raw["id"],
        revision=revision,
        display_name=raw["displayName"],
        target_node_type=raw["targetNodeType"],
        output_entity_definition_id=output["entityDefinition"],
        output_data_type=output["dataType"],
        output_unit=output["unit"],
        temporal_semantics=output["temporalSemantics"],
        window_kind=window_kind,
        rolling_window_seconds=rolling_seconds,
        sources=sources,
        quality=quality,
        allowed_lateness_seconds=allowed_lateness_seconds,
        automatic_correction_horizon_seconds=correction_seconds,
        control_eligible=False,
        flow_direction=flow_direction,
        normalize_flow_direction=normalize_flow_direction,
        content_digest=content_digest,
    )


def compile_business_metric(
    template: BusinessMetricTemplate,
    resolution: MetricSourceResolution,
) -> CompiledMetricRevision:
    """Compile an already-selected source set into a private point-processing revision."""
    _validate_resolution(template, resolution)
    sources = tuple(sorted(resolution.sources, key=lambda item: str(item.entity_instance_id)))
    source_digest = _digest(
        {
            "timezone": resolution.timezone,
            "sources": [
                {
                    "entityInstanceId": str(item.entity_instance_id),
                    "entityDefinition": item.entity_definition_id,
                    "method": item.method.value,
                    "dataType": item.data_type,
                    "unit": item.unit,
                    "estimated": item.estimated,
                }
                for item in sources
            ],
        }
    )
    output_summary = {
        "entityDefinition": template.output_entity_definition_id,
        "dataType": template.output_data_type,
        "unit": template.output_unit,
        "temporalSemantics": template.temporal_semantics,
    }
    compile_digest = _digest(
        {
            "templateDigest": template.content_digest,
            "sourceDigest": source_digest,
            "output": output_summary,
        }
    )
    asset_id = f"internal.business-metric.{uuid5(NAMESPACE_URL, f'zizu/business-metric/{compile_digest}')}"
    inputs = tuple(
        PointProcessingInput(
            input_id=f"source_{index + 1}",
            source_kind="l2",
            source_key=source.entity_definition_id,
            aliases=(),
            data_type=source.data_type,
            unit=source.unit,
            required=True,
        )
        for index, source in enumerate(sources)
    )
    transform = MappingProxyType(
        {
            "kind": "business_metric",
            "templateDigest": template.content_digest,
            "sourceDigest": source_digest,
            "window": template.window_kind.value,
            "rollingWindowSeconds": template.rolling_window_seconds,
            "qualityGoodCoverage": template.quality.good_coverage,
            "qualityMinimumUsableCoverage": template.quality.minimum_usable_coverage,
            "allowedLatenessSeconds": template.allowed_lateness_seconds,
            "automaticCorrectionHorizonSeconds": template.automatic_correction_horizon_seconds,
            "methods": tuple(source.method.value for source in sources),
            "flowDirection": template.flow_direction.value,
            "normalizeFlowDirection": template.normalize_flow_direction,
            "controlEligible": False,
        }
    )
    asset = PointProcessingAsset(
        asset_id=asset_id,
        display_name=template.display_name,
        device_category=template.target_node_type,
        brand="ZiZu",
        model="BUSINESS_METRIC",
        revision=template.revision,
        status="active",
        content_digest=compile_digest,
        inputs=inputs,
        outputs=(
            PointProcessingOutput(
                output_id="metric_value",
                entity_definition_id=template.output_entity_definition_id,
                data_type=template.output_data_type,
                unit=template.output_unit,
                freshness_seconds=1.0,
                transform=transform,
            ),
        ),
    )
    return CompiledMetricRevision(
        processing_revision_id=point_processing_revision_id(asset),
        point_processing_asset=asset,
        temporal_semantics=template.temporal_semantics,
        control_eligible=False,
        template_digest=template.content_digest,
        source_digest=source_digest,
        content_digest=compile_digest,
        timezone=resolution.timezone,
        sources=sources,
    )


def validate_business_metric_assets(
    manifest: Mapping[str, Any],
    assets: Mapping[str, bytes],
    load_mapping,
) -> tuple[dict[str, Any], ...]:
    normalized = []
    for declaration in manifest.get("assets", ()):
        if declaration.get("kind") != "business_metric_template":
            continue
        raw = load_mapping(assets.get(declaration["path"]), "ASSET_REFERENCE_INVALID")
        if raw.get("id") != declaration["id"]:
            raise DeliveryError(
                "ASSET_REFERENCE_INVALID",
                "Business metric asset identity does not match its declaration",
            )
        try:
            normalized.append(_template_dict(parse_business_metric_asset(raw)))
        except BusinessMetricAssetError as exc:
            raise DeliveryError(exc.code, str(exc)) from exc
    return tuple(sorted(normalized, key=lambda item: (item["template_id"], item["revision"])))


def business_metric_assets(package: "PackageImport") -> tuple[BusinessMetricTemplate, ...]:
    return tuple(_template_from_dict(raw) for raw in package.manifest.get("_business_metric_assets", ()))


def _parse_output(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"entityDefinition", "dataType", "unit", "temporalSemantics"}:
        raise _invalid("Business metric output fields are invalid")
    if (
        not isinstance(raw.get("entityDefinition"), str)
        or not raw["entityDefinition"].strip()
        or raw.get("dataType") != "FLOAT"
        or not isinstance(raw.get("unit"), (str, type(None)))
        or raw.get("temporalSemantics") != "windowed"
    ):
        raise _invalid("Business metric output contract is invalid")
    return dict(raw)


def _parse_window(raw: Any) -> tuple[WindowKind, int | None]:
    if not isinstance(raw, Mapping):
        raise _invalid("Business metric window is invalid")
    try:
        kind = WindowKind(raw.get("kind"))
    except ValueError as exc:
        raise _invalid("Business metric window kind is unsupported") from exc
    if kind is WindowKind.ALIGNED_DAILY and set(raw) == {"kind"}:
        return kind, None
    if kind is WindowKind.ROLLING and set(raw) == {"kind", "duration"}:
        return kind, _duration_seconds(raw["duration"])
    raise _invalid("Business metric window fields are invalid")


def _parse_sources(raw: Any) -> tuple[MetricSourceOption, ...]:
    if not isinstance(raw, list) or not raw:
        raise _invalid("Business metric sources must be a non-empty list")
    parsed = []
    priorities: set[int] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"method", "entityDefinition", "priority"}:
            raise _invalid("Business metric source fields are invalid")
        try:
            method = MetricAggregator(item.get("method"))
        except ValueError as exc:
            raise _invalid("Business metric source method is unsupported") from exc
        priority = item.get("priority")
        if (
            not isinstance(item.get("entityDefinition"), str)
            or not item["entityDefinition"].strip()
            or not isinstance(priority, int)
            or isinstance(priority, bool)
            or priority < 1
            or priority in priorities
        ):
            raise _invalid("Business metric source contract is invalid")
        priorities.add(priority)
        parsed.append(MetricSourceOption(method, item["entityDefinition"], priority))
    return tuple(sorted(parsed, key=lambda item: item.priority))


def _parse_quality(raw: Any) -> MetricQualityContract:
    if not isinstance(raw, Mapping) or set(raw) != {"goodCoverage", "minimumUsableCoverage"}:
        raise _invalid("Business metric quality contract is invalid")
    good = raw.get("goodCoverage")
    minimum_usable = raw.get("minimumUsableCoverage")
    if (
        not isinstance(good, (int, float))
        or isinstance(good, bool)
        or not math.isfinite(float(good))
        or not isinstance(minimum_usable, (int, float))
        or isinstance(minimum_usable, bool)
        or not math.isfinite(float(minimum_usable))
        or not 0 <= float(minimum_usable) <= float(good) <= 1
    ):
        raise _invalid("Business metric quality contract is invalid")
    return MetricQualityContract(float(good), float(minimum_usable))


def _parse_allowed_lateness(raw: Any) -> int:
    return _duration_seconds(raw, allow_zero=True)


def _parse_correction(raw: Any, window_kind: WindowKind) -> int:
    if not isinstance(raw, Mapping) or set(raw) != {"automaticHorizon"}:
        raise _invalid("Business metric correction contract is invalid")
    seconds = _duration_seconds(raw.get("automaticHorizon"))
    expected = {
        WindowKind.ALIGNED_DAILY: 7 * 24 * 60 * 60,
        WindowKind.ROLLING: 6 * 60 * 60,
    }[window_kind]
    if seconds != expected:
        raise _invalid("Business metric correction horizon does not match window kind")
    return seconds


def _parse_flow(raw: Any) -> tuple[FlowDirection, bool]:
    if raw is None:
        return FlowDirection.BOTH, False
    if not isinstance(raw, Mapping) or set(raw) != {"direction", "normalize"}:
        raise _invalid("Business metric flow contract is invalid")
    try:
        direction = FlowDirection(raw.get("direction"))
    except ValueError as exc:
        raise _invalid("Business metric flow direction is invalid") from exc
    if not isinstance(raw.get("normalize"), bool):
        raise _invalid("Business metric flow normalization is invalid")
    return direction, raw["normalize"]


def _validate_resolution(template: BusinessMetricTemplate, resolution: MetricSourceResolution) -> None:
    if (
        not isinstance(resolution, MetricSourceResolution)
        or not isinstance(resolution.timezone, str)
        or not resolution.timezone
        or not isinstance(resolution.sources, tuple)
        or not resolution.sources
    ):
        raise _invalid("Business metric source resolution is invalid")
    try:
        ZoneInfo(resolution.timezone)
    except ZoneInfoNotFoundError as exc:
        raise _invalid("Business metric source resolution timezone is invalid") from exc
    allowed = {(source.entity_definition_id, source.method) for source in template.sources}
    identifiers: set[UUID] = set()
    for source in resolution.sources:
        if not isinstance(source, ResolvedMetricSource):
            raise _invalid("Business metric resolved source is invalid")
        try:
            method = MetricAggregator(source.method)
        except ValueError as exc:
            raise _invalid("Business metric resolved source method is invalid") from exc
        if (
            not isinstance(source.entity_instance_id, UUID)
            or source.entity_instance_id in identifiers
            or (source.entity_definition_id, method) not in allowed
            or source.data_type not in _DATA_TYPES
            or not isinstance(source.unit, (str, type(None)))
            or not isinstance(source.estimated, bool)
        ):
            raise _invalid("Business metric resolved source contract is invalid")
        identifiers.add(source.entity_instance_id)


def _template_dict(template: BusinessMetricTemplate) -> dict[str, Any]:
    return {
        "template_id": template.template_id,
        "revision": template.revision,
        "display_name": template.display_name,
        "target_node_type": template.target_node_type,
        "output_entity_definition_id": template.output_entity_definition_id,
        "output_data_type": template.output_data_type,
        "output_unit": template.output_unit,
        "temporal_semantics": template.temporal_semantics,
        "window_kind": template.window_kind.value,
        "rolling_window_seconds": template.rolling_window_seconds,
        "sources": [
            {"method": item.method.value, "entity_definition_id": item.entity_definition_id, "priority": item.priority}
            for item in template.sources
        ],
        "good_coverage": template.quality.good_coverage,
        "minimum_usable_coverage": template.quality.minimum_usable_coverage,
        "allowed_lateness_seconds": template.allowed_lateness_seconds,
        "automatic_correction_horizon_seconds": template.automatic_correction_horizon_seconds,
        "control_eligible": template.control_eligible,
        "flow_direction": template.flow_direction.value,
        "normalize_flow_direction": template.normalize_flow_direction,
        "content_digest": template.content_digest,
    }


def _template_from_dict(raw: Mapping[str, Any]) -> BusinessMetricTemplate:
    return BusinessMetricTemplate(
        template_id=raw["template_id"],
        revision=raw["revision"],
        display_name=raw["display_name"],
        target_node_type=raw["target_node_type"],
        output_entity_definition_id=raw["output_entity_definition_id"],
        output_data_type=raw["output_data_type"],
        output_unit=raw["output_unit"],
        temporal_semantics=raw["temporal_semantics"],
        window_kind=WindowKind(raw["window_kind"]),
        rolling_window_seconds=raw["rolling_window_seconds"],
        sources=tuple(
            MetricSourceOption(
                MetricAggregator(item["method"]),
                item["entity_definition_id"],
                item["priority"],
            )
            for item in raw["sources"]
        ),
        quality=MetricQualityContract(
            raw["good_coverage"],
            raw["minimum_usable_coverage"],
        ),
        allowed_lateness_seconds=raw["allowed_lateness_seconds"],
        automatic_correction_horizon_seconds=raw["automatic_correction_horizon_seconds"],
        control_eligible=raw["control_eligible"],
        flow_direction=FlowDirection(raw["flow_direction"]),
        normalize_flow_direction=raw["normalize_flow_direction"],
        content_digest=raw["content_digest"],
    )


def _duration_seconds(value: Any, *, allow_zero: bool = False) -> int:
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if match is None or (not allow_zero and int(match.group(1)) == 0):
        raise _invalid("Business metric duration is invalid")
    return int(match.group(1)) * _DURATION_SECONDS[match.group(2)]


def _template_content_digest(
    *,
    template_id: str,
    revision: int,
    display_name: str,
    target_node_type: str,
    output: Mapping[str, Any],
    window_kind: WindowKind,
    rolling_window_seconds: int | None,
    sources: tuple[MetricSourceOption, ...],
    quality: MetricQualityContract,
    allowed_lateness_seconds: int,
    automatic_correction_horizon_seconds: int,
    flow_direction: FlowDirection,
    normalize_flow_direction: bool,
) -> str:
    return _digest(
        {
            "schemaVersion": _SCHEMA_VERSION,
            "id": template_id,
            "revision": revision,
            "displayName": display_name,
            "targetNodeType": target_node_type,
            "output": dict(output),
            "window": {
                "kind": window_kind.value,
                "rollingWindowSeconds": rolling_window_seconds,
            },
            "sources": [
                {
                    "method": source.method.value,
                    "entityDefinition": source.entity_definition_id,
                    "priority": source.priority,
                }
                for source in sources
            ],
            "quality": {
                "goodCoverage": quality.good_coverage,
                "minimumUsableCoverage": quality.minimum_usable_coverage,
            },
            "allowedLatenessSeconds": allowed_lateness_seconds,
            "automaticCorrectionHorizonSeconds": automatic_correction_horizon_seconds,
            "flow": {
                "direction": flow_direction.value,
                "normalize": normalize_flow_direction,
            },
            "controlEligible": False,
        }
    )


def _invalid(message: str) -> BusinessMetricAssetError:
    return BusinessMetricAssetError("BUSINESS_METRIC_ASSET_INVALID", message)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = [
    "BusinessMetricAssetError",
    "BusinessMetricTemplate",
    "CompiledMetricRevision",
    "FlowDirection",
    "MetricAggregator",
    "MetricLifecycle",
    "MetricQualityContract",
    "MetricSourceOption",
    "MetricSourceResolution",
    "ResolvedMetricSource",
    "WindowKind",
    "business_metric_assets",
    "compile_business_metric",
    "parse_business_metric_asset",
    "validate_business_metric_assets",
]
