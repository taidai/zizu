"""Public release-preflight CLI contract tests."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "scripts" / "release_preflight.py"


class ReleasePreflightCliTest(unittest.TestCase):
    def test_accepts_a_complete_multi_architecture_immutable_release(self) -> None:
        """A release candidate is usable only with digest-pinned amd64 and arm64 images."""
        release = {
            "platform_version": "0.4.78",
            "schema_version": "031",
            "edge_proxy_image": "registry.example/caddy@sha256:" + "c" * 64,
            "images": {
                "linux/amd64": "registry.example/zizu@sha256:" + "a" * 64,
                "linux/arm64": "registry.example/zizu@sha256:" + "b" * 64,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "release.json"
            manifest.write_text(json.dumps(release), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(CLI), "verify", "--release", str(manifest)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "platform_version": "0.4.78",
                "schema_version": "031",
                "architectures": ["linux/amd64", "linux/arm64"],
                "status": "verified",
            },
        )

    def test_rejects_a_mutable_or_single_architecture_image(self) -> None:
        release = {
            "platform_version": "0.4.78",
            "schema_version": "031",
            "edge_proxy_image": "registry.example/caddy@sha256:" + "c" * 64,
            "images": {"linux/arm64": "registry.example/zizu:latest"},
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "release.json"
            manifest.write_text(json.dumps(release), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(CLI), "verify", "--release", str(manifest)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["code"], "RELEASE_PREFLIGHT_FAILED")
        self.assertIn("linux/amd64", json.loads(result.stderr)["message"])

    def test_rejects_a_release_without_a_digest_pinned_tls_proxy(self) -> None:
        release = {
            "platform_version": "0.4.78",
            "schema_version": "031",
            "images": {
                "linux/amd64": "registry.example/zizu@sha256:" + "a" * 64,
                "linux/arm64": "registry.example/zizu@sha256:" + "b" * 64,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "release.json"
            manifest.write_text(json.dumps(release), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(CLI), "verify", "--release", str(manifest)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["code"], "RELEASE_PREFLIGHT_FAILED")
        self.assertIn("edge_proxy_image", json.loads(result.stderr)["message"])

    def test_rejects_a_release_that_does_not_include_the_current_schema(self) -> None:
        release = {
            "platform_version": "0.4.78",
            "schema_version": "030",
            "edge_proxy_image": "registry.example/caddy@sha256:" + "c" * 64,
            "images": {
                "linux/amd64": "registry.example/zizu@sha256:" + "a" * 64,
                "linux/arm64": "registry.example/zizu@sha256:" + "b" * 64,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "release.json"
            manifest.write_text(json.dumps(release), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "verify",
                    "--release",
                    str(manifest),
                    "--migrations-dir",
                    str(REPO_ROOT / "init-db"),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["code"], "RELEASE_SCHEMA_MISMATCH")

    def test_renders_a_digest_only_environment_for_the_target_architecture(self) -> None:
        release = {
            "platform_version": "0.4.78",
            "schema_version": "031",
            "edge_proxy_image": "registry.example/caddy@sha256:" + "c" * 64,
            "images": {
                "linux/amd64": "registry.example/zizu@sha256:" + "a" * 64,
                "linux/arm64": "registry.example/zizu@sha256:" + "b" * 64,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "release.json"
            manifest.write_text(json.dumps(release), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "render-env",
                    "--release",
                    str(manifest),
                    "--architecture",
                    "linux/arm64",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "ZIZU_PLATFORM_VERSION=0.4.78",
                "ZIZU_SCHEMA_VERSION=031",
                "ZIZU_PLATFORM_IMAGE=registry.example/zizu@sha256:" + "b" * 64,
                "ZIZU_EDGE_PROXY_IMAGE=registry.example/caddy@sha256:" + "c" * 64,
            ],
        )


if __name__ == "__main__":
    unittest.main()
