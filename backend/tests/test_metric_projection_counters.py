from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from app.services.data_trunk_contracts import L2Observation, TrunkQuality, TypedValue
from app.services.metric_projection import CounterContract, counter_delta


class MetricProjectionCounterTest(unittest.TestCase):
    def test_normal_counter_sums_only_monotonic_differences(self) -> None:
        result = counter_delta(
            (Decimal("100"), Decimal("130"), Decimal("155")),
            CounterContract(maximum=Decimal("999999")),
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.value, Decimal("55"))
        self.assertEqual(result.quality, TrunkQuality.GOOD)

    def test_frozen_reset_rule_accumulates_value_after_reset(self) -> None:
        result = counter_delta(
            (100, 130, 5, 20),
            CounterContract(maximum=999999, reset_on_decrease=True),
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.value, Decimal("50"))

    def test_frozen_16_bit_rollover_is_exact(self) -> None:
        result = counter_delta(
            (65530, 3),
            CounterContract(
                bit_width=16,
                maximum=65535,
                rollover_on_decrease=True,
            ),
        )

        self.assertEqual(result.value, Decimal("9"))

    def test_frozen_32_bit_rollover_is_exact(self) -> None:
        result = counter_delta(
            (4294967290, 4),
            CounterContract(
                bit_width=32,
                maximum=4294967295,
                rollover_on_decrease=True,
            ),
        )

        self.assertEqual(result.value, Decimal("10"))

    def test_frozen_64_bit_rollover_does_not_round_through_float(self) -> None:
        result = counter_delta(
            (18446744073709551610, 4),
            CounterContract(
                bit_width=64,
                maximum=18446744073709551615,
                rollover_on_decrease=True,
            ),
        )

        self.assertEqual(result.value, Decimal("10"))

    def test_unclassified_decrease_is_bad_instead_of_negative_or_zero(self) -> None:
        result = counter_delta(
            (100, 5),
            CounterContract(maximum=999999),
        )

        self.assertFalse(result.valid)
        self.assertIsNone(result.value)
        self.assertEqual(result.quality, TrunkQuality.BAD)
        self.assertEqual(result.reason, "COUNTER_DISCONTINUITY_AMBIGUOUS")

    def test_competing_reset_and_rollover_rules_are_ambiguous(self) -> None:
        result = counter_delta(
            (65530, 3),
            CounterContract(
                bit_width=16,
                maximum=65535,
                reset_on_decrease=True,
                rollover_on_decrease=True,
            ),
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.quality, TrunkQuality.BAD)
        self.assertEqual(result.reason, "COUNTER_DISCONTINUITY_AMBIGUOUS")

    def test_value_above_frozen_maximum_is_invalid(self) -> None:
        result = counter_delta(
            (65535, 65536),
            CounterContract(bit_width=16, maximum=65535),
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "COUNTER_VALUE_OUT_OF_RANGE")

    def test_nonfinite_counter_values_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), Decimal("NaN")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite"):
                    counter_delta((0, value), CounterContract(maximum=999999))

    def test_counter_requires_two_samples_instead_of_fabricating_zero(self) -> None:
        for values in ((), (100,)):
            with self.subTest(values=values):
                result = counter_delta(values, CounterContract(maximum=999999))

                self.assertFalse(result.valid)
                self.assertIsNone(result.value)
                self.assertEqual(result.quality, TrunkQuality.BAD)

    def test_counter_converts_frozen_mwh_to_kwh(self) -> None:
        observed_at = datetime(2026, 8, 23, tzinfo=timezone.utc)
        samples = (
            L2Observation(
                event_id=UUID(int=1),
                entity_instance_id=UUID(int=101),
                definition_id="site.energy",
                value=TypedValue.float(1.0),
                unit="MWh",
                quality=TrunkQuality.GOOD,
                reason=None,
                observed_at=observed_at,
                received_at=observed_at,
                calculated_at=observed_at,
                processing_revision_id=UUID(int=201),
                site_configuration_version=1,
                source_observation_ids=(),
                source_digest="1" * 64,
                source_order_key="1",
                event_time_basis="observed_at",
            ),
            L2Observation(
                event_id=UUID(int=2),
                entity_instance_id=UUID(int=101),
                definition_id="site.energy",
                value=TypedValue.float(1.5),
                unit="MWh",
                quality=TrunkQuality.GOOD,
                reason=None,
                observed_at=observed_at + timedelta(minutes=1),
                received_at=observed_at + timedelta(minutes=1),
                calculated_at=observed_at + timedelta(minutes=1),
                processing_revision_id=UUID(int=201),
                site_configuration_version=1,
                source_observation_ids=(),
                source_digest="2" * 64,
                source_order_key="2",
                event_time_basis="observed_at",
            ),
        )

        result = counter_delta(
            samples,
            CounterContract(maximum=Decimal("999")),
            source_unit="MWh",
            output_unit="kWh",
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.value, Decimal("500.0"))


if __name__ == "__main__":
    unittest.main()
