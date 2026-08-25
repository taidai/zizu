"""
ZiZu Alarms API - 告警中心
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from app.api.business_security import (
    RUNTIME_READ,
    protected,
)

router = APIRouter()


def _serialize_alarm(row: dict) -> dict:
    row = dict(row)
    row["id"] = str(row["id"])
    for key in ["rule_id", "node_id", "entity_id"]:
        if row.get(key):
            row[key] = str(row[key])
    for key in ["created_at", "ack_at", "resolved_at"]:
        if row.get(key):
            row[key] = row[key].isoformat()
    return row


@router.get("/alarms", **protected(RUNTIME_READ))
async def list_alarms(
    level: str | None = Query(None, pattern="^(INFO|WARNING|MAJOR|CRITICAL)$"),
    source_key: str | None = Query(None, pattern="^(error1|error2|error3)$"),
    acknowledged: bool | None = Query(None),
    resolved: bool | None = Query(None),
    node_id: UUID | None = Query(None),
    entity_id: UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict:
    """分页查询告警，支持严重度/确认/恢复状态过滤。"""
    from app.services.telemetry_store import get_connection

    conditions = []
    params: list = []
    if level:
        conditions.append("a.level = %s")
        params.append(level)
    if source_key:
        conditions.append("a.source_key = %s")
        params.append(source_key)
    if acknowledged is not None:
        conditions.append("a.acknowledged = %s")
        params.append(acknowledged)
    if resolved is not None:
        conditions.append("a.resolved_at IS " + ("NOT NULL" if resolved else "NULL"))
    if node_id:
        conditions.append("a.node_id = %s")
        params.append(node_id)
    if entity_id:
        conditions.append("a.entity_id = %s")
        params.append(entity_id)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size

    query = f"""
    SELECT a.id, a.rule_id, a.node_id, a.level, a.message,
           a.acknowledged, a.ack_user, a.ack_at, a.created_at, a.resolved_at,
           a.source_topic, a.source_key, a.external_id,
           a.alarm_type, a.alarm_threshold, a.alarm_source, a.alarm_count, a.alarm_code,
           a.entity_id, e.name AS entity_name,
           r.name AS rule_name, n.name AS node_name
    FROM t_alarms a
    LEFT JOIN t_rules r ON r.id = a.rule_id
    LEFT JOIN t_nodes n ON n.id = a.node_id
    LEFT JOIN t_entities e ON e.id = a.entity_id
    {where}
    ORDER BY
        CASE a.level WHEN 'CRITICAL' THEN 0 WHEN 'MAJOR' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END,
        a.created_at DESC
    LIMIT %s OFFSET %s
    """
    count_query = f"SELECT COUNT(*) FROM t_alarms a {where}"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params + [page_size, offset])
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
                cur.execute(count_query, params)
                total = cur.fetchone()[0]

        return {
            "alarms": [_serialize_alarm(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }
    except Exception as e:
        logger.error("[API/alarms] list failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/alarms/alarm-types", **protected(RUNTIME_READ))
async def list_alarm_types() -> dict:
    """返回标准告警类型列表 (GB/T 36276, GB/T 19963, GB/T 51048)。"""
    from app.services.alarm_logic import STANDARD_ALARM_TYPES
    return {"types": sorted(STANDARD_ALARM_TYPES)}

@router.get("/alarms/counts", **protected(RUNTIME_READ))
async def alarm_counts(
    node_ids: list[str] | None = Query(None, description="节点 ID 列表，逗号分隔"),
) -> dict:
    """按节点统计统一活动告警事件，用于节点树角标。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            if node_ids:
                try:
                    uuids = [UUID(nid) for nid in node_ids]
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid node_id in list")
                placeholders = ",".join(["%s"] * len(uuids))
                cur.execute(
                    f"""
                    SELECT entity.node_id, COUNT(DISTINCT event.id) AS cnt
                    FROM t_alarm_events event
                    JOIN t_entity_instances entity
                      ON entity.id = event.entity_instance_id
                    WHERE event.state IN ('active_unacknowledged', 'active_acknowledged')
                      AND entity.node_id IN ({placeholders})
                    GROUP BY entity.node_id
                    """,
                    uuids,
                )
            else:
                cur.execute(
                    """
                    SELECT entity.node_id, COUNT(DISTINCT event.id) AS cnt
                    FROM t_alarm_events event
                    JOIN t_entity_instances entity
                      ON entity.id = event.entity_instance_id
                    WHERE event.state IN ('active_unacknowledged', 'active_acknowledged')
                    GROUP BY entity.node_id
                    """
                )
            counts = {str(row[0]): row[1] for row in cur.fetchall()}

    return {"counts": counts}
@router.get("/alarms/entities", **protected(RUNTIME_READ))
async def list_alarm_entities() -> dict:
    """返回当前有活动事件的实体实例列表。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT instance.id, instance.definition_id, instance.display_name
                FROM t_alarm_events event
                JOIN t_entity_instances instance ON instance.id = event.entity_instance_id
                WHERE event.state IN ('active_unacknowledged', 'active_acknowledged')
                ORDER BY instance.display_name, instance.definition_id
            """)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return {"items": [{"id": str(r["id"]), "name": r["definition_id"], "display_name": r.get("display_name")} for r in rows]}

@router.get("/alarms/group-counts", **protected(RUNTIME_READ))
async def alarm_group_counts() -> dict:
    """按严重度统计统一活动事件。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT severity, COUNT(*) AS cnt
                FROM t_alarm_events
                WHERE state IN ('active_unacknowledged', 'active_acknowledged')
                GROUP BY severity
                """
            )
            counts = {row[0]: row[1] for row in cur.fetchall()}

    for key in ("CRITICAL", "MAJOR", "WARNING", "INFO"):
        counts.setdefault(key, 0)
    return {"counts": counts}


# New lifecycle state is owned by t_alarm_events; this module only exposes history and summaries.
