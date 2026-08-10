"""
Tag Alarm Engine — 基于点位 alarm_level / alarm_type / alarm_threshold 与 fault_map 生成告警。

逻辑：
  - 当点位的 alarm_level 为 error1/error2/error3 且当前值为"激活"状态时：
    - 若同一点位同级别已有未恢复告警 → alarm_count += 1 (不重复创建)
    - 否则 → 插入新告警，携带 alarm_type / alarm_threshold / alarm_source
  - 当值变为"非激活"时 → 恢复最新未恢复告警
  - 若点位绑定了 fault_map → 优先使用故障码映射生成 message (支持 hex/wildcard 匹配)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from loguru import logger

from app.services.alarm_logic import (
    is_alarm_active,
    build_alarm_message,
)
from app.services.telemetry_store import get_connection

ERROR_GROUP_MAP = {
    "error1": "CRITICAL",
    "error2": "MAJOR",
    "error3": "WARNING",
}

ERROR_LEVELS = {"error1", "error2", "error3"}

# node_type → alarm_source 映射 (对齐国标 9.1.4)
NODE_TYPE_TO_SOURCE = {
    "ESS": "ESS",
    "PV": "PV",
    "PCS": "PCS",
    "EVSE": "EVSE",
    "BMS": "BMS",
    "Meter": "Grid",
    "GRID": "Grid",
    "site": "System",
    "station": "System",
}


def _extract_value(record: dict) -> Any:
    """从 TelemetryRecord 字典中还原工程值。"""
    if record.get("value_str") is not None:
        return record["value_str"]
    if record.get("value_bool") is not None:
        return record["value_bool"]
    if record.get("value_int") is not None:
        return record["value_int"]
    if record.get("value_float") is not None:
        return record["value_float"]
    return None


def _check_threshold_active(value: Any, threshold: float | None) -> bool:
    """阈值模式: value >= threshold → active; 无阈值 → fallback to is_alarm_active."""
    if threshold is not None and isinstance(value, (int, float)):
        return value >= threshold
    return is_alarm_active(value)


def process_tag_alarms(records: list, tag_meta: dict[UUID, dict]) -> dict:
    """
    批量处理点位告警。

    Args:
        records: TelemetryRecord 列表（或兼容 dict）
        tag_meta: {tag_id(str): {"alarm_level":..., "tag_name":..., "alarm_type":...,
                                "alarm_threshold":..., "node_type":...,
                                "fault_map_entries":[...]}}

    Returns:
        {"created": int, "resolved": int, "incremented": int}
    """
    created = 0
    resolved = 0
    incremented = 0
    now = datetime.now(timezone.utc)

    if not records or not tag_meta:
        return {"created": 0, "resolved": 0, "incremented": 0}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for record in records:
                    tag_id = str(record.get("tag_id"))
                    meta = tag_meta.get(tag_id)
                    if not meta:
                        continue

                    alarm_level = meta.get("alarm_level")
                    if alarm_level not in ERROR_LEVELS:
                        continue

                    value = _extract_value(record)
                    threshold = meta.get("alarm_threshold")
                    active = _check_threshold_active(value, threshold)
                    level = ERROR_GROUP_MAP[alarm_level]
                    source_key = alarm_level
                    tag_name = meta.get("tag_name") or "unknown"
                    alarm_type = meta.get("alarm_type")
                    node_id = record.get("node_id")
                    node_type = meta.get("node_type")
                    alarm_source = NODE_TYPE_TO_SOURCE.get(node_type, node_type or "System")

                    trigger_value = None
                    if isinstance(value, (int, float)):
                        trigger_value = float(value)

                    cur.execute(
                        "SELECT id, resolved_at, alarm_count FROM t_alarms "
                        "WHERE tag_id = %s AND source_key = %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (tag_id, source_key),
                    )
                    row = cur.fetchone()

                    if active:
                        if row is None or row[1] is not None:
                            # 创建新告警
                            message = build_alarm_message(
                                tag_name=tag_name,
                                alarm_level=alarm_level,
                                alarm_type=alarm_type,
                                threshold=threshold,
                                value=value,
                                entries=meta.get("fault_map_entries"),
                            )
                            cur.execute(
                                "INSERT INTO t_alarms (tag_id, node_id, source_key, external_id, "
                                "level, message, alarm_type, alarm_threshold, alarm_source, "
                                "trigger_tag_name, trigger_value, created_at) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                (
                                    tag_id, node_id, source_key, tag_name,
                                    level, message, alarm_type, threshold, alarm_source,
                                    tag_name, trigger_value, now,
                                ),
                            )
                            created += 1
                        else:
                            # 已有未恢复告警 → 累计计数
                            cur.execute(
                                "UPDATE t_alarms SET alarm_count = alarm_count + 1 WHERE id = %s",
                                (row[0],),
                            )
                            incremented += 1
                    else:
                        if row is not None and row[1] is None:
                            cur.execute(
                                "UPDATE t_alarms SET resolved_at = %s WHERE id = %s",
                                (now, row[0]),
                            )
                            resolved += 1

            conn.commit()
    except Exception as e:
        logger.error("[TagAlarmEngine] process failed: {}", e)
        return {"created": 0, "resolved": 0, "incremented": 0}

    logger.debug("[TagAlarmEngine] created={} resolved={} incremented={}", created, resolved, incremented)
    return {"created": created, "resolved": resolved, "incremented": incremented}
