"""Contract tests for the digest-only multi-architecture release producer."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


class BuildReleaseImagesTest(unittest.TestCase):
    def test_frontend_build_runs_on_native_builder_platform(self) -> None:
        dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend-builder",
            dockerfile,
        )
        self.assertIn("FROM python:3.12-slim", dockerfile)

    def test_builds_each_required_architecture_and_writes_a_verified_manifest(self) -> None:
        from scripts.build_release_images import build_release_images

        calls: list[list[str]] = []

        def runner(command: list[str]) -> None:
            calls.append(command)
            metadata_path = Path(command[command.index("--metadata-file") + 1])
            architecture = command[command.index("--platform") + 1]
            digest = "a" * 64 if architecture == "linux/amd64" else "b" * 64
            metadata_path.write_text(
                json.dumps({"containerimage.digest": f"sha256:{digest}"}),
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release.json"
            release = build_release_images(
                repository="registry.example/zizu",
                platform_version=SOURCE_VERSION,
                edge_proxy_image="registry.example/caddy@sha256:" + "c" * 64,
                output=output,
                migrations_dir=REPO_ROOT / "init-db",
                build_context=REPO_ROOT,
                runner=runner,
            )
            persisted = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(release, persisted)
        self.assertEqual("051", release["schema_version"])
        self.assertEqual(
            {
                "linux/amd64": "registry.example/zizu@sha256:" + "a" * 64,
                "linux/arm64": "registry.example/zizu@sha256:" + "b" * 64,
            },
            release["images"],
        )
        self.assertEqual(2, len(calls))
        self.assertTrue(all("--push" in command for command in calls))
        self.assertTrue(all("--metadata-file" in command for command in calls))
        self.assertTrue(
            all(
                command[command.index("--build-arg") + 1]
                == f"ZIZU_VERSION={SOURCE_VERSION}"
                for command in calls
            )
        )
        self.assertEqual(
            ["linux/amd64", "linux/arm64"],
            [command[command.index("--platform") + 1] for command in calls],
        )

    def test_refuses_to_write_a_manifest_when_buildx_does_not_report_a_digest(self) -> None:
        from scripts.build_release_images import ReleaseBuildError, build_release_images

        def runner(command: list[str]) -> None:
            metadata_path = Path(command[command.index("--metadata-file") + 1])
            metadata_path.write_text("{}", encoding="utf-8")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release.json"
            with self.assertRaises(ReleaseBuildError):
                build_release_images(
                    repository="registry.example/zizu",
                    platform_version=SOURCE_VERSION,
                    edge_proxy_image="registry.example/caddy@sha256:" + "c" * 64,
                    output=output,
                    migrations_dir=REPO_ROOT / "init-db",
                    build_context=REPO_ROOT,
                    runner=runner,
                )
            self.assertFalse(output.exists())

    def test_refuses_a_tagged_repository_before_any_build_can_start(self) -> None:
        from scripts.build_release_images import ReleaseBuildError, build_release_images

        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ReleaseBuildError):
                build_release_images(
                    repository="registry.example/zizu:latest",
                    platform_version="0.4.78",
                    edge_proxy_image="registry.example/caddy@sha256:" + "c" * 64,
                    output=Path(directory) / "release.json",
                    migrations_dir=REPO_ROOT / "init-db",
                    runner=calls.append,
                )
        self.assertEqual([], calls)

    def test_refuses_a_version_that_does_not_match_the_release_source(self) -> None:
        from scripts.build_release_images import ReleaseBuildError, build_release_images

        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ReleaseBuildError, "source VERSION"):
                build_release_images(
                    repository="registry.example/zizu",
                    platform_version="0.0.0",
                    edge_proxy_image="registry.example/caddy@sha256:" + "c" * 64,
                    output=Path(directory) / "release.json",
                    migrations_dir=REPO_ROOT / "init-db",
                    build_context=REPO_ROOT,
                    runner=calls.append,
                )
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
