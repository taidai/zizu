"""Immutable contracts shared by business-metric package compilation and delivery."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID

from app.services.solution_point_processings import PointProcessingAsset


class WindowKind(str, Enum):
    ALIGNED_DAILY = "aligned_daily"
    ROLLING = "rolling"


class MetricAggregator(str, Enum):
    COUNTER_DELTA = "counter_delta"
    POWER_INTEGRAL = "power_integral"
    AVERAGE = "average"
    MAXIMUM = "maximum"


class FlowDirection(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    BOTH = "both"


class MetricLifecycle(str, Enum):
    PROVISIONAL = "provisional"
    COMPLETED = "completed"
    CORRECTED = "corrected"
    INVALID = "invalid"


@dataclass(frozen=True)
class MetricCounterContract:
    maximum: Decimal
    bit_width: int | None
    reset_on_decrease: bool
    rollover_on_decrease: bool

    def __post_init__(self) -> None:
        maximum = Decimal(self.maximum)
        if not maximum.is_finite() or maximum < 0:
            raise ValueError("counter maximum must be finite and non-negative")
        if self.bit_width is not None:
            if self.bit_width not in {16, 32, 64}:
                raise ValueError("counter bit width must be 16, 32, or 64")
            if maximum != Decimal((1 << self.bit_width) - 1):
                raise ValueError("counter maximum must match bit width")
        if self.rollover_on_decrease and self.bit_width is None:
            raise ValueError("counter rollover requires bit width")
        if not isinstance(self.reset_on_decrease, bool) or not isinstance(
            self.rollover_on_decrease, bool
        ):
            raise ValueError("counter decrease rules must be boolean")
        if self.reset_on_decrease and self.rollover_on_decrease:
            raise ValueError("counter reset and rollover rules are mutually exclusive")
        object.__setattr__(self, "maximum", maximum)


@dataclass(frozen=True)
class MetricSourceOption:
    method: MetricAggregator
    entity_definition_id: str
    priority: int
    counter_contract: MetricCounterContract | None = None


@dataclass(frozen=True)
class MetricQualityContract:
    good_coverage: float
    minimum_usable_coverage: float


@dataclass(frozen=True)
class BusinessMetricTemplate:
    template_id: str
    revision: int
    display_name: str
    target_node_type: str
    output_entity_definition_id: str
    output_data_type: str
    output_unit: str | None
    temporal_semantics: str
    window_kind: WindowKind
    rolling_window_seconds: int | None
    sources: tuple[MetricSourceOption, ...]
    quality: MetricQualityContract
    allowed_lateness_seconds: int
    automatic_correction_horizon_seconds: int
    control_eligible: bool
    flow_direction: FlowDirection
    normalize_flow_direction: bool
    content_digest: str


@dataclass(frozen=True)
class ResolvedMetricSource:
    """One source selected by installation preview and frozen into a revision."""

    entity_instance_id: UUID
    entity_definition_id: str
    method: MetricAggregator
    data_type: str
    unit: str | None
    estimated: bool
    direction: str = "R"
    maximum_sample_gap_seconds: int | None = None
    producer_contract_digest: str | None = None
    counter_contract: MetricCounterContract | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", MetricAggregator(self.method))


@dataclass(frozen=True)
class MetricSourceResolution:
    """The installation-time source decision; no runtime discovery is permitted."""

    timezone: str
    sources: tuple[ResolvedMetricSource, ...]


@dataclass(frozen=True)
class CompiledMetricRevision:
    processing_revision_id: UUID
    point_processing_asset: PointProcessingAsset
    temporal_semantics: str
    control_eligible: bool
    template_digest: str
    source_digest: str
    content_digest: str
    timezone: str
    sources: tuple[ResolvedMetricSource, ...]
