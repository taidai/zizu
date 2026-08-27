"""
F0 Hook 3 — 时序存储引擎 (M4)

职责：
  - psycopg2 连接池管理 (同步驱动，批量写入性能最优)
  - execute_values 批量写入 t_telemetry Hypertable
  - 多粒度查询 API:
      raw   → 直接查 t_telemetry
      1m    → tel_agg_5min CAGG view
      1h    → tel_agg_1h CAGG view
      1d    → tel_agg_1d CAGG view
  - 最新值查询 (每个 tag 的最新一行)
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from uuid import UUID

from loguru import logger
import psycopg2
import psycopg2.pool
from psycopg2 import sql
from psycopg2.extras import execute_values

from app.core.config import settings
from app.models.schemas import NormalizedMessage, TelemetryRecord, Quality

# ══════════════════════════════════════
# 连接池
# ══════════════════════════════════════

_pool: psycopg2.pool.AbstractConnectionPool | None = None


def init_db_pool(min_conn: int = 2, max_conn: int = 10) -> None:
    """初始化连接池。应在应用启动时调用一次（幂等）。"""
    global _pool
    if _pool is not None:
        return
    # psycopg2 默认不能适配 Python UUID → 必须注册 adapter
    from psycopg2.extras import register_uuid

    register_uuid()

    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=min_conn,
        maxconn=max_conn,
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )
    # 验证连接
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            ver = cur.fetchone()
            logger.info("[TSDB] Connected: {}", ver[0])


def close_db_pool() -> None:
    """关闭连接池。应在应用停更时调用。"""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("[DB] Connection pool closed")


def verify_legacy_alarm_history_gate() -> None:
    """Fail closed unless the web process has read-only, non-owner legacy access."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user),
                    has_table_privilege(current_user, 'public.t_alarms', 'SELECT'),
                    has_table_privilege(current_user, 'public.t_alarms', 'INSERT')
                        OR has_table_privilege(current_user, 'public.t_alarms', 'UPDATE')
                        OR has_table_privilege(current_user, 'public.t_alarms', 'DELETE')
                        OR has_table_privilege(current_user, 'public.t_alarms', 'TRUNCATE'),
                    has_schema_privilege(current_user, 'public', 'CREATE')
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = 't_alarms'
                """
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("Legacy alarm history table is unavailable")
    is_owner, can_read, can_write, can_create_schema = row
    if is_owner or not can_read or can_write or can_create_schema:
        raise RuntimeError(
            "Application DB role must be a non-owner without public schema CREATE and with SELECT-only access to t_alarms; "
            "run scripts/provision_database_roles.py before production startup"
        )


@contextmanager
def get_connection():
    """获取数据库连接 (上下文管理器)。"""
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_db_pool() first.")
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


# ══════════════════════════════════════
# 批量写入
# ══════════════════════════════════════

_INSERT_SQL = """
INSERT INTO t_telemetry (ts, node_id, tag_id, value_float, value_int,
                         value_bool, value_str, is_virtual, quality)
