"""
ZiZu Alarm Levels API - 自定义告警等级 + 全局实体批量绑定
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter()


# ── Models ──

class TriggerRule(BaseModel):
    op: str = Field(..., pattern="^(active|eq|ne|gte|gt|lte|lt|fault)$")
    value: str | int | float | None = None
    threshold: float | int | None = None
    fault_map_id: str | None = None


class AlarmLevelCreateRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    severity: str = Field(..., pattern="^(CRITICAL|MAJOR|WARNING|INFO)$")
    color: str | None = Field(None, max_length=50)
    trigger_rules: list[TriggerRule] = Field(default_factory=list)
    enabled: bool = True
    sort_order: int = 0


class AlarmLevelUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    severity: str | None = Field(None, pattern="^(CRITICAL|MAJOR|WARNING|INFO)$")
    color: str | None = Field(None, max_length=50)
    trigger_rules: list[TriggerRule] | None = None
    enabled: bool | None = None
    sort_order: int | None = None


class BatchEntityBindRequest(BaseModel):
    entity_ids: list[str] = Field(..., min_length=1)
    trigger_rules: list[TriggerRule] | None = None
    fault_map_id: str | None = None
    enabled: bool = True


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


def _rules_to_json(rules: list[TriggerRule] | None) -> list[dict] | None:
    if rules is None:
        return None
    return [r.model_dump(exclude_none=True) for r in rules]


# ── Endpoints ──

@router.get("/alarm-levels")
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
        return {"items": [_serialize_level(r) for r in rows]}
    except Exception as e:
        logger.error("[API/alarm-levels] list failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/alarm-levels", status_code=status.HTTP_201_CREATED)
async def create_alarm_level(req: AlarmLevelCreateRequest) -> dict:
    from app.services.telemetry_store import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO t_alarm_levels
                    (code, name, severity, color, trigger_rules, enabled, sort_order, is_system)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                    RETURNING id, created_at
                    """,
                    (
                        req.code, req.name, req.severity, req.color,
                        _rules_to_json(req.trigger_rules), req.enabled, req.sort_order,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
        return {"id": str(row[0]), "created_at": row[1].isoformat()}
    except Exception as e:
        logger.error("[API/alarm-levels] create failed: {}", e)
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Alarm level code already exists")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/alarm-levels/{level_id}")
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
    return {"item": _serialize_level(dict(zip(columns, row)))}


@router.put("/alarm-levels/{level_id}")
async def update_alarm_level(level_id: UUID, req: AlarmLevelUpdateRequest) -> dict:
    from app.services.telemetry_store import get_connection

    updates = []
    params = []
    if req.name is not None:
        updates.append("name = %s")
        params.append(req.name)
    if req.severity is not None:
        updates.append("severity = %s")
        params.append(req.severity)
    if req.color is not None:
        updates.append("color = %s")
        params.append(req.color)
    if req.trigger_rules is not None:
        updates.append("trigger_rules = %s")
        params.append(_rules_to_json(req.trigger_rules))
    if req.enabled is not None:
        updates.append("enabled = %s")
        params.append(req.enabled)
    if req.sort_order is not None:
        updates.append("sort_order = %s")
        params.append(req.sort_order)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT is_system FROM t_alarm_levels WHERE id = %s", (level_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Alarm level not found")
                if row[0] and req.severity is not None:
                    raise HTTPException(status_code=403, detail="Cannot modify severity of system alarm level")

                cur.execute(
                    f"UPDATE t_alarm_levels SET {', '.join(updates)}, updated_at = %s WHERE id = %s RETURNING id",
                    params + [datetime.now(timezone.utc), level_id],
                )
                conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/alarm-levels] update failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

    try:
        from app.main import get_pipeline
        pipeline = get_pipeline()
        if pipeline is not None:
            await pipeline.reload_rules_now()
    except Exception as e:
        logger.warning("[API/alarm-levels] pipeline reload failed: {}", e)

    return {"status": "updated"}


@router.delete("/alarm-levels/{level_id}")
async def delete_alarm_level(level_id: UUID) -> dict:
    from app.services.telemetry_store import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT is_system FROM t_alarm_levels WHERE id = %s", (level_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Alarm level not found")
                if row[0]:
                    raise HTTPException(status_code=403, detail="Cannot delete system alarm level")

                cur.execute("DELETE FROM t_alarm_levels WHERE id = %s RETURNING id", (level_id,))
                conn.commit()
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/alarm-levels] delete failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/alarm-levels/{level_id}/entities")
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
    return {"items": [_serialize_binding(r) for r in rows]}


@router.post("/alarm-levels/{level_id}/entities")
async def batch_bind_entities(level_id: UUID, req: BatchEntityBindRequest) -> dict:
    from app.services.telemetry_store import get_connection

    try:
        level_uuid = UUID(str(level_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid level_id")

    entity_uuids = []
    for eid in req.entity_ids:
        try:
            entity_uuids.append(UUID(eid))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid entity_id: {eid}")

    rules_json = _rules_to_json(req.trigger_rules)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM t_alarm_levels WHERE id = %s", (level_uuid,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Alarm level not found")

                inserted = 0
                for eid in entity_uuids:
                    cur.execute(
                        """
                        INSERT INTO t_entity_alarm_bindings
                        (entity_id, alarm_level_id, trigger_rules, fault_map_id, enabled)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (entity_id, alarm_level_id) DO UPDATE SET
                            trigger_rules = EXCLUDED.trigger_rules,
                            fault_map_id = EXCLUDED.fault_map_id,
                            enabled = EXCLUDED.enabled,
                            updated_at = now()
                        RETURNING id
                        """,
                        (eid, level_uuid, rules_json, UUID(req.fault_map_id) if req.fault_map_id else None, req.enabled),
                    )
                    inserted += 1
                conn.commit()

        try:
            from app.main import get_pipeline
            pipeline = get_pipeline()
            if pipeline is not None:
                await pipeline.reload_rules_now()
        except Exception as e:
            logger.warning("[API/alarm-levels] pipeline reload failed: {}", e)

        return {"bound": inserted}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/alarm-levels] batch bind failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/alarm-levels/{level_id}/entities/{binding_id}")
async def unbind_entity(level_id: UUID, binding_id: UUID) -> dict:
    from app.services.telemetry_store import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM t_entity_alarm_bindings WHERE id = %s AND alarm_level_id = %s RETURNING id",
                    (binding_id, level_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Binding not found")
                conn.commit()

        try:
            from app.main import get_pipeline
            pipeline = get_pipeline()
            if pipeline is not None:
                await pipeline.reload_rules_now()
        except Exception as e:
            logger.warning("[API/alarm-levels] pipeline reload failed: {}", e)

        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/alarm-levels] unbind failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entities/{entity_id}/alarm-levels")
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
    return {"items": [_serialize_binding(r) for r in rows]}
