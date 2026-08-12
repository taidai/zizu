from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.bootstrap_runtime_secrets import bootstrap, parse_env


def parse_secret_file(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, value = line.split("=", 1)
        result[key.strip()] = json.loads(value.strip())
    return result


class BootstrapRuntimeSecretsTest(unittest.TestCase):
    def test_new_deployment_generates_matching_non_default_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq-http.conf"

            self.assertEqual(bootstrap(env_file, secret_file, rotate=False), "ready")

            env = parse_env(env_file.read_text(encoding="utf-8"))
            secret = parse_secret_file(secret_file.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(env["NANOMQ_API_PASSWORD"]), 32)
            self.assertNotEqual(env["NANOMQ_API_PASSWORD"], "public")
            self.assertEqual(secret["password"], env["NANOMQ_API_PASSWORD"])
            self.assertEqual(secret["username"], env["NANOMQ_API_USERNAME"])

    def test_existing_secure_secret_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq-http.conf"
            env_file.write_text(
                "NANOMQ_API_USERNAME=engineer\n"
                "NANOMQ_API_PASSWORD=a-secure-existing-value\n",
                encoding="utf-8",
            )

            bootstrap(env_file, secret_file, rotate=False)
            first_env = env_file.read_bytes()
            first_secret = secret_file.read_bytes()
            bootstrap(env_file, secret_file, rotate=False)

            self.assertEqual(env_file.read_bytes(), first_env)
            self.assertEqual(secret_file.read_bytes(), first_secret)

    def test_existing_public_default_requires_explicit_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq-http.conf"
            env_file.write_text(
                "NANOMQ_API_USERNAME=admin\nNANOMQ_API_PASSWORD=public\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "--rotate"):
                bootstrap(env_file, secret_file, rotate=False)

            self.assertFalse(secret_file.exists())
            self.assertEqual(parse_env(env_file.read_text())["NANOMQ_API_PASSWORD"], "public")

    def test_explicit_rotation_replaces_public_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq-http.conf"
            env_file.write_text(
                "NANOMQ_API_USERNAME=admin\nNANOMQ_API_PASSWORD=public\n",
                encoding="utf-8",
            )

            self.assertEqual(bootstrap(env_file, secret_file, rotate=True), "rotated")
            env = parse_env(env_file.read_text(encoding="utf-8"))
            secret = parse_secret_file(secret_file.read_text(encoding="utf-8"))
            self.assertNotEqual(env["NANOMQ_API_PASSWORD"], "public")
            self.assertEqual(secret["password"], env["NANOMQ_API_PASSWORD"])


if __name__ == "__main__":
    unittest.main()
