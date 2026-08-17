"""Deterministic planning and application of installed L1 conversions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.solution_point_conversions import PointConversionAsset


class PointConversionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PointConversionSource:
    source_id: UUID
    source_kind: str
    node_id: UUID | None
    stable_source_key: str
    data_type: str
    unit: str | None
    confirmed: bool


@dataclass(frozen=True)
class CurrentPointConversionContext:
    entity_identity_installation_id: UUID
    solution_installation_id: UUID
    input_source_ids: Mapping[str, UUID]
    output_entity_ids: Mapping[str, UUID]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_source_ids",
            MappingProxyType(dict(self.input_source_ids)),
        )
        object.__setattr__(
            self,
            "output_entity_ids",
            MappingProxyType(dict(self.output_entity_ids)),
        )


@dataclass(frozen=True)
class PlanPointConversion:
    node_id: UUID
    template_revision_id: UUID
    input_selections: Mapping[str, UUID]
    actor: str
    entity_identity_installation_id: UUID | None = None
    planned_output_entity_ids: Mapping[str, UUID] = field(default_factory=dict)
    solution_installation_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_selections",
            MappingProxyType(dict(self.input_selections)),
        )
        object.__setattr__(
            self,
            "planned_output_entity_ids",
            MappingProxyType(dict(self.planned_output_entity_ids)),
        )


@dataclass(frozen=True)
class ApplyPointConversionPlan:
    plan_id: UUID
    plan_digest: str
    idempotency_key: str
    actor: str


@dataclass(frozen=True)
class PointConversionPlan:
    id: UUID
    node_id: UUID
    template_revision_id: UUID
    entity_identity_installation_id: UUID
    solution_installation_id: UUID
    base_site_configuration_version: int
    source_catalog_digest: str
    status: str
    items: tuple[Mapping[str, Any], ...]
    blockers: tuple[Mapping[str, str], ...]
    digest: str
    planned_by: str


@dataclass(frozen=True)
class PointConversionApplication:
    id: UUID
    plan_id: UUID
    installed_conversion_id: UUID
    solution_installation_id: UUID
    revision_id: UUID
    site_configuration_version: int
    output_entity_instance_ids: tuple[UUID, ...]
    actor: str


class PointConversionCatalog(Protocol):
    def get_template(self, revision_id: UUID) -> PointConversionAsset | None: ...

    def list_sources(self, node_id: UUID) -> tuple[PointConversionSource, ...]: ...


class PointConversionRepository(Protocol):
    def site_configuration_version(self) -> int: ...

    def current_context(
        self,
        node_id: UUID,
    ) -> CurrentPointConversionContext | None: ...

    def save_plan(self, plan: PointConversionPlan) -> PointConversionPlan: ...

    def get_plan(self, plan_id: UUID) -> PointConversionPlan | None: ...

    def apply_plan(
        self,
        command: ApplyPointConversionPlan,
        catalog: PointConversionCatalog,
        *,
        transaction: Any | None = None,
    ) -> PointConversionApplication: ...


class PointConversion:
    """Hide deterministic matching, stable L2 identity and apply preconditions."""

    def __init__(
        self,
        repository: PointConversionRepository,
        catalog: PointConversionCatalog,
    ) -> None:
        self._repository = repository
        self._catalog = catalog

    def plan(self, command: PlanPointConversion) -> PointConversionPlan:
        plan = compile_point_conversion_plan(
            command,
            self._catalog,
            self._repository,
        )
        return self._repository.save_plan(plan)

    def get_plan(self, plan_id: UUID) -> PointConversionPlan:
        plan = self._repository.get_plan(plan_id)
        if plan is None:
            raise PointConversionError(
                "POINT_CONVERSION_PLAN_NOT_FOUND",
                "Point conversion plan was not found",
            )
        return plan

    def apply(
        self,
        command: ApplyPointConversionPlan,
        *,
        transaction: Any | None = None,
    ) -> PointConversionApplication:
        return self._repository.apply_plan(
            command,
            self._catalog,
            transaction=transaction,
        )


class InMemoryPointConversionCatalog:
    def __init__(
        self,
        *,
        templates: Mapping[UUID, PointConversionAsset],
        sources: tuple[PointConversionSource, ...] = (),
    ) -> None:
        self._templates = dict(templates)
        self._sources = tuple(sources)

    def get_template(self, revision_id: UUID) -> PointConversionAsset | None:
        return self._templates.get(revision_id)

    def list_sources(self, node_id: UUID) -> tuple[PointConversionSource, ...]:
        return tuple(
            item
            for item in self._sources
            if item.source_kind == "l2" or item.node_id == node_id
        )

    def replace_sources(self, sources: tuple[PointConversionSource, ...]) -> None:
        self._sources = tuple(sources)


class InMemoryPointConversionRepository:
    def __init__(self) -> None:
        self._plans: dict[UUID, PointConversionPlan] = {}
        self._applications: dict[UUID, PointConversionApplication] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, UUID]] = {}
        self._current: dict[UUID, CurrentPointConversionContext] = {}
        self._site_version = 0
        self._lock = RLock()

    def site_configuration_version(self) -> int:
        return self._site_version

    def current_context(
        self,
        node_id: UUID,
    ) -> CurrentPointConversionContext | None:
        return self._current.get(node_id)

    def save_plan(self, plan: PointConversionPlan) -> PointConversionPlan:
        existing = self._plans.get(plan.id)
        if existing is not None and existing != plan:
            raise PointConversionError(
                "POINT_CONVERSION_PLAN_CONFLICT",
                "Point conversion plan identity conflicts with stored evidence",
            )
        self._plans[plan.id] = plan
        return plan

    def get_plan(self, plan_id: UUID) -> PointConversionPlan | None:
        return self._plans.get(plan_id)

    def application_count(self) -> int:
        return len(self._applications)

    def apply_plan(
        self,
        command: ApplyPointConversionPlan,
        catalog: PointConversionCatalog,
        *,
        transaction: Any | None = None,
    ) -> PointConversionApplication:
        del transaction
        if not command.actor.strip() or not command.idempotency_key.strip():
            raise PointConversionError(
                "POINT_CONVERSION_APPLY_INVALID",
                "Point conversion apply actor and idempotency key are required",
            )
        request_digest = _digest(
            {
                "plan_id": str(command.plan_id),
                "plan_digest": command.plan_digest,
            }
        )
        key = (command.actor, command.idempotency_key)
        with self._lock:
            existing_binding = self._idempotency.get(key)
            if existing_binding is not None:
                stored_digest, application_id = existing_binding
                if stored_digest != request_digest:
                    raise PointConversionError(
                        "POINT_CONVERSION_IDEMPOTENCY_KEY_REUSED",
                        "Idempotency key was already used for a different request",
                    )
                return self._applications[application_id]

            plan = self._plans.get(command.plan_id)
            if plan is None:
                raise PointConversionError(
                    "POINT_CONVERSION_PLAN_NOT_FOUND",
                    "Point conversion plan was not found",
                )
            if plan.digest != command.plan_digest:
                raise PointConversionError(
                    "POINT_CONVERSION_PLAN_DIGEST_MISMATCH",
                    "Point conversion plan digest does not match",
                )
            if plan.status != "ready" or plan.blockers:
                raise PointConversionError(
                    "POINT_CONVERSION_PLAN_BLOCKED",
                    "Point conversion plan is not ready",
                )
            if (
                plan.base_site_configuration_version != self._site_version
                or plan.source_catalog_digest
                != _source_catalog_digest(catalog.list_sources(plan.node_id))
            ):
                raise PointConversionError(
                    "POINT_CONVERSION_PLAN_STALE",
                    "Point conversion sources or site configuration changed after planning",
                )
            template = catalog.get_template(plan.template_revision_id)
            if template is None or template.status != "active":
                raise PointConversionError(
                    "POINT_CONVERSION_PLAN_STALE",
                    "Point conversion template changed after planning",
                )

            output_ids = {
                item["output_id"]: UUID(item["output_entity_instance_id"])
                for item in plan.items
                if item.get("kind") == "output_binding"
                and item.get("action") != "block"
            }
            input_ids = {
                item["input_id"]: UUID(item["selected_source_id"])
                for item in plan.items
                if item.get("kind") == "input_binding"
                and item.get("action") != "block"
                and item.get("selected_source_id") is not None
            }
            current = self._current.get(plan.node_id)
            solution_id = (
                plan.solution_installation_id
                if current is None
                else uuid5(
                    NAMESPACE_URL,
                    f"zizu/derived-solution/{plan.solution_installation_id}/{plan.digest}",
                )
            )
            next_version = self._site_version + 1
            installed_id = uuid5(
                NAMESPACE_URL,
                f"zizu/installed-point-conversion/{plan.id}",
            )
            application_id = uuid5(
                NAMESPACE_URL,
                f"zizu/point-conversion-application/{command.actor}/{command.idempotency_key}",
            )
            application = PointConversionApplication(
                id=application_id,
                plan_id=plan.id,
                installed_conversion_id=installed_id,
                solution_installation_id=solution_id,
                revision_id=plan.template_revision_id,
                site_configuration_version=next_version,
                output_entity_instance_ids=tuple(
                    sorted(output_ids.values(), key=str)
                ),
                actor=command.actor,
            )
            self._applications[application.id] = application
            self._idempotency[key] = (request_digest, application.id)
            self._current[plan.node_id] = CurrentPointConversionContext(
                entity_identity_installation_id=plan.entity_identity_installation_id,
                solution_installation_id=solution_id,
                input_source_ids=input_ids,
                output_entity_ids=output_ids,
            )
            self._site_version = next_version
            self._plans[plan.id] = replace(plan, status="applied")
            return application


def compile_point_conversion_plan(
    command: PlanPointConversion,
    catalog: PointConversionCatalog,
    repository: PointConversionRepository,
) -> PointConversionPlan:
    if not command.actor.strip():
        raise PointConversionError(
            "POINT_CONVERSION_ACTOR_INVALID",
            "Point conversion plan actor is required",
        )
    template = catalog.get_template(command.template_revision_id)
    if template is None:
        raise PointConversionError(
            "POINT_CONVERSION_TEMPLATE_NOT_FOUND",
            "Point conversion template revision was not found",
        )
    if template.status != "active":
        raise PointConversionError(
            "POINT_CONVERSION_TEMPLATE_RETIRED",
            "Retired point conversion revisions cannot be selected for a new plan",
        )
    current = repository.current_context(command.node_id)
    identity_id = command.entity_identity_installation_id or (
        current.entity_identity_installation_id if current is not None else None
    )
    solution_id = command.solution_installation_id or (
        current.solution_installation_id if current is not None else None
    )
    if identity_id is None or solution_id is None:
        raise PointConversionError(
            "POINT_CONVERSION_INSTALLATION_CONTEXT_REQUIRED",
            "Entity identity and solution installation context are required",
        )

    sources = catalog.list_sources(command.node_id)
    source_digest = _source_catalog_digest(sources)
    items: list[Mapping[str, Any]] = []
    blockers: list[Mapping[str, str]] = []
    selected_inputs: dict[str, UUID] = {}
    for input_contract in template.inputs:
        candidates = _input_candidates(input_contract, sources, command.node_id)
        selected_id = command.input_selections.get(input_contract.input_id)
        selected = next(
            (item for item in candidates if item.source_id == selected_id),
            None,
        )
        code: str | None = None
        if selected_id is not None and selected is None:
            code = "POINT_CONVERSION_INPUT_SELECTION_INVALID"
        elif not candidates and input_contract.required:
            code = "POINT_CONVERSION_INPUT_MISSING"
        elif selected is None and len(candidates) > 1:
            code = "POINT_CONVERSION_INPUT_AMBIGUOUS"
        elif selected is None and candidates:
            selected = candidates[0]
        if selected is not None and (
            selected.data_type != input_contract.data_type
            or (selected.unit or None) != (input_contract.unit or None)
            or (selected.source_kind == "l2" and not selected.confirmed)
        ):
            code = "POINT_CONVERSION_INPUT_INCOMPATIBLE"
            selected = None
        if code is not None:
            blocker = MappingProxyType(
                {
                    "code": code,
                    "input_id": input_contract.input_id,
                }
            )
            blockers.append(blocker)
        elif selected is not None:
            selected_inputs[input_contract.input_id] = selected.source_id
        if code is not None:
            action = "block"
        elif selected is not None and current is not None:
            action = (
                "preserve"
                if current.input_source_ids.get(input_contract.input_id)
                == selected.source_id
                else "update"
            )
        else:
            action = "add"
        items.append(
            MappingProxyType(
                {
                    "item_key": f"input:{input_contract.input_id}",
                    "kind": "input_binding",
                    "action": action,
                    "input_id": input_contract.input_id,
                    "candidate_source_ids": tuple(
                        str(item.source_id) for item in candidates
                    ),
                    "selected_source_id": (
                        str(selected.source_id) if selected is not None else None
                    ),
                    "blocker_code": code,
                }
            )
        )

    current_outputs = dict(current.output_entity_ids) if current is not None else {}
    requested_outputs = dict(command.planned_output_entity_ids)
    output_ids: dict[str, UUID] = {}
    for output in template.outputs:
        expected_id = _stable_output_entity_id(
            identity_id,
            command.node_id,
            output.entity_definition_id,
        )
        entity_id = requested_outputs.get(
            output.output_id,
            current_outputs.get(output.output_id, expected_id),
        )
        if entity_id != expected_id:
            blockers.append(
                MappingProxyType(
                    {
                        "code": "POINT_CONVERSION_OUTPUT_ID_INVALID",
                        "input_id": output.output_id,
                    }
                )
            )
            action = "block"
        else:
            action = "preserve" if output.output_id in current_outputs else "add"
        output_ids[output.output_id] = entity_id
        items.append(
            MappingProxyType(
                {
                    "item_key": f"output:{output.output_id}",
                    "kind": "output_binding",
                    "action": action,
                    "output_id": output.output_id,
                    "entity_definition_id": output.entity_definition_id,
                    "output_entity_instance_id": str(entity_id),
                }
            )
        )

    content = {
        "node_id": str(command.node_id),
        "template_revision_id": str(command.template_revision_id),
        "template_digest": template.content_digest,
        "entity_identity_installation_id": str(identity_id),
        "solution_installation_id": str(solution_id),
        "base_site_configuration_version": repository.site_configuration_version(),
        "source_catalog_digest": source_digest,
        "selected_inputs": {key: str(value) for key, value in sorted(selected_inputs.items())},
        "output_entity_ids": {key: str(value) for key, value in sorted(output_ids.items())},
        "items": [_plain(item) for item in items],
        "blockers": [_plain(item) for item in blockers],
        "planned_by": command.actor,
    }
    digest = _digest(content)
    return PointConversionPlan(
        id=uuid5(NAMESPACE_URL, f"zizu/point-conversion-plan/{digest}"),
        node_id=command.node_id,
        template_revision_id=command.template_revision_id,
        entity_identity_installation_id=identity_id,
        solution_installation_id=solution_id,
        base_site_configuration_version=repository.site_configuration_version(),
        source_catalog_digest=source_digest,
        status="blocked" if blockers else "ready",
        items=tuple(items),
        blockers=tuple(blockers),
        digest=digest,
        planned_by=command.actor,
    )


def _input_candidates(input_contract, sources, node_id: UUID):
    compatible_scope = tuple(
        source
        for source in sources
        if source.source_kind == input_contract.source_kind
        and (source.source_kind != "l0" or source.node_id == node_id)
        and (source.source_kind != "l2" or source.confirmed)
    )
    exact = tuple(
        source
        for source in compatible_scope
        if source.stable_source_key.casefold() == input_contract.source_key.casefold()
    )
    if exact:
        return tuple(sorted(exact, key=lambda item: str(item.source_id)))
    aliases = {item.casefold() for item in input_contract.aliases}
    return tuple(
        sorted(
            (
                source
                for source in compatible_scope
                if source.stable_source_key.casefold() in aliases
            ),
            key=lambda item: str(item.source_id),
        )
    )


def _stable_output_entity_id(
    identity_installation_id: UUID,
    node_id: UUID,
    definition_id: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"zizu/entity/{identity_installation_id}/{node_id}/{definition_id}",
    )


def _source_catalog_digest(sources: tuple[PointConversionSource, ...]) -> str:
    return _digest(
        [
            {
                **asdict(source),
                "source_id": str(source.source_id),
                "node_id": str(source.node_id) if source.node_id else None,
            }
            for source in sorted(sources, key=lambda item: str(item.source_id))
        ]
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _digest(value: Any) -> str:
    canonical = json.dumps(
        _plain(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
