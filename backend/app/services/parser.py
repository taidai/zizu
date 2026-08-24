"""
F0 Hook 1 — Neuron JSON 解析器

职责：从 MQTT payload 中提取结构化数据。
输入: RawMessage (topic + bytes payload)
输出: ParsedMessage (node_name + timestamp + tags dict)
异常: 返回 None 并记录日志, 不抛出管道外
"""
from __future__ import annotations

import json

from loguru import logger

from app.models.schemas import ParsedMessage, RawMessage

# 允许的 Neuron timestamp 字段名候选
_TS_KEYS = ("timestamp", "ts", "time", "t")
_NODE_KEYS = ("node_name", "nodeName", "node", "device", "name")
_GROUP_KEYS = ("group", "groupName", "grp")
_TAG_KEYS = ("tags", "values", "metrics", "data", "payload")


def parse_neuron_json(raw: RawMessage) -> ParsedMessage | None:
    """
    解析 Neuron JSON payload → ParsedMessage。

    支持两种格式:
      1. 标准格式: {"node_name": "...", "timestamp": ..., "tags": {...}}
      2. 简化格式: topic 最后一段作为 node_name, body 就是 tags dict

    Returns:
        ParsedMessage | None (解析失败返回 None, 不中断管道)
    """
    try:
        body = json.loads(raw.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(
            "[Parser] JSON decode failed on topic={} : {}",
            raw.topic,
            e,
        )
        return None

    if not isinstance(body, dict):
        logger.warning("[Parser] Body is not a dict: type={}", type(body).__name__)
        return None

    # ---- 提取 node_name ----
    node_name = _extract_field(body, _NODE_KEYS)
    if not node_name:
        # fallback: 从 topic 最后一段提取
        # e.g. "telemetry/HuaweiInverter_01" → "HuaweiInverter_01"
        node_name = raw.topic.split("/")[-1] if "/" in raw.topic else raw.topic

    # ---- 提取 group ----
    group = _extract_field(body, _GROUP_KEYS)

    # ---- 提取 timestamp ----
    ts_ms, event_time_basis = _extract_timestamp(body, raw.timestamp_recv)

    # ---- 提取 tags ----
    tags = _extract_tags(body)
    if not tags:
        logger.warning("[Parser] No tags found in message from node={}", node_name)
        return None

    return ParsedMessage(
        node_name=node_name,
        group=group,
        timestamp_ms=ts_ms,
        event_time_basis=event_time_basis,
        tags=tags,
    )


def _extract_field(data: dict, candidates: list[str]) -> str | None:
    """按优先级列表尝试取字段。"""
    for key in candidates:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_timestamp(data: dict, received_at) -> tuple[int, str]:
    """
    提取时间戳，统一为毫秒 epoch。

    支持:
      - 毫秒整数 (Neuron 默认): 1721223400000
      - 秒浮点数: 1721223400.123
      - ISO 字符串: "2024-07-17T10:30:00Z"
    """
    for key in _TS_KEYS:
        val = data.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            # 判断是秒还是毫秒: > 10位数字 → 毫秒
            if val > 1e12:
                return int(val), "observed_at"
            return int(val * 1000), "observed_at"
        if isinstance(val, str):
            from datetime import datetime as dt

            try:
                parsed = dt.fromisoformat(val.replace("Z", "+00:00"))
                return int(parsed.timestamp() * 1000), "observed_at"
            except (ValueError, OSError):
                continue

    # 无 timestamp 字段 → 使用接收时间
    return int(received_at.timestamp() * 1000), "received_at"


def _extract_tags(data: dict) -> dict:
    """提取 tags 值字典。"""
    for key in _TAG_KEYS:
        val = data.get(key)
        if isinstance(val, dict):
            return val
    # fallback: 整个 body 减去元数据字段就是 tags
    meta_keys = set(_TS_KEYS + _NODE_KEYS + _TAG_KEYS)
    tags = {k: v for k, v in data.items() if k not in meta_keys}
    return tags if tags else {}
