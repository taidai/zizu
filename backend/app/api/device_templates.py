"""
ZiZu Device Templates API — 设备模板

通过模板一次性创建设备节点、点位，并自动绑定全局实体，
降低新品牌/新型号设备的接入配置成本。
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from psycopg2.extras import Json

from app.services.telemetry_store import get_connection
from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    protected,
)

router = APIRouter()

MAX_LAYER = 5


class DeviceTemplateCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    category: str | None = None
    description: str | None = None
    content: dict = Field(default_factory=dict)
    enabled: bool = True


class DeviceTemplateUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    category: str | None = None
    description: str | None = None
    content: dict | None = None
    enabled: bool | None = None


class DeviceTemplateApplyRequest(BaseModel):
    parent_node_id: str = Field(..., description="挂载到哪个现有节点下")
    instance_name: str | None = Field(None, description="设备实例名，会作为顶层节点名称前缀")
    source_prefix: str | None = Field(None, description="Neuron 来源前缀，替换 source_path 中的 {prefix}")
    brand: str | None = Field(None, description="品牌/型号，写入实体绑定")


def _serialize_template(row: dict) -> dict:
    row = dict(row)
    row["id"] = str(row["id"])
    for k in ("created_at", "updated_at"):
        if row.get(k) and hasattr(row[k], "isoformat"):
            row[k] = row[k].isoformat()
    return row


@router.get("/device-templates", **protected(CONFIGURATION_READ))
async def list_templates() -> dict:
    """列出所有设备模板。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, category, description, content, is_system, enabled, created_at, updated_at
                FROM t_device_templates
                WHERE enabled = TRUE
                ORDER BY category NULLS LAST, name
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"items": [_serialize_template(r) for r in rows]}


