"""
ZiZu Tags API — 点位管理（含偏移校准）

GET    /api/v1/tags              → 分页查询点位列表（含原始值/工程值实时对照）
GET    /api/v1/tags/{tag_id}     → 单个点位详情
PUT    /api/v1/tags/{tag_id}     → 修改 scale_factor / value_offset / unit 等
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

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
_LEGACY_ALARM_FIELDS = frozenset(
    {"alarm_level", "alarm_type", "alarm_threshold", "fault_map_id"}
)


def _reject_legacy_alarm_fields(request: BaseModel) -> None:
    if request.model_fields_set & _LEGACY_ALARM_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ALARM_CONFIGURATION_MIGRATION_REQUIRED",
                "message": "Legacy tag alarm fields are read-only; use /api/v1/alarm-configurations",
            },
        )


# ══════════════════════════════════════
# Request / Response Models
# ══════════════════════════════════════

_AGG_FNS = {"SUM", "AVG", "MAX", "MIN", "COUNT", "LAST"}
_FORMULA_TYPES = {"expression", "aggregate", "condition"}

_OPERATOR_CONFIGURATION_FIELDS = frozenset(
    {
        "scale_factor",
        "value_offset",
        "source_path",
        "source_type",
        "sources",
        "formula",
        "formula_type",
        "aggregate_fn",
        "fault_map_id",
        "fault_map_name",
        "alarm_level",
        "alarm_type",
        "alarm_threshold",
    }
)


def _runtime_tag(tag: dict, principal: Principal) -> dict:
    if principal.role == "operator":
        for field in _OPERATOR_CONFIGURATION_FIELDS:
            tag.pop(field, None)
    return tag


class TagUpdateRequest(BaseModel):
    """允许修改的点位字段。"""
    scale_factor: float | None = Field(None, description="缩放系数")
    value_offset: float | None = Field(None, description="偏移量：工程值 = 原始值 × scale + offset")
    unit: str | None = Field(None, description="单位")
    display_name: str | None = Field(None, description="显示名称")
    read_write: str | None = Field(None, pattern="^[RrWw]+$", description="读写权限")
    enabled: bool | None = Field(None, description="是否启用")
    description: str | None = Field(None, description="描述")
    source_type: str | None = Field(None, description="来源类型：neuron / manual / opcua / modbus 等")
    source_path: str | None = Field(None, description="来源路径")
    # LogicalTag 汇总规则字段 (S11)
    aggregate_fn: str | None = Field(None, description="聚合函数 SUM/AVG/MAX/MIN/COUNT/LAST")
    formula: str | None = Field(None, description="表达式或聚合来源引用")
    formula_type: str | None = Field(None, description="expression/aggregate/condition")
    sources: list[str] | None = Field(None, description="来源点位 UUID 列表")
    alarm_level: Any = None
    alarm_type: Any = None
    alarm_threshold: Any = None
    fault_map_id: Any = None


class TagResponse(BaseModel):
    id: str
    node_id: str
    node_name: str | None = None
    name: str
    display_name: str | None = None
    data_type: str
    tag_type: str
    unit: str | None = None
    scale_factor: float = 1.0
    value_offset: float = 0.0
    source_path: str | None = None
    source_type: str | None = None
    read_write: str = "R"
    enabled: bool = True
    description: str | None = None
    # LogicalTag 汇总规则字段 (S11)
    aggregate_fn: str | None = None
    formula: str | None = None
    formula_type: str | None = None
    sources: list[str] | None = None
    alarm_level: str | None = None
    fault_map_id: str | None = None
    fault_map_name: str | None = None
    # 实时值 (由 /tags/{id} 附加)
    raw_value: float | int | bool | str | None = None
    eng_value: float | None = None
    latest_ts: str | None = None
    quality: int | None = None


class HistoryPoint(BaseModel):
    ts: str
    raw_value: float | None
    eng_value: float | None


class HistoryResponse(BaseModel):
    tag_id: str
    tag_name: str
    range: str
    bucket: str
    points: list[HistoryPoint]




def _coerce_latest_value(tag: dict) -> None:
    """
    将 t_telemetry_latest 的 value_* 列转换为 API 层的 raw_value / eng_value。

    数据库层存储的是工程值（归一化后）。原始值通过 scale/offset 反向推导：
      工程值 = 原始值 × scale + offset
      原始值 = (工程值 - offset) / scale
    由于 Neuron 上报值可能为浮点但 t_tags 配置为 INT，这里做跨列回退。
    BOOL/STRING 类型只返回 raw_value，eng_value 为 None。
    """
    data_type = tag.get("data_type")
    value_float = tag.pop("value_float", None)
    value_int = tag.pop("value_int", None)
    value_bool = tag.pop("value_bool", None)
    value_str = tag.pop("value_str", None)

    if data_type == "BOOL":
        eng = value_bool
    elif data_type == "STRING":
        eng = value_str
    elif data_type == "INT":
        # INT 配置优先取 value_int，缺失时回退 value_float
        eng = value_int if value_int is not None else value_float
    else:  # FLOAT / 默认
        # FLOAT 配置优先取 value_float，缺失时回退 value_int
        eng = value_float if value_float is not None else value_int

    scale = tag.get("scale_factor", 1.0) or 1.0
    offset = tag.get("value_offset", 0.0) or 0.0

    if data_type in ("BOOL", "STRING"):
        tag["raw_value"] = eng
        tag["eng_value"] = None
        return

    try:
        eng_num = float(eng) if eng is not None else None
    except (TypeError, ValueError):
        eng_num = None

    tag["eng_value"] = round(eng_num, 4) if eng_num is not None else None

    if eng_num is not None and scale != 0:
        tag["raw_value"] = round((eng_num - offset) / scale, 6)
    else:
        tag["raw_value"] = eng

# ══════════════════════════════════════
# Endpoints
# ══════════════════════════════════════

@router.get(
    "/tags",
    openapi_extra=capability_metadata(RUNTIME_READ),
)
async def list_tags(
    node_id: str | None = Query(None, description="按节点过滤"),
    data_type: str | None = Query(None, description="按数据类型过滤"),
    tag_type: str | None = Query(None, description="按点位类型过滤 PHYSICAL/LOGICAL"),
    read_write: str | None = Query(None, description="按读写权限过滤 R/RW/W"),
    search: str | None = Query(None, description="按名称/显示名模糊搜索"),
    enabled: bool = Query(True, description="只看启用点位"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    sort_by: str = Query("sort_order", description="排序字段"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$", description="排序方向"),
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
) -> dict:
    """
    分页查询点位列表，附带每个点位的最新值。

    用于前端 TagsTable 主页面。
    """
    from app.services.telemetry_store import get_connection

    conditions = ["t.enabled = TRUE"] if enabled else []
    params: list = []

    if node_id:
        conditions.append("t.node_id = %s")
        params.append(UUID(node_id))
    if data_type:
        conditions.append("t.data_type = %s")
        params.append(data_type.upper())
    if tag_type:
        conditions.append("t.tag_type = %s")
        params.append(tag_type.upper())
    if read_write:
        conditions.append("t.read_write = %s")
        params.append(read_write.upper())
    if search:
        conditions.append("(t.name ILIKE %s OR t.display_name ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    # 排序白名单
    sort_map = {
        "name": "t.name",
        "display_name": "t.display_name",
        "node_name": "n.name",
        "data_type": "t.data_type",
        "unit": "t.unit",
        # raw_value/eng_value 在 Python 层从 value_* 列构造，不支持 SQL 排序
        "raw_value": "latest.value_float",
        "eng_value": "latest.value_float",
        "quality": "latest.quality",
        "latest_ts": "latest.ts",
        "scale_factor": "t.scale_factor",
        "value_offset": "t.value_offset",
        "sort_order": "t.sort_order",
    }
    order_by = sort_map.get(sort_by, "t.sort_order")
    order_dir = "DESC" if sort_order.lower() == "desc" else "ASC"

    # 分页 offset
    offset = (page - 1) * page_size

    query = f"""
    SELECT
       t.id, t.node_id, t.name, t.display_name, t.data_type, t.tag_type,
       t.unit, t.scale_factor, t.value_offset, t.source_path, t.source_type,
       t.read_write, t.enabled, t.description,
       t.aggregate_fn, t.formula, t.formula_type, t.sources,
       n.name AS node_name,
        t.alarm_level, t.alarm_type, t.alarm_threshold, t.fault_map_id,
        fm.name AS fault_map_name,
        -- 最新值缓存表 (value_* 列由 Python 层按 data_type 转换)
        latest.ts AS latest_ts,
        latest.value_float,
        latest.value_int,
        latest.value_bool,
        latest.value_str,
        latest.quality
    FROM t_tags t
    JOIN t_nodes n ON n.id = t.node_id
    LEFT JOIN t_fault_maps fm ON fm.id = t.fault_map_id
    LEFT JOIN t_telemetry_latest latest ON latest.tag_id = t.id
    {where}
    ORDER BY
        CASE WHEN latest.ts IS NOT NULL THEN 0 ELSE 1 END,
        {order_by} {order_dir}, t.sort_order, t.name
    LIMIT %s OFFSET %s
    """

    count_query = f"SELECT COUNT(*) FROM t_tags t {where}"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params + [page_size, offset])
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]

                # Get total count
                cur.execute(count_query, params)
                total = cur.fetchone()[0]

        # Serialize
        for row in rows:
            row["id"] = str(row["id"])
            row["node_id"] = str(row["node_id"])
            if row.get("latest_ts"):
                row["latest_ts"] = row["latest_ts"].isoformat()
            _coerce_latest_value(row)
            _runtime_tag(row, principal)

        return {
            "tags": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }
    except Exception as e:
        logger.error("[API/tags] Query failed: {}", e)
        return {"tags": [], "total": 0, "page": page, "page_size": page_size, "error": str(e)}


@router.get("/tags/export", **protected(CONFIGURATION_READ))
async def export_tags_csv(
    node_id: str | None = Query(None, description="按节点过滤"),
    data_type: str | None = Query(None, description="按数据类型过滤"),
    search: str | None = Query(None, description="按名称/显示名模糊搜索"),
) -> StreamingResponse:
    """
    导出点位列表为 CSV（含最新值）。
    支持当前筛选条件，最多导出 5000 条。
    """
    import csv
    import io

    from app.services.telemetry_store import get_connection

    conditions = ["t.enabled = TRUE"]
    params: list = []

    if node_id:
        conditions.append("t.node_id = %s")
        params.append(UUID(node_id))
    if data_type:
        conditions.append("t.data_type = %s")
        params.append(data_type.upper())
    if search:
        conditions.append("(t.name ILIKE %s OR t.display_name ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = " WHERE " + " AND ".join(conditions)

    query = f"""
    SELECT
        n.name AS node_name,
        t.name,
        t.display_name,
        t.data_type,
        t.unit,
        t.scale_factor,
        t.value_offset,
        latest.value_float,
        latest.value_int,
        latest.value_bool,
        latest.value_str,
        latest.ts AS latest_ts
    FROM t_tags t
    JOIN t_nodes n ON n.id = t.node_id
    LEFT JOIN t_telemetry_latest latest ON latest.tag_id = t.id
    {where}
    ORDER BY n.sort_order, t.sort_order, t.name
    LIMIT 5000
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    # 生成 CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "节点", "点位名", "显示名", "数据类型", "单位",
        "原始值", "工程值", "Scale", "Offset", "最新时间"
    ])
    for row in rows:
        node_name, name, display_name, data_type, unit, scale, offset, value_float, value_int, value_bool, value_str, ts = row
        tag = {
            "data_type": data_type,
            "value_float": value_float,
            "value_int": value_int,
            "value_bool": value_bool,
            "value_str": value_str,
        }
        _coerce_latest_value(tag)
        raw = tag["raw_value"]
        eng = tag["eng_value"]
        writer.writerow([
            node_name,
            name,
            display_name or "",
            data_type,
            unit or "",
            f"{raw:.4f}" if raw is not None else "",
            f"{eng:.4f}" if eng is not None else "",
            f"{scale:.6f}",
            f"{offset:.6f}",
            ts.isoformat() if ts else "",
        ])

    output.seek(0)
    filename = f"zizu_tags_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/tags/alarm-config", **protected(CONFIGURATION_READ))
