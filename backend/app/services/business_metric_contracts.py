"""Immutable contracts shared by business-metric package compilation and delivery."""
from __future__ import annotations

from dataclasses import dataclass
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
class MetricSourceOption:
    method: MetricAggregator
    entity_definition_id: str
    priority: int


@dataclass(frozen=True)
class MetricQualityContract:
    minimum_coverage: float


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
