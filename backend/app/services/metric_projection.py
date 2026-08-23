"""Deterministic, I/O-free projection calculations for windowed L2 metrics.

The module deliberately knows nothing about persistence, clocks, late-arrival
policy, or publication.  Callers pass a frozen compiled revision, the previous
recoverable state, committed L2 observations, and an explicit ``now``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import math
from typing import Iterable, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.business_metric_contracts import (
    CompiledMetricRevision,
    FlowDirection,
    MetricAggregator,
    MetricLifecycle,
    WindowKind,
)
from app.services.data_trunk_contracts import L2Observation, TrunkQuality


Number = int | float | Decimal
_BAD_QUALITIES = {TrunkQuality.BAD, TrunkQuality.STALE}


@dataclass(frozen=True)
class MetricWindow:
    start: datetime
    end: datetime
    include_start: bool = True
    include_end: bool = False

    def __post_init__(self) -> None:
        start = _utc(self.start, "window start")
        end = _utc(self.end, "window end")
        if start >= end:
            raise ValueError("metric window end must be after its start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def contains(self, instant: datetime) -> bool:
        candidate = _utc(instant, "event timestamp")
        starts_inside = candidate >= self.start if self.include_start else candidate > self.start
        ends_inside = candidate <= self.end if self.include_end else candidate < self.end
        return starts_inside and ends_inside


@dataclass(frozen=True)
class CounterContract:
    """Frozen interpretation of a counter decrease.

    A decrease is accepted only when exactly one installed rule classifies it.
    This prevents runtime magnitude heuristics from guessing reset versus wrap.
    """

    maximum: Number
    bit_width: int | None = None
    reset_on_decrease: bool = False
    rollover_on_decrease: bool = False

    def __post_init__(self) -> None:
        maximum = _decimal(self.maximum)
        if maximum < 0:
            raise ValueError("counter maximum must not be negative")
        if self.bit_width is not None:
            if self.bit_width not in {16, 32, 64}:
                raise ValueError("counter bit width must be 16, 32, or 64")
            if maximum != Decimal((1 << self.bit_width) - 1):
                raise ValueError("counter maximum must match its frozen bit width")
        if self.rollover_on_decrease and self.bit_width is None:
            raise ValueError("counter rollover requires a frozen bit width")
        if not isinstance(self.reset_on_decrease, bool) or not isinstance(
            self.rollover_on_decrease, bool
        ):
            raise ValueError("counter decrease rules must be boolean")
        object.__setattr__(self, "maximum", maximum)


@dataclass(frozen=True)
class MetricCalculation:
    value: Decimal | None
    coverage: float
    valid: bool
    quality: TrunkQuality
    reason: str | None = None
    peak_at: datetime | None = None
    peak_event_id: UUID | None = None
    source_event_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class MetricProjectionState:
    revision: CompiledMetricRevision
    events: tuple[L2Observation, ...] = ()
    counter_contract: CounterContract | None = None
    maximum_sample_gap_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revision, CompiledMetricRevision):
            raise TypeError("projection state requires a compiled metric revision")
        if not isinstance(self.events, tuple):
            raise TypeError("projection state events must be immutable")
        if self.maximum_sample_gap_seconds is not None and (
            not isinstance(self.maximum_sample_gap_seconds, int)
            or isinstance(self.maximum_sample_gap_seconds, bool)
            or self.maximum_sample_gap_seconds <= 0
        ):
            raise ValueError("maximum sample gap must be a positive number of seconds")


@dataclass(frozen=True)
class ProjectionDecision:
    window: MetricWindow
    lifecycle: MetricLifecycle
    quality: TrunkQuality
    value: Decimal | None
    coverage: float
    estimated: bool
    updated_at: datetime
    reason: str | None
    source_event_ids: tuple[UUID, ...]
    peak_at: datetime | None = None
    peak_event_id: UUID | None = None
    history_facts: tuple[object, ...] = ()


def aligned_daily_window(now: datetime, timezone: str) -> MetricWindow:
    """Return the local civil day containing ``now`` as UTC boundaries."""
    instant = _utc(now, "now")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError("metric timezone must be a valid IANA timezone") from exc
    local_day = instant.astimezone(zone).date()
    local_start = datetime.combine(local_day, time.min, tzinfo=zone)
    local_end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=zone)
    return MetricWindow(local_start.astimezone(UTC), local_end.astimezone(UTC), True, False)


def rolling_window(end: datetime, seconds: int) -> MetricWindow:
    """Return ``(end-seconds, end]`` without consulting a wall clock."""
    end_utc = _utc(end, "window end")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
        raise ValueError("rolling window seconds must be a positive integer")
    return MetricWindow(end_utc - timedelta(seconds=seconds), end_utc, False, True)


def counter_delta(
    samples: Sequence[Number] | Sequence[L2Observation],
    contract: CounterContract,
    window: MetricWindow | None = None,
) -> MetricCalculation:
    """Sum monotonic counter segments under one explicit frozen decrease policy."""
    if not isinstance(contract, CounterContract):
        raise TypeError("counter calculation requires a frozen counter contract")
    prepared = _calculation_samples(samples, window)
    if any(item.quality in _BAD_QUALITIES for item in prepared):
        return _invalid_calculation("SOURCE_BAD", prepared)
    values = tuple(_decimal(item.value) for item in prepared)
    for value in values:
        if value < 0 or value > contract.maximum:
            return _invalid_calculation("COUNTER_VALUE_OUT_OF_RANGE", prepared)
    total = Decimal(0)
    for previous, current in zip(values, values[1:]):
        if current >= previous:
            total += current - previous
            continue
        rules = int(contract.reset_on_decrease) + int(contract.rollover_on_decrease)
        if rules != 1:
            return _invalid_calculation("COUNTER_DISCONTINUITY_AMBIGUOUS", prepared)
        if contract.reset_on_decrease:
            total += current
        else:
            total += contract.maximum - previous + Decimal(1) + current
    coverage = _span_coverage(prepared, window)
    quality = _source_quality(prepared)
    return MetricCalculation(
        value=total,
        coverage=coverage,
        valid=True,
        quality=quality,
        source_event_ids=_event_ids(prepared),
    )


def integrate_power(
    events: Sequence[L2Observation],
    window: MetricWindow,
    *,
    maximum_sample_gap_seconds: int,
    direction: FlowDirection | str,
) -> MetricCalculation:
    """Trapezoid-integrate selected power flow to energy in kWh."""
    prepared = _calculation_samples(events, window)
    maximum_gap = _maximum_gap(maximum_sample_gap_seconds)
    try:
        flow = FlowDirection(direction)
    except ValueError as exc:
        raise ValueError("power flow direction is invalid") from exc
    total_kw_seconds = Decimal(0)
    covered_seconds = Decimal(0)
    for previous, current in zip(prepared, prepared[1:]):
        interval = Decimal(str((current.observed_at - previous.observed_at).total_seconds()))
        if interval <= 0 or interval > maximum_gap:
            continue
        previous_value = _flow_value(_decimal(previous.value), flow)
        current_value = _flow_value(_decimal(current.value), flow)
        total_kw_seconds += (previous_value + current_value) * interval / Decimal(2)
        covered_seconds += interval
    coverage = _duration_coverage(covered_seconds, window)
    return MetricCalculation(
        value=total_kw_seconds / Decimal(3600),
        coverage=coverage,
        valid=True,
        quality=_source_quality(prepared),
        source_event_ids=_event_ids(prepared),
    )


def time_weighted_average(
    events: Sequence[L2Observation],
    window: MetricWindow,
    *,
    maximum_sample_gap_seconds: int,
) -> MetricCalculation:
    """Average by integrable elapsed time, never by number of samples."""
    prepared = _calculation_samples(events, window)
    maximum_gap = _maximum_gap(maximum_sample_gap_seconds)
    weighted = Decimal(0)
    covered_seconds = Decimal(0)
    for previous, current in zip(prepared, prepared[1:]):
        interval = Decimal(str((current.observed_at - previous.observed_at).total_seconds()))
        if interval <= 0 or interval > maximum_gap:
            continue
        previous_value = _decimal(previous.value)
        current_value = _decimal(current.value)
        weighted += (previous_value + current_value) * interval / Decimal(2)
        covered_seconds += interval
    value = weighted / covered_seconds if covered_seconds else None
    return MetricCalculation(
        value=value,
        coverage=_duration_coverage(covered_seconds, window),
        valid=value is not None,
        quality=_source_quality(prepared) if value is not None else TrunkQuality.BAD,
        reason=None if value is not None else "COVERAGE_INSUFFICIENT",
        source_event_ids=_event_ids(prepared),
    )


def window_maximum(
    events: Sequence[L2Observation],
    window: MetricWindow,
) -> MetricCalculation:
    """Select the stable first maximum among quality-usable source samples."""
    prepared = tuple(
        item for item in _calculation_samples(events, window)
        if item.quality not in _BAD_QUALITIES
    )
    valued = tuple((item, _decimal(item.value)) for item in prepared)
    if not valued:
        return MetricCalculation(None, 0.0, False, TrunkQuality.BAD, "COVERAGE_INSUFFICIENT")
    winner, value = max(valued, key=lambda pair: pair[1])
    return MetricCalculation(
        value=value,
        coverage=_span_coverage(prepared, window),
        valid=True,
        quality=_source_quality(prepared),
        peak_at=winner.observed_at,
        peak_event_id=winner.event_id,
        source_event_ids=_event_ids(prepared),
    )


def project_metric(
    state: MetricProjectionState,
    events: Sequence[L2Observation],
    now: datetime,
) -> ProjectionDecision:
    """Project one current window from immutable input with no I/O or clock reads."""
    if not isinstance(state, MetricProjectionState):
        raise TypeError("project_metric requires a metric projection state")
    instant = _utc(now, "now")
    contract = _revision_contract(state.revision)
    if contract.window_kind is WindowKind.ALIGNED_DAILY:
        window = aligned_daily_window(instant, state.revision.timezone)
    else:
        if contract.rolling_window_seconds is None:
            raise ValueError("rolling projection requires frozen window seconds")
        window = rolling_window(instant, contract.rolling_window_seconds)

    combined, duplicate_conflict = _stable_unique_events((*state.events, *events))
    selected = tuple(item for item in combined if window.contains(item.observed_at))
    if duplicate_conflict:
        return _invalid_decision(window, instant, selected, contract.estimated, "DUPLICATE_EVENT_CONFLICT")
    if any(item.quality in _BAD_QUALITIES for item in selected):
        return _invalid_decision(window, instant, selected, contract.estimated, "SOURCE_BAD")

    try:
        calculation = _calculate(state, selected, window, contract)
    except (InvalidOperation, ValueError) as exc:
        if "timezone-aware" in str(exc):
            raise
        reason = "NONFINITE_VALUE" if "finite" in str(exc) else "CALCULATION_CONTRACT_INVALID"
        return _invalid_decision(window, instant, selected, contract.estimated, reason)

    if not calculation.valid:
        return _invalid_decision(
            window,
            instant,
            selected,
            contract.estimated,
            calculation.reason or "CALCULATION_INVALID",
            coverage=calculation.coverage,
        )
    if calculation.coverage < contract.minimum_usable_coverage:
        return _invalid_decision(
            window,
            instant,
            selected,
            contract.estimated,
            "COVERAGE_INSUFFICIENT",
            coverage=calculation.coverage,
        )
    quality = (
        TrunkQuality.GOOD
        if calculation.coverage >= contract.good_coverage
        and calculation.quality is TrunkQuality.GOOD
        else TrunkQuality.UNCERTAIN
    )
    return ProjectionDecision(
        window=window,
        lifecycle=MetricLifecycle.PROVISIONAL,
        quality=quality,
        value=calculation.value,
        coverage=calculation.coverage,
        estimated=contract.estimated,
        updated_at=instant,
        reason=None,
        source_event_ids=calculation.source_event_ids,
        peak_at=calculation.peak_at,
        peak_event_id=calculation.peak_event_id,
        history_facts=(),
    )


@dataclass(frozen=True)
class _RevisionContract:
    window_kind: WindowKind
    rolling_window_seconds: int | None
    method: MetricAggregator
    good_coverage: float
    minimum_usable_coverage: float
    flow_direction: FlowDirection
    estimated: bool


@dataclass(frozen=True)
class _Sample:
    value: Number
    observed_at: datetime
    quality: TrunkQuality
    event_id: UUID | None
    source_order_key: str


def _revision_contract(revision: CompiledMetricRevision) -> _RevisionContract:
    outputs = revision.point_processing_asset.outputs
    if len(outputs) != 1:
        raise ValueError("compiled metric revision must have one output")
    transform = outputs[0].transform
    methods = transform.get("methods")
    if not isinstance(methods, tuple) or len(methods) != 1:
        raise ValueError("compiled metric revision must freeze one method")
    try:
        window_kind = WindowKind(transform.get("window"))
        method = MetricAggregator(methods[0])
        direction = FlowDirection(transform.get("flowDirection"))
    except ValueError as exc:
        raise ValueError("compiled metric revision calculation contract is invalid") from exc
    good = _finite_ratio(transform.get("qualityGoodCoverage"), "good coverage")
    minimum = _finite_ratio(
        transform.get("qualityMinimumUsableCoverage"),
        "minimum usable coverage",
    )
    if minimum > good:
        raise ValueError("minimum usable coverage cannot exceed good coverage")
    rolling_seconds = transform.get("rollingWindowSeconds")
    if rolling_seconds is not None and (
        not isinstance(rolling_seconds, int)
        or isinstance(rolling_seconds, bool)
        or rolling_seconds <= 0
    ):
        raise ValueError("compiled rolling window seconds are invalid")
    return _RevisionContract(
        window_kind=window_kind,
        rolling_window_seconds=rolling_seconds,
        method=method,
        good_coverage=good,
        minimum_usable_coverage=minimum,
        flow_direction=direction,
        estimated=any(source.estimated for source in revision.sources),
    )


def _calculate(
    state: MetricProjectionState,
    events: tuple[L2Observation, ...],
    window: MetricWindow,
    contract: _RevisionContract,
) -> MetricCalculation:
    if contract.method is MetricAggregator.COUNTER_DELTA:
        if state.counter_contract is None:
            return _invalid_calculation("COUNTER_CONTRACT_INVALID", ())
        return counter_delta(events, state.counter_contract, window)
    if contract.method is MetricAggregator.POWER_INTEGRAL:
        return integrate_power(
            events,
            window,
            maximum_sample_gap_seconds=_required_maximum_gap(state),
            direction=contract.flow_direction,
        )
    if contract.method is MetricAggregator.AVERAGE:
        return time_weighted_average(
            events,
            window,
            maximum_sample_gap_seconds=_required_maximum_gap(state),
        )
    return window_maximum(events, window)


def _required_maximum_gap(state: MetricProjectionState) -> int:
    if state.maximum_sample_gap_seconds is None:
        raise ValueError("maximum sample gap is not frozen")
    return state.maximum_sample_gap_seconds


def _calculation_samples(
    samples: Sequence[Number] | Sequence[L2Observation],
    window: MetricWindow | None,
) -> tuple[_Sample, ...]:
    normalized: list[_Sample] = []
    for index, item in enumerate(samples):
        if isinstance(item, L2Observation):
            observed_at = _utc(item.observed_at, "event timestamp")
            if window is not None and not window.contains(observed_at):
                continue
            normalized.append(
                _Sample(
                    value=_typed_numeric(item),
                    observed_at=observed_at,
                    quality=TrunkQuality(item.quality),
                    event_id=item.event_id,
                    source_order_key=item.source_order_key,
                )
            )
        else:
            normalized.append(
                _Sample(
                    value=item,
                    observed_at=datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                    quality=TrunkQuality.GOOD,
                    event_id=None,
                    source_order_key=f"N:{index:020d}",
                )
            )
    normalized.sort(
        key=lambda item: (
            item.observed_at,
            item.source_order_key,
            str(item.event_id) if item.event_id is not None else "",
        )
    )
    return tuple(normalized)


def _stable_unique_events(
    events: Iterable[L2Observation],
) -> tuple[tuple[L2Observation, ...], bool]:
    materialized = tuple(events)
    for event in materialized:
        if not isinstance(event, L2Observation):
            raise TypeError("metric events must be committed L2 observations")
        _utc(event.observed_at, "event timestamp")
    ordered = sorted(
        materialized,
        key=lambda item: (
            item.observed_at.astimezone(UTC),
            item.source_order_key,
            str(item.event_id),
        ),
    )
    unique: dict[UUID, L2Observation] = {}
    conflict = False
    for event in ordered:
        existing = unique.get(event.event_id)
        if existing is None:
            unique[event.event_id] = event
        elif existing != event:
            conflict = True
    canonical = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.observed_at.astimezone(UTC),
                item.source_order_key,
                str(item.event_id),
            ),
        )
    )
    return canonical, conflict


def _invalid_calculation(
    reason: str,
    samples: Sequence[_Sample],
) -> MetricCalculation:
    return MetricCalculation(
        value=None,
        coverage=0.0,
        valid=False,
        quality=TrunkQuality.BAD,
        reason=reason,
        source_event_ids=_event_ids(samples),
    )


def _invalid_decision(
    window: MetricWindow,
    now: datetime,
    events: Sequence[L2Observation],
    estimated: bool,
    reason: str,
    *,
    coverage: float = 0.0,
) -> ProjectionDecision:
    return ProjectionDecision(
        window=window,
        lifecycle=MetricLifecycle.INVALID,
        quality=TrunkQuality.BAD,
        value=None,
        coverage=coverage,
        estimated=estimated,
        updated_at=now,
        reason=reason,
        source_event_ids=tuple(item.event_id for item in events),
        history_facts=(),
    )


def _typed_numeric(event: L2Observation) -> Number:
    value = event.value.value
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("metric value must be numeric and finite")
    return value


def _decimal(value: Number) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("metric value must be numeric and finite")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metric value must be finite")
        result = Decimal(str(value))
    else:
        result = Decimal(value)
    if not result.is_finite():
        raise ValueError("metric value must be finite")
    return result


def _flow_value(value: Decimal, direction: FlowDirection) -> Decimal:
    if direction is FlowDirection.POSITIVE:
        return value if value > 0 else Decimal(0)
    if direction is FlowDirection.NEGATIVE:
        return -value if value < 0 else Decimal(0)
    return abs(value)


def _maximum_gap(value: int) -> Decimal:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("maximum sample gap must be a positive number of seconds")
    return Decimal(value)


def _duration_coverage(seconds: Decimal, window: MetricWindow) -> float:
    return min(1.0, float(seconds / Decimal(str(window.duration_seconds))))


def _span_coverage(samples: Sequence[_Sample], window: MetricWindow | None) -> float:
    if window is None:
        return 1.0 if len(samples) >= 2 else 0.0
    if len(samples) < 2:
        return 0.0
    seconds = Decimal(str((samples[-1].observed_at - samples[0].observed_at).total_seconds()))
    return _duration_coverage(max(seconds, Decimal(0)), window)


def _source_quality(samples: Sequence[_Sample]) -> TrunkQuality:
    if not samples:
        return TrunkQuality.BAD
    if any(item.quality in _BAD_QUALITIES for item in samples):
        return TrunkQuality.BAD
    if any(item.quality is TrunkQuality.UNCERTAIN for item in samples):
        return TrunkQuality.UNCERTAIN
    return TrunkQuality.GOOD


def _event_ids(samples: Sequence[_Sample]) -> tuple[UUID, ...]:
    return tuple(item.event_id for item in samples if item.event_id is not None)


def _finite_ratio(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError(f"{name} must be a finite ratio")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "CounterContract",
    "MetricCalculation",
    "MetricProjectionState",
    "MetricWindow",
    "ProjectionDecision",
    "aligned_daily_window",
    "counter_delta",
    "integrate_power",
    "project_metric",
    "rolling_window",
    "time_weighted_average",
    "window_maximum",
]
