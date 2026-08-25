"""统一控制命令的公开 HTTP Adapter。"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.api.business_security import (
    CONTROL_WRITE,
    capability_metadata,
    principal_for,
    protected,
)
from app.api.entity_instances import (
    get_entity_instance_registry,
    get_entity_instance_repository,
    get_entity_instance_runtime,
)
from app.services.control_commands import (
    ControlCommand,
    ControlCommandCompatibility,
    ControlCommandRuntime,
    SubmitControlCommand,
)
from app.services.identity import Principal


router = APIRouter()
_commands: ControlCommandRuntime | None = None
_compatibility: ControlCommandCompatibility | None = None
_automated = None


def get_default_control_commands() -> ControlCommandRuntime:
    global _commands
    if _commands is None:
        from app.services.control_commands import (
            NeuronControlDispatcher,
            PostgresControlCommandRepository,
        )

        _commands = ControlCommandRuntime(
            registry=get_entity_instance_registry(),
            policies=get_entity_instance_repository(),
            readback=get_entity_instance_runtime(),
            dispatcher=NeuronControlDispatcher(),
            repository=PostgresControlCommandRepository(),
        )
    return _commands


def get_automated_control_commands():
    global _automated
    if _automated is None:
        from app.services.automated_control_commands import AutomatedControlCommands

        _automated = AutomatedControlCommands(get_default_control_commands())
    return _automated


class ControlCommandRequest(BaseModel):
    value: object
    confirmation_id: UUID | None = None


def get_control_compatibility() -> ControlCommandCompatibility:
    global _compatibility
    if _compatibility is None:
        from app.services.control_commands import PostgresControlTargetResolver

        _compatibility = ControlCommandCompatibility(
            get_default_control_commands(),
            PostgresControlTargetResolver(),
        )
    return _compatibility


def compatibility_response(command: ControlCommand) -> dict:
    """Return a command, never a false synchronous device-success response."""
    public = command.public_dict()
    public["migration"] = {
        "deprecated": True,
        "replacement": "/api/v1/entity-instances/{id}/control-commands",
    }
    public["links"] = {
        "command": f"/api/v1/control-commands/{command.id}",
    }
    return public


def compatibility_error(command: ControlCommand) -> HTTPException:
    public = compatibility_response(command)
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": public["code"],
            "message": "Legacy control target cannot be executed",
            "command": public,
        },
    )


def _command(
    principal: Principal,
    entity_instance_id: UUID,
    body: ControlCommandRequest,
    idempotency_key: str,
) -> SubmitControlCommand:
    return SubmitControlCommand(
        actor=principal.actor,
        source_type="manual",
        entity_instance_id=entity_instance_id,
        value=body.value,
        idempotency_key=idempotency_key,
        confirmation_id=body.confirmation_id,
        origin_evidence={"actor_role": principal.role},
    )


@router.post(
    "/entity-instances/{entity_instance_id}/control-confirmations",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONTROL_WRITE),
)
async def request_control_confirmation(
    entity_instance_id: UUID,
    body: ControlCommandRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200),
    principal: Principal = Depends(principal_for(CONTROL_WRITE)),
    commands: ControlCommandRuntime = Depends(get_default_control_commands),
) -> dict:
    confirmation = commands.request_confirmation(
        _command(principal, entity_instance_id, body, idempotency_key)
    )
    return {
        "id": str(confirmation.id),
        "expires_at": confirmation.expires_at.isoformat(),
    }


@router.post(
    "/entity-instances/{entity_instance_id}/control-commands",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(CONTROL_WRITE),
)
async def submit_control_command(
    entity_instance_id: UUID,
    body: ControlCommandRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200),
    principal: Principal = Depends(principal_for(CONTROL_WRITE)),
    commands: ControlCommandRuntime = Depends(get_default_control_commands),
) -> dict:
    command = commands.submit(_command(principal, entity_instance_id, body, idempotency_key))
    response = command.public_dict()
    response_status = status.HTTP_201_CREATED
    if command.status == "rejected":
        response_status = status.HTTP_409_CONFLICT
    if response_status != status.HTTP_201_CREATED:
        raise HTTPException(
            status_code=response_status,
            detail={"code": command.code, "message": "Control command was rejected", "command": response},
        )
    return response


@router.get(
    "/control-commands/{command_id}",
    **protected(CONTROL_WRITE),
)
async def get_control_command(
    command_id: UUID,
    commands: ControlCommandRuntime = Depends(get_default_control_commands),
) -> dict:
    try:
        return commands.get(command_id).public_dict()
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONTROL_COMMAND_NOT_FOUND", "message": "Control command was not found"},
        ) from exc


@router.post(
    "/control-commands/{command_id}/reconcile",
    openapi_extra=capability_metadata(CONTROL_WRITE),
)
async def reconcile_control_command(
    command_id: UUID,
    principal: Principal = Depends(principal_for(CONTROL_WRITE)),
    commands: ControlCommandRuntime = Depends(get_default_control_commands),
) -> dict:
    del principal
    try:
        return commands.reconcile(command_id).public_dict()
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONTROL_COMMAND_NOT_FOUND", "message": "Control command was not found"},
        ) from exc
