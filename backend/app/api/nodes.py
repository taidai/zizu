"""
ZiZu Nodes API — 节点树全生命周期管理

GET    /api/v1/nodes            -> 节点列表（含 tag 数量统计）
POST   /api/v1/nodes            -> 创建节点
GET    /api/v1/nodes/{id}/tree  -> 递归子树
PUT    /api/v1/nodes/{id}       -> 更新节点（支持移动父节点/层级校验）
DELETE /api/v1/nodes/{id}       -> 级联删除子孙节点
GET    /api/v1/nodes/export     -> 导出 YAML
"""
from __future__ import annotations

import yaml
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg2.extras import Json
from loguru import logger
from pydantic import BaseModel, Field

from app.models.schemas import NodeCreate
from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    RUNTIME_READ,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.identity import Principal

router = APIRouter()

# 级联深度上限 (G3 审查 R2) — 防循环依赖 / 超深树
MAX_CASCADE_DEPTH = 5


class NodeUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    node_type: str | None = Field(None, min_length=1, max_length=100)
    parent_id: UUID | None = None
    layer: int | None = Field(None, ge=1, le=MAX_CASCADE_DEPTH)
    sort_order: int | None = None
    enabled: bool | None = None
    config: dict | None = None


def _serialize_node(row: dict) -> dict:
    row = dict(row)
    row["id"] = str(row["id"])
    if row.get("parent_id"):
        row["parent_id"] = str(row["parent_id"])
    else:
        row["parent_id"] = None
    for field in ("created_at", "updated_at"):
        if row.get(field) and hasattr(row[field], "isoformat"):
            row[field] = row[field].isoformat()
    return row


def _runtime_node(row: dict, principal: Principal) -> dict:
    serialized = _serialize_node(row)
    if principal.role == "operator":
        serialized.pop("config", None)
    return serialized


