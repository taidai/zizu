"""Deterministic planning and application of installed L1 point-processing rules."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.data_trunk_contracts import (
    EnumTransform,
    FaultCodeTransform,
    InputReference,
    InstalledPointProcessing,
    NumericTransform,
    ValueKind,
)
from app.services.solution_point_processings import PointProcessingAsset


class PointProcessingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PointProcessingSource:
    source_id: UUID
    source_kind: str
    node_id: UUID | None
    stable_source_key: str
    data_type: str
    unit: str | None
    confirmed: bool


@dataclass(frozen=True)
class CurrentPointProcessingContext:
    entity_identity_installation_id: UUID
    solution_installation_id: UUID
    revision_id: UUID
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
class PreviewPointProcessing:
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
class ApplyPointProcessingPlan:
    plan_id: UUID
    plan_digest: str
    idempotency_key: str
    actor: str


@dataclass(frozen=True)
class PointProcessingPlan:
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

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "node_id": str(self.node_id),
            "template_revision_id": str(self.template_revision_id),
            "entity_identity_installation_id": str(
                self.entity_identity_installation_id
            ),
            "solution_installation_id": str(self.solution_installation_id),
            "base_site_configuration_version": self.base_site_configuration_version,
            "source_catalog_digest": self.source_catalog_digest,
            "status": self.status,
            "items": [_plain(item) for item in self.items],
            "blockers": [_plain(item) for item in self.blockers],
            "digest": self.digest,
        }


@dataclass(frozen=True)
class PointProcessingApplication:
    id: UUID
    plan_id: UUID
    installed_processing_id: UUID
    solution_installation_id: UUID
    revision_id: UUID
    site_configuration_version: int
    output_entity_instance_ids: tuple[UUID, ...]
    actor: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "plan_id": str(self.plan_id),
            "installed_processing_id": str(self.installed_processing_id),
            "solution_installation_id": str(self.solution_installation_id),
            "revision_id": str(self.revision_id),
            "site_configuration_version": self.site_configuration_version,
            "output_entity_instance_ids": [
                str(item) for item in self.output_entity_instance_ids
            ],
        }


@dataclass(frozen=True)
class PointProcessingTemplateSummary:
    revision_id: UUID
    asset: PointProcessingAsset

    def public_dict(self) -> dict[str, Any]:
        return {
            "revision_id": str(self.revision_id),
            "asset_id": self.asset.asset_id,
            "display_name": self.asset.display_name,
            "device_category": self.asset.device_category,
            "brand": self.asset.brand,
            "model": self.asset.model,
            "revision": self.asset.revision,
            "status": self.asset.status,
            "content_digest": self.asset.content_digest,
            "inputs": [
                {
                    "input_id": item.input_id,
                    "source_kind": item.source_kind,
                    "source_key": item.source_key,
                    "aliases": list(item.aliases),
                    "data_type": item.data_type,
                    "unit": item.unit,
                    "required": item.required,
                }
                for item in self.asset.inputs
            ],
            "outputs": [
                {
                    "output_key": item.output_id,
                    "entity_definition_id": item.entity_definition_id,
                    "data_type": item.data_type,
                    "unit": item.unit,
                }
                for item in self.asset.outputs
            ],
        }


@dataclass(frozen=True)
class NodeDataTrunkView:
    node_id: UUID
    l0: tuple[Mapping[str, Any], ...]
    l1_summary: Mapping[str, Any]
    l2: tuple[Mapping[str, Any], ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "node_id": str(self.node_id),
            "l0": [_plain(item) for item in self.l0],
            "l1_summary": _plain(self.l1_summary),
            "l2": [_plain(item) for item in self.l2],
        }


class PointProcessingCatalog(Protocol):
    def get_template(self, revision_id: UUID) -> PointProcessingAsset | None: ...

    def list_sources(self, node_id: UUID) -> tuple[PointProcessingSource, ...]: ...

    def list_templates(
        self,
        device_category: str,
    ) -> tuple[PointProcessingTemplateSummary, ...]: ...

    def node_source_key(self, node_id: UUID) -> str | None: ...


class PointProcessingRepository(Protocol):
    def site_configuration_version(self) -> int: ...

    def current_context(
        self,
        node_id: UUID,
    ) -> CurrentPointProcessingContext | None: ...

    def save_plan(self, plan: PointProcessingPlan) -> PointProcessingPlan: ...

    def get_plan(self, plan_id: UUID) -> PointProcessingPlan | None: ...

    def apply_plan(
        self,
        command: ApplyPointProcessingPlan,
        catalog: PointProcessingCatalog,
        *,
        transaction: Any | None = None,
    ) -> PointProcessingApplication: ...


class PointProcessingDelivery:
    """Hide deterministic matching, stable L2 identity and apply preconditions."""

    def __init__(
        self,
        repository: PointProcessingRepository,
        catalog: PointProcessingCatalog,
    ) -> None:
        self._repository = repository
        self._catalog = catalog

    def preview(self, command: PreviewPointProcessing) -> PointProcessingPlan:
        plan = compile_point_processing_plan(
            command,
            self._catalog,
            self._repository,
        )
        return self._repository.save_plan(plan)

    def list_templates(
        self,
        device_category: str,
    ) -> tuple[PointProcessingTemplateSummary, ...]:
        return self._catalog.list_templates(device_category)

    def node_source_key(self, node_id: UUID) -> str | None:
        return self._catalog.node_source_key(node_id)

    def inspect(
        self,
        node_id: UUID,
        *,
        include_engineering: bool,
    ) -> NodeDataTrunkView:
        sources = self._catalog.list_sources(node_id)
        current = self._repository.current_context(node_id)
        l0 = (
            tuple(
                MappingProxyType(
                    {
                        "source_id": str(item.source_id),
                        "source_key": item.stable_source_key,
                        "data_type": item.data_type,
                        "unit": item.unit,
                    }
                )
                for item in sources
                if item.source_kind == "l0"
            )
            if include_engineering
            else ()
        )
        input_bindings = (
            {
                key: str(value)
                for key, value in sorted(current.input_source_ids.items())
            }
            if current is not None
            else {}
        )
        l1_summary = {
            "installed": current is not None,
            "revision_id": str(current.revision_id) if current is not None else None,
            "output_count": len(current.output_entity_ids) if current is not None else 0,
            **({"input_bindings": input_bindings} if include_engineering else {}),
        }
        l2 = (
            tuple(
                MappingProxyType(
                    {
                        "output_key": key,
                        "entity_instance_id": str(value),
                    }
                )
                for key, value in sorted(current.output_entity_ids.items())
            )
            if current is not None
            else ()
        )
        return NodeDataTrunkView(
            node_id=node_id,
            l0=l0,
            l1_summary=MappingProxyType(l1_summary),
            l2=l2,
        )

    def get_plan(self, plan_id: UUID) -> PointProcessingPlan:
        plan = self._repository.get_plan(plan_id)
        if plan is None:
            raise PointProcessingError(
                "POINT_PROCESSING_PLAN_NOT_FOUND",
                "Point processing plan was not found",
            )
        return plan

    def apply(
        self,
        command: ApplyPointProcessingPlan,
        *,
        transaction: Any | None = None,
    ) -> PointProcessingApplication:
        return self._repository.apply_plan(
            command,
            self._catalog,
            transaction=transaction,
        )


class InMemoryPointProcessingCatalog:
    def __init__(
        self,
        *,
        templates: Mapping[UUID, PointProcessingAsset],
        sources: tuple[PointProcessingSource, ...] = (),
        node_source_keys: Mapping[UUID, str] | None = None,
    ) -> None:
        self._templates = dict(templates)
        self._sources = tuple(sources)
        self._node_source_keys = dict(node_source_keys or {})

    def get_template(self, revision_id: UUID) -> PointProcessingAsset | None:
        return self._templates.get(revision_id)

    def list_sources(self, node_id: UUID) -> tuple[PointProcessingSource, ...]:
        return tuple(
            item
            for item in self._sources
            if item.source_kind == "l2" or item.node_id == node_id
        )

    def list_templates(
        self,
        device_category: str,
    ) -> tuple[PointProcessingTemplateSummary, ...]:
        return tuple(
            PointProcessingTemplateSummary(revision_id, asset)
            for revision_id, asset in sorted(
                self._templates.items(),
                key=lambda item: (item[1].asset_id, item[1].revision, str(item[0])),
            )
            if asset.device_category == device_category and asset.status == "active"
        )

    def node_source_key(self, node_id: UUID) -> str | None:
        return self._node_source_keys.get(node_id)

    def replace_sources(self, sources: tuple[PointProcessingSource, ...]) -> None:
        self._sources = tuple(sources)


class InMemoryPointProcessingRepository:
    def __init__(
        self,
        *,
        on_applied: Callable[[PointProcessingApplication], None] | None = None,
    ) -> None:
        self._plans: dict[UUID, PointProcessingPlan] = {}
        self._applications: dict[UUID, PointProcessingApplication] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, UUID]] = {}
        self._current: dict[UUID, CurrentPointProcessingContext] = {}
        self._installed_ids: dict[UUID, UUID] = {}
        self._site_version = 0
        self._lock = RLock()
        self._on_applied = on_applied or (lambda _application: None)

    def site_configuration_version(self) -> int:
        return self._site_version

    def current_context(
        self,
        node_id: UUID,
    ) -> CurrentPointProcessingContext | None:
        return self._current.get(node_id)

    def save_plan(self, plan: PointProcessingPlan) -> PointProcessingPlan:
        existing = self._plans.get(plan.id)
        if existing is not None and existing != plan:
            raise PointProcessingError(
                "POINT_PROCESSING_PLAN_CONFLICT",
                "Point processing plan identity conflicts with stored evidence",
            )
        self._plans[plan.id] = plan
        return plan

    def get_plan(self, plan_id: UUID) -> PointProcessingPlan | None:
        return self._plans.get(plan_id)

    def application_count(self) -> int:
        return len(self._applications)

    def installed_processings(
        self,
        catalog: PointProcessingCatalog,
    ) -> tuple[InstalledPointProcessing, ...]:
        installed: list[InstalledPointProcessing] = []
        for node_id, current in sorted(self._current.items(), key=lambda item: str(item[0])):
            asset = catalog.get_template(current.revision_id)
            installation_id = self._installed_ids.get(node_id)
            if asset is None or installation_id is None:
                continue
            inputs = {item.input_id: item for item in asset.inputs}
            for output in asset.outputs:
                input_id = str(output.transform["input"])
                input_contract = inputs[input_id]
                input_source_id = current.input_source_ids[input_id]
                input_reference = InputReference(
                    input_contract.source_kind,
                    input_source_id,
                )
                kind = output.transform["kind"]
                if kind == "numeric":
                    transform = NumericTransform(
                        input=input_reference,
                        scale=float(output.transform["scale"]),
                        offset=float(output.transform["offset"]),
                        input_unit=input_contract.unit,
                        minimum=float(output.transform["minimum"]),
                        maximum=float(output.transform["maximum"]),
                    )
                elif kind == "enum":
                    transform = EnumTransform(
                        input=input_reference,
                        entries=dict(output.transform["entries"]),
                    )
                else:
                    transform = FaultCodeTransform(
                        input=input_reference,
                        delimiter=str(output.transform["delimiter"]),
                        entries={
                            raw_code: str(entry["code"])
                            for raw_code, entry in output.transform["entries"].items()
                        },
                    )
                installed.append(
                    InstalledPointProcessing(
                        installation_id=installation_id,
                        revision_id=current.revision_id,
                        entity_instance_id=current.output_entity_ids[
                            output.output_id
                        ],
                        entity_definition_id=output.entity_definition_id,
                        output_kind=ValueKind(output.data_type),
                        output_unit=output.unit,
                        freshness_seconds=output.freshness_seconds,
                        transform=transform,
                    )
                )
        return tuple(sorted(installed, key=lambda item: str(item.entity_instance_id)))

    def apply_plan(
        self,
        command: ApplyPointProcessingPlan,
        catalog: PointProcessingCatalog,
        *,
        transaction: Any | None = None,
    ) -> PointProcessingApplication:
        del transaction
        if not command.actor.strip() or not command.idempotency_key.strip():
            raise PointProcessingError(
                "POINT_PROCESSING_APPLY_INVALID",
                "Point processing apply actor and idempotency key are required",
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
                    raise PointProcessingError(
                        "POINT_PROCESSING_IDEMPOTENCY_KEY_REUSED",
                        "Idempotency key was already used for a different request",
                    )
                return self._applications[application_id]

            plan = self._plans.get(command.plan_id)
            if plan is None:
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_NOT_FOUND",
                    "Point processing plan was not found",
                )
            if plan.digest != command.plan_digest:
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_DIGEST_MISMATCH",
                    "Point processing plan digest does not match",
                )
            if plan.status != "ready" or plan.blockers:
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_BLOCKED",
                    "Point processing plan is not ready",
                )
            template = catalog.get_template(plan.template_revision_id)
            if template is None or template.status != "active":
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_STALE",
                    "Point processing template changed after planning",
                )
            if (
                plan.base_site_configuration_version != self._site_version
                or plan.source_catalog_digest
                != _template_source_catalog_digest(
                    template,
                    catalog.list_sources(plan.node_id),
                    plan.node_id,
                )
            ):
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_STALE",
                    "Point processing sources or site configuration changed after planning",
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
                f"zizu/installed-point-processing/{plan.id}",
            )
            application_id = uuid5(
                NAMESPACE_URL,
                f"zizu/point-processing-application/{command.actor}/{command.idempotency_key}",
            )
            application = PointProcessingApplication(
                id=application_id,
                plan_id=plan.id,
                installed_processing_id=installed_id,
                solution_installation_id=solution_id,
                revision_id=plan.template_revision_id,
                site_configuration_version=next_version,
                output_entity_instance_ids=tuple(
                    sorted(output_ids.values(), key=str)
                ),
                actor=command.actor,
            )
            self._on_applied(application)
            self._applications[application.id] = application
            self._idempotency[key] = (request_digest, application.id)
            self._current[plan.node_id] = CurrentPointProcessingContext(
                entity_identity_installation_id=plan.entity_identity_installation_id,
                solution_installation_id=solution_id,
                revision_id=plan.template_revision_id,
                input_source_ids=input_ids,
                output_entity_ids=output_ids,
            )
            self._installed_ids[plan.node_id] = installed_id
            self._site_version = next_version
            self._plans[plan.id] = replace(plan, status="applied")
            return application


def compile_point_processing_plan(
    command: PreviewPointProcessing,
    catalog: PointProcessingCatalog,
    repository: PointProcessingRepository,
) -> PointProcessingPlan:
    if not command.actor.strip():
        raise PointProcessingError(
            "POINT_PROCESSING_ACTOR_INVALID",
            "Point processing plan actor is required",
        )
    template = catalog.get_template(command.template_revision_id)
    if template is None:
        raise PointProcessingError(
            "POINT_PROCESSING_TEMPLATE_NOT_FOUND",
            "Point processing template revision was not found",
        )
    if template.status != "active":
        raise PointProcessingError(
            "POINT_PROCESSING_TEMPLATE_RETIRED",
            "Retired point processing revisions cannot be selected for a new plan",
        )
    current = repository.current_context(command.node_id)
    identity_id = command.entity_identity_installation_id or (
        current.entity_identity_installation_id if current is not None else None
    )
    solution_id = command.solution_installation_id or (
        current.solution_installation_id if current is not None else None
    )
    if identity_id is None or solution_id is None:
        raise PointProcessingError(
            "POINT_PROCESSING_INSTALLATION_CONTEXT_REQUIRED",
            "Entity identity and solution installation context are required",
        )

    sources = catalog.list_sources(command.node_id)
    source_digest = _template_source_catalog_digest(
        template,
        sources,
        command.node_id,
    )
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
            code = "POINT_PROCESSING_INPUT_SELECTION_INVALID"
        elif not candidates and input_contract.required:
            code = "POINT_PROCESSING_INPUT_MISSING"
        elif selected is None and len(candidates) > 1:
            code = "POINT_PROCESSING_INPUT_AMBIGUOUS"
        elif selected is None and candidates:
            selected = candidates[0]
        if selected is not None and (
            selected.data_type != input_contract.data_type
            or (selected.unit or None) != (input_contract.unit or None)
            or (selected.source_kind == "l2" and not selected.confirmed)
        ):
            code = "POINT_PROCESSING_INPUT_INCOMPATIBLE"
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
    target_output_contract = {
        item.output_id: item.entity_definition_id for item in template.outputs
    }
    if current is not None:
        current_template = catalog.get_template(current.revision_id)
        current_output_contract = (
            {
                item.output_id: item.entity_definition_id
                for item in current_template.outputs
            }
            if current_template is not None
            else {}
        )
        if current_output_contract != target_output_contract:
            blockers.append(
                MappingProxyType(
                    {
                        "code": "POINT_PROCESSING_OUTPUT_CONTRACT_MISMATCH",
                        "input_id": "outputs",
                    }
                )
            )
    elif requested_outputs and set(requested_outputs) != set(target_output_contract):
        blockers.append(
            MappingProxyType(
                {
                    "code": "POINT_PROCESSING_OUTPUT_CONTRACT_MISMATCH",
                    "input_id": "outputs",
                }
            )
        )
    output_ids: dict[str, UUID] = {}
    for output in template.outputs:
        expected_id = current_outputs.get(
            output.output_id,
            requested_outputs.get(
                output.output_id,
                _stable_output_entity_id(
                    identity_id,
                    command.node_id,
                    output.entity_definition_id,
                ),
            ),
        )
        entity_id = expected_id
        if current is not None and output.output_id not in current_outputs:
            blockers.append(
                MappingProxyType(
                    {
                        "code": "POINT_PROCESSING_OUTPUT_CONTRACT_MISMATCH",
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
    return PointProcessingPlan(
        id=uuid5(NAMESPACE_URL, f"zizu/point-processing-plan/{digest}"),
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


def _source_catalog_digest(sources: tuple[PointProcessingSource, ...]) -> str:
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


def _template_source_catalog_digest(
    template: PointProcessingAsset,
    sources: tuple[PointProcessingSource, ...],
    node_id: UUID,
) -> str:
    relevant = {
        candidate.source_id: candidate
        for input_contract in template.inputs
        for candidate in _input_candidates(input_contract, sources, node_id)
    }
    return _source_catalog_digest(
        tuple(relevant[key] for key in sorted(relevant, key=str))
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
