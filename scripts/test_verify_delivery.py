"""Minimal delivery verifier tests."""
from __future__ import annotations

import unittest
import sys
import tempfile
from pathlib import Path

from scripts.verify_delivery import latest_schema, run_check, summarize_status, validate_liveness


class VerifyDeliveryTest(unittest.TestCase):
    def test_status_is_failed_when_any_check_fails(self) -> None:
        checks = [
            {"status": "PASSED"},
            {"status": "FAILED"},
        ]

        self.assertEqual("FAILED", summarize_status(checks, site_requested=True))

    def test_status_is_incomplete_without_a_site_check(self) -> None:
        checks = [{"status": "PASSED"}]

        self.assertEqual("INCOMPLETE", summarize_status(checks, site_requested=False))

    def test_status_is_passed_when_local_and_site_checks_pass(self) -> None:
        checks = [
            {"status": "PASSED"},
            {"status": "PASSED"},
        ]

        self.assertEqual("PASSED", summarize_status(checks, site_requested=True))

    def test_liveness_must_be_alive_and_match_the_repository_version(self) -> None:
        validate_liveness({"status": "alive", "version": "0.4.87"}, "0.4.87")

        with self.assertRaisesRegex(ValueError, "version"):
            validate_liveness({"status": "alive", "version": "0.4.86"}, "0.4.87")

        with self.assertRaisesRegex(ValueError, "status"):
            validate_liveness({"status": "starting", "version": "0.4.87"}, "0.4.87")

    def test_latest_schema_comes_from_the_highest_migration_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(directory)
            (migrations / "migration_009_old.sql").touch()
            (migrations / "migration_051_current.sql").touch()

            self.assertEqual("051", latest_schema(migrations))

    def test_command_result_preserves_pass_or_fail(self) -> None:
        passed = run_check(
            "example pass",
            [sys.executable, "-c", "raise SystemExit(0)"],
            Path.cwd(),
        )
        failed = run_check(
            "example fail",
            [sys.executable, "-c", "raise SystemExit(7)"],
            Path.cwd(),
        )

        self.assertEqual({"name": "example pass", "status": "PASSED", "exit_code": 0}, passed)
        self.assertEqual({"name": "example fail", "status": "FAILED", "exit_code": 7}, failed)


if __name__ == "__main__":
    unittest.main()
