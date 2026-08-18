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

# F1/F2/F3 调度任务列表
_scheduler_tasks = []
_data_trunk_tasks = []
_data_trunk_stop = None
_outbox_dispatcher = None
# 聚合 tick 间隔 (秒)
AGGREGATION_INTERVAL_SEC = 60
# F1 公式 tick 间隔 (秒)，比聚合更频繁，保证虚拟点先产出
FORMULA_INTERVAL_SEC = 30
# F2 规则与固定 EMS 策略 tick 间隔 (秒)
RULE_INTERVAL_SEC = 60


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
    global _pipeline, _scheduler_tasks
    global _data_trunk_tasks, _data_trunk_stop, _outbox_dispatcher
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
            from app.api.solution_delivery import get_default_control_commands

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
        # 幂等播种标准全局实体目录（单一数据源）
        try:
            from app.core.standard_entities import seed_standard_entities
            _seed_res = seed_standard_entities()
            logger.info("[Main] Standard entities seeded: {}", _seed_res.get("seeded"))
        except Exception as _se:
            logger.warning("[Main] Standard entity seed (non-fatal): {}", _se)
        try:
            from app.core.standard_fault_maps import seed_standard_fault_maps
            _fm_res = seed_standard_fault_maps()
            logger.info("[Main] Standard fault maps: {}", _fm_res)
        except Exception as _fe:
            logger.warning("[Main] Standard fault map seed (non-fatal): {}", _fe)
        try:
            from app.core.standard_device_templates import seed_standard_device_templates
            _dt_res = seed_standard_device_templates()
            logger.info("[Main] Standard device templates: {}", _dt_res)
        except Exception as _de:
            logger.warning("[Main] Standard device template seed (non-fatal): {}", _de)
        # 若当前没有任何实体绑定，自动执行一次国标映射绑定，提升开箱即用性
        try:
            from app.services.entity_binder import auto_bind_standard_entities
            from app.services.telemetry_store import get_connection
            _binding_count = 0
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM t_entity_bindings")
                    _binding_count = cur.fetchone()[0]
            if _binding_count == 0:
                _ab_res = auto_bind_standard_entities(dry_run=False)
                logger.info("[Main] Auto-bind on startup: created={}, skipped={}", _ab_res.get("created"), _ab_res.get("skipped"))
            else:
                logger.info("[Main] Auto-bind skipped: {} existing bindings", _binding_count)
        except Exception as _abe:
            logger.warning("[Main] Auto-bind on startup (non-fatal): {}", _abe)
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

    # L2 freshness and committed outbox delivery share the production DB seam.
    _data_trunk_tasks = []
    _data_trunk_stop = asyncio.Event()
    try:
        from app.api.websocket import get_entity_observation_broadcaster
        from app.services.data_trunk_outbox import (
            OutboxDispatcher,
            PostgresOutboxRepository,
        )
        from app.services.data_trunk_postgres import PostgresDataTrunkRepository

        freshness_repository = PostgresDataTrunkRepository()
        _outbox_dispatcher = OutboxDispatcher(
            PostgresOutboxRepository(),
            get_entity_observation_broadcaster(),
        )

        async def _wait_or_stop(seconds: float) -> None:
            try:
                await asyncio.wait_for(_data_trunk_stop.wait(), timeout=seconds)
            except TimeoutError:
                pass

        async def _freshness_loop() -> None:
            while not _data_trunk_stop.is_set():
                try:
                    await asyncio.to_thread(
                        freshness_repository.mark_expired_outputs_stale,
                        datetime.now(timezone.utc),
                    )
                except Exception as error:
                    logger.warning("[DataTrunk] freshness tick failed: {}", error)
                await _wait_or_stop(5.0)

        async def _outbox_loop() -> None:
            while not _data_trunk_stop.is_set():
                try:
                    await _outbox_dispatcher.run_once()
                except Exception as error:
                    logger.warning("[DataTrunk] outbox tick failed: {}", error)
                await _wait_or_stop(0.25)

        _data_trunk_tasks = [
            asyncio.create_task(_freshness_loop(), name="data_trunk_freshness"),
            asyncio.create_task(_outbox_loop(), name="data_trunk_outbox"),
        ]
        logger.success("[Main] L2 freshness and outbox dispatch started ✅")
    except Exception as error:
        from app.core.config import settings

        if settings.deployment_mode == "production":
            raise RuntimeError("L2 runtime services failed to start") from error
        logger.warning(
            "[Main] L2 runtime services unavailable (development): {}",
            error,
        )

    # Phase 2 S12/S6/S7: 启动 F1/F2/F3 调度器（原生 asyncio，不依赖 APScheduler）
    # 非致命：调度器失败不影响 F0 采集主链路
    _scheduler_tasks = []
    try:
        from app.services.aggregator import run_aggregation_tick
        from app.services.formula_engine import run_formula_tick
        from app.services.rule_engine import run_rule_tick
        from app.api.solution_delivery import get_default_ems_policy_runtime

        async def _periodic_task(name: str, interval: int, fn) -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await asyncio.to_thread(fn)
                except Exception as e:
                    logger.warning("[Scheduler] {} tick failed: {}", name, e)

        _scheduler_tasks.append(
            asyncio.create_task(
                _periodic_task("aggregation", AGGREGATION_INTERVAL_SEC, run_aggregation_tick),
                name="f3_aggregation",
            )
        )
        _scheduler_tasks.append(
            asyncio.create_task(
                _periodic_task("formula", FORMULA_INTERVAL_SEC, run_formula_tick),
                name="f1_formula",
            )
        )
        _scheduler_tasks.append(
            asyncio.create_task(
                _periodic_task("rules", RULE_INTERVAL_SEC, run_rule_tick),
                name="f2_rules",
            )
        )
        _scheduler_tasks.append(
            asyncio.create_task(
                _periodic_task("ems-policies", RULE_INTERVAL_SEC, get_default_ems_policy_runtime().tick),
                name="ems_policies",
            )
        )
        logger.success(
            "[Main] F1/F2/F3 schedulers started (formula={}s, rules={}s, agg={}s) ✅",
            FORMULA_INTERVAL_SEC, RULE_INTERVAL_SEC, AGGREGATION_INTERVAL_SEC,
        )
    except Exception as e:
        logger.error("[Main] F1/F2/F3 scheduler start failed (non-fatal): {}", e)

    yield

    # ---- Shutdown ----
    logger.info("ZiZu IoT Platform shutting down...")
    for _task in _scheduler_tasks:
        if not _task.done():
            _task.cancel()
    if _scheduler_tasks:
        await asyncio.gather(*_scheduler_tasks, return_exceptions=True)
        logger.info("[Main] F1/F2/F3 schedulers stopped")
    if _pipeline:
        await _pipeline.stop(close_database=False)
        logger.info("[Main] F0 data pipeline stopped")
    if _data_trunk_stop is not None:
        _data_trunk_stop.set()
    if _data_trunk_tasks:
        await asyncio.gather(*_data_trunk_tasks, return_exceptions=True)
    if _outbox_dispatcher is not None:
        try:
            await _outbox_dispatcher.run_once()
        except Exception as error:
            logger.warning("[DataTrunk] final outbox dispatch failed: {}", error)
    from app.services.telemetry_store import close_db_pool

    close_db_pool()
    logger.info("[Main] L2 freshness and outbox dispatch stopped")


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

    # F0 可视化: Nodes + Tags + Telemetry WS
    from app.api.nodes import router as nodes_router
    app.include_router(nodes_router, prefix="/api/v1", tags=["Nodes"])

    from app.api.tags import router as tags_router
    app.include_router(tags_router, prefix="/api/v1", tags=["Tags"])

    from app.api.websocket import router as ws_router
    app.include_router(ws_router, prefix="/api/v1", tags=["Telemetry WS"])

    from app.api.telemetry import router as telemetry_router
    app.include_router(telemetry_router, prefix="/api/v1", tags=["Telemetry"])

    from app.api.admin import router as admin_router
    app.include_router(admin_router, prefix="/api/v1", tags=["Admin"])

    from app.api.neuron import router as neuron_router
    app.include_router(neuron_router, prefix="/api/v1", tags=["Neuron"])

    from app.api.categories import router as categories_router
    app.include_router(categories_router, prefix="/api/v1", tags=["Categories"])

    from app.api.rules import router as rules_router
    app.include_router(rules_router, prefix="/api/v1", tags=["Rules"])

    from app.api.alarms import router as alarms_router
    app.include_router(alarms_router, prefix="/api/v1", tags=["Alarms"])

    from app.api.rule_templates import router as rule_templates_router
    app.include_router(rule_templates_router, prefix="/api/v1", tags=["Rule Templates"])

    # ---- Static Frontend (F0 可视化 V1) ----
    from app.api.entities import router as entities_router
    app.include_router(entities_router, prefix="/api/v1", tags=["Entities"])

    from app.api.fault_maps import router as fault_maps_router
    app.include_router(fault_maps_router, prefix="/api/v1", tags=["Fault Maps"])

    from app.api.alarm_levels import router as alarm_levels_router
    app.include_router(alarm_levels_router, prefix="/api/v1", tags=["Alarm Levels"])

    from app.api.device_templates import router as device_templates_router
    app.include_router(device_templates_router, prefix="/api/v1", tags=["Device Templates"])

    from app.api.nanomq import router as nanomq_router
    app.include_router(nanomq_router, prefix="/api/v1", tags=["nanoMQ"])

    from app.api.solution_delivery import router as solution_delivery_router
    app.include_router(
        solution_delivery_router,
        prefix="/api/v1",
        tags=["Solution Delivery"],
    )

    from app.api.ems_workbench import router as ems_workbench_router
    app.include_router(
        ems_workbench_router,
        prefix="/api/v1",
        tags=["EMS Workbench"],
    )

    from app.api.ems_policies import router as ems_policy_router
    app.include_router(ems_policy_router, prefix="/api/v1", tags=["EMS Policies"])

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

    from app.api.control_commands import router as control_commands_router
    app.include_router(
        control_commands_router,
        prefix="/api/v1",
        tags=["Control Commands"],
    )

    from app.api.rpc import router as rpc_router
    app.include_router(rpc_router, prefix="/api/v1", tags=["Control Commands"])

    from app.api.point_conversions import router as point_conversions_router
    app.include_router(
        point_conversions_router,
        prefix="/api/v1",
        tags=["Point Conversions"],
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
