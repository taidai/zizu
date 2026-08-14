"""Verify that a ZiZu release candidate can be deployed immutably.

This public deployment gate deliberately consumes only a release manifest.  It
does not contact a registry or a target host, so an engineer can fail a bad
release before a maintenance window begins.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


REQUIRED_ARCHITECTURES = ("linux/amd64", "linux/arm64")
_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_MIGRATION_FILE = re.compile(r"^migration_(\d+).*\.sql$")


class ReleasePreflightError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def verify_release(document: dict[str, Any], expected_schema_version: str | None = None) -> dict[str, Any]:
    """Return the public release summary or raise ValueError for an unsafe input."""
    platform_version = document.get("platform_version")
    schema_version = document.get("schema_version")
    images = document.get("images")
    edge_proxy_image = document.get("edge_proxy_image")
    if not isinstance(platform_version, str) or not platform_version.strip():
        raise ValueError("platform_version must be a non-empty string")
    if not isinstance(schema_version, str) or not schema_version.isdecimal():
        raise ValueError("schema_version must be a decimal migration version")
    if not isinstance(images, dict):
        raise ValueError("images must map each required architecture to an immutable digest image")
    if not isinstance(edge_proxy_image, str) or not _DIGEST_IMAGE.fullmatch(edge_proxy_image):
        raise ValueError("edge_proxy_image must be an image pinned by sha256 digest")
    if ":latest" in edge_proxy_image.lower():
        raise ValueError("edge_proxy_image must not use latest")

    if expected_schema_version is not None and schema_version != expected_schema_version:
        raise ReleasePreflightError(
            "RELEASE_SCHEMA_MISMATCH",
            f"schema_version {schema_version} does not match release migrations {expected_schema_version}",
        )

    for architecture in REQUIRED_ARCHITECTURES:
        image = images.get(architecture)
        if not isinstance(image, str) or not _DIGEST_IMAGE.fullmatch(image):
            raise ValueError(f"images.{architecture} must be an image pinned by sha256 digest")
        if ":latest" in image.lower():
            raise ValueError(f"images.{architecture} must not use latest")

    return {
        "platform_version": platform_version,
        "schema_version": schema_version,
        "architectures": list(REQUIRED_ARCHITECTURES),
        "status": "verified",
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cannot read release manifest: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"release manifest is not valid JSON: {error.msg}") from error
    if not isinstance(document, dict):
        raise ValueError("release manifest root must be an object")
    return document


def _latest_migration_version(directory: Path) -> str:
    try:
        filenames = list(directory.iterdir())
    except OSError as error:
        raise ValueError(f"cannot read migrations directory: {error}") from error
    versions = [match.group(1) for path in filenames if (match := _MIGRATION_FILE.fullmatch(path.name))]
    if not versions:
        raise ValueError("migrations directory contains no migration_*.sql files")
    return max(versions, key=int)


def render_environment(document: dict[str, Any], architecture: str) -> str:
    """Render the only image values a release Compose file may interpolate."""
    verify_release(document)
    image = document["images"].get(architecture)
    if not isinstance(image, str):
        raise ValueError(f"release does not contain an image for {architecture}")
    return "\n".join(
        (
            f"ZIZU_PLATFORM_VERSION={document['platform_version']}",
            f"ZIZU_SCHEMA_VERSION={document['schema_version']}",
            f"ZIZU_PLATFORM_IMAGE={image}",
            f"ZIZU_EDGE_PROXY_IMAGE={document['edge_proxy_image']}",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a ZiZu immutable release manifest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="verify a release manifest without contacting a target")
    verify.add_argument("--release", type=Path, required=True, help="path to release.json")
    verify.add_argument(
        "--migrations-dir",
        type=Path,
        help="require schema_version to equal the latest migration in this release",
    )
    render = subparsers.add_parser("render-env", help="render architecture-specific immutable image variables")
    render.add_argument("--release", type=Path, required=True, help="path to release.json")
    render.add_argument("--architecture", choices=REQUIRED_ARCHITECTURES, required=True)
    arguments = parser.parse_args(argv)

    try:
        document = _read_manifest(arguments.release)
        if arguments.command == "render-env":
            print(render_environment(document, arguments.architecture))
            return 0
        expected_schema = _latest_migration_version(arguments.migrations_dir) if arguments.migrations_dir else None
        summary = verify_release(document, expected_schema)
    except ReleasePreflightError as error:
        print(json.dumps({"status": "rejected", "code": error.code, "message": str(error)}), file=sys.stderr)
        return 2
    except ValueError as error:
        print(json.dumps({"status": "rejected", "code": "RELEASE_PREFLIGHT_FAILED", "message": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