VALUES %s
ON CONFLICT DO NOTHING;  -- 允许重复时间戳(不同tag), 不做 upsert
"""


async def batch_insert_telemetry(
    records: list[TelemetryRecord],
) -> int:
    """
    Legacy test/helper write; production pipeline must use the data-frame runtime.

    批量写入遥测记录到 Hypertable。

    Args:
        records: TelemetryRecord 列表 (通常来自 NormalizedMessage.points)

    Returns:
        成功写入的行数

    Raises:
        psycopg2.Error: 数据库错误
    """
    if not records:
        return 0

    rows = []
    for r in records:
        rows.append((
            r.ts,
            r.node_id,
            r.tag_id,
            r.value_float,
            r.value_int,
            r.value_bool,
            r.value_str,
            r.is_virtual,
            r.quality or Quality.GOOD.value,
        ))

    def _insert():
        with get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, _INSERT_SQL, rows)
                conn.commit()
                return cur.rowcount

    inserted = await asyncio.to_thread(_insert)
    logger.debug("[TSDB] Batch insert {} records", inserted)
    return inserted


def insert_normalized_message(
    msg: NormalizedMessage,
    node_id_map: dict[str, UUID],     # {node_name → node_id}
    tag_id_map: dict[str, UUID],      # {tag_name → tag_id}
) -> int:
    """
    便捷方法：直接将 NormalizedMessage 写入 DB。
    需要 node_id / tag_id 映射表（由 Pipeline 从 t_tags 加载）。
    """
    records = []
    for point in msg.points:
        nid = node_id_map.get(point.node_name or msg.source_node)
        tid = tag_id_map.get(point.tag_name)
        if nid and tid:
            records.append(TelemetryRecord.from_point(point, nid, tid))
        else:
            logger.debug(
                "[TSDB] Skip unresolved point: node={} tag={}",
                point.node_name,
                point.tag_name,
            )
    return batch_insert_telemetry(records)

# ══════════════════════════════════════
# 查询 API
# ══════════════════════════════════════

class AggregationGranularity(str, Enum):
    RAW = "raw"       # 原始数据
    M5 = "5m"         # 5 分钟 (CAGG tel_agg_5min)
    H1 = "1h"         # 1 小时 (CAGG tel_agg_1h)
    D1 = "1d"         # 1 天 (CAGG tel_agg_1d)

from enum import Enum


def query_telemetry(
    node_id: UUID | None = None,
    tag_ids: list[UUID] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    agg: AggregationGranularity = AggregationGranularity.RAW,
    limit: int = 10000,
) -> list[dict]:
    """
    查询历史遥测数据。

    根据 agg 粒度自动选择表/Hypertable/CAGG View:
      - RAW  → t_telemetry
      - 5m   → tel_agg_5min
      - 1h   → tel_agg_1h
      - 1d   → tel_agg_1d
    """
    if agg == AggregationGranularity.RAW:
        table = sql.Identifier("t_telemetry")
        ts_col = sql.Identifier("ts")
        val_col = sql.Identifier("value_float")
    elif agg == AggregationGranularity.M5:
        table = sql.Identifier("tel_agg_5min")
        ts_col = sql.Identifier("bucket")
        val_col = sql.Identifier("avg_val")
    elif agg == AggregationGranularity.H1:
        table = sql.Identifier("tel_agg_1h")
        ts_col = sql.Identifier("bucket")
        val_col = sql.Identifier("avg_val")
    elif agg == AggregationGranularity.D1:
        table = sql.Identifier("tel_agg_1d")
        ts_col = sql.Identifier("bucket")
        val_col = sql.Identifier("avg_val")
    else:
        raise ValueError(f"Unknown aggregation: {agg}")

    conditions = []
    params: list = []

    if node_id:
        conditions.append("node_id = %s")
        params.append(node_id)
    if tag_ids:
        conditions.append("tag_id = ANY(%s)")
        params.append(tag_ids)
    if start:
        conditions.append(f"{ts_col.string} >= %s")
        params.append(start)
    if end:
        conditions.append(f"{ts_col.string} <= %s")
        params.append(end)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    order = f" ORDER BY {ts_col.string} DESC LIMIT %s"
    params.append(limit)

    query_sql = (
        f"SELECT {ts_col.string} AS ts, node_id, tag_id, "
        f"{val_col.string} AS value "
        f"FROM {table.string}{where}{order}"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query_sql, params)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    return rows


def query_latest_values(
    tag_ids: list[UUID] | None = None,
) -> list[dict]:
    """
    每个指定 tag 的最新值。

    直接从 t_telemetry_latest 缓存表读取，避免历史 hypertable 上做 DISTINCT ON。
    用于 Dashboard 实时数字展示。
    """
    base_query = """
    SELECT ts, node_id, tag_id,
           COALESCE(value_float, value_int::float) as value,
           is_virtual, quality
    FROM t_telemetry_latest
    """
    conditions = []
    params: list = []

    if tag_ids:
        conditions.append("tag_id = ANY(%s)")
        params.append(tag_ids)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(base_query + where, params)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    return rows


_UPSERT_LATEST_SQL = """
INSERT INTO t_telemetry_latest (
    node_id, tag_id, ts, value_float, value_int,
    value_bool, value_str, is_virtual, quality
) VALUES %s
ON CONFLICT (node_id, tag_id) DO UPDATE SET
    ts = EXCLUDED.ts,
    value_float = EXCLUDED.value_float,
    value_int = EXCLUDED.value_int,
    value_bool = EXCLUDED.value_bool,
    value_str = EXCLUDED.value_str,
    is_virtual = EXCLUDED.is_virtual,
    quality = EXCLUDED.quality,
    updated_at = now()
"""


async def upsert_telemetry_latest(records: list[TelemetryRecord]) -> int:
    """
    Legacy test/helper write; production pipeline must use the data-frame runtime.

    将遥测记录 upsert 到 t_telemetry_latest 缓存表。
    每个 (node_id, tag_id) 只保留最新一行。
    """
    if not records:
        return 0

    # 同一批次中可能对同一 (node_id, tag_id) 有多行；按时间戳保留最新一行
    latest: dict[tuple[UUID, UUID], TelemetryRecord] = {}
    for r in records:
        key = (r.node_id, r.tag_id)
        existing = latest.get(key)
        if existing is None or r.ts > existing.ts:
            latest[key] = r

    rows = [
        (
            r.node_id,
            r.tag_id,
            r.ts,
            r.value_float,
            r.value_int,
            r.value_bool,
            r.value_str,
            r.is_virtual,
            r.quality,
        )
        for r in latest.values()
    ]

    def _upsert():
        with get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, _UPSERT_LATEST_SQL, rows)
                conn.commit()
                return cur.rowcount

    updated = await asyncio.to_thread(_upsert)
    logger.debug("[TSDB] Upsert latest {} rows", updated)
    return updated
