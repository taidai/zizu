"""Transactional PostgreSQL owner of the real-node tree."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from psycopg2.extras import Json

from app.services.alarm_configuration import canonical_digest
from app.services.configuration_revision_postgres import PostgresConfigurationRevisions


NODE_COLUMNS = (
    "id",
    "name",
    "parent_id",
    "layer",
    "node_type",
    "sort_order",
    "enabled",
    "config",
    "source_catalog_key",
    "created_at",
    "updated_at",
    "retired_at",
)
NODE_SELECT = ",".join(NODE_COLUMNS)


class NodeTreeError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(f"{code}: {message or code}")
        self.code = code


def _node(row: tuple[Any, ...]) -> dict[str, Any]:
    value = dict(zip(NODE_COLUMNS, row))
    value["id"] = str(value["id"])
    value["parent_id"] = None if value["parent_id"] is None else str(value["parent_id"])
    for field in ("created_at", "updated_at", "retired_at"):
        if value[field] is not None:
            value[field] = value[field].isoformat()
    return value


class PostgresNodeTree:
    def __init__(self, connection_factory: Callable[[], Any] | None = None) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection_factory = connection_factory
        self._revisions = PostgresConfigurationRevisions()

    def current_revision(self) -> int:
        with self._connection_factory() as connection:
            return self._revisions.current(transaction=connection)

    def list_active(self) -> list[dict[str, Any]]:
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {','.join('node.' + field for field in NODE_COLUMNS)},
                           count(tag.id) AS tag_count
                    FROM t_nodes AS node
                    LEFT JOIN t_tags AS tag
                      ON tag.node_id=node.id AND tag.enabled=TRUE
                    WHERE node.retired_at IS NULL
                    GROUP BY {','.join('node.' + field for field in NODE_COLUMNS)}
                    ORDER BY node.layer,node.sort_order,node.name
                    """
                )
                rows = cursor.fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            item = _node(row[: len(NODE_COLUMNS)])
            item["tag_count"] = int(row[-1])
            values.append(item)
        return values

    def get_active(self, node_id: str | UUID) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {NODE_SELECT} FROM t_nodes WHERE id=%s AND retired_at IS NULL",
                    (str(node_id),),
                )
                row = cursor.fetchone()
        return None if row is None else _node(row)

    def create(
        self,
        *,
        name: str,
        node_type: str,
        parent_id: str | UUID | None,
        config: Mapping[str, Any],
        sort_order: int,
        source_catalog_key: str | None,
        actor: str,
        base_revision: int,
    ) -> dict[str, Any]:
        with self._connection_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    layer = self._parent_layer(cursor, parent_id)
                    cursor.execute(
                        f"""
                        INSERT INTO t_nodes
                          (name,parent_id,layer,node_type,config,sort_order,
                           source_catalog_key,enabled)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)
                        RETURNING {NODE_SELECT}
                        """,
                        (
                            name.strip(),
                            None if parent_id is None else str(parent_id),
                            layer,
                            node_type.strip(),
                            Json(dict(config)),
                            sort_order,
                            source_catalog_key,
                        ),
                    )
                    created = _node(cursor.fetchone())
                revision = self._revisions.publish(
                    transaction=connection,
                    base_revision=base_revision,
                    actor=actor,
                    action="node.create",
                    resource_kind="node",
                    resource_id=created["id"],
                    before_digest=None,
                    after_digest=canonical_digest(created),
                    details={"parent_id": created["parent_id"], "layer": created["layer"]},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"node": created, "configuration_revision": revision}

    def update(
        self,
        *,
        node_id: str | UUID,
        changes: Mapping[str, Any],
        actor: str,
        base_revision: int,
    ) -> dict[str, Any]:
        allowed = {
            "name",
            "node_type",
            "parent_id",
            "config",
            "sort_order",
            "source_catalog_key",
        }
        if not changes or not set(changes).issubset(allowed):
            raise NodeTreeError("NODE_UPDATE_INVALID")
        node_key = str(node_id)
        with self._connection_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {NODE_SELECT} FROM t_nodes WHERE id=%s AND retired_at IS NULL FOR UPDATE",
                        (node_key,),
                    )
                    current_row = cursor.fetchone()
                    if current_row is None:
                        raise NodeTreeError("NODE_NOT_FOUND")
                    before = _node(current_row)
                    if "parent_id" in changes:
                        self._move_subtree(cursor, before, changes["parent_id"])
                    updates: list[str] = []
                    params: list[Any] = []
                    for field in ("name", "node_type", "config", "sort_order", "source_catalog_key"):
                        if field not in changes:
                            continue
                        updates.append(f"{field}=%s")
                        value = changes[field]
                        params.append(Json(dict(value)) if field == "config" else value)
                    if updates:
                        updates.append("updated_at=%s")
                        params.append(datetime.now(timezone.utc))
                        params.append(node_key)
                        cursor.execute(
                            f"UPDATE t_nodes SET {','.join(updates)} WHERE id=%s",
                            params,
                        )
                    cursor.execute(f"SELECT {NODE_SELECT} FROM t_nodes WHERE id=%s", (node_key,))
                    after = _node(cursor.fetchone())
                revision = self._revisions.publish(
                    transaction=connection,
                    base_revision=base_revision,
                    actor=actor,
                    action="node.update",
                    resource_kind="node",
                    resource_id=node_key,
                    before_digest=canonical_digest(before),
                    after_digest=canonical_digest(after),
                    details={"fields": sorted(changes)},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"node": after, "configuration_revision": revision}

    def retire(
        self,
        *,
        node_id: str | UUID,
        actor: str,
        base_revision: int,
    ) -> dict[str, Any]:
        node_key = str(node_id)
        retired_at = datetime.now(timezone.utc)
        with self._connection_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH RECURSIVE subtree AS (
                          SELECT id,layer FROM t_nodes
                          WHERE id=%s AND retired_at IS NULL
                          UNION ALL
                          SELECT child.id,child.layer
                          FROM t_nodes AS child
                          JOIN subtree AS parent ON child.parent_id=parent.id
                          WHERE child.retired_at IS NULL
                        )
                        SELECT id,layer FROM subtree ORDER BY layer,id FOR UPDATE
                        """,
                        (node_key,),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise NodeTreeError("NODE_NOT_FOUND")
                    node_ids = [str(row[0]) for row in rows]
                    before_digest = canonical_digest(node_ids)
                    cursor.execute(
                        """
                        SELECT id
                        FROM t_installed_point_processings
                        WHERE node_id=ANY(%s::uuid[]) AND current=TRUE
                        ORDER BY id
                        FOR UPDATE
                        """,
                        (node_ids,),
                    )
                    installed_ids = [str(row[0]) for row in cursor.fetchall()]
                    cursor.execute(
                        "UPDATE t_entity_instances SET active=FALSE "
                        "WHERE node_id=ANY(%s::uuid[]) AND active=TRUE",
                        (node_ids,),
                    )
                    stopped_entities = cursor.rowcount
                    if installed_ids:
                        cursor.execute(
                            "UPDATE t_installed_point_processings SET current=FALSE "
                            "WHERE id=ANY(%s::uuid[]) AND current=TRUE",
                            (installed_ids,),
                        )
                    cursor.execute(
                        "UPDATE t_tags SET enabled=FALSE WHERE node_id=ANY(%s::uuid[])",
                        (node_ids,),
                    )
                    cursor.execute(
                        """
                        UPDATE t_nodes
                        SET enabled=FALSE,retired_at=%s,retired_by=%s,updated_at=%s
                        WHERE id=ANY(%s::uuid[])
                        """,
                        (retired_at, actor, retired_at, node_ids),
                    )
                after_digest = canonical_digest(
                    {"node_ids": node_ids, "retired_at": retired_at.isoformat()}
                )
                revision = self._revisions.publish(
                    transaction=connection,
                    base_revision=base_revision,
                    actor=actor,
                    action="node.retire",
                    resource_kind="node",
                    resource_id=node_key,
                    before_digest=before_digest,
                    after_digest=after_digest,
                    details={
                        "retired_nodes": len(node_ids),
                        "stopped_point_processings": len(installed_ids),
                        "stopped_entities": stopped_entities,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "retired": node_key,
            "retired_nodes": len(node_ids),
            "configuration_revision": revision,
        }

    @staticmethod
    def _parent_layer(cursor: Any, parent_id: str | UUID | None) -> int:
        if parent_id is None:
            return 1
        cursor.execute(
            "SELECT layer FROM t_nodes WHERE id=%s AND retired_at IS NULL AND enabled=TRUE",
            (str(parent_id),),
        )
        parent = cursor.fetchone()
        if parent is None:
            raise NodeTreeError("NODE_PARENT_NOT_FOUND")
        layer = int(parent[0]) + 1
        if layer > 5:
            raise NodeTreeError("NODE_TREE_TOO_DEEP")
        return layer

    def _move_subtree(
        self,
        cursor: Any,
        current: Mapping[str, Any],
        parent_id: str | UUID | None,
    ) -> None:
        node_id = current["id"]
        cursor.execute(
            """
            WITH RECURSIVE subtree AS (
              SELECT id,layer FROM t_nodes WHERE id=%s
              UNION ALL
              SELECT child.id,child.layer
              FROM t_nodes AS child JOIN subtree AS parent ON child.parent_id=parent.id
              WHERE child.retired_at IS NULL
            )
            SELECT id,layer FROM subtree
            """,
            (node_id,),
        )
        subtree = cursor.fetchall()
        descendants = {str(row[0]) for row in subtree}
        if parent_id is not None and str(parent_id) in descendants:
            raise NodeTreeError("NODE_TREE_CYCLE")
        target_layer = self._parent_layer(cursor, parent_id)
        delta = target_layer - int(current["layer"])
        if max(int(row[1]) + delta for row in subtree) > 5:
            raise NodeTreeError("NODE_TREE_TOO_DEEP")
        node_ids = list(descendants)
        cursor.execute(
            "UPDATE t_nodes SET layer=layer+%s,updated_at=clock_timestamp() WHERE id=ANY(%s::uuid[])",
            (delta, node_ids),
        )
        cursor.execute(
            "UPDATE t_nodes SET parent_id=%s,updated_at=clock_timestamp() WHERE id=%s",
            (None if parent_id is None else str(parent_id), node_id),
        )


__all__ = ["NodeTreeError", "PostgresNodeTree"]
