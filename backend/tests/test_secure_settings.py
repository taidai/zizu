from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = {
    "DEPLOYMENT_MODE",
    "ALLOW_INSECURE_DEV_SECRETS",
    "DB_PASSWORD",
    "NEURON_PASSWORD",
    "NANOMQ_API_PASSWORD",
    "JWT_SECRET",
}


def run_settings_import(extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in SECRET_NAMES}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", "import app.core.config"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class SecureSettingsTest(unittest.TestCase):
    def test_application_refuses_to_start_without_runtime_secrets(self) -> None:
        result = run_settings_import()

        self.assertNotEqual(result.returncode, 0)
        for field in (
            "db_password",
            "neuron_password",
            "nanomq_api_password",
            "jwt_secret",
        ):
            self.assertIn(field, result.stderr)

    def test_application_accepts_complete_runtime_secrets(self) -> None:
        result = run_settings_import(
            {
                "DB_PASSWORD": "database-secret-value",
                "NEURON_PASSWORD": "neuron-secret-value",
                "NANOMQ_API_PASSWORD": "nanomq-secret-value",
                "JWT_SECRET": "jwt-secret-value-that-is-at-least-32-chars",
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_application_rejects_public_default_secrets(self) -> None:
        result = run_settings_import(
            {
                "DB_PASSWORD": "omnidev_2026",
                "NEURON_PASSWORD": "000000",
                "NANOMQ_API_PASSWORD": "public",
                "JWT_SECRET": "zizu-dev-secret-change-in-production",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        for message in (
            "public database password",
            "public Neuron password",
            "public NanoMQ API password",
            "public JWT secret",
        ):
            self.assertIn(message, result.stderr)

    def test_application_rejects_blank_runtime_secrets(self) -> None:
        result = run_settings_import(
            {
                "DB_PASSWORD": "   ",
                "NEURON_PASSWORD": "\t",
                "NANOMQ_API_PASSWORD": "  ",
                "JWT_SECRET": " " * 32,
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be blank", result.stderr)

    def test_explicit_insecure_development_mode_emits_visible_warning(self) -> None:
        result = run_settings_import(
            {
                "DEPLOYMENT_MODE": "development",
                "ALLOW_INSECURE_DEV_SECRETS": "true",
                "DB_PASSWORD": "zizu_dev_2026",
                "NEURON_PASSWORD": "000000",
                "NANOMQ_API_PASSWORD": "public",
                "JWT_SECRET": "zizu-dev-secret-change-in-production",
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INSECURE DEVELOPMENT MODE", result.stderr)

    def test_production_cannot_enable_insecure_development_secrets(self) -> None:
        result = run_settings_import(
            {
                "ALLOW_INSECURE_DEV_SECRETS": "true",
                "DB_PASSWORD": "database-secret-value",
                "NEURON_PASSWORD": "neuron-secret-value",
                "NANOMQ_API_PASSWORD": "nanomq-secret-value",
                "JWT_SECRET": "jwt-secret-value-that-is-at-least-32-chars",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires DEPLOYMENT_MODE=development", result.stderr)

    def test_standalone_clients_reject_public_default_passwords(self) -> None:
        cases = (
            (
                "from app.services.neuron_client import NeuronConfig; "
                "NeuronConfig(password='0000')",
                "public Neuron password",
            ),
            (
                "from app.services.nanomq_client import NanoMQConfig; "
                "NanoMQConfig(password='public')",
                "public NanoMQ API password",
            ),
        )
        for code, message in cases:
            with self.subTest(message=message):
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=BACKEND_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_standalone_client_development_exception_requires_mode_and_warns(self) -> None:
        code = (
            "from app.services.neuron_client import NeuronConfig; "
            "NeuronConfig(password='0000', deployment_mode='development', "
            "allow_insecure_dev_secrets=True)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=BACKEND_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INSECURE DEVELOPMENT MODE", result.stderr)


if __name__ == "__main__":
    unittest.main()
