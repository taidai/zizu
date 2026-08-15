"""
ZiZu Alarm Levels API - 自定义告警等级 + 全局实体批量绑定
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from loguru import logger

from app.api.business_security import (
    CONFIGURATION_READ,
    CONFIGURATION_WRITE,
    protected,
)

router = APIRouter()
_LEGACY_REPLACEMENT = "/api/v1/alarm-configurations"


def _migration_required() -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "ALARM_CONFIGURATION_MIGRATION_REQUIRED",
            "message": "Legacy alarm configuration is read-only; use an explicit migration plan",
        },
    )


def _legacy_read(items: dict) -> dict:
    return {
        **items,
        "deprecated": True,
        "replacement": _LEGACY_REPLACEMENT,
    }


def _serialize_level(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "code": row["code"],
        "name": row["name"],
        "severity": row["severity"],
        "color": row.get("color"),
        "trigger_rules": row.get("trigger_rules") or [],
        "enabled": row["enabled"],
        "sort_order": row.get("sort_order", 0),
        "is_system": row.get("is_system", False),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _serialize_binding(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "entity_id": str(row["entity_id"]),
        "entity_name": row.get("entity_name"),
        "entity_display_name": row.get("entity_display_name"),
        "alarm_level_id": str(row["alarm_level_id"]),
        "trigger_rules": row.get("trigger_rules") or [],
        "fault_map_id": str(row["fault_map_id"]) if row.get("fault_map_id") else None,
        "fault_map_name": row.get("fault_map_name"),
        "enabled": row["enabled"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


# ── Endpoints ──

@router.get("/alarm-levels", **protected(CONFIGURATION_READ))
async def list_alarm_levels(enabled_only: bool = Query(False)) -> dict:
    from app.services.telemetry_store import get_connection

    where = "WHERE enabled = TRUE" if enabled_only else ""
    query = f"""
        SELECT id, code, name, severity, color, trigger_rules, enabled, sort_order, is_system, created_at, updated_at
        FROM t_alarm_levels
        {where}
        ORDER BY sort_order ASC, created_at ASC
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        return _legacy_read({"items": [_serialize_level(r) for r in rows]})
    except Exception as e:
        logger.error("[API/alarm-levels] list failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/alarm-levels",
    status_code=status.HTTP_201_CREATED,
    **protected(CONFIGURATION_WRITE),
)
async def create_alarm_level(req: Request) -> dict:
    del req
    _migration_required()


@router.get("/alarm-levels/{level_id}", **protected(CONFIGURATION_READ))
async def get_alarm_level(level_id: UUID) -> dict:
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, code, name, severity, color, trigger_rules, enabled, sort_order, is_system, created_at, updated_at
                FROM t_alarm_levels WHERE id = %s
                """,
                (level_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Alarm level not found")
            columns = [desc[0] for desc in cur.description]
    return _legacy_read({"item": _serialize_level(dict(zip(columns, row)))})


@router.put("/alarm-levels/{level_id}", **protected(CONFIGURATION_WRITE))
async def update_alarm_level(level_id: UUID, req: Request) -> dict:
    del level_id, req
    _migration_required()


@router.delete("/alarm-levels/{level_id}", **protected(CONFIGURATION_WRITE))
async def delete_alarm_level(level_id: UUID) -> dict:
    del level_id
    _migration_required()


@router.get(
    "/alarm-levels/{level_id}/entities",
    **protected(CONFIGURATION_READ),
)
async def list_level_entities(level_id: UUID) -> dict:
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.id, b.entity_id, b.alarm_level_id, b.trigger_rules, b.enabled, b.created_at,
                       b.fault_map_id,
                       e.name AS entity_name, e.display_name AS entity_display_name,
                       fm.name AS fault_map_name
                FROM t_entity_alarm_bindings b
                JOIN t_entities e ON e.id = b.entity_id
                LEFT JOIN t_fault_maps fm ON fm.id = b.fault_map_id
                WHERE b.alarm_level_id = %s
                ORDER BY e.name
                """,
                (level_id,),
            )
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return _legacy_read({"items": [_serialize_binding(r) for r in rows]})


@router.post(
    "/alarm-levels/{level_id}/entities",
    **protected(CONFIGURATION_WRITE),
)
async def batch_bind_entities(level_id: UUID, req: Request) -> dict:
    del level_id, req
    _migration_required()


@router.delete(
    "/alarm-levels/{level_id}/entities/{binding_id}",
    **protected(CONFIGURATION_WRITE),
)
async def unbind_entity(level_id: UUID, binding_id: UUID) -> dict:
    del level_id, binding_id
    _migration_required()


@router.get(
    "/entities/{entity_id}/alarm-levels",
    **protected(CONFIGURATION_READ),
)
async def list_entity_alarm_levels(entity_id: UUID) -> dict:
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.id, b.entity_id, b.alarm_level_id, b.trigger_rules, b.enabled, b.created_at,
                       l.code, l.name, l.severity, l.color
                FROM t_entity_alarm_bindings b
                JOIN t_alarm_levels l ON l.id = b.alarm_level_id
                WHERE b.entity_id = %s
                ORDER BY l.sort_order
                """,
                (entity_id,),
            )
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return _legacy_read({"items": [_serialize_binding(r) for r in rows]})
