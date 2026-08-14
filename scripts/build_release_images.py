"""Build ZiZu platform images and emit the only accepted immutable release manifest.

This command deliberately performs two architecture-specific Buildx pushes.  A
tag is only a short-lived build transport; the resulting ``release.json``
contains the digest returned by Buildx and is the sole deployment input.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:  # support both `python scripts/...` and `python -m scripts...`
    from release_preflight import REQUIRED_ARCHITECTURES, _latest_migration_version, verify_release
except ModuleNotFoundError:  # pragma: no cover - module entrypoint
    from scripts.release_preflight import REQUIRED_ARCHITECTURES, _latest_migration_version, verify_release


REPO_ROOT = Path(__file__).resolve().parents[1]
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
Runner = Callable[[list[str]], None]


class ReleaseBuildError(RuntimeError):
    pass


def build_release_images(
    *,
    repository: str,
    platform_version: str,
    edge_proxy_image: str,
    output: Path,
    migrations_dir: Path,
    build_context: Path = REPO_ROOT,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Push amd64/arm64 images and atomically write a verified release manifest."""
    repository = _repository(repository)
    platform_version = _nonempty("platform_version", platform_version)
    source_version = _source_version(build_context)
    if platform_version != source_version:
        raise ReleaseBuildError(
            f"platform_version {platform_version!r} does not match source VERSION {source_version!r}"
        )
    schema_version = _latest_migration_version(migrations_dir)
    # Validate every non-build input before an external push can begin.
    verify_release(
        {
            "platform_version": platform_version,
            "schema_version": schema_version,
            "edge_proxy_image": edge_proxy_image,
            "images": {
                architecture: f"{repository}@sha256:{'0' * 64}"
                for architecture in REQUIRED_ARCHITECTURES
            },
        },
        expected_schema_version=schema_version,
    )
    execute = runner or _subprocess_runner
    images: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="zizu-release-") as directory:
        metadata_dir = Path(directory)
        for architecture in REQUIRED_ARCHITECTURES:
            metadata = metadata_dir / f"{architecture.removeprefix('linux/')}.json"
            tag = f"{repository}:{platform_version}-{architecture.removeprefix('linux/')}"
            command = [
                "docker",
                "buildx",
                "build",
                "--platform",
                architecture,
                "--file",
                str(REPO_ROOT / "backend" / "Dockerfile"),
                "--build-arg",
                f"ZIZU_VERSION={platform_version}",
                "--tag",
                tag,
                "--push",
                "--metadata-file",
                str(metadata),
                str(build_context),
            ]
            try:
                execute(command)
            except (OSError, subprocess.SubprocessError) as error:
                raise ReleaseBuildError(f"Buildx failed for {architecture}: {error}") from error
            images[architecture] = f"{repository}@{_metadata_digest(metadata, architecture)}"

    release = {
        "platform_version": platform_version,
        "schema_version": schema_version,
        "edge_proxy_image": edge_proxy_image,
        "images": images,
    }
    verify_release(release, expected_schema_version=schema_version)
    _write_json_atomically(output, release)
    return release


def _repository(value: str) -> str:
    value = _nonempty("repository", value).rstrip("/")
    repository_name = value.rsplit("/", 1)[-1]
    if "@" in value or ":" in repository_name or any(character.isspace() for character in value):
        raise ReleaseBuildError("repository must be an untagged registry/repository path")
    return value


def _nonempty(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(character.isspace() for character in value):
        raise ReleaseBuildError(f"{name} must be a non-empty value without whitespace")
    return value.strip()


def _source_version(build_context: Path) -> str:
    try:
        return _nonempty(
            "source VERSION",
            (build_context / "VERSION").read_text(encoding="utf-8").strip(),
        )
    except OSError as error:
        raise ReleaseBuildError("release source VERSION is not readable") from error


def _metadata_digest(path: Path, architecture: str) -> str:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseBuildError(f"Buildx did not write valid metadata for {architecture}") from error
    digest = document.get("containerimage.digest") if isinstance(document, dict) else None
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ReleaseBuildError(f"Buildx did not report an immutable digest for {architecture}")
    return digest


def _subprocess_runner(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _write_json_atomically(output: Path, document: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(document, temporary, ensure_ascii=False, sort_keys=True, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ZiZu immutable amd64 and arm64 release images")
    parser.add_argument("--repository", required=True, help="registry/repository path, without a tag or digest")
    parser.add_argument("--platform-version", required=True, help="ZiZu platform version recorded in release.json")
    parser.add_argument("--edge-proxy-image", required=True, help="pre-approved digest-pinned TLS proxy image")
    parser.add_argument("--output", type=Path, default=Path("release.json"), help="release manifest output path")
    parser.add_argument("--migrations-dir", type=Path, default=REPO_ROOT / "init-db")
    parser.add_argument("--build-context", type=Path, default=REPO_ROOT)
    arguments = parser.parse_args(argv)
    try:
        release = build_release_images(
            repository=arguments.repository,
            platform_version=arguments.platform_version,
            edge_proxy_image=arguments.edge_proxy_image,
            output=arguments.output,
            migrations_dir=arguments.migrations_dir,
            build_context=arguments.build_context,
        )
    except (ReleaseBuildError, ValueError) as error:
        print(json.dumps({"status": "rejected", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "built", "release": release}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
