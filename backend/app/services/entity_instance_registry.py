"""设备/实体实例的确定性计划、应用与运行解析深模块。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.entity_instance_catalog import (
    EntityInstanceDescriptor,
    LegacyEntityMigrationItem,
)


class EntityInstanceError(ValueError):
    """携带稳定机器码的实体实例错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceDescriptor:
    tag_id: UUID
    device_key: str
    device_name: str
    tag_name: str
    data_type: str
    unit: str | None
    direction: str
    enabled: bool

    def public_dict(self, *, matcher_id: str, reason: str) -> dict[str, Any]:
        value = asdict(self)
        value["tag_id"] = str(self.tag_id)
        value["matcher_id"] = matcher_id
        value["reason"] = reason
        return value


@dataclass(frozen=True)
class PlanEntityInstances:
    package_digest: str
    site_configuration_version: int
    installation_id: UUID
    slots: tuple[dict[str, Any], ...]
    selections: dict[str, UUID]
    actor: str
    overrides: dict[str, UUID] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityInstancePlan:
    id: UUID
    package_digest: str
    site_configuration_version: int
    installation_id: UUID
    source_catalog_version: str
    status: str
    items: tuple[dict[str, Any], ...]
    blockers: tuple[dict[str, str], ...]
    actor: str
    digest: str

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = str(self.id)
        value["installation_id"] = str(self.installation_id)
        value["items"] = list(self.items)
        value["blockers"] = list(self.blockers)
        return value


@dataclass(frozen=True)
class ApplyEntityInstancePlan:
    plan_id: UUID
    plan_digest: str
    actor: str


@dataclass(frozen=True)
class ApplyOutcome:
    plan_id: UUID
    device_instance_ids: tuple[UUID, ...]
    entity_instance_ids: tuple[UUID, ...]
    binding_ids: tuple[UUID, ...]
    status: str = "applied"

    def public_dict(self) -> dict[str, Any]:
        return {
            "plan_id": str(self.plan_id),
            "device_instance_ids": [str(item) for item in self.device_instance_ids],
            "entity_instance_ids": [str(item) for item in self.entity_instance_ids],
            "binding_ids": [str(item) for item in self.binding_ids],
            "status": self.status,
        }


@dataclass(frozen=True)
class ResolvedEntitySource:
    entity_instance_id: UUID
    definition_id: str
    instance_key: str
    device_instance_id: UUID
    binding_id: UUID | None
    tag_id: UUID | None
    matcher_id: str | None
    confirmation_audit_id: UUID | None
    data_type: str
    unit: str | None
    direction: str
    freshness_seconds: float
    source_kind: str = "legacy_tag"
    source_id: UUID | None = None
    conversion_revision_id: UUID | None = None
    site_configuration_version: int | None = None

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for field in (
            "entity_instance_id",
            "device_instance_id",
            "binding_id",
            "tag_id",
            "confirmation_audit_id",
        ):
            value[field] = str(value[field])
        return value


class SourceCatalog(Protocol):
    def version(self, transaction: Any | None = None) -> str: ...

    def list_sources(
        self,
        transaction: Any | None = None,
    ) -> tuple[SourceDescriptor, ...]: ...


class EntityInstanceRepository(Protocol):
    def save_plan(self, plan: EntityInstancePlan) -> EntityInstancePlan: ...

    def get_approved_plan(
        self,
        plan_id: UUID,
        transaction: Any | None = None,
    ) -> EntityInstancePlan | None: ...

    def apply_plan(
        self,
        plan: EntityInstancePlan,
        actor: str,
        transaction: Any | None = None,
    ) -> ApplyOutcome: ...

    def resolve(self, entity_instance_id: UUID) -> ResolvedEntitySource | None: ...

    def control_policy(self, entity_instance_id: UUID) -> dict[str, Any] | None: ...

    def entity_instance_for_definition(
        self,
        device_instance_id: UUID,
        definition_id: str,
    ) -> UUID | None: ...

