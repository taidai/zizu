"""Postgres 公开交付主缝使用的无 lifespan HTTP 应用。"""
from dataclasses import dataclass
from datetime import UTC, datetime
import asyncio
import json

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.solution_delivery import router as solution_delivery_router
from app.api.entity_instances import router as entity_instances_router
from app.api.alarm_events import router as alarm_events_router
from app.api.alarm_configurations import router as alarm_configurations_router
from app.api.control_commands import router as control_commands_router
from app.api.neuron import router as neuron_router
from app.api.rpc import router as rpc_router
from app.api.dispatch_strategies import router as dispatch_strategies_router
from app.api.point_processings import router as point_processings_router
from app.api.committed_frames import (
    router as committed_frames_router,
    set_committed_frame_stream,
)
from app.core.config import settings
from app.services.telemetry_store import init_db_pool
from app.services.data_trunk_postgres import build_postgres_data_trunk
from app.services.pipeline import DataPipeline
from app.services.committed_frame_stream import CommittedFrameStream
from app.services.committed_frame_stream_postgres import (
    PostgresCommittedFrameStreamRepository,
)
from app.services.data_trunk_outbox import (
    FrameOutboxDispatcher,
    PostgresFrameOutboxRepository,
)


init_db_pool(min_conn=1, max_conn=4)

app = FastAPI()
app.include_router(health_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(solution_delivery_router, prefix="/api/v1")
app.include_router(entity_instances_router, prefix="/api/v1")
app.include_router(alarm_events_router, prefix="/api/v1")
app.include_router(alarm_configurations_router, prefix="/api/v1")
app.include_router(control_commands_router, prefix="/api/v1")
app.include_router(neuron_router, prefix="/api/v1")
app.include_router(rpc_router, prefix="/api/v1")
app.include_router(dispatch_strategies_router, prefix="/api/v1")
app.include_router(point_processings_router, prefix="/api/v1")
app.include_router(committed_frames_router, prefix="/api/v1")


@dataclass(frozen=True)
class _SimulatedMqttMessage:
    topic: str
    payload: bytes
    qos: int = 1


_protocol_pipeline = DataPipeline(data_trunk=build_postgres_data_trunk())
_committed_frame_stream = CommittedFrameStream(
    PostgresCommittedFrameStreamRepository()
)
set_committed_frame_stream(_committed_frame_stream)
_protocol_outbox = FrameOutboxDispatcher(
    PostgresFrameOutboxRepository(),
    _committed_frame_stream,
)


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
    await asyncio.to_thread(
        _protocol_pipeline.data_trunk.capture_tick,
        datetime.now(UTC),
    )
    await asyncio.to_thread(
        _protocol_pipeline.data_trunk.process_next,
        datetime.now(UTC),
    )
    await _protocol_outbox.run_once()
    return {
        "messages_received": (
            _protocol_pipeline.metrics.messages_received - received_before
        ),
        "points_written": (
            _protocol_pipeline.metrics.points_written_db - written_before
        ),
    }
