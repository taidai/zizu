"""
ZiZu 版本号管理脚本。

用法：
    python scripts/bump_version.py           # 默认递增 patch
    python scripts/bump_version.py minor     # 递增 minor
    python scripts/bump_version.py major     # 递增 major

行为：
    1. 校验所有版本来源一致。
    2. 按十进制进位规则递增指定段位（0.4.9 -> 0.5.0）。
    3. 同步更新前端 package/lock、后端 pyproject 与运行时版本文件。
    4. 打印新版本。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
PACKAGE_JSON = ROOT / "frontend" / "package.json"
PACKAGE_LOCK_JSON = ROOT / "frontend" / "package-lock.json"
PYPROJECT_TOML = ROOT / "backend" / "pyproject.toml"

def version_files() -> list[Path]:
    return [VERSION_FILE, ROOT / "backend" / "app" / "VERSION"]

def read_version() -> tuple[int, int, int]:
    files = version_files()
    missing = [str(vf) for vf in files if not vf.exists()]
    if missing:
        raise FileNotFoundError(f"VERSION file not found: {', '.join(missing)}")
    values = {vf: vf.read_text(encoding="utf-8").strip() for vf in files}
    if len(set(values.values())) != 1:
        details = ", ".join(f"{vf}={value}" for vf, value in values.items())
        raise ValueError(f"VERSION files disagree: {details}")
    text = next(iter(values.values()))
    parts = text.split(".")
    if len(parts) != 3:
        raise ValueError(f"VERSION must be in MAJOR.MINOR.PATCH format, got: {text}")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def bump(major: int, minor: int, patch: int, level: str) -> tuple[int, int, int]:
    if level == "major":
        return major + 1, 0, 0
    if level == "minor":
        minor += 1
        if minor >= 10:
            return major + 1, 0, 0
        return major, minor, 0
    if level == "patch":
        patch += 1
        if patch >= 10:
            minor += 1
            patch = 0
        if minor >= 10:
            major += 1
            minor = 0
        return major, minor, patch
    raise ValueError(f"Unknown bump level: {level}")


def write_version(version: tuple[int, int, int]) -> str:
    version_str = ".".join(str(v) for v in version)
    for vf in version_files():
        vf.parent.mkdir(parents=True, exist_ok=True)
        vf.write_text(version_str + "\n", encoding="utf-8")
    return version_str


def update_package_json(version: str) -> None:
    if not PACKAGE_JSON.exists():
        return
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    data["version"] = version
    PACKAGE_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if PACKAGE_LOCK_JSON.exists():
        lock_data = json.loads(PACKAGE_LOCK_JSON.read_text(encoding="utf-8"))
        lock_data["version"] = version
        root_package = lock_data.get("packages", {}).get("")
        if isinstance(root_package, dict):
            root_package["version"] = version
        PACKAGE_LOCK_JSON.write_text(
            json.dumps(lock_data, indent=2) + "\n", encoding="utf-8"
        )


def update_pyproject_toml(version: str) -> None:
    if not PYPROJECT_TOML.exists():
        return
    text = PYPROJECT_TOML.read_text(encoding="utf-8")
    text = re.sub(r'^(version\s*=\s*")[^"]+(".*)$', rf'\g<1>{version}\g<2>', text, flags=re.MULTILINE)
    PYPROJECT_TOML.write_text(text, encoding="utf-8")


def main() -> None:
    level = sys.argv[1] if len(sys.argv) > 1 else "patch"
    if level not in {"major", "minor", "patch"}:
        print(f"Usage: {sys.argv[0]} [major|minor|patch]", file=sys.stderr)
        sys.exit(1)

    current = read_version()
    new = bump(*current, level)
    version_str = write_version(new)
    update_package_json(version_str)
    update_pyproject_toml(version_str)
    print(version_str)


if __name__ == "__main__":
    main()
