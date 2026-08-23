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
        value: float,
        *,
        event_number: int,
        quality: TrunkQuality = TrunkQuality.GOOD,
        second: int = 0,
    ) -> L2Observation:
        event_id = UUID(int=event_number)
        observed_at = self.BASE + timedelta(minutes=minute, seconds=second)
        return L2Observation(
            event_id=event_id,
            entity_instance_id=self.SOURCE_ID,
            definition_id="site.active_power",
            value=TypedValue.float(value),
            unit="kW",
            quality=quality,
            reason=None,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
            calculated_at=observed_at + timedelta(seconds=1),
            processing_revision_id=self.REVISION_ID,
            site_configuration_version=7,
            source_observation_ids=(),
            source_digest=f"{event_number:064x}",
            source_order_key=f"S:{event_number:020d}",
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
    ):
        raw = {
            "schemaVersion": "zizu.business-metric/v1alpha1",
            "id": f"test.{method}",
            "revision": 1,
            "displayName": method,
            "targetNodeType": "SITE",
            "output": {
                "entityDefinition": f"site.{method}",
                "dataType": "FLOAT",
                "unit": "kW" if method in {"average", "maximum"} else "kWh",
                "temporalSemantics": "windowed",
            },
            "window": {"kind": "rolling", "duration": "15m"},
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
                        unit="kW",
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

        self.assertEqual(result.value, Decimal("0"))
        self.assertEqual(result.coverage, 0.0)

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

    def test_average_is_time_weighted_instead_of_sample_weighted(self) -> None:
        result = time_weighted_average(
            (
                self.event(0, 0, event_number=1),
                self.event(1, 10, event_number=2),
                self.event(10, 10, event_number=3),
            ),
            self.window(),
            maximum_sample_gap_seconds=10 * 60,
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
        )

        self.assertEqual(result.value, Decimal("18"))
        self.assertEqual(result.peak_at, winner.observed_at)
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

    def test_bad_source_invalidates_decision_even_when_other_samples_cover_window(self) -> None:
        decision = project_metric(
            self.state(good_coverage=0.5, minimum_coverage=0.2),
            (
                self.event(1, 10, event_number=1),
                self.event(15, 10, event_number=2),
                self.event(8, 10, event_number=3, quality=TrunkQuality.BAD),
            ),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.quality, TrunkQuality.BAD)
        self.assertEqual(decision.reason, "SOURCE_BAD")

    def test_ambiguous_counter_is_an_invalid_ledger_decision(self) -> None:
        revision = self.compiled(method="counter_delta")
        state = MetricProjectionState(
            revision=revision,
            counter_contract=CounterContract(maximum=999999),
        )
        first = self.event(1, 100, event_number=1)
        second = replace(self.event(14, 5, event_number=2), definition_id="site.active_power")

        decision = project_metric(
            state,
            (first, second),
            self.BASE + timedelta(minutes=15),
        )

        self.assertEqual(decision.lifecycle, MetricLifecycle.INVALID)
        self.assertEqual(decision.quality, TrunkQuality.BAD)
        self.assertIsNone(decision.value)
        self.assertEqual(decision.reason, "COUNTER_DISCONTINUITY_AMBIGUOUS")
        self.assertEqual(decision.history_facts, ())

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
