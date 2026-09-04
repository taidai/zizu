"""
ZiZu IoT Platform - FastAPI Application Entry Point
Phase 1 S0-S5: 集成 F0 数据管道到应用生命周期
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

import sys
from pathlib import Path

# 配置 loguru
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
           "<level>{level:<7}</level> | "
           "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
           "<level>{message}</level>",
)

# Pipeline 实例引用 (供 Health API 使用)
_pipeline = None

def get_pipeline():
    """Return the pipeline instance (or None if not started)."""
    return _pipeline

_data_trunk_tasks = []
_data_trunk_stop = None


async def run_alarm_http_notification_loop(dispatcher, stop_event) -> None:
    """Deliver committed alarm intents without exposing request secrets in logs."""
    import asyncio

    while not stop_event.is_set():
        try:
            processed = await dispatcher.run_once()
        except Exception as error:
            logger.warning(
                "[AlarmHTTP] delivery tick failed: {}",
                type(error).__name__,
            )
            processed = 0
        if processed == 0 and not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.5)
            except TimeoutError:
                pass


def build_committed_frame_fanout(alarm_consumer, jdm_consumer, stream):
    """Keep the production committed-frame consumer order explicit."""
    from app.services.data_trunk_outbox import CommittedFrameFanout

    return CommittedFrameFanout((alarm_consumer, jdm_consumer, stream))


def _load_version() -> str:
    """从当前文件所在目录向上查找 VERSION 文件。"""
    here = Path(__file__).resolve().parent
    candidates = [here / "VERSION"]
    for parent in here.parents:
        candidates.append(parent / "VERSION")
    for cand in candidates:
        if cand.exists():
            return cand.read_text().strip()
    return "0.0.0"


APP_VERSION = _load_version()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — 启停 F0 数据管道 + F3 聚合调度器。"""
    global _pipeline
    global _data_trunk_tasks, _data_trunk_stop
    import asyncio

    # ---- Startup ----
    logger.info("ZiZu IoT Platform starting up...")

    # Load runtime config from DB (overrides .env defaults)
    try:
        from app.core.config import settings
        from app.services.config_store import init_config_table, load_mqtt_topics
        from app.services.telemetry_store import (
            init_db_pool,
            verify_legacy_alarm_history_gate,
        )
        from app.core.migrations import run_migrations

        # DB pool must be initialized before reading t_system_config
        init_db_pool(min_conn=settings.db_pool_min, max_conn=settings.db_pool_max)
        mig_result = run_migrations()
        logger.info(
            "[Main] DB migrations: applied={}, skipped={}, errors={}",
            mig_result.get("applied"), mig_result.get("skipped"), mig_result.get("errors"),
        )
        if mig_result.get("errors"):
            message = "Database migrations did not complete successfully"
            if settings.deployment_mode == "production":
                raise RuntimeError(message)
            logger.warning("[Main] {} (development mode)", message)
        if settings.deployment_mode == "production":
            verify_legacy_alarm_history_gate()
            from app.services.data_trunk_postgres import (
                verify_data_trunk_contract_gate,
            )

            verified_entities = verify_data_trunk_contract_gate()
            logger.info(
                "[Main] Data trunk contract verified for {} entities",
                verified_entities,
            )
        try:
            from app.services.identity import verify_identity_schema

            verify_identity_schema()
        except Exception as identity_schema_error:
            if settings.deployment_mode == "production":
                raise RuntimeError(
                    "Production identity schema is unavailable"
                ) from identity_schema_error
            logger.warning(
                "[Main] Identity schema unavailable (development mode): {}",
                identity_schema_error,
            )
        init_config_table()
        try:
            from app.api.control_commands import get_default_control_commands

            recovered_commands = get_default_control_commands().recover()
            if recovered_commands:
                logger.info(
                    "[Main] Recovered {} in-flight control commands without redispatch",
                    len(recovered_commands),
                )
        except Exception as control_recovery_error:
            if settings.deployment_mode == "production":
                raise RuntimeError("Control command recovery failed") from control_recovery_error
            logger.warning(
                "[Main] Control command recovery unavailable (development mode): {}",
                control_recovery_error,
            )
        try:
            from app.core.standard_fault_maps import seed_standard_fault_maps
            _fm_res = seed_standard_fault_maps()
            logger.info("[Main] Standard fault maps: {}", _fm_res)
        except Exception as _fe:
            logger.warning("[Main] Standard fault map seed (non-fatal): {}", _fe)
        persisted_topic = load_mqtt_topics()
        if persisted_topic:
            settings.mqtt_telemetry_topic = persisted_topic
            logger.info("[Main] Loaded MQTT telemetry topic from DB: {}", persisted_topic)
    except Exception as e:
        from app.core.config import settings

        if settings.deployment_mode == "production":
            logger.error("[Main] Production database initialization failed: {}", e)
            raise
        logger.warning("[Main] Failed to load runtime config from DB (development): {}", e)

    # Phase 1 S2+: 启动 F0 数据管道 (MQTT → Parse → Normalize → Store)
    try:
        from app.services.pipeline import DataPipeline

        _pipeline = DataPipeline()
        await _pipeline.start()

        # 注入给 Health API
        from app.api.health import set_pipeline
        set_pipeline(_pipeline)

        logger.success("[Main] F0 data pipeline started ✅")
    except Exception as e:
        logger.error("[Main] F0 pipeline CRITICAL failure: {}", e)
        logger.error("[Main] Shutting down — no MQTT ingestion without pipeline")
        raise  # fail-fast: 管道是核心组件，死了就不该假装活着

    # One capture cadence and one ordered processor are the only data writers.
    _data_trunk_tasks = []
    _data_trunk_stop = asyncio.Event()
    runtime = _pipeline.data_trunk
    from app.api.committed_frames import get_committed_frame_stream
    from app.services.data_trunk_outbox import (
        FrameOutboxDispatcher,
        PostgresFrameOutboxRepository,
    )
    from app.services.committed_l2_alarm_consumer import (
        build_postgres_committed_l2_alarm_consumer,
    )
    from app.services.committed_l2_jdm_consumer import CommittedL2JdmConsumer
    from app.services.dispatch_strategies import StrategyRuntime
    from app.services.dispatch_strategy_postgres import PostgresStrategyRepository
    from app.services.dispatch_strategy_workers import (
        ControlIntentDispatcher,
        FixedMinuteTickWorker,
    )
    from app.api.control_commands import get_automated_control_commands
    from app.services.alarm_http_notification_postgres import (
        build_postgres_alarm_http_notification_dispatcher,
    )

    committed_frame_stream = get_committed_frame_stream()
    strategy_repository = PostgresStrategyRepository()
    strategy_runtime = StrategyRuntime(strategy_repository)
    strategy_minute_worker = FixedMinuteTickWorker(
        strategy_repository,
        strategy_runtime,
    )
    strategy_intent_dispatcher = ControlIntentDispatcher(
        strategy_repository,
        get_automated_control_commands(),
    )
    committed_frame_fanout = build_committed_frame_fanout(
        build_postgres_committed_l2_alarm_consumer(),
        CommittedL2JdmConsumer(strategy_runtime),
        committed_frame_stream,
    )
    frame_outbox_dispatcher = FrameOutboxDispatcher(
        PostgresFrameOutboxRepository(),
        committed_frame_fanout,
    )
    runtime.configuration_gate.register_committed_frame_consumer()
    alarm_http_dispatcher = build_postgres_alarm_http_notification_dispatcher()
    recovered_strategy_intents = await asyncio.to_thread(
        strategy_intent_dispatcher.recover,
        datetime.now(timezone.utc),
    )
    if recovered_strategy_intents:
        logger.info(
            "[Strategy] Reconciled {} committed in-flight intents",
            recovered_strategy_intents,
        )

    async def _wait_or_stop(seconds: float) -> None:
        try:
            await asyncio.wait_for(_data_trunk_stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _data_frame_capture_loop() -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while not _data_trunk_stop.is_set():
            try:
                await asyncio.to_thread(
                    runtime.capture_tick,
                    datetime.now(timezone.utc),
                )
            except Exception as error:
                logger.warning("[DataFrame] capture tick failed: {}", error)
            deadline += 1.0
            await _wait_or_stop(max(0.0, deadline - loop.time()))

    async def _data_frame_processor_loop() -> None:
        while not _data_trunk_stop.is_set():
            try:
                terminal = await asyncio.to_thread(
                    runtime.process_next,
                    datetime.now(timezone.utc),
                )
            except Exception as error:
                logger.warning("[DataFrame] processor tick failed: {}", error)
                terminal = None
            if terminal is None:
                await _wait_or_stop(0.25)

    async def _data_frame_outbox_loop() -> None:
        while not _data_trunk_stop.is_set():
            try:
                published = await frame_outbox_dispatcher.run_once()
            except Exception as error:
                logger.warning("[DataFrame] outbox tick failed: {}", error)
                published = 0
            if published == 0:
                await _wait_or_stop(0.25)

    _data_trunk_tasks = [
        asyncio.create_task(
            _data_frame_capture_loop(), name="data_frame_capture"
        ),
        asyncio.create_task(
            _data_frame_processor_loop(), name="data_frame_processor"
        ),
        asyncio.create_task(
            _data_frame_outbox_loop(), name="data_frame_outbox"
        ),
        asyncio.create_task(
            run_alarm_http_notification_loop(
                alarm_http_dispatcher,
                _data_trunk_stop,
            ),
            name="alarm_http_notification",
        ),
        asyncio.create_task(
            strategy_minute_worker.run(_data_trunk_stop),
            name="strategy_minute_tick",
        ),
        asyncio.create_task(
            strategy_intent_dispatcher.run(_data_trunk_stop),
            name="strategy_intent_dispatcher",
        ),
    ]
    logger.success(
        "[Main] data-frame, alarm HTTP and dispatch-strategy workers started ✅"
    )

    yield

    # ---- Shutdown ----
    logger.info("ZiZu IoT Platform shutting down...")
    if _data_trunk_stop is not None:
        _data_trunk_stop.set()
    if _data_trunk_tasks:
        await asyncio.gather(*_data_trunk_tasks, return_exceptions=True)
    if _pipeline:
        await _pipeline.stop(close_database=False)
        logger.info("[Main] F0 data pipeline stopped")
    from app.services.telemetry_store import close_db_pool

    close_db_pool()
    logger.info("[Main] data-frame runtime stopped")


def create_app() -> FastAPI:
    """Application factory."""
    from app.core.config import settings

    expose_development_docs = settings.deployment_mode == "development"
    app = FastAPI(
        title="ZiZu API",
        description="ZiZu IoT Platform - 替代 ThingsBoard 的工业 IoT 开发平台",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs" if expose_development_docs else None,
        redoc_url="/api/redoc" if expose_development_docs else None,
        openapi_url="/api/openapi.json" if expose_development_docs else None,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Register routers ----
    # Phase 1 S1: Health
    from app.api.health import router as health_router
    app.include_router(health_router, prefix="/api/v1", tags=["Health"])

    from app.api.auth import router as auth_router
    app.include_router(auth_router, prefix="/api/v1", tags=["Authentication"])

    # F0 可视化: Nodes + Tags
    from app.api.nodes import router as nodes_router
    app.include_router(nodes_router, prefix="/api/v1", tags=["Nodes"])

    from app.api.tags import router as tags_router
    app.include_router(tags_router, prefix="/api/v1", tags=["Tags"])

    from app.api.committed_frames import router as committed_frames_router
    app.include_router(
        committed_frames_router,
        prefix="/api/v1",
        tags=["Committed Data Frames"],
    )

    from app.api.telemetry import router as telemetry_router
    app.include_router(telemetry_router, prefix="/api/v1", tags=["Telemetry"])

    from app.api.admin import router as admin_router
    app.include_router(admin_router, prefix="/api/v1", tags=["Admin"])

    from app.api.neuron import router as neuron_router
    app.include_router(neuron_router, prefix="/api/v1", tags=["Neuron"])

    from app.api.categories import router as categories_router
    app.include_router(categories_router, prefix="/api/v1", tags=["Categories"])

    from app.api.dispatch_strategies import router as dispatch_strategies_router
    app.include_router(
        dispatch_strategies_router,
        prefix="/api/v1",
        tags=["Dispatch Strategies"],
    )

    from app.api.alarms import router as alarms_router
    app.include_router(alarms_router, prefix="/api/v1", tags=["Alarms"])

    # ---- Static Frontend (F0 可视化 V1) ----
    from app.api.fault_maps import router as fault_maps_router
    app.include_router(fault_maps_router, prefix="/api/v1", tags=["Fault Maps"])

    from app.api.nanomq import router as nanomq_router
    app.include_router(nanomq_router, prefix="/api/v1", tags=["nanoMQ"])

    from app.api.ems_workbench import router as ems_workbench_router
    app.include_router(
        ems_workbench_router,
        prefix="/api/v1",
        tags=["EMS Workbench"],
    )

    from app.api.entity_instances import router as entity_instances_router
    app.include_router(
        entity_instances_router,
        prefix="/api/v1",
        tags=["Entity Instances"],
    )

    from app.api.alarm_events import router as alarm_events_router
    app.include_router(
        alarm_events_router,
        prefix="/api/v1",
        tags=["Alarm Events"],
    )

    from app.api.alarm_configurations import router as alarm_configurations_router
    app.include_router(
        alarm_configurations_router,
        prefix="/api/v1",
        tags=["Alarm Configurations"],
    )

    from app.api.alarm_http_notifications import (
        router as alarm_http_notifications_router,
    )
    app.include_router(
        alarm_http_notifications_router,
        prefix="/api/v1",
        tags=["Alarm HTTP Notifications"],
    )

    from app.api.alarm_notification_deliveries import (
        router as alarm_notification_deliveries_router,
    )
    app.include_router(
        alarm_notification_deliveries_router,
        prefix="/api/v1",
        tags=["Alarm HTTP Notifications"],
    )

    from app.api.control_commands import router as control_commands_router
    app.include_router(
        control_commands_router,
        prefix="/api/v1",
        tags=["Control Commands"],
    )

    from app.api.rpc import router as rpc_router
    app.include_router(rpc_router, prefix="/api/v1", tags=["Control Commands"])

    from app.api.point_processings import router as point_processings_router
    app.include_router(
        point_processings_router,
        prefix="/api/v1",
        tags=["Point Processing"],
    )

     # ---- Static Frontend (F0 可视化 V1) ----
    # 后端直接托管前端 dist，无需独立 nginx 容器
    import os
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    FRONTEND_DIST = os.environ.get("FRONTEND_DIST", "/app/frontend/dist")

    if os.path.isdir(FRONTEND_DIST):
        _assets = os.path.join(FRONTEND_DIST, "assets")
        if os.path.isdir(_assets):
            app.mount("/assets", StaticFiles(directory=_assets), name="assets")
            logger.info("[Main] Frontend assets mounted at /assets")

        # SPA catch-all — 非 API 路由回退到 index.html
        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            # 绝不拦截 API / 文档路由
            if full_path.startswith(("api/", "docs", "redoc", "openapi")):
                raise StarletteHTTPException(status_code=404)
            # 尝试静态文件 (favicon, robots.txt 等)
            candidate = os.path.join(FRONTEND_DIST, full_path)
            if os.path.isfile(candidate):
                return FileResponse(candidate)
            # SPA 回退 → index.html
            index = os.path.join(FRONTEND_DIST, "index.html")
            if os.path.isfile(index):
                return FileResponse(index)
            raise StarletteHTTPException(status_code=404)

        logger.info("[Main] Frontend SPA served from {}", FRONTEND_DIST)
    else:
        # 无前端 dist 时保留 API 文档重定向
        from fastapi.responses import RedirectResponse

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse(
                url="/api/docs" if expose_development_docs else "/api/v1/health/live"
            )

    # TODO Phase 1 S4: app.include_router(telemetry_router, prefix="/api/v1")
    # TODO Phase 2:     app.include_router(virtual_points_router, prefix="/api/v1")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
