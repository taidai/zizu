"""Postgres 公开交付主缝使用的无 lifespan HTTP 应用。"""
from dataclasses import dataclass
import json

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.solution_delivery import router as solution_delivery_router
from app.api.entity_instances import router as entity_instances_router
from app.api.rules import router as rules_router
from app.core.config import settings
from app.services.telemetry_store import init_db_pool
from app.services.pipeline import DataPipeline


init_db_pool(min_conn=1, max_conn=4)

app = FastAPI()
app.include_router(health_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(solution_delivery_router, prefix="/api/v1")
app.include_router(entity_instances_router, prefix="/api/v1")
app.include_router(rules_router, prefix="/api/v1")


@dataclass(frozen=True)
class _SimulatedMqttMessage:
    topic: str
    payload: bytes
    qos: int = 1


_protocol_pipeline = DataPipeline()


@app.post("/protocol-simulator/neuron")
async def publish_neuron_observation(payload: dict) -> dict:
    """Test-only protocol boundary; never registered by the production app."""
    await _protocol_pipeline.reload_rules_now()
    received_before = _protocol_pipeline.metrics.messages_received
    written_before = _protocol_pipeline.metrics.points_written_db
    await _protocol_pipeline.on_message(
        _SimulatedMqttMessage(
            topic=f"neuron/{payload.get('node', 'unknown')}/telemetry",
            payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )
    )
    await _protocol_pipeline.flush_now()
    return {
        "messages_received": (
            _protocol_pipeline.metrics.messages_received - received_before
        ),
        "points_written": (
            _protocol_pipeline.metrics.points_written_db - written_before
        ),
    }
