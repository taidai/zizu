"""
nanoMQ Proxy API — nanoMQ 配置与管理界面后端代理

统一暴露 /api/v1/nanomq/*，前端无需直连 nanoMQ REST 端口。
"""
from __future__ import annotations

import subprocess
from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from app.api.business_security import SYSTEM_MANAGE, protected
from app.services.nanomq_client import NanoMQAPIError, get_nanomq_client

router = APIRouter()


# ══════════════════════════════════════
# 请求/响应模型
# ══════════════════════════════════════

class NanoMQSubscribeRequest(BaseModel):
    topic: str = Field(..., description="订阅主题")
    qos: int = Field(0, ge=0, le=2, description="QoS 等级")


class NanoMQACLRule(BaseModel):
    action: str = Field(..., description="pub / sub / all")
    permit: str = Field(..., description="allow / deny")
    username: str | None = Field(None, description="用户名")
    clientid: str | None = Field(None, description="客户端 ID")
    ipaddr: str | None = Field(None, description="IP 地址")
    topic: str = Field(..., description="主题过滤")


class NanoMQACLUpdate(BaseModel):
    rules: list[NanoMQACLRule] = Field(..., description="ACL 规则列表")


class NanoMQConfigUpdate(BaseModel):
    content: str = Field(..., description="完整 HOCON 配置内容")


class NanoMQRestartResponse(BaseModel):
    restarted: bool
    message: str


# ══════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════


def _proxy_nanomq(method: str, path: str, data: Any | None = None) -> Any:
    """代理到 nanoMQ REST API，统一处理异常。"""
    client = get_nanomq_client()
    try:
        return client._request(method, path, data=data)
    except NanoMQAPIError as e:
        logger.warning("[API/nanomq] {} {} failed: {}", method, path, e.detail)
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


# ══════════════════════════════════════
# 状态与监控
# ══════════════════════════════════════

@router.get("/nanomq/status", **protected(SYSTEM_MANAGE))
async def nanomq_status() -> dict:
    """获取 nanoMQ 运行状态（brokers/nodes/metrics）。"""
    client = get_nanomq_client()
    try:
        return client.get_status()
    except NanoMQAPIError as e:
        # 返回结构化错误，前端仍可展示离线状态
        return {
            "error": True,
            "status_code": e.status_code,
            "message": e.detail,
        }


@router.get("/nanomq/clients", **protected(SYSTEM_MANAGE))
async def nanomq_clients() -> dict:
    """获取已连接客户端列表。"""
    return _proxy_nanomq("GET", "/api/v4/clients")


@router.get("/nanomq/subscriptions", **protected(SYSTEM_MANAGE))
async def nanomq_subscriptions() -> dict:
    """获取订阅列表。"""
    return _proxy_nanomq("GET", "/api/v4/subscriptions")


@router.get("/nanomq/routes", **protected(SYSTEM_MANAGE))
async def nanomq_routes() -> dict:
    """获取路由列表。"""
    return _proxy_nanomq("GET", "/api/v4/routes")


# ══════════════════════════════════════
# 订阅管理
# ══════════════════════════════════════

@router.post("/nanomq/subscribe", **protected(SYSTEM_MANAGE))
async def nanomq_subscribe(req: NanoMQSubscribeRequest) -> dict:
    """通过 nanoMQ REST API 订阅一个主题。"""
    client = get_nanomq_client()
    try:
        return client.subscribe(req.topic, req.qos)
    except NanoMQAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


# ══════════════════════════════════════
# ACL 管理
# ══════════════════════════════════════

@router.get("/nanomq/acl", **protected(SYSTEM_MANAGE))
async def nanomq_acl() -> dict:
    """获取 ACL 规则。"""
    return _proxy_nanomq("GET", "/api/v4/acl")


@router.post("/nanomq/acl", **protected(SYSTEM_MANAGE))
async def nanomq_acl_update(req: NanoMQACLUpdate) -> dict:
    """覆盖写入 ACL 规则。"""
    client = get_nanomq_client()
    try:
        rules = [r.model_dump(exclude_none=True) for r in req.rules]
        return client.set_acl(rules)
    except NanoMQAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


# ══════════════════════════════════════
# 配置文件管理
# ══════════════════════════════════════

@router.get("/nanomq/config", **protected(SYSTEM_MANAGE))
async def nanomq_config() -> dict:
    """读取当前 nanoMQ 配置文件内容。"""
    client = get_nanomq_client()
    content = client.read_conf()
    return {"content": content, "path": client.config.conf_path}


@router.put("/nanomq/config", **protected(SYSTEM_MANAGE))
async def nanomq_config_update(req: NanoMQConfigUpdate) -> dict:
    """写入 nanoMQ 配置文件（写前自动备份）。"""
    client = get_nanomq_client()
    try:
        client.write_conf(req.content)
        return {"saved": True, "path": client.config.conf_path}
    except Exception as e:
        logger.error("[API/nanomq] Save config failed: {}", e)
        raise HTTPException(status_code=500, detail={"message": str(e)}) from e


# ══════════════════════════════════════
# 容器重启
# ══════════════════════════════════════

@router.post(
    "/nanomq/restart",
    response_model=NanoMQRestartResponse,
    **protected(SYSTEM_MANAGE),
)
async def nanomq_restart() -> NanoMQRestartResponse:
    """
    重启 nanoMQ 容器使配置生效。
    需要 backend 容器挂载 /var/run/docker.sock。
    """
    container_candidates = ["zizu-nanomq", "omnithings-nanomq", "nanomq"]
    for container in container_candidates:
        try:
            result = subprocess.run(
                ["docker", "restart", container],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0:
                return NanoMQRestartResponse(
                    restarted=True,
                    message=f"容器 {container} 已重启",
                )
        except Exception as e:
            logger.warning("[API/nanomq] Restart attempt {} failed: {}", container, e)

    return NanoMQRestartResponse(
        restarted=False,
        message="未能自动重启 nanoMQ 容器（未找到容器或无 docker.sock 权限）。"
                "请手动重启 nanoMQ 容器使配置生效。",
    )
