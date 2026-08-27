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
    BooleanCodeInput,
    BooleanSetTransform,
    CompiledFormula,
    DataTrunkError,
    EnumTransform,
    FaultCodeTransform,
    FormulaSource,
    FormulaTransform,
    InputReference,
    InstalledPointProcessing,
    NumericTransform,
    ValueKind,
)
from app.services.point_processing_templates import PointProcessingTemplate
from app.services.point_processing_dag import (
    PointProcessingDagError,
    validate_processing_dag,
)
from app.services.point_processing_selectors import (
    FrozenSelection,
    PointProcessingSelectorError,
    Selector,
    freeze_selector,
)
from app.services.point_processing_formula import FormulaCompileError, compile_formula
from app.services.neuron_point_processing_catalog import (
    ScannedPoint,
    ScannedPointCatalog,
)


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
    revision_id: UUID
    input_source_ids: Mapping[str, UUID]
    output_entity_ids: Mapping[str, UUID]
    selector_source_ids: Mapping[str, tuple[UUID, ...]] = field(default_factory=dict)

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
        object.__setattr__(
            self,
            "selector_source_ids",
            MappingProxyType(
                {
                    key: tuple(sorted(values, key=str))
                    for key, values in self.selector_source_ids.items()
                }
            ),
        )


@dataclass(frozen=True)
class PreviewPointProcessing:
    node_id: UUID
    template_revision_id: UUID
    input_selections: Mapping[str, UUID]
    actor: str
    planned_output_entity_ids: Mapping[str, UUID] = field(default_factory=dict)

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
    base_configuration_revision: int
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
            "base_configuration_revision": self.base_configuration_revision,
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
    revision_id: UUID
    configuration_revision: int
    output_entity_instance_ids: tuple[UUID, ...]
    actor: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "plan_id": str(self.plan_id),
            "installed_processing_id": str(self.installed_processing_id),
            "revision_id": str(self.revision_id),
            "configuration_revision": self.configuration_revision,
            "output_entity_instance_ids": [
                str(item) for item in self.output_entity_instance_ids
            ],
        }


