"""
ZiZu Node Category API — 节点大类管理

GET    /api/v1/categories              → 大类列表
POST   /api/v1/categories              → 创建大类
PUT    /api/v1/categories/{id}         → 更新大类
DELETE /api/v1/categories/{id}         → 删除大类
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    protected,
)

router = APIRouter()


class CategoryCreate(BaseModel):
    name: str = Field(..., description="大类名称")
    node_type: str = Field(..., description="节点类型")
    description: str | None = Field(None, description="描述")


class CategoryUpdate(BaseModel):
    name: str | None = None
    node_type: str | None = None
    description: str | None = None


@router.get("/categories", **protected(CONFIGURATION_READ))
async def list_categories() -> dict:
    """获取节点大类列表。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, node_type, description, created_at "
                "FROM t_node_categories ORDER BY name"
            )
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    for row in rows:
        row["id"] = str(row["id"])
        row["created_at"] = row["created_at"].isoformat()

    return {"categories": rows, "total": len(rows)}


@router.post("/categories", **protected(CONFIGURATION_WRITE))
async def create_category(req: CategoryCreate) -> dict:
    """创建节点大类。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO t_node_categories (name, node_type, description)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (req.name, req.node_type, req.description),
            )
            row = cur.fetchone()
            conn.commit()

    logger.info("[API/categories] Created: {}", req.name)
    return {"status": "ok", "id": str(row[0])}


@router.put("/categories/{category_id}", **protected(CONFIGURATION_WRITE))
async def update_category(category_id: UUID, req: CategoryUpdate) -> dict:
    """更新节点大类。"""
    from app.services.telemetry_store import get_connection

    updates = []
    params = []
    if req.name is not None:
        updates.append("name = %s")
        params.append(req.name)
    if req.node_type is not None:
        updates.append("node_type = %s")
        params.append(req.node_type)
    if req.description is not None:
        updates.append("description = %s")
        params.append(req.description)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    params.append(category_id)
    query = f"UPDATE t_node_categories SET {', '.join(updates)} WHERE id = %s"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()

    logger.info("[API/categories] Updated: {}", category_id)
    return {"status": "ok"}


@router.delete("/categories/{category_id}", **protected(CONFIGURATION_WRITE))
async def delete_category(category_id: UUID) -> dict:
    """删除节点大类。"""
    from app.services.telemetry_store import get_connection

    # 检查是否有节点使用
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM t_nodes WHERE category_id = %s", (category_id,))
            count = cur.fetchone()[0]
            if count > 0:
                raise HTTPException(status_code=400, detail=f"Cannot delete: {count} nodes using this category")

            cur.execute("DELETE FROM t_node_categories WHERE id = %s", (category_id,))
            conn.commit()

    logger.info("[API/categories] Deleted: {}", category_id)
    return {"status": "ok"}
