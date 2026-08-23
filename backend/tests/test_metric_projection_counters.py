from __future__ import annotations

import unittest
from decimal import Decimal

from app.services.data_trunk_contracts import TrunkQuality
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


if __name__ == "__main__":
    unittest.main()