@router.get(
    "/nodes",
    openapi_extra=capability_metadata(RUNTIME_READ),
)
async def list_nodes(
    layer: int | None = Query(None, description="按层级过滤 1=Site 2=Station 3=EnergyNode 4=Device 5=Tag"),
    enabled: bool = Query(True, description="只看启用节点"),
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
) -> dict:
    """
    返回所有节点列表，含每个节点下的 tag 数量。

    用于前端树形结构展示 + 点位管理页的节点下拉选择。
    """
    from app.services.telemetry_store import get_connection

    conditions = []
    params: list = []

    if layer is not None:
        conditions.append("n.layer = %s")
        params.append(layer)
    if enabled:
        conditions.append("n.enabled = TRUE")

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
    SELECT
        n.id,
        n.name,
        n.parent_id,
        n.layer,
        n.node_type,
        n.sort_order,
        n.enabled,
        n.config,
        n.created_at,
        COUNT(t.id) AS tag_count
    FROM t_nodes n
    LEFT JOIN t_tags t ON t.node_id = n.id AND t.enabled = TRUE
    {where}
    GROUP BY n.id, n.name, n.parent_id, n.layer, n.node_type, n.sort_order, n.enabled, n.config, n.created_at
    ORDER BY n.layer, n.sort_order, n.name
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]

        return {
            "nodes": [_runtime_node(r, principal) for r in rows],
            "total": len(rows),
        }
    except Exception as e:
        logger.error("[API/nodes] Query failed: {}", e)
        return {"nodes": [], "total": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# 导入 / 导出 YAML — 注意: 静态路径须先于 /nodes/{node_id} 注册，
# 否则 "export"/"import" 会被当作 UUID 路径参数解析
# ---------------------------------------------------------------------------
@router.get("/nodes/export", **protected(CONFIGURATION_READ))
async def export_nodes() -> Response:
    """
    导出整棵节点树为 YAML。每个节点含其挂载的 tags（点位）。

    结构: 顶层为 Site 列表，children 递归嵌套；tags 内联在各节点下。
    可用于备份 / 迁移 / 版本管理。
    """
    from app.services.telemetry_store import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, parent_id, layer, node_type, config, sort_order, enabled
                    FROM t_nodes WHERE enabled = TRUE
                    ORDER BY layer, sort_order, name
                    """
                )
                ncols = [desc[0] for desc in cur.description]
                nodes = {str(r[0]): dict(zip(ncols, r)) for r in cur.fetchall()}

                cur.execute(
                    """
                    SELECT node_id, name, display_name, data_type, tag_type, unit,
                           source_type, source_path, scale_factor, value_offset,
                           formula, formula_type, aggregate_fn, read_write, sort_order
                    FROM t_tags WHERE enabled = TRUE
                    ORDER BY sort_order, name
                    """
                )
                tcols = [desc[0] for desc in cur.description]
                tags_by_node: dict[str | None, list] = {}
                for r in cur.fetchall():
                    tag = dict(zip(tcols, r))
                    nid = str(tag.pop("node_id"))
                    tag = {k: v for k, v in tag.items() if v is not None}
                    tags_by_node.setdefault(nid, []).append(tag)

        children_map: dict[str | None, list] = {}
        for nid, n in nodes.items():
            pid = str(n["parent_id"]) if n["parent_id"] else None
            children_map.setdefault(pid, []).append(nid)

        def _node_yaml(nid: str) -> dict:
            n = nodes[nid]
            out: dict = {"name": n["name"], "layer": n["layer"]}
            if n["node_type"]:
                out["node_type"] = n["node_type"]
            if n["config"]:
                out["config"] = n["config"]
            if tags_by_node.get(nid):
                out["tags"] = tags_by_node[nid]
            kids = [_node_yaml(c) for c in children_map.get(nid, [])]
            if kids:
                out["children"] = kids
            return out

        roots = [_node_yaml(nid) for nid in children_map.get(None, [])]
        doc = {"version": 1, "nodes": roots}
        text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)
        return Response(
            content=text,
            media_type="application/x-yaml",
            headers={"Content-Disposition": "attachment; filename=node_tree.yaml"},
        )
    except Exception as e:
        logger.error("[API/nodes] Export failed: {}", e)
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")


@router.get(
    "/nodes/{node_id}",
    openapi_extra=capability_metadata(RUNTIME_READ),
)
async def get_node(
    node_id: UUID,
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
) -> dict:
    """获取单个节点详情（含其 tags 列表）。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Node info
            cur.execute(
                "SELECT id, name, parent_id, layer, node_type, sort_order, enabled, config, created_at "
                "FROM t_nodes WHERE id = %s",
                (node_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "Node not found"}

            node = _runtime_node(
                dict(
                    zip(
                        ["id", "name", "parent_id", "layer", "node_type", "sort_order", "enabled", "config", "created_at"],
                        row,
                    )
                ),
                principal,
            )

            # Tags under this node
            cur.execute(
                "SELECT id, name, display_name, data_type, tag_type, unit, "
                "scale_factor, value_offset, source_path, read_write, enabled "
                "FROM t_tags WHERE node_id = %s AND enabled = TRUE "
                "ORDER BY sort_order, name",
                (node_id,),
            )
            tag_columns = [desc[0] for desc in cur.description]
            tags = []
            for r in cur.fetchall():
                tag = dict(zip(tag_columns, r))
                tag["id"] = str(tag["id"])
                if principal.role == "operator":
                    for field in ("scale_factor", "value_offset", "source_path"):
                        tag.pop(field, None)
                tags.append(tag)

    return {"node": node, "tags": tags}


@router.put(
    "/nodes/{node_id}",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
async def update_node(
    node_id: UUID,
    req: NodeUpdateRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
) -> dict:
    """更新节点（部分更新），支持改名、改配置、移动父节点。"""
    from app.services.telemetry_store import get_connection

    data = req.model_dump(exclude_none=True)
    parent_id = data.get("parent_id")
    layer = data.get("layer")

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 当前节点存在性
            cur.execute(
                "SELECT id, parent_id, layer FROM t_nodes WHERE id = %s",
                (node_id,),
            )
            current = cur.fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Node not found")

            # 如果要移动父节点，校验层级并防成环
            if parent_id is not None:
                if str(parent_id) == str(node_id):
                    raise HTTPException(status_code=400, detail="cannot move node under itself")

                cur.execute("SELECT layer FROM t_nodes WHERE id = %s", (parent_id,))
                parent_row = cur.fetchone()
                if not parent_row:
                    raise HTTPException(status_code=404, detail="Parent node not found")
                expected_layer = parent_row[0] + 1
                if layer is not None and layer != expected_layer:
                    raise HTTPException(
                        status_code=400,
                        detail=f"layer must be {expected_layer} under selected parent",
                    )
                if layer is None:
                    layer = expected_layer
                    data["layer"] = layer

                # 防成环：目标父节点不能是当前节点的子孙
                cur.execute(
                    """
                    WITH RECURSIVE descendants AS (
                        SELECT id, parent_id FROM t_nodes WHERE id = %s
                        UNION ALL
                        SELECT n.id, n.parent_id
                        FROM t_nodes n
                        JOIN descendants d ON n.parent_id = d.id
                    )
                    SELECT 1 FROM descendants WHERE id = %s
                    """,
                    (node_id, parent_id),
                )
                if cur.fetchone():
                    raise HTTPException(status_code=400, detail="cannot move node under its own descendant")
            elif layer is not None:
                # 只改层级不改父节点：仅允许与当前父节点的期望层级一致
                cur.execute("SELECT layer FROM t_nodes WHERE id = %s", (current[1],))
                parent_row = cur.fetchone()
                expected_layer = (parent_row[0] + 1) if parent_row else 1
                if layer != expected_layer:
                    raise HTTPException(
                        status_code=400,
                        detail=f"layer must be {expected_layer} under current parent",
                    )

            updates = []
            params: list = []
            for field, value in data.items():
                if field == "config":
                    updates.append("config = %s")
                    params.append(Json(value))
                else:
                    updates.append(f"{field} = %s")
                    params.append(value)

            if not updates:
                return await get_node(node_id, principal)

            updates.append("updated_at = %s")
            params.append(datetime.now(timezone.utc))
            params.append(node_id)

            query = (
                f"UPDATE t_nodes SET {', '.join(updates)} WHERE id = %s "
                "RETURNING id, name, parent_id, layer, node_type, sort_order, enabled, config, created_at, updated_at"
            )
            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Node not found")
            conn.commit()
            columns = [desc[0] for desc in cur.description]
            return {"node": _serialize_node(dict(zip(columns, row)))}


@router.post("/nodes", **protected(CONFIGURATION_WRITE))
async def create_node(req: NodeCreate) -> dict:
    """创建节点，自动校验层级与父节点关系。"""
    from app.services.telemetry_store import get_connection

    parent_id = req.parent_id
    layer = req.layer

    with get_connection() as conn:
        with conn.cursor() as cur:
            if parent_id is not None:
                cur.execute("SELECT layer FROM t_nodes WHERE id = %s", (parent_id,))
                parent_row = cur.fetchone()
                if not parent_row:
                    raise HTTPException(status_code=404, detail="Parent node not found")
                expected_layer = parent_row[0] + 1
                if layer != expected_layer:
                    raise HTTPException(
                        status_code=400,
                        detail=f"layer must be {expected_layer} under selected parent",
                    )
            else:
                if layer != 1:
                    raise HTTPException(status_code=400, detail="root node layer must be 1")

            cur.execute(
                """
                INSERT INTO t_nodes (name, parent_id, layer, node_type, config, sort_order, enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, name, parent_id, layer, node_type, sort_order, enabled, config, created_at, updated_at
                """,
                (
                    req.name,
                    parent_id,
                    layer,
                    req.node_type or "",
                    Json(req.config or {}),
                    req.sort_order or 0,
                    req.enabled,
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                ),
            )
            row = cur.fetchone()
            conn.commit()
            columns = [desc[0] for desc in cur.description]
            return {"node": _serialize_node(dict(zip(columns, row)))}


@router.get("/nodes/{node_id}/tree", **protected(RUNTIME_READ))
async def get_node_tree(node_id: UUID) -> dict:
    """获取以 node_id 为根的递归子树（含 tag_count）。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE descendants AS (
                    SELECT id, name, parent_id, layer, node_type, sort_order, enabled
                    FROM t_nodes WHERE id = %s
                    UNION ALL
                    SELECT n.id, n.name, n.parent_id, n.layer, n.node_type, n.sort_order, n.enabled
                    FROM t_nodes n
                    JOIN descendants d ON n.parent_id = d.id
                )
                SELECT d.id, d.name, d.parent_id, d.layer, d.node_type, d.sort_order, d.enabled,
                       COUNT(t.id) AS tag_count
                FROM descendants d
                LEFT JOIN t_tags t ON t.node_id = d.id AND t.enabled = TRUE
                GROUP BY d.id, d.name, d.parent_id, d.layer, d.node_type, d.sort_order, d.enabled
                ORDER BY d.layer, d.sort_order, d.name
                """,
                (node_id,),
            )
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    node_map = {str(r["id"]): _serialize_node(r) for r in rows}
    children_map: dict[str | None, list] = {str(node_id): []}
    for r in rows:
        nid = str(r["id"])
        pid = str(r["parent_id"]) if r.get("parent_id") else str(node_id)
        children_map.setdefault(pid, [])
        if nid != str(node_id):
            children_map.setdefault(pid, []).append(nid)

    def build(nid: str) -> dict:
        n = node_map[nid]
        return {
            **n,
            "children": [build(cid) for cid in children_map.get(nid, [])],
        }

    return {"tree": build(str(node_id))}


@router.delete("/nodes/{node_id}", **protected(CONFIGURATION_WRITE))
async def delete_node(node_id: UUID) -> dict:
    """删除节点及其所有子孙节点（级联删除挂载的 tags、快照、遥测、告警）。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1) 收集待删除节点及其所有子孙
            cur.execute(
                """
                WITH RECURSIVE descendants AS (
                    SELECT id, parent_id, layer FROM t_nodes WHERE id = %s
                    UNION ALL
                    SELECT n.id, n.parent_id, n.layer
                    FROM t_nodes n
                    JOIN descendants d ON n.parent_id = d.id
                )
                SELECT id, layer FROM descendants ORDER BY layer DESC
                """,
                (node_id,),
            )
            rows = cur.fetchall()
            if not rows:
                raise HTTPException(status_code=404, detail="Node not found")
            node_ids = [r[0] for r in rows]

            # 2) 先删除关联告警：t_alarms 对 t_nodes/t_tags 无外键级联，
            #    必须先清掉，否则删除节点时会触发外键约束 500 错误。
            node_placeholders = ",".join(["%s"] * len(node_ids))
            cur.execute(
                f"""
                DELETE FROM t_alarms
                WHERE node_id IN ({node_placeholders})
                   OR tag_id IN (
                       SELECT id FROM t_tags WHERE node_id IN ({node_placeholders})
                   )
                """,
                node_ids + node_ids,
            )
            alarms_deleted = cur.rowcount

            # 3) 删除节点：t_tags/t_telemetry/t_telemetry_latest
            #    均已设置 ON DELETE CASCADE（t_node_snapshot 已移除）
            cur.execute(
                f"DELETE FROM t_nodes WHERE id IN ({node_placeholders})",
                node_ids,
            )
            deleted_count = cur.rowcount
            conn.commit()

    logger.info(
        "[API/nodes] deleted node {} and {} descendants, {} alarms",
        node_id,
        deleted_count - 1,
        alarms_deleted,
    )
    return {"deleted": str(node_id), "cascade_nodes": deleted_count, "alarms_deleted": alarms_deleted}
