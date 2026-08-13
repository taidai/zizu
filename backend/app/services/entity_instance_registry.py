"""设备/实体实例的确定性计划、应用与运行解析深模块。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Callable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5


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
    binding_id: UUID
    tag_id: UUID
    matcher_id: str
    confirmation_audit_id: UUID
    data_type: str
    unit: str | None
    direction: str
    freshness_seconds: float

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

    def __init__(self) -> None:
        self._plans: dict[UUID, EntityInstancePlan] = {}
        self._plans_by_digest: dict[str, EntityInstancePlan] = {}
        self._outcomes: dict[UUID, ApplyOutcome] = {}
        self._devices: dict[UUID, dict[str, Any]] = {}
        self._entities: dict[UUID, dict[str, Any]] = {}
        self._bindings: dict[UUID, ResolvedEntitySource] = {}

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
        used_tags = {source.tag_id for source in self._bindings.values()}
        for item in plan.items:
            tag_id = UUID(item["selected_tag_id"])
            if tag_id in used_tags and not any(
                source.entity_instance_id == UUID(item["entity_instance_id"])
                and source.tag_id == tag_id
                for source in self._bindings.values()
            ):
                raise EntityInstanceError(
                    "ENTITY_BINDING_SOURCE_IN_USE",
                    "Physical source already has an active primary binding",
                )
            device_id = UUID(item["device_instance_id"])
            entity_id = UUID(item["entity_instance_id"])
            binding_id = UUID(item["binding_id"])
            audit_id = UUID(item["confirmation_audit_id"])
            pending_devices[device_id] = {
                "slot_id": item["slot_id"],
                "instance_key": item["instance_key"],
                "display_name": item["device_display_name"],
            }
            pending_entities[entity_id] = {
                "definition_id": item["definition_id"],
                "device_instance_id": device_id,
            }
            pending_bindings[entity_id] = ResolvedEntitySource(
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
            device_ids.append(device_id)
            entity_ids.append(entity_id)
            binding_ids.append(binding_id)
            used_tags.add(tag_id)

        self._devices.update(pending_devices)
        self._entities.update(pending_entities)
        self._bindings.update(pending_bindings)
        outcome = ApplyOutcome(
            plan.id,
            tuple(dict.fromkeys(device_ids)),
            tuple(entity_ids),
            tuple(binding_ids),
        )
        self._outcomes[plan.id] = outcome
        return outcome

    def resolve(self, entity_instance_id: UUID) -> ResolvedEntitySource | None:
        return self._bindings.get(entity_instance_id)


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
        seen_slot_ids: set[str] = set()
        for raw_slot in request.slots:
            slot = _validated_slot(raw_slot)
            if slot["id"] in seen_slot_ids:
                raise EntityInstanceError(
                    "ENTITY_SLOT_INVALID",
                    "Entity slot ids must be unique",
                )
            seen_slot_ids.add(slot["id"])
            for definition in slot["definitions"]:
                item, blocker = _plan_definition(
                    request.installation_id,
                    request.site_configuration_version,
                    slot,
                    definition,
                    sources,
                    request.selections,
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
    resolve_existing: Callable[[UUID], ResolvedEntitySource | None],
) -> tuple[dict[str, Any], dict[str, str] | None]:
    matcher = definition["matcher"]
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
    matches = [
        source
        for source in sources
        if source.enabled
        and source.device_key == matcher["device_key"]
        and source.tag_name.casefold() == matcher["tag_name"].casefold()
    ]
    reason = (
        f"{matcher['id']}: device_key={matcher['device_key']}, "
        f"tag_name={matcher['tag_name']}"
    )
    candidates = tuple(
        source.public_dict(matcher_id=matcher["id"], reason=reason)
        for source in sorted(matches, key=lambda item: str(item.tag_id))
    )
    selected_id = selections.get(key)
    selected = next((item for item in matches if item.tag_id == selected_id), None)
    selection_source = "engineer_selection" if selected is not None else "unique_match"
    code: str
    if selected_id is not None and selected is None:
        code = "ENTITY_BINDING_SELECTION_INVALID"
    elif selected is None and not matches:
        code = "ENTITY_BINDING_MISSING"
    elif selected is None and len(matches) > 1:
        code = "ENTITY_BINDING_AMBIGUOUS"
    else:
        selected = selected or matches[0]
        code = _compatibility_code(definition, selected)

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
        "candidates": candidates,
        "selected_tag_id": str(selected.tag_id) if ready else None,
        "selection_source": selection_source if ready else None,
        "binding_id": str(binding_id) if binding_id else None,
        "confirmation_audit_id": str(audit_id) if audit_id else None,
        "data_type": definition["data_type"],
        "unit": definition.get("unit"),
        "direction": definition["direction"],
        "freshness_seconds": float(slot["freshness_seconds"]),
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
        ) or definition["data_type"] not in {"FLOAT", "INT", "BOOL", "STRING", "ENUM"} \
                or definition["direction"] not in {"R", "W", "RW"}:
            raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity definition is invalid")
        if definition["id"] in definition_ids:
            raise EntityInstanceError("ENTITY_SLOT_INVALID", "Entity definition ids must be unique")
        definition_ids.add(definition["id"])
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
