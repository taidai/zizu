from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.services.metric_projection import aligned_daily_window, rolling_window


class MetricProjectionTimezoneTest(unittest.TestCase):
    def test_shanghai_daily_window_is_local_midnight_in_utc(self) -> None:
        window = aligned_daily_window(
            datetime(2026, 8, 23, 4, tzinfo=UTC),
            "Asia/Shanghai",
        )

        self.assertEqual(window.start, datetime(2026, 8, 22, 16, tzinfo=UTC))
        self.assertEqual(window.end, datetime(2026, 8, 23, 16, tzinfo=UTC))
        self.assertEqual(window.duration_seconds, 24 * 60 * 60)

    def test_spring_dst_daily_window_has_23_real_hours(self) -> None:
        window = aligned_daily_window(
            datetime(2026, 3, 8, 16, tzinfo=UTC),
            "America/New_York",
        )

        self.assertEqual(window.start, datetime(2026, 3, 8, 5, tzinfo=UTC))
        self.assertEqual(window.end, datetime(2026, 3, 9, 4, tzinfo=UTC))
        self.assertEqual(window.duration_seconds, 23 * 60 * 60)

    def test_autumn_dst_daily_window_has_25_real_hours(self) -> None:
        window = aligned_daily_window(
            datetime(2026, 11, 1, 16, tzinfo=UTC),
            "America/New_York",
        )

        self.assertEqual(window.start, datetime(2026, 11, 1, 4, tzinfo=UTC))
        self.assertEqual(window.end, datetime(2026, 11, 2, 5, tzinfo=UTC))
        self.assertEqual(window.duration_seconds, 25 * 60 * 60)

    def test_rolling_window_is_open_on_start_and_closed_on_end(self) -> None:
        end = datetime(2026, 8, 23, 4, 15, tzinfo=UTC)
        window = rolling_window(end, 15 * 60)

        self.assertEqual(window.start, end - timedelta(minutes=15))
        self.assertFalse(window.contains(window.start))
        self.assertTrue(window.contains(window.start + timedelta(microseconds=1)))
        self.assertTrue(window.contains(window.end))
        self.assertFalse(window.contains(window.end + timedelta(microseconds=1)))

    def test_window_inputs_must_be_timezone_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            aligned_daily_window(datetime(2026, 8, 23, 4), "Asia/Shanghai")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            rolling_window(datetime(2026, 8, 23, 4), 900)


if __name__ == "__main__":
    unittest.main()
