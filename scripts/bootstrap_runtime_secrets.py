#!/usr/bin/env python3
"""为 ZiZu 新部署生成不进入 Git 的运行时 Secret 配置。"""
from __future__ import annotations

import argparse
import json
import secrets
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSECURE_NANOMQ_PASSWORDS = {"", "public", "admin", "password", "changeme"}


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.split(" #", 1)[0].strip()
    return values


def replace_env_value(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    replacement = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    return "\n".join(lines) + "\n"


def secure_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows ACL 不由 chmod 完整表达；文件仍位于已忽略的 runtime 目录。
        pass


def bootstrap(env_file: Path, secret_file: Path, rotate: bool) -> str:
    if env_file.exists():
        env_text = env_file.read_text(encoding="utf-8")
    else:
        env_text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    env = parse_env(env_text)
    current = env.get("NANOMQ_API_PASSWORD", "")
    insecure = current.lower() in INSECURE_NANOMQ_PASSWORDS
    if env_file.exists() and insecure and not rotate:
        raise RuntimeError(
            "Existing .env has a missing or public NanoMQ API password. "
            "Coordinate the broker/backend restart, then rerun with --rotate."
        )

    password = secrets.token_urlsafe(32) if rotate or insecure else current
    username = env.get("NANOMQ_API_USERNAME", "admin") or "admin"
    env_text = replace_env_value(env_text, "NANOMQ_API_USERNAME", username)
    env_text = replace_env_value(env_text, "NANOMQ_API_PASSWORD", password)

    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(env_text, encoding="utf-8")
    secure_file(env_file)

    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(
        f"username = {json.dumps(username)}\npassword = {json.dumps(password)}\n",
        encoding="utf-8",
    )
    secure_file(secret_file)
    return "rotated" if rotate else "ready"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument(
        "--nanomq-secret-file",
        type=Path,
        default=REPO_ROOT / "config/runtime/nanomq-http.conf",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Replace an existing/default NanoMQ API password after coordinating restart.",
    )
    args = parser.parse_args()
    result = bootstrap(args.env_file, args.nanomq_secret_file, args.rotate)
    print(f"Runtime secrets {result}; no secret values were printed.")


if __name__ == "__main__":
    main()
