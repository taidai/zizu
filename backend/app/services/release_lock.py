"""Read the deployment-owned, immutable release lock without exposing registry paths."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.telemetry_store import get_connection


def _digest(image_reference: str) -> str:
    """Keep the content identity while omitting a potentially private registry name."""
    _name, separator, digest = image_reference.partition("@")
    return digest if separator else ""


class PostgresReleaseLockRepository:
    """The web process may read locks but never creates or edits them."""

    def current_summary(self) -> dict[str, Any]:
        try:
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, platform_version, platform_image, edge_proxy_image, architecture,
                               schema_version, site_configuration_version,
                               package_id, package_version, package_digest, generated_at
                        FROM t_release_locks
                        ORDER BY generated_at DESC, id DESC
                        LIMIT 1
                        """
                    )
                    row = cursor.fetchone()
        except Exception:
            return {"status": "unavailable"}
        if row is None:
            return {"status": "missing"}
        (
            lock_id,
            platform_version,
            platform_image,
            edge_proxy_image,
            architecture,
            schema_version,
            site_configuration_version,
            package_id,
            package_version,
            package_digest,
            generated_at,
        ) = row
        return {
            "status": "locked",
            "id": str(lock_id),
            "platform_version": platform_version,
            "architecture": architecture,
            "schema_version": schema_version,
            "site_configuration_version": site_configuration_version,
            "package": (
                {"id": package_id, "version": package_version, "digest": package_digest}
                if package_id is not None and package_version is not None and package_digest is not None
                else None
            ),
            "image_digests": {
                "platform": _digest(platform_image),
                "edge_proxy": _digest(edge_proxy_image),
            },
            "generated_at": generated_at.isoformat() if isinstance(generated_at, datetime) else str(generated_at),
        }


_repository = PostgresReleaseLockRepository()


def current_release_lock_summary() -> dict[str, Any]:
    return _repository.current_summary()
