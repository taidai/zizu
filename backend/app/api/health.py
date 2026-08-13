"""
ZiZu Health Check API

GET /api/v1/health → 全组件状态 + F0 Pipeline Metrics
GET /api/v1/health/live → 最小匿名存活探针
GET /api/v1/health/ready → K8s readiness probe
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from loguru import logger

from app.api.business_security import RUNTIME_READ, protected

router = APIRouter()

# Track startup time for uptime calculation
_start_time: float = time.monotonic()

# Global reference to the pipeline (set by main.py lifespan)
_pipeline = None

def _load_version() -> str:
    """从当前目录向上查找 VERSION 文件；失败时返回 0.0.0。"""
    here = Path(__file__).resolve().parent
    candidates = [here / "VERSION"]
    for parent in here.parents:
        candidates.append(parent / "VERSION")
    for p in candidates:
        if p.exists():
            return p.read_text().strip()
    logger.warning("VERSION file not found, fallback to 0.0.0")
    return "0.0.0"


_VERSION = _load_version()


def set_pipeline(pipeline) -> None:
    """由 main.py 在启动时注入 pipeline 实例。"""
    global _pipeline
    _pipeline = pipeline


def get_pipeline():
    """获取当前 pipeline 实例（供其他 API 模块使用）。"""
    return _pipeline


@router.get("/health/live")
async def liveness_check() -> dict:
    """最小匿名存活探针，不暴露组件状态或现场拓扑。"""
    return {"status": "alive", "version": _VERSION}

async def _check_neuron() -> str:
    try:
        from app.core.config import settings
        from app.services.neuron_client import get_neuron_client

        if not settings.neuron_api_url:
            return "not_configured"
        client = get_neuron_client()
        await asyncio.to_thread(client.get_version)
        return "connected"
    except Exception:
        return "disconnected"


@router.get("/health", **protected(RUNTIME_READ))
async def health_check() -> dict:
    """
    ZiZu 健康检查 — 包含 F0 Pipeline Metrics。

    Returns:
        {
            "status": "ok" | "degraded",
            "version": "0.4.0",
            "uptime_seconds": float,
            "components": { timescaledb, mqtt, pipeline },
            "pipeline": { messages_received, points_written_db, ... }
        }
    """
    # ---- DB 连接状态 ----
    tsdb_ok = _check_tsdb()

    # ---- MQTT 连接状态 ----
    mqtt_ok = False
    if _pipeline and _pipeline._mqtt:
        mqtt_ok = _pipeline._mqtt.is_connected

    # ---- Pipeline 运行状态 ----
    pipe_status = "not_started"
    pipe_metrics = {}
    validation_points = {}
    if _pipeline:
        m = _pipeline.metrics
        pipe_status = m.status.value
        pipe_metrics = {
            "status": m.status.value,
            "messages_received": m.messages_received,
            "messages_parsed_ok": m.messages_parsed_ok,
            "messages_parse_error": m.messages_parse_error,
            "points_normalized": m.points_normalized,
            "points_written_db": m.points_written_db,
            "db_write_errors": m.db_write_errors,
            "last_message_at": m.last_message_at.isoformat() if m.last_message_at else None,
            "uptime_seconds": round(_pipeline.uptime_seconds, 2),
        }

        # ---- 验证点状态 ----
        total = m.messages_received
        parse_ok = m.messages_parsed_ok
        parse_err = m.messages_parse_error
        db_err = m.db_write_errors
        buffered = len(_pipeline._buffer) if hasattr(_pipeline, '_buffer') else 0

        validation_points = {
            "mqtt_connection": {
                "status": "ok" if mqtt_ok else "error",
                "message": "MQTT connected" if mqtt_ok else "MQTT disconnected",
            },
            "message_parsing": {
                "status": "ok" if parse_err == 0 else ("warning" if parse_ok > parse_err * 10 else "error"),
                "success_rate": round(parse_ok / total * 100, 1) if total > 0 else 0,
                "parse_errors": parse_err,
            },
            "normalization": {
                "status": "ok" if m.points_normalized > 0 else "warning",
                "points_normalized": m.points_normalized,
                "unmatched_rules": max(0, m.messages_received - m.points_normalized),
            },
            "db_write": {
                "status": "ok" if db_err == 0 else "error",
                "write_errors": db_err,
                "buffered_records": buffered,
                "last_write_at": m.last_message_at.isoformat() if m.last_message_at else None,
            },
        }

    overall = "ok"
    if tsdb_ok is False or (mqtt_ok is False and pipe_status == "ERROR"):
        overall = "degraded"

    return {
        "status": overall,
        "version": _VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(time.monotonic() - _start_time, 2),
        "components": {
            "timescaledb": {
                "status": "connected" if tsdb_ok else ("disconnected" if tsdb_ok is False else "unknown"),
            },
            "mqtt": {
                "status": "connected" if mqtt_ok else ("disconnected" if mqtt_ok is False else "unknown"),
            },
            "neuron": {"status": await _check_neuron()},
        },
        "pipeline": pipe_metrics,
        "validation": validation_points,
    }


def _check_tsdb() -> bool | None:
    """检查 TimescaleDB 连接。None=未配置, True=OK, False=失败。"""
    try:
        from app.services.telemetry_store import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
        return True
    except Exception:
        return False
    except ImportError:
        return None  # 模块未加载 (管道未启动)


@router.get("/health/ready", **protected(RUNTIME_READ))
async def readiness_check() -> dict:
    """
    K8s readiness probe.
    仅当 Pipeline RUNNING + MQTT 已连接时返回 ready=True。
    """
    is_ready = False
    if _pipeline and _pipeline.metrics.status.name == "RUNNING":
        if _pipeline._mqtt and _pipeline._mqtt.is_connected:
            is_ready = True

    return {
        "ready": is_ready,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
