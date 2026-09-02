"""System-admin API for configurable alarm HTTP requests."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.business_security import (
    SYSTEM_MANAGE,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.alarm_http_notifications import (
    AlarmHttpNotifications,
    HttpNotificationDraft,
    HttpNotificationError,
    RequestField,
)
from app.services.identity import Principal


router = APIRouter(prefix="/admin/alarm-http-notifications")
_notifications: AlarmHttpNotifications | None = None


def get_alarm_http_notifications() -> AlarmHttpNotifications:
    global _notifications
    if _notifications is None:
        from app.services.alarm_http_notification_postgres import (
            build_postgres_alarm_http_notifications,
        )

        _notifications = build_postgres_alarm_http_notifications()
    return _notifications


class HttpRequestFieldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(default="", max_length=8192)
    sensitive: bool = False
    clear: bool = False

    def domain(self) -> RequestField:
        return RequestField(self.key, self.value, self.sensitive, self.clear)


class HttpNotificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    url: str = Field(max_length=8192)
    query_params: list[HttpRequestFieldRequest] = Field(
        default_factory=list,
        max_length=50,
    )
    headers: list[HttpRequestFieldRequest] = Field(
        default_factory=list,
        max_length=50,
    )
    content_type: str = Field(min_length=1, max_length=200)
    body_template: str = Field(default="", max_length=65536)
    timeout_seconds: int = Field(default=5, ge=1, le=30)

    def domain(self) -> HttpNotificationDraft:
        return HttpNotificationDraft(
            name=self.name,
            description=self.description,
            method=self.method,
            url=self.url,
            query_params=tuple(field.domain() for field in self.query_params),
            headers=tuple(field.domain() for field in self.headers),
            content_type=self.content_type,
            body_template=self.body_template,
            timeout_seconds=self.timeout_seconds,
        )


def _error(error: HttpNotificationError) -> HTTPException:
    if error.code == "HTTP_NOTIFICATION_NOT_FOUND":
        response_status = status.HTTP_404_NOT_FOUND
    elif error.code in {
        "HTTP_NOTIFICATION_DISABLED",
        "HTTP_NOTIFICATION_NOT_TESTED",
        "HTTP_NOTIFICATION_TEST_STALE",
    }:
        response_status = status.HTTP_409_CONFLICT
    elif error.code in {
        "HTTP_NOTIFICATION_SECRET_KEY_NOT_CONFIGURED",
        "HTTP_NOTIFICATION_PERSISTENCE_UNAVAILABLE",
    }:
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        response_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    return HTTPException(
        status_code=response_status,
        detail={"code": error.code, "message": str(error)},
    )


@router.get("", **protected(SYSTEM_MANAGE))
def list_configs(
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        return notifications.list()
    except HttpNotificationError as error:
        raise _error(error) from error


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
def create_config(
    command: HttpNotificationRequest,
    principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        return notifications.create(command.domain(), principal.actor)
    except HttpNotificationError as error:
        raise _error(error) from error


@router.put(
    "/{config_id}",
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
def update_config(
    config_id: UUID,
    command: HttpNotificationRequest,
    principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        return notifications.update(config_id, command.domain(), principal.actor)
    except HttpNotificationError as error:
        raise _error(error) from error


@router.post(
    "/{config_id}/test",
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
async def test_config(
    config_id: UUID,
    principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        return await notifications.test(config_id, principal.actor)
    except HttpNotificationError as error:
        raise _error(error) from error


@router.post(
    "/{config_id}/enable",
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
def enable_config(
    config_id: UUID,
    principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        return notifications.enable(config_id, principal.actor)
    except HttpNotificationError as error:
        raise _error(error) from error


@router.post(
    "/{config_id}/disable",
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
def disable_config(
    config_id: UUID,
    principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        return notifications.disable(config_id, principal.actor)
    except HttpNotificationError as error:
        raise _error(error) from error


@router.delete(
    "/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=capability_metadata(SYSTEM_MANAGE),
)
def delete_config(
    config_id: UUID,
    principal: Principal = Depends(principal_for(SYSTEM_MANAGE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
) -> Response:
    try:
        notifications.delete(config_id, principal.actor)
    except HttpNotificationError as error:
        raise _error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["get_alarm_http_notifications", "router"]
