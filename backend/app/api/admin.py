"""
ZiZu Admin / Developer API — 开发者工具

GET    /api/v1/pipeline/config      → 获取管道配置
PUT    /api/v1/pipeline/config      → 更新入库节拍 (batch_size / flush_interval)
GET    /api/v1/mqtt-config          → 获取 MQTT 北向主题配置
PUT    /api/v1/mqtt-config          → 更新 MQTT 北向主题配置（实时重订阅）
POST   /api/v1/query                → 执行 SELECT SQL 查询
POST   /api/v1/admin/truncate       → 清空指定表 (白名单 + 确认)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from app.api.business_security import SYSTEM_MANAGE, protected

router = APIRouter()


# ══════════════════════════════════════
# 1. 入库节拍配置
# ══════════════════════════════════════

class PipelineConfig(BaseModel):
    batch_size: int = Field(..., ge=1, le=1000, description="批量写入条数")
    flush_interval_sec: float = Field(..., ge=0.1, le=60.0, description="定时 flush 间隔 (秒)")


@router.get("/pipeline/config", **protected(SYSTEM_MANAGE))
async def get_pipeline_config() -> dict:
    """获取当前管道配置。"""
    from app.core.config import settings
    return {
        "batch_size": settings.pipeline_batch_size,
        "flush_interval_sec": settings.pipeline_flush_interval_sec,
    }


@router.put("/pipeline/config", **protected(SYSTEM_MANAGE))
async def update_pipeline_config(req: PipelineConfig) -> dict:
    """更新入库节拍配置 (运行时生效, 不重启服务)。"""
    from app.core.config import settings

    settings.pipeline_batch_size = req.batch_size
    settings.pipeline_flush_interval_sec = req.flush_interval_sec

    logger.info("[API/pipeline] Config updated: batch_size={}, flush_interval={}s",
                req.batch_size, req.flush_interval_sec)

    return {
        "status": "ok",
        "batch_size": settings.pipeline_batch_size,
        "flush_interval_sec": settings.pipeline_flush_interval_sec,
    }


# ══════════════════════════════════════
# 2. MQTT 北向主题配置
# ══════════════════════════════════════

class MqttConfigRequest(BaseModel):
    mqtt_telemetry_topic: str = Field(..., description="MQTT 遥测主题，支持逗号分隔与 +/# 通配符")


@router.get("/mqtt-config", **protected(SYSTEM_MANAGE))
async def get_mqtt_config() -> dict:
    """获取当前 MQTT 遥测主题配置（.env 与 DB 合并后的实际生效值）。"""
    from app.core.config import settings
    from app.services.config_store import load_mqtt_topics

    persisted = load_mqtt_topics()
    return {
        "mqtt_telemetry_topic": settings.mqtt_telemetry_topic,
        "persisted": persisted,
        "effective_topics": settings.mqtt_telemetry_topics,
    }


@router.put("/mqtt-config", **protected(SYSTEM_MANAGE))
async def update_mqtt_config(req: MqttConfigRequest) -> dict:
    """更新 MQTT 遥测主题并实时重订阅。"""
    from app.core.config import settings
    from app.services.config_store import save_mqtt_topics

    topic_string = req.mqtt_telemetry_topic.strip()
    if not topic_string:
        raise HTTPException(status_code=400, detail="MQTT topic cannot be empty")

    # Validate topic format (basic)
    for t in [x.strip() for x in topic_string.split(",") if x.strip()]:
        if " " in t:
            raise HTTPException(status_code=400, detail=f"Invalid topic: {t}")

    # Persist to DB
    save_mqtt_topics(topic_string)

    # Update runtime settings
    settings.mqtt_telemetry_topic = topic_string

    # Resubscribe via pipeline
    from app.api.health import get_pipeline
    pipeline = get_pipeline()
    if pipeline is not None:
        await pipeline.reload_mqtt_topics()
    else:
        logger.warning("[API/mqtt-config] Pipeline not available, settings saved but not resubscribed")

    logger.info("[API/mqtt-config] Updated MQTT telemetry topic to: {}", topic_string)

    return {
        "status": "ok",
        "mqtt_telemetry_topic": settings.mqtt_telemetry_topic,
        "effective_topics": settings.mqtt_telemetry_topics,
    }


# ══════════════════════════════════════
# 3. SQL 语句查表
# ══════════════════════════════════════

class SqlQueryRequest(BaseModel):
    sql: str = Field(..., description="SELECT 语句 (只允许 SELECT)")
    limit: int = Field(500, ge=1, le=5000, description="最大返回行数")


@router.post("/query", **protected(SYSTEM_MANAGE))
async def execute_sql(req: SqlQueryRequest) -> dict:
    """
    执行 SELECT SQL 查询 (只允许 SELECT, 禁止写操作)。
    """
    sql = req.sql.strip().rstrip(';')

    # 安全检查: 只允许 SELECT
    if not sql.upper().startswith('SELECT'):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed")

    # 危险关键字检查
    forbidden = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'TRUNCATE', 'ALTER', 'CREATE', 'GRANT', 'REVOKE']
    for kw in forbidden:
        if kw in sql.upper():
            raise HTTPException(status_code=400, detail=f"Forbidden keyword: {kw}")

    from app.services.telemetry_store import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchmany(req.limit)

        # 序列化: 所有值转为字符串, 特殊类型处理
        result = []
        for row in rows:
            serialized = []
            for val in row:
                if val is None:
                    serialized.append(None)
                elif isinstance(val, (int, float)):
                    serialized.append(val)
                else:
                    serialized.append(str(val))
            result.append(serialized)

        return {
            "columns": columns,
            "rows": result,
            "row_count": len(result),
            "sql": sql,
        }
    except Exception as e:
        logger.error("[API/query] SQL failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


# ══════════════════════════════════════
# 3. 清空指定表
# ══════════════════════════════════════

# 前端 DataBrowser / AdminPanel 已支持 t_node_snapshot，加入白名单
TRUNCATE_WHITELIST = {'t_telemetry', 't_audit_log'}


class TruncateRequest(BaseModel):
    table: str = Field(..., description="表名 (白名单: t_telemetry, t_audit_log)")
    confirm: str = Field(..., description="确认字符串, 必须输入 'yes' 才执行")


@router.post("/admin/truncate", **protected(SYSTEM_MANAGE))
async def truncate_table(req: TruncateRequest) -> dict:
    """
    清空指定表 (白名单 + 确认机制)。
    """
    if req.table not in TRUNCATE_WHITELIST:
        raise HTTPException(
            status_code=400,
            detail=f"Table '{req.table}' not in whitelist. Allowed: {', '.join(TRUNCATE_WHITELIST)}"
        )

    if req.confirm.lower() != 'yes':
        raise HTTPException(status_code=400, detail="Confirmation required: send 'yes' to execute")

    from app.services.telemetry_store import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {req.table}")
                before_count = cur.fetchone()[0]

                cur.execute(f"TRUNCATE TABLE {req.table}")
                conn.commit()

        logger.warning("[API/admin] Table {} truncated, {} rows deleted", req.table, before_count)

        return {
            "status": "ok",
            "table": req.table,
            "rows_deleted": before_count,
        }
    except Exception as e:
        logger.error("[API/admin] Truncate failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))
