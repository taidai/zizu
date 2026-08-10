"""M2.5 - MQTT 分级告警处理器

从 MQTT payload 中提取 error1/error2/error3 分组，生成/恢复告警。
若提供 tag_name_meta，则通过统一 fault_map 解析故障描述 + 携带 alarm_type/threshold。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.services.alarm_logic import is_alarm_active, build_alarm_message
from app.services.telemetry_store import get_connection

ERROR_GROUP_MAP = {
    "error1": "CRITICAL",
    "error2": "MAJOR",
    "error3": "WARNING",
}

ERROR_LEVELS = {"error1", "error2", "error3"}

_NESTED_CONTAINER_KEYS = {"values", "tags", "data", "metrics", "payload"}


def _iter_error_groups(data: dict[str, Any], _depth: int = 0) -> dict[str, dict[str, Any]]:
    """
    在 payload 中递归收集 error1/error2/error3 分组。
    返回: {source_key: {external_id: value}}
    """
    groups: dict[str, dict[str, Any]] = {k: {} for k in ERROR_LEVELS}
    if not isinstance(data, dict) or _depth > 2:
        return groups

    for key, value in data.items():
        if key in ERROR_LEVELS:
            if isinstance(value, dict):
                for external_id, val in value.items():
                    groups[key][str(external_id)] = val
            elif isinstance(value, list):
                for item in value:
                    if item is None or item == "":
                        continue
                    groups[key][str(item)] = 1
            else:
                groups[key][""] = value
        elif key in _NESTED_CONTAINER_KEYS and isinstance(value, dict):
            nested = _iter_error_groups(value, _depth + 1)
            for level in ERROR_LEVELS:
                groups[level].update(nested[level])

    return groups


def _alarms_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把 error groups 展开为告警记录列表。"""
    groups = _iter_error_groups(payload)
    alarms: list[dict[str, Any]] = []
    for source_key, items in groups.items():
        level = ERROR_GROUP_MAP[source_key]
        for external_id, value in items.items():
            alarms.append({
                "source_key": source_key,
                "external_id": external_id,
                "level": level,
                "value": value,
            })
    return alarms


def process_alarm_message(
    topic: str,
    payload_bytes: bytes,
    tag_name_meta: dict[str, dict] | None = None,
) -> dict:
    """
    处理 MQTT 分级告警消息。

    Args:
        topic: MQTT 主题
        payload_bytes: 原始 payload
        tag_name_meta: 可选 {tag_name(str): {alarm_type, alarm_threshold, fault_map_entries, ...}}
                       用于故障码转义和补充告警类型/阈值
    """
    try:
        payload = json.loads(payload_bytes.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning("[Alarm] Invalid JSON on topic {}: {}", topic, e)
        return {"created": 0, "resolved": 0, "skipped": 1}

    if not isinstance(payload, dict):
        logger.debug("[Alarm] Non-dict payload on topic {}", topic)
        return {"created": 0, "resolved": 0, "skipped": 1}

    alarms = _alarms_from_payload(payload)
    if not alarms:
        return {"created": 0, "resolved": 0, "skipped": 0}

    created = 0
    resolved = 0
    incremented = 0
    now = datetime.now(timezone.utc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for alarm in alarms:
                    source_key = alarm["source_key"]
                    external_id = alarm["external_id"]
                    level = alarm["level"]
                    value = alarm["value"]
                    active = is_alarm_active(value)

                    # 查找已有的同源告警
                    cur.execute(
                        "SELECT id, resolved_at, alarm_count FROM t_alarms "
                        "WHERE source_topic = %s AND source_key = %s AND external_id = %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (topic, source_key, external_id),
                    )
                    row = cur.fetchone()

                    # 从 tag_name_meta 获取故障码映射和告警类型
                    meta = (tag_name_meta or {}).get(external_id, {})
                    alarm_type = meta.get("alarm_type")
                    threshold = meta.get("alarm_threshold")
                    entries = meta.get("fault_map_entries")

                    if active:
                        if row is None or row[1] is not None:
                            message = build_alarm_message(
                                tag_name=external_id,
                                alarm_level=source_key,
                                alarm_type=alarm_type,
                                threshold=threshold,
                                value=value,
                                entries=entries,
                            )
                            cur.execute(
                                "INSERT INTO t_alarms (source_topic, source_key, external_id, "
                                "level, message, alarm_type, alarm_threshold, alarm_source, created_at) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                (topic, source_key, external_id, level, message,
                                 alarm_type, threshold, meta.get("alarm_source"), now),
                            )
                            created += 1
                        else:
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
        logger.error("[Alarm] DB processing failed: {}", e)
        return {"created": 0, "resolved": 0, "skipped": len(alarms)}

    logger.debug("[Alarm] topic={} created={} resolved={} incr={}", topic, created, resolved, incremented)
    return {"created": created, "resolved": resolved, "skipped": 0, "incremented": incremented}
