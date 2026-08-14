"""实体实例显式主备策略，不扩大安装 Registry 的三操作边界。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.services.entity_instance_registry import EntityInstanceError


@dataclass(frozen=True)
class EntityFailoverState:
    entity_instance_id: UUID
    current_role: str
    switch_count: int
    actor: str | None = None
    reason: str | None = None
    changed_at: datetime | None = None
    audit: tuple[dict[str, Any], ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "entity_instance_id": str(self.entity_instance_id),
            "policy": "manual",
            "current_role": self.current_role,
            "switch_count": self.switch_count,
            "actor": self.actor,
            "reason": self.reason,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
            "audit": list(self.audit),
        }


class EntityFailoverRepository(Protocol):
    def failover_state(self, entity_instance_id: UUID) -> EntityFailoverState | None: ...

    def switch_failover(
        self,
        entity_instance_id: UUID,
        expected_current_role: str,
        target_role: str,
        actor: str,
        reason: str,
    ) -> EntityFailoverState: ...


class EntityFailoverPolicy:
    """读取和执行显式人工切换；安装计划仍只声明/持久化策略。"""

    def __init__(self, repository: EntityFailoverRepository) -> None:
        self._repository = repository

    def state(self, entity_instance_id: UUID) -> EntityFailoverState:
        state = self._repository.failover_state(entity_instance_id)
        if state is None:
            raise EntityInstanceError(
                "ENTITY_FAILOVER_NOT_CONFIGURED",
                "Entity instance has no manual failover policy",
            )
        return state

    def switch(
        self,
        entity_instance_id: UUID,
        *,
        expected_current_role: str,
        target_role: str,
        actor: str,
        reason: str,
    ) -> EntityFailoverState:
        if not reason.strip() or len(reason) > 500:
            raise EntityInstanceError(
                "ENTITY_FAILOVER_REASON_INVALID",
                "Failover reason is required and limited to 500 characters",
            )
        return self._repository.switch_failover(
            entity_instance_id,
            expected_current_role,
            target_role,
            actor,
            reason.strip(),
        )
