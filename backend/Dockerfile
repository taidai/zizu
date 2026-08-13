# ============================================================
# ZiZu Unified Image - Multi-stage Docker Build
#
# Stage 1: Node.js 构建前端 dist
# Stage 2: Python 后端 + 前端静态文件
#
# 构建:
#   docker build -t zizu:0.1.0 -f backend/Dockerfile .
#   docker buildx build --platform linux/arm64 -t zizu:0.1.0 -f backend/Dockerfile . --load
#
# 说明:
#   - 基础镜像走 DaoCloud 加速 (docker.io 直连国内 EOF)
#   - pip 走清华源 (pypi.org 直连慢)
#   - F0 精简依赖: zen-engine/pandas/sympy/sqlalchemy 等 F1/F2 组件后置
# ============================================================

# ---- Stage 1: Frontend Build ----
FROM docker.m.daocloud.io/library/node:22-alpine AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline

COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Backend + Frontend ----
FROM docker.m.daocloud.io/library/python:3.12-slim

LABEL maintainer="ZiZu Team"
LABEL description="ZiZu IoT Platform - Unified Backend + Frontend"
LABEL version="0.1.0"

WORKDIR /app

# 系统依赖 (psycopg2 需要 libpq 运行时)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# F0 核心依赖 + F3 运行时依赖 (清华镜像加速)
RUN pip install --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        fastapi \
        "uvicorn[standard]" \
        psycopg2-binary \
        paho-mqtt \
        loguru \
        pydantic \
        pydantic-settings \
        pint \
        websockets \
        zen-engine \
        httpx \
        python-multipart \
        "apscheduler[asyncio,timezone]>=3.10.4" \
        "pyyaml>=6.0.2"

# F2 规则引擎依赖
RUN pip install --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        zen-engine

# 复制后端应用代码
COPY backend/app ./app

# 首个管理员一次性引导（使用 python -m scripts.bootstrap_admin 执行）
COPY scripts/bootstrap_admin.py ./scripts/bootstrap_admin.py

# 复制前端构建产物
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# 非 root 运行
RUN groupadd -r zizu && useradd -r -g zizu zizu && \
    chown -R zizu:zizu /app
USER zizu

EXPOSE 9000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/api/v1/health/live')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "info", "--no-proxy-headers"]
