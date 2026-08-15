"""
ZiZu Telemetry API — 数据表查询（验证数据入库）

GET    /api/v1/telemetry           → 分页查询 t_telemetry 原始数据
GET    /api/v1/telemetry/export    → 导出 CSV
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from app.api.business_security import CONFIGURATION_READ, RUNTIME_READ, protected

router = APIRouter()


class TelemetryPoint(BaseModel):
    ts: str
    tag_id: str
    tag_name: str
    node_name: str
    raw_value: float | None
    eng_value: float | None
    quality: int | None


def _cursor_scope(
    tag_id: str | None,
    node_id: str | None,
    range_name: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {"tag_id": tag_id, "node_id": node_id, "range": range_name},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _encode_cursor(ts: datetime, tag_id: UUID, scope: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "ts": ts.isoformat(),
            "tag_id": str(tag_id),
            "scope": scope,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str, expected_scope: str) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
        )
        ts = datetime.fromisoformat(payload["ts"])
        tag_id = UUID(payload["tag_id"])
        if (
            payload.get("v") != 1
            or payload.get("scope") != expected_scope
            or ts.tzinfo is None
        ):
            raise ValueError("cursor scope or version does not match")
        return ts, tag_id
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TELEMETRY_CURSOR_INVALID",
                "message": "Telemetry cursor is invalid for this query",
            },
        ) from exc


@router.get("/telemetry", **protected(RUNTIME_READ))
async def list_telemetry(
    tag_id: str | None = Query(None, description="按点位过滤"),
    node_id: str | None = Query(None, description="按节点过滤"),
    range: str = Query("1h", pattern="^(1h|24h|7d|all)$", description="时间范围"),
    cursor: str | None = Query(None, description="上一页返回的不透明游标"),
    page_size: int = Query(50, ge=1, le=500, description="每页条数"),
) -> dict:
    """
    用稳定游标查询 t_telemetry 原始数据；交互查询不执行精确计数。
    """
    from app.services.telemetry_store import get_connection

    conditions = []
    params: list = []

    if tag_id:
        conditions.append("t.tag_id = %s")
        params.append(UUID(tag_id))
    if node_id:
        conditions.append("tag.node_id = %s")
        params.append(UUID(node_id))

    interval_map = {
        "1h": "1 hour",
        "24h": "24 hours",
        "7d": "7 days",
    }
    if range != "all":
        conditions.append("t.ts > NOW() - %s::interval")
        params.append(interval_map[range])

    scope = _cursor_scope(tag_id, node_id, range)
    if cursor:
        cursor_ts, cursor_tag_id = _decode_cursor(cursor, scope)
        conditions.append("(t.ts, t.tag_id) < (%s, %s)")
        params.extend((cursor_ts, cursor_tag_id))

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
    SELECT
        t.ts,
        t.tag_id,
        tag.name AS tag_name,
        tag.display_name,
        n.name AS node_name,
        COALESCE(t.value_float, t.value_int::float) AS raw_value,
        CASE
            WHEN COALESCE(t.value_float, t.value_int::float) IS NULL THEN NULL
            WHEN tag.scale_factor = 1.0 AND tag.value_offset = 0.0
                THEN COALESCE(t.value_float, t.value_int::float)
            ELSE (COALESCE(t.value_float, t.value_int::float) + tag.value_offset) * tag.scale_factor
        END AS eng_value,
        t.quality
    FROM t_telemetry t
    JOIN t_tags tag ON tag.id = t.tag_id
    JOIN t_nodes n ON n.id = tag.node_id
    {where}
    ORDER BY t.ts DESC, t.tag_id DESC
    LIMIT %s
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params + [page_size + 1])
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]

        has_more = len(rows) > page_size
        rows = rows[:page_size]
        next_cursor = (
            _encode_cursor(rows[-1]["ts"], rows[-1]["tag_id"], scope)
            if has_more and rows
            else None
        )

        for row in rows:
            row["tag_id"] = str(row["tag_id"])
            row["ts"] = row["ts"].isoformat()
            if row.get("raw_value") is not None:
                row["raw_value"] = round(float(row["raw_value"]), 4)
            if row.get("eng_value") is not None:
                row["eng_value"] = round(float(row["eng_value"]), 4)
            row["tag_name"] = row.get("display_name") or row["tag_name"]

        return {
            "points": rows,
            "total": None,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "page_size": page_size,
        }
    except Exception as e:
        logger.error("[API/telemetry] Query failed: {}", e)
        return {
            "points": [],
            "total": None,
            "has_more": False,
            "next_cursor": None,
            "page_size": page_size,
            "error": str(e),
        }


@router.get("/telemetry/export", **protected(RUNTIME_READ))
async def export_telemetry_csv(
    tag_id: str | None = Query(None, description="按点位过滤"),
    node_id: str | None = Query(None, description="按节点过滤"),
    range: str = Query("1h", pattern="^(1h|24h|7d|all)$", description="时间范围"),
) -> StreamingResponse:
    """
    导出 t_telemetry 数据为 CSV。
    """
    import csv
    import io

    from app.services.telemetry_store import get_connection

    conditions = []
    params: list = []

    if tag_id:
        conditions.append("t.tag_id = %s")
        params.append(UUID(tag_id))
    if node_id:
        conditions.append("tag.node_id = %s")
        params.append(UUID(node_id))

    interval_map = {
        "1h": "1 hour",
        "24h": "24 hours",
        "7d": "7 days",
    }
    if range != "all":
        conditions.append("t.ts > NOW() - %s::interval")
        params.append(interval_map[range])

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
    SELECT
        t.ts,
        n.name AS node_name,
        tag.name AS tag_name,
        tag.display_name,
        COALESCE(t.value_float, t.value_int::float) AS raw_value,
        CASE
            WHEN COALESCE(t.value_float, t.value_int::float) IS NULL THEN NULL
            WHEN tag.scale_factor = 1.0 AND tag.value_offset = 0.0
                THEN COALESCE(t.value_float, t.value_int::float)
            ELSE (COALESCE(t.value_float, t.value_int::float) + tag.value_offset) * tag.scale_factor
        END AS eng_value,
        t.quality
    FROM t_telemetry t
    JOIN t_tags tag ON tag.id = t.tag_id
    JOIN t_nodes n ON n.id = tag.node_id
    {where}
    ORDER BY t.ts DESC
    LIMIT 10000
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["时间", "节点", "点位名", "显示名", "原始值", "工程值", "Quality"])
    for row in rows:
        ts, node_name, tag_name, display_name, raw, eng, quality = row
        writer.writerow([
            ts.isoformat() if ts else "",
            node_name,
            tag_name,
            display_name or "",
            f"{raw:.4f}" if raw is not None else "",
            f"{eng:.4f}" if eng is not None else "",
            quality or "",
        ])

    output.seek(0)
    filename = f"zizu_telemetry_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