async def list_alarm_configured_tags() -> dict:
    """列出所有已配置告警级别 (alarm_level) 的点位，按级别/类型分组。"""
    from app.services.telemetry_store import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.id, t.name, t.display_name, t.node_id, n.name AS node_name,
                           n.node_type, t.alarm_level, t.alarm_type, t.alarm_threshold,
                           t.fault_map_id, fm.name AS fault_map_name
                    FROM t_tags t
                    JOIN t_nodes n ON n.id = t.node_id
                    LEFT JOIN t_fault_maps fm ON fm.id = t.fault_map_id
                    WHERE t.alarm_level IS NOT NULL AND t.enabled = TRUE
                    ORDER BY t.alarm_level, t.alarm_type, n.node_type
                """);
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        for row in rows:
            row["id"] = str(row["id"])
            row["node_id"] = str(row["node_id"])
            if row.get("fault_map_id"):
                row["fault_map_id"] = str(row["fault_map_id"])
        return {
            "tags": rows,
            "total": len(rows),
            "deprecated": True,
            "replacement": "/api/v1/alarm-configurations",
        }
    except Exception as e:
        logger.error("[API/tags/alarm-config] failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/tags/{tag_id}",
    openapi_extra=capability_metadata(RUNTIME_READ),
)
async def get_tag(
    tag_id: UUID,
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
) -> dict:
    """获取单个点位详情 + 最新值。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
               SELECT t.id, t.node_id, t.name, t.display_name, t.data_type, t.tag_type,
                      t.unit, t.scale_factor, t.value_offset, t.source_path, t.source_type,
                      t.read_write, t.enabled, t.description,
                      t.aggregate_fn, t.formula, t.formula_type, t.sources,
                       t.alarm_level, t.alarm_type, t.alarm_threshold, t.fault_map_id,
                       fm.name AS fault_map_name,
                       n.name AS node_name,
                       latest.ts, latest.value_float, latest.value_int,
                       latest.value_bool, latest.value_str, latest.quality
                FROM t_tags t
                JOIN t_nodes n ON n.id = t.node_id
                LEFT JOIN t_fault_maps fm ON fm.id = t.fault_map_id
                LEFT JOIN t_telemetry_latest latest ON latest.tag_id = t.id
                WHERE t.id = %s
                """,
                (tag_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Tag not found")

            columns = [desc[0] for desc in cur.description]
            tag = dict(zip(columns, row))
            tag["id"] = str(tag["id"])
            tag["node_id"] = str(tag["node_id"])
            if tag.get("ts"):
                tag["latest_ts"] = tag["ts"].isoformat()
            _coerce_latest_value(tag)
            _runtime_tag(tag, principal)

    return tag


@router.get("/tags/{tag_id}/history", **protected(RUNTIME_READ))
async def get_tag_history(
    tag_id: UUID,
    range: str = Query("1h", pattern="^(1h|24h|7d)$", description="时间范围"),
) -> dict:
    """
    查询点位历史趋势数据。

    - 1h: 原始数据 (约 1-2s/条)
    - 24h: 5 分钟聚合
    - 7d: 30 分钟聚合
    """
    from app.services.telemetry_store import get_connection

    # 确定 bucket 间隔
    bucket_map = {
        "1h": None,           # 原始数据
        "24h": "5 minutes",
        "7d": "30 minutes",
    }
    interval_map = {
        "1h": "1 hour",
        "24h": "24 hours",
        "7d": "7 days",
    }
    bucket = bucket_map[range]
    interval = interval_map[range]

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 获取 tag 信息
            cur.execute(
                "SELECT name, display_name, scale_factor, value_offset FROM t_tags WHERE id = %s",
                (tag_id,),
            )
            tag_row = cur.fetchone()
            if not tag_row:
                raise HTTPException(status_code=404, detail="Tag not found")
            tag_name, display_name, scale_factor, value_offset = tag_row

            if bucket:
                # 聚合查询
                query = """
                SELECT
                    time_bucket(%s::interval, ts) AS bucket_ts,
                    AVG(COALESCE(value_float, value_int::float)) AS raw_value
                FROM t_telemetry
                WHERE tag_id = %s AND ts > NOW() - %s::interval
                GROUP BY bucket_ts
                ORDER BY bucket_ts ASC
                """
                cur.execute(query, (bucket, tag_id, interval))
            else:
                # 原始数据，但限制最多 2000 条防止爆内存
                query = """
                SELECT ts AS bucket_ts, COALESCE(value_float, value_int::float) AS raw_value
                FROM t_telemetry
                WHERE tag_id = %s AND ts > NOW() - %s::interval
                ORDER BY ts ASC
                LIMIT 2000
                """
                cur.execute(query, (tag_id, interval))

            points = []
            for row in cur.fetchall():
                ts, raw = row
                eng = None
                if raw is not None:
                    eng = round(float(raw), 4)
                points.append({
                    "ts": ts.isoformat(),
                    "raw_value": round(float(raw), 4) if raw is not None else None,
                    "eng_value": eng,
                })

    return {
        "tag_id": str(tag_id),
        "tag_name": display_name or tag_name,
        "range": range,
        "bucket": bucket or "raw",
        "points": points,
    }


class BatchUpdateRequest(BaseModel):
    """批量更新请求。"""
    tag_ids: list[str] = Field(..., description="点位 ID 列表")
    scale_factor: float | None = Field(None, description="统一缩放系数")
    value_offset: float | None = Field(None, description="统一偏移量")
    unit: str | None = Field(None, description="统一单位")
    read_write: str | None = Field(None, pattern="^[RrWw]+$", description="统一读写权限")
    enabled: bool | None = Field(None, description="统一启用状态")
    node_id: str | None = Field(None, description="统一移动到目标节点 UUID")
    alarm_level: Any = None
    alarm_type: Any = None
    alarm_threshold: Any = None
    fault_map_id: Any = None



@router.put("/tags/batch", **protected(CONFIGURATION_WRITE))
async def batch_update_tags(req: BatchUpdateRequest) -> dict:
    """
    批量更新点位的 scale_factor / value_offset / unit / read_write / enabled，
    或批量移动到新的 node_id。
    """
    _reject_legacy_alarm_fields(req)
    from app.services.telemetry_store import get_connection

    if not req.tag_ids:
        return {"status": "no_change", "updated": 0}

    updates = []
    params: list = []
    if req.scale_factor is not None:
        updates.append("scale_factor = %s")
        params.append(req.scale_factor)
    if req.value_offset is not None:
        updates.append("value_offset = %s")
        params.append(req.value_offset)
    if req.unit is not None:
        updates.append("unit = %s")
        params.append(req.unit)
    if req.read_write is not None:
        updates.append("read_write = %s")
        params.append(req.read_write.upper())
    if req.enabled is not None:
        updates.append("enabled = %s")
        params.append(req.enabled)
    if req.alarm_level is not None:
        updates.append("alarm_level = %s")
        params.append(req.alarm_level if req.alarm_level else None)
    if req.alarm_type is not None:
        updates.append("alarm_type = %s")
        params.append(req.alarm_type if req.alarm_type else None)
    if req.alarm_threshold is not None:
        updates.append("alarm_threshold = %s")
        params.append(req.alarm_threshold)
    if req.fault_map_id is not None:
        updates.append("fault_map_id = %s")
        params.append(UUID(req.fault_map_id) if req.fault_map_id else None)
    if req.node_id is not None:
        try:
            target_node = UUID(req.node_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid node_id (not a UUID)")
        updates.append("node_id = %s")
        params.append(target_node)

    if not updates:
        return {"status": "no_change", "updated": 0}

    updates.append("updated_at = %s")
    params.append(datetime.now(timezone.utc))

    # 构建 IN 子句
    uuid_params = [UUID(tid) for tid in req.tag_ids]
    placeholders = ",".join(["%s"] * len(uuid_params))
    params.extend(uuid_params)

    query = f"""
    UPDATE t_tags
    SET {", ".join(updates)}
    WHERE id IN ({placeholders})
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 若移动节点，校验目标节点存在
                if req.node_id is not None:
                    cur.execute("SELECT id FROM t_nodes WHERE id = %s", (target_node,))
                    if not cur.fetchone():
                        raise HTTPException(status_code=404, detail="Target node not found")

                    # 避免目标节点内出现同名点位
                    placeholders = ",".join(["%s"] * len(uuid_params))
                    cur.execute(
                        f"""
                        SELECT t.name
                        FROM t_tags t
                        WHERE t.node_id = %s AND t.id NOT IN ({placeholders})
                        """,
                        [target_node] + uuid_params,
                    )
                    existing_names = {r[0] for r in cur.fetchall()}
                    cur.execute(
                        f"""
                        SELECT name FROM t_tags WHERE id IN ({placeholders})
                        """,
                        uuid_params,
                    )
                    moving_names = {r[0] for r in cur.fetchall()}
                    conflicts = existing_names & moving_names
                    if conflicts:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Duplicate tag names in target node: {sorted(conflicts)}",
                        )

                cur.execute(query, params)
                conn.commit()
                updated = cur.rowcount

        # Trigger immediate pipeline reload
        try:
            from app.main import get_pipeline
            pipeline = get_pipeline()
            if pipeline:
                import asyncio as _aio
                _aio.ensure_future(pipeline.reload_rules_now())
        except Exception:
            pass

        return {"status": "ok", "updated": updated}
    except Exception as e:
        logger.error("[API/tags/batch] Update failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/tags/{tag_id}", **protected(CONFIGURATION_WRITE))
