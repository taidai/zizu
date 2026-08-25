"""Internal configuration revision contract shared by configuration publishers."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ConfigurationRevisionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def validate_configuration_publish(
    *,
    base_revision: int,
    actor: str,
    action: str,
    resource_kind: str,
    resource_id: str,
    before_digest: str | None,
    after_digest: str,
    details: Mapping[str, Any],
) -> None:
    if base_revision < 0:
        raise ConfigurationRevisionError(
            "CONFIGURATION_REVISION_INVALID", "Base revision must be non-negative"
        )
    for name, value in (
        ("actor", actor),
        ("action", action),
        ("resource_kind", resource_kind),
        ("resource_id", resource_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationRevisionError(
                "CONFIGURATION_REVISION_INVALID", f"{name} is required"
            )
    if not _DIGEST.fullmatch(after_digest) or (
        before_digest is not None and not _DIGEST.fullmatch(before_digest)
    ):
        raise ConfigurationRevisionError(
            "CONFIGURATION_REVISION_INVALID", "Configuration digests must be SHA-256"
        )
    if not isinstance(details, Mapping):
        raise ConfigurationRevisionError(
            "CONFIGURATION_REVISION_INVALID", "details must be a mapping"
        )


__all__ = ["ConfigurationRevisionError", "validate_configuration_publish"]
