from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.services.business_metric_contracts import MetricLifecycle
from app.services.data_trunk_contracts import (
    L2Observation,
    TrunkQuality,
    TypedValue,
)
from app.services.metric_projection import (
    CounterContract,
    MetricProjectionState,
    MetricWindow,
    aligned_daily_window,
    counter_delta,
    integrate_power,
    project_metric,
    time_weighted_average,
    window_maximum,
)
from app.services.solution_business_metrics import (
    MetricSourceResolution,
    ResolvedMetricSource,
    compile_business_metric,
    parse_business_metric_asset,
)


class MetricProjectionTest(unittest.TestCase):
    SOURCE_ID = UUID("00000000-0000-0000-0000-000000000101")
    REVISION_ID = UUID("00000000-0000-0000-0000-000000000201")
    BASE = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)

    def event(
        self,
        minute: int,
        value,
        *,
        event_number: int,
        quality: TrunkQuality = TrunkQuality.GOOD,
        second: int = 0,
        unit: str | None = "kW",
        source_order_key: str | None = None,
    ) -> L2Observation:
        event_id = UUID(int=event_number)
        observed_at = self.BASE + timedelta(minutes=minute, seconds=second)
        return L2Observation(
            event_id=event_id,
            entity_instance_id=self.SOURCE_ID,
            definition_id="site.active_power",
            value=TypedValue.float(value),
            unit=unit,
            quality=quality,
            reason=None,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
            calculated_at=observed_at + timedelta(seconds=1),
            processing_revision_id=self.REVISION_ID,
            site_configuration_version=7,
            source_observation_ids=(),
            source_digest=f"{event_number:064x}",
            source_order_key=source_order_key or f"S:{event_number:020d}",
        )

    @staticmethod
    def window(minutes: int = 10) -> MetricWindow:
        start = MetricProjectionTest.BASE
        return MetricWindow(start, start + timedelta(minutes=minutes), True, True)

    def compiled(
        self,
        *,
        method: str = "average",
        good_coverage: float = 0.9,
        minimum_coverage: float = 0.5,
        flow_direction: str = "both",
        source_unit: str | None = None,
        output_unit: str | None = None,
        window_minutes: int = 15,
    ):
        source_unit = source_unit or ("kWh" if method == "counter_delta" else "kW")
        output_unit = output_unit or (
            "kW" if method in {"average", "maximum"} else "kWh"
        )
        raw = {
            "schemaVersion": "zizu.business-metric/v1alpha1",
            "id": f"test.{method}",
            "revision": 1,
            "displayName": method,
            "targetNodeType": "SITE",
            "output": {
                "entityDefinition": f"site.{method}",
                "dataType": "FLOAT",
                "unit": output_unit,
                "temporalSemantics": "windowed",
            },
            "window": {"kind": "rolling", "duration": f"{window_minutes}m"},
            "sources": [
                {
                    "method": method,
                    "entityDefinition": "site.active_power",
                    "priority": 1,
                }
            ],
            "quality": {
                "goodCoverage": good_coverage,
                "minimumUsableCoverage": minimum_coverage,
            },
            "allowedLateness": "1m",
            "correction": {"automaticHorizon": "6h"},
            "flow": {"direction": flow_direction, "normalize": True},
            "capabilities": {"controlEligible": False},
        }
        template = parse_business_metric_asset(raw)
        return compile_business_metric(
            template,
            MetricSourceResolution(
                timezone="Asia/Shanghai",
                sources=(
                    ResolvedMetricSource(
                        entity_instance_id=self.SOURCE_ID,
                        entity_definition_id="site.active_power",
                        method=method,
                        data_type="FLOAT",
                        unit=source_unit,
                        estimated=method == "power_integral",
                    ),
                ),
            ),
        )

    def state(self, **kwargs) -> MetricProjectionState:
        return MetricProjectionState(
            revision=self.compiled(**kwargs),
            maximum_sample_gap_seconds=10 * 60,
        )

    def test_trapezoid_integral_uses_adjacent_samples(self) -> None:
        result = integrate_power(
            (self.event(0, 0, event_number=1), self.event(10, 36, event_number=2)),
            self.window(),
            maximum_sample_gap_seconds=10 * 60,
            direction="positive",
        )

        self.assertEqual(result.value, Decimal("3"))
        self.assertEqual(result.coverage, 1.0)

    def test_gap_larger_than_frozen_limit_is_not_filled_with_zero(self) -> None:
        result = integrate_power(
            (
                self.event(0, 36, event_number=1),
                self.event(10, 36, event_number=2, second=1),
            ),
            MetricWindow(self.BASE, self.BASE + timedelta(minutes=10, seconds=1), True, True),
            maximum_sample_gap_seconds=10 * 60,
            direction="positive",
        )

        self.assertIsNone(result.value)
        self.assertEqual(result.coverage, 0.0)
        self.assertFalse(result.valid)
        self.assertEqual(result.quality, TrunkQuality.BAD)

    def test_direction_contract_zeroes_opposite_flow_and_never_returns_negative_energy(self) -> None:
        events = (self.event(0, -10, event_number=1), self.event(60, -10, event_number=2))
        hour = self.window(60)

        positive = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="positive",
        )
        negative = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="negative",
        )
        both = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="both",
        )

        self.assertEqual(positive.value, Decimal("0"))
        self.assertEqual(negative.value, Decimal("10"))
        self.assertEqual(both.value, Decimal("10"))
        self.assertGreaterEqual(negative.value, 0)

    def test_cross_zero_interval_splits_positive_negative_and_absolute_areas(self) -> None:
        events = (self.event(0, 10, event_number=1), self.event(60, -10, event_number=2))
        hour = self.window(60)

        positive = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="positive",
        )
        negative = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="negative",
        )
        both = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="both",
        )

        self.assertEqual(positive.value, Decimal("2.5"))
        self.assertEqual(negative.value, Decimal("2.5"))
        self.assertEqual(both.value, Decimal("5"))
        self.assertEqual((positive.coverage, negative.coverage, both.coverage), (1.0, 1.0, 1.0))

    def test_cross_zero_interval_is_symmetric_from_negative_to_positive(self) -> None:
        events = (self.event(0, -10, event_number=1), self.event(60, 10, event_number=2))
        hour = self.window(60)

        positive = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="positive",
        )
        negative = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="negative",
        )
        both = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="both",
        )

        self.assertEqual(positive.value, Decimal("2.5"))
        self.assertEqual(negative.value, Decimal("2.5"))
        self.assertEqual(both.value, Decimal("5"))

    def test_interval_touching_zero_has_one_triangle_and_full_coverage(self) -> None:
        events = (self.event(0, 0, event_number=1), self.event(60, 10, event_number=2))
        hour = self.window(60)

        positive = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="positive",
        )
        negative = integrate_power(
            events,
            hour,
            maximum_sample_gap_seconds=60 * 60,
            direction="negative",
        )

        self.assertEqual(positive.value, Decimal("5"))
        self.assertEqual(negative.value, Decimal("0"))
        self.assertEqual((positive.coverage, negative.coverage), (1.0, 1.0))

    def test_power_integral_converts_frozen_watts_to_kwh(self) -> None:
        result = integrate_power(
            (self.event(0, 1000, event_number=1, unit="W"), self.event(60, 1000, event_number=2, unit="W")),
            self.window(60),
            maximum_sample_gap_seconds=60 * 60,
            direction="positive",
            source_unit="W",
            output_unit="kWh",
        )

        self.assertEqual(result.value, Decimal("1"))

    def test_power_integral_converts_frozen_mw_to_mwh(self) -> None:
        result = integrate_power(
            (
                self.event(0, 1, event_number=1, unit="MW"),
                self.event(60, 1, event_number=2, unit="MW"),
            ),
            self.window(60),
            maximum_sample_gap_seconds=60 * 60,
            direction="positive",
            source_unit="MW",
            output_unit="MWh",
        )

        self.assertEqual(result.value, Decimal("1"))

    def test_project_uses_frozen_power_and_output_units(self) -> None:
        state = MetricProjectionState(
            revision=self.compiled(
                method="power_integral",
                source_unit="W",
                output_unit="kWh",
                good_coverage=0.8,
                minimum_coverage=0.5,
            ),
            maximum_sample_gap_seconds=15 * 60,
        )

        decision = project_metric(
            state,
            (
                self.event(1, 900, second=40, event_number=1, unit="W"),
                self.event(15, 900, event_number=2, unit="W"),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.value, Decimal("0.2"))
        self.assertEqual(decision.quality, TrunkQuality.GOOD)

    def test_project_rejects_compatible_but_non_frozen_event_unit(self) -> None:
        decision = project_metric(
            self.state(
                source_unit="kW",
                output_unit="kW",
                good_coverage=0.5,
                minimum_coverage=0.2,
            ),
            (
                self.event(5, 1000, event_number=1, unit="W"),
                self.event(15, 1000, event_number=2, unit="W"),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "UNIT_MISMATCH")

    def test_project_rejects_missing_event_unit(self) -> None:
        decision = project_metric(
            self.state(good_coverage=0, minimum_coverage=0),
            (
                self.event(5, 10, event_number=1, unit=None),
                self.event(15, 10, event_number=2, unit=None),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "UNIT_MISMATCH")

    def test_l2_event_identity_is_validated_before_its_unit(self) -> None:
        malformed = replace(
            self.event(15, 10, event_number=1),
            event_id=None,
            unit=None,
        )

        decision = project_metric(
            self.state(method="maximum", good_coverage=0, minimum_coverage=0),
            (malformed,),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "EVENT_ID_INVALID")

    def test_l2_event_id_must_be_uuid_even_when_unit_matches(self) -> None:
        malformed = replace(self.event(15, 10, event_number=1), event_id=None)

        decision = project_metric(
            self.state(method="maximum", good_coverage=0, minimum_coverage=0),
            (malformed,),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "EVENT_ID_INVALID")

    def test_bad_l2_sample_still_must_carry_the_exact_frozen_unit(self) -> None:
        for event_number, unit in ((2, None), (3, "W")):
            with self.subTest(unit=unit):
                decision = project_metric(
                    self.state(method="maximum", good_coverage=0, minimum_coverage=0),
                    (
                        self.event(15, 18, event_number=1),
                        self.event(
                            14,
                            99,
                            event_number=event_number,
                            quality=TrunkQuality.BAD,
                            unit=unit,
                        ),
                    ),
                    self.BASE + timedelta(minutes=15),
                )

                self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
                self.assertEqual(decision.reason, "UNIT_MISMATCH")

    def test_bad_counter_baseline_validates_identity_and_unit_before_quality(self) -> None:
        endpoint = self.event(15, 1300, event_number=3, unit="Wh")
        cases = (
            (
                self.event(
                    -1,
                    1000,
                    event_number=1,
                    quality=TrunkQuality.BAD,
                    unit="kWh",
                ),
                "UNIT_MISMATCH",
            ),
            (
                replace(
                    self.event(
                        -1,
                        1000,
                        event_number=2,
                        quality=TrunkQuality.BAD,
                        unit="kWh",
                    ),
                    event_id=None,
                ),
                "EVENT_ID_INVALID",
            ),
        )

        for baseline, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                decision = project_metric(
                    self.counter_state(),
                    (baseline, endpoint),
                    self.BASE + timedelta(minutes=15),
                )

                self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
                self.assertEqual(decision.reason, expected_reason)

    def test_maximum_converts_frozen_watts_to_output_kw(self) -> None:
        decision = project_metric(
            self.state(
                method="maximum",
                source_unit="W",
                output_unit="kW",
                good_coverage=0,
                minimum_coverage=0,
            ),
            (self.event(15, 18000, event_number=1, unit="W"),),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.value, Decimal("18"))

    def test_incompatible_event_unit_fails_closed(self) -> None:
        decision = project_metric(
            self.state(good_coverage=0, minimum_coverage=0),
            (
                self.event(5, 10, event_number=1, unit="A"),
                self.event(15, 10, event_number=2, unit="A"),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "UNIT_MISMATCH")

    def test_incompatible_frozen_output_unit_fails_closed(self) -> None:
        decision = project_metric(
            self.state(
                source_unit="kW",
                output_unit="kWh",
                good_coverage=0,
                minimum_coverage=0,
            ),
            (
                self.event(5, 10, event_number=1),
                self.event(15, 10, event_number=2),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "UNIT_CONTRACT_INVALID")

    def test_public_helpers_reject_good_samples_with_wrong_unit_families(self) -> None:
        power_as_energy = (
            self.event(0, 10, event_number=11, unit="Wh"),
            self.event(10, 20, event_number=12, unit="Wh"),
        )
        counter_as_power = (
            self.event(0, 10, event_number=13, unit="kW"),
            self.event(10, 20, event_number=14, unit="kW"),
        )
        cases = (
            (
                "counter",
                counter_delta(
                    counter_as_power,
                    CounterContract(maximum=999999),
                    source_unit="kW",
                    output_unit="kW",
                ),
                (counter_as_power[0].event_id, counter_as_power[1].event_id),
            ),
            (
                "integral",
                integrate_power(
                    power_as_energy,
                    self.window(),
                    maximum_sample_gap_seconds=10 * 60,
                    direction="positive",
                    source_unit="Wh",
                    output_unit="Wh",
                ),
                (power_as_energy[0].event_id, power_as_energy[1].event_id),
            ),
            (
                "average",
                time_weighted_average(
                    power_as_energy,
                    self.window(),
                    maximum_sample_gap_seconds=10 * 60,
                    source_unit="Wh",
                    output_unit="Wh",
                ),
                (power_as_energy[0].event_id, power_as_energy[1].event_id),
            ),
            (
                "maximum",
                window_maximum(
                    power_as_energy,
                    self.window(),
                    source_unit="Wh",
                    output_unit="Wh",
                ),
                (power_as_energy[0].event_id, power_as_energy[1].event_id),
            ),
        )

        for helper, result, expected_ids in cases:
            with self.subTest(helper=helper):
                self.assertFalse(result.valid)
                self.assertIsNone(result.value)
                self.assertIsNone(result.peak_at)
                self.assertIsNone(result.peak_event_id)
                self.assertEqual(result.reason, "UNIT_CONTRACT_INVALID")
                self.assertEqual(result.source_event_ids, expected_ids)

    def test_public_helpers_reject_bad_only_samples_with_wrong_unit_families(self) -> None:
        def bad_empty(event: L2Observation) -> L2Observation:
            return replace(
                event,
                quality=TrunkQuality.BAD,
                value=TypedValue.float(None),
            )

        power_as_energy = (
            bad_empty(self.event(0, 10, event_number=21, unit="Wh")),
            bad_empty(self.event(10, 20, event_number=22, unit="Wh")),
        )
        counter_as_power = (
            bad_empty(self.event(0, 10, event_number=23, unit="kW")),
            bad_empty(self.event(10, 20, event_number=24, unit="kW")),
        )
        cases = (
            (
                "counter",
                counter_delta(
                    counter_as_power,
                    CounterContract(maximum=999999),
                    source_unit="kW",
                    output_unit="kW",
                ),
                (counter_as_power[0].event_id, counter_as_power[1].event_id),
            ),
            (
                "integral",
                integrate_power(
                    power_as_energy,
                    self.window(),
                    maximum_sample_gap_seconds=10 * 60,
                    direction="positive",
                    source_unit="Wh",
                    output_unit="Wh",
                ),
                (power_as_energy[0].event_id, power_as_energy[1].event_id),
            ),
            (
                "average",
                time_weighted_average(
                    power_as_energy,
                    self.window(),
                    maximum_sample_gap_seconds=10 * 60,
                    source_unit="Wh",
                    output_unit="Wh",
                ),
                (power_as_energy[0].event_id, power_as_energy[1].event_id),
            ),
            (
                "maximum",
                window_maximum(
                    power_as_energy,
                    self.window(),
                    source_unit="Wh",
                    output_unit="Wh",
                ),
                (power_as_energy[0].event_id, power_as_energy[1].event_id),
            ),
        )

        for helper, result, expected_ids in cases:
            with self.subTest(helper=helper):
                self.assertFalse(result.valid)
                self.assertIsNone(result.value)
                self.assertIsNone(result.peak_at)
                self.assertIsNone(result.peak_event_id)
                self.assertEqual(result.reason, "UNIT_CONTRACT_INVALID")
                self.assertEqual(result.source_event_ids, expected_ids)

    def test_missing_frozen_units_fail_closed(self) -> None:
        revision = self.compiled()
        output = revision.point_processing_asset.outputs[0]
        revision = replace(
            revision,
            sources=(replace(revision.sources[0], unit=None),),
            point_processing_asset=replace(
                revision.point_processing_asset,
                outputs=(replace(output, unit=None),),
            ),
        )

        decision = project_metric(
            MetricProjectionState(
                revision=revision,
                maximum_sample_gap_seconds=15 * 60,
            ),
            (
                self.event(5, 10, event_number=1, unit=None),
                self.event(15, 10, event_number=2, unit=None),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "UNIT_CONTRACT_INVALID")

    def test_average_is_time_weighted_instead_of_sample_weighted(self) -> None:
        result = time_weighted_average(
            (
                self.event(0, 0, event_number=1),
                self.event(1, 10, event_number=2),
                self.event(10, 10, event_number=3),
            ),
            self.window(),
            maximum_sample_gap_seconds=10 * 60,
            source_unit="kW",
            output_unit="kW",
        )

        self.assertEqual(result.value, Decimal("9.5"))
        self.assertEqual(result.coverage, 1.0)

    def test_peak_excludes_bad_samples_and_keeps_source_identity(self) -> None:
        winner = self.event(5, 18, event_number=2)
        result = window_maximum(
            (
                self.event(1, 10, event_number=1),
                winner,
                self.event(6, 99, event_number=3, quality=TrunkQuality.BAD),
            ),
            self.window(),
            source_unit="kW",
            output_unit="kW",
        )

        self.assertEqual(result.value, Decimal("18"))
        self.assertEqual(result.peak_at, winner.observed_at)
        self.assertEqual(result.peak_event_id, winner.event_id)

    def test_peak_project_ignores_bad_larger_sample(self) -> None:
        winner = self.event(5, 18, event_number=1)
        bad = self.event(10, 99, event_number=2, quality=TrunkQuality.BAD)

        decision = project_metric(
            self.state(
                method="maximum",
                good_coverage=0,
                minimum_coverage=0,
            ),
            (bad, winner),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.value, Decimal("18"))
        self.assertEqual(decision.quality, TrunkQuality.GOOD)
        self.assertEqual(decision.peak_at, winner.observed_at)
        self.assertEqual(decision.peak_event_id, winner.event_id)

    def test_peak_project_is_invalid_when_all_samples_are_bad(self) -> None:
        decision = project_metric(
            self.state(
                method="maximum",
                good_coverage=0,
                minimum_coverage=0,
            ),
            (
                self.event(5, True, event_number=1, quality=TrunkQuality.BAD),
                self.event(10, 99, event_number=2, quality=TrunkQuality.STALE),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.quality, TrunkQuality.BAD)
        self.assertEqual(decision.reason, "COVERAGE_INSUFFICIENT")

    def test_peak_tie_uses_full_stable_event_order(self) -> None:
        later_key = self.event(5, 18, event_number=2, source_order_key="S:B")
        earlier_key = self.event(5, 18, event_number=3, source_order_key="S:A")

        result = window_maximum(
            (later_key, earlier_key),
            self.window(),
            source_unit="kW",
            output_unit="kW",
        )

        self.assertEqual(result.peak_event_id, earlier_key.event_id)

    def test_peak_ignores_nonfinite_bad_sample(self) -> None:
        winner = self.event(5, 18, event_number=1)
        bad_nan = self.event(10, float("nan"), event_number=2, quality=TrunkQuality.BAD)

        result = window_maximum(
            (bad_nan, winner),
            self.window(),
            source_unit="kW",
            output_unit="kW",
        )

        self.assertEqual(result.value, Decimal("18"))
        self.assertEqual(result.peak_event_id, winner.event_id)

    def test_project_sorts_out_of_order_events_and_deduplicates_event_ids(self) -> None:
        first = self.event(1, 10, event_number=1)
        second = self.event(8, 20, event_number=2)
        now = self.BASE + timedelta(minutes=15)

        ordered = project_metric(self.state(), (first, second), now)
        shuffled = project_metric(self.state(), (second, first, second), now)

        self.assertEqual(shuffled.value, ordered.value)
        self.assertEqual(shuffled.coverage, ordered.coverage)
        self.assertEqual(shuffled.source_event_ids, ordered.source_event_ids)

    def test_conflicting_duplicate_event_id_fails_closed(self) -> None:
        first = self.event(5, 10, event_number=1)
        conflict = replace(first, value=TypedValue.float(11))

        decision = project_metric(
            self.state(good_coverage=0, minimum_coverage=0),
            (first, conflict),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "DUPLICATE_EVENT_CONFLICT")

    def test_non_counter_window_isolated_before_identity_and_duplicate_validation(self) -> None:
        first = self.event(5, 10, event_number=31)
        endpoint = self.event(15, 20, event_number=32)
        outside_invalid_id = replace(
            self.event(-1, 99, event_number=33),
            event_id=None,
        )
        outside_conflict = replace(
            first,
            observed_at=self.BASE - timedelta(minutes=1),
            value=TypedValue.float(999),
        )

        for pollutant in (outside_invalid_id, outside_conflict):
            with self.subTest(pollutant=pollutant):
                state = replace(
                    self.state(good_coverage=0.5, minimum_coverage=0.2),
                    events=(pollutant,),
                )
                decision = project_metric(
                    state,
                    (endpoint, first),
                    self.BASE + timedelta(minutes=15),
                )

                self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
                self.assertEqual(decision.value, Decimal("15"))
                self.assertEqual(
                    decision.source_event_ids,
                    (first.event_id, endpoint.event_id),
                )

    def test_invalid_project_evidence_is_stable_when_input_order_changes(self) -> None:
        earlier = self.event(5, 10, event_number=41)
        invalid = replace(self.event(7, 15, event_number=42), event_id=None)
        later = self.event(10, 20, event_number=43)
        state = self.state(method="maximum", good_coverage=0, minimum_coverage=0)

        forward = project_metric(
            state,
            (later, invalid, earlier),
            self.BASE + timedelta(minutes=15),
        )
        reversed_input = project_metric(
            state,
            (earlier, invalid, later),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(forward.reason, "EVENT_ID_INVALID")
        self.assertEqual(reversed_input.reason, "EVENT_ID_INVALID")
        self.assertEqual(
            forward.source_event_ids,
            (earlier.event_id, later.event_id),
        )
        self.assertEqual(reversed_input.source_event_ids, forward.source_event_ids)

    def test_two_none_event_ids_fail_as_invalid_identity_before_deduplication(self) -> None:
        first = replace(self.event(5, 10, event_number=1), event_id=None)
        second = replace(self.event(10, 20, event_number=2), event_id=None)

        decision = project_metric(
            self.state(good_coverage=0, minimum_coverage=0),
            (first, second),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "EVENT_ID_INVALID")

    def test_unhashable_event_ids_fail_as_invalid_identity_without_type_error(self) -> None:
        for invalid_id in (["not-a-uuid"], {"not": "a-uuid"}):
            with self.subTest(invalid_id=invalid_id):
                malformed = replace(
                    self.event(15, 10, event_number=1),
                    event_id=invalid_id,
                )

                decision = project_metric(
                    self.state(method="maximum", good_coverage=0, minimum_coverage=0),
                    (malformed,),
                    self.BASE + timedelta(minutes=15),
                )

                self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
                self.assertEqual(decision.reason, "EVENT_ID_INVALID")

    def test_l2_helpers_require_an_explicit_unit_contract(self) -> None:
        power_events = (
            self.event(0, 10, event_number=1),
            self.event(10, 20, event_number=2),
        )
        counter_events = (
            self.event(0, 1000, event_number=3, unit="Wh"),
            self.event(10, 1300, event_number=4, unit="Wh"),
        )

        results = (
            window_maximum(power_events, self.window()),
            time_weighted_average(
                power_events,
                self.window(),
                maximum_sample_gap_seconds=10 * 60,
            ),
            counter_delta(counter_events, CounterContract(maximum=999999)),
        )

        for result in results:
            with self.subTest(result=result):
                self.assertFalse(result.valid)
                self.assertEqual(result.reason, "UNIT_CONTRACT_INVALID")

    def test_l2_helper_validates_event_identity_before_missing_unit_contract(self) -> None:
        malformed = replace(
            self.event(5, 10, event_number=1, unit=None),
            event_id=None,
        )

        result = window_maximum((malformed,), self.window())

        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "EVENT_ID_INVALID")

    def test_all_l2_helpers_validate_event_identity_before_event_unit(self) -> None:
        malformed_power = replace(
            self.event(0, 10, event_number=1, quality=TrunkQuality.BAD, unit=None),
            event_id=None,
        )
        power_endpoint = self.event(10, 20, event_number=2)
        malformed_counter = replace(
            self.event(-1, 1000, event_number=3, quality=TrunkQuality.BAD, unit=None),
            event_id=None,
        )
        counter_endpoint = self.event(10, 1300, event_number=4, unit="Wh")
        cases = (
            (
                "counter",
                lambda: counter_delta(
                    (malformed_counter, counter_endpoint),
                    CounterContract(maximum=999999),
                    self.window(),
                    source_unit="Wh",
                    output_unit="kWh",
                ),
            ),
            (
                "integral",
                lambda: integrate_power(
                    (malformed_power, power_endpoint),
                    self.window(),
                    maximum_sample_gap_seconds=10 * 60,
                    direction="positive",
                ),
            ),
            (
                "average",
                lambda: time_weighted_average(
                    (malformed_power, power_endpoint),
                    self.window(),
                    maximum_sample_gap_seconds=10 * 60,
                    source_unit="kW",
                    output_unit="kW",
                ),
            ),
            (
                "maximum",
                lambda: window_maximum(
                    (malformed_power, power_endpoint),
                    self.window(),
                    source_unit="kW",
                    output_unit="kW",
                ),
            ),
        )

        for name, calculate in cases:
            with self.subTest(helper=name):
                result = calculate()
                self.assertFalse(result.valid)
                self.assertEqual(result.reason, "EVENT_ID_INVALID")

    def test_integral_l2_unit_contract_cannot_be_disabled_with_none(self) -> None:
        result = integrate_power(
            (
                self.event(0, 10, event_number=1),
                self.event(10, 20, event_number=2),
            ),
            self.window(),
            maximum_sample_gap_seconds=10 * 60,
            direction="positive",
            source_unit=None,
            output_unit=None,
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "UNIT_CONTRACT_INVALID")

    def test_l2_helper_rejects_empty_frozen_units_before_quality_filtering(self) -> None:
        bad_empty_unit = self.event(
            5,
            10,
            event_number=1,
            quality=TrunkQuality.BAD,
            unit="",
        )
        good = self.event(5, 10, event_number=2)
        cases = (
            window_maximum(
                (bad_empty_unit,),
                self.window(),
                source_unit="",
                output_unit="kW",
            ),
            window_maximum(
                (good,),
                self.window(),
                source_unit="kW",
                output_unit="",
            ),
        )

        for result in cases:
            with self.subTest(result=result):
                self.assertFalse(result.valid)
                self.assertEqual(result.reason, "UNIT_CONTRACT_INVALID")

    def test_public_helpers_reject_mixed_l2_and_number_inputs_without_evidence(self) -> None:
        mixed_power = (self.event(0, 10, event_number=51), 20)
        mixed_counter = (
            self.event(0, 1000, event_number=52, unit="Wh"),
            1300,
        )
        cases = (
            counter_delta(
                mixed_counter,
                CounterContract(maximum=999999),
                source_unit="Wh",
                output_unit="kWh",
            ),
            integrate_power(
                mixed_power,
                self.window(),
                maximum_sample_gap_seconds=10 * 60,
                direction="positive",
            ),
            time_weighted_average(
                mixed_power,
                self.window(),
                maximum_sample_gap_seconds=10 * 60,
                source_unit="kW",
                output_unit="kW",
            ),
            window_maximum(
                mixed_power,
                self.window(),
                source_unit="kW",
                output_unit="kW",
            ),
        )

        for result in cases:
            with self.subTest(result=result):
                self.assertFalse(result.valid)
                self.assertEqual(result.reason, "INPUT_KIND_MIXED")
                self.assertIsNone(result.value)
                self.assertIsNone(result.peak_at)
                self.assertIsNone(result.peak_event_id)
                self.assertEqual(result.source_event_ids, ())

    def test_out_of_window_l2_helpers_still_require_frozen_unit_contract(self) -> None:
        outside_power = self.event(-1, 10, event_number=53)
        outside_counter = self.event(-11, 1000, event_number=54, unit="Wh")
        cases = (
            counter_delta(
                (outside_counter,),
                CounterContract(maximum=999999),
                self.window(),
            ),
            integrate_power(
                (outside_power,),
                self.window(),
                maximum_sample_gap_seconds=10 * 60,
                direction="positive",
                source_unit=None,
                output_unit=None,
            ),
            time_weighted_average(
                (outside_power,),
                self.window(),
                maximum_sample_gap_seconds=10 * 60,
            ),
            window_maximum((outside_power,), self.window()),
        )

        for result in cases:
            with self.subTest(result=result):
                self.assertFalse(result.valid)
                self.assertEqual(result.reason, "UNIT_CONTRACT_INVALID")
                self.assertEqual(result.source_event_ids, ())

    def test_unitless_helpers_remain_available_for_pure_numbers(self) -> None:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        numeric_window = MetricWindow(epoch, epoch + timedelta(seconds=2), True, True)

        average = time_weighted_average(
            (0, 10, 10),
            numeric_window,
            maximum_sample_gap_seconds=1,
        )
        integral = integrate_power(
            (3600, 3600),
            MetricWindow(epoch, epoch + timedelta(seconds=1), True, True),
            maximum_sample_gap_seconds=1,
            direction="positive",
            source_unit=None,
            output_unit=None,
        )
        maximum = window_maximum((1, 3, 2), numeric_window)
        delta = counter_delta((1, 3), CounterContract(maximum=999999))

        self.assertEqual(average.value, Decimal("7.5"))
        self.assertEqual(integral.value, Decimal("1"))
        self.assertEqual(maximum.value, Decimal("3"))
        self.assertEqual(delta.value, Decimal("2"))

    def test_rolling_start_event_is_excluded_and_end_event_is_included(self) -> None:
        start = self.event(0, 999, event_number=1)
        middle = self.event(5, 10, event_number=2)
        end = self.event(15, 10, event_number=3)

        decision = project_metric(
            self.state(good_coverage=0.5, minimum_coverage=0.2),
            (start, middle, end),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.value, Decimal("10"))
        self.assertEqual(decision.coverage, 10 * 60 / (15 * 60))

    def test_current_window_is_projection_not_history_fact(self) -> None:
        decision = project_metric(
            self.state(good_coverage=0.5, minimum_coverage=0.2),
            (self.event(5, 10, event_number=1), self.event(15, 10, event_number=2)),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
        self.assertEqual(decision.history_facts, ())
        self.assertEqual(decision.quality, TrunkQuality.GOOD)

    def test_usable_but_incomplete_coverage_is_uncertain(self) -> None:
        decision = project_metric(
            self.state(good_coverage=0.9, minimum_coverage=0.2),
            (self.event(5, 10, event_number=1), self.event(10, 10, event_number=2)),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
        self.assertEqual(decision.quality, TrunkQuality.UNCERTAIN)

    def test_coverage_thresholds_are_inclusive(self) -> None:
        events = (
            self.event(7, 10, second=30, event_number=1),
            self.event(15, 10, event_number=2),
        )

        at_minimum = project_metric(
            self.state(good_coverage=0.9, minimum_coverage=0.5),
            events,
            self.BASE + timedelta(minutes=15),
        )
        at_good = project_metric(
            self.state(good_coverage=0.5, minimum_coverage=0.2),
            events,
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(at_minimum.quality, TrunkQuality.UNCERTAIN)
        self.assertEqual(at_good.quality, TrunkQuality.GOOD)

    def test_insufficient_coverage_is_invalid_bad_without_history(self) -> None:
        decision = project_metric(
            self.state(good_coverage=0.9, minimum_coverage=0.5),
            (self.event(10, 10, event_number=1), self.event(11, 10, event_number=2)),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.quality, TrunkQuality.BAD)
        self.assertEqual(decision.reason, "COVERAGE_INSUFFICIENT")
        self.assertEqual(decision.history_facts, ())

    def test_bad_average_sample_breaks_adjacent_intervals_without_bridging_gap(self) -> None:
        decision = project_metric(
            self.state(good_coverage=0.5, minimum_coverage=0.4),
            (
                self.event(1, 10, event_number=1),
                self.event(4, 10, event_number=2),
                self.event(5, True, event_number=3, quality=TrunkQuality.BAD),
                self.event(10, 10, event_number=4),
                self.event(15, 10, event_number=5),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
        self.assertEqual(decision.value, Decimal("10"))
        self.assertEqual(decision.coverage, 8 * 60 / (15 * 60))
        self.assertEqual(decision.quality, TrunkQuality.GOOD)

    def test_ambiguous_counter_is_an_invalid_ledger_decision(self) -> None:
        revision = self.compiled(method="counter_delta")
        state = MetricProjectionState(
            revision=revision,
            counter_contract=CounterContract(maximum=999999),
            maximum_sample_gap_seconds=15 * 60,
        )
        first = self.event(0, 100, event_number=1, unit="kWh")
        second = self.event(14, 5, event_number=2, unit="kWh")

        decision = project_metric(
            state,
            (first, second),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.quality, TrunkQuality.BAD)
        self.assertIsNone(decision.value)
        self.assertEqual(decision.reason, "COUNTER_DISCONTINUITY_AMBIGUOUS")
        self.assertEqual(decision.source_event_ids, (first.event_id, second.event_id))
        self.assertEqual(decision.history_facts, ())

    def counter_state(
        self,
        *,
        maximum_sample_gap_seconds: int = 2 * 60,
    ) -> MetricProjectionState:
        return MetricProjectionState(
            revision=self.compiled(
                method="counter_delta",
                source_unit="Wh",
                output_unit="kWh",
                good_coverage=0.9,
                minimum_coverage=0.5,
            ),
            counter_contract=CounterContract(maximum=999999),
            maximum_sample_gap_seconds=maximum_sample_gap_seconds,
        )

    def test_counter_uses_exact_window_start_as_boundary_baseline(self) -> None:
        baseline = self.event(0, 1000, event_number=1, unit="Wh")
        endpoint = self.event(15, 1300, event_number=2, unit="Wh")

        decision = project_metric(
            self.counter_state(),
            (endpoint, baseline),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.value, Decimal("0.3"))
        self.assertEqual(decision.quality, TrunkQuality.GOOD)
        self.assertEqual(decision.source_event_ids, (baseline.event_id, endpoint.event_id))

    def test_counter_uses_one_recent_before_start_baseline_idempotently(self) -> None:
        baseline = self.event(-1, 1000, event_number=1, unit="Wh")
        endpoint = self.event(15, 1300, event_number=2, unit="Wh")

        ordered = project_metric(
            self.counter_state(),
            (baseline, endpoint),
            self.BASE + timedelta(minutes=15),
        )
        replayed = project_metric(
            self.counter_state(),
            (endpoint, baseline, endpoint),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(ordered.value, Decimal("0.3"))
        self.assertEqual(replayed.value, ordered.value)
        self.assertEqual(replayed.source_event_ids, ordered.source_event_ids)

    def test_counter_scope_isolated_before_identity_and_duplicate_validation(self) -> None:
        baseline = self.event(-1, 1000, event_number=61, unit="Wh")
        endpoint = self.event(15, 1300, event_number=62, unit="Wh")
        outside_invalid_id = replace(
            self.event(-16, 999, event_number=63, unit="Wh"),
            event_id=None,
        )
        outside_conflict = replace(
            endpoint,
            observed_at=self.BASE - timedelta(minutes=16),
            value=TypedValue.float(999999),
        )

        for pollutant in (outside_invalid_id, outside_conflict):
            with self.subTest(pollutant=pollutant):
                state = replace(self.counter_state(), events=(pollutant,))
                decision = project_metric(
                    state,
                    (endpoint, baseline),
                    self.BASE + timedelta(minutes=15),
                )

                self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
                self.assertEqual(decision.value, Decimal("0.3"))
                self.assertEqual(
                    decision.source_event_ids,
                    (baseline.event_id, endpoint.event_id),
                )

    def test_counter_validates_endpoint_unit_before_baseline_selection(self) -> None:
        endpoint = self.event(15, 1300, event_number=71, unit="kWh")
        bad_empty_baseline = replace(
            self.event(
                -1,
                1000,
                event_number=72,
                unit="Wh",
                quality=TrunkQuality.BAD,
            ),
            value=TypedValue.float(None),
        )
        cases = (
            ((endpoint,), (endpoint.event_id,)),
            (
                (bad_empty_baseline, endpoint),
                (bad_empty_baseline.event_id, endpoint.event_id),
            ),
        )

        for events, expected_ids in cases:
            with self.subTest(events=events):
                decision = project_metric(
                    self.counter_state(),
                    events,
                    self.BASE + timedelta(minutes=15),
                )

                self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
                self.assertEqual(decision.reason, "UNIT_MISMATCH")
                self.assertEqual(decision.source_event_ids, expected_ids)

    def test_counter_missing_frozen_unit_preserves_baseline_and_endpoint_evidence(self) -> None:
        baseline = self.event(-1, 1000, event_number=81, unit="Wh")
        endpoint = self.event(15, 1300, event_number=82, unit="Wh")
        base_state = self.counter_state()
        output = base_state.revision.point_processing_asset.outputs[0]
        cases = (
            (
                "source",
                replace(
                    base_state.revision,
                    sources=(replace(base_state.revision.sources[0], unit=None),),
                ),
            ),
            (
                "output",
                replace(
                    base_state.revision,
                    point_processing_asset=replace(
                        base_state.revision.point_processing_asset,
                        outputs=(replace(output, unit=None),),
                    ),
                ),
            ),
        )

        for missing, revision in cases:
            with self.subTest(missing=missing):
                decision = project_metric(
                    replace(base_state, revision=revision),
                    (endpoint, baseline),
                    self.BASE + timedelta(minutes=15),
                )

                self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
                self.assertEqual(decision.reason, "UNIT_CONTRACT_INVALID")
                self.assertEqual(
                    decision.source_event_ids,
                    (baseline.event_id, endpoint.event_id),
                )

    def test_counter_wrong_unit_family_is_invalid_before_bad_baseline_selection(self) -> None:
        baseline = replace(
            self.event(
                -1,
                1000,
                event_number=91,
                unit="kW",
                quality=TrunkQuality.BAD,
            ),
            value=TypedValue.float(None),
        )
        endpoint = self.event(15, 1300, event_number=92, unit="kW")
        revision = self.compiled(
            method="counter_delta",
            source_unit="kW",
            output_unit="kW",
        )
        state = MetricProjectionState(
            revision=revision,
            counter_contract=CounterContract(maximum=999999),
            maximum_sample_gap_seconds=2 * 60,
        )

        decision = project_metric(
            state,
            (endpoint, baseline),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "UNIT_CONTRACT_INVALID")
        self.assertEqual(
            decision.source_event_ids,
            (baseline.event_id, endpoint.event_id),
        )

    def test_counter_accepts_baseline_within_one_window_when_sample_gap_is_smaller(self) -> None:
        decision = project_metric(
            self.counter_state(maximum_sample_gap_seconds=2 * 60),
            (
                self.event(-3, 1000, event_number=1, unit="Wh"),
                self.event(15, 1300, event_number=2, unit="Wh"),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
        self.assertEqual(decision.value, Decimal("0.3"))

    def test_counter_rejects_baseline_older_than_one_window_when_sample_gap_is_larger(self) -> None:
        decision = project_metric(
            self.counter_state(maximum_sample_gap_seconds=60 * 60),
            (
                self.event(-16, 1000, event_number=1, unit="Wh"),
                self.event(15, 1300, event_number=2, unit="Wh"),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "COUNTER_BASELINE_MISSING")

    def test_counter_selects_last_trustworthy_baseline_within_one_window(self) -> None:
        good = self.event(-4, 1000, event_number=1, unit="Wh")
        bad = self.event(-3, 1200, event_number=2, unit="Wh", quality=TrunkQuality.BAD)
        nonfinite = self.event(-2, float("nan"), event_number=3, unit="Wh")
        wrong_type = self.event(-1, True, event_number=4, unit="Wh")
        endpoint = self.event(15, 1300, event_number=5, unit="Wh")

        decision = project_metric(
            self.counter_state(),
            (endpoint, wrong_type, nonfinite, bad, good),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
        self.assertEqual(decision.value, Decimal("0.3"))
        self.assertEqual(decision.source_event_ids, (good.event_id, endpoint.event_id))

    def test_counter_reports_source_bad_when_baseline_range_only_has_bad_samples(self) -> None:
        baseline = self.event(
            -1,
            1000,
            event_number=1,
            unit="Wh",
            quality=TrunkQuality.BAD,
        )
        endpoint = self.event(15, 1300, event_number=2, unit="Wh")
        decision = project_metric(
            self.counter_state(),
            (baseline, endpoint),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "SOURCE_BAD")
        self.assertEqual(decision.source_event_ids, (baseline.event_id, endpoint.event_id))

    def test_bad_counter_baseline_may_have_an_empty_typed_value(self) -> None:
        baseline = replace(
            self.event(-1, 1000, event_number=1, unit="Wh", quality=TrunkQuality.BAD),
            value=TypedValue.float(None),
        )
        endpoint = self.event(15, 1300, event_number=2, unit="Wh")

        decision = project_metric(
            self.counter_state(),
            (endpoint, baseline),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.reason, "SOURCE_BAD")
        self.assertEqual(decision.source_event_ids, (baseline.event_id, endpoint.event_id))

    def test_counter_bool_only_baseline_preserves_value_type_reason(self) -> None:
        baseline = self.event(-1, True, event_number=1, unit="Wh")
        endpoint = self.event(15, 1300, event_number=2, unit="Wh")
        decision = project_metric(
            self.counter_state(),
            (baseline, endpoint),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "VALUE_TYPE_INVALID")
        self.assertEqual(decision.source_event_ids, (baseline.event_id, endpoint.event_id))

    def test_counter_nonfinite_only_baseline_preserves_reason(self) -> None:
        baseline = self.event(-1, float("nan"), event_number=1, unit="Wh")
        endpoint = self.event(15, 1300, event_number=2, unit="Wh")
        decision = project_metric(
            self.counter_state(),
            (baseline, endpoint),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "NONFINITE_VALUE")
        self.assertEqual(decision.source_event_ids, (baseline.event_id, endpoint.event_id))

    def test_counter_wrong_unit_only_baseline_preserves_reason(self) -> None:
        baseline = self.event(-1, 1000, event_number=1, unit="kWh")
        endpoint = self.event(15, 1300, event_number=2, unit="Wh")
        decision = project_metric(
            self.counter_state(),
            (baseline, endpoint),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "UNIT_MISMATCH")
        self.assertEqual(decision.source_event_ids, (baseline.event_id, endpoint.event_id))

    def test_counter_baseline_error_evidence_excludes_candidates_outside_lookback(self) -> None:
        unrelated = self.event(-16, 900, event_number=1, unit="Wh")
        baseline = self.event(-1, True, event_number=2, unit="Wh")
        endpoint = self.event(15, 1300, event_number=3, unit="Wh")

        decision = project_metric(
            self.counter_state(),
            (endpoint, unrelated, baseline),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.reason, "VALUE_TYPE_INVALID")
        self.assertEqual(decision.source_event_ids, (baseline.event_id, endpoint.event_id))

    def test_counter_selection_error_evidence_is_stably_sorted_and_deduplicated(self) -> None:
        earlier_bad = self.event(
            -2,
            900,
            event_number=1,
            unit="Wh",
            quality=TrunkQuality.BAD,
        )
        later_wrong_unit = self.event(-1, 1000, event_number=2, unit="kWh")
        endpoint = self.event(15, 1300, event_number=3, unit="Wh")

        result = counter_delta(
            (
                endpoint,
                later_wrong_unit,
                earlier_bad,
                endpoint,
                later_wrong_unit,
            ),
            CounterContract(maximum=999999),
            self.window(15),
            source_unit="Wh",
            output_unit="kWh",
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "UNIT_MISMATCH")
        self.assertEqual(
            result.source_event_ids,
            (earlier_bad.event_id, later_wrong_unit.event_id, endpoint.event_id),
        )

    def test_counter_mixed_invalid_baselines_use_last_non_quality_reason(self) -> None:
        decision = project_metric(
            self.counter_state(),
            (
                self.event(-3, True, event_number=1, unit="Wh"),
                self.event(-2, float("nan"), event_number=2, unit="Wh"),
                self.event(-1, 1000, event_number=3, unit="kWh"),
                self.event(15, 1300, event_number=4, unit="Wh"),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "UNIT_MISMATCH")

    def test_counter_exact_start_uses_last_trustworthy_candidate(self) -> None:
        trusted = self.event(
            0,
            1000,
            event_number=1,
            unit="Wh",
            source_order_key="S:1",
        )
        later_bad = self.event(
            0,
            True,
            event_number=2,
            unit="Wh",
            source_order_key="S:2",
        )
        endpoint = self.event(15, 1300, event_number=3, unit="Wh")

        decision = project_metric(
            self.counter_state(),
            (later_bad, endpoint, trusted),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
        self.assertEqual(decision.value, Decimal("0.3"))
        self.assertEqual(decision.source_event_ids, (trusted.event_id, endpoint.event_id))

    def test_counter_bool_endpoint_is_invalid_and_keeps_baseline_evidence(self) -> None:
        baseline = self.event(0, 1000, event_number=1, unit="Wh")
        endpoint = self.event(15, True, event_number=2, unit="Wh")

        decision = project_metric(
            self.counter_state(),
            (endpoint, baseline),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "VALUE_TYPE_INVALID")
        self.assertEqual(decision.source_event_ids, (baseline.event_id, endpoint.event_id))

    def test_counter_baseline_lookback_uses_real_dst_window_duration(self) -> None:
        for now, hours in (
            (datetime(2026, 3, 8, 16, tzinfo=UTC), 23),
            (datetime(2026, 11, 1, 16, tzinfo=UTC), 25),
        ):
            with self.subTest(hours=hours):
                window = aligned_daily_window(now, "America/New_York")
                baseline = replace(
                    self.event(0, 1000, event_number=hours, unit="Wh"),
                    observed_at=window.start - timedelta(hours=hours),
                )
                endpoint = replace(
                    self.event(15, 1300, event_number=hours + 100, unit="Wh"),
                    observed_at=window.end - timedelta(seconds=1),
                )

                result = counter_delta(
                    (endpoint, baseline),
                    CounterContract(maximum=999999),
                    window,
                    source_unit="Wh",
                    output_unit="kWh",
                )

                self.assertTrue(result.valid)
                self.assertEqual(result.value, Decimal("0.3"))
                self.assertEqual(
                    result.coverage,
                    (hours * 60 * 60 - 1) / (hours * 60 * 60),
                )

    def test_counter_coverage_starts_at_window_boundary_not_before_window_baseline(self) -> None:
        result = counter_delta(
            (
                self.event(-10, 1000, event_number=1, unit="Wh"),
                self.event(5, 1300, event_number=2, unit="Wh"),
            ),
            CounterContract(maximum=999999),
            self.window(15),
            source_unit="Wh",
            output_unit="kWh",
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.coverage, 5 * 60 / (15 * 60))

    def test_counter_rejects_missing_baseline_or_window_endpoint(self) -> None:
        missing_baseline = project_metric(
            self.counter_state(),
            (self.event(15, 1300, event_number=1, unit="Wh"),),
            self.BASE + timedelta(minutes=15),
        )
        missing_endpoint = project_metric(
            self.counter_state(),
            (self.event(0, 1000, event_number=2, unit="Wh"),),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(missing_baseline.reason, "COUNTER_BASELINE_MISSING")
        self.assertEqual(missing_endpoint.reason, "COUNTER_ENDPOINT_MISSING")
        self.assertEqual(missing_baseline.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(missing_endpoint.lifecycle, MetricLifecycle.INVALID)

    def test_bad_counter_chain_is_invalid_instead_of_skipping_discontinuity(self) -> None:
        decision = project_metric(
            self.counter_state(),
            (
                self.event(0, 1000, event_number=1, unit="Wh"),
                self.event(8, 1100, event_number=2, unit="Wh", quality=TrunkQuality.BAD),
                self.event(15, 1300, event_number=3, unit="Wh"),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "SOURCE_BAD")

    def test_zero_integrable_duration_is_invalid_even_when_threshold_is_zero(self) -> None:
        state = MetricProjectionState(
            revision=self.compiled(
                method="power_integral",
                good_coverage=0,
                minimum_coverage=0,
            ),
            maximum_sample_gap_seconds=15 * 60,
        )

        empty = project_metric(state, (), self.BASE + timedelta(minutes=15))
        one_sample = project_metric(
            state,
            (self.event(15, 10, event_number=1),),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(empty.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(one_sample.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(empty.reason, "COVERAGE_INSUFFICIENT")
        self.assertEqual(one_sample.reason, "COVERAGE_INSUFFICIENT")

    def test_zero_duration_integral_helper_is_invalid(self) -> None:
        first = self.event(5, 10, event_number=1)
        second = self.event(5, 20, event_number=2)

        result = integrate_power(
            (first, second),
            self.window(),
            maximum_sample_gap_seconds=10 * 60,
            direction="positive",
        )

        self.assertFalse(result.valid)
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "COVERAGE_INSUFFICIENT")

    def test_average_with_only_oversized_gaps_is_invalid(self) -> None:
        result = time_weighted_average(
            (
                self.event(0, 10, event_number=1),
                self.event(10, 20, event_number=2),
            ),
            self.window(),
            maximum_sample_gap_seconds=5 * 60,
            source_unit="kW",
            output_unit="kW",
        )

        self.assertFalse(result.valid)
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "COVERAGE_INSUFFICIENT")

    def test_opposite_direction_with_valid_duration_is_real_zero(self) -> None:
        state = MetricProjectionState(
            revision=self.compiled(
                method="power_integral",
                flow_direction="positive",
                good_coverage=0.5,
                minimum_coverage=0.2,
            ),
            maximum_sample_gap_seconds=15 * 60,
        )

        decision = project_metric(
            state,
            (
                self.event(5, -10, event_number=1),
                self.event(15, -10, event_number=2),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.PROVISIONAL)
        self.assertEqual(decision.value, Decimal("0"))
        self.assertEqual(decision.coverage, 10 * 60 / (15 * 60))

    def test_bool_value_fails_closed_with_stable_reason(self) -> None:
        decision = project_metric(
            self.state(good_coverage=0, minimum_coverage=0),
            (
                self.event(5, True, event_number=1),
                self.event(15, True, event_number=2),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "VALUE_TYPE_INVALID")

    def test_nonfinite_power_is_rejected_and_projected_as_invalid(self) -> None:
        invalid = self.event(15, float("nan"), event_number=2)

        with self.assertRaisesRegex(ValueError, "finite"):
            integrate_power(
                (self.event(5, 10, event_number=1), invalid),
                self.window(15),
                maximum_sample_gap_seconds=15 * 60,
                direction="positive",
            )

        decision = project_metric(
            self.state(good_coverage=0.5, minimum_coverage=0.2),
            (self.event(5, 10, event_number=1), invalid),
            self.BASE + timedelta(minutes=15),
        )
        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.reason, "NONFINITE_VALUE")

    def test_event_timestamp_must_be_aware(self) -> None:
        event = replace(
            self.event(5, 10, event_number=1),
            observed_at=datetime(2026, 8, 23, 4, 5),
        )

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            project_metric(
                self.state(),
                (event,),
                self.BASE + timedelta(minutes=15),
            )


if __name__ == "__main__":
    unittest.main()
