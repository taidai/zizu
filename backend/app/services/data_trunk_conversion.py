"""把固定 L1 修订纯计算为确定性的 L2 观测。"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.data_trunk_contracts import (
    BooleanSetTransform,
    DataTrunkError,
    EnumTransform,
    FaultCodeTransform,
    InputReference,
    InstalledPointProcessing,
    L2Observation,
    NumericTransform,
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)


def evaluate_processing(
    *,
    installed: tuple[InstalledPointProcessing, ...],
    current_inputs: Mapping[InputReference, RawObservation | L2Observation],
    site_configuration_version: int,
    calculated_at: datetime,
) -> tuple[L2Observation, ...]:
    """计算所有已安装输出；不执行持久化或其他副作用。"""
    outputs = tuple(
        _evaluate_output(
            item,
            current_inputs,
            site_configuration_version,
            calculated_at,
        )
        for item in installed
    )
    return tuple(sorted(outputs, key=lambda item: str(item.entity_instance_id)))


def _evaluate_output(
    installed: InstalledPointProcessing,
    current_inputs: Mapping[InputReference, RawObservation | L2Observation],
    site_configuration_version: int,
    calculated_at: datetime,
) -> L2Observation:
    if isinstance(installed.transform, BooleanSetTransform):
        return _evaluate_boolean_set_output(
            installed,
            current_inputs,
            site_configuration_version,
            calculated_at,
        )
    if isinstance(installed.transform, FaultCodeTransform):
        return _evaluate_fault_code_output(
            installed,
            current_inputs,
            site_configuration_version,
            calculated_at,
        )
    if isinstance(installed.transform, EnumTransform):
        return _evaluate_enum_output(
            installed,
            current_inputs,
            site_configuration_version,
            calculated_at,
        )
    if not isinstance(installed.transform, NumericTransform):
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "unsupported point processing transform",
        )
    if installed.output_kind is not ValueKind.FLOAT:
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "numeric processing output must be FLOAT",
        )

    source = current_inputs.get(installed.transform.input)
    if source is None:
        return _runtime_failure(
            installed,
            site_configuration_version,
            calculated_at,
            "REQUIRED_INPUT_MISSING",
        )
    if not isinstance(source, RawObservation):
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "numeric L2 inputs are not supported by this revision",
        )

    if source.raw_unit != installed.transform.input_unit:
        return _runtime_failure_from_source(
            installed,
            source,
            site_configuration_version,
            calculated_at,
            "UNIT_MISMATCH",
        )
    if source.value.kind not in {ValueKind.FLOAT, ValueKind.INT} or not isinstance(
        source.value.value, (int, float)
    ) or isinstance(source.value.value, bool):
        return _runtime_failure_from_source(
            installed,
            source,
            site_configuration_version,
            calculated_at,
            "TYPE_MISMATCH",
        )

    raw_value = float(source.value.value)
    if not all(
        math.isfinite(value)
        for value in (installed.transform.scale, installed.transform.offset)
    ):
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "numeric scale and offset must be finite",
        )
    value = (raw_value * installed.transform.scale) + installed.transform.offset
    if not math.isfinite(value):
        return _runtime_failure_from_source(
            installed,
            source,
            site_configuration_version,
            calculated_at,
            "INVALID_NUMBER",
        )
    if (
        installed.transform.minimum is not None
        and value < installed.transform.minimum
    ) or (
        installed.transform.maximum is not None
        and value > installed.transform.maximum
    ):
        return _runtime_failure_from_source(
            installed,
            source,
            site_configuration_version,
            calculated_at,
            "OUT_OF_RANGE",
        )

    quality = source.quality
    typed_value = TypedValue.float(
        value if quality not in {TrunkQuality.BAD, TrunkQuality.STALE} else None
    )
    return _observation(
        installed=installed,
        value=typed_value,
        quality=quality,
        reason=_input_quality_reason(quality),
        observed_at=source.source_timestamp,
        received_at=source.received_at,
        calculated_at=calculated_at,
        site_configuration_version=site_configuration_version,
        source_observation_ids=(source.observation_id,),
        source_digest=source.source_digest,
        source_order_key=_raw_order_key(source),
    )


def _evaluate_boolean_set_output(
    installed: InstalledPointProcessing,
    current_inputs: Mapping[InputReference, RawObservation | L2Observation],
    site_configuration_version: int,
    calculated_at: datetime,
) -> L2Observation:
    if installed.output_kind is not ValueKind.CODE_SET:
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "boolean-set processing output must be CODE_SET",
        )
    transform = installed.transform
    if not isinstance(transform, BooleanSetTransform):
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "boolean-set processing requires a boolean-set transform",
        )

    sources: list[RawObservation] = []
    active_codes: list[str] = []
    for item in transform.inputs:
        source = current_inputs.get(item.input)
        if source is None:
            if item.required:
                return _boolean_set_observation(
                    installed,
                    sources,
                    site_configuration_version,
                    calculated_at,
                    value=None,
                    quality=TrunkQuality.BAD,
                    reason="REQUIRED_INPUT_MISSING",
                )
            continue
        if not isinstance(source, RawObservation):
            raise DataTrunkError(
                "POINT_PROCESSING_CONFIGURATION_INVALID",
                "boolean-set processing requires L0 inputs",
            )
        sources.append(source)
        if source.value.kind is not ValueKind.BOOL or not isinstance(
            source.value.value,
            bool,
        ):
            return _boolean_set_observation(
                installed,
                sources,
                site_configuration_version,
                calculated_at,
                value=None,
                quality=TrunkQuality.BAD,
                reason="TYPE_MISMATCH",
            )
        if source.value.value:
            active_codes.append(item.code)

    quality = min((source.quality for source in sources), default=TrunkQuality.GOOD)
    if quality in {TrunkQuality.BAD, TrunkQuality.STALE}:
        return _boolean_set_observation(
            installed,
            sources,
            site_configuration_version,
            calculated_at,
            value=None,
            quality=quality,
            reason=_input_quality_reason(quality),
        )
    return _boolean_set_observation(
        installed,
        sources,
        site_configuration_version,
        calculated_at,
        value=tuple(active_codes),
        quality=quality,
        reason=_input_quality_reason(quality),
    )


def _boolean_set_observation(
    installed: InstalledPointProcessing,
    sources: list[RawObservation],
    site_configuration_version: int,
    calculated_at: datetime,
    *,
    value: tuple[str, ...] | None,
    quality: TrunkQuality,
    reason: str | None,
) -> L2Observation:
    source_digests = sorted(source.source_digest for source in sources)
    aggregate_digest = hashlib.sha256(
        "|".join(source_digests).encode("ascii")
    ).hexdigest()
    return _observation(
        installed=installed,
        value=TypedValue.code_set(value),
        quality=quality,
        reason=reason,
        observed_at=max(
            (source.source_timestamp for source in sources),
            default=calculated_at,
        ),
        received_at=max(
            (source.received_at for source in sources),
            default=calculated_at,
        ),
        calculated_at=calculated_at,
        site_configuration_version=site_configuration_version,
        source_observation_ids=tuple(source.observation_id for source in sources),
        source_digest=aggregate_digest,
        source_order_key=f"B:{len(sources):04d}:{aggregate_digest}",
    )


def _evaluate_fault_code_output(
    installed: InstalledPointProcessing,
    current_inputs: Mapping[InputReference, RawObservation | L2Observation],
    site_configuration_version: int,
    calculated_at: datetime,
) -> L2Observation:
    if installed.output_kind is not ValueKind.CODE_SET:
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "fault-code processing output must be CODE_SET",
        )
    transform = installed.transform
    if not isinstance(transform, FaultCodeTransform):
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "fault-code processing requires a fault-code transform",
        )
    source = current_inputs.get(transform.input)
    if source is None:
        return _runtime_failure(
            installed,
            site_configuration_version,
            calculated_at,
            "REQUIRED_INPUT_MISSING",
        )
    if not isinstance(source, RawObservation):
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "fault-code processing requires an L0 input",
        )
    if source.value.kind not in {ValueKind.STRING, ValueKind.ENUM} or not isinstance(
        source.value.value,
        str,
    ):
        return _fault_code_observation(
            installed,
            source,
            site_configuration_version,
            calculated_at,
            value=None,
            quality=TrunkQuality.BAD,
            reason="TYPE_MISMATCH",
        )
    if source.quality in {TrunkQuality.BAD, TrunkQuality.STALE}:
        return _fault_code_observation(
            installed,
            source,
            site_configuration_version,
            calculated_at,
            value=None,
            quality=source.quality,
            reason=_input_quality_reason(source.quality),
        )

    separators = {
        "semicolon": ";",
        "comma": ",",
        "pipe": "|",
    }
    if transform.delimiter == "whitespace":
        pieces = source.value.value.split()
    else:
        pieces = source.value.value.split(separators[transform.delimiter])
    raw_codes = {
        piece.strip().upper()
        for piece in pieces
        if piece.strip()
    }
    unknown = any(code not in transform.entries for code in raw_codes)
    canonical_codes = tuple(
        sorted({transform.entries.get(code, code) for code in raw_codes})
    )
    return _fault_code_observation(
        installed,
        source,
        site_configuration_version,
        calculated_at,
        value=canonical_codes,
        quality=(TrunkQuality.UNCERTAIN if unknown else source.quality),
        reason="UNMAPPED_FAULT_CODE" if unknown else None,
    )


def _fault_code_observation(
    installed: InstalledPointProcessing,
    source: RawObservation,
    site_configuration_version: int,
    calculated_at: datetime,
    *,
    value: tuple[str, ...] | None,
    quality: TrunkQuality,
    reason: str | None,
) -> L2Observation:
    return _observation(
        installed=installed,
        value=TypedValue.code_set(value),
        quality=quality,
        reason=reason,
        observed_at=source.source_timestamp,
        received_at=source.received_at,
        calculated_at=calculated_at,
        site_configuration_version=site_configuration_version,
        source_observation_ids=(source.observation_id,),
        source_digest=source.source_digest,
        source_order_key=_raw_order_key(source),
    )


def _evaluate_enum_output(
    installed: InstalledPointProcessing,
    current_inputs: Mapping[InputReference, RawObservation | L2Observation],
    site_configuration_version: int,
    calculated_at: datetime,
) -> L2Observation:
    if installed.output_kind is not ValueKind.ENUM:
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "enum processing output must be ENUM",
        )
    transform = installed.transform
    if not isinstance(transform, EnumTransform):
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "enum processing requires an enum transform",
        )
    source = current_inputs.get(transform.input)
    if source is None:
        return _runtime_failure(
            installed,
            site_configuration_version,
            calculated_at,
            "REQUIRED_INPUT_MISSING",
        )
    if not isinstance(source, RawObservation):
        raise DataTrunkError(
            "POINT_PROCESSING_CONFIGURATION_INVALID",
            "enum processing requires an L0 input",
        )
    if source.value.kind not in {ValueKind.STRING, ValueKind.ENUM, ValueKind.INT} or (
        not isinstance(source.value.value, (str, int))
        or isinstance(source.value.value, bool)
    ):
        return _observation(
            installed=installed,
            value=TypedValue.enum(None),
            quality=TrunkQuality.BAD,
            reason="TYPE_MISMATCH",
            observed_at=source.source_timestamp,
            received_at=source.received_at,
            calculated_at=calculated_at,
            site_configuration_version=site_configuration_version,
            source_observation_ids=(source.observation_id,),
            source_digest=source.source_digest,
            source_order_key=_raw_order_key(source),
        )
    mapped = transform.entries.get(str(source.value.value))
    if mapped is None:
        return _observation(
            installed=installed,
            value=TypedValue.enum(None),
            quality=TrunkQuality.BAD,
            reason="UNMAPPED_ENUM",
            observed_at=source.source_timestamp,
            received_at=source.received_at,
            calculated_at=calculated_at,
            site_configuration_version=site_configuration_version,
            source_observation_ids=(source.observation_id,),
            source_digest=source.source_digest,
            source_order_key=_raw_order_key(source),
        )
    quality = source.quality
    return _observation(
        installed=installed,
        value=TypedValue.enum(
            mapped if quality not in {TrunkQuality.BAD, TrunkQuality.STALE} else None
        ),
        quality=quality,
        reason=_input_quality_reason(quality),
        observed_at=source.source_timestamp,
        received_at=source.received_at,
        calculated_at=calculated_at,
        site_configuration_version=site_configuration_version,
        source_observation_ids=(source.observation_id,),
        source_digest=source.source_digest,
        source_order_key=_raw_order_key(source),
    )


def _runtime_failure_from_source(
    installed: InstalledPointProcessing,
    source: RawObservation,
    site_configuration_version: int,
    calculated_at: datetime,
    reason: str,
) -> L2Observation:
    return _observation(
        installed=installed,
        value=TypedValue(installed.output_kind, None),
        quality=TrunkQuality.BAD,
        reason=reason,
        observed_at=source.source_timestamp,
        received_at=source.received_at,
        calculated_at=calculated_at,
        site_configuration_version=site_configuration_version,
        source_observation_ids=(source.observation_id,),
        source_digest=source.source_digest,
        source_order_key=_raw_order_key(source),
    )


def _runtime_failure(
    installed: InstalledPointProcessing,
    site_configuration_version: int,
    calculated_at: datetime,
    reason: str,
) -> L2Observation:
    return _observation(
        installed=installed,
        value=TypedValue(installed.output_kind, None),
        quality=TrunkQuality.BAD,
        reason=reason,
        observed_at=calculated_at,
        received_at=calculated_at,
        calculated_at=calculated_at,
        site_configuration_version=site_configuration_version,
        source_observation_ids=(),
        source_digest=hashlib.sha256(b"").hexdigest(),
        source_order_key=f"{calculated_at.isoformat()}||",
    )


def _input_quality_reason(quality: TrunkQuality) -> str | None:
    if quality is TrunkQuality.BAD:
        return "INPUT_BAD"
    if quality is TrunkQuality.STALE:
        return "INPUT_STALE"
    if quality is TrunkQuality.UNCERTAIN:
        return "INPUT_UNCERTAIN"
    return None


def _observation(
    *,
    installed: InstalledPointProcessing,
    value: TypedValue,
    quality: TrunkQuality,
    reason: str | None,
    observed_at: datetime,
    received_at: datetime,
    calculated_at: datetime,
    site_configuration_version: int,
    source_observation_ids: tuple[UUID, ...],
    source_digest: str,
    source_order_key: str,
) -> L2Observation:
    event_material = json.dumps(
        {
            "revision_id": str(installed.revision_id),
            "entity_instance_id": str(installed.entity_instance_id),
            "source_observation_ids": sorted(map(str, source_observation_ids)),
            "quality": int(quality),
            "reason": reason,
            "value_kind": value.kind.value,
            "value": value.value,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return L2Observation(
        event_id=uuid5(NAMESPACE_URL, event_material),
        entity_instance_id=installed.entity_instance_id,
        definition_id=installed.entity_definition_id,
        value=value,
        unit=installed.output_unit,
        quality=quality,
        reason=reason,
        observed_at=observed_at,
        received_at=received_at,
        calculated_at=calculated_at,
        processing_revision_id=installed.revision_id,
        site_configuration_version=site_configuration_version,
        source_observation_ids=tuple(sorted(source_observation_ids, key=str)),
        source_digest=source_digest,
        source_order_key=source_order_key,
    )


def _raw_order_key(source: RawObservation) -> str:
    if source.source_sequence is None:
        return f"D:{source.source_digest}"
    return f"S:{source.source_sequence:020d}:{source.source_digest}"
