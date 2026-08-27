"""L0—L2 数据主干的不可变领域契约。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, IntEnum
import math
from types import MappingProxyType
from typing import Any
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


class SourceOrderMode(str, Enum):
    SEQUENCE = "sequence"
    OBSERVED_AT = "observed_at"
    RECEIVED_AT = "received_at"


def _utc_epoch_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source order timestamp must be timezone-aware")
    normalized = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = normalized - epoch
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


@dataclass(frozen=True)
class SourceOrder:
    mode: SourceOrderMode
    primary: int
    secondary: int
    tie_breaker: str

    @classmethod
    def sequence(cls, value: int) -> "SourceOrder":
        return cls(SourceOrderMode.SEQUENCE, value, 0, "")

    @classmethod
    def observed_at(
        cls,
        value: datetime,
        receive_ordinal: int,
        tie_breaker: str = "",
    ) -> "SourceOrder":
        return cls(
            SourceOrderMode.OBSERVED_AT,
            _utc_epoch_microseconds(value),
            receive_ordinal,
            tie_breaker,
        )

    @classmethod
    def received_at(
        cls,
        value: datetime,
        receive_ordinal: int,
        tie_breaker: str = "",
    ) -> "SourceOrder":
        return cls(
            SourceOrderMode.RECEIVED_AT,
            _utc_epoch_microseconds(value),
            receive_ordinal,
            tie_breaker,
        )

    def __post_init__(self) -> None:
        if self.secondary < 0:
            raise ValueError("source receive ordinal must not be negative")
        if self.mode is SourceOrderMode.SEQUENCE and (
            self.secondary != 0 or self.tie_breaker
        ):
            raise ValueError("sequence source order cannot carry a tie breaker")

    def is_after(self, previous: "SourceOrder") -> bool:
        if self.mode is not previous.mode:
            raise DataTrunkError(
                "DATA_FRAME_SOURCE_ORDER_MODE_MISMATCH",
                "DATA_FRAME_SOURCE_ORDER_MODE_MISMATCH",
            )
        if self.mode is SourceOrderMode.SEQUENCE:
            return self.primary > previous.primary
        return (self.primary, self.secondary, self.tie_breaker) > (
            previous.primary,
            previous.secondary,
            previous.tie_breaker,
        )


@dataclass(frozen=True)
class TypedValue:
    kind: ValueKind
    value: Decimal | float | int | bool | str | tuple[str, ...] | None

    @classmethod
    def float(cls, value: Decimal | float | None) -> "TypedValue":
        return cls(ValueKind.FLOAT, value)

    @classmethod
    def integer(cls, value: Decimal | int | None) -> "TypedValue":
        return cls(ValueKind.INT, value)

    @classmethod
    def boolean(cls, value: bool | None) -> "TypedValue":
        return cls(ValueKind.BOOL, value)

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
    event_time_basis: str
    source_order: SourceOrder | None = None

    def __post_init__(self) -> None:
        if self.event_time_basis not in {"unknown", "observed_at", "received_at"}:
            raise ValueError("raw observation event time basis is invalid")


class BlackboardState(str, Enum):
    WARMING = "WARMING"
    READY = "READY"


class FrameStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class FramedRawObservation:
    observation: RawObservation
    accepted_beat: int
    effective_quality: TrunkQuality


@dataclass(frozen=True)
class FrozenFrameCandidate:
    frame_id: UUID
    candidate_digest: str
    generation: int
    capture_beat: int
    shot_at: datetime
    configuration_revision: int
    cells: Mapping[UUID, FramedRawObservation]
    changed_l0: tuple[FramedRawObservation, ...]


@dataclass(frozen=True)
class PendingFrame:
    frame_id: UUID
    frame_sequence: int
    capture_beat: int
    shot_at: datetime
    configuration_revision: int
    status: FrameStatus


@dataclass(frozen=True)
class ClaimedFrame:
    frame_id: UUID
    frame_sequence: int
    capture_beat: int
    shot_at: datetime
    configuration_revision: int
    attempt_count: int
    processing_owner: UUID
    processing_token: UUID
    lease_until: datetime
    created_at: datetime


@dataclass(frozen=True)
class BudgetTerminalizationClaim:
    frame_id: UUID
    frame_sequence: int
    capture_beat: int
    shot_at: datetime
    configuration_revision: int
    attempt_count: int
    processing_owner: UUID
    processing_token: UUID
    lease_until: datetime
    created_at: datetime
    affected_l2: frozenset[UUID]


@dataclass(frozen=True)
class FrameFailure:
    code: str
    failed_entity_ids: frozenset[UUID]


@dataclass(frozen=True)
class BlackboardRecovery:
    capture_beat: int
    configuration_revision: int
    active_input_contracts: Mapping[UUID, SourceOrderMode]
    required_tag_ids: frozenset[UUID]
    observations: tuple[FramedRawObservation, ...]


@dataclass(frozen=True)
class TerminalFrame:
    frame_id: UUID
    frame_sequence: int
    configuration_revision: int
    status: FrameStatus
    finished_at: datetime


@dataclass(frozen=True)
class AcceptReceipt:
    accepted_count: int
    dropped_count: int


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


@dataclass(frozen=True)
class BooleanCodeInput:
    input: InputReference
    code: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("boolean-set code is required")
        object.__setattr__(self, "code", self.code.strip())


@dataclass(frozen=True)
class BooleanSetTransform:
    inputs: tuple[BooleanCodeInput, ...]

    def __post_init__(self) -> None:
        canonical = tuple(sorted(self.inputs, key=lambda item: item.code))
        if (
            not canonical
            or len({item.input for item in canonical}) != len(canonical)
            or len({item.code for item in canonical}) != len(canonical)
        ):
            raise ValueError("boolean-set inputs and codes must be unique")
        object.__setattr__(self, "inputs", canonical)


@dataclass(frozen=True)
class FormulaSource:
    name: str
    data_type: ValueKind
    unit: str | None
    cardinality: str
    required: bool
    default_value: float | int | bool | None

    def __post_init__(self) -> None:
        if not self.name.strip() or self.cardinality not in {"one", "many"}:
            raise ValueError("formula source contract is invalid")
        if self.data_type not in {ValueKind.FLOAT, ValueKind.INT, ValueKind.BOOL}:
            raise ValueError("formula source data type is unsupported")
        if self.cardinality == "many" and self.default_value is not None:
            raise ValueError("collection formula sources cannot have scalar defaults")
        if self.required and self.default_value is not None:
            raise ValueError("required formula sources cannot have defaults")
        if self.default_value is not None:
            if self.data_type is ValueKind.BOOL:
                valid_default = isinstance(self.default_value, bool)
            elif self.data_type is ValueKind.INT:
                valid_default = isinstance(self.default_value, int) and not isinstance(
                    self.default_value, bool
                )
            else:
                valid_default = isinstance(self.default_value, (int, float)) and not isinstance(
                    self.default_value, bool
                ) and math.isfinite(float(self.default_value))
            if not valid_default:
                raise ValueError("formula source default does not match its data type")
        object.__setattr__(self, "name", self.name.strip())


@dataclass(frozen=True)
class CompiledFormula:
    text: str
    ast: Mapping[str, Any]
    digest: str
    result_kind: ValueKind
    result_unit: str | None

    def __post_init__(self) -> None:
        if len(self.digest) != 64:
            raise ValueError("formula digest must be SHA-256")
        object.__setattr__(self, "ast", MappingProxyType(dict(self.ast)))


@dataclass(frozen=True)
class FormulaTransform:
    sources: Mapping[str, tuple[InputReference, ...]]
    source_contracts: Mapping[str, FormulaSource]
    compiled: CompiledFormula
    schedule_seconds: int
    control_eligible: bool

    def __post_init__(self) -> None:
        source_map = {
            name: tuple(sorted(references, key=lambda item: str(item.source_id)))
            for name, references in self.sources.items()
        }
        contracts = dict(self.source_contracts)
        if (
            set(source_map) != set(contracts)
            or not 1 <= self.schedule_seconds <= 3600
            or any(
                reference.source_kind != "l2"
                for references in source_map.values()
                for reference in references
            )
            or any(
                len(set(references)) != len(references)
                for references in source_map.values()
            )
            or any(
                contracts[name].cardinality == "one"
                and (
                    len(references) > 1
                    or (
                        not references
                        and (
                            contracts[name].required
                            or contracts[name].default_value is None
                        )
                    )
                )
                for name, references in source_map.items()
            )
        ):
            raise ValueError("formula transform contract is invalid")
        object.__setattr__(self, "sources", MappingProxyType(source_map))
        object.__setattr__(self, "source_contracts", MappingProxyType(contracts))


Transform = (
    NumericTransform
    | EnumTransform
    | FaultCodeTransform
    | BooleanSetTransform
    | FormulaTransform
)


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
class ProcessingSnapshot:
    l0_by_tag: Mapping[UUID, FramedRawObservation]
    installed_by_entity_id: Mapping[UUID, InstalledPointProcessing]
    topological_output_ids: tuple[UUID, ...]
    dependency_edges: tuple[tuple[UUID, UUID], ...]

    def current_inputs(
        self,
    ) -> dict[InputReference, RawObservation | "L2Observation"]:
        return {
            InputReference.l0(tag_id): replace(
                cell.observation,
                quality=cell.effective_quality,
            )
            for tag_id, cell in self.l0_by_tag.items()
        }


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
    configuration_revision: int
    source_observation_ids: tuple[UUID, ...]
    source_digest: str
    source_order_key: str
    event_time_basis: str
    frame_id: UUID | None = None
    frame_sequence: int = 0

    def __post_init__(self) -> None:
        if self.event_time_basis not in {
            "observed_at",
            "received_at",
            "calculated_at",
            "unknown",
        }:
            raise ValueError("L2 observation event time basis is invalid")


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
