# ============================================================
# ZiZu Unified Image - Multi-stage Docker Build
#
# Stage 1: Node.js 构建前端 dist
# Stage 2: Python 后端 + 前端静态文件
#
# 构建:
#   docker build --build-arg ZIZU_VERSION=0.4.78 -t zizu:0.4.78 -f backend/Dockerfile .
#   docker buildx build --platform linux/arm64 --build-arg ZIZU_VERSION=0.4.78 -t zizu:0.4.78 -f backend/Dockerfile . --load
#
# 说明:
#   - 正式构建使用 Docker Official Images；镜像平台摘要由 release.json 锁定
#   - pip 走清华源 (pypi.org 直连慢)
#   - F0 精简依赖: zen-engine/pandas/sympy/sqlalchemy 等 F1/F2 组件后置
# ============================================================

# ---- Stage 1: Frontend Build ----
FROM node:22-alpine AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline --no-audit --no-fund --fetch-retries=2 --fetch-timeout=120000

COPY VERSION /app/VERSION
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Backend + Frontend ----
FROM python:3.12-slim

ARG ZIZU_VERSION
LABEL maintainer="ZiZu Team"
LABEL description="ZiZu IoT Platform - Unified Backend + Frontend"
LABEL org.opencontainers.image.version="${ZIZU_VERSION}"

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
        "python-jose[cryptography]>=3.3.0" \
        "passlib[bcrypt]>=1.7.4" \
        python-multipart \
        "apscheduler[asyncio,timezone]>=3.10.4" \
        "pyyaml>=6.0.2"

# 复制后端应用代码
COPY backend/app ./app
COPY init-db ./init-db

# 首个管理员一次性引导（使用 python -m scripts.bootstrap_admin 执行）
COPY scripts/bootstrap_admin.py ./scripts/bootstrap_admin.py
COPY backend/docker-entrypoint.sh ./docker-entrypoint.sh

# 复制前端构建产物
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# 非 root 运行
RUN groupadd -r zizu && useradd -r -g zizu zizu && \
    chown -R zizu:zizu /app && \
    chmod 0755 /app/docker-entrypoint.sh
USER zizu

EXPOSE 9000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/api/v1/health/live')" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
