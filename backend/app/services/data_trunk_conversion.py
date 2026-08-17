"""把固定 L1 修订纯计算为确定性的 L2 观测。"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.data_trunk_contracts import (
    DataTrunkError,
    InputReference,
    InstalledPointConversion,
    L2Observation,
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)


def evaluate_conversion(
    *,
    installed: tuple[InstalledPointConversion, ...],
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
    installed: InstalledPointConversion,
    current_inputs: Mapping[InputReference, RawObservation | L2Observation],
    site_configuration_version: int,
    calculated_at: datetime,
) -> L2Observation:
    if installed.output_kind is not ValueKind.FLOAT:
        raise DataTrunkError(
            "POINT_CONVERSION_CONFIGURATION_INVALID",
            "numeric conversion output must be FLOAT",
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
            "POINT_CONVERSION_CONFIGURATION_INVALID",
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
            "POINT_CONVERSION_CONFIGURATION_INVALID",
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
        reason=None,
        observed_at=source.source_timestamp,
        received_at=source.received_at,
        calculated_at=calculated_at,
        site_configuration_version=site_configuration_version,
        source_observation_ids=(source.observation_id,),
        source_digest=source.source_digest,
        source_order_key=_raw_order_key(source),
    )


def _runtime_failure_from_source(
    installed: InstalledPointConversion,
    source: RawObservation,
    site_configuration_version: int,
    calculated_at: datetime,
    reason: str,
) -> L2Observation:
    return _observation(
        installed=installed,
        value=TypedValue.float(None),
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
    installed: InstalledPointConversion,
    site_configuration_version: int,
    calculated_at: datetime,
    reason: str,
) -> L2Observation:
    return _observation(
        installed=installed,
        value=TypedValue.float(None),
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


def _observation(
    *,
    installed: InstalledPointConversion,
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
        conversion_revision_id=installed.revision_id,
        site_configuration_version=site_configuration_version,
        source_observation_ids=tuple(sorted(source_observation_ids, key=str)),
        source_digest=source_digest,
        source_order_key=source_order_key,
    )


def _raw_order_key(source: RawObservation) -> str:
    sequence = "" if source.source_sequence is None else str(source.source_sequence)
    return f"{source.source_timestamp.isoformat()}|{sequence}|{source.source_digest}"
