"""
Entity Resolver — 全局实体解析服务

把全局实体名（如 pcs.activePower）解析为具体 tag，支撑：
  - 实时数据查询
  - 历史数据查询
  - 规则引擎输入/输出
  - RW/W 实体控制下发
"""
from __future__ import annotations

from uuid import UUID

from loguru import logger

from app.services.telemetry_store import get_connection


def _tag_value_to_entity_value(tag: dict, record: dict) -> float | int | bool | str | None:
    """把 tag 的 telemetry 记录值按 entity 语义转换为统一值。"""
    data_type = tag.get("data_type", "FLOAT")
    if data_type == "BOOL":
        return record.get("value_bool")
    if data_type == "STRING":
        return record.get("value_str")
    if data_type == "INT":
        return record.get("value_int") if record.get("value_int") is not None else record.get("value_float")
    # FLOAT / ENUM 默认
    return record.get("value_float") if record.get("value_float") is not None else record.get("value_int")


def resolve_entity_binding(entity_id: str | UUID) -> dict | None:
    """
    解析实体当前应使用的绑定。
    规则：取 enabled 绑定中 priority 最小（最优先）且 tag/node 均 enabled 的第一条。
    """
    try:
        eid = UUID(str(entity_id))
    except ValueError:
        logger.warning("[EntityResolver] invalid entity_id: {}", entity_id)
        return None

    query = """
    SELECT
        b.id AS binding_id,
        b.entity_id,
        b.tag_id,
        b.node_id,
        b.binding_type,
        b.brand,
        b.priority,
        t.name AS tag_name,
        t.display_name AS tag_display_name,
        t.data_type,
        t.read_write,
        t.source_type,
        t.source_path,
        n.name AS node_name,
        e.name AS entity_name,
        e.display_name AS entity_display_name,
        e.entity_type AS entity_type,
        e.unit AS entity_unit
    FROM t_entity_bindings b
    JOIN t_entities e ON e.id = b.entity_id
    JOIN t_tags t ON t.id = b.tag_id
    JOIN t_nodes n ON n.id = b.node_id
    WHERE b.entity_id = %s
      AND b.enabled = TRUE
      AND t.enabled = TRUE
      AND n.enabled = TRUE
      AND e.enabled = TRUE
    ORDER BY b.priority ASC, b.created_at ASC
    LIMIT 1
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (eid,))
                row = cur.fetchone()
                if not row:
                    return None
                columns = [desc[0] for desc in cur.description]
                return dict(zip(columns, row))
    except Exception as e:
        logger.error("[EntityResolver] resolve failed: {}", e)
        return None


def get_entity_realtime(entity_id_or_name: str | UUID) -> dict | None:
    """获取实体最新实时值。"""
    binding = resolve_entity_binding(entity_id_or_name)
    if not binding:
        binding = resolve_entity_binding_by_name(str(entity_id_or_name))
    if not binding:
        raise ValueError(f'Entity not found: {entity_id_or_name}')
    if not binding:
        return None

    # 优先从 t_entity_telemetry_latest 查，未命中则回退到 t_telemetry_latest
    query_cache = """
    SELECT entity_id, binding_id, tag_id, node_id, ts,
           value_float, value_int, value_bool, value_str, quality
    FROM t_entity_telemetry_latest
    WHERE entity_id = %s
    """
    query_tag = """
    SELECT tl.ts, tl.value_float, tl.value_int, tl.value_bool, tl.value_str, tl.quality
    FROM t_telemetry_latest tl
    WHERE tl.tag_id = %s AND tl.node_id = %s
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query_cache, (binding["entity_id"],))
                row = cur.fetchone()
                if row:
                    columns = [desc[0] for desc in cur.description]
                    record = dict(zip(columns, row))
                else:
                    cur.execute(query_tag, (binding["tag_id"], binding["node_id"]))
                    row = cur.fetchone()
                    if not row:
                        return None
                    record = {
                        "ts": row[0],
                        "value_float": row[1],
                        "value_int": row[2],
                        "value_bool": row[3],
                        "value_str": row[4],
                        "quality": row[5],
                    }

        value = _tag_value_to_entity_value(binding, record)
        return {
            "entity_id": str(binding["entity_id"]),
            "entity_name": binding["entity_name"],
            "entity_display_name": binding["entity_display_name"],
            "entity_type": binding["entity_type"],
            "binding_id": str(binding["binding_id"]),
            "tag_id": str(binding["tag_id"]),
            "tag_name": binding["tag_name"],
            "node_id": str(binding["node_id"]),
            "node_name": binding["node_name"],
            "data_type": binding["data_type"],
            "unit": binding["entity_unit"],
            "ts": record["ts"].isoformat() if record.get("ts") else None,
            "value": value,
            "quality": record.get("quality", 192),
        }
    except Exception as e:
        logger.error("[EntityResolver] realtime failed: {}", e)
        return None


