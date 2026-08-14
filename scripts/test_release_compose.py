"""Public Docker Compose contract for an immutable ZiZu release."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.release.yml"
E606_COMPOSE = REPO_ROOT / "deploy" / "docker-compose.release.e606.yml"


class ReleaseComposeContractTest(unittest.TestCase):
    def test_renders_a_tls_fronted_backend_without_host_source_overlays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            release_env = temporary / "release.env"
            runtime_env = temporary / "runtime.env"
            release_env.write_text(
                "\n".join(
                    (
                        "ZIZU_PLATFORM_VERSION=0.4.78",
                        "ZIZU_SCHEMA_VERSION=031",
                        "ZIZU_PLATFORM_IMAGE=registry.example/zizu@sha256:" + "a" * 64,
                        "ZIZU_EDGE_PROXY_IMAGE=registry.example/caddy@sha256:" + "b" * 64,
                    )
                ),
                encoding="utf-8",
            )
            runtime_env.write_text("DB_HOST=postgres\n", encoding="utf-8")
            environment = os.environ | {
                "ZIZU_RUNTIME_ENV": str(runtime_env),
                "ZIZU_PUBLIC_HOST": "release.example",
                "ZIZU_ACME_EMAIL": "ops@example.com",
            }
            result = subprocess.run(
                ["docker", "compose", "--env-file", str(release_env), "-f", str(COMPOSE), "config"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("registry.example/zizu@sha256:" + "a" * 64, result.stdout)
        self.assertIn("registry.example/caddy@sha256:" + "b" * 64, result.stdout)
        self.assertIn("172.31.0.2/32", result.stdout)
        self.assertNotIn("build:", result.stdout)
        self.assertNotIn("9000:9000", result.stdout)
        self.assertNotIn("./backend", result.stdout)
        self.assertNotIn("./frontend", result.stdout)
        self.assertNotIn("./init-db", result.stdout)

    def test_renders_a_loopback_backend_for_e606_host_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            release_env = temporary / "release.env"
            runtime_env = temporary / "runtime.env"
            release_env.write_text(
                "\n".join(
                    (
                        "ZIZU_PLATFORM_VERSION=0.4.78",
                        "ZIZU_SCHEMA_VERSION=031",
                        "ZIZU_PLATFORM_IMAGE=registry.example/zizu@sha256:" + "a" * 64,
                        "ZIZU_EDGE_PROXY_IMAGE=registry.example/caddy@sha256:" + "b" * 64,
                    )
                ),
                encoding="utf-8",
            )
            runtime_env.write_text("DB_HOST=127.0.0.1\n", encoding="utf-8")
            environment = os.environ | {
                "ZIZU_RUNTIME_ENV": str(runtime_env),
                "ZIZU_PUBLIC_HOST": "release.example",
                "ZIZU_ACME_EMAIL": "ops@example.com",
            }
            result = subprocess.run(
                ["docker", "compose", "--env-file", str(release_env), "-f", str(E606_COMPOSE), "config"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("network_mode: host", result.stdout)
        self.assertIn("APP_BIND_HOST: 127.0.0.1", result.stdout)
        self.assertIn("AUTH_TRUSTED_PROXY_CIDRS: '[\"127.0.0.1/32\"]'", result.stdout)
        self.assertNotIn("build:", result.stdout)
        self.assertNotIn("./backend", result.stdout)
        self.assertNotIn("./frontend", result.stdout)
        self.assertNotIn("./init-db", result.stdout)


if __name__ == "__main__":
    unittest.main()
