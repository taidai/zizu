#!/usr/bin/env python3
"""为 ZiZu 新部署生成不进入 Git 的运行时 Secret 配置。"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.secret_policy import PUBLIC_SECRET_VALUES, validate_secret


NANOMQ_TEMPLATE = REPO_ROOT / "config" / "nanomq.conf"


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


def _stage_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline=None,
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    staged = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        secure_file(staged)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _restore_file(path: Path, original: bytes | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
        return
    staged = _stage_text(path, original.decode("utf-8"))
    os.replace(staged, path)


def write_runtime_pair(
    env_file: Path,
    env_text: str,
    secret_file: Path,
    secret_text: str,
) -> None:
    """先完整暂存两份配置，替换失败时恢复，避免凭据分叉。"""
    originals = {
        env_file: env_file.read_bytes() if env_file.exists() else None,
        secret_file: secret_file.read_bytes() if secret_file.exists() else None,
    }
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        staged[env_file] = _stage_text(env_file, env_text)
        staged[secret_file] = _stage_text(secret_file, secret_text)
        for target in (env_file, secret_file):
            os.replace(staged[target], target)
            replaced.append(target)
            secure_file(target)
    except Exception:
        for target in reversed(replaced):
            _restore_file(target, originals[target])
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def render_nanomq_config(username: str, password: str) -> str:
    template = NANOMQ_TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "__ZIZU_NANOMQ_API_USERNAME_JSON__": json.dumps(username),
        "__ZIZU_NANOMQ_API_PASSWORD_JSON__": json.dumps(password),
    }
    for marker, value in replacements.items():
        if template.count(marker) != 1:
            raise RuntimeError(f"NanoMQ template marker must occur once: {marker}")
        template = template.replace(marker, value)
    return template


def bootstrap(
    env_file: Path,
    secret_file: Path,
    rotate: bool,
    neuron_password: str | None = None,
) -> str:
    existing = env_file.exists()
    if existing:
        env_text = env_file.read_text(encoding="utf-8")
    else:
        env_text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    env = parse_env(env_text)
    current_nanomq = env.get("NANOMQ_API_PASSWORD", "")
    current_db = env.get("DB_PASSWORD", "")
    current_db_owner = env.get("DB_OWNER_PASSWORD", "")
    current_neuron = env.get("NEURON_PASSWORD", "")
    current_jwt = env.get("JWT_SECRET", "")

    if existing and (
        not current_db.strip()
        or current_db.strip().lower() in PUBLIC_SECRET_VALUES["database"]
    ):
        raise RuntimeError(
            "Existing .env has a missing or public database password. "
            "Rotate the database role first, then update DB_PASSWORD."
        )
    if existing and (
        not current_jwt.strip()
        or current_jwt.strip().lower() in PUBLIC_SECRET_VALUES["jwt"]
    ):
        raise RuntimeError(
            "Existing .env has a missing or public JWT secret. "
            "Rotate active sessions, then update JWT_SECRET."
        )
    if existing:
        try:
            current_db = validate_secret("database", current_db)
            current_jwt = validate_secret("jwt", current_jwt)
            if current_db_owner.strip():
                current_db_owner = validate_secret("database", current_db_owner)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
    if existing and neuron_password is None and (
        not current_neuron.strip()
        or current_neuron.strip().lower() in PUBLIC_SECRET_VALUES["neuron"]
    ):
        raise RuntimeError(
            "Existing .env has a missing or public Neuron password. "
            "Rotate Neuron first, then rerun with --update-neuron."
        )
    if not existing and (
        neuron_password is None
        or not neuron_password.strip()
        or neuron_password.strip().lower() in PUBLIC_SECRET_VALUES["neuron"]
    ):
        raise RuntimeError(
            "A rotated Neuron password is required before creating a deployment."
        )
    if neuron_password is not None:
        try:
            neuron_password = validate_secret("neuron", neuron_password)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    insecure_nanomq = (
        not current_nanomq.strip()
        or current_nanomq.strip().lower() in PUBLIC_SECRET_VALUES["nanomq"]
    )
    if existing and insecure_nanomq and not rotate:
        raise RuntimeError(
            "Existing .env has a missing or public NanoMQ API password. "
            "Coordinate the broker/backend restart, then rerun with --rotate."
        )

    db_password = secrets.token_urlsafe(32) if not existing else current_db
    db_owner_password = secrets.token_urlsafe(32) if not existing else current_db_owner
    jwt_secret = secrets.token_urlsafe(48) if not existing else current_jwt
    resolved_neuron = neuron_password or current_neuron
    password = (
        secrets.token_urlsafe(32)
        if rotate or insecure_nanomq
        else current_nanomq
    )
    username = env.get("NANOMQ_API_USERNAME", "admin") or "admin"
    env_text = replace_env_value(env_text, "DB_PASSWORD", db_password)
    env_text = replace_env_value(env_text, "DB_OWNER_PASSWORD", db_owner_password)
    env_text = replace_env_value(env_text, "NEURON_PASSWORD", resolved_neuron)
    env_text = replace_env_value(env_text, "NANOMQ_API_USERNAME", username)
    env_text = replace_env_value(env_text, "NANOMQ_API_PASSWORD", password)
    env_text = replace_env_value(env_text, "JWT_SECRET", jwt_secret)

    write_runtime_pair(
        env_file,
        env_text,
        secret_file,
        render_nanomq_config(username, password),
    )
    return "rotated" if rotate else "ready"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument(
        "--nanomq-secret-file",
        type=Path,
        default=REPO_ROOT / "config/runtime/nanomq.conf",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Replace an existing/default NanoMQ API password after coordinating restart.",
    )
    parser.add_argument(
        "--update-neuron",
        action="store_true",
        help="Prompt for the already-rotated Neuron password without exposing it in argv.",
    )
    args = parser.parse_args()
    neuron_password = None
    if not args.env_file.exists() or args.update_neuron:
        neuron_password = getpass.getpass("Rotated Neuron password: ")
    result = bootstrap(
        args.env_file,
        args.nanomq_secret_file,
        args.rotate,
        neuron_password=neuron_password,
    )
    print(f"Runtime secrets {result}; no secret values were printed.")


if __name__ == "__main__":
    main()
