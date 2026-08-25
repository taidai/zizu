"""实体实例的统一控制命令运行时。

本模块是人工、规则、策略与兼容入口共享的控制安全边界；设备写入 Adapter 只能接收
已经验证的 ``DispatchControlCommand``，不能自行解释 HTTP 请求或物理地址。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Callable, Protocol
from uuid import UUID, uuid4


from app.services.entity_instance_registry import ResolvedEntitySource
from app.services.entity_instance_runtime import EntityInstanceObservation


TERMINAL_STATUSES = frozenset({"rejected", "timeout", "failed", "mismatch", "readback_confirmed"})


@dataclass(frozen=True)
class ControlInterlock:
    definition_id: str
    equals: object


@dataclass(frozen=True)
class ControlPolicy:
    minimum: float | None
    maximum: float | None
    cooldown_seconds: int
    readback_definition: str
    tolerance: float | None
    timeout_seconds: int
    interlocks: tuple[ControlInterlock, ...] = ()
    high_risk: bool = False


@dataclass(frozen=True)
class SubmitControlCommand:
    actor: str
    source_type: str
    entity_instance_id: UUID | None
    value: object
    idempotency_key: str
    confirmation_id: UUID | None = None
    capability: str = "control.write"
    origin_evidence: dict[str, object] = field(default_factory=dict)
    # An opaque, process-local proof emitted only by the policy runtime.  It is
    # deliberately not persisted or exposed: request evidence is audit data,
    # never an authority to bypass a high-risk confirmation.
    policy_authorization: str | None = None


@dataclass(frozen=True)
class ControlConfirmation:
    id: UUID
    actor: str
    request_digest: str
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True)
class DispatchControlCommand:
    command_id: UUID
    entity_instance_id: UUID
    tag_id: UUID
    value: object
    data_type: str


@dataclass(frozen=True)
class ControlCommand:
    id: UUID
    actor: str
    source_type: str
    capability: str
    entity_instance_id: UUID | None
    expected_value: object
    data_type: str
    tolerance: float | None
    policy_snapshot: dict[str, object]
    origin_evidence: dict[str, object]
    timeout_at: datetime | None
    status: str
    code: str
    idempotency_key: str
    request_digest: str
    created_at: datetime
    audit_event_id: UUID | None = None
    dispatched_at: datetime | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "actor": self.actor,
            "source_type": self.source_type,
            "capability": self.capability,
            "entity_instance_id": str(self.entity_instance_id) if self.entity_instance_id else None,
            "expected_value": self.expected_value,
            "data_type": self.data_type,
            "tolerance": self.tolerance,
            "policy_snapshot": self.policy_snapshot,
            "origin_evidence": self.origin_evidence,
            "timeout_at": self.timeout_at.isoformat() if self.timeout_at else None,
            "status": self.status,
            "code": self.code,
            "created_at": self.created_at.isoformat(),
            "audit_event_id": str(self.audit_event_id) if self.audit_event_id else None,
            "dispatched_at": self.dispatched_at.isoformat() if self.dispatched_at else None,
        }


@dataclass(frozen=True)
class ControlCommandEvent:
    command_id: UUID
    to_status: str
    code: str
    at: datetime


class ControlIdempotencyConflict(RuntimeError):
    """A concurrent request claimed the actor/key pair with different content."""


class ControlCommandRepository(Protocol):
    def idempotent(self, actor: str, key: str) -> ControlCommand | None: ...
    def save(self, command: ControlCommand, *, idempotent: bool) -> ControlCommand: ...
    def update(self, command: ControlCommand, *, occurred_at: datetime) -> ControlCommand: ...
    def get(self, command_id: UUID) -> ControlCommand | None: ...
    def events(self, command_id: UUID) -> tuple[ControlCommandEvent, ...]: ...
    def inflight(self) -> tuple[ControlCommand, ...]: ...
    def reserve_cooldown(
        self,
        entity_instance_id: UUID,
        command_id: UUID,
        until: datetime,
        now: datetime,
    ) -> bool: ...
    def save_confirmation(self, confirmation: ControlConfirmation) -> None: ...
    def consume_confirmation(
        self,
        confirmation_id: UUID,
        *,
        actor: str,
        request_digest: str,
        now: datetime,
    ) -> bool: ...


class ControlPolicyCatalog(Protocol):
    def control_policy(self, entity_instance_id: UUID) -> ControlPolicy | None: ...
    def entity_instance_for_definition(
        self,
        node_id: UUID,
        definition_id: str,
    ) -> UUID | None: ...


class EntityInstanceReader(Protocol):
    def read(self, entity_instance_id: UUID) -> EntityInstanceObservation: ...


class ControlDispatcher(Protocol):
    def dispatch(self, request: DispatchControlCommand) -> None: ...


class ControlTargetResolver(Protocol):
    """Resolve only a confirmed entity instance from a legacy control address."""

    def neuron_target(self, *, node: str, group: str, tag: str) -> UUID | None: ...

    def rpc_target(
        self,
        *,
        node_id: UUID,
        entity_instance_id: UUID,
    ) -> UUID | None: ...

    def legacy_rpc_target(self, *, node_id: UUID, command: str) -> UUID | None: ...

    def legacy_entity_target(self, *, entity_id: UUID) -> UUID | None: ...


class PostgresControlTargetResolver:
    """Compatibility adapter; never turns an arbitrary address into a write."""

    @staticmethod
    def _connection():
        from app.services.telemetry_store import get_connection

        return get_connection()

    def neuron_target(self, *, node: str, group: str, tag: str) -> UUID | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ei.id, n.name, t.name, t.source_path
                FROM t_entity_instances ei
                JOIN t_l2_control_bindings binding
                  ON binding.entity_instance_id = ei.id
                JOIN t_tags t ON t.id = binding.l0_tag_id AND t.enabled = TRUE
                JOIN t_nodes n ON n.id = t.node_id AND n.enabled = TRUE
                WHERE ei.active = TRUE
                  AND ei.node_id = n.id
                  AND n.name = %s
                  AND t.name = %s
                  AND (t.source_type IS NULL OR lower(t.source_type) = 'neuron')
                """,
                (node, tag),
            )
            matches = [
                row[0]
                for row in cur.fetchall()
                if _neuron_target(row[1], row[2], row[3]) == (node, group, tag)
            ]
        return matches[0] if len(matches) == 1 else None

    def rpc_target(
        self,
        *,
        node_id: UUID,
        entity_instance_id: UUID,
    ) -> UUID | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT binding.entity_instance_id
                FROM t_l2_control_bindings binding
                JOIN t_entity_instances ei ON ei.id = binding.entity_instance_id
                JOIN t_tags tag ON tag.id = binding.l0_tag_id AND tag.enabled = TRUE
                JOIN t_nodes node ON node.id = tag.node_id AND node.enabled = TRUE
                WHERE binding.entity_instance_id = %s
                  AND ei.active = TRUE
                  AND ei.node_id = node.id
                  AND node.id = %s
                  AND (tag.source_type IS NULL OR lower(tag.source_type) = 'neuron')
                """,
                (entity_instance_id, node_id),
            )
            rows = cur.fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def legacy_rpc_target(self, *, node_id: UUID, command: str) -> UUID | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT binding.entity_instance_id
                FROM t_l2_control_bindings binding
                JOIN t_entity_instances ei ON ei.id = binding.entity_instance_id
                JOIN t_tags tag ON tag.id = binding.l0_tag_id AND tag.enabled = TRUE
                JOIN t_nodes node ON node.id = tag.node_id AND node.enabled = TRUE
                WHERE ei.definition_id = %s
                  AND ei.active = TRUE
                  AND ei.node_id = node.id
                  AND node.id = %s
                  AND (tag.source_type IS NULL OR lower(tag.source_type) = 'neuron')
                """,
                (command, node_id),
            )
            rows = cur.fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def legacy_entity_target(self, *, entity_id: UUID) -> UUID | None:
        """Accept only an active L2 entity with an explicit physical control binding."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT binding.entity_instance_id
                FROM t_l2_control_bindings binding
                JOIN t_entity_instances entity
                  ON entity.id=binding.entity_instance_id AND entity.active=TRUE
                JOIN t_tags tag
                  ON tag.id=binding.l0_tag_id AND tag.enabled=TRUE
                WHERE binding.entity_instance_id=%s
                """,
                (entity_id,),
            )
            rows = cur.fetchall()
        return rows[0][0] if len(rows) == 1 else None


