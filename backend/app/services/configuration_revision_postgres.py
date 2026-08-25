"""PostgreSQL transaction helper for internal configuration revisions."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from psycopg2.extras import Json

from app.services.configuration_revision import (
    ConfigurationRevisionError,
    validate_configuration_publish,
)


class PostgresConfigurationRevisions:
    def current(self, transaction: Any | None = None) -> int:
        if transaction is not None:
            with transaction.cursor() as cursor:
                cursor.execute(
                    "SELECT current_revision FROM t_configuration_state "
                    "WHERE singleton=TRUE"
                )
                row = cursor.fetchone()
        else:
            from app.services.telemetry_store import get_connection

            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT current_revision FROM t_configuration_state "
                        "WHERE singleton=TRUE"
                    )
                    row = cursor.fetchone()
        if row is None:
            raise ConfigurationRevisionError(
                "CONFIGURATION_REVISION_UNAVAILABLE",
                "Configuration revision state is missing",
            )
        return int(row[0])

    def publish(
        self,
        *,
        transaction: Any,
        base_revision: int,
        actor: str,
        action: str,
        resource_kind: str,
        resource_id: str,
        before_digest: str | None,
        after_digest: str,
        details: Mapping[str, Any],
    ) -> int:
        validate_configuration_publish(
            base_revision=base_revision,
            actor=actor,
            action=action,
            resource_kind=resource_kind,
            resource_id=resource_id,
            before_digest=before_digest,
            after_digest=after_digest,
            details=details,
        )
        with transaction.cursor() as cursor:
            cursor.execute(
                "SELECT current_revision FROM t_configuration_state "
                "WHERE singleton=TRUE FOR UPDATE"
            )
            row = cursor.fetchone()
            if row is None or int(row[0]) != base_revision:
                raise ConfigurationRevisionError(
                    "CONFIGURATION_REVISION_STALE",
                    "Configuration changed after preview",
                )
            revision = base_revision + 1
            audit_id = uuid5(
                NAMESPACE_URL,
                f"zizu/configuration-audit/{revision}/{resource_kind}/{resource_id}/{after_digest}",
            )
            cursor.execute(
                """
                INSERT INTO t_configuration_revisions
                  (revision, previous_revision, actor, action, resource_kind,
                   resource_id, before_digest, after_digest, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    revision,
                    base_revision,
                    actor.strip(),
                    action.strip(),
                    resource_kind.strip(),
                    resource_id.strip(),
                    before_digest,
                    after_digest,
                    Json(dict(details)),
                ),
            )
            cursor.execute(
                """
                INSERT INTO t_configuration_audit
                  (id, configuration_revision, actor, action, resource_kind,
                   resource_id, before_digest, after_digest, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(audit_id),
                    revision,
                    actor.strip(),
                    action.strip(),
                    resource_kind.strip(),
                    resource_id.strip(),
                    before_digest,
                    after_digest,
                    Json(dict(details)),
                ),
            )
            cursor.execute(
                "UPDATE t_configuration_state SET current_revision=%s "
                "WHERE singleton=TRUE",
                (revision,),
            )
        return revision


__all__ = ["PostgresConfigurationRevisions"]
