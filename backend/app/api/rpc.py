"""遗留 MQTT RPC 兼容入口，只转换为统一控制命令。"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.api.business_security import CONTROL_WRITE, capability_metadata, principal_for
from app.api.control_commands import (
    compatibility_error,
    compatibility_response,
    get_control_compatibility,
)
from app.services.control_commands import ControlCommandCompatibility
from app.services.identity import Principal


router = APIRouter()


class RpcRequest(BaseModel):
    entity_instance_id: UUID | None = Field(
        None,
        description="已确认的目标实体实例；新客户端必须提供",
    )
    value: object | None = Field(None, description="目标值；新客户端必须提供")
    confirmation_id: UUID | None = Field(None, description="高风险命令的确认 ID")
    # Legacy shape maps only a declared entity definition; it never selects an
    # arbitrary MQTT topic or protocol payload route.
    command: str | None = Field(None, min_length=1, description="旧 RPC 命令名")
    payload: dict = Field(default_factory=dict, description="旧 RPC payload")
    topic: str | None = Field(None, description="旧自定义 MQTT topic（不再执行）")
    qos: int = Field(1, ge=0, le=2, description="旧 MQTT QoS（不再执行）")


@router.post(
    "/devices/{node_id}/rpc",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONTROL_WRITE),
)
async def send_rpc(
    node_id: UUID,
    req: RpcRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200),
    principal: Principal = Depends(principal_for(CONTROL_WRITE)),
    compatibility: ControlCommandCompatibility = Depends(get_control_compatibility),
) -> dict:
    """兼容入口：只映射确认实体实例，绝不执行任意 MQTT 写入。"""
    if req.entity_instance_id is not None:
        command = compatibility.submit_rpc(
            actor=principal.actor,
            node_id=node_id,
            entity_instance_id=req.entity_instance_id,
            value=req.value,
            idempotency_key=idempotency_key,
            confirmation_id=req.confirmation_id,
        )
    elif req.command is not None:
        command = compatibility.submit_legacy_rpc(
            actor=principal.actor,
            node_id=node_id,
            command=req.command,
            payload=req.payload,
            idempotency_key=idempotency_key,
            confirmation_id=req.confirmation_id,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CONTROL_RPC_MIGRATION_REQUIRED",
                "message": "Provide entity_instance_id and value, or a declared legacy command",
                "replacement": "/api/v1/entity-instances/{id}/control-commands",
            },
        )
    if command.status == "rejected":
        raise compatibility_error(command)
    return compatibility_response(command)