class InMemoryControlTargetResolver:
    """Compatibility resolver for the public HTTP seam."""

    def __init__(self) -> None:
        self._neuron: dict[tuple[str, str, str], UUID] = {}
        self._rpc: dict[tuple[UUID, UUID], UUID] = {}
        self._legacy_rpc: dict[tuple[UUID, str], UUID] = {}
        self._legacy_entities: dict[UUID, UUID] = {}

    def register_neuron(
        self,
        *,
        node: str,
        group: str,
        tag: str,
        entity_instance_id: UUID,
    ) -> None:
        self._neuron[(node, group, tag)] = entity_instance_id

    def register_rpc(self, node_id: UUID, entity_instance_id: UUID) -> None:
        self._rpc[(node_id, entity_instance_id)] = entity_instance_id

    def register_legacy_rpc(
        self,
        *,
        node_id: UUID,
        command: str,
        entity_instance_id: UUID,
    ) -> None:
        self._legacy_rpc[(node_id, command)] = entity_instance_id

    def register_legacy_entity(
        self,
        *,
        entity_id: UUID,
        entity_instance_id: UUID,
    ) -> None:
        self._legacy_entities[entity_id] = entity_instance_id

    def neuron_target(self, *, node: str, group: str, tag: str) -> UUID | None:
        return self._neuron.get((node, group, tag))

    def rpc_target(
        self,
        *,
        node_id: UUID,
        entity_instance_id: UUID,
    ) -> UUID | None:
        return self._rpc.get((node_id, entity_instance_id))

    def legacy_rpc_target(self, *, node_id: UUID, command: str) -> UUID | None:
        return self._legacy_rpc.get((node_id, command))

    def legacy_entity_target(self, *, entity_id: UUID) -> UUID | None:
        return self._legacy_entities.get(entity_id)


