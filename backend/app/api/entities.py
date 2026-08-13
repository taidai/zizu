"""
ZiZu Entities API - 全局实体管理

实体是业务语义层（如 pcs.activePower），与具体品牌物理/虚拟点位解耦。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
import csv
import io
import json
from datetime import datetime, timezone
from loguru import logger
from pydantic import BaseModel, Field

from app.services.telemetry_store import get_connection
from app.services.entity_resolver import (
    get_entity_history,
    get_entity_realtime,
    resolve_entity_binding,
)
from app.core.standard_entities import STANDARD_ENTITIES, seed_standard_entities
from app.services.entity_binder import auto_bind_standard_entities
from app.api.health import _VERSION as APP_VERSION
from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    CONTROL_WRITE,
    RUNTIME_READ,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.identity import Principal
from app.api.control_commands import (
    compatibility_error,
    compatibility_response,
    get_control_compatibility,
)
from app.services.control_commands import ControlCommandCompatibility

router = APIRouter()


# ========================================
# Request / Response Models
# ========================================

class EntityCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="实体全局名，如 pcs.activePower")
    display_name: str | None = None
    entity_type: str = Field(..., pattern="^(R|W|RW)$", description="R/W/RW")
    data_type: str = Field(..., pattern="^(FLOAT|INT|BOOL|STRING|ENUM)$")
    unit: str | None = None
    category: str | None = None
    description: str | None = None
    enabled: bool = True


class EntityUpdateRequest(BaseModel):
    display_name: str | None = None
    entity_type: str | None = Field(None, pattern="^(R|W|RW)$")
    data_type: str | None = Field(None, pattern="^(FLOAT|INT|BOOL|STRING|ENUM)$")
    unit: str | None = None
    category: str | None = None
    description: str | None = None
    enabled: bool | None = None


class EntityBindingRequest(BaseModel):
    tag_id: str = Field(..., description="点位 UUID")
    node_id: str = Field(..., description="节点 UUID")
    binding_type: str = Field(..., pattern="^(PHYSICAL|VIRTUAL)$")
    brand: str | None = None
    priority: int = Field(1, ge=1, description="绑定优先级，数字越小越优先")
    enabled: bool = True


class LegacyEntityWriteRequest(BaseModel):
    """Compatibility-only write shape; new callers use an entity instance command."""

    value: object
    confirmation_id: UUID | None = None


class EntityResponse(BaseModel):
    id: str
    name: str
    display_name: str | None = None
    entity_type: str
    data_type: str
    unit: str | None = None
    category: str | None = None
    description: str | None = None
    enabled: bool
    binding_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    is_system: bool = False


class BindingResponse(BaseModel):
    id: str
    entity_id: str
    tag_id: str
    node_id: str
    binding_type: str
    brand: str | None = None
    priority: int
    enabled: bool
    tag_name: str | None = None
    tag_display_name: str | None = None
    node_name: str | None = None
    created_at: str | None = None


# ========================================
# Helpers
# ========================================

def _row_to_entity(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "display_name": row.get("display_name"),
        "entity_type": row["entity_type"],
        "data_type": row["data_type"],
        "unit": row.get("unit"),
        "category": row.get("category"),
        "description": row.get("description"),
        "enabled": row["enabled"],
        "binding_count": row.get("binding_count", 0),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "is_system": bool(row.get("is_system", False)),
        "std_field": row.get("std_field"),
        "std_ref": row.get("std_ref"),
    }


_OPERATOR_ENTITY_SOURCE_FIELDS = frozenset(
    {"binding_id", "tag_id", "tag_name", "node_id", "node_name"}
)


def _runtime_entity(data: dict, principal: Principal) -> dict:
    """Return the role-appropriate entity runtime projection.

    Operators consume stable entity semantics and observed values.  Physical
    binding identifiers and Neuron-facing names are configuration details and
    remain available only to engineers and administrators for diagnostics.
    """
    projected = dict(data)
    if principal.role == "operator":
        for field in _OPERATOR_ENTITY_SOURCE_FIELDS:
            projected.pop(field, None)
    return projected


# ========================================
# Endpoints
# ========================================


def _standard_entities_rows() -> list[tuple]:
    """内置标准实体定义（单一数据源：app.core.standard_entities）。"""
    return list(STANDARD_ENTITIES)


@router.post("/entities/seed", **protected(CONFIGURATION_WRITE))
async def seed_entities() -> dict:
    """重新初始化系统内置标准实体（幂等，单一数据源）。"""
    return seed_standard_entities()


@router.post("/entities/bindings/auto-bind", **protected(CONFIGURATION_WRITE))
async def auto_bind_entities(dry_run: bool = False) -> dict:
    """根据内置映射表自动把国标实体绑定到 enabled tag（幂等）。"""
    try:
        result = auto_bind_standard_entities(dry_run=dry_run)
        return result
    except Exception as e:
        logger.error("[API/entities] auto-bind failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entities/export", **protected(CONFIGURATION_READ))
async def export_entities(
    format: str = Query("csv", pattern="^(csv|json)$", description="导出格式"),
    category: str | None = Query(None, description="按分类过滤"),
) -> StreamingResponse:
    """导出全局实体目录为 CSV(Excel) 或 JSON。"""
    where = "1=1"
    params: list = []
    if category:
        where += " AND category = %s"
        params.append(category)
    query = f"""
        SELECT name, display_name, entity_type, data_type, unit,
               category, description, std_field, std_ref, enabled
        FROM t_entities
        WHERE {where}
        ORDER BY category NULLS LAST, name
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.error("[API/entities] export failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if format == "json":
        payload = {
            "version": APP_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(rows),
            "entities": rows,
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return StreamingResponse(
            iter([data]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="zizu_entities_{ts}.json"'},
        )

    # CSV with UTF-8 BOM for Excel
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(cols)
    for r in rows:
        writer.writerow([r[c] if r[c] is not None else "" for c in cols])
    data = buf.getvalue().encode("utf-8")
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="zizu_entities_{ts}.csv"'},
    )


@router.post("/entities/import", **protected(CONFIGURATION_WRITE))
async def import_entities(
    request: Request,
    mode: str = Query("upsert", pattern="^(upsert|create)$", description="upsert=更新或新建, create=仅新建跳过已存在"),
    dry_run: bool = Query(False, description="只校验不写库"),
) -> dict:
    """导入全局实体目录（CSV/JSON 文本），按 name upsert 或仅新建。"""
    raw = await request.body()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    entries: list[dict] = []
    stripped = text.lstrip()
    try:
        if stripped.startswith("[") or stripped.startswith("{"):
            data = json.loads(text)
            if isinstance(data, dict) and "entities" in data:
                data = data["entities"]
            if not isinstance(data, list):
                raise ValueError("JSON must be an array or {entities:[...]}")
            entries = data
        else:
            reader = csv.DictReader(io.StringIO(text))
            entries = list(reader)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Parse failed: {e}")

    created = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    valid_et = {"R", "W", "RW"}
    valid_dt = {"FLOAT", "INT", "BOOL", "STRING", "ENUM"}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, is_system FROM t_entities")
                existing = {r[0]: bool(r[1]) for r in cur.fetchall()}

                for i, e in enumerate(entries):
                    if not isinstance(e, dict):
                        errors.append(f"row {i}: not an object")
                        continue
                    name = (str(e.get("name") or "")).strip()
                    if not name:
                        errors.append(f"row {i}: missing name")
                        continue
                    et = str(e.get("entity_type") or "R").upper()
                    dt = str(e.get("data_type") or "FLOAT").upper()
                    if et not in valid_et:
                        et = "R"
                    if dt not in valid_dt:
                        dt = "FLOAT"
                    display = e.get("display_name") or None
                    unit = e.get("unit") or None
                    category = e.get("category") or None
                    desc = e.get("description") or None
                    std_field = e.get("std_field") or None
                    std_ref = e.get("std_ref") or None
                    en_val = e.get("enabled")
                    if isinstance(en_val, str):
                        enabled = en_val.strip().lower() in ("1", "true", "yes", "y", "t")
                    elif en_val is None:
                        enabled = True
                    else:
                        enabled = bool(en_val)

                    if name in existing:
                        if mode == "create":
                            skipped += 1
                            continue
                        if dry_run:
                            updated += 1
                            continue
                        is_sys = existing[name]
                        if is_sys:
                            cur.execute(
                                """UPDATE t_entities SET
                                     display_name = COALESCE(%s, display_name),
                                     unit = COALESCE(%s, unit),
                                     description = COALESCE(%s, description),
                                     std_field = COALESCE(%s, std_field),
                                     std_ref = COALESCE(%s, std_ref),
                                     enabled = %s, updated_at = now()
                                   WHERE name = %s""",
                                (display, unit, desc, std_field, std_ref, enabled, name),
                            )
                        else:
                            cur.execute(
                                """UPDATE t_entities SET
                                     display_name = COALESCE(%s, display_name),
                                     entity_type = %s, data_type = %s,
                                     unit = COALESCE(%s, unit),
                                     category = COALESCE(%s, category),
                                     description = COALESCE(%s, description),
                                     std_field = COALESCE(%s, std_field),
                                     std_ref = COALESCE(%s, std_ref),
                                     enabled = %s, updated_at = now()
                                   WHERE name = %s""",
                                (display, et, dt, unit, category, desc, std_field, std_ref, enabled, name),
                            )
                        updated += 1
                    else:
                        if dry_run:
                            created += 1
                            continue
                        cur.execute(
                            """INSERT INTO t_entities
                                 (name, display_name, entity_type, data_type, unit, category,
                                  description, enabled, is_system, std_field, std_ref)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s)""",
                            (name, display, et, dt, unit, category, desc, enabled, std_field, std_ref),
                        )
                        created += 1
                        existing[name] = False
                if not dry_run:
                    conn.commit()
        return {"created": created, "updated": updated, "skipped": skipped,
                "errors": errors, "dry_run": dry_run, "total": len(entries)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/entities] import failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entities", **protected(CONFIGURATION_READ))
async def list_entities(
    category: str | None = Query(None, description="按分类过滤"),
    entity_type: str | None = Query(None, description="按 R/W/RW 过滤"),
    search: str | None = Query(None, description="按名称/显示名搜索"),
    enabled: bool | None = Query(None, description="按启用状态过滤"),
    node_id: str | None = Query(None, description="按节点绑定关系过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict:
    """分页查询全局实体列表。"""
    conditions = ["1=1"]
    params: list = []

    if category:
        conditions.append("e.category = %s")
        params.append(category)
    if entity_type:
        conditions.append("e.entity_type = %s")
        params.append(entity_type.upper())
    if enabled is not None:
        conditions.append("e.enabled = %s")
        params.append(enabled)
    if search:
        conditions.append("(e.name ILIKE %s OR e.display_name ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])

    try:
        nid = UUID(str(node_id)) if node_id else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid node_id")

    if nid:
        conditions.append("EXISTS (SELECT 1 FROM t_entity_bindings b WHERE b.entity_id = e.id AND b.node_id = %s AND b.enabled = TRUE)")
        params.append(nid)

    where = " AND ".join(conditions)
    offset = (page - 1) * page_size

    query = f"""
    SELECT e.*, COUNT(b.id) AS binding_count
    FROM t_entities e
    LEFT JOIN t_entity_bindings b ON b.entity_id = e.id
    WHERE {where}
    GROUP BY e.id
    ORDER BY e.category NULLS LAST, e.name
    LIMIT %s OFFSET %s
    """
    count_query = f"""
    SELECT COUNT(*) FROM t_entities e WHERE {where}
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params + [page_size, offset])
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]

                cur.execute(count_query, params)
                total = cur.fetchone()[0]

        return {
            "items": [_row_to_entity(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }
    except Exception as e:
        logger.error("[API/entities] list failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post(
    "/entities",
    status_code=status.HTTP_201_CREATED,
    **protected(CONFIGURATION_WRITE),
)
async def create_entity(req: EntityCreateRequest) -> dict:
    """创建全局实体。"""
    query = """
    INSERT INTO t_entities (name, display_name, entity_type, data_type, unit, category, description, enabled)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING id, created_at
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (
                    req.name, req.display_name, req.entity_type.upper(),
                    req.data_type.upper(), req.unit, req.category,
                    req.description, req.enabled,
                ))
                row = cur.fetchone()
                conn.commit()
        return {"id": str(row[0]), "created_at": row[1].isoformat()}
    except Exception as e:
        logger.error("[API/entities] create failed: {}", e)
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"Entity name '{req.name}' already exists")
        raise HTTPException(status_code=500, detail=str(e))


class BatchBindingItem(BaseModel):
    entity_id: str = Field(..., description="实体 UUID")
    tag_id: str = Field(..., description="点位 UUID")
    node_id: str = Field(..., description="节点 UUID")
    binding_type: str = Field(..., pattern="^(PHYSICAL|VIRTUAL)$")
    brand: str | None = None
    priority: int = Field(1, ge=1, description="绑定优先级，数字越小越优先")
    enabled: bool = True

class BatchBindRequest(BaseModel):
    bindings: list[BatchBindingItem] = Field(..., min_length=1, max_length=200)

class BatchUnbindRequest(BaseModel):
    binding_ids: list[str] = Field(..., min_length=1, max_length=200)

@router.get("/entities/bindings", **protected(CONFIGURATION_READ))
async def list_bindings(
    node_id: str | None = Query(None, description="按节点过滤"),
    entity_id: str | None = Query(None, description="按实体过滤"),
) -> dict:
    """查询实体-点位绑定列表，支持按节点或实体过滤。"""
    conditions = ["1=1"]
    params: list = []

    if node_id:
        try:
            params.append(UUID(node_id))
            conditions.append("b.node_id = %s")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid node_id")
    if entity_id:
        try:
            params.append(UUID(entity_id))
            conditions.append("b.entity_id = %s")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid entity_id")

    where = " AND ".join(conditions)
    query = f"""
    SELECT
        b.id,
        b.entity_id,
        b.tag_id,
        b.node_id,
        b.binding_type,
        b.brand,
        b.priority,
        b.enabled,
        b.created_at,
        t.name AS tag_name,
        t.display_name AS tag_display_name,
        n.name AS node_name,
        e.name AS entity_name,
        e.display_name AS entity_display_name,
        e.entity_type,
        e.data_type,
        e.unit AS entity_unit,
        e.is_system AS entity_is_system
    FROM t_entity_bindings b
    JOIN t_tags t ON t.id = b.tag_id
    JOIN t_nodes n ON n.id = b.node_id
    JOIN t_entities e ON e.id = b.entity_id
    WHERE {where}
    ORDER BY b.priority ASC, e.name ASC, t.name ASC
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        return {
            "bindings": [
                {
                    "id": str(r["id"]),
                    "entity_id": str(r["entity_id"]),
                    "tag_id": str(r["tag_id"]),
                    "node_id": str(r["node_id"]),
                    "binding_type": r["binding_type"],
                    "brand": r.get("brand"),
                    "priority": r["priority"],
                    "enabled": r["enabled"],
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    "tag_name": r.get("tag_name"),
                    "tag_display_name": r.get("tag_display_name"),
                    "node_name": r.get("node_name"),
                    "entity_name": r.get("entity_name"),
                    "entity_display_name": r.get("entity_display_name"),
                    "entity_type": r.get("entity_type"),
                    "data_type": r.get("data_type"),
                    "unit": r.get("entity_unit"),
                    "entity_is_system": bool(r.get("entity_is_system", False)),
                }
                for r in rows
            ],
            "total": len(rows),
        }
    except Exception as e:
        logger.error("[API/entities] list bindings failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/entities/bindings/batch", **protected(CONFIGURATION_WRITE))
async def batch_create_bindings(req: BatchBindRequest) -> dict:
    """批量创建实体-点位绑定。重复绑定（同一 entity+tag）会自动跳过。"""
    validated = []
    for item in req.bindings:
        try:
            validated.append({
                "entity_id": UUID(item.entity_id),
                "tag_id": UUID(item.tag_id),
                "node_id": UUID(item.node_id),
                "binding_type": item.binding_type.upper(),
                "brand": item.brand,
                "priority": item.priority,
                "enabled": item.enabled,
            })
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid UUID in binding: {item}")

    eids = list({v["entity_id"] for v in validated})
    tids = list({v["tag_id"] for v in validated})
    nids = list({v["node_id"] for v in validated})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM t_entities WHERE id = ANY(%s)", (eids,))
                existing_eids = {r[0] for r in cur.fetchall()}
                cur.execute("SELECT id FROM t_tags WHERE id = ANY(%s)", (tids,))
                existing_tids = {r[0] for r in cur.fetchall()}
                cur.execute("SELECT id FROM t_nodes WHERE id = ANY(%s)", (nids,))
                existing_nids = {r[0] for r in cur.fetchall()}

        missing = [
            i for i, v in enumerate(validated)
            if v["entity_id"] not in existing_eids
            or v["tag_id"] not in existing_tids
            or v["node_id"] not in existing_nids
        ]
        if missing:
            raise HTTPException(status_code=400, detail=f"Binding item {missing[0]} references non-existing entity/tag/node")

        insert_sql = """
        INSERT INTO t_entity_bindings
          (entity_id, tag_id, node_id, binding_type, brand, priority, enabled)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_id, tag_id) DO NOTHING
        RETURNING id
        """
        created = 0
        skipped = 0
        with get_connection() as conn:
            with conn.cursor() as cur:
                for v in validated:
                    cur.execute(insert_sql, (
                        v["entity_id"], v["tag_id"], v["node_id"],
                        v["binding_type"], v["brand"], v["priority"], v["enabled"],
                    ))
                    if cur.fetchone():
                        created += 1
                    else:
                        skipped += 1
                conn.commit()

        return {"created": created, "skipped": skipped, "total": len(validated)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/entities] batch bind failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/entities/bindings/batch", **protected(CONFIGURATION_WRITE))
async def batch_delete_bindings(req: BatchUnbindRequest) -> dict:
    """批量删除实体-点位绑定。"""
    try:
        bids = [UUID(bid) for bid in req.binding_ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid binding_id")
    if not bids:
        return {"deleted": 0}

    placeholders = ",".join(["%s"] * len(bids))
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM t_entity_bindings WHERE id IN ({placeholders})",
                    bids,
                )
                deleted = cur.rowcount
                conn.commit()
        return {"deleted": deleted}
    except Exception as e:
        logger.error("[API/entities] batch unbind failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))
@router.get("/entities/{entity_id}", **protected(CONFIGURATION_READ))
async def get_entity(entity_id: str) -> dict:
    """获取实体详情及绑定。"""
    try:
        eid = UUID(entity_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entity_id")

    entity_query = """
    SELECT e.*, COUNT(b.id) AS binding_count
    FROM t_entities e
    LEFT JOIN t_entity_bindings b ON b.entity_id = e.id
    WHERE e.id = %s
    GROUP BY e.id
    """
    bindings_query = """
    SELECT b.*, t.name AS tag_name, t.display_name AS tag_display_name, n.name AS node_name
    FROM t_entity_bindings b
    JOIN t_tags t ON t.id = b.tag_id
    JOIN t_nodes n ON n.id = b.node_id
    WHERE b.entity_id = %s
    ORDER BY b.priority ASC, b.created_at ASC
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(entity_query, (eid,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Entity not found")
                columns = [desc[0] for desc in cur.description]
                entity = _row_to_entity(dict(zip(columns, row)))

                cur.execute(bindings_query, (eid,))
                b_columns = [desc[0] for desc in cur.description]
                bindings = []
                for b_row in cur.fetchall():
                    b = dict(zip(b_columns, b_row))
                    bindings.append({
                        "id": str(b["id"]),
                        "entity_id": str(b["entity_id"]),
                        "tag_id": str(b["tag_id"]),
                        "node_id": str(b["node_id"]),
                        "binding_type": b["binding_type"],
                        "brand": b.get("brand"),
                        "priority": b["priority"],
                        "enabled": b["enabled"],
                        "tag_name": b.get("tag_name"),
                        "tag_display_name": b.get("tag_display_name"),
                        "node_name": b.get("node_name"),
                        "created_at": b["created_at"].isoformat() if b.get("created_at") else None,
                    })

        entity["bindings"] = bindings
        return entity
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/entities] get failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


def _check_system_protected(eid: UUID, req: EntityUpdateRequest) -> None:
    """系统实体不允许修改核心元数据（名称、分类、类型、数据类型）。"""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT is_system FROM t_entities WHERE id = %s", (eid,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Entity not found")
                is_system = bool(row[0]) if row[0] is not None else False
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/entities] check system failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

    if not is_system:
        return

    forbidden = []
    if req.entity_type is not None:
        forbidden.append("entity_type")
    if req.data_type is not None:
        forbidden.append("data_type")
    if req.category is not None:
        forbidden.append("category")
    if forbidden:
        raise HTTPException(
            status_code=403,
            detail=f"System entity cannot modify: {', '.join(forbidden)}",
        )


@router.put("/entities/{entity_id}", **protected(CONFIGURATION_WRITE))
async def update_entity(entity_id: str, req: EntityUpdateRequest) -> dict:
    """更新实体元数据。"""
    try:
        eid = UUID(entity_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entity_id")

    _check_system_protected(eid, req)

    fields = []
    params: list = []
    if req.display_name is not None:
        fields.append("display_name = %s")
        params.append(req.display_name)
    if req.entity_type is not None:
        fields.append("entity_type = %s")
        params.append(req.entity_type.upper())
    if req.data_type is not None:
        fields.append("data_type = %s")
        params.append(req.data_type.upper())
    if req.unit is not None:
        fields.append("unit = %s")
        params.append(req.unit)
    if req.category is not None:
        fields.append("category = %s")
        params.append(req.category)
    if req.description is not None:
        fields.append("description = %s")
        params.append(req.description)
    if req.enabled is not None:
        fields.append("enabled = %s")
        params.append(req.enabled)

    if not fields:
        return {"updated": False}

    params.append(eid)
    query = f"""
    UPDATE t_entities SET {', '.join(fields)}, updated_at = now()
    WHERE id = %s
    RETURNING updated_at
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Entity not found")
                conn.commit()
        return {"updated": True, "updated_at": row[0].isoformat()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/entities] update failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/entities/{entity_id}", **protected(CONFIGURATION_WRITE))
async def delete_entity(entity_id: str) -> dict:
    """删除实体（级联删除绑定）。"""
    try:
        eid = UUID(entity_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entity_id")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT is_system FROM t_entities WHERE id = %s", (eid,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Entity not found")
                if row[0]:
                    raise HTTPException(status_code=403, detail="System entity cannot be deleted")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/entities] delete check failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM t_entities WHERE id = %s RETURNING id", (eid,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Entity not found")
                conn.commit()
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/entities] delete failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/entities/{entity_id}/bindings", **protected(CONFIGURATION_WRITE))
async def create_binding(entity_id: str, req: EntityBindingRequest) -> dict:
    """为实体绑定一个点位。"""
    try:
        eid = UUID(entity_id)
        tid = UUID(req.tag_id)
        nid = UUID(req.node_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")

    # 校验 entity 与 tag/node 存在
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM t_entities WHERE id = %s", (eid,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Entity not found")
            cur.execute("SELECT id FROM t_tags WHERE id = %s", (tid,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Tag not found")
            cur.execute("SELECT id FROM t_nodes WHERE id = %s", (nid,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Node not found")

    query = """
    INSERT INTO t_entity_bindings (entity_id, tag_id, node_id, binding_type, brand, priority, enabled)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING id, created_at
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (
                    eid, tid, nid, req.binding_type.upper(),
                    req.brand, req.priority, req.enabled,
                ))
                row = cur.fetchone()
                conn.commit()
        return {"id": str(row[0]), "created_at": row[1].isoformat()}
    except Exception as e:
        logger.error("[API/entities] binding failed: {}", e)
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Entity already bound to this tag")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/entities/{entity_id}/bindings/{binding_id}",
    **protected(CONFIGURATION_WRITE),
)
async def delete_binding(entity_id: str, binding_id: str) -> dict:
    """删除绑定。"""
    try:
        bid = UUID(binding_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid binding_id")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM t_entity_bindings WHERE id = %s AND entity_id = %s RETURNING id",
                    (bid, UUID(entity_id)),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Binding not found")
                conn.commit()
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/entities] delete binding failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/entities/{entity_id}/realtime",
    openapi_extra=capability_metadata(RUNTIME_READ),
)
async def entity_realtime(
    entity_id: str,
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
) -> dict:
    """获取实体实时值。"""
    data = get_entity_realtime(entity_id)
    if not data:
        raise HTTPException(status_code=404, detail="Entity has no active binding or no data")
    return _runtime_entity(data, principal)


@router.get(
    "/entities/{entity_id}/history",
    openapi_extra=capability_metadata(RUNTIME_READ),
)
async def entity_history(
    entity_id: str,
    range: str = Query("1h", pattern="^(1h|24h|7d|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=2000),
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
) -> dict:
    """获取实体历史数据。"""
    data = get_entity_history(entity_id, range, page, page_size)
    if not data:
        raise HTTPException(status_code=404, detail="Entity has no active binding or no data")
    return _runtime_entity(data, principal)


@router.post(
    "/entities/{entity_id}/write",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONTROL_WRITE),
)
async def entity_write(
    entity_id: UUID,
    req: LegacyEntityWriteRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200),
    principal: Principal = Depends(principal_for(CONTROL_WRITE)),
    compatibility: ControlCommandCompatibility = Depends(get_control_compatibility),
) -> dict:
    """旧全局实体写入兼容入口：只能映射唯一确认实体实例，绝不直接下发设备。"""
    command = compatibility.submit_legacy_entity(
        actor=principal.actor,
        entity_id=entity_id,
        value=req.value,
        idempotency_key=idempotency_key,
        confirmation_id=req.confirmation_id,
    )
    if command.status == "rejected":
        raise compatibility_error(command)
    return compatibility_response(command)

