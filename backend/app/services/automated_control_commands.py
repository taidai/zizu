"""规则与策略进入统一控制命令的唯一入口。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal
from uuid import UUID

from app.services.control_commands import ControlCommand, ControlCommandRuntime, SubmitControlCommand


AutomatedSourceType = Literal["rule", "policy", "strategy"]


@dataclass(frozen=True)
class AutomatedControlCommandRequest:
    """自动化主体触发的一次声明式控制动作。"""

    source_type: AutomatedSourceType
    subject_id: UUID
    subject_version: int
    action_key: str
    entity_instance_id: UUID
    value: object
    trigger_evidence: dict[str, object]
    policy_authorization: str | None = None
    attempt_idempotency_key: str | None = None


class AutomatedControlCommands:
    """把自动化身份、可重放幂等和触发证据藏在一个小接口之后。"""

    def __init__(self, runtime: ControlCommandRuntime) -> None:
        self._runtime = runtime

    def submit(self, request: AutomatedControlCommandRequest) -> ControlCommand:
        if request.source_type not in {"rule", "policy", "strategy"}:
            raise ValueError("Automatic control source must be rule, policy or strategy")
        if request.source_type == "strategy" and (
            request.attempt_idempotency_key is None
            or len(request.attempt_idempotency_key) != 64
            or any(character not in "0123456789abcdef" for character in request.attempt_idempotency_key)
        ):
            raise ValueError("Strategy attempt idempotency key is invalid")
        if request.subject_version < 1:
            raise ValueError("Automatic control subject version must be positive")
        if not request.action_key or len(request.action_key) > 200:
            raise ValueError("Automatic control action key is invalid")
        evidence = _origin_evidence(request)
        return self._runtime.submit(
            SubmitControlCommand(
                actor=f"{request.source_type}:{request.subject_id}",
                source_type=request.source_type,
                entity_instance_id=request.entity_instance_id,
                value=request.value,
                idempotency_key=_idempotency_key(request),
                origin_evidence=evidence,
                policy_authorization=request.policy_authorization,
            )
        )

    def reconcile(self, command_id: UUID) -> ControlCommand:
        """Observe an already-dispatched automatic command without issuing another write."""
        return self._runtime.reconcile(command_id)

    def get(self, command_id: UUID) -> ControlCommand:
        """Read one automatic command's public, persisted state."""
        return self._runtime.get(command_id)


def _origin_evidence(request: AutomatedControlCommandRequest) -> dict[str, object]:
    """Keep the actor, action and replayable trigger proof with the command."""
    return {
        "subject": {
            "type": request.source_type,
            "id": str(request.subject_id),
            "version": request.subject_version,
        },
        "action_key": request.action_key,
        "trigger": _safe_trigger_evidence(request.trigger_evidence),
    }


_UNSAFE_EVIDENCE_FIELDS = frozenset({
    "address", "command", "deviceid", "entity", "entityid", "entityname",
    "group", "mqtt", "neuron", "node", "payload", "qos", "sourcepath",
    "tag", "topic",
})
_UNSAFE_EVIDENCE_PREFIXES = (
    "address", "command", "device", "group", "mqtt", "neuron", "node",
    "payload", "qos", "sourcepath", "tag", "topic",
)


def _safe_trigger_evidence(value: object) -> object:
    """Keep replay proof while preventing physical transport details from escaping."""
    if isinstance(value, dict):
        return {
            str(key): _safe_trigger_evidence(item)
            for key, item in value.items()
            if not _is_unsafe_evidence_field(str(key))
        }
    if isinstance(value, list):
        return [_safe_trigger_evidence(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_trigger_evidence(item) for item in value]
    return value


def _is_unsafe_evidence_field(key: str) -> bool:
    normalized = "".join(character for character in key.lower() if character.isalnum())
    return (
        normalized in _UNSAFE_EVIDENCE_FIELDS
        or normalized.startswith(_UNSAFE_EVIDENCE_PREFIXES)
    )


def _idempotency_key(request: AutomatedControlCommandRequest) -> str:
    if request.source_type == "strategy":
        assert request.attempt_idempotency_key is not None
        return request.attempt_idempotency_key
    evidence = _origin_evidence(request)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "entity_instance_id": str(request.entity_instance_id),
                "value": request.value,
                "origin": evidence,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"{request.source_type}:{request.subject_id}:{request.action_key}:{fingerprint}"