class NeuronControlDispatcher:
    """仅执行已验证命令的生产 Neuron Adapter。"""

    def dispatch(self, request: DispatchControlCommand) -> None:
        from app.core.config import settings
        from app.services.neuron_client import NeuronClient, NeuronConfig
        from app.services.telemetry_store import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.name, t.name, t.source_type, t.source_path
                FROM t_tags t JOIN t_nodes n ON n.id = t.node_id
                WHERE t.id = %s AND t.enabled = TRUE AND n.enabled = TRUE
                """,
                (request.tag_id,),
            )
            row = cur.fetchone()
        if row is None or (row[2] or "NEURON").casefold() != "neuron":
            raise RuntimeError("Confirmed command source is not an enabled Neuron tag")
        node_name, tag_name, _source_type, source_path = row
        node_name, group_name, neuron_tag_name = _neuron_target(node_name, tag_name, source_path)
        client = NeuronClient(
            NeuronConfig(
                url=settings.neuron_api_url,
                username=settings.neuron_username,
                password=settings.neuron_password,
                deployment_mode=settings.deployment_mode,
                allow_insecure_dev_secrets=settings.allow_insecure_dev_secrets,
            )
        )
        result = client.write_tag(node_name, group_name, neuron_tag_name, request.value)
        if isinstance(result, dict) and result.get("error") not in (None, 0):
            raise RuntimeError("Neuron rejected the control command")


class InMemoryControlCommandRepository:
    """公开辅助测试 Adapter；重建运行时仍复用同一持久状态。"""

    def __init__(self) -> None:
        self._commands: dict[UUID, ControlCommand] = {}
        self._idempotency: dict[tuple[str, str], UUID] = {}
        self._events: dict[UUID, list[ControlCommandEvent]] = {}
        self._cooldowns: dict[UUID, datetime] = {}
        self._confirmations: dict[UUID, ControlConfirmation] = {}

    def idempotent(self, actor: str, key: str) -> ControlCommand | None:
        command_id = self._idempotency.get((actor, key))
        return self._commands.get(command_id) if command_id else None

    def save(self, command: ControlCommand, *, idempotent: bool) -> ControlCommand:
        if idempotent:
            existing = self.idempotent(command.actor, command.idempotency_key)
            if existing is not None:
                if existing.request_digest == command.request_digest:
                    return existing
                raise ControlIdempotencyConflict()
        # Keep the test adapter semantically aligned with the production
        # repository: every persisted command carries immutable audit evidence.
        command = replace(command, audit_event_id=command.audit_event_id or uuid4())
        self._commands[command.id] = command
        if idempotent:
            self._idempotency[(command.actor, command.idempotency_key)] = command.id
        self._events.setdefault(command.id, []).append(
            ControlCommandEvent(command.id, command.status, command.code, command.created_at)
        )
        return command

    def update(self, command: ControlCommand, *, occurred_at: datetime) -> ControlCommand:
        previous = self._commands[command.id]
        if previous.status in TERMINAL_STATUSES:
            return previous
        self._commands[command.id] = command
        self._events[command.id].append(
            ControlCommandEvent(command.id, command.status, command.code, occurred_at)
        )
        return command

    def get(self, command_id: UUID) -> ControlCommand | None:
        return self._commands.get(command_id)

    def events(self, command_id: UUID) -> tuple[ControlCommandEvent, ...]:
        return tuple(self._events.get(command_id, ()))

    def inflight(self) -> tuple[ControlCommand, ...]:
        return tuple(
            command
            for command in self._commands.values()
            if command.status in {"accepted", "validated", "dispatched"}
        )

    def reserve_cooldown(
        self,
        entity_instance_id: UUID,
        command_id: UUID,
        until: datetime,
        now: datetime,
    ) -> bool:
        del command_id
        existing = self._cooldowns.get(entity_instance_id)
        if existing is not None and existing > now:
            return False
        self._cooldowns[entity_instance_id] = until
        return True

    def save_confirmation(self, confirmation: ControlConfirmation) -> None:
        self._confirmations[confirmation.id] = confirmation

    def consume_confirmation(
        self,
        confirmation_id: UUID,
        *,
        actor: str,
        request_digest: str,
        now: datetime,
    ) -> bool:
        confirmation = self._confirmations.get(confirmation_id)
        if (
            confirmation is None
            or confirmation.consumed_at is not None
            or confirmation.expires_at <= now
            or confirmation.actor != actor
            or confirmation.request_digest != request_digest
        ):
            return False
        self._confirmations[confirmation_id] = replace(confirmation, consumed_at=now)
        return True


class PostgresControlCommandRepository:
    """命令状态、幂等、冷却和确认的 PostgreSQL Adapter。"""

    @staticmethod
    def _connection():
        from app.services.telemetry_store import get_connection

        return get_connection()

    def idempotent(self, actor: str, key: str) -> ControlCommand | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT """ + _command_columns("command.") + """
                FROM t_control_command_idempotency idem
                JOIN t_control_commands command ON command.id = idem.command_id
                WHERE idem.actor = %s AND idem.idempotency_key = %s
                """,
                (actor, key),
            )
            row = cur.fetchone()
        return _command_from_row(row) if row else None

    def save(self, command: ControlCommand, *, idempotent: bool) -> ControlCommand:
        with self._connection() as conn, conn.cursor() as cur:
            if idempotent:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (command.actor, command.idempotency_key),
                )
                cur.execute(
                    """
                    SELECT """ + _command_columns("command.") + """
                    FROM t_control_command_idempotency idem
                    JOIN t_control_commands command ON command.id = idem.command_id
                    WHERE idem.actor = %s AND idem.idempotency_key = %s
                    """,
                    (command.actor, command.idempotency_key),
                )
                existing = cur.fetchone()
                if existing is not None:
                    conn.commit()
                    saved = _command_from_row(existing)
                    if saved.request_digest == command.request_digest:
                        return saved
                    raise ControlIdempotencyConflict()
            audit_event_id = self._append_audit(cur, command)
            command = replace(command, audit_event_id=audit_event_id)
            cur.execute(
                """
                INSERT INTO t_control_commands
                  (id, actor, source_type, capability, entity_instance_id, expected_value, data_type,
                   tolerance, policy_snapshot, origin_evidence, timeout_at, status, code, idempotency_key, request_digest, audit_event_id,
                   created_at, dispatched_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                _command_values(command),
            )
            self._append_event(cur, command, command.created_at, audit_event_id)
            if idempotent:
                cur.execute(
                    """
                    INSERT INTO t_control_command_idempotency
                      (actor, idempotency_key, request_digest, command_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (command.actor, command.idempotency_key, command.request_digest, command.id),
                )
            conn.commit()
        return command

    def update(self, command: ControlCommand, *, occurred_at: datetime) -> ControlCommand:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE t_control_commands
                SET status = %s, code = %s, dispatched_at = %s
                WHERE id = %s
                  AND status NOT IN ('readback_confirmed', 'rejected', 'timeout', 'failed', 'mismatch')
                RETURNING """ + _COMMAND_COLUMNS,
                (command.status, command.code, command.dispatched_at, command.id),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT " + _COMMAND_COLUMNS + " FROM t_control_commands WHERE id = %s", (command.id,))
                row = cur.fetchone()
                conn.commit()
                return _command_from_row(row)
            saved = _command_from_row(row)
            audit_event_id = self._append_audit(cur, saved)
            self._append_event(cur, saved, occurred_at, audit_event_id)
            conn.commit()
        return saved

    def get(self, command_id: UUID) -> ControlCommand | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT " + _COMMAND_COLUMNS + " FROM t_control_commands WHERE id = %s", (command_id,))
            row = cur.fetchone()
        return _command_from_row(row) if row else None

    def events(self, command_id: UUID) -> tuple[ControlCommandEvent, ...]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT command_id, to_status, code, occurred_at FROM t_control_command_events WHERE command_id = %s ORDER BY occurred_at, id",
                (command_id,),
            )
            return tuple(ControlCommandEvent(*row) for row in cur.fetchall())

    def inflight(self) -> tuple[ControlCommand, ...]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT " + _COMMAND_COLUMNS + " FROM t_control_commands "
                "WHERE status IN ('accepted', 'validated', 'dispatched') ORDER BY created_at, id"
            )
            rows = cur.fetchall()
        return tuple(_command_from_row(row) for row in rows)

    def reserve_cooldown(
        self,
        entity_instance_id: UUID,
        command_id: UUID,
        until: datetime,
        now: datetime,
    ) -> bool:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM t_entity_instances WHERE id = %s FOR UPDATE", (entity_instance_id,))
            cur.execute(
                "SELECT until_at FROM t_control_command_cooldowns WHERE entity_instance_id = %s FOR UPDATE",
                (entity_instance_id,),
            )
            row = cur.fetchone()
            if row is not None and row[0] > now:
                conn.commit()
                return False
            cur.execute(
                """
                INSERT INTO t_control_command_cooldowns (entity_instance_id, command_id, until_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (entity_instance_id) DO UPDATE
                SET command_id = EXCLUDED.command_id, until_at = EXCLUDED.until_at
                """,
                (entity_instance_id, command_id, until),
            )
            conn.commit()
        return True

    def save_confirmation(self, confirmation: ControlConfirmation) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_control_confirmations (id, actor, request_digest, expires_at) VALUES (%s, %s, %s, %s)",
                (confirmation.id, confirmation.actor, confirmation.request_digest, confirmation.expires_at),
            )
            conn.commit()

    def consume_confirmation(
        self,
        confirmation_id: UUID,
        *,
        actor: str,
        request_digest: str,
        now: datetime,
    ) -> bool:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE t_control_confirmations
                SET consumed_at = %s
                WHERE id = %s AND actor = %s AND request_digest = %s
                  AND consumed_at IS NULL AND expires_at > %s
                """,
                (now, confirmation_id, actor, request_digest, now),
            )
            consumed = cur.rowcount == 1
            conn.commit()
        return consumed

    @staticmethod
    def _append_audit(cur, command: ControlCommand) -> UUID:
        audit_event_id = uuid4()
        cur.execute(
            """
            INSERT INTO t_audit_events
              (id, event, outcome, reason, actor, target, details)
            VALUES (%s, 'control.command', %s, %s, %s, %s, %s::jsonb)
            """,
            (
                audit_event_id,
                command.status,
                command.code,
                command.actor,
                (
                    f"entity-instance:{command.entity_instance_id}"
                    if command.entity_instance_id
                    else "legacy-control-target:unresolved"
                ),
                json.dumps(
                    {
                        "command_id": str(command.id),
                        "source_type": command.source_type,
                        "capability": command.capability,
                        "origin_evidence": command.origin_evidence,
                    }
                ),
            ),
        )
        return audit_event_id

    @staticmethod
    def _append_event(
        cur,
        command: ControlCommand,
        occurred_at: datetime,
        audit_event_id: UUID,
    ) -> None:
        cur.execute(
            """
            INSERT INTO t_control_command_events
              (id, command_id, audit_event_id, to_status, code, occurred_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (uuid4(), command.id, audit_event_id, command.status, command.code, occurred_at),
        )


class ControlCommandRuntime:
    """将实体实例控制请求收口为持久状态机。"""

    def __init__(
        self,
        *,
        registry: Any,
        policies: ControlPolicyCatalog,
        readback: EntityInstanceReader,
        dispatcher: ControlDispatcher,
        repository: ControlCommandRepository,
        clock: Callable[[], datetime] | None = None,
        policy_high_risk_authorizer: Callable[[SubmitControlCommand], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._policies = policies
        self._readback = readback
        self._dispatcher = dispatcher
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._policy_high_risk_authorizer = policy_high_risk_authorizer

    def request_confirmation(self, request: SubmitControlCommand) -> ControlConfirmation:
        now = self._now()
        policy = (
            _control_policy(self._policies.control_policy(request.entity_instance_id))
            if request.entity_instance_id is not None
            else None
        )
        confirmation = ControlConfirmation(
            id=uuid4(),
            actor=request.actor,
            request_digest=_confirmation_digest(request, policy),
            expires_at=now + timedelta(seconds=60),
        )
        self._repository.save_confirmation(confirmation)
        return confirmation

    def submit(self, request: SubmitControlCommand) -> ControlCommand:
        now = self._now()
        digest = _request_digest(request)
        existing = self._repository.idempotent(request.actor, request.idempotency_key)
        if existing is not None:
            if existing.request_digest == digest:
                return existing
            return self._reject(request, digest, now, "IDEMPOTENCY_KEY_REUSED", reserve_key=False)
        if not isinstance(request.idempotency_key, str) or not request.idempotency_key.strip():
            return self._reject(request, digest, now, "CONTROL_IDEMPOTENCY_KEY_INVALID", reserve_key=False)
        if request.entity_instance_id is None:
            return self._reject(
                request,
                digest,
                now,
                (
                    "CONTROL_COMPATIBILITY_TARGET_UNRESOLVED"
                    if request.source_type == "compatibility"
                    else "CONTROL_ENTITY_UNAVAILABLE"
                ),
                reserve_key=True,
            )

        try:
            source: ResolvedEntitySource = self._registry.resolve(request.entity_instance_id)
        except Exception:
            return self._reject(
                request,
                digest,
                now,
                (
                    "CONTROL_COMPATIBILITY_TARGET_UNRESOLVED"
                    if request.source_type == "compatibility"
                    else "CONTROL_ENTITY_UNAVAILABLE"
                ),
                reserve_key=True,
            )
        policy = _control_policy(self._policies.control_policy(request.entity_instance_id))
        if policy is None or source.direction not in {"W", "RW"}:
            return self._reject(request, digest, now, "CONTROL_NOT_CONFIGURED", reserve_key=True, data_type=source.data_type)
        invalid_code = _validate_value(request.value, source.data_type, policy)
        if invalid_code:
            return self._reject(request, digest, now, invalid_code, reserve_key=True, data_type=source.data_type)
        if policy.high_risk:
            if (
                self._policy_high_risk_authorizer is not None
                and self._policy_high_risk_authorizer(request)
            ):
                pass
            elif request.confirmation_id is None:
                return self._reject(request, digest, now, "CONTROL_CONFIRMATION_REQUIRED", reserve_key=False, data_type=source.data_type)
            elif not self._repository.consume_confirmation(
                request.confirmation_id,
                actor=request.actor,
                request_digest=_confirmation_digest(request, policy),
                now=now,
            ):
                return self._reject(request, digest, now, "CONTROL_CONFIRMATION_INVALID", reserve_key=True, data_type=source.data_type)
        interlock_code = self._validate_interlocks(source, policy)
        if interlock_code:
            return self._reject(request, digest, now, interlock_code, reserve_key=True, data_type=source.data_type)

        proposed_id = uuid4()
        command = ControlCommand(
            id=proposed_id, actor=request.actor, source_type=request.source_type,
            capability=request.capability,
            entity_instance_id=request.entity_instance_id, expected_value=request.value,
            data_type=source.data_type, tolerance=policy.tolerance,
            policy_snapshot=_policy_snapshot(policy),
            origin_evidence=request.origin_evidence,
            timeout_at=now + timedelta(seconds=policy.timeout_seconds),
            status="accepted", code="CONTROL_ACCEPTED", idempotency_key=request.idempotency_key,
            request_digest=digest, created_at=now,
        )
        try:
            command = self._repository.save(command, idempotent=True)
        except ControlIdempotencyConflict:
            return self._reject(
                request, digest, now, "IDEMPOTENCY_KEY_REUSED", reserve_key=False,
                data_type=source.data_type,
            )
        if command.id != proposed_id:
            return command
        command = self._transition(command, "validated", "CONTROL_VALIDATED", at=now)
        if not self._repository.reserve_cooldown(
            command.entity_instance_id,
            command.id,
            now + timedelta(seconds=policy.cooldown_seconds),
            now,
        ):
            return self._transition(command, "rejected", "CONTROL_COOLDOWN_ACTIVE", at=now)
        try:
            if source.control_tag_id is None:
                return self._transition(
                    command,
                    "rejected",
                    "CONTROL_TARGET_UNMAPPED",
                    at=now,
                )
            self._dispatcher.dispatch(
                DispatchControlCommand(
                    command.id,
                    command.entity_instance_id,
                    source.control_tag_id,
                    request.value,
                    source.data_type,
                )
            )
        except Exception:
            return self._transition(command, "failed", "CONTROL_DISPATCH_FAILED", at=now)
        command = self._transition(command, "dispatched", "CONTROL_DISPATCHED", at=now, dispatched_at=now)
        return self.reconcile(command.id)

    def reject_unresolved_compatibility_target(
        self,
        *,
        actor: str,
        value: object,
        idempotency_key: str,
        origin_evidence: dict[str, object] | None = None,
    ) -> ControlCommand:
        """Persist an unmappable legacy request without inventing an entity ID."""
        now = self._now()
        request = SubmitControlCommand(
            actor=actor,
            source_type="compatibility",
            entity_instance_id=None,
            value=value,
            idempotency_key=idempotency_key,
            origin_evidence=origin_evidence or {},
        )
        digest = _request_digest(request)
        existing = self._repository.idempotent(actor, idempotency_key)
        if existing is not None:
            if existing.request_digest == digest:
                return existing
            return self._reject(request, digest, now, "IDEMPOTENCY_KEY_REUSED", reserve_key=False)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return self._reject(request, digest, now, "CONTROL_IDEMPOTENCY_KEY_INVALID", reserve_key=False)
        return self._reject(
            request,
            digest,
            now,
            "CONTROL_COMPATIBILITY_TARGET_UNRESOLVED",
            reserve_key=True,
        )

    def reconcile(self, command_id: UUID) -> ControlCommand:
        command = self.get(command_id)
        if command.status in TERMINAL_STATUSES:
            return command
        now = self._now()
        if command.status in {"accepted", "validated"}:
            return self._transition(
                command,
                "failed",
                "CONTROL_DISPATCH_INTERRUPTED",
                at=now,
            )
        if command.timeout_at is not None and now >= command.timeout_at:
            return self._transition(command, "timeout", "CONTROL_READBACK_TIMEOUT", at=now)
        try:
            source = self._registry.resolve(command.entity_instance_id)
        except Exception:
            return self._transition(command, "failed", "CONTROL_ENTITY_UNAVAILABLE", at=now)
        readback_id = self._policies.entity_instance_for_definition(
            source.node_id, str(command.policy_snapshot["readback_definition"])
        )
        if readback_id is None:
            return self._transition(command, "failed", "CONTROL_READBACK_UNAVAILABLE", at=now)
        try:
            observation = self._readback.read(readback_id)
        except Exception:
            return command
        if command.dispatched_at and observation.observed_at < command.dispatched_at:
            return command
        if _matches(command.expected_value, observation.value, command.data_type, command.tolerance):
            return self._transition(command, "readback_confirmed", "CONTROL_READBACK_CONFIRMED", at=now)
        return self._transition(command, "mismatch", "CONTROL_READBACK_MISMATCH", at=now)

    def get(self, command_id: UUID) -> ControlCommand:
        command = self._repository.get(command_id)
        if command is None:
            raise KeyError(command_id)
        return command

    def set_policy_high_risk_authorizer(
        self,
        authorizer: Callable[[SubmitControlCommand], bool],
    ) -> None:
        """Install the server-owned policy verifier after policy runtime wiring."""
        self._policy_high_risk_authorizer = authorizer

    def recover(self) -> tuple[ControlCommand, ...]:
        """Resume persisted in-flight commands without ever repeating a write."""
        return tuple(self.reconcile(command.id) for command in self._repository.inflight())

    def _validate_interlocks(self, source: ResolvedEntitySource, policy: ControlPolicy) -> str | None:
        for interlock in policy.interlocks:
            entity_id = self._policies.entity_instance_for_definition(
                source.node_id, interlock.definition_id
            )
            if entity_id is None:
                return "CONTROL_INTERLOCK_UNAVAILABLE"
            try:
                observation = self._readback.read(entity_id)
            except Exception:
                return "CONTROL_INTERLOCK_UNAVAILABLE"
            if not observation.fresh or not observation.quality_good:
                return "CONTROL_INTERLOCK_UNAVAILABLE"
            if observation.value != interlock.equals:
                return "CONTROL_INTERLOCK_UNSATISFIED"
        return None

    def _reject(
        self,
        request: SubmitControlCommand,
        digest: str,
        now: datetime,
        code: str,
        *,
        reserve_key: bool,
        data_type: str = "UNKNOWN",
    ) -> ControlCommand:
        normalized_type = data_type if data_type in {"FLOAT", "INT", "BOOL", "STRING", "ENUM"} else "STRING"
        command = ControlCommand(
            id=uuid4(), actor=request.actor, source_type=request.source_type,
            capability=request.capability,
            entity_instance_id=request.entity_instance_id, expected_value=request.value,
            data_type=normalized_type, tolerance=None, policy_snapshot={},
            origin_evidence=request.origin_evidence, timeout_at=None, status="rejected",
            code=code, idempotency_key=request.idempotency_key, request_digest=digest,
            created_at=now,
        )
        try:
            return self._repository.save(command, idempotent=reserve_key)
        except ControlIdempotencyConflict:
            # A concurrent valid command owns the key. Persist this rejection
            # without attempting to reserve the same idempotency pair again.
            return self._repository.save(
                replace(command, id=uuid4(), code="IDEMPOTENCY_KEY_REUSED"),
                idempotent=False,
            )

    def _transition(
        self,
        command: ControlCommand,
        status: str,
        code: str,
        *,
        at: datetime,
        dispatched_at: datetime | None = None,
    ) -> ControlCommand:
        return self._repository.update(
            replace(command, status=status, code=code, dispatched_at=dispatched_at or command.dispatched_at),
            occurred_at=at,
        )

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class ControlCommandCompatibility:
    """将遗留 Neuron/RPC 入口压缩为唯一的命令运行时接口。"""

    def __init__(
        self,
        runtime: ControlCommandRuntime,
        targets: ControlTargetResolver,
    ) -> None:
        self._runtime = runtime
        self._targets = targets

    def submit_neuron(
        self,
        *,
        actor: str,
        node: str,
        group: str,
        tag: str,
        value: object,
        idempotency_key: str,
        confirmation_id: UUID | None = None,
    ) -> ControlCommand:
        target = self._targets.neuron_target(node=node, group=group, tag=tag)
        return self._submit(
            actor=actor,
            entity_instance_id=target,
            value=value,
            idempotency_key=idempotency_key,
            confirmation_id=confirmation_id,
        )

    def submit_rpc(
        self,
        *,
        actor: str,
        node_id: UUID,
        entity_instance_id: UUID,
        value: object,
        idempotency_key: str,
        confirmation_id: UUID | None = None,
    ) -> ControlCommand:
        target = self._targets.rpc_target(
            node_id=node_id,
            entity_instance_id=entity_instance_id,
        )
        return self._submit(
            actor=actor,
            entity_instance_id=target,
            value=value,
            idempotency_key=idempotency_key,
            confirmation_id=confirmation_id,
        )

    def submit_legacy_rpc(
        self,
        *,
        actor: str,
        node_id: UUID,
        command: str,
        payload: dict[str, object],
        idempotency_key: str,
        confirmation_id: UUID | None = None,
    ) -> ControlCommand:
        """Map only a declared entity definition; never use MQTT topic routing."""
        target = self._targets.legacy_rpc_target(node_id=node_id, command=command)
        return self._submit(
            actor=actor,
            entity_instance_id=target,
            value=payload.get("value"),
            idempotency_key=idempotency_key,
            confirmation_id=confirmation_id,
        )

    def submit_legacy_entity(
        self,
        *,
        actor: str,
        entity_id: UUID,
        value: object,
        idempotency_key: str,
        confirmation_id: UUID | None = None,
    ) -> ControlCommand:
        """Accept a global-entity compatibility request only when its migration is unique."""
        target = self._targets.legacy_entity_target(entity_id=entity_id)
        return self._submit(
            actor=actor,
            entity_instance_id=target,
            value=value,
            idempotency_key=idempotency_key,
            confirmation_id=confirmation_id,
            origin_evidence={"compatibility": {"legacy_entity_id": str(entity_id)}},
        )

    def _submit(
        self,
        *,
        actor: str,
        entity_instance_id: UUID | None,
        value: object,
        idempotency_key: str,
        confirmation_id: UUID | None,
        origin_evidence: dict[str, object] | None = None,
    ) -> ControlCommand:
        if entity_instance_id is None:
            return self._runtime.reject_unresolved_compatibility_target(
                actor=actor,
                value=value,
                idempotency_key=idempotency_key,
                origin_evidence=origin_evidence or {},
            )
        return self._runtime.submit(
            SubmitControlCommand(
                actor=actor,
                source_type="compatibility",
                entity_instance_id=entity_instance_id,
                value=value,
                idempotency_key=idempotency_key,
                confirmation_id=confirmation_id,
                origin_evidence=origin_evidence or {},
            )
        )


def _validate_value(value: object, data_type: str, policy: ControlPolicy) -> str | None:
    valid_type = {
        "FLOAT": isinstance(value, (int, float)) and not isinstance(value, bool),
        "INT": isinstance(value, int) and not isinstance(value, bool),
        "BOOL": isinstance(value, bool),
        "STRING": isinstance(value, str),
        "ENUM": isinstance(value, str),
    }.get(data_type, False)
    if not valid_type:
        return "CONTROL_VALUE_TYPE_INVALID"
    if data_type in {"FLOAT", "INT"} and not math.isfinite(float(value)):
        return "CONTROL_VALUE_TYPE_INVALID"
    if data_type in {"FLOAT", "INT"} and (
        (policy.minimum is not None and float(value) < policy.minimum)
        or (policy.maximum is not None and float(value) > policy.maximum)
    ):
        return "CONTROL_VALUE_OUT_OF_RANGE"
    return None


def _matches(expected: object, actual: object, data_type: str, tolerance: float | None) -> bool:
    if data_type in {"FLOAT", "INT"}:
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        return abs(float(expected) - float(actual)) <= (tolerance or 0.0)
    return expected == actual


def _request_digest(request: SubmitControlCommand) -> str:
    return hashlib.sha256(json.dumps({
        "actor": request.actor,
        "source_type": request.source_type,
        "capability": request.capability,
        "entity_instance_id": str(request.entity_instance_id) if request.entity_instance_id else None,
        "value": request.value,
        "origin_evidence": request.origin_evidence,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _confirmation_digest(
    request: SubmitControlCommand,
    policy: ControlPolicy | None,
) -> str:
    """Bind a high-risk confirmation to normalized command content, not its HTTP route."""
    return hashlib.sha256(json.dumps({
        "actor": request.actor,
        "capability": request.capability,
        "entity_instance_id": str(request.entity_instance_id) if request.entity_instance_id else None,
        "value": request.value,
        "policy_snapshot": _policy_snapshot(policy) if policy is not None else None,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _control_policy(value: ControlPolicy | dict[str, Any] | None) -> ControlPolicy | None:
    if value is None or isinstance(value, ControlPolicy):
        return value
    return ControlPolicy(
        minimum=value.get("minimum"),
        maximum=value.get("maximum"),
        cooldown_seconds=int(value["cooldown_seconds"]),
        readback_definition=str(value["readback_definition"]),
        tolerance=value.get("tolerance"),
        timeout_seconds=int(value["timeout_seconds"]),
        interlocks=tuple(
            ControlInterlock(str(item["definition_id"]), item["equals"])
            for item in value.get("interlocks", ())
        ),
        high_risk=bool(value.get("high_risk", False)),
    )


_COMMAND_FIELD_NAMES = (
    "id", "actor", "source_type", "capability", "entity_instance_id", "expected_value", "data_type",
    "tolerance", "policy_snapshot", "origin_evidence", "timeout_at", "status", "code", "idempotency_key", "request_digest",
    "audit_event_id", "created_at", "dispatched_at",
)
_COMMAND_COLUMNS = ", ".join(_COMMAND_FIELD_NAMES)


def _command_columns(prefix: str = "") -> str:
    return ", ".join(f"{prefix}{field}" for field in _COMMAND_FIELD_NAMES)


def _command_values(command: ControlCommand) -> tuple[object, ...]:
    return (
        command.id,
        command.actor,
        command.source_type,
        command.capability,
        command.entity_instance_id,
        json.dumps(command.expected_value, ensure_ascii=True),
        command.data_type,
        command.tolerance,
        json.dumps(command.policy_snapshot, ensure_ascii=True),
        json.dumps(command.origin_evidence, ensure_ascii=True),
        command.timeout_at,
        command.status,
        command.code,
        command.idempotency_key,
        command.request_digest,
        command.audit_event_id,
        command.created_at,
        command.dispatched_at,
    )


def _command_from_row(row: tuple[object, ...]) -> ControlCommand:
    expected = row[5]
    if isinstance(expected, str):
        expected = json.loads(expected)
    return ControlCommand(
        id=row[0],  # type: ignore[arg-type]
        actor=row[1],  # type: ignore[arg-type]
        source_type=row[2],  # type: ignore[arg-type]
        capability=row[3],  # type: ignore[arg-type]
        entity_instance_id=row[4],  # type: ignore[arg-type]
        expected_value=expected,
        data_type=row[6],  # type: ignore[arg-type]
        tolerance=row[7],  # type: ignore[arg-type]
        policy_snapshot=row[8] if isinstance(row[8], dict) else json.loads(row[8]),  # type: ignore[arg-type]
        origin_evidence=row[9] if isinstance(row[9], dict) else json.loads(row[9]),  # type: ignore[arg-type]
        timeout_at=row[10],  # type: ignore[arg-type]
        status=row[11],  # type: ignore[arg-type]
        code=row[12],  # type: ignore[arg-type]
        idempotency_key=row[13],  # type: ignore[arg-type]
        request_digest=row[14],  # type: ignore[arg-type]
        audit_event_id=row[15],  # type: ignore[arg-type]
        created_at=row[16],  # type: ignore[arg-type]
        dispatched_at=row[17],  # type: ignore[arg-type]
    )


def _neuron_target(
    node_name: str,
    tag_name: str,
    source_path: str | None,
) -> tuple[str, str, str]:
    """Translate the catalogued Neuron source without accepting caller input."""
    group_name = "group0"
    neuron_tag_name = tag_name
    if source_path and "/" in source_path:
        parts = source_path.split("/")
        if len(parts) >= 3:
            return parts[0], parts[1], "/".join(parts[2:])
        if len(parts) == 2:
            return node_name, parts[0], parts[1]
    return node_name, group_name, neuron_tag_name


def _policy_snapshot(policy: ControlPolicy) -> dict[str, object]:
    return {
        "minimum": policy.minimum,
        "maximum": policy.maximum,
        "cooldown_seconds": policy.cooldown_seconds,
        "readback_definition": policy.readback_definition,
        "tolerance": policy.tolerance,
        "timeout_seconds": policy.timeout_seconds,
        "interlocks": [
            {"definition_id": item.definition_id, "equals": item.equals}
            for item in policy.interlocks
        ],
        "high_risk": policy.high_risk,
    }
