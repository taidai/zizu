from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

from scripts.alarm_http_notification_e2e_fixture import (
    validate_force_due_candidate,
)


class AlarmHttpNotificationE2eFixtureTest(unittest.TestCase):
    def test_script_can_run_directly(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "alarm_http_notification_e2e_fixture.py"
        )

        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

        self.assertEqual(0, result.returncode, result.stderr)

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