@router.get("/device-templates/{template_id}", **protected(CONFIGURATION_READ))
async def get_template(template_id: str) -> dict:
    """获取单个模板。"""
    try:
        tid = UUID(template_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid template_id")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, category, description, content, is_system, enabled, created_at, updated_at
                FROM t_device_templates WHERE id = %s
            """, (tid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")
            cols = [d[0] for d in cur.description]
    return _serialize_template(dict(zip(cols, row)))


@router.post("/device-templates", **protected(CONFIGURATION_WRITE))
async def create_template(req: DeviceTemplateCreateRequest) -> dict:
    """创建模板。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO t_device_templates (name, category, description, content, enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, name, category, description, content, is_system, enabled, created_at, updated_at
            """, (req.name, req.category, req.description, Json(req.content), req.enabled,
                    datetime.now(timezone.utc), datetime.now(timezone.utc)))
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, cur.fetchone()))
            conn.commit()
    return _serialize_template(row)


@router.put("/device-templates/{template_id}", **protected(CONFIGURATION_WRITE))
async def update_template(template_id: str, req: DeviceTemplateUpdateRequest) -> dict:
    try:
        tid = UUID(template_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid template_id")
    fields = []
    params = []
    if req.name is not None:
        fields.append("name = %s")
        params.append(req.name)
    if req.category is not None:
        fields.append("category = %s")
        params.append(req.category)
    if req.description is not None:
        fields.append("description = %s")
        params.append(req.description)
    if req.content is not None:
        fields.append("content = %s")
        params.append(Json(req.content))
    if req.enabled is not None:
        fields.append("enabled = %s")
        params.append(req.enabled)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    fields.append("updated_at = %s")
    params.append(datetime.now(timezone.utc))
    params.append(tid)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE t_device_templates SET {', '.join(fields)} WHERE id = %s RETURNING id",
                params
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Template not found")
            conn.commit()
    return {"updated": True}


@router.delete("/device-templates/{template_id}", **protected(CONFIGURATION_WRITE))
async def delete_template(template_id: str) -> dict:
    try:
        tid = UUID(template_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid template_id")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_device_templates WHERE id = %s AND is_system = FALSE RETURNING id", (tid,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Template not found or is system template")
            conn.commit()
    return {"deleted": True}


@router.post(
    "/device-templates/{template_id}/apply",
    **protected(CONFIGURATION_WRITE),
)
async def apply_template(template_id: str, req: DeviceTemplateApplyRequest) -> dict:
    """
    应用模板：在 parent_node_id 下递归创建节点、点位，并绑定全局实体。
    """
    try:
        tid = UUID(template_id)
        parent_id = UUID(req.parent_node_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 读取模板
            cur.execute("SELECT id, name, content, enabled FROM t_device_templates WHERE id = %s", (tid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")
            _, tpl_name, content, enabled = row
            if not enabled:
                raise HTTPException(status_code=400, detail="Template is disabled")

            # 读取父节点
            cur.execute("SELECT id, name, layer FROM t_nodes WHERE id = %s", (parent_id,))
            parent_row = cur.fetchone()
            if not parent_row:
                raise HTTPException(status_code=404, detail="Parent node not found")
            _, parent_name, parent_layer = parent_row
            if parent_layer >= MAX_LAYER:
                raise HTTPException(status_code=400, detail="Parent node is already at max layer")

            content = content or {}
            nodes_spec = content.get("nodes", [])
            if not nodes_spec:
                raise HTTPException(status_code=400, detail="Template content has no nodes")

            summary = {
                "nodes_created": 0,
                "tags_created": 0,
                "bindings_created": 0,
                "entity_missing": [],
                "warnings": [],
            }

            def _substitute_source_path(path: str | None) -> str | None:
                if not path:
                    return path
                if req.source_prefix:
                    path = path.replace("{prefix}", req.source_prefix)
                if req.instance_name:
                    path = path.replace("{device}", req.instance_name)
                return path

            def _create_tag(node_uuid: UUID, tag_spec: dict):
                name = tag_spec.get("name", "").strip()
                if not name:
                    summary["warnings"].append("skipped tag with empty name")
                    return
                tag_type = (tag_spec.get("tag_type", "PHYSICAL") or "PHYSICAL").upper()
                data_type = (tag_spec.get("data_type", "FLOAT") or "FLOAT").upper()
                source_path = _substitute_source_path(tag_spec.get("source_path"))
                source_type = tag_spec.get("source_type")
                if not source_type:
                    source_type = "NEURON" if source_path else "manual"
                cur.execute("""
                    INSERT INTO t_tags (node_id, tag_type, data_type, name, display_name, unit,
                                        read_write, source_type, source_path,
                                        scale_factor, value_offset, description, enabled)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    RETURNING id
                """, (
                    node_uuid, tag_type, data_type, name,
                    tag_spec.get("display_name"), tag_spec.get("unit"),
                    (tag_spec.get("read_write") or "R").upper(),
                    source_type, source_path,
                    tag_spec.get("scale_factor", 1.0),
                    tag_spec.get("value_offset", 0.0),
                    tag_spec.get("description"),
                ))
                tag_id = cur.fetchone()[0]
                summary["tags_created"] += 1

                # 自动绑定全局实体
                entity_name = tag_spec.get("entity_name")
                binding_type = (tag_spec.get("binding_type") or "PHYSICAL").upper()
                if entity_name:
                    cur.execute("SELECT id FROM t_entities WHERE name = %s LIMIT 1", (entity_name,))
                    ent = cur.fetchone()
                    if ent:
                        cur.execute("""
                            INSERT INTO t_entity_bindings
                                (entity_id, tag_id, node_id, binding_type, brand, priority, enabled)
                            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                            ON CONFLICT (entity_id, tag_id) DO NOTHING
                        """, (ent[0], tag_id, node_uuid, binding_type, req.brand, 1))
                        summary["bindings_created"] += 1
                    else:
                        summary["entity_missing"].append(entity_name)

            def _create_nodes(parent_uuid: UUID, parent_layer: int, specs: list, name_prefix: str = ""):
                for spec in specs or []:
                    layer = parent_layer + 1
                    if layer > MAX_LAYER:
                        summary["warnings"].append(f"skipped node {spec.get('name')}: exceeds max layer")
                        continue
                    name = name_prefix + (spec.get("name", "").strip() or "未命名")
                    node_type = spec.get("node_type")
                    config = spec.get("config", {}) or {}
                    cur.execute("""
                        INSERT INTO t_nodes (name, parent_id, layer, node_type, config, sort_order, enabled, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s)
                        RETURNING id
                    """, (name, parent_uuid, layer, node_type, Json(config),
                            spec.get("sort_order", 0),
                            datetime.now(timezone.utc), datetime.now(timezone.utc)))
                    node_id = cur.fetchone()[0]
                    summary["nodes_created"] += 1

                    for tag_spec in spec.get("tags", []) or []:
                        _create_tag(node_id, tag_spec)

                    _create_nodes(node_id, layer, spec.get("children", []), name_prefix="")

            top_prefix = f"{req.instance_name}_" if req.instance_name else ""
            _create_nodes(parent_id, parent_layer, nodes_spec, name_prefix=top_prefix)
            conn.commit()

    return {
        "status": "ok",
        "template_id": template_id,
        "parent_node_id": req.parent_node_id,
        "summary": summary,
    }
