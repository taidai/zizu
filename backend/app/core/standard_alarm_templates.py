"""
ZiZu Standard Alarm Templates — 光储充国标三级告警模板

预置 error1/error2/error3 三级告警等级，并自动绑定到对应标准全局实体，
实现开箱即用的设备分级告警。
"""
from __future__ import annotations

from loguru import logger
from psycopg2.extras import Json

# 告警等级定义
LEVELS = [
    {
        "code": "error1",
        "name": "一级告警（严重）",
        "severity": "CRITICAL",
        "color": "#dc2626",
        "sort_order": 1,
    },
    {
        "code": "error2",
        "name": "二级告警（重要）",
        "severity": "MAJOR",
        "color": "#f97316",
        "sort_order": 2,
    },
    {
        "code": "error3",
        "name": "三级告警（一般）",
        "severity": "WARNING",
        "color": "#f59e0b",
        "sort_order": 3,
    },
]

# 实体 -> 等级 -> 触发规则
ENTITY_ALARM_BINDINGS: dict[str, dict[str, list[dict]]] = {
    # error1: 故障码/急停/消防类，active 即告警
    "ess.faultCode": {"error1": [{"op": "active"}]},
    "pcs.faultCode": {"error1": [{"op": "active"}]},
    "pv.faultCode": {"error1": [{"op": "active"}]},
    "charger.bmsFaultCode": {"error1": [{"op": "active"}]},
    "protection.emergencyStop": {"error1": [{"op": "active"}]},
    "protection.fireAlarm": {"error1": [{"op": "active"}]},
    "protection.arcFault": {"error1": [{"op": "active"}]},
    # error2: 重要状态/温度超限
    "ess.bmsAlarm": {"error2": [{"op": "active"}]},
    "ess.maxCellTemp": {"error2": [{"op": "gte", "threshold": 55}]},
    "pcs.temp": {"error2": [{"op": "gte", "threshold": 70}]},
    "pv.inverterTemp": {"error2": [{"op": "gte", "threshold": 70}]},
    "charger.bmsBatteryTemp": {"error2": [{"op": "gte", "threshold": 60}]},
    "protection.insulationResistanceEss": {"error2": [{"op": "lt", "threshold": 0.5}]},
    # error3: 预警/偏离正常范围
    "ess.soc": {
        "error3": [
            {"op": "lt", "threshold": 10},
            {"op": "gt", "threshold": 95},
        ]
    },
    "pv.performanceRatio": {"error3": [{"op": "lt", "threshold": 50}]},
    "grid.voltageThd": {"error3": [{"op": "gt", "threshold": 5}]},
    "grid.currentThd": {"error3": [{"op": "gt", "threshold": 5}]},
    "ess.cellVoltageDiff": {"error3": [{"op": "gt", "threshold": 500}]},
}


def seed_standard_alarm_templates() -> dict:
    """幂等播种系统告警等级及实体绑定。"""
    from app.services.telemetry_store import get_connection

    level_ids: dict[str, str] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1) 确保等级存在
            for lvl in LEVELS:
                cur.execute("SELECT id FROM t_alarm_levels WHERE code = %s", (lvl["code"],))
                row = cur.fetchone()
                if row:
                    level_ids[lvl["code"]] = str(row[0])
                    cur.execute("""
                        UPDATE t_alarm_levels
                        SET name = %s, severity = %s, color = %s, sort_order = %s, is_system = TRUE, enabled = TRUE, updated_at = now()
                        WHERE code = %s
                    """, (lvl["name"], lvl["severity"], lvl["color"], lvl["sort_order"], lvl["code"]))
                else:
                    cur.execute("""
                        INSERT INTO t_alarm_levels (code, name, severity, color, trigger_rules, enabled, sort_order, is_system)
                        VALUES (%s, %s, %s, %s, %s::jsonb, TRUE, %s, TRUE)
                        RETURNING id
                    """, (lvl["code"], lvl["name"], lvl["severity"], lvl["color"], Json([]), lvl["sort_order"]))
                    level_ids[lvl["code"]] = str(cur.fetchone()[0])

            # 2) 查询实体 ID
            cur.execute("SELECT id, name FROM t_entities WHERE name = ANY(%s)", (list(ENTITY_ALARM_BINDINGS.keys()),))
            entity_map = {name: str(eid) for eid, name in cur.fetchall()}

            bound = 0
            skipped = 0
            for entity_name, level_rules in ENTITY_ALARM_BINDINGS.items():
                entity_id = entity_map.get(entity_name)
                if not entity_id:
                    skipped += 1
                    continue
                for code, rules in level_rules.items():
                    level_id = level_ids[code]
                    cur.execute("""
                        INSERT INTO t_entity_alarm_bindings (entity_id, alarm_level_id, trigger_rules, enabled)
                        VALUES (%s, %s, %s::jsonb, TRUE)
                        ON CONFLICT (entity_id, alarm_level_id) DO UPDATE SET
                            trigger_rules = EXCLUDED.trigger_rules,
                            enabled = TRUE,
                            updated_at = now()
                        RETURNING id
                    """, (entity_id, level_id, Json(rules)))
                    if cur.fetchone():
                        bound += 1

            conn.commit()

    logger.info("[StandardAlarmTemplates] levels={}, bound={}, skipped_entities={}", len(level_ids), bound, skipped)
    return {"levels": len(level_ids), "bound": bound, "skipped_entities": skipped}