def get_entity_history(
    entity_id_or_name: str | UUID,
    range_key: str = "1h",
    page: int = 1,
    page_size: int = 500,
) -> dict | None:
    """获取实体历史数据（按绑定 tag 查询）。"""
    binding = resolve_entity_binding(entity_id_or_name)
    if not binding:
        binding = resolve_entity_binding_by_name(str(entity_id_or_name))
    if not binding:
        raise ValueError(f'Entity not found: {entity_id_or_name}')
    if not binding:
        return None

    interval_map = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days"}
    interval = interval_map.get(range_key, "1 hour")
    offset = (page - 1) * page_size

    query = """
    SELECT
        t.ts,
        t.value_float,
        t.value_int,
        t.value_bool,
        t.value_str,
        t.quality
    FROM t_telemetry t
    WHERE t.tag_id = %s
      AND t.ts > NOW() - %s::interval
    ORDER BY t.ts DESC
    LIMIT %s OFFSET %s
    """
    count_query = """
    SELECT COUNT(*)
    FROM t_telemetry t
    WHERE t.tag_id = %s
      AND t.ts > NOW() - %s::interval
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (binding["tag_id"], interval, page_size, offset))
                rows = cur.fetchall()
                points = [
                    {
                        "ts": r[0].isoformat(),
                        "value": _tag_value_to_entity_value(binding, {
                            "value_float": r[1], "value_int": r[2],
                            "value_bool": r[3], "value_str": r[4],
                        }),
                        "quality": r[5],
                    }
                    for r in rows
                ]

                cur.execute(count_query, (binding["tag_id"], interval))
                total = cur.fetchone()[0]

        return {
            "entity_id": str(binding["entity_id"]),
            "entity_name": binding["entity_name"],
            "tag_id": str(binding["tag_id"]),
            "range": range_key,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
            "points": points,
        }
    except Exception as e:
        logger.error("[EntityResolver] history failed: {}", e)
        return None


def resolve_entity_binding_by_name(entity_name: str) -> dict | None:
    """通过实体名解析绑定（用于规则引擎等使用 entity_name 的场景）。"""
    from app.services.telemetry_store import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM t_entities WHERE name = %s AND enabled = TRUE', (entity_name,))
                row = cur.fetchone()
                if not row:
                    return None
                entity_id = row[0]
        return resolve_entity_binding(entity_id)
    except Exception as e:
        logger.error('[EntityResolver] resolve by name failed: {}', e)
        return None


def write_entity_value(entity_id_or_name: str | UUID, value: float | int | bool | str) -> dict:
    """
    向实体写入控制值。
    规则：
      1. 仅 entity_type 为 W 或 RW 的实体可写。
      2. 优先绑定 binding_type='PHYSICAL' 的 tag（可直接下发）。
      3. 通过 Neuron REST API 下发到 source_path 或 tag 对应 node。
    """
    from app.services.neuron_client import NeuronClient, NeuronConfig
    from app.core.config import settings

    binding = resolve_entity_binding(entity_id_or_name)
    if not binding:
        binding = resolve_entity_binding_by_name(str(entity_id_or_name))
    if not binding:
        raise ValueError(f'Entity not found: {entity_id_or_name}')
    if not binding:
        raise ValueError("Entity has no active binding")

    if binding["entity_type"] not in ("W", "RW"):
        raise ValueError(f"Entity {binding['entity_name']} is not writable")

    if binding["binding_type"] != "PHYSICAL":
        raise ValueError("Only PHYSICAL binding supports write-back in MVP")

    tag_id = binding["tag_id"]
    node_name = binding["node_name"]
    tag_name = binding["tag_name"]
    source_path = binding.get("source_path") or tag_name
    source_type = binding.get("source_type") or "neuron"

    # MVP 通过 Neuron REST API 写 tag。
    # Neuron source_path 标准格式: neuron_node/group/tag (如 en9_pcs/cmd/心跳信号)
    group_name = "group0"
    neuron_tag_name = tag_name
    if source_type.lower() == "neuron" and "/" in source_path:
        parts = source_path.split("/")
        if len(parts) >= 3:
            node_name = parts[0]          # Neuron 节点名
            group_name = parts[1]
            neuron_tag_name = "/".join(parts[2:])
        elif len(parts) == 2:
            group_name, neuron_tag_name = parts
    elif "/" in source_path:
        group_name, neuron_tag_name = source_path.split("/", 1)

    config = NeuronConfig(
        url=settings.neuron_api_url,
        username=settings.neuron_username,
        password=settings.neuron_password,
        deployment_mode=settings.deployment_mode,
        allow_insecure_dev_secrets=settings.allow_insecure_dev_secrets,
    )
    client = NeuronClient(config)
    try:
        result = client.write_tag(node_name, group_name, neuron_tag_name, value)
        logger.info("[EntityResolver] write entity={} tag={}/{} value={} result={}",
                    binding["entity_name"], node_name, tag_name, value, result)
        return {
            "status": "ok",
            "entity_id": str(binding["entity_id"]),
            "entity_name": binding["entity_name"],
            "tag_id": str(tag_id),
            "tag_name": tag_name,
            "node_name": node_name,
            "value": value,
            "neuron_result": result,
        }
    except Exception as e:
        logger.error("[EntityResolver] write failed: {}", e)
        raise ValueError(f"Write failed: {e}") from e


def refresh_entity_latest(tag_id: str | UUID, node_id: str | UUID, ts, **values) -> None:
    """
    由 pipeline 在更新 t_telemetry_latest 后调用，同步刷新实体最新值缓存。
    找到所有绑定该 tag 的 enabled 实体，按优先级更新 t_entity_telemetry_latest。
    """
    query = """
    SELECT b.id, b.entity_id, b.priority
    FROM t_entity_bindings b
    JOIN t_entities e ON e.id = b.entity_id
    WHERE b.tag_id = %s
      AND b.node_id = %s
      AND b.enabled = TRUE
      AND e.enabled = TRUE
    ORDER BY b.priority ASC
    """
    upsert = """
    INSERT INTO t_entity_telemetry_latest
      (entity_id, binding_id, tag_id, node_id, ts, value_float, value_int, value_bool, value_str, quality, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
    ON CONFLICT (entity_id)
    DO UPDATE SET
      binding_id = EXCLUDED.binding_id,
      tag_id = EXCLUDED.tag_id,
      node_id = EXCLUDED.node_id,
      ts = EXCLUDED.ts,
      value_float = EXCLUDED.value_float,
      value_int = EXCLUDED.value_int,
      value_bool = EXCLUDED.value_bool,
      value_str = EXCLUDED.value_str,
      quality = EXCLUDED.quality,
      updated_at = now()
    WHERE EXCLUDED.ts > t_entity_telemetry_latest.ts
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (UUID(str(tag_id)), UUID(str(node_id))))
                bindings = cur.fetchall()
                if not bindings:
                    return
                # MVP：只更新优先级最高（priority 最小）的绑定对应的实体值
                top_priority = min(b[2] for b in bindings)
                for bid, eid, priority in bindings:
                    if priority != top_priority:
                        continue
                    cur.execute(upsert, (
                        eid, bid, UUID(str(tag_id)), UUID(str(node_id)), ts,
                        values.get("value_float"), values.get("value_int"),
                        values.get("value_bool"), values.get("value_str"),
                        values.get("quality", 192),
                    ))
                conn.commit()
    except Exception as e:
        logger.error("[EntityResolver] refresh entity latest failed: {}", e)
