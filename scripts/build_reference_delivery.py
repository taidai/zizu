"""Build the public PV/storage/charging EMS reference package deterministically."""
from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import zipfile

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "reference-deliveries" / "pv-storage-charging-ems"


def _yaml_mapping(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read YAML asset {path.as_posix()}") from error
    if not isinstance(value, dict):
        raise ValueError(f"YAML asset {path.as_posix()} must be a mapping")
    return value


def build_archive(source: Path = DEFAULT_SOURCE) -> bytes:
    """Return a canonical ZIP assembled from reviewed, source-controlled assets."""
    package = _yaml_mapping(source / "package.yaml")
    metadata = package.get("package")
    assets = package.get("assets")
    acceptance = package.get("acceptance")
    if not isinstance(metadata, dict) or not isinstance(assets, list) or not isinstance(acceptance, list):
        raise ValueError("package.yaml must declare package, assets and acceptance")
    files: dict[str, bytes] = {}
    manifest_assets: list[dict[str, str]] = []
    for declaration in assets:
        if not isinstance(declaration, dict):
            raise ValueError("asset declaration must be a mapping")
        asset_id = declaration.get("id")
        kind = declaration.get("kind")
        relative_path = declaration.get("path")
        if (
            not isinstance(asset_id, str)
            or not isinstance(kind, str)
            or not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
        ):
            raise ValueError("asset declaration is invalid")
        asset_path = source / relative_path
        content = asset_path.read_bytes()
        parsed = _yaml_mapping(asset_path)
        if parsed.get("id") != asset_id or (
            kind != "acceptance" and parsed.get("kind") != kind
        ):
            raise ValueError(f"asset identity mismatch: {relative_path}")
        files[relative_path] = content
        manifest_assets.append(
            {
                "id": asset_id,
                "kind": kind,
                "path": relative_path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if set(acceptance) - {item["id"] for item in manifest_assets}:
        raise ValueError("acceptance references an undeclared asset")
    manifest = {
        "schemaVersion": "zizu.solution/v1alpha1",
        "id": metadata.get("id"),
        "version": metadata.get("version"),
        "displayName": metadata.get("displayName"),
        "platform": {"version": metadata.get("platformVersion")},
        "parameters": package.get("parameters", []),
        "assets": manifest_assets,
        "acceptance": acceptance,
    }
    files["solution.yaml"] = yaml.safe_dump(
        manifest, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as target:
        for path in sorted(files):
            entry = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(entry, files[path])
    return archive.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the public ZiZu PV/storage/charging reference package")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    archive = build_archive(arguments.source)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(archive)
    print(f"wrote {arguments.output} ({len(archive)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