async def delete_tag(tag_id: UUID) -> dict:
    """删除单个点位及其历史遥测、最新值缓存。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM t_tags WHERE id = %s", (tag_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Tag not found")

            # t_telemetry / t_telemetry_latest 外键级联删除；migration_030
            # 已移除只读 t_alarms 的外键，以保留不可变告警历史。
            cur.execute("DELETE FROM t_tags WHERE id = %s", (tag_id,))
            conn.commit()

    logger.info("[API/tags] deleted tag {}", tag_id)
    return {"status": "ok", "deleted": str(tag_id)}


@router.put("/tags/{tag_id}", **protected(CONFIGURATION_WRITE))
async def update_tag(tag_id: UUID, req: TagUpdateRequest) -> dict:
    """
    更新点位配置（offset/scale/unit 等）。

    只更新 req 中非 None 的字段（部分更新）。
    """
    _reject_legacy_alarm_fields(req)
    from app.services.telemetry_store import get_connection

    data = req.model_dump(exclude_none=True)

    if "alarm_level" in data:
        data["alarm_level"] = data["alarm_level"].lower() if data["alarm_level"] else None
    if "fault_map_id" in data:
        data["fault_map_id"] = UUID(data["fault_map_id"]) if data["fault_map_id"] else None

    # 校验 LogicalTag 字段
    if "aggregate_fn" in data and data["aggregate_fn"] not in _AGG_FNS:
        raise HTTPException(status_code=400, detail=f"aggregate_fn must be one of {sorted(_AGG_FNS)}")
    if "formula_type" in data and data["formula_type"] not in _FORMULA_TYPES:
        raise HTTPException(status_code=400, detail=f"formula_type must be one of {sorted(_FORMULA_TYPES)}")

    # 构建动态 UPDATE
    updates = []
    params: list = []
    for field, value in data.items():
        if field == "sources":
            # UUID[] — 转为 psycopg2 可识别的 UUID 列表
            updates.append("sources = %s")
            params.append([UUID(s) for s in value])
        else:
            updates.append(f"{field} = %s")
            params.append(value)

    if not updates:
        return {"status": "no_change", "message": "No fields to update"}

    updates.append("updated_at = %s")
    params.append(datetime.now(timezone.utc))
    params.append(tag_id)

    query = f"""
    UPDATE t_tags
    SET {", ".join(updates)}
    WHERE id = %s
    RETURNING id, name, scale_factor, value_offset, unit
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                conn.commit()
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Tag not found")

        return {
            "status": "ok",
            "tag": {
                "id": str(row[0]),
                "name": row[1],
                "scale_factor": float(row[2]),
                "value_offset": float(row[3]),
                "unit": row[4],
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/tags/{tag_id}] Update failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════
# Neuron 同步导入 (M1 / F3 · S10)
# ══════════════════════════════════════

class NeuronImportRequest(BaseModel):
    """从 Neuron 采集组批量导入点位到指定节点。"""
    node_id: str = Field(..., description="ZiZu 目标节点 (Station/EnergyNode) UUID")
    neuron_node: str = Field(..., description="Neuron 南向节点名 (driver node)")
    neuron_group: str = Field(..., description="Neuron 采集组名")


# Neuron data type code → ZiZu data_type
# 参考 Neuron: 3=INT16 4=UINT16 5=INT32 6=UINT32 9=FLOAT 10=DOUBLE 11=BIT ...
# NOTE: data_type 必须为大写 (t_tags CHECK 约束: FLOAT/INT/BOOL/STRING/ENUM)
_NEURON_TYPE_MAP = {
    3: "INT", 4: "INT", 5: "INT", 6: "INT", 7: "INT", 8: "INT",
    9: "FLOAT", 10: "FLOAT", 11: "BOOL", 13: "STRING",
}


@router.post("/tags/import-neuron", **protected(CONFIGURATION_WRITE))
async def import_neuron_tags(req: NeuronImportRequest) -> dict:
    """
    从 Neuron 指定采集组拉取点位，作为 PHYSICAL 点位挂载到目标节点。

    - source_type = "neuron"
    - source_path = "{neuron_node}/{neuron_group}/{tag_name}" (供采集管道路由)
    - 已存在同名点位 (同 node_id + name) 则跳过，避免重复导入。
    """
    from app.services.telemetry_store import get_connection
    from app.services.neuron_client import get_neuron_client

    # 1) 校验目标节点
    try:
        node_uuid = UUID(req.node_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid node_id (not a UUID)")

    # 2) 从 Neuron 拉取点位
    try:
        client = get_neuron_client()
        neuron_tags = client.get_tags(req.neuron_node, req.neuron_group)
    except Exception as e:
        logger.error("[API/tags/import-neuron] Neuron fetch failed: {}", e)
        raise HTTPException(status_code=502, detail=f"Neuron fetch failed: {e}")

    if not neuron_tags:
        return {"imported": 0, "skipped": 0, "message": "No tags in Neuron group"}

    imported = 0
    skipped = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 校验节点存在
                cur.execute("SELECT id FROM t_nodes WHERE id = %s", (node_uuid,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Target node not found")

                # 已存在点位名集合 (去重)
                cur.execute(
                    "SELECT name FROM t_tags WHERE node_id = %s", (node_uuid,)
                )
                existing = {r[0] for r in cur.fetchall()}

                for nt in neuron_tags:
                    tname = nt.get("name")
                    if not tname or tname in existing:
                        skipped += 1
                        continue

                    dtype = _NEURON_TYPE_MAP.get(nt.get("type"), "FLOAT")
                    # Neuron attribute bit0=read bit1=write → RW 权限
                    attr = nt.get("attribute", 1)
                    rw = "RW" if (attr & 0x02) else "R"
                    source_path = f"{req.neuron_node}/{req.neuron_group}/{tname}"

                    cur.execute(
                        """
                        INSERT INTO t_tags (node_id, name, display_name, data_type, tag_type,
                                            source_type, source_path, read_write, enabled)
                        VALUES (%s, %s, %s, %s, 'PHYSICAL', 'neuron', %s, %s, TRUE)
                        """,
                        (node_uuid, tname, tname, dtype, source_path, rw),
                    )
                    imported += 1
                    existing.add(tname)

                conn.commit()

        logger.info(
            "[API/tags/import-neuron] node={} group={}/{} imported={} skipped={}",
            req.node_id, req.neuron_node, req.neuron_group, imported, skipped,
        )
        return {"imported": imported, "skipped": skipped, "total": len(neuron_tags)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/tags/import-neuron] Insert failed: {}", e)
        raise HTTPException(status_code=500, detail=f"Import failed: {e}")


# ══════════════════════════════════════
# LogicalTag 创建 — 汇总规则挂载 (M5 / F3 · S11)
# ══════════════════════════════════════

class TagCreateRequest(BaseModel):
    """创建点位。支持自定义 PHYSICAL（手动录入来源）和 LOGICAL（公式/聚合派生）点位。"""
    node_id: str = Field(..., description="挂载的目标节点 UUID")
    name: str = Field(..., min_length=1, description="点位名 (节点内唯一)")
    tag_type: str = Field("LOGICAL", pattern="^(PHYSICAL|LOGICAL)$", description="PHYSICAL/LOGICAL")
    data_type: str = Field("FLOAT", description="FLOAT/INT/BOOL/STRING/ENUM")
    display_name: str | None = Field(None, description="显示名称")
    unit: str | None = Field(None, description="单位")
    description: str | None = Field(None, description="描述")
    read_write: str = Field("R", pattern="^[RrWw]+$", description="读写权限")
    source_type: str = Field("manual", description="来源类型：neuron / manual / opcua / modbus 等")
    source_path: str | None = Field(None, description="来源路径，如 neuron/node/group/tag 或自定义")
    # LogicalTag 汇总规则
    aggregate_fn: str | None = Field(None, description="聚合函数 SUM/AVG/MAX/MIN/COUNT/LAST")
    formula: str | None = Field(None, description="表达式或聚合来源引用")
    formula_type: str | None = Field("expression", description="expression/aggregate/condition")
    sources: list[str] = Field(default_factory=list, description="来源点位 UUID 列表")
    alarm_level: Any = None
    alarm_type: Any = None
    alarm_threshold: Any = None
    fault_map_id: Any = None


@router.post(
    "/tags",
    status_code=status.HTTP_201_CREATED,
    **protected(CONFIGURATION_WRITE),
)
async def create_tag(req: TagCreateRequest) -> dict:
    """
    创建点位并挂载到指定节点。

    LOGICAL 点位用于节点树汇总聚合 (由 F3 聚合器每 10s 计算)：
    - formula_type="aggregate" + aggregate_fn=SUM/AVG/... + sources=[子点位UUID...]
    - 聚合器按 aggregate_fn 汇总 sources 的最新值，写回本点位 (is_virtual=True)
    """
    _reject_legacy_alarm_fields(req)
    from app.services.telemetry_store import get_connection

    # 1) 校验
    try:
        node_uuid = UUID(req.node_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid node_id (not a UUID)")

    alarm_level = req.alarm_level.lower() if req.alarm_level else None
    alarm_type_val = req.alarm_type if req.alarm_type else None
    alarm_threshold_val = req.alarm_threshold
    fault_map_uuid = UUID(req.fault_map_id) if req.fault_map_id else None

    data_type = req.data_type.upper()
    if data_type not in {"FLOAT", "INT", "BOOL", "STRING", "ENUM"}:
        raise HTTPException(status_code=400, detail="Invalid data_type")
    source_type = "neuron" if req.tag_type == "PHYSICAL" else (req.source_type or "manual")

    if req.tag_type == "LOGICAL":
        if req.formula_type not in _FORMULA_TYPES:
            raise HTTPException(status_code=400, detail=f"formula_type must be one of {sorted(_FORMULA_TYPES)}")
        if req.formula_type == "aggregate":
            if req.aggregate_fn not in _AGG_FNS:
                raise HTTPException(status_code=400, detail=f"aggregate_fn must be one of {sorted(_AGG_FNS)}")
            if not req.sources:
                raise HTTPException(status_code=400, detail="aggregate LogicalTag requires non-empty 'sources'")
        elif req.formula_type == "expression":
            if not req.formula:
                raise HTTPException(status_code=400, detail="expression LogicalTag requires non-empty 'formula'")

    try:
        source_uuids = [UUID(s) for s in req.sources]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID in 'sources'")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 校验节点存在
                cur.execute("SELECT id FROM t_nodes WHERE id = %s", (node_uuid,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Target node not found")

                # 节点内点位名唯一性
                cur.execute(
                    "SELECT 1 FROM t_tags WHERE node_id = %s AND name = %s",
                    (node_uuid, req.name),
                )
                if cur.fetchone():
                    raise HTTPException(status_code=409, detail="Tag name already exists on this node")

                if fault_map_uuid:
                    cur.execute("SELECT id FROM t_fault_maps WHERE id = %s", (fault_map_uuid,))
                    if not cur.fetchone():
                        raise HTTPException(status_code=404, detail="Fault map not found")

                cur.execute(
                    """
                    INSERT INTO t_tags (node_id, name, display_name, data_type, tag_type,
                                        unit, description, read_write, source_type, source_path,
                                        aggregate_fn, formula, formula_type, sources,
                                        alarm_level, alarm_type, alarm_threshold, fault_map_id, enabled)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        node_uuid, req.name, req.display_name, data_type, req.tag_type,
                        req.unit, req.description, req.read_write.upper(), source_type, req.source_path,
                        req.aggregate_fn, req.formula,
                        req.formula_type if req.tag_type == "LOGICAL" else None,
                        source_uuids, alarm_level, alarm_type_val, alarm_threshold_val, fault_map_uuid, True,
                    ),
                )
                new_id = cur.fetchone()[0]
                conn.commit()

        logger.info(
            "[API/tags] created {} tag id={} name={} on node={}",
            req.tag_type, new_id, req.name, req.node_id,
        )
        return {"status": "ok", "id": str(new_id), "name": req.name, "tag_type": req.tag_type}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/tags] Create failed: {}", e)
        raise HTTPException(status_code=500, detail=f"Create failed: {e}")
