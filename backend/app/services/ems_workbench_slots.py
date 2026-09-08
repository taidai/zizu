"""Deterministic fixed-slot projection for the light-storage-charging workbench."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from app.services.configuration_revision import ConfigurationRevisionError, GateState
from app.services.data_trunk_contracts import DataTrunkError
from app.services.entity_instance_catalog import EntityInstanceCatalog, EntityInstanceDescriptor


class EmsWorkbenchSlotError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkbenchSlotSpec:
    key: str
    label: str
    definition_keys: tuple[str, ...]
    unit: str


@dataclass(frozen=True)
class ResolvedWorkbenchSlot:
    key: str
    label: str
    binding_mode: str
    reason: str
    entity: EntityInstanceDescriptor | None


@dataclass(frozen=True)
class WorkbenchSlotWriteReceipt:
    slot_key: str
    entity_instance_id: UUID | None
    configuration_revision: int
    replayed: bool


@dataclass(frozen=True)
class _PendingWorkbenchSlotRecovery:
    slot_key: str
    entity_instance_id: UUID | None
    base_configuration_revision: int
    actor: str
    idempotency_key: str
    configuration_revision: int | None

    def matches(
        self,
        *,
        slot_key: str,
        entity_instance_id: UUID | None,
        base_configuration_revision: int,
        actor: str,
        idempotency_key: str,
        receipt: WorkbenchSlotWriteReceipt,
    ) -> bool:
        return (
            self.slot_key == slot_key
            and self.entity_instance_id == entity_instance_id
            and self.base_configuration_revision == base_configuration_revision
            and self.actor == actor.strip()
            and self.idempotency_key == idempotency_key.strip()
            and receipt.slot_key == self.slot_key
            and receipt.entity_instance_id == self.entity_instance_id
            and receipt.configuration_revision == (
                self.configuration_revision
                if self.configuration_revision is not None
                else self.base_configuration_revision + 1
            )
        )


class WorkbenchSlotRepository(Protocol):
    def list_manual_bindings(self) -> Mapping[str, UUID]: ...

    def find_replay(
        self,
        *,
        slot_key: str,
        entity_instance_id: UUID | None,
        base_configuration_revision: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkbenchSlotWriteReceipt | None: ...

    def set_binding(
        self,
        *,
        slot_key: str,
        entity_instance_id: UUID | None,
        base_configuration_revision: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkbenchSlotWriteReceipt: ...


class WorkbenchSlotRuntimeGate(Protocol):
    @property
    def state(self) -> GateState: ...

    def begin_configuration_publish(self, base_revision: int) -> None: ...

    def cancel_configuration_publish(self) -> None: ...

    def reconcile_configuration_runtime(self) -> object: ...


WORKBENCH_SLOT_SPECS = (
    WorkbenchSlotSpec(
        "site-power",
        "站点功率",
        ("site.active_power", "site.activePower", "grid.active_power", "grid.activePower"),
        "kW",
    ),
    WorkbenchSlotSpec(
        "pv-power",
        "光伏功率",
        ("pv.active_power", "pv.activePower", "inverter.active_power", "inverter.activePower"),
        "kW",
    ),
    WorkbenchSlotSpec(
        "storage-power",
        "储能功率",
        (
            "storage.active_power",
            "storage.activePower",
            "ess.active_power",
            "ess.activePower",
            "pcs.active_power",
            "pcs.activePower",
        ),
        "kW",
    ),
    WorkbenchSlotSpec(
        "storage-soc",
        "储能 SOC",
        ("storage.soc", "bms.soc"),
        "%",
    ),
    WorkbenchSlotSpec(
        "charging-power",
        "充电功率",
        (
            "charging.active_power",
            "charging.activePower",
            "charger.active_power",
            "charger.activePower",
            "evse.active_power",
            "evse.activePower",
        ),
        "kW",
    ),
    WorkbenchSlotSpec("load-power", "站内负荷", ("load.active_power", "load.activePower"), "kW"),
)
WORKBENCH_SLOT_SPEC_BY_KEY = MappingProxyType(
    {spec.key: spec for spec in WORKBENCH_SLOT_SPECS}
)


class EmsWorkbenchSlots:
    """Resolve and persist fixed workbench slots through one seam."""

    def __init__(
        self,
        catalog: EntityInstanceCatalog,
        repository: WorkbenchSlotRepository,
    ) -> None:
        self._catalog = catalog
        self._repository = repository
        self._publish_lock = Lock()
        self._pending_recovery: _PendingWorkbenchSlotRecovery | None = None

    def resolve(
        self,
        descriptors: Iterable[EntityInstanceDescriptor] | None = None,
    ) -> tuple[ResolvedWorkbenchSlot, ...]:
        return resolve_workbench_slots(
            self._catalog.list() if descriptors is None else descriptors,
            self._repository.list_manual_bindings(),
        )

    def bind(
        self,
        *,
        slot_key: str,
        entity_instance_id: UUID | None,
        base_configuration_revision: int,
        actor: str,
        idempotency_key: str,
        runtime_gate: WorkbenchSlotRuntimeGate | None = None,
    ) -> WorkbenchSlotWriteReceipt:
        with self._publish_lock:
            return self._bind_locked(
                slot_key=slot_key,
                entity_instance_id=entity_instance_id,
                base_configuration_revision=base_configuration_revision,
                actor=actor,
                idempotency_key=idempotency_key,
                runtime_gate=runtime_gate,
            )

    def _bind_locked(
        self,
        *,
        slot_key: str,
        entity_instance_id: UUID | None,
        base_configuration_revision: int,
        actor: str,
        idempotency_key: str,
        runtime_gate: WorkbenchSlotRuntimeGate | None,
    ) -> WorkbenchSlotWriteReceipt:
        spec = WORKBENCH_SLOT_SPEC_BY_KEY.get(slot_key)
        if spec is None:
            raise EmsWorkbenchSlotError(
                "WORKBENCH_SLOT_NOT_FOUND",
                "EMS workbench slot is not defined",
            )
        replay = self._repository.find_replay(
            slot_key=slot_key,
            entity_instance_id=entity_instance_id,
            base_configuration_revision=base_configuration_revision,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            if runtime_gate is None:
                if self._pending_recovery is not None:
                    raise DataTrunkError(
                        "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
                        "Slot publication requires runtime reconciliation",
                    )
                return replay
            runtime_state = runtime_gate.state
            if runtime_state is GateState.RUNNING:
                self._pending_recovery = None
                return replay
            if runtime_state is GateState.QUIESCED:
                pending = self._pending_recovery
                if pending is None or not pending.matches(
                    slot_key=slot_key,
                    entity_instance_id=entity_instance_id,
                    base_configuration_revision=base_configuration_revision,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    receipt=replay,
                ):
                    raise DataTrunkError(
                        "CONFIGURATION_RUNTIME_BUSY",
                        "CONFIGURATION_RUNTIME_BUSY",
                    )
                if (
                    pending.configuration_revision is None
                    and self._repository.list_manual_bindings().get(slot_key)
                    != replay.entity_instance_id
                ):
                    raise DataTrunkError(
                        "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
                        "Slot commit result is unknown: persisted binding does not match its receipt",
                    )
                runtime_gate.reconcile_configuration_runtime()
                self._pending_recovery = None
                return replay
            raise DataTrunkError(
                "CONFIGURATION_RUNTIME_BUSY",
                "CONFIGURATION_RUNTIME_BUSY",
            )
        if self._pending_recovery is not None:
            raise DataTrunkError(
                "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
                "Slot commit result is unknown: no authoritative persisted receipt; runtime remains quiesced",
            )
        if entity_instance_id is not None:
            by_id = {item.id: item for item in self._catalog.list()}
            descriptor = by_id.get(entity_instance_id)
            if descriptor is None or not descriptor.confirmed:
                raise EmsWorkbenchSlotError(
                    "WORKBENCH_SLOT_ENTITY_NOT_AVAILABLE",
                    "EMS workbench slot target is missing, inactive, or unconfirmed",
                )
            if descriptor.data_type.upper() not in {"FLOAT", "INT"}:
                raise EmsWorkbenchSlotError(
                    "WORKBENCH_SLOT_ENTITY_TYPE_INVALID",
                    "EMS workbench slot target must be numeric",
                )
            if descriptor.unit != spec.unit:
                raise EmsWorkbenchSlotError(
                    "WORKBENCH_SLOT_ENTITY_UNIT_INVALID",
                    f"EMS workbench slot target unit must be {spec.unit}",
                )
        if runtime_gate is None:
            return self._repository.set_binding(
                slot_key=slot_key,
                entity_instance_id=entity_instance_id,
                base_configuration_revision=base_configuration_revision,
                actor=actor,
                idempotency_key=idempotency_key,
            )
        runtime_gate.begin_configuration_publish(base_configuration_revision)
        try:
            receipt = self._repository.set_binding(
                slot_key=slot_key,
                entity_instance_id=entity_instance_id,
                base_configuration_revision=base_configuration_revision,
                actor=actor,
                idempotency_key=idempotency_key,
            )
        except (EmsWorkbenchSlotError, ConfigurationRevisionError):
            # These domain failures are raised before commit by the repository.
            runtime_gate.cancel_configuration_publish()
            raise
        except Exception as exc:
            # A transport failure can arrive after PostgreSQL committed. Keep
            # ownership and the fence until the atomic receipt proves the result.
            self._pending_recovery = _PendingWorkbenchSlotRecovery(
                slot_key=slot_key,
                entity_instance_id=entity_instance_id,
                base_configuration_revision=base_configuration_revision,
                actor=actor.strip(),
                idempotency_key=idempotency_key.strip(),
                configuration_revision=None,
            )
            raise DataTrunkError(
                "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
                "Slot commit result is unknown; retry the same request and idempotency key to reconcile",
            ) from exc
        try:
            runtime_gate.reconcile_configuration_runtime()
        except Exception:
            self._pending_recovery = _PendingWorkbenchSlotRecovery(
                slot_key=slot_key,
                entity_instance_id=entity_instance_id,
                base_configuration_revision=base_configuration_revision,
                actor=actor.strip(),
                idempotency_key=idempotency_key.strip(),
                configuration_revision=receipt.configuration_revision,
            )
            raise
        self._pending_recovery = None
        return receipt


def resolve_workbench_slots(
    descriptors: Iterable[EntityInstanceDescriptor],
    manual_bindings: Mapping[str, UUID],
) -> tuple[ResolvedWorkbenchSlot, ...]:
    """Resolve every fixed slot without using names, addresses, or row order."""
    available = tuple(descriptors)
    by_id = {item.id: item for item in available}
    resolved: list[ResolvedWorkbenchSlot] = []
    for spec in WORKBENCH_SLOT_SPECS:
        manual_id = manual_bindings.get(spec.key)
        if manual_id is not None:
            manual = by_id.get(manual_id)
            compatible = manual is not None and _compatible(spec, manual)
            resolved.append(
                ResolvedWorkbenchSlot(
                    key=spec.key,
                    label=spec.label,
                    binding_mode="manual",
                    reason=(
                        "WORKBENCH_SLOT_MANUAL_BINDING"
                        if compatible
                        else (
                            "WORKBENCH_SLOT_MANUAL_TARGET_INCOMPATIBLE"
                            if manual is not None and manual.confirmed
                            else "WORKBENCH_SLOT_MANUAL_TARGET_UNAVAILABLE"
                        )
                    ),
                    entity=manual if compatible else None,
                )
            )
            continue
        candidates = tuple(
            item for item in available if item.definition_id in spec.definition_keys
        )
        if len(candidates) > 1:
            resolved.append(
                ResolvedWorkbenchSlot(
                    key=spec.key,
                    label=spec.label,
                    binding_mode="ambiguous",
                    reason="WORKBENCH_SLOT_EXACT_MATCH_AMBIGUOUS",
                    entity=None,
                )
            )
            continue
        candidate = candidates[0] if candidates else None
        entity = candidate if candidate is not None and _compatible(spec, candidate) else None
        resolved.append(
            ResolvedWorkbenchSlot(
                key=spec.key,
                label=spec.label,
                binding_mode="exact" if entity else "unconfigured",
                reason=(
                    "WORKBENCH_SLOT_EXACT_MATCH"
                    if entity
                    else (
                        "WORKBENCH_SLOT_CANDIDATE_INCOMPATIBLE"
                        if candidate is not None
                        else "WORKBENCH_SLOT_NOT_CONFIGURED"
                    )
                ),
                entity=entity,
            )
        )
    return tuple(resolved)


def _compatible(
    spec: WorkbenchSlotSpec,
    descriptor: EntityInstanceDescriptor,
) -> bool:
    return (
        descriptor.confirmed
        and descriptor.data_type.upper() in {"FLOAT", "INT"}
        and descriptor.unit == spec.unit
    )


__all__ = [
    "EmsWorkbenchSlotError",
    "EmsWorkbenchSlots",
    "ResolvedWorkbenchSlot",
    "WORKBENCH_SLOT_SPECS",
    "WORKBENCH_SLOT_SPEC_BY_KEY",
    "WorkbenchSlotSpec",
    "WorkbenchSlotRuntimeGate",
    "WorkbenchSlotWriteReceipt",
    "resolve_workbench_slots",
]
