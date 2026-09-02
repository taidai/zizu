from __future__ import annotations

import unittest
from uuid import uuid4

from scripts.alarm_http_notification_e2e_fixture import (
    validate_force_due_candidate,
)


class AlarmHttpNotificationE2eFixtureTest(unittest.TestCase):
    def test_force_due_accepts_only_current_run_and_loopback_receiver(self) -> None:
        notification_id = uuid4()
        validate_force_due_candidate(
            notification_id=notification_id,
            alarm_name="E2E通知-run-1",
            target_display="http://127.0.0.1:19091/***",
            expected_run_id="run-1",
        )

    def test_force_due_refuses_non_e2e_notification(self) -> None:
        with self.assertRaisesRegex(ValueError, "refusing non-E2E notification"):
            validate_force_due_candidate(
                notification_id=uuid4(),
                alarm_name="现场真实告警",
                target_display="https://business.example/***",
                expected_run_id="run-1",
            )


if __name__ == "__main__":
    unittest.main()
