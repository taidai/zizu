"""Postgres 公开交付主缝使用的无 lifespan HTTP 应用。"""
from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.solution_delivery import router as solution_delivery_router
from app.core.config import settings
from app.services.telemetry_store import init_db_pool


init_db_pool(min_conn=1, max_conn=4)

app = FastAPI()
app.include_router(health_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(solution_delivery_router, prefix="/api/v1")
