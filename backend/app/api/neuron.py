"""
Neuron Proxy API — Neuron 代理接口

代理 Neuron API，提供统一的节点/组/点位管理接口。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field

from app.api.business_security import (
    CONTROL_WRITE,
    GATEWAY_MANAGE,
    capability_metadata,
    principal_for,
    protected,
)
from app.api.control_commands import (
    compatibility_error,
    compatibility_response,
    get_control_compatibility,
)
from app.services.control_commands import ControlCommandCompatibility
from app.services.identity import Principal

router = APIRouter()


# ══════════════════════════════════════
# 请求/响应模型
# ══════════════════════════════════════

class NeuronNodeCreate(BaseModel):
    name: str = Field(..., description="节点名称")
    plugin: str = Field(..., description="插件名称 (modbus-tcp / modbus-rtu)")
    host: str = Field("127.0.0.1", description="设备 IP (TCP)")
    port: int = Field(502, description="设备端口 (TCP)")
    device: str = Field("/dev/ttyS0", description="串口设备 (RTU)")
    baud: int = Field(9600, description="波特率 (RTU)")


class NeuronGroupCreate(BaseModel):
    node: str = Field(..., description="节点名称")
    name: str = Field(..., description="组名称")
    interval: int = Field(1000, ge=100, le=60000, description="采集间隔 (毫秒)")


class NeuronTagCreate(BaseModel):
    node: str = Field(..., description="节点名称")
    group: str = Field(..., description="组名称")
    name: str = Field(..., description="点位名称")
    address: str = Field(..., description="寄存器地址")
    data_type: str = Field("FLOAT", description="数据类型")


class NeuronWriteRequest(BaseModel):
    node: str = Field(..., description="Neuron 南向节点名")
    group: str = Field(..., description="采集组名")
    tag: str = Field(..., description="点位名")
    value: Any = Field(..., description="写入值")
    confirmation_id: UUID | None = Field(None, description="高风险命令的确认 ID")


# ══════════════════════════════════════
# 节点管理
# ══════════════════════════════════════

@router.get("/neuron/nodes", **protected(GATEWAY_MANAGE))
async def list_neuron_nodes() -> dict:
    """获取 Neuron 驱动节点列表。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        nodes = client.get_nodes(node_type=1)
        return {"nodes": nodes, "total": len(nodes)}
    except Exception as e:
        logger.error("[API/neuron] Get nodes failed: {}", e)
        return {"nodes": [], "total": 0, "error": str(e)}


@router.post("/neuron/nodes", **protected(GATEWAY_MANAGE))
async def create_neuron_node(req: NeuronNodeCreate) -> dict:
    """创建 Neuron 驱动节点。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()

        # 根据插件类型构建参数
        if "tcp" in req.plugin.lower():
            params = {"host": req.host, "port": req.port}
        else:
            params = {"device": req.device, "baud": req.baud}

        result = client.add_node(req.name, req.plugin, params)
        logger.info("[API/neuron] Node created: {}", req.name)
        return {"status": "ok", "node": result}
    except Exception as e:
        logger.error("[API/neuron] Create node failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/neuron/nodes/{name}", **protected(GATEWAY_MANAGE))
async def delete_neuron_node(name: str) -> dict:
    """删除 Neuron 节点。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        result = client.delete_node(name)
        logger.info("[API/neuron] Node deleted: {}", name)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error("[API/neuron] Delete node failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/neuron/nodes/{name}/start", **protected(GATEWAY_MANAGE))
