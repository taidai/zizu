"""
ZiZu Fault Map API — 故障码映射表管理
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field

from app.services.telemetry_store import get_connection
from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    protected,
)

router = APIRouter()


class FaultMapEntry(BaseModel):
    code: str = Field(..., min_length=1, description="故障码（与点位值匹配）")
    message: str = Field(..., min_length=1, description="故障描述")


class FaultMapCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    entries: list[FaultMapEntry] = Field(default_factory=list)


class FaultMapUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    entries: list[FaultMapEntry] | None = None


def _serialize(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row.get("description"),
        "entries": row.get("entries") or [],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


@router.get("/fault-maps", **protected(CONFIGURATION_READ))
async def list_fault_maps() -> dict:
    """获取全部故障码映射表。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_fault_maps ORDER BY name")
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return {"items": [_serialize(r) for r in rows], "total": len(rows)}


@router.post(
    "/fault-maps",
    status_code=status.HTTP_201_CREATED,
    **protected(CONFIGURATION_WRITE),
)
async def create_fault_map(req: FaultMapCreate) -> dict:
    """创建故障码映射表。"""
    entries = [e.model_dump() for e in req.entries]
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO t_fault_maps (name, description, entries) VALUES (%s, %s, %s) RETURNING id",
                    (req.name, req.description, entries),
                )
                new_id = cur.fetchone()[0]
                conn.commit()
        return {"id": str(new_id), "status": "ok"}
    except Exception as e:
        logger.error("[API/fault-maps] create failed: {}", e)
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Fault map name already exists")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fault-maps/{map_id}", **protected(CONFIGURATION_READ))
async def get_fault_map(map_id: UUID) -> dict:
    """获取单个映射表详情。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_fault_maps WHERE id = %s", (map_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Fault map not found")
            columns = [desc[0] for desc in cur.description]
    return _serialize(dict(zip(columns, row)))


@router.put("/fault-maps/{map_id}", **protected(CONFIGURATION_WRITE))
async def update_fault_map(map_id: UUID, req: FaultMapUpdate) -> dict:
    """更新故障码映射表。"""
    fields = []
    params: list = []
    if req.name is not None:
        fields.append("name = %s")
        params.append(req.name)
    if req.description is not None:
        fields.append("description = %s")
        params.append(req.description)
    if req.entries is not None:
        fields.append("entries = %s")
        params.append([e.model_dump() for e in req.entries])
    if not fields:
        return {"status": "no_change"}

    fields.append("updated_at = now()")
    params.append(map_id)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE t_fault_maps SET {', '.join(fields)} WHERE id = %s RETURNING id",
                    params,
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Fault map not found")
                conn.commit()
        return {"status": "ok"}
    except Exception as e:
        logger.error("[API/fault-maps] update failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/fault-maps/{map_id}", **protected(CONFIGURATION_WRITE))
async def delete_fault_map(map_id: UUID) -> dict:
    """删除故障码映射表（被引用的点位会自动清空 fault_map_id）。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_fault_maps WHERE id = %s RETURNING id", (map_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Fault map not found")
            conn.commit()
    return {"status": "ok", "deleted": str(map_id)}