@dataclass(frozen=True)
class PointProcessingTemplateSummary:
    revision_id: UUID
    asset: PointProcessingTemplate

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
                    "cardinality": item.cardinality,
                    "selector": _plain(item.selector),
                    "default_value": item.default_value,
                }
                for item in self.asset.inputs
            ],
            "outputs": [
                {
                    "output_key": item.output_id,
                    "entity_definition_id": item.entity_definition_id,
                    "data_type": item.data_type,
                    "unit": item.unit,
                    "freshness_seconds": item.freshness_seconds,
                    "transform": _plain(item.transform),
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
    def get_template(self, revision_id: UUID) -> PointProcessingTemplate | None: ...

    def list_sources(self, node_id: UUID) -> tuple[PointProcessingSource, ...]: ...

    def list_templates(
        self,
        device_category: str,
    ) -> tuple[PointProcessingTemplateSummary, ...]: ...

    def node_source_key(self, node_id: UUID) -> str | None: ...

    def list_selector_members(
        self,
        target_node_id: UUID,
        selector: Selector,
    ) -> tuple[UUID, ...]: ...

    def dependency_edges(self) -> tuple[tuple[UUID, UUID], ...]: ...

    def record_dependencies(
        self,
        edges: tuple[tuple[UUID, UUID], ...],
    ) -> None: ...


class PointProcessingRepository(Protocol):
    def configuration_revision(self) -> int: ...

    def current_context(
        self,
        node_id: UUID,
    ) -> CurrentPointProcessingContext | None: ...

    def save_plan(
        self,
        plan: PointProcessingPlan,
        *,
        transaction: Any | None = None,
    ) -> PointProcessingPlan: ...

    def get_plan(self, plan_id: UUID) -> PointProcessingPlan | None: ...

    def apply_plan(
        self,
        command: ApplyPointProcessingPlan,
        catalog: PointProcessingCatalog,
        *,
        transaction: Any | None = None,
        verified_source_catalog_digest: str | None = None,
    ) -> PointProcessingApplication: ...


class PointScanner(Protocol):
    def scan(self, node_name: str) -> ScannedPointCatalog: ...


class PointProcessingService:
    """Hide deterministic matching, stable L2 identity and apply preconditions."""

    def __init__(
        self,
        repository: PointProcessingRepository,
        catalog: PointProcessingCatalog,
        *,
        point_scanner: PointScanner | None = None,
        runtime_gate: Any | None = None,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._point_scanner = point_scanner
        self._runtime_gate = runtime_gate

    def preview(self, command: PreviewPointProcessing) -> PointProcessingPlan:
        node_source_key = self._catalog.node_source_key(command.node_id)
        template = self._catalog.get_template(command.template_revision_id)
        scan = None
        if template is not None and template.asset_id == "pcs.en9":
            if self._point_scanner is None or node_source_key is None:
                raise PointProcessingError(
                    "NEURON_POINT_CATALOG_UNAVAILABLE",
                    "EN9 installation requires a readable Neuron point catalog",
                )
            try:
                scan = self._point_scanner.scan(node_source_key)
            except Exception as exc:
                raise PointProcessingError(
                    "NEURON_POINT_CATALOG_UNAVAILABLE",
                    "Neuron point catalog could not be scanned",
                ) from exc
        plan = compile_point_processing_plan(
            command,
            self._catalog,
            self._repository,
            scan=scan,
        )
        return self._repository.save_plan(plan)

    def preview_formula(
        self,
        *,
        node_id: UUID,
        template_revision_id: UUID,
        expression: str,
    ) -> Mapping[str, Any]:
        template = self._catalog.get_template(template_revision_id)
        if template is None or template.status != "active":
            raise PointProcessingError(
                "POINT_PROCESSING_TEMPLATE_NOT_FOUND",
                "Point processing template revision was not found",
            )
        outputs = tuple(
            item for item in template.outputs
            if item.transform.get("kind") == "formula"
        )
        if len(outputs) != 1:
            raise PointProcessingError(
                "POINT_PROCESSING_FORMULA_INVALID",
                "Formula preview requires exactly one formula output",
            )
        output = outputs[0]
        try:
            compiled = compile_formula(
                expression,
                sources=tuple(
                    FormulaSource(
                        item.input_id,
                        ValueKind(item.data_type),
                        item.unit,
                        item.cardinality,
                        item.required,
                        item.default_value,
                    )
                    for item in template.inputs
                ),
                result_type=ValueKind(output.data_type),
                result_unit=output.unit,
            )
        except (FormulaCompileError, ValueError) as exc:
            raise PointProcessingError(
                getattr(exc, "code", "POINT_PROCESSING_FORMULA_INVALID"),
                str(exc),
            ) from exc

        input_contracts = {item.input_id: item for item in template.inputs}
        referenced_inputs = _formula_input_names(compiled.ast)
        if any(input_contracts[name].source_kind != "l2" for name in referenced_inputs):
            raise PointProcessingError(
                "POINT_PROCESSING_FORMULA_INVALID",
                "Cross-node formulas may only reference L2 inputs",
            )
        current = self._repository.current_context(node_id)
        target_id = (
            current.output_entity_ids.get(output.output_id)
            if current is not None
            else None
        ) or uuid5(
            NAMESPACE_URL,
            f"zizu/formula-preview/{node_id}/{output.entity_definition_id}",
        )
        configuration_revision = self._repository.configuration_revision()
        sources = self._catalog.list_sources(node_id)
        selected: dict[str, tuple[UUID, ...]] = {}
        selector_previews: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        for input_id in referenced_inputs:
            contract = input_contracts[input_id]
            if contract.selector is not None:
                selector = Selector(
                    scope=str(contract.selector["scope"]),
                    node_type=str(contract.selector["nodeType"]),
                    entity_definition_id=str(
                        contract.selector["entityDefinition"]
                    ),
                    cardinality=contract.cardinality,
                )
                try:
                    frozen = freeze_selector(
                        selector=selector,
                        target_node_id=node_id,
                        configuration_revision=configuration_revision,
                        entity_instance_ids=self._catalog.list_selector_members(
                            node_id,
                            selector,
                        ),
                    )
                except PointProcessingSelectorError as exc:
                    blockers.append({"code": exc.code, "input_id": input_id})
                    continue
                selected[input_id] = frozen.entity_instance_ids
                selector_previews.append(
                    {
                        "input_id": input_id,
                        "member_count": len(frozen.entity_instance_ids),
                        "member_ids": [str(item) for item in frozen.entity_instance_ids],
                        "digest": frozen.digest,
                    }
                )
                continue
            candidates = _input_candidates(contract, sources, node_id)
            if len(candidates) == 1:
                selected[input_id] = (candidates[0].source_id,)
            elif not candidates and not contract.required and contract.default_value is not None:
                selected[input_id] = ()
            else:
                blockers.append(
                    {
                        "code": (
                            "POINT_PROCESSING_INPUT_MISSING"
                            if not candidates
                            else "POINT_PROCESSING_INPUT_AMBIGUOUS"
                        ),
                        "input_id": input_id,
                    }
                )

        planned_edges = tuple(
            (source_id, target_id)
            for input_id in referenced_inputs
            for source_id in selected.get(input_id, ())
        )
        try:
            dag = validate_processing_dag(
                existing_edges=self._catalog.dependency_edges(),
                planned_edges=planned_edges,
                max_depth=8,
            )
        except PointProcessingDagError as exc:
            blockers.append({"code": exc.code, "input_id": "dag"})
            dag = None
        return MappingProxyType(
            {
                "expression": compiled.text,
                "canonical_ast": _plain(compiled.ast),
                "ast_digest": compiled.digest,
                "result_type": compiled.result_kind.value,
                "result_unit": compiled.result_unit,
                "member_count": sum(
                    item["member_count"] for item in selector_previews
                ),
                "selector_members": selector_previews,
                "dag_summary": {
                    "edge_count": len(planned_edges) if dag is None else dag.edge_count,
                    "max_depth": None if dag is None else dag.max_depth,
                    "digest": None if dag is None else dag.digest,
                },
                "blockers": blockers,
            }
        )

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
        plan = self.get_plan(command.plan_id)
        template = self._catalog.get_template(plan.template_revision_id)
        verified_digest = None
        if (
            template is not None
            and template.asset_id == "pcs.en9"
            and self._point_scanner is not None
        ):
            node_source_key = self._catalog.node_source_key(plan.node_id)
            if node_source_key is None:
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_STALE",
                    "Neuron source catalog identity is missing",
                )
            try:
                verified_digest = self._point_scanner.scan(node_source_key).digest
            except Exception as exc:
                raise PointProcessingError(
                    "NEURON_POINT_CATALOG_UNAVAILABLE",
                    "Neuron point catalog could not be rescanned",
                ) from exc
        if self._runtime_gate is None:
            return self._repository.apply_plan(
                command,
                self._catalog,
                transaction=transaction,
                verified_source_catalog_digest=verified_digest,
            )
        try:
            self._runtime_gate.begin_configuration_publish(
                plan.base_configuration_revision
            )
        except DataTrunkError as exc:
            raise PointProcessingError(exc.code, str(exc)) from exc
        try:
            application = self._repository.apply_plan(
                command,
                self._catalog,
                transaction=transaction,
                verified_source_catalog_digest=verified_digest,
            )
        except PointProcessingError:
            self._runtime_gate.cancel_configuration_publish()
            raise
        try:
            self._runtime_gate.reconcile_configuration_runtime()
        except DataTrunkError as exc:
            raise PointProcessingError(exc.code, str(exc)) from exc
        return application


class InMemoryPointProcessingCatalog:
    def __init__(
        self,
        *,
        templates: Mapping[UUID, PointProcessingTemplate],
        sources: tuple[PointProcessingSource, ...] = (),
        node_source_keys: Mapping[UUID, str] | None = None,
        selector_members: Mapping[tuple[UUID, str, str], tuple[UUID, ...]] | None = None,
        dependency_edges: tuple[tuple[UUID, UUID], ...] = (),
    ) -> None:
        self._templates = dict(templates)
        self._sources = tuple(sources)
        self._node_source_keys = dict(node_source_keys or {})
        self._selector_members = dict(selector_members or {})
        self._dependency_edges = set(dependency_edges)

    def get_template(self, revision_id: UUID) -> PointProcessingTemplate | None:
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

    def list_selector_members(
        self,
        target_node_id: UUID,
        selector: Selector,
    ) -> tuple[UUID, ...]:
        return tuple(
            self._selector_members.get(
                (
                    target_node_id,
                    selector.node_type,
                    selector.entity_definition_id,
                ),
                (),
            )
        )

    def dependency_edges(self) -> tuple[tuple[UUID, UUID], ...]:
        return tuple(sorted(self._dependency_edges, key=lambda item: (str(item[0]), str(item[1]))))

    def record_dependencies(
        self,
        edges: tuple[tuple[UUID, UUID], ...],
    ) -> None:
        self._dependency_edges.update(edges)

    def replace_selector_members(
        self,
        members: Mapping[tuple[UUID, str, str], tuple[UUID, ...]],
    ) -> None:
        self._selector_members = dict(members)

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
        self._configuration_revision = 0
        self._lock = RLock()
        self._on_applied = on_applied or (lambda _application: None)

    def configuration_revision(self) -> int:
        return self._configuration_revision

    def current_context(
        self,
        node_id: UUID,
    ) -> CurrentPointProcessingContext | None:
        return self._current.get(node_id)

    def save_plan(
        self,
        plan: PointProcessingPlan,
        *,
        transaction: Any | None = None,
    ) -> PointProcessingPlan:
        del transaction
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
                kind = output.transform["kind"]
                if kind == "boolean_set":
                    transform = BooleanSetTransform(
                        inputs=tuple(
                            BooleanCodeInput(
                                input=InputReference(
                                    inputs[entry["input"]].source_kind,
                                    current.input_source_ids[entry["input"]],
                                ),
                                code=entry["code"],
                            )
                            for entry in output.transform["entries"]
                        )
                    )
                elif kind != "formula":
                    input_id = str(output.transform["input"])
                    input_contract = inputs[input_id]
                    input_source_id = current.input_source_ids[input_id]
                    input_reference = InputReference(
                        input_contract.source_kind,
                        input_source_id,
                    )
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
                elif kind == "fault_codes":
                    transform = FaultCodeTransform(
                        input=input_reference,
                        delimiter=str(output.transform["delimiter"]),
                        entries={
                            raw_code: str(entry["code"])
                            for raw_code, entry in output.transform["entries"].items()
                        },
                    )
                elif kind == "formula":
                    referenced_inputs = _formula_input_names(
                        output.transform["canonicalAst"]
                    )
                    formula_contracts = {
                        input_id: FormulaSource(
                            input_id,
                            ValueKind(inputs[input_id].data_type),
                            inputs[input_id].unit,
                            inputs[input_id].cardinality,
                            inputs[input_id].required,
                            inputs[input_id].default_value,
                        )
                        for input_id in referenced_inputs
                    }
                    transform = FormulaTransform(
                        sources={
                            input_id: tuple(
                                InputReference.l2(source_id)
                                for source_id in (
                                    current.selector_source_ids.get(input_id)
                                    or (
                                        (current.input_source_ids[input_id],)
                                        if input_id in current.input_source_ids
                                        else ()
                                    )
                                )
                            )
                            for input_id in referenced_inputs
                        },
                        source_contracts=formula_contracts,
                        compiled=CompiledFormula(
                            text=str(output.transform["expression"]),
                            ast=output.transform["canonicalAst"],
                            digest=str(output.transform["astDigest"]),
                            result_kind=ValueKind(output.data_type),
                            result_unit=output.unit,
                        ),
                        schedule_seconds=int(output.transform["scheduleSeconds"]),
                        control_eligible=bool(output.transform["controlEligible"]),
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
        verified_source_catalog_digest: str | None = None,
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
            try:
                for item in plan.items:
                    if item.get("kind") != "selector_binding":
                        continue
                    selector = Selector(
                        scope=str(item["selector"]["scope"]),
                        node_type=str(item["selector"]["nodeType"]),
                        entity_definition_id=str(
                            item["selector"]["entityDefinition"]
                        ),
                        cardinality=str(item["cardinality"]),
                    )
                    current_selection = freeze_selector(
                        selector=selector,
                        target_node_id=plan.node_id,
                        configuration_revision=self._configuration_revision,
                        entity_instance_ids=catalog.list_selector_members(
                            plan.node_id,
                            selector,
                        ),
                    )
                    if current_selection.digest != item.get("selector_digest"):
                        raise PointProcessingSelectorError(
                            "POINT_PROCESSING_SELECTOR_STALE",
                            "Point processing selector members changed",
                        )
            except PointProcessingSelectorError as exc:
                raise PointProcessingError(
                    "POINT_PROCESSING_SELECTOR_STALE",
                    "Point processing selector members changed after planning",
                ) from exc
            try:
                actual_source_digest = (
                    verified_source_catalog_digest
                    if verified_source_catalog_digest is not None
                    else _effective_source_catalog_digest(
                        template,
                        catalog,
                        plan.node_id,
                        self._configuration_revision,
                    )
                )
            except PointProcessingSelectorError as exc:
                raise PointProcessingError(
                    "POINT_PROCESSING_SELECTOR_STALE",
                    "Point processing selector members changed after planning",
                ) from exc
            if (
                plan.base_configuration_revision != self._configuration_revision
                or plan.source_catalog_digest
                != actual_source_digest
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
            selector_ids = {
                item["input_id"]: tuple(
                    UUID(value) for value in item["selected_source_ids"]
                )
                for item in plan.items
                if item.get("kind") == "selector_binding"
                and item.get("action") != "block"
            }
            planned_edges = tuple(
                (UUID(source), UUID(target))
                for item in plan.items
                if item.get("kind") == "dag_validation"
                for source, target in item.get("planned_edges", ())
            )
            next_version = self._configuration_revision + 1
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
                revision_id=plan.template_revision_id,
                configuration_revision=next_version,
                output_entity_instance_ids=tuple(
                    sorted(output_ids.values(), key=str)
                ),
                actor=command.actor,
            )
            self._on_applied(application)
            self._applications[application.id] = application
            self._idempotency[key] = (request_digest, application.id)
            self._current[plan.node_id] = CurrentPointProcessingContext(
                revision_id=plan.template_revision_id,
                input_source_ids=input_ids,
                output_entity_ids=output_ids,
                selector_source_ids=selector_ids,
            )
            catalog.record_dependencies(planned_edges)
            self._installed_ids[plan.node_id] = installed_id
            self._configuration_revision = next_version
            self._plans[plan.id] = replace(plan, status="applied")
            return application


def compile_point_processing_plan(
    command: PreviewPointProcessing,
    catalog: PointProcessingCatalog,
    repository: PointProcessingRepository,
    *,
    scan: ScannedPointCatalog | None = None,
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
    base_configuration_revision = repository.configuration_revision()

    items: list[Mapping[str, Any]] = []
    blockers: list[Mapping[str, str]] = []
    sources = catalog.list_sources(command.node_id)
    if scan is not None:
        scanned_sources, l0_items, scan_blockers = _scan_plan_inputs(
            command.node_id,
            template,
            scan,
            sources,
        )
        sources = tuple(
            item for item in sources if item.source_kind != "l0"
        ) + scanned_sources
        items.extend(l0_items)
        blockers.extend(scan_blockers)
        source_digest = scan.digest
    else:
        source_digest = _template_source_catalog_digest(
            template,
            sources,
            command.node_id,
        )
    selected_inputs: dict[str, UUID] = {}
    frozen_selectors: dict[str, FrozenSelection] = {}
    for input_contract in template.inputs:
        if input_contract.selector is not None:
            selector = Selector(
                scope=str(input_contract.selector["scope"]),
                node_type=str(input_contract.selector["nodeType"]),
                entity_definition_id=str(
                    input_contract.selector["entityDefinition"]
                ),
                cardinality=input_contract.cardinality,
            )
            code: str | None = None
            try:
                frozen = freeze_selector(
                    selector=selector,
                    target_node_id=command.node_id,
                    configuration_revision=base_configuration_revision,
                    entity_instance_ids=catalog.list_selector_members(
                        command.node_id,
                        selector,
                    ),
                )
                frozen_selectors[input_contract.input_id] = frozen
            except PointProcessingSelectorError as exc:
                code = exc.code
                frozen = None
                blockers.append(
                    MappingProxyType(
                        {"code": code, "input_id": input_contract.input_id}
                    )
                )
            previous_members = (
                current.selector_source_ids.get(input_contract.input_id, ())
                if current is not None
                else ()
            )
            selected_members = (
                frozen.entity_instance_ids if frozen is not None else ()
            )
            action = (
                "block"
                if code is not None
                else "preserve"
                if previous_members == selected_members and previous_members
                else "update"
                if previous_members
                else "add"
            )
            items.append(
                MappingProxyType(
                    {
                        "item_key": f"selector:{input_contract.input_id}",
                        "layer": "L1",
                        "kind": "selector_binding",
                        "action": action,
                        "input_id": input_contract.input_id,
                        "selector": _plain(input_contract.selector),
                        "cardinality": input_contract.cardinality,
                        "selected_source_ids": tuple(
                            str(item) for item in selected_members
                        ),
                        "selector_digest": (
                            frozen.digest if frozen is not None else None
                        ),
                        "blocker_code": code,
                    }
                )
            )
            continue
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
                    "layer": "L1",
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

    if frozen_selectors:
        source_digest = _digest(
            {
                "catalog_digest": source_digest,
                "selector_digests": {
                    key: value.digest
                    for key, value in sorted(frozen_selectors.items())
                },
            }
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
                    "layer": "L2",
                    "kind": "output_binding",
                    "action": action,
                    "output_id": output.output_id,
                    "entity_definition_id": output.entity_definition_id,
                    "output_entity_instance_id": str(entity_id),
                }
            )
        )

    formula_outputs = tuple(
        output for output in template.outputs
        if output.transform["kind"] == "formula"
    )
    if formula_outputs:
        planned_dependencies = tuple(
            (
                input_id,
                output.output_id,
                source_id,
                output_ids[output.output_id],
            )
            for output in formula_outputs
            for input_id in _formula_input_names(output.transform["canonicalAst"])
            for source_id in (
                frozen_selectors[input_id].entity_instance_ids
                if input_id in frozen_selectors
                else (
                    (selected_inputs[input_id],)
                    if input_id in selected_inputs
                    else ()
                )
            )
        ) if not blockers else ()
        planned_edges = tuple(
            (source_id, target_id)
            for _input_id, _output_id, source_id, target_id in planned_dependencies
        )
        try:
            dag = validate_processing_dag(
                existing_edges=catalog.dependency_edges(),
                planned_edges=planned_edges,
                max_depth=8,
            )
            dag_code = None
        except PointProcessingDagError as exc:
            dag = None
            dag_code = exc.code
            blockers.append(
                MappingProxyType({"code": exc.code, "input_id": "dag"})
            )
        items.append(
            MappingProxyType(
                {
                    "item_key": "dag:site",
                    "layer": "L1",
                    "kind": "dag_validation",
                    "action": "block" if dag_code else "add",
                    "planned_edges": tuple(
                        (str(source), str(target))
                        for source, target in planned_edges
                    ),
                    "planned_dependencies": tuple(
                        {
                            "input_id": input_id,
                            "output_id": output_id,
                            "source_entity_instance_id": str(source_id),
                            "target_entity_instance_id": str(target_id),
                        }
                        for input_id, output_id, source_id, target_id
                        in planned_dependencies
                    ),
                    "max_depth": dag.max_depth if dag is not None else None,
                    "dag_digest": dag.digest if dag is not None else None,
                    "blocker_code": dag_code,
                }
            )
        )

    content = {
        "node_id": str(command.node_id),
        "template_revision_id": str(command.template_revision_id),
        "template_digest": template.content_digest,
        "base_configuration_revision": base_configuration_revision,
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
        base_configuration_revision=base_configuration_revision,
        source_catalog_digest=source_digest,
        status="blocked" if blockers else "ready",
        items=tuple(items),
        blockers=tuple(blockers),
        digest=digest,
        planned_by=command.actor,
    )


def _scan_plan_inputs(
    node_id: UUID,
    template: PointProcessingTemplate,
    scan: ScannedPointCatalog,
    existing_sources: tuple[PointProcessingSource, ...],
) -> tuple[
    tuple[PointProcessingSource, ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, str], ...],
]:
    sources: list[PointProcessingSource] = []
    items: list[Mapping[str, Any]] = []
    blockers: list[Mapping[str, str]] = list(scan.blockers)
    existing_l0 = tuple(
        item for item in existing_sources
        if item.source_kind == "l0" and item.node_id == node_id
    )
    for input_contract in template.inputs:
        source_contract = input_contract.source_contract
        accepted_names = {
            _normalized_source_key(input_contract.source_key),
            *(_normalized_source_key(alias) for alias in input_contract.aliases),
        }
        matches = tuple(
            point for point in scan.points
            if _normalized_source_key(point.name) in accepted_names
            or _normalized_source_key(point.address) in accepted_names
            or (
                source_contract is not None
                and point.address == source_contract["address"]
            )
        )
        if not matches:
            blockers.append(
                MappingProxyType({
                    "code": "NEURON_REQUIRED_POINT_MISSING",
                    "input_id": input_contract.input_id,
                })
            )
            continue
        if source_contract is not None:
            compatible = tuple(
                point for point in matches
                if point.address == source_contract["address"]
                and point.wire_data_type == source_contract["wireDataType"]
                and point.value_data_type == input_contract.data_type
                and point.decimal == source_contract["decimal"]
                and point.read_only is source_contract["readOnly"]
            )
            preferred = tuple(
                point for point in compatible
                if point.group == source_contract["group"]
            )
            matches = preferred or compatible
            if not matches:
                blockers.append(
                    MappingProxyType({
                        "code": "NEURON_POINT_CONTRACT_MISMATCH",
                        "input_id": input_contract.input_id,
                    })
                )
                continue
        if len(matches) != 1:
            code = (
                "NEURON_REQUIRED_POINT_MISSING"
                if not matches
                else "NEURON_REQUIRED_POINT_AMBIGUOUS"
            )
            blockers.append(
                MappingProxyType(
                    {"code": code, "input_id": input_contract.input_id}
                )
            )
            continue
        point = matches[0]
        if point.group_interval_ms <= 0:
            blockers.append(
                MappingProxyType({
                    "code": "NEURON_GROUP_INTERVAL_MISSING",
                    "input_id": input_contract.input_id,
                })
            )
            continue
        freshness_seconds = max(3 * point.group_interval_ms / 1000, 5.0)
        existing = next(
            (
                source for source in existing_l0
                if _normalized_source_key(source.stable_source_key)
                == _normalized_source_key(point.name)
            ),
            None,
        )
        source_id = (
            existing.source_id if existing is not None
            else uuid5(NAMESPACE_URL, f"zizu/l0/{node_id}/{point.address}")
        )
        source = PointProcessingSource(
            source_id=source_id,
            source_kind="l0",
            node_id=node_id,
            stable_source_key=point.name,
            data_type=point.value_data_type,
            unit=input_contract.unit,
            confirmed=True,
        )
        sources.append(source)
        after = {
            "source_id": str(source_id),
            "group": point.group,
            "name": point.name,
            "wire_data_type": point.wire_data_type,
            "value_data_type": point.value_data_type,
            "source_address": point.address,
            "decimal": point.decimal,
            "read_only": point.read_only,
            "unit": input_contract.unit,
            "freshness_seconds": freshness_seconds,
        }
        items.append(
            MappingProxyType(
                {
                    "item_key": f"l0:{input_contract.input_id}",
                    "layer": "L0",
                    "kind": "l0_point",
                    "action": "add" if existing is None else "update",
                    "resource_key": point.address,
                    "input_id": input_contract.input_id,
                    "before": None,
                    "after": MappingProxyType(after),
                    "blocker_code": None,
                }
            )
        )
    return tuple(sources), tuple(items), tuple(blockers)


def _normalized_source_key(value: str) -> str:
    return "".join(value.split()).casefold()


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
    node_id: UUID,
    definition_id: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"zizu/entity/{node_id}/{definition_id}",
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
    template: PointProcessingTemplate,
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


def _effective_source_catalog_digest(
    template: PointProcessingTemplate,
    catalog: PointProcessingCatalog,
    node_id: UUID,
    configuration_revision: int,
) -> str:
    base_digest = _template_source_catalog_digest(
        template,
        catalog.list_sources(node_id),
        node_id,
    )
    selector_digests: dict[str, str] = {}
    for input_contract in template.inputs:
        if input_contract.selector is None:
            continue
        selector = Selector(
            scope=str(input_contract.selector["scope"]),
            node_type=str(input_contract.selector["nodeType"]),
            entity_definition_id=str(input_contract.selector["entityDefinition"]),
            cardinality=input_contract.cardinality,
        )
        frozen = freeze_selector(
            selector=selector,
            target_node_id=node_id,
            configuration_revision=configuration_revision,
            entity_instance_ids=catalog.list_selector_members(node_id, selector),
        )
        selector_digests[input_contract.input_id] = frozen.digest
    if not selector_digests:
        return base_digest
    return _digest(
        {
            "catalog_digest": base_digest,
            "selector_digests": selector_digests,
        }
    )


def _formula_input_names(value: Any) -> tuple[str, ...]:
    names: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            if isinstance(item.get("input"), str):
                names.add(item["input"])
            for child in item.values():
                visit(child)
        elif isinstance(item, (tuple, list)):
            for child in item:
                visit(child)

    visit(value)
    return tuple(sorted(names))


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