class InMemorySourceCatalog:
    """固定来源目录 Adapter；版本由规范内容决定，与查询顺序无关。"""

    def __init__(self, sources: tuple[SourceDescriptor, ...] = ()) -> None:
        self._sources = sources

    def replace(self, sources: tuple[SourceDescriptor, ...]) -> None:
        self._sources = sources

    def version(self, transaction: Any | None = None) -> str:
        del transaction
        content = []
        for source in sorted(self._sources, key=lambda item: str(item.tag_id)):
            value = asdict(source)
            value["tag_id"] = str(source.tag_id)
            content.append(value)
        return _digest(content)

    def list_sources(
        self,
        transaction: Any | None = None,
    ) -> tuple[SourceDescriptor, ...]:
        del transaction
        return self._sources


class InMemoryEntityInstanceRepository:
    """辅助测试 Adapter；应用计划原子、幂等且不读旧绑定。"""

    def __init__(
        self,
        *,
        legacy_entities: tuple[tuple[UUID, str, tuple[UUID, ...]], ...] = (),
    ) -> None:
        self._plans: dict[UUID, EntityInstancePlan] = {}
        self._plans_by_digest: dict[str, EntityInstancePlan] = {}
        self._outcomes: dict[UUID, ApplyOutcome] = {}
        self._devices: dict[UUID, dict[str, Any]] = {}
        self._entities: dict[UUID, dict[str, Any]] = {}
        self._bindings: dict[UUID, ResolvedEntitySource] = {}
        self._point_sources: dict[UUID, ResolvedEntitySource] = {}
        self._legacy_entities = legacy_entities
        self._failovers: dict[UUID, dict[str, Any]] = {}

    @property
    def device_instance_count(self) -> int:
        return len(self._devices)

    @property
    def entity_instance_count(self) -> int:
        return len(self._entities)

    @property
    def binding_count(self) -> int:
        return len(self._bindings)

    def save_plan(self, plan: EntityInstancePlan) -> EntityInstancePlan:
        existing = self._plans_by_digest.get(plan.digest)
        if existing is not None:
            return existing
        self._plans[plan.id] = plan
        self._plans_by_digest[plan.digest] = plan
        return plan

    def get_approved_plan(
        self,
        plan_id: UUID,
        transaction: Any | None = None,
    ) -> EntityInstancePlan | None:
        del transaction
        return self._plans.get(plan_id)

    def apply_plan(
        self,
        plan: EntityInstancePlan,
        actor: str,
        transaction: Any | None = None,
    ) -> ApplyOutcome:
        del actor
        del transaction
        existing = self._outcomes.get(plan.id)
        if existing is not None:
            return existing

        device_ids: list[UUID] = []
        entity_ids: list[UUID] = []
        binding_ids: list[UUID] = []
        pending_devices: dict[UUID, dict[str, Any]] = {}
        pending_entities: dict[UUID, dict[str, Any]] = {}
        pending_bindings: dict[UUID, ResolvedEntitySource] = {}
        pending_failovers: dict[UUID, dict[str, Any]] = {}
        removed_failovers: set[UUID] = set()
        reserved_tags = {
            source.tag_id: entity_id for entity_id, source in self._bindings.items()
        }
        for entity_id, failover in self._failovers.items():
            reserved_tags[failover["primary_tag_id"]] = entity_id
            reserved_tags[failover["standby_tag_id"]] = entity_id
        for item in plan.items:
            device_id = UUID(item["device_instance_id"])
            entity_id = UUID(item["entity_instance_id"])
            pending_devices[device_id] = {
                "slot_id": item["slot_id"],
                "instance_key": item["instance_key"],
                "display_name": item["device_display_name"],
                "device_category": item["device_category"],
                "node_id": item.get("node_id"),
            }
            pending_entities[entity_id] = {
                "definition_id": item["definition_id"],
                "device_instance_id": device_id,
                "display_name": item["definition_display_name"],
                "data_type": item["data_type"],
                "unit": item["unit"],
                "direction": item["direction"],
                "freshness_seconds": float(item["freshness_seconds"]),
                "control": item.get("control"),
                "source_kind": item.get("source_kind", "legacy_tag"),
            }
            device_ids.append(device_id)
            entity_ids.append(entity_id)
            if item.get("source_kind") == "point_conversion":
                continue
            tag_id = UUID(item["selected_tag_id"])
            standby_tag_id = (
                UUID(item["standby_tag_id"]) if item.get("standby_tag_id") else None
            )
            for reserved_tag_id in (tag_id, standby_tag_id):
                if reserved_tag_id is None:
                    continue
                owner = reserved_tags.get(reserved_tag_id)
                if owner is not None and owner != entity_id:
                    raise EntityInstanceError(
                        "ENTITY_BINDING_SOURCE_IN_USE",
                        "Physical source is reserved by another entity instance",
                    )
                reserved_tags[reserved_tag_id] = entity_id
            binding_id = UUID(item["binding_id"])
            audit_id = UUID(item["confirmation_audit_id"])
            resolved_binding = ResolvedEntitySource(
                entity_instance_id=entity_id,
                definition_id=item["definition_id"],
                instance_key=item["instance_key"],
                device_instance_id=device_id,
                binding_id=binding_id,
                tag_id=tag_id,
                matcher_id=item["matcher_id"],
                confirmation_audit_id=audit_id,
                data_type=item["data_type"],
                unit=item["unit"],
                direction=item["direction"],
                freshness_seconds=float(item["freshness_seconds"]),
            )
            if item.get("failover_policy") == "manual" and standby_tag_id:
                previous = self._failovers.get(entity_id)
                if previous and previous["current_role"] == "standby" and (
                    previous["primary_tag_id"] != tag_id
                    or previous["standby_tag_id"] != standby_tag_id
                ):
                    raise EntityInstanceError(
                        "ENTITY_FAILOVER_POLICY_CHANGE_REQUIRES_PRIMARY",
                        "Switch the entity instance back to primary before changing its failover policy",
                    )
                failover = {
                    "primary_tag_id": tag_id,
                    "standby_tag_id": standby_tag_id,
                    "current_role": previous["current_role"] if previous else "primary",
                    "switch_count": previous["switch_count"] if previous else 0,
                    "audit": list(previous["audit"]) if previous else [],
                }
                resolved_binding = ResolvedEntitySource(
                    **{
                        **asdict(resolved_binding),
                        "tag_id": failover[f"{failover['current_role']}_tag_id"],
                    }
                )
                pending_failovers[entity_id] = failover
            elif (
                entity_id in self._failovers
                and self._failovers[entity_id]["current_role"] == "standby"
            ):
                raise EntityInstanceError(
                    "ENTITY_FAILOVER_POLICY_CHANGE_REQUIRES_PRIMARY",
                    "Switch the entity instance back to primary before removing its failover policy",
                )
            else:
                removed_failovers.add(entity_id)
            pending_bindings[entity_id] = resolved_binding
            binding_ids.append(binding_id)

        self._devices.update(pending_devices)
        self._entities.update(pending_entities)
        self._bindings.update(pending_bindings)
        for entity_id in removed_failovers:
            self._failovers.pop(entity_id, None)
        self._failovers.update(pending_failovers)
        outcome = ApplyOutcome(
            plan.id,
            tuple(dict.fromkeys(device_ids)),
            tuple(entity_ids),
            tuple(binding_ids),
        )
        self._outcomes[plan.id] = outcome
        return outcome

    def resolve(self, entity_instance_id: UUID) -> ResolvedEntitySource | None:
        return self._bindings.get(entity_instance_id) or self._point_sources.get(
            entity_instance_id
        )

    def activate_point_conversion_outputs(
        self,
        revision_id: UUID,
        site_configuration_version: int,
        entity_instance_ids: tuple[UUID, ...],
    ) -> None:
        """Link the in-memory runtime Adapter to applied L1 output evidence."""
        pending: dict[UUID, ResolvedEntitySource] = {}
        for entity_instance_id in entity_instance_ids:
            entity = self._entities.get(entity_instance_id)
            if entity is None or entity.get("source_kind") != "point_conversion":
                raise EntityInstanceError(
                    "ENTITY_SOURCE_INVALID",
                    "Point conversion output does not belong to an installed entity",
                )
            device = self._devices[entity["device_instance_id"]]
            pending[entity_instance_id] = ResolvedEntitySource(
                entity_instance_id=entity_instance_id,
                definition_id=entity["definition_id"],
                instance_key=device["instance_key"],
                device_instance_id=entity["device_instance_id"],
                binding_id=None,
                tag_id=None,
                matcher_id=None,
                confirmation_audit_id=None,
                data_type=entity["data_type"],
                unit=entity["unit"],
                direction=entity["direction"],
                freshness_seconds=entity["freshness_seconds"],
                source_kind="point_conversion",
                source_id=entity_instance_id,
                conversion_revision_id=revision_id,
                site_configuration_version=site_configuration_version,
            )
        self._point_sources.update(pending)

    def control_policy(self, entity_instance_id: UUID) -> dict[str, Any] | None:
        entity = self._entities.get(entity_instance_id)
        return entity.get("control") if entity is not None else None

    def entity_instance_for_definition(
        self,
        device_instance_id: UUID,
        definition_id: str,
    ) -> UUID | None:
        matches = [
            entity_id
            for entity_id, entity in self._entities.items()
            if entity["device_instance_id"] == device_instance_id
            and entity["definition_id"] == definition_id
            and (
                entity_id in self._bindings
                or entity_id in self._point_sources
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def list_instances(self) -> tuple[EntityInstanceDescriptor, ...]:
        descriptors = []
        for entity_id, entity in self._entities.items():
            if (
                entity_id not in self._bindings
                and entity.get("source_kind") != "point_conversion"
            ):
                continue
            device = self._devices[entity["device_instance_id"]]
            descriptors.append(
                EntityInstanceDescriptor(
                    id=entity_id,
                    device_instance_id=entity["device_instance_id"],
                    slot_id=device["slot_id"],
                    instance_key=device["instance_key"],
                    device_category=device["device_category"],
                    device_display_name=device["display_name"],
                    definition_id=entity["definition_id"],
                    display_name=entity["display_name"],
                    data_type=entity["data_type"],
                    unit=entity["unit"],
                    direction=entity["direction"],
                    freshness_seconds=entity["freshness_seconds"],
                    confirmed=True,
                )
            )
        return tuple(sorted(descriptors, key=lambda item: (item.instance_key, item.definition_id)))

    def failover_state(self, entity_instance_id: UUID) -> EntityFailoverState | None:
        from app.services.entity_instance_failover import EntityFailoverState

        state = self._failovers.get(entity_instance_id)
        if state is None:
            return None
        latest = state["audit"][-1] if state["audit"] else {}
        return EntityFailoverState(
            entity_instance_id,
            state["current_role"],
            state["switch_count"],
            latest.get("actor"),
            latest.get("reason"),
            latest.get("changed_at"),
            tuple(
                {**item, "changed_at": item["changed_at"].isoformat()}
                for item in state["audit"]
            ),
        )

    def switch_failover(
        self,
        entity_instance_id: UUID,
        expected_current_role: str,
        target_role: str,
        actor: str,
        reason: str,
    ) -> EntityFailoverState:
        state = self._failovers.get(entity_instance_id)
        if state is None:
            raise EntityInstanceError(
                "ENTITY_FAILOVER_NOT_CONFIGURED",
                "Entity instance has no manual failover policy",
            )
        if state["current_role"] != expected_current_role:
            raise EntityInstanceError(
                "ENTITY_FAILOVER_STATE_CHANGED",
                "Entity source role changed after it was read",
            )
        if target_role == expected_current_role or target_role not in {"primary", "standby"}:
            raise EntityInstanceError(
                "ENTITY_FAILOVER_TARGET_INVALID",
                "Failover target must be the other configured role",
            )
        current = self._bindings[entity_instance_id]
        target_tag_id = state[f"{target_role}_tag_id"]
        changed_at = datetime.now(timezone.utc)
        audit = {
            "from_role": expected_current_role,
            "to_role": target_role,
            "actor": actor,
            "reason": reason,
            "changed_at": changed_at,
        }
        self._bindings[entity_instance_id] = ResolvedEntitySource(
            **{**asdict(current), "tag_id": target_tag_id}
        )
        state["current_role"] = target_role
        state["switch_count"] += 1
        state["audit"].append(audit)
        return self.failover_state(entity_instance_id)  # type: ignore[return-value]

    def preview_legacy(self) -> tuple[LegacyEntityMigrationItem, ...]:
        candidates_by_tag = {
            source.tag_id: source.entity_instance_id for source in self._bindings.values()
        }
        for entity_id, failover in self._failovers.items():
            candidates_by_tag[failover["primary_tag_id"]] = entity_id
            candidates_by_tag[failover["standby_tag_id"]] = entity_id
        items = []
        for legacy_id, name, tag_ids in self._legacy_entities:
            candidates = tuple(
                sorted(
                    {
                        candidates_by_tag[tag_id]
                        for tag_id in tag_ids
                        if tag_id in candidates_by_tag
                    },
                    key=str,
                )
            )
            classification = (
                "unique" if len(candidates) == 1
                else "ambiguous" if len(candidates) > 1
                else "missing"
            )
            items.append(
                LegacyEntityMigrationItem(legacy_id, name, classification, candidates)
            )
        return tuple(items)


class EntityInstanceRegistry:
    """安装与运行只依赖的三个实体实例公开操作。"""

    def __init__(
        self,
        repository: EntityInstanceRepository,
        source_catalog: SourceCatalog,
        current_site_configuration_version: Callable[[Any | None], int],
    ) -> None:
        self._repository = repository
        self._source_catalog = source_catalog
        self._current_site_configuration_version = current_site_configuration_version

    def plan(self, request: PlanEntityInstances) -> EntityInstancePlan:
        sources = self._source_catalog.list_sources()
        catalog_version = self._source_catalog.version()
        items: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        seen_slot_instances: set[tuple[str, str]] = set()
        for raw_slot in request.slots:
            slot = _validated_slot(raw_slot)
            slot_instance = (slot["id"], slot["instance_key"])
            if slot_instance in seen_slot_instances:
                raise EntityInstanceError(
                    "ENTITY_SLOT_INVALID",
                    "Entity slot instance keys must be unique",
                )
            seen_slot_instances.add(slot_instance)
            for definition in slot["definitions"]:
                item, blocker = _plan_definition(
                    request.installation_id,
                    request.site_configuration_version,
                    slot,
                    definition,
                    sources,
                    request.selections,
                    request.overrides,
                    self._repository.resolve,
                )
                items.append(item)
                if blocker is not None:
                    blockers.append(blocker)

        content = {
            "package_digest": request.package_digest,
            "site_configuration_version": request.site_configuration_version,
            "installation_id": str(request.installation_id),
            "source_catalog_version": catalog_version,
            "items": items,
            "blockers": blockers,
            "actor": request.actor,
        }
        plan_digest = _digest(content)
        plan = EntityInstancePlan(
            id=uuid5(NAMESPACE_URL, f"zizu/entity-plan/{plan_digest}"),
            package_digest=request.package_digest,
            site_configuration_version=request.site_configuration_version,
            installation_id=request.installation_id,
            source_catalog_version=catalog_version,
            status="blocked" if blockers else "ready",
            items=tuple(items),
            blockers=tuple(blockers),
            actor=request.actor,
            digest=plan_digest,
        )
        return self._repository.save_plan(plan)

    def apply(
        self,
        command: ApplyEntityInstancePlan,
        *,
        transaction: Any | None = None,
    ) -> ApplyOutcome:
        # The repository is the approval boundary. Callers cannot inject a
        # reconstructed plan and thereby bypass the persisted package/plan
        # evidence checked by the production Adapter.
        plan = self._repository.get_approved_plan(command.plan_id, transaction)
        if plan is None:
            raise EntityInstanceError(
                "ENTITY_INSTANCE_PLAN_NOT_FOUND",
                "Entity instance plan was not found",
            )
        if plan.digest != command.plan_digest:
            raise EntityInstanceError(
                "ENTITY_INSTANCE_PLAN_DIGEST_MISMATCH",
                "Entity instance plan digest does not match",
            )
        if plan.blockers:
            raise EntityInstanceError(
                "ENTITY_INSTANCE_PLAN_BLOCKED",
                "Entity instance plan contains blockers",
            )
        current_version = self._current_site_configuration_version(transaction)
        if (
            current_version != plan.site_configuration_version
            or plan.source_catalog_version
            != self._source_catalog.version(transaction)
        ):
            raise EntityInstanceError(
                "ENTITY_BINDING_PLAN_STALE",
                "Site configuration or source catalog changed after planning",
            )
        return self._repository.apply_plan(plan, command.actor, transaction)

    def resolve(self, entity_instance_id: UUID) -> ResolvedEntitySource:
        source = self._repository.resolve(entity_instance_id)
        if source is None:
            raise EntityInstanceError(
                "ENTITY_INSTANCE_NOT_BOUND",
                "Entity instance has no confirmed active primary binding",
            )
        if source.source_kind == "point_conversion":
            if (
                source.source_id != source.entity_instance_id
                or source.conversion_revision_id is None
                or source.site_configuration_version is None
            ):
                raise EntityInstanceError(
                    "ENTITY_SOURCE_INVALID",
                    "Point conversion entity source evidence is incomplete",
                )
            return source
        if source.source_kind != "legacy_tag" or source.tag_id is None:
            raise EntityInstanceError(
                "ENTITY_SOURCE_KIND_INVALID",
                "Entity source kind is invalid",
            )
        catalog_source = next(
            (
                candidate
                for candidate in self._source_catalog.list_sources()
                if candidate.tag_id == source.tag_id and candidate.enabled
            ),
            None,
        )
        if catalog_source is None:
            raise EntityInstanceError(
                "ENTITY_BINDING_SOURCE_INVALID",
                "Confirmed entity source is missing or disabled",
            )
        expected = {
            "data_type": source.data_type,
            "unit": source.unit,
            "direction": source.direction,
        }
        actual = {
            "data_type": catalog_source.data_type,
            "unit": catalog_source.unit,
            "direction": catalog_source.direction,
        }
        if expected != actual:
            raise EntityInstanceError(
                "ENTITY_BINDING_SOURCE_INVALID",
                "Confirmed entity source metadata changed",
            )
        return source

def _plan_definition(
    installation_id: UUID,
    site_configuration_version: int,
    slot: dict[str, Any],
    definition: dict[str, Any],
    sources: tuple[SourceDescriptor, ...],
    selections: dict[str, UUID],
    overrides: dict[str, UUID],
    resolve_existing: Callable[[UUID], ResolvedEntitySource | None],
) -> tuple[dict[str, Any], dict[str, str] | None]:
    key = f"{slot['id']}/{slot['instance_key']}/{definition['id']}"
    entity_id = _stable_id(
        "entity",
        installation_id,
        slot["id"],
        slot["instance_key"],
        definition["id"],
    )
    device_id = _stable_id(
        "device",
        installation_id,
        slot["id"],
        slot["instance_key"],
    )
    if definition.get("source_kind") == "point_conversion":
        return {
            "slot_id": slot["id"],
            "device_category": slot["device_category"],
            "instance_key": slot["instance_key"],
            "device_display_name": slot["display_name"],
            "node_id": slot.get("node_id"),
            "definition_id": definition["id"],
            "definition_display_name": definition["display_name"],
            "device_instance_id": str(device_id),
            "entity_instance_id": str(entity_id),
            "source_kind": "point_conversion",
            "conversion_output_key": definition["conversion_output_key"],
            "matcher_id": None,
            "expected_tag_name": None,
            "candidates": (),
            "override_candidates": (),
            "standby_candidates": (),
            "selected_tag_id": None,
            "failover_policy": None,
            "standby_tag_id": None,
            "selection_source": "point_conversion_plan",
            "selection_reason": "Output is bound by the point-conversion subplan",
            "binding_id": None,
            "confirmation_audit_id": None,
            "data_type": definition["data_type"],
            "unit": definition.get("unit"),
            "direction": definition["direction"],
            "freshness_seconds": float(slot["freshness_seconds"]),
            "control": definition.get("control"),
            "status": "ready",
            "code": "ENTITY_POINT_CONVERSION_READY",
            "action": "add",
        }, None
    matcher = definition["matcher"]
    matches = [
        source
        for source in sources
        if source.enabled
        and source.device_key == matcher["device_key"]
        and source.tag_name.casefold() == matcher["tag_name"].casefold()
    ]
    standby_matches = (
        [
            source
            for source in sources
            if source.enabled
            and source.device_key == matcher.get("standby_device_key")
            and source.tag_name.casefold() == matcher["tag_name"].casefold()
        ]
        if matcher.get("failover_policy") == "manual"
        else []
    )
    reason = (
        f"{matcher['id']}: device_key={matcher['device_key']}, "
        f"tag_name={matcher['tag_name']}"
    )
    candidates = tuple(
        source.public_dict(matcher_id=matcher["id"], reason=reason)
        for source in sorted(matches, key=lambda item: str(item.tag_id))
    )
    override_matches = (
        [
            source
            for source in sources
            if source.enabled
            and source.device_key == matcher["device_key"]
            and _compatibility_code(definition, source) == "ENTITY_BINDING_READY"
        ]
        if not matches
        else []
    )
    override_candidates = tuple(
        source.public_dict(
            matcher_id=matcher["id"],
            reason=(
                f"{matcher['id']}: engineer override from expected "
                f"tag_name={matcher['tag_name']} to tag_name={source.tag_name} "
                f"on device_key={matcher['device_key']}"
            ),
        )
        for source in sorted(override_matches, key=lambda item: str(item.tag_id))
    )
    standby_candidates = tuple(
        source.public_dict(
            matcher_id=matcher["id"],
            reason=(
                f"{matcher['id']}: standby_device_key={matcher.get('standby_device_key')}, "
                f"tag_name={matcher['tag_name']}"
            ),
        )
        for source in sorted(standby_matches, key=lambda item: str(item.tag_id))
    )
    selected_id = selections.get(key)
    override_id = overrides.get(key)
    selected = next((item for item in matches if item.tag_id == selected_id), None)
    override_selected = next(
        (item for item in sources if item.enabled and item.tag_id == override_id),
        None,
    )
    selection_source = "engineer_selection" if selected is not None else "unique_match"
    selection_reason: str | None = None
    code: str
    if selected_id is not None and override_id is not None:
        code = "ENTITY_BINDING_OVERRIDE_CONFLICT"
    elif override_id is not None:
        if (
            matches
            or override_selected is None
            or override_selected.device_key != matcher["device_key"]
        ):
            code = "ENTITY_BINDING_OVERRIDE_INVALID"
        else:
            selected = override_selected
            code = _compatibility_code(definition, selected)
            selection_source = "engineer_override"
            selection_reason = (
                f"{matcher['id']}: engineer override from expected "
                f"tag_name={matcher['tag_name']} to tag_name={selected.tag_name} "
                f"on device_key={matcher['device_key']}"
            )
    elif selected_id is not None and selected is None:
        code = "ENTITY_BINDING_SELECTION_INVALID"
    elif selected is None and not matches:
        code = "ENTITY_BINDING_MISSING"
    elif selected is None and len(matches) > 1:
        code = "ENTITY_BINDING_AMBIGUOUS"
    else:
        selected = selected or matches[0]
        code = _compatibility_code(definition, selected)
    if code == "ENTITY_BINDING_READY" and selection_reason is None and selected is not None:
        selection_reason = next(
            item["reason"] for item in candidates if item["tag_id"] == str(selected.tag_id)
        )
    standby = standby_matches[0] if len(standby_matches) == 1 else None
    if code == "ENTITY_BINDING_READY" and matcher.get("failover_policy") == "manual":
        if standby is None:
            code = (
                "ENTITY_FAILOVER_STANDBY_MISSING"
                if not standby_matches
                else "ENTITY_FAILOVER_STANDBY_AMBIGUOUS"
            )
        else:
            standby_code = _compatibility_code(definition, standby)
            if standby_code != "ENTITY_BINDING_READY":
                code = standby_code

    ready = code == "ENTITY_BINDING_READY"
    existing = resolve_existing(entity_id)
    # The relationship has one stable identity across rebindings. Confirmation
    # evidence is a separate immutable event for each configuration version.
    binding_id = _stable_id("binding", entity_id) if ready else None
    audit_id = (
        _stable_id(
            "confirmation",
            entity_id,
            selected.tag_id,
            site_configuration_version,
        )
        if ready
        else None
    )
    item: dict[str, Any] = {
        "slot_id": slot["id"],
        "device_category": slot["device_category"],
        "instance_key": slot["instance_key"],
        "device_display_name": slot["display_name"],
        "definition_id": definition["id"],
        "definition_display_name": definition["display_name"],
        "device_instance_id": str(device_id),
        "entity_instance_id": str(entity_id),
        "matcher_id": matcher["id"],
        "expected_tag_name": matcher["tag_name"],
        "candidates": candidates,
        "override_candidates": override_candidates,
        "standby_candidates": standby_candidates,
        "selected_tag_id": str(selected.tag_id) if ready else None,
        "failover_policy": matcher.get("failover_policy"),
        "standby_tag_id": str(standby.tag_id) if ready and standby is not None else None,
        "selection_source": selection_source if ready else None,
        "selection_reason": selection_reason if ready else None,
        "binding_id": str(binding_id) if binding_id else None,
        "confirmation_audit_id": str(audit_id) if audit_id else None,
        "data_type": definition["data_type"],
        "unit": definition.get("unit"),
        "direction": definition["direction"],
        "freshness_seconds": float(slot["freshness_seconds"]),
        "control": definition.get("control"),
        "status": "ready" if ready else "blocked",
        "code": code,
        "action": (
            "preserve"
            if ready and existing is not None and existing.tag_id == selected.tag_id
            else "update"
            if ready and existing is not None
            else "add"
        ),
    }
    if ready:
        return item, None
    return item, {
        "code": code,
        "entity_key": key,
        "message": "Entity binding requires an explicit compatible source",
    }


def _compatibility_code(
    definition: dict[str, Any],
    source: SourceDescriptor,
) -> str:
    if source.data_type != definition["data_type"]:
        return "ENTITY_BINDING_TYPE_MISMATCH"
    if (source.unit or None) != (definition.get("unit") or None):
        return "ENTITY_BINDING_UNIT_MISMATCH"
    required_direction = definition["direction"]
    compatible = {
        "R": source.direction in {"R", "RW"},
        "W": source.direction in {"W", "RW"},
        "RW": source.direction == "RW",
    }[required_direction]
    if not compatible:
        return "ENTITY_BINDING_DIRECTION_MISMATCH"
    return "ENTITY_BINDING_READY"


def _validated_slot(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity slot must be a mapping")
    required = ("id", "device_category", "instance_key", "display_name")
    if any(not isinstance(value.get(field), str) or not value[field] for field in required):
        raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity slot identity is invalid")
    definitions = value.get("definitions")
    if not isinstance(value.get("freshness_seconds"), (int, float)) or not (
        0 < float(value["freshness_seconds"]) <= 3600
    ):
        raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity freshness is invalid")
    if not isinstance(definitions, list) or not definitions:
        raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity slot definitions are required")
    definition_ids: set[str] = set()
    matcher_ids: set[str] = set()
    for definition in definitions:
        if not isinstance(definition, dict):
            raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity definition is invalid")
        definition_required = ("id", "display_name", "data_type", "direction")
        if any(
            not isinstance(definition.get(field), str) or not definition[field]
            for field in definition_required
        ) or definition["data_type"] not in {"FLOAT", "INT", "BOOL", "STRING", "ENUM", "CODE_SET"} \
                or definition["direction"] not in {"R", "W", "RW"}:
            raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity definition is invalid")
        if definition["id"] in definition_ids:
            raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity definition ids must be unique")
        definition_ids.add(definition["id"])
        if definition.get("source_kind") == "point_conversion":
            if (
                not isinstance(definition.get("conversion_output_key"), str)
                or not definition["conversion_output_key"]
                or definition["direction"] not in {"R", "RW"}
            ):
                raise EntityInstanceError(
                    "ENTITY_SLOT_INVALID",
                    "Point-conversion entity definition is invalid",
                )
            continue
        matcher = definition.get("matcher")
        if not isinstance(matcher, dict) or any(
            not isinstance(matcher.get(field), str) or not matcher[field]
            for field in ("id", "device_key", "tag_name")
        ):
            raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity matcher is invalid")
        if matcher["id"] in matcher_ids:
            raise EntityInstanceError(
                "ENTITY_SLOT_INVALID",
                "Entity matcher ids must be unique",
            )
        matcher_ids.add(matcher["id"])
    return value


def _stable_id(kind: str, *parts: object) -> UUID:
    return uuid5(NAMESPACE_URL, "/".join(("zizu", kind, *(str(part) for part in parts))))


def entity_instance_plan_from_dict(value: dict[str, Any]) -> EntityInstancePlan:
    """Rebuild the immutable entity plan stored inside an installation plan."""
    return EntityInstancePlan(
        id=UUID(value["id"]),
        package_digest=value["package_digest"],
        site_configuration_version=value["site_configuration_version"],
        installation_id=UUID(value["installation_id"]),
        source_catalog_version=value["source_catalog_version"],
        status=value["status"],
        items=tuple(value["items"]),
        blockers=tuple(value["blockers"]),
        actor=value["actor"],
        digest=value["digest"],
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
