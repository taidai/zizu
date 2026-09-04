"""Operator-visible alarm HTTP delivery history and manual retry command."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.alarm_http_notifications import get_alarm_http_notifications
from app.api.business_security import (
    CONFIGURATION_WRITE,
    RUNTIME_READ,
    capability_metadata,
    principal_for,
    protected,
)
from app.services.alarm_http_notifications import (
    AlarmHttpNotifications,
    HttpNotificationError,
)
from app.services.identity import Principal


router = APIRouter(prefix="/alarms/notification-deliveries")


class DeleteDeliveriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_ids: list[UUID] = Field(min_length=1, max_length=200)


def _error(error: HttpNotificationError) -> HTTPException:
    if error.code in {
        "HTTP_NOTIFICATION_NOT_FOUND",
        "HTTP_NOTIFICATION_DELIVERY_NOT_FOUND",
    }:
        response_status = status.HTTP_404_NOT_FOUND
    elif error.code in {
        "HTTP_NOTIFICATION_RETRY_NOT_ALLOWED",
        "HTTP_NOTIFICATION_IDEMPOTENCY_KEY_REUSED",
        "HTTP_NOTIFICATION_DELIVERY_NOT_TERMINAL",
    }:
        response_status = status.HTTP_409_CONFLICT
    elif error.code == "HTTP_NOTIFICATION_PERSISTENCE_UNAVAILABLE":
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        response_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    return HTTPException(
        status_code=response_status,
        detail={"code": error.code, "message": str(error)},
    )


@router.get("", **protected(RUNTIME_READ))
def list_deliveries(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        return notifications.list_deliveries(page=page, page_size=page_size)
    except HttpNotificationError as error:
        raise _error(error) from error


@router.post(
    "/deletions",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
def delete_deliveries(
    request: DeleteDeliveriesRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        deleted = notifications.delete_deliveries(
            tuple(request.delivery_ids), principal.actor
        )
        return {"deleted": deleted}
    except HttpNotificationError as error:
        raise _error(error) from error


@router.delete(
    "/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
def delete_delivery(
    notification_id: UUID,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        notifications.delete_deliveries((notification_id,), principal.actor)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HttpNotificationError as error:
        raise _error(error) from error


@router.post(
    "/{notification_id}/retry",
    openapi_extra=capability_metadata(CONFIGURATION_WRITE),
)
def retry_delivery(
    notification_id: UUID,
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
    ),
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    notifications: AlarmHttpNotifications = Depends(get_alarm_http_notifications),
):
    try:
        return notifications.retry(
            notification_id,
            principal.actor,
            idempotency_key,
        )
    except HttpNotificationError as error:
        raise _error(error) from error


__all__ = ["router"]
