"""Safe, revisioned maintenance for L0 raw-point identities."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable
from uuid import UUID

from app.services.alarm_configuration import canonical_digest
from app.services.configuration_revision_postgres import PostgresConfigurationRevisions


RAW_POINT_COLUMNS = (
    "id",
    "node_id",
    "name",
    "display_name",
    "enabled",
    "source_path",
)


class RawPointMaintenanceError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(f"{code}: {message or code}")
        self.code = code


def _point(row: tuple[Any, ...]) -> dict[str, Any]:
    value = dict(zip(RAW_POINT_COLUMNS, row))
    value["id"] = str(value["id"])
    value["node_id"] = str(value["node_id"])
    return value


class PostgresRawPointMaintenance:
    def __init__(self, connection_factory: Callable[[], Any] | None = None) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection_factory = connection_factory
        self._revisions = PostgresConfigurationRevisions()

    def current_revision(self) -> int:
        with self._connection_factory() as connection:
            return self._revisions.current(transaction=connection)

    def update(
        self,
        *,
        tag_ids: tuple[str | UUID, ...],
        changes: Mapping[str, Any],
        actor: str,
        base_revision: int,
    ) -> dict[str, Any]:
        normalized_ids = tuple(dict.fromkeys(str(tag_id) for tag_id in tag_ids))
        normalized_changes = dict(changes)
        if not normalized_ids:
            raise RawPointMaintenanceError("RAW_POINT_SELECTION_REQUIRED")
        if not normalized_changes or not set(normalized_changes).issubset(
            {"display_name", "enabled"}
        ):
            raise RawPointMaintenanceError("RAW_POINT_CHANGE_INVALID")
        if "display_name" in normalized_changes:
            display_name = str(normalized_changes["display_name"]).strip()
            if not display_name:
                raise RawPointMaintenanceError("RAW_POINT_DISPLAY_NAME_REQUIRED")
            if len(normalized_ids) != 1:
                raise RawPointMaintenanceError("RAW_POINT_DISPLAY_NAME_SINGLE_ONLY")
            normalized_changes["display_name"] = display_name
        if "enabled" in normalized_changes and not isinstance(
            normalized_changes["enabled"], bool
        ):
            raise RawPointMaintenanceError("RAW_POINT_CHANGE_INVALID")

        with self._connection_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT {','.join(RAW_POINT_COLUMNS)}
                        FROM t_tags
                        WHERE id=ANY(%s::uuid[])
                        ORDER BY id
                        FOR UPDATE
                        """,
                        (list(normalized_ids),),
                    )
                    before = [_point(row) for row in cursor.fetchall()]
                    if len(before) != len(normalized_ids):
                        raise RawPointMaintenanceError("RAW_POINT_NOT_FOUND")
                    if normalized_changes.get("enabled") is False:
                        cursor.execute(
                            """
                            SELECT DISTINCT binding.l0_tag_id
                            FROM t_point_processing_input_bindings AS binding
                            JOIN t_installed_point_processings AS installed
                              ON installed.id=binding.installed_processing_id
                             AND installed.current=TRUE
                            WHERE binding.source_kind='l0'
                              AND binding.l0_tag_id=ANY(%s::uuid[])
                            ORDER BY binding.l0_tag_id
                            """,
                            (list(normalized_ids),),
                        )
                        used = [str(row[0]) for row in cursor.fetchall()]
                        if used:
                            raise RawPointMaintenanceError(
                                "RAW_POINT_IN_USE",
                                "先修改或停用引用这些点位的当前加工，再停用原始点位",
                            )
                    assignments: list[str] = []
                    params: list[Any] = []
                    for field in ("display_name", "enabled"):
                        if field in normalized_changes:
                            assignments.append(f"{field}=%s")
                            params.append(normalized_changes[field])
                    params.append(list(normalized_ids))
                    cursor.execute(
                        f"""
                        UPDATE t_tags
                        SET {','.join(assignments)}
                        WHERE id=ANY(%s::uuid[])
                        """,
                        params,
                    )
                    cursor.execute(
                        f"""
                        SELECT {','.join(RAW_POINT_COLUMNS)}
                        FROM t_tags WHERE id=ANY(%s::uuid[]) ORDER BY id
                        """,
                        (list(normalized_ids),),
                    )
                    after = [_point(row) for row in cursor.fetchall()]
                revision = self._revisions.publish(
                    transaction=connection,
                    base_revision=base_revision,
                    actor=actor,
                    action="raw_point.update",
                    resource_kind="raw_point_batch",
                    resource_id=canonical_digest(normalized_ids),
                    before_digest=canonical_digest(before),
                    after_digest=canonical_digest(after),
                    details={
                        "tag_ids": list(normalized_ids),
                        "fields": sorted(normalized_changes),
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "updated": len(after),
            "configuration_revision": revision,
            "items": after,
        }


__all__ = ["PostgresRawPointMaintenance", "RawPointMaintenanceError"]
