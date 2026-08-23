"""Deterministic, I/O-free projection calculations for windowed L2 metrics.

The module deliberately knows nothing about persistence, clocks, late-arrival
policy, or publication.  Callers pass a frozen compiled revision, the previous
recoverable state, committed L2 observations, and an explicit ``now``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
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
_BAD_QUALITIES = frozenset({TrunkQuality.BAD, TrunkQuality.STALE})
_POWER_UNIT_FACTORS = {
    "W": Decimal(1),
    "kW": Decimal(1000),
    "MW": Decimal(1000000),
}
_ENERGY_UNIT_FACTORS = {
    "Wh": Decimal(1),
    "kWh": Decimal(1000),
    "MWh": Decimal(1000000),
}


class MetricProjectionError(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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
    *,
    source_unit: str | None = None,
    output_unit: str | None = None,
) -> MetricCalculation:
    """Sum monotonic counter segments under one explicit frozen decrease policy."""
    if not isinstance(contract, CounterContract):
        raise TypeError("counter calculation requires a frozen counter contract")
    input_kind = _input_kind(samples)
    if input_kind == "mixed":
        return _invalid_calculation("INPUT_KIND_MIXED", ())
    relevant = _counter_relevant_samples(
        _calculation_samples(samples, None),
        window,
    )
    contract_error = _metric_contract_error(
        relevant,
        source_unit,
        output_unit,
        source_family="energy",
        output_family="energy",
        require_units=input_kind == "l2",
    )
    if contract_error is not None:
        return _invalid_calculation(contract_error, relevant)
    prepared, selection_error = _counter_samples(relevant, window, source_unit)
    if selection_error is not None:
        return _invalid_calculation(selection_error, prepared)
    if any(item.quality in _BAD_QUALITIES for item in prepared):
        return _invalid_calculation("SOURCE_BAD", prepared)
    try:
        values = tuple(
            _sample_value(item, source_unit, "energy")
            if source_unit is not None or output_unit is not None
            else _decimal(item.value)
            for item in prepared
        )
    except MetricProjectionError as exc:
        if any(item.is_l2_observation for item in prepared):
            return _invalid_calculation(exc.reason, prepared)
        raise
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
    coverage = _counter_coverage(prepared, window)
    quality = _source_quality(prepared)
    return MetricCalculation(
        value=_convert_same_family(total, source_unit, output_unit, "energy"),
        coverage=coverage,
        valid=True,
        quality=quality,
        source_event_ids=_event_ids(prepared),
    )


def integrate_power(
    events: Sequence[Number] | Sequence[L2Observation],
    window: MetricWindow,
    *,
    maximum_sample_gap_seconds: int,
    direction: FlowDirection | str,
    source_unit: str | None = "kW",
    output_unit: str | None = "kWh",
) -> MetricCalculation:
    """Trapezoid-integrate selected power flow to energy in kWh."""
    input_kind = _input_kind(events)
    if input_kind == "mixed":
        return _invalid_calculation("INPUT_KIND_MIXED", ())
    prepared = _calculation_samples(events, window)
    contract_error = _metric_contract_error(
        prepared,
        source_unit,
        output_unit,
        source_family="power",
        output_family="energy",
        require_units=input_kind == "l2",
    )
    if contract_error is not None:
        return _invalid_calculation(contract_error, prepared)
    maximum_gap = _maximum_gap(maximum_sample_gap_seconds)
    try:
        flow = FlowDirection(direction)
    except ValueError as exc:
        raise ValueError("power flow direction is invalid") from exc
    total_kw_seconds = Decimal(0)
    covered_seconds = Decimal(0)
    used_samples: list[_Sample] = []
    for previous, current in zip(prepared, prepared[1:]):
        interval = Decimal(str((current.observed_at - previous.observed_at).total_seconds()))
        if (
            interval <= 0
            or interval > maximum_gap
            or previous.quality in _BAD_QUALITIES
            or current.quality in _BAD_QUALITIES
        ):
            continue
        previous_value = (
            _sample_value(previous, source_unit, "power")
            if source_unit is not None or output_unit is not None
            else _decimal(previous.value)
        )
        current_value = (
            _sample_value(current, source_unit, "power")
            if source_unit is not None or output_unit is not None
            else _decimal(current.value)
        )
        total_kw_seconds += _directional_linear_area(
            previous_value,
            current_value,
            interval,
            flow,
        )
        covered_seconds += interval
        used_samples.extend((previous, current))
    coverage = _duration_coverage(covered_seconds, window)
    if not covered_seconds:
        return MetricCalculation(
            value=None,
            coverage=0.0,
            valid=False,
            quality=TrunkQuality.BAD,
            reason="COVERAGE_INSUFFICIENT",
            source_event_ids=_event_ids(prepared),
        )
    value = (
        total_kw_seconds / Decimal(3600)
        if source_unit is None and output_unit is None
        else _power_seconds_to_energy(
            total_kw_seconds,
            _required_unit(source_unit),
            _required_unit(output_unit),
        )
    )
    return MetricCalculation(
        value=value,
        coverage=coverage,
        valid=True,
        quality=_source_quality(used_samples),
        source_event_ids=_event_ids(prepared),
    )


def time_weighted_average(
    events: Sequence[Number] | Sequence[L2Observation],
    window: MetricWindow,
    *,
    maximum_sample_gap_seconds: int,
    source_unit: str | None = None,
    output_unit: str | None = None,
) -> MetricCalculation:
    """Average by integrable elapsed time, never by number of samples."""
    input_kind = _input_kind(events)
    if input_kind == "mixed":
        return _invalid_calculation("INPUT_KIND_MIXED", ())
    prepared = _calculation_samples(events, window)
    contract_error = _metric_contract_error(
        prepared,
        source_unit,
        output_unit,
        source_family="power",
        output_family="power",
        require_units=input_kind == "l2",
    )
    if contract_error is not None:
        return _invalid_calculation(contract_error, prepared)
    maximum_gap = _maximum_gap(maximum_sample_gap_seconds)
    weighted = Decimal(0)
    covered_seconds = Decimal(0)
    used_samples: list[_Sample] = []
    for previous, current in zip(prepared, prepared[1:]):
        interval = Decimal(str((current.observed_at - previous.observed_at).total_seconds()))
        if (
            interval <= 0
            or interval > maximum_gap
            or previous.quality in _BAD_QUALITIES
            or current.quality in _BAD_QUALITIES
        ):
            continue
        previous_value = (
            _sample_value(previous, source_unit, "power")
            if source_unit is not None or output_unit is not None
            else _decimal(previous.value)
        )
        current_value = (
            _sample_value(current, source_unit, "power")
            if source_unit is not None or output_unit is not None
            else _decimal(current.value)
        )
        weighted += (previous_value + current_value) * interval / Decimal(2)
        covered_seconds += interval
        used_samples.extend((previous, current))
    value = weighted / covered_seconds if covered_seconds else None
    if value is not None:
        value = _convert_same_family(value, source_unit, output_unit, "power")
    return MetricCalculation(
        value=value,
        coverage=_duration_coverage(covered_seconds, window),
        valid=value is not None,
        quality=_source_quality(used_samples) if value is not None else TrunkQuality.BAD,
        reason=None if value is not None else "COVERAGE_INSUFFICIENT",
        source_event_ids=_event_ids(prepared),
    )


def window_maximum(
    events: Sequence[Number] | Sequence[L2Observation],
    window: MetricWindow,
    *,
    source_unit: str | None = None,
    output_unit: str | None = None,
) -> MetricCalculation:
    """Select the stable first maximum among quality-usable source samples."""
    input_kind = _input_kind(events)
    if input_kind == "mixed":
        return _invalid_calculation("INPUT_KIND_MIXED", ())
    all_samples = _calculation_samples(events, window)
    contract_error = _metric_contract_error(
        all_samples,
        source_unit,
        output_unit,
        source_family="power",
        output_family="power",
        require_units=input_kind == "l2",
    )
    if contract_error is not None:
        return _invalid_calculation(contract_error, all_samples)
    prepared = tuple(item for item in all_samples if item.quality not in _BAD_QUALITIES)
    valued = tuple(
        (
            item,
            _sample_value(item, source_unit, "power")
            if source_unit is not None or output_unit is not None
            else _decimal(item.value),
        )
        for item in prepared
    )
    if not valued:
        return _invalid_calculation("COVERAGE_INSUFFICIENT", all_samples)
    winner, value = max(valued, key=lambda pair: pair[1])
    return MetricCalculation(
        value=_convert_same_family(value, source_unit, output_unit, "power"),
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

    all_events = (*state.events, *events)
    relevant = _project_relevant_events(all_events, window, contract.method)
    try:
        combined, duplicate_conflict = _ordered_unique_events(relevant)
    except MetricProjectionError as exc:
        return _invalid_decision(
            window,
            instant,
            relevant,
            contract.estimated,
            exc.reason,
        )
    if duplicate_conflict:
        return _invalid_decision(
            window,
            instant,
            combined,
            contract.estimated,
            "DUPLICATE_EVENT_CONFLICT",
        )
    try:
        calculation = _calculate(
            state,
            combined,
            window,
            contract,
        )
    except MetricProjectionError as exc:
        return _invalid_decision(
            window,
            instant,
            combined,
            contract.estimated,
            exc.reason,
        )
    except ValueError as exc:
        if "timezone-aware" in str(exc):
            raise
        reason = "NONFINITE_VALUE" if "finite" in str(exc) else "CALCULATION_CONTRACT_INVALID"
        return _invalid_decision(window, instant, combined, contract.estimated, reason)

    if not calculation.valid:
        evidence_ids = calculation.source_event_ids or _observation_ids(combined)
        return _invalid_decision(
            window,
            instant,
            combined,
            contract.estimated,
            calculation.reason or "CALCULATION_INVALID",
            coverage=calculation.coverage,
            source_event_ids=evidence_ids,
        )
    if calculation.coverage < contract.minimum_usable_coverage:
        evidence_ids = calculation.source_event_ids or _observation_ids(combined)
        return _invalid_decision(
            window,
            instant,
            combined,
            contract.estimated,
            "COVERAGE_INSUFFICIENT",
            coverage=calculation.coverage,
            source_event_ids=evidence_ids,
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
    source_unit: str | None
    output_unit: str | None


@dataclass(frozen=True)
class _Sample:
    value: object
    observed_at: datetime
    quality: TrunkQuality
    event_id: UUID | None
    source_order_key: str
    unit: str | None
    is_l2_observation: bool


def _revision_contract(revision: CompiledMetricRevision) -> _RevisionContract:
    outputs = revision.point_processing_asset.outputs
    if len(outputs) != 1:
        raise ValueError("compiled metric revision must have one output")
    if len(revision.sources) != 1:
        raise ValueError("compiled metric revision must have one frozen source")
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
        source_unit=revision.sources[0].unit,
        output_unit=outputs[0].unit,
    )


def _calculate(
    state: MetricProjectionState,
    events: tuple[L2Observation, ...],
    window: MetricWindow,
    contract: _RevisionContract,
) -> MetricCalculation:
    source_unit = contract.source_unit
    output_unit = contract.output_unit
    source_family = (
        "energy"
        if contract.method is MetricAggregator.COUNTER_DELTA
        else "power"
    )
    output_family = (
        "energy"
        if contract.method
        in {MetricAggregator.COUNTER_DELTA, MetricAggregator.POWER_INTEGRAL}
        else "power"
    )
    prepared = _calculation_samples(events, None)
    contract_error = _metric_contract_error(
        prepared,
        source_unit,
        output_unit,
        source_family=source_family,
        output_family=output_family,
        require_units=True,
    )
    if contract_error is not None:
        return _invalid_calculation(contract_error, prepared)
    if contract.method is MetricAggregator.COUNTER_DELTA:
        if state.counter_contract is None:
            return _invalid_calculation("COUNTER_CONTRACT_INVALID", ())
        return counter_delta(
            events,
            state.counter_contract,
            window,
            source_unit=source_unit,
            output_unit=output_unit,
        )
    if contract.method is MetricAggregator.POWER_INTEGRAL:
        return integrate_power(
            events,
            window,
            maximum_sample_gap_seconds=_required_maximum_gap(state),
            direction=contract.flow_direction,
            source_unit=source_unit,
            output_unit=output_unit,
        )
    if contract.method is MetricAggregator.AVERAGE:
        return time_weighted_average(
            events,
            window,
            maximum_sample_gap_seconds=_required_maximum_gap(state),
            source_unit=source_unit,
            output_unit=output_unit,
        )
    return window_maximum(
        events,
        window,
        source_unit=source_unit,
        output_unit=output_unit,
    )


def _required_maximum_gap(state: MetricProjectionState) -> int:
    if state.maximum_sample_gap_seconds is None:
        raise ValueError("maximum sample gap is not frozen")
    return state.maximum_sample_gap_seconds


def _input_kind(
    samples: Sequence[Number] | Sequence[L2Observation],
) -> str:
    has_l2 = any(isinstance(item, L2Observation) for item in samples)
    has_non_l2 = any(not isinstance(item, L2Observation) for item in samples)
    if has_l2 and has_non_l2:
        return "mixed"
    return "l2" if has_l2 else "number"


def _project_relevant_events(
    events: Sequence[L2Observation],
    window: MetricWindow,
    method: MetricAggregator,
) -> tuple[L2Observation, ...]:
    for event in events:
        if not isinstance(event, L2Observation):
            raise TypeError("metric events must be committed L2 observations")

    lookback_start = window.start - timedelta(seconds=window.duration_seconds)
    relevant: list[L2Observation] = []
    for event in events:
        observed_at = _utc(event.observed_at, "event timestamp")
        if method is MetricAggregator.COUNTER_DELTA:
            if lookback_start <= observed_at <= window.end:
                relevant.append(event)
        elif window.contains(observed_at):
            relevant.append(event)
    return tuple(relevant)


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
                    value=item.value.value,
                    observed_at=observed_at,
                    quality=TrunkQuality(item.quality),
                    event_id=item.event_id,
                    source_order_key=item.source_order_key,
                    unit=item.unit,
                    is_l2_observation=True,
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
                    unit=None,
                    is_l2_observation=False,
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


def _counter_relevant_samples(
    samples: tuple[_Sample, ...],
    window: MetricWindow | None,
) -> tuple[_Sample, ...]:
    if window is None:
        return samples
    lookback_start = window.start - timedelta(seconds=window.duration_seconds)
    return tuple(
        sample
        for sample in samples
        if lookback_start <= sample.observed_at <= window.end
    )


def _counter_samples(
    prepared: tuple[_Sample, ...],
    window: MetricWindow | None,
    source_unit: str | None,
) -> tuple[tuple[_Sample, ...], str | None]:
    if window is None:
        if len(prepared) < 2:
            return prepared, "COUNTER_ENDPOINT_MISSING"
        return prepared, None

    lookback_start = window.start - timedelta(seconds=window.duration_seconds)
    candidates = tuple(
        item
        for item in prepared
        if lookback_start <= item.observed_at <= window.start
    )
    endpoints = tuple(
        item
        for item in prepared
        if item.observed_at > window.start and window.contains(item.observed_at)
    )
    classified_candidates = tuple(
        (item, _counter_baseline_error(item, source_unit))
        for item in candidates
    )
    trusted_candidates = tuple(
        item for item, reason in classified_candidates if reason is None
    )
    if not trusted_candidates:
        if not candidates:
            return prepared, "COUNTER_BASELINE_MISSING"
        non_quality_errors = tuple(
            reason
            for _, reason in classified_candidates
            if reason != "SOURCE_BAD"
        )
        reason = non_quality_errors[-1] if non_quality_errors else "SOURCE_BAD"
        return prepared, reason
    baseline = trusted_candidates[-1]

    if not endpoints:
        return prepared, "COUNTER_ENDPOINT_MISSING"
    return (baseline, *endpoints), None


def _ordered_unique_events(
    events: Iterable[L2Observation],
) -> tuple[tuple[L2Observation, ...], bool]:
    materialized = tuple(events)
    for event in materialized:
        if not isinstance(event, L2Observation):
            raise TypeError("metric events must be committed L2 observations")
    for event in materialized:
        if not isinstance(event.event_id, UUID):
            raise MetricProjectionError(
                "EVENT_ID_INVALID",
                "metric event ID must be a UUID",
            )
    for event in materialized:
        _utc(event.observed_at, "event timestamp")
    ordered = sorted(materialized, key=_observation_order_key)
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
            key=_observation_order_key,
        )
    )
    return canonical, conflict


def _observation_order_key(event: L2Observation) -> tuple[datetime, str, str]:
    return (
        _utc(event.observed_at, "event timestamp"),
        event.source_order_key,
        str(event.event_id),
    )


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
    source_event_ids: tuple[UUID, ...] | None = None,
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
        source_event_ids=(
            _observation_ids(events)
            if source_event_ids is None
            else source_event_ids
        ),
        history_facts=(),
    )


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise MetricProjectionError(
            "VALUE_TYPE_INVALID",
            "metric value must be numeric",
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MetricProjectionError("NONFINITE_VALUE", "metric value must be finite")
        result = Decimal(str(value))
    else:
        result = Decimal(value)
    if not result.is_finite():
        raise MetricProjectionError("NONFINITE_VALUE", "metric value must be finite")
    return result


def _required_unit(unit: str | None) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise MetricProjectionError(
            "UNIT_CONTRACT_INVALID",
            "metric unit contract is missing",
        )
    return unit


def _sample_value(sample: _Sample, source_unit: str | None, family: str) -> Decimal:
    frozen_unit = _required_unit(source_unit)
    _unit_factor(frozen_unit, family, "UNIT_CONTRACT_INVALID")
    _validate_l2_sample_contract(sample, frozen_unit)
    return _decimal(sample.value)


def _counter_baseline_error(
    sample: _Sample,
    source_unit: str | None,
) -> str | None:
    try:
        _validate_l2_sample_contract(sample, source_unit)
    except MetricProjectionError as exc:
        return exc.reason
    if sample.quality in _BAD_QUALITIES:
        return "SOURCE_BAD"
    try:
        _decimal(sample.value)
    except MetricProjectionError as exc:
        return exc.reason
    return None


def _metric_contract_error(
    samples: Sequence[_Sample],
    source_unit: str | None,
    output_unit: str | None,
    *,
    source_family: str,
    output_family: str,
    require_units: bool = False,
) -> str | None:
    has_l2_samples = any(sample.is_l2_observation for sample in samples)
    try:
        _validate_l2_sample_identities(samples)
        has_declared_units = source_unit is not None or output_unit is not None
        if not (has_l2_samples or require_units or has_declared_units):
            return None
        frozen_source_unit = _required_unit(source_unit)
        frozen_output_unit = _required_unit(output_unit)
        for sample in samples:
            _validate_l2_sample_unit(sample, frozen_source_unit)
        _unit_factor(frozen_source_unit, source_family, "UNIT_CONTRACT_INVALID")
        _unit_factor(frozen_output_unit, output_family, "UNIT_CONTRACT_INVALID")
    except MetricProjectionError as exc:
        return exc.reason
    return None


def _validate_l2_sample_identities(samples: Sequence[_Sample]) -> None:
    for sample in samples:
        _validate_l2_sample_identity(sample)


def _validate_l2_sample_identity(sample: _Sample) -> None:
    if not sample.is_l2_observation:
        return
    if not isinstance(sample.event_id, UUID):
        raise MetricProjectionError(
            "EVENT_ID_INVALID",
            "metric event ID must be a UUID",
        )


def _validate_l2_sample_unit(sample: _Sample, frozen_unit: str) -> None:
    if not sample.is_l2_observation:
        return
    if sample.unit != frozen_unit:
        raise MetricProjectionError(
            "UNIT_MISMATCH",
            "metric event unit does not match the frozen source unit",
        )


def _validate_l2_sample_contract(
    sample: _Sample,
    frozen_unit: str | None,
) -> None:
    _validate_l2_sample_identity(sample)
    if not sample.is_l2_observation:
        return
    _validate_l2_sample_unit(sample, _required_unit(frozen_unit))


def _convert_same_family(
    value: Decimal,
    source_unit: str | None,
    output_unit: str | None,
    family: str,
) -> Decimal:
    if source_unit is None and output_unit is None:
        return value
    source_factor = _unit_factor(
        _required_unit(source_unit),
        family,
        "UNIT_CONTRACT_INVALID",
    )
    output_factor = _unit_factor(
        _required_unit(output_unit),
        family,
        "UNIT_CONTRACT_INVALID",
    )
    return value * source_factor / output_factor


def _power_seconds_to_energy(
    value: Decimal,
    source_unit: str,
    output_unit: str,
) -> Decimal:
    power_factor = _unit_factor(source_unit, "power", "UNIT_CONTRACT_INVALID")
    energy_factor = _unit_factor(output_unit, "energy", "UNIT_CONTRACT_INVALID")
    return value * power_factor / Decimal(3600) / energy_factor


def _unit_factor(unit: str, family: str, reason: str) -> Decimal:
    factors = _POWER_UNIT_FACTORS if family == "power" else _ENERGY_UNIT_FACTORS
    try:
        return factors[unit]
    except (KeyError, TypeError) as exc:
        raise MetricProjectionError(
            reason,
            f"metric unit {unit!r} is not a supported {family} unit",
        ) from exc


def _directional_linear_area(
    start: Decimal,
    end: Decimal,
    duration: Decimal,
    direction: FlowDirection,
) -> Decimal:
    positive = _positive_linear_area(start, end, duration)
    if direction is FlowDirection.POSITIVE:
        return positive
    negative = _positive_linear_area(-start, -end, duration)
    if direction is FlowDirection.NEGATIVE:
        return negative
    return positive + negative


def _positive_linear_area(
    start: Decimal,
    end: Decimal,
    duration: Decimal,
) -> Decimal:
    if start <= 0 and end <= 0:
        return Decimal(0)
    if start >= 0 and end >= 0:
        return (start + end) * duration / Decimal(2)
    if start > 0:
        positive_duration = duration * start / (start - end)
        return start * positive_duration / Decimal(2)
    positive_duration = duration * end / (end - start)
    return end * positive_duration / Decimal(2)


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


def _counter_coverage(samples: Sequence[_Sample], window: MetricWindow | None) -> float:
    if window is None:
        return 1.0 if len(samples) >= 2 else 0.0
    if len(samples) < 2:
        return 0.0
    seconds = Decimal(str((samples[-1].observed_at - window.start).total_seconds()))
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
    ordered = sorted(
        samples,
        key=lambda item: (
            item.observed_at,
            item.source_order_key,
            str(item.event_id),
        ),
    )
    return _unique_uuid_ids(item.event_id for item in ordered)


def _observation_ids(events: Sequence[L2Observation]) -> tuple[UUID, ...]:
    ordered = sorted(
        (
            event
            for event in events
            if isinstance(event, L2Observation) and isinstance(event.event_id, UUID)
        ),
        key=_observation_order_key,
    )
    return _unique_uuid_ids(item.event_id for item in ordered)


def _unique_uuid_ids(values: Iterable[object]) -> tuple[UUID, ...]:
    ordered: list[UUID] = []
    seen: set[UUID] = set()
    for value in values:
        if isinstance(value, UUID) and value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


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
    "MetricProjectionError",
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
