from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.bootstrap_runtime_secrets import bootstrap, parse_env


def parse_nanomq_http_credentials(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    in_http_server = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "http_server {":
            in_http_server = True
            continue
        if in_http_server and stripped == "}":
            break
        if in_http_server and "=" in stripped:
            key, value = stripped.split("=", 1)
            if key.strip() in {"username", "password"}:
                result[key.strip()] = json.loads(value.strip())
    return result


class BootstrapRuntimeSecretsTest(unittest.TestCase):
    def test_new_deployment_generates_all_local_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq.conf"

            self.assertEqual(
                bootstrap(
                    env_file,
                    secret_file,
                    rotate=False,
                    neuron_password="rotated-" + "neuron-test-value",
                ),
                "ready",
            )

            env = parse_env(env_file.read_text(encoding="utf-8"))
            secret = parse_nanomq_http_credentials(secret_file.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(env["DB_PASSWORD"]), 32)
            self.assertGreaterEqual(len(env["DB_OWNER_PASSWORD"]), 32)
            self.assertNotEqual(env["DB_OWNER_PASSWORD"], env["DB_PASSWORD"])
            self.assertGreaterEqual(len(env["JWT_SECRET"]), 32)
            self.assertGreaterEqual(len(env["HTTP_NOTIFICATION_ENCRYPTION_KEY"]), 40)
            self.assertGreaterEqual(len(env["NANOMQ_API_PASSWORD"]), 32)
            self.assertEqual(env["NEURON_PASSWORD"], "rotated-neuron-test-value")
            self.assertNotEqual(env["NANOMQ_API_PASSWORD"], "public")
            self.assertEqual(secret["password"], env["NANOMQ_API_PASSWORD"])
            self.assertEqual(secret["username"], env["NANOMQ_API_USERNAME"])

    def test_new_deployment_requires_rotated_neuron_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq.conf"

            with self.assertRaisesRegex(RuntimeError, "Neuron"):
                bootstrap(env_file, secret_file, rotate=False)

            self.assertFalse(env_file.exists())
            self.assertFalse(secret_file.exists())

    def test_existing_secure_secret_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq.conf"
            env_file.write_text(
                "DB_PASSWORD=a-secure-database-value\n"
                "NEURON_PASSWORD=a-secure-neuron-value\n"
                "JWT_SECRET=a-secure-jwt-value-long-enough-123\n"
                "NANOMQ_API_USERNAME=engineer\n"
                "NANOMQ_API_PASSWORD=a-secure-existing-value\n",
                encoding="utf-8",
            )

            bootstrap(env_file, secret_file, rotate=False)
            first_env = env_file.read_bytes()
            first_http_key = parse_env(first_env.decode("utf-8"))[
                "HTTP_NOTIFICATION_ENCRYPTION_KEY"
            ]
            first_secret = secret_file.read_bytes()
            bootstrap(env_file, secret_file, rotate=False)

            self.assertEqual(env_file.read_bytes(), first_env)
            self.assertEqual(
                parse_env(env_file.read_text(encoding="utf-8"))[
                    "HTTP_NOTIFICATION_ENCRYPTION_KEY"
                ],
                first_http_key,
            )
            self.assertEqual(secret_file.read_bytes(), first_secret)

    def test_existing_public_default_requires_explicit_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq.conf"
            env_file.write_text(
                "DB_PASSWORD=a-secure-database-value\n"
                "NEURON_PASSWORD=a-secure-neuron-value\n"
                "JWT_SECRET=a-secure-jwt-value-long-enough-123\n"
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
            secret_file = Path(temp_dir) / "nanomq.conf"
            env_file.write_text(
                "DB_PASSWORD=a-secure-database-value\n"
                "NEURON_PASSWORD=a-secure-neuron-value\n"
                "JWT_SECRET=a-secure-jwt-value-long-enough-123\n"
                "NANOMQ_API_USERNAME=admin\nNANOMQ_API_PASSWORD=public\n",
                encoding="utf-8",
            )

            self.assertEqual(bootstrap(env_file, secret_file, rotate=True), "rotated")
            env = parse_env(env_file.read_text(encoding="utf-8"))
            secret = parse_nanomq_http_credentials(secret_file.read_text(encoding="utf-8"))
            self.assertNotEqual(env["NANOMQ_API_PASSWORD"], "public")
            self.assertEqual(secret["password"], env["NANOMQ_API_PASSWORD"])

    def test_existing_public_database_password_is_refused_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_file = Path(temp_dir) / "nanomq.conf"
            original = (
                "DB_PASSWORD=omnidev_2026\n"
                "NEURON_PASSWORD=a-secure-neuron-value\n"
                "JWT_SECRET=a-secure-jwt-value-long-enough-123\n"
                "NANOMQ_API_USERNAME=admin\n"
                "NANOMQ_API_PASSWORD=a-secure-nanomq-value\n"
            )
            env_file.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "database"):
                bootstrap(env_file, secret_file, rotate=False)

            self.assertEqual(env_file.read_text(encoding="utf-8"), original)
            self.assertFalse(secret_file.exists())

    def test_failed_secret_write_leaves_existing_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            blocker = root / "not-a-directory"
            secret_file = blocker / "nanomq.conf"
            original_env = (
                "DB_PASSWORD=a-secure-database-value\n"
                "NEURON_PASSWORD=a-secure-neuron-value\n"
                "JWT_SECRET=a-secure-jwt-value-long-enough-123\n"
                "NANOMQ_API_USERNAME=admin\n"
                "NANOMQ_API_PASSWORD=a-secure-nanomq-value\n"
            )
            original_secret = "existing broker configuration\n"
            env_file.write_text(original_env, encoding="utf-8")
            blocker.write_text(original_secret, encoding="utf-8")

            with self.assertRaises(OSError):
                bootstrap(env_file, secret_file, rotate=True)

            self.assertEqual(env_file.read_text(encoding="utf-8"), original_env)
            self.assertEqual(blocker.read_text(encoding="utf-8"), original_secret)

if __name__ == "__main__":
    unittest.main()
