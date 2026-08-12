"""ZiZu 运行时 Secret 的统一安全策略。"""
from __future__ import annotations

import os
import warnings
from typing import Literal


SecretKind = Literal["database", "neuron", "nanomq", "jwt"]

PUBLIC_SECRET_VALUES: dict[SecretKind, frozenset[str]] = {
    "database": frozenset({"omnidev_2026", "zizu_dev", "zizu_dev_2026"}),
    "neuron": frozenset({"0000", "000000", "password", "changeme"}),
    "nanomq": frozenset({"public", "admin", "password", "changeme"}),
    "jwt": frozenset({"zizu-dev-secret-change-in-production"}),
}

INSECURE_DEVELOPMENT_EXAMPLES: dict[SecretKind, str] = {
    "database": "zizu_dev_2026",
    "neuron": "000000",
    "nanomq": "public",
    "jwt": "zizu-dev-secret-change-in-production",
}

SECRET_LABELS: dict[SecretKind, str] = {
    "database": "database password",
    "neuron": "Neuron password",
    "nanomq": "NanoMQ API password",
    "jwt": "JWT secret",
}

INSECURE_DEVELOPMENT_WARNING = (
    "INSECURE DEVELOPMENT MODE: public example credentials are enabled; "
    "never use this configuration for a deployed or reachable system."
)


def insecure_development_enabled(deployment_mode: str, allow_insecure: bool) -> bool:
    """仅允许在显式 development 模式开启公开示例凭据。"""
    if allow_insecure and deployment_mode != "development":
        raise ValueError(
            "ALLOW_INSECURE_DEV_SECRETS requires DEPLOYMENT_MODE=development"
        )
    return deployment_mode == "development" and allow_insecure


def validate_secret(
    kind: SecretKind,
    value: str,
    *,
    allow_insecure: bool = False,
    warn: bool = False,
) -> str:
    """拒绝空白 Secret；公开默认值只能显式用于不安全开发模式。"""
    normalized = value.strip()
    label = SECRET_LABELS[kind]
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    if kind == "jwt" and len(normalized) < 32:
        raise ValueError("JWT secret must contain at least 32 characters")
    if normalized.lower() in PUBLIC_SECRET_VALUES[kind]:
        if not allow_insecure:
            raise ValueError(f"public {label} is forbidden")
        if warn:
            warnings.warn(INSECURE_DEVELOPMENT_WARNING, RuntimeWarning, stacklevel=2)
    return normalized


def require_env(name: str) -> str:
    """读取必需且非空白的运行时环境变量。"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value