async def start_neuron_node(name: str) -> dict:
    """启动 Neuron 节点。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        result = client.start_node(name)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error("[API/neuron] Start node failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/neuron/nodes/{name}/stop", **protected(GATEWAY_MANAGE))
async def stop_neuron_node(name: str) -> dict:
    """停止 Neuron 节点。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        result = client.stop_node(name)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error("[API/neuron] Stop node failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


# ══════════════════════════════════════
# 组管理
# ══════════════════════════════════════

@router.get("/neuron/groups", **protected(GATEWAY_MANAGE))
async def list_neuron_groups(node: str) -> dict:
    """获取节点下的组列表。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        groups = client.get_groups(node)
        return {"groups": groups, "total": len(groups)}
    except Exception as e:
        logger.error("[API/neuron] Get groups failed: {}", e)
        return {"groups": [], "total": 0, "error": str(e)}


@router.post("/neuron/groups", **protected(GATEWAY_MANAGE))
async def create_neuron_group(req: NeuronGroupCreate) -> dict:
    """创建采集组。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        result = client.add_group(req.node, req.name, req.interval)
        logger.info("[API/neuron] Group created: {}/{}", req.node, req.name)
        return {"status": "ok", "group": result}
    except Exception as e:
        logger.error("[API/neuron] Create group failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/neuron/groups/{node}/{name}", **protected(GATEWAY_MANAGE))
async def delete_neuron_group(node: str, name: str) -> dict:
    """删除采集组。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        result = client.delete_group(node, name)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error("[API/neuron] Delete group failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


# ══════════════════════════════════════
# 点位管理
# ══════════════════════════════════════

@router.get("/neuron/tags", **protected(GATEWAY_MANAGE))
async def list_neuron_tags(node: str, group: str) -> dict:
    """获取组下的点位列表。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        tags = client.get_tags(node, group)
        return {"tags": tags, "total": len(tags)}
    except Exception as e:
        logger.error("[API/neuron] Get tags failed: {}", e)
        return {"tags": [], "total": 0, "error": str(e)}


@router.post("/neuron/tags", **protected(GATEWAY_MANAGE))
async def create_neuron_tags(req: list[NeuronTagCreate]) -> dict:
    """批量创建点位。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        # 按 node/group 分组
        grouped: dict[tuple[str, str], list[dict]] = {}
        for tag in req:
            key = (tag.node, tag.group)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append({
                "name": tag.name,
                "address": tag.address,
                "attribute": 1,  # read
                "type": 4,       # float
            })

        results = []
        for (node, group), tags in grouped.items():
            result = client.add_tags(node, group, tags)
            results.append(result)

        logger.info("[API/neuron] Tags created: {} groups", len(results))
        return {"status": "ok", "groups_created": len(results)}
    except Exception as e:
        logger.error("[API/neuron] Create tags failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/neuron/tags/{node}/{group}/{name}", **protected(GATEWAY_MANAGE))
async def delete_neuron_tag(node: str, group: str, name: str) -> dict:
    """删除点位。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        result = client.delete_tag(node, group, name)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error("[API/neuron] Delete tag failed: {}", e)
        raise HTTPException(status_code=400, detail=str(e))


# ══════════════════════════════════════
# 状态监控
# ══════════════════════════════════════

@router.get("/neuron/status", **protected(GATEWAY_MANAGE))
async def get_neuron_status() -> dict:
    """获取 Neuron 全局状态。"""
    from app.services.neuron_client import get_neuron_client

    try:
        client = get_neuron_client()
        version = client.get_version()
        plugins = client.get_plugin_list()
        nodes = client.get_nodes(node_type=1)

        return {
            "version": version,
            "plugins": plugins,
            "nodes": nodes,
            "node_count": len(nodes),
        }
    except Exception as e:
        logger.error("[API/neuron] Get status failed: {}", e)
        return {"error": str(e)}
@router.post(
    "/neuron/write",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONTROL_WRITE),
)
async def write_neuron_tag(
    req: NeuronWriteRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200),
    principal: Principal = Depends(principal_for(CONTROL_WRITE)),
    compatibility: ControlCommandCompatibility = Depends(get_control_compatibility),
) -> dict:
    """兼容入口：仅把已确认的 Neuron 点位加工为统一控制命令。"""
    command = compatibility.submit_neuron(
        actor=principal.actor,
        node=req.node,
        group=req.group,
        tag=req.tag,
        value=req.value,
        idempotency_key=idempotency_key,
        confirmation_id=req.confirmation_id,
    )
    if command.status == "rejected":
        raise compatibility_error(command)
    return compatibility_response(command)
