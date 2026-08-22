"""L0—L2 数据主干的不可变领域契约。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum
from types import MappingProxyType
from uuid import UUID


class TrunkQuality(IntEnum):
    BAD = 0
    STALE = 1
    UNCERTAIN = 64
    GOOD = 192


class ValueKind(str, Enum):
    FLOAT = "FLOAT"
    INT = "INT"
    BOOL = "BOOL"
    STRING = "STRING"
    ENUM = "ENUM"
    CODE_SET = "CODE_SET"


@dataclass(frozen=True)
class TypedValue:
    kind: ValueKind
    value: float | int | bool | str | tuple[str, ...] | None

    @classmethod
    def float(cls, value: float | None) -> "TypedValue":
        return cls(ValueKind.FLOAT, value)

    @classmethod
    def enum(cls, value: str | None) -> "TypedValue":
        return cls(ValueKind.ENUM, value)

    @classmethod
    def code_set(cls, value: tuple[str, ...] | None) -> "TypedValue":
        canonical = None if value is None else tuple(sorted(set(value)))
        return cls(ValueKind.CODE_SET, canonical)


@dataclass(frozen=True)
class RawObservation:
    observation_id: UUID
    node_id: UUID
    tag_id: UUID
    source_key: str
    value: TypedValue
    raw_unit: str | None
    quality: TrunkQuality
    source_timestamp: datetime
    received_at: datetime
    source_message_id: str | None
    source_sequence: int | None
    source_digest: str


@dataclass(frozen=True)
class InputReference:
    source_kind: str
    source_id: UUID

    @classmethod
    def l0(cls, tag_id: UUID) -> "InputReference":
        return cls("l0", tag_id)

    @classmethod
    def l2(cls, entity_instance_id: UUID) -> "InputReference":
        return cls("l2", entity_instance_id)


@dataclass(frozen=True)
class NumericTransform:
    input: InputReference
    scale: float
    offset: float
    input_unit: str | None
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True)
class EnumTransform:
    input: InputReference
    entries: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entries",
            MappingProxyType(dict(sorted(self.entries.items()))),
        )


@dataclass(frozen=True)
class FaultCodeTransform:
    input: InputReference
    delimiter: str
    entries: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.delimiter not in {"semicolon", "comma", "pipe", "whitespace"}:
            raise ValueError("unsupported fault-code delimiter")
        object.__setattr__(
            self,
            "entries",
            MappingProxyType(
                {
                    raw.strip().upper(): canonical.strip()
                    for raw, canonical in sorted(self.entries.items())
                }
            ),
        )


Transform = NumericTransform | EnumTransform | FaultCodeTransform


@dataclass(frozen=True)
class InstalledPointProcessing:
    installation_id: UUID
    revision_id: UUID
    entity_instance_id: UUID
    entity_definition_id: str
    output_kind: ValueKind
    output_unit: str | None
    freshness_seconds: float
    transform: Transform

    @classmethod
    def numeric(
        cls,
        *,
        installation_id: UUID,
        revision_id: UUID,
        input_tag_id: UUID,
        output_entity_instance_id: UUID,
        output_definition_id: str,
        scale: float,
        offset: float,
        input_unit: str | None,
        output_unit: str | None,
        minimum: float | None,
        maximum: float | None,
    ) -> "InstalledPointProcessing":
        return cls(
            installation_id=installation_id,
            revision_id=revision_id,
            entity_instance_id=output_entity_instance_id,
            entity_definition_id=output_definition_id,
            output_kind=ValueKind.FLOAT,
            output_unit=output_unit,
            freshness_seconds=30.0,
            transform=NumericTransform(
                input=InputReference.l0(input_tag_id),
                scale=scale,
                offset=offset,
                input_unit=input_unit,
                minimum=minimum,
                maximum=maximum,
            ),
        )


@dataclass(frozen=True)
class L2Observation:
    event_id: UUID
    entity_instance_id: UUID
    definition_id: str
    value: TypedValue
    unit: str | None
    quality: TrunkQuality
    reason: str | None
    observed_at: datetime
    received_at: datetime
    calculated_at: datetime
    processing_revision_id: UUID
    site_configuration_version: int
    source_observation_ids: tuple[UUID, ...]
    source_digest: str
    source_order_key: str


@dataclass(frozen=True)
class CommitReceipt:
    transaction_id: UUID
    accepted_l0_count: int
    duplicate_l0_count: int
    l2_event_ids: tuple[UUID, ...]
    late_observation_count: int
    failure_reference: UUID | None = None
    accepted_l0_observation_ids: tuple[UUID, ...] = ()


class DataTrunkError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
