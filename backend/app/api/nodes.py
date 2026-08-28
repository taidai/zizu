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
import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

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
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=200)
    node_type: str | None = Field(None, min_length=1, max_length=100)
    parent_id: UUID | None = None
    sort_order: int | None = None
    config: dict | None = None
    source_catalog_key: str | None = Field(None, min_length=1, max_length=128)


class NodeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    node_type: str = Field(min_length=1, max_length=100)
    parent_id: UUID | None = None
    sort_order: int = 0
    config: dict = Field(default_factory=dict)
    source_catalog_key: str | None = Field(None, min_length=1, max_length=128)


def get_node_tree_repository():
    from app.services.node_tree_postgres import PostgresNodeTree

    return PostgresNodeTree()


def get_node_tree_runtime():
    from app.main import get_pipeline

    runtime = get_pipeline()
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATA_TRUNK_UNAVAILABLE", "message": "数据主干尚未启动"},
        )
    return runtime


def _raise_node_tree_http(error: Exception) -> None:
    from app.services.configuration_revision import ConfigurationRevisionError
    from app.services.data_trunk_contracts import DataTrunkError
    from app.services.node_tree_postgres import NodeTreeError

    if isinstance(error, (NodeTreeError, ConfigurationRevisionError, DataTrunkError)):
        code = error.code
    else:
        raise error
    status_by_code = {
        "NODE_NOT_FOUND": 404,
        "NODE_PARENT_NOT_FOUND": 404,
        "NODE_TREE_CYCLE": 409,
        "NODE_TREE_TOO_DEEP": 409,
        "CONFIGURATION_REVISION_STALE": 409,
        "DATA_FRAME_CONFIGURATION_STALE": 409,
        "CONFIGURATION_RUNTIME_BUSY": 409,
    }
    raise HTTPException(
        status_code=status_by_code.get(code, 422),
        detail={"code": code, "message": str(error)},
    )


async def _apply_node_change(repository, runtime, operation):
    base_revision = await asyncio.to_thread(repository.current_revision)
    gate = runtime.data_trunk.configuration_gate
    await asyncio.to_thread(gate.begin_configuration_publish, base_revision)
    try:
        result = await asyncio.to_thread(operation, base_revision)
    except Exception:
        gate.cancel_configuration_publish()
        raise
    await runtime.reload_rules_now()
    await asyncio.to_thread(gate.reconcile_configuration_runtime)
    return result


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
        serialized.pop("source_catalog_key", None)
    return serialized


@router.get(
    "/nodes",
    openapi_extra=capability_metadata(RUNTIME_READ),
)
async def list_nodes(
    layer: int | None = Query(None, description="按层级过滤 1=Site 2=Station 3=EnergyNode 4=Device 5=Tag"),
    enabled: bool = Query(True, description="只看启用节点"),
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
    repository=Depends(get_node_tree_repository),
) -> dict:
    """
    返回所有节点列表，含每个节点下的 tag 数量。

    用于前端树形结构展示 + 点位管理页的节点下拉选择。
    """
    try:
        rows = await asyncio.to_thread(repository.list_active)
        if layer is not None:
            rows = [row for row in rows if row["layer"] == layer]
        return {
            "nodes": [
                {
                    key: value
                    for key, value in row.items()
                    if principal.role != "operator" or key not in {"config", "source_catalog_key"}
                }
                for row in rows
            ],
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
                    SELECT id, name, parent_id, layer, node_type, config,
                           source_catalog_key, sort_order, enabled
                    FROM t_nodes WHERE enabled = TRUE AND retired_at IS NULL
                    ORDER BY layer, sort_order, name
                    """
                )
                ncols = [desc[0] for desc in cur.description]
                nodes = {str(r[0]): dict(zip(ncols, r)) for r in cur.fetchall()}

                cur.execute(
                    """
                    SELECT node_id, name, display_name, data_type, unit,
                           source_type, source_path, scale_factor, value_offset,
                           read_write
                    FROM t_tags WHERE enabled = TRUE
                    ORDER BY name
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
            if n.get("source_catalog_key"):
                out["source_catalog_key"] = n["source_catalog_key"]
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
    repository=Depends(get_node_tree_repository),
) -> dict:
    """获取单个节点详情（含其 tags 列表）。"""
    from app.services.telemetry_store import get_connection

    node = await asyncio.to_thread(repository.get_active, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    if principal.role == "operator":
        node = {
            key: value
            for key, value in node.items()
            if key not in {"config", "source_catalog_key"}
        }
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Tags under this node
            cur.execute(
                "SELECT id, name, display_name, data_type, unit, "
                "scale_factor, value_offset, source_path, read_write, enabled "
                "FROM t_tags WHERE node_id = %s AND enabled = TRUE "
                "ORDER BY name",
                (str(node_id),),
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
    repository=Depends(get_node_tree_repository),
    runtime=Depends(get_node_tree_runtime),
) -> dict:
    """更新节点；父级决定层级，显式 null 表示移动为根节点。"""
    changes = req.model_dump(exclude_unset=True)
    if "parent_id" in changes and changes["parent_id"] is not None:
        changes["parent_id"] = str(changes["parent_id"])
    try:
        return await _apply_node_change(
            repository,
            runtime,
            lambda base_revision: repository.update(
                node_id=node_id,
                changes=changes,
                actor=principal.actor,
                base_revision=base_revision,
            ),
        )
    except Exception as error:
        _raise_node_tree_http(error)


@router.post("/nodes", **protected(CONFIGURATION_WRITE))
async def create_node(
    req: NodeCreateRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_node_tree_repository),
    runtime=Depends(get_node_tree_runtime),
) -> dict:
    """创建根节点或子节点；层级由服务端根据父级计算。"""
    try:
        return await _apply_node_change(
            repository,
            runtime,
            lambda base_revision: repository.create(
                name=req.name,
                node_type=req.node_type,
                parent_id=req.parent_id,
                config=req.config,
                sort_order=req.sort_order,
                source_catalog_key=req.source_catalog_key,
                actor=principal.actor,
                base_revision=base_revision,
            ),
        )
    except Exception as error:
        _raise_node_tree_http(error)


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
                    FROM t_nodes WHERE id = %s AND retired_at IS NULL
                    UNION ALL
                    SELECT n.id, n.name, n.parent_id, n.layer, n.node_type, n.sort_order, n.enabled
                    FROM t_nodes n
                    JOIN descendants d ON n.parent_id = d.id
                    WHERE n.retired_at IS NULL
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
async def delete_node(
    node_id: UUID,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    repository=Depends(get_node_tree_repository),
    runtime=Depends(get_node_tree_runtime),
) -> dict:
    """从活动运行树退役节点子树；不可变历史和来源证据仍保留。"""
    try:
        return await _apply_node_change(
            repository,
            runtime,
            lambda base_revision: repository.retire(
                node_id=node_id,
                actor=principal.actor,
                base_revision=base_revision,
            ),
        )
    except Exception as error:
        _raise_node_tree_http(error)
