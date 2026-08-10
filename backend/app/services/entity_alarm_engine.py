"""
Entity Alarm Engine — 全局实体告警评估器

把全局实体绑定到告警等级，当实体解析到的点位有新值时：
  1. 取该实体在各告警等级上的触发规则（binding 覆盖 > level 默认）
  2. 用点位当前工程值评估规则
  3. 命中则生成/累计告警；未命中则恢复告警

设计原则：
  - DB-free 纯函数优先，便于 TDD
  - pipeline 只负责提供 (tag_id -> [binding...]) 索引和原始值
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.services.alarm_logic import is_alarm_active, build_alarm_message, match_fault_entry


def _extract_value(record: dict) -> Any:
    """从 telemetry record 字典还原工程值。"""
    if record.get("value_str") is not None:
        return record["value_str"]
    if record.get("value_bool") is not None:
        return record["value_bool"]
    if record.get("value_int") is not None:
        return record["value_int"]
    if record.get("value_float") is not None:
        return record["value_float"]
    return None


def evaluate_trigger_rule(value: Any, rule: dict, entries: list[dict] | None) -> bool:
    """
    评估单条触发规则。

    支持 op:
      - active:   is_alarm_active(value) 为真
      - eq:       str(value) == str(rule['value'])
      - ne:       str(value) != str(rule['value'])
      - gte:      numeric value >= rule['threshold']
      - gt:       numeric value >  rule['threshold']
      - lte:      numeric value <= rule['threshold']
      - lt:       numeric value <  rule['threshold']
      - fault:    value 命中 fault_map entries（entries 由调用方提供）
    """
    op = str(rule.get("op", "active")).lower()

    if op == "active":
        return is_alarm_active(value)

    if op == "eq":
        return str(value).strip() == str(rule.get("value")).strip()

    if op == "ne":
        return str(value).strip() != str(rule.get("value")).strip()

    if op in ("gte", "gt", "lte", "lt"):
        if not isinstance(value, (int, float)):
            return False
        threshold = rule.get("threshold")
        if not isinstance(threshold, (int, float)):
            return False
        if op == "gte":
            return value >= threshold
        if op == "gt":
            return value > threshold
        if op == "lte":
            return value <= threshold
        if op == "lt":
            return value < threshold

    if op == "fault":
        return match_fault_entry(value, entries) is not None

    logger.warning("[EntityAlarmEngine] unknown trigger rule op '{}', fallback to active", op)
    return is_alarm_active(value)


def evaluate_trigger_rules(
    value: Any,
    rules: list[dict],
    entries: list[dict] | None = None,
    match_mode: str = "any",
) -> bool:
    """
    评估规则数组。

    match_mode:
      - any: 任意一条命中即触发（默认，适合告警）
      - all: 全部命中才触发
    """
    if not rules:
        return is_alarm_active(value)

    results = [evaluate_trigger_rule(value, r, entries) for r in rules]

    if match_mode == "all":
        return all(results)
    return any(results)


def process_entity_alarms(
    records: list[dict],
    tag_entity_alarm_index: dict[str, list[dict]],
) -> dict:
    """
    批量处理实体告警。

    Args:
        records: TelemetryRecord 列表（或兼容 dict），含 tag_id / node_id / value_* / ts
        tag_entity_alarm_index: {
            tag_id(str): [binding_meta, ...]
        }

    Returns:
        {"created": int, "resolved": int, "incremented": int}
    """
    from app.services.telemetry_store import get_connection

    created = 0
    resolved = 0
    incremented = 0
    now = datetime.now(timezone.utc)

    if not records or not tag_entity_alarm_index:
        return {"created": 0, "resolved": 0, "incremented": 0}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for record in records:
                    tag_id = str(record.get("tag_id") or "")
                    bindings = tag_entity_alarm_index.get(tag_id)
                    if not bindings:
                        continue

                    value = _extract_value(record)
                    node_id = record.get("node_id")

                    for binding in bindings:
                        source_key = binding["alarm_level_code"]
                        level = binding["alarm_level_severity"]
                        entity_id = binding["entity_id"]
                        entity_name = binding["entity_name"]
                        entity_display = binding.get("entity_display_name") or entity_name
                        rules = binding.get("trigger_rules") or []
                        entries = binding.get("fault_map_entries") or []

                        active = evaluate_trigger_rules(value, rules, entries)

                        trigger_value = None
                        if isinstance(value, (int, float)):
                            trigger_value = float(value)

                        cur.execute(
                            "SELECT id, resolved_at, alarm_count FROM t_alarms "
                            "WHERE entity_id = %s AND source_key = %s "
                            "ORDER BY created_at DESC LIMIT 1",
                            (entity_id, source_key),
                        )
                        row = cur.fetchone()

                        if active:
                            if row is None or row[1] is not None:
                                message = build_alarm_message(
                                    tag_name=entity_display,
                                    alarm_level=source_key,
                                    alarm_type=None,
                                    threshold=None,
                                    value=value,
                                    entries=entries,
                                )
                                cur.execute(
                                    "INSERT INTO t_alarms "
                                    "(entity_id, node_id, source_key, external_id, level, message, "
                                    "alarm_source, trigger_tag_name, trigger_value, created_at) "
                                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                    (
                                        entity_id, node_id, source_key, entity_name,
                                        level, message, "Entity", entity_name,
                                        trigger_value, now,
                                    ),
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
        logger.error("[EntityAlarmEngine] process failed: {}", e)
        return {"created": 0, "resolved": 0, "incremented": 0}

    logger.debug(
        "[EntityAlarmEngine] created={} resolved={} incremented={}",
        created, resolved, incremented,
    )
    return {"created": created, "resolved": resolved, "incremented": incremented}
