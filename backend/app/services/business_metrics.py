"""Business-metric catalog, zero-runtime-write planning, and atomic installation.

This module deliberately owns configuration delivery only.  Projection, window
calculation, recomputation, and REST delivery are separate concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.services.business_metric_contracts import (
    BusinessMetricTemplate,
    MetricAggregator,
    MetricSourceResolution,
    ResolvedMetricSource,
)
from app.services.solution_business_metrics import compile_business_metric


class BusinessMetricError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class MetricNode:
    id: UUID
    node_type: str
    parent_id: UUID | None
    timezone: str | None
    raw_detail_retention_days: int | None = None


@dataclass(frozen=True)
class MetricSourceCandidate:
    entity_instance_id: UUID
    node_id: UUID
    entity_definition_id: str
    data_type: str
    unit: str | None


@dataclass(frozen=True)
class MetricTemplateSummary:
    template_id: str
    revision: int
    display_name: str
    output_entity_definition_id: str
    installed: bool


@dataclass(frozen=True)
class PreviewMetricInstallation:
    node_id: UUID
    template_id: str
    actor: str


@dataclass(frozen=True)
class ApplyMetricInstallation:
    plan_id: UUID
    expected_digest: str
    actor: str
    idempotency_key: str


@dataclass(frozen=True)
class ChangeMetricState:
    installation_id: UUID
    state: str
    actor: str


@dataclass(frozen=True)
class MetricInstallationPlan:
    id: UUID
    node_id: UUID
    template_id: str
    template_revision: int
    template_digest: str
    timezone: str | None
    raw_detail_retention_days: int | None
    site_configuration_version: int
    sources: tuple[ResolvedMetricSource, ...]
    internal_processing_digest: str | None
    output_entity_instance_id: UUID | None
    status: str
    blockers: tuple[dict[str, str], ...]
    digest: str
    planned_by: str


@dataclass(frozen=True)
class MetricInstallation:
    id: UUID
    node_id: UUID
    template_id: str
    template_revision: int
    entity_instance_id: UUID
    processing_revision_id: UUID
    timezone: str
    site_configuration_version: int
    state: str
    plan_digest: str


@dataclass(frozen=True)
class MetricInstallationState:
    installation_id: UUID
    template_id: str
    entity_instance_id: UUID
    state: str


class BusinessMetricCatalog(Protocol):
    def get_template(self, template_id: str) -> BusinessMetricTemplate | None: ...
    def list_templates(self, node_id: UUID) -> tuple[BusinessMetricTemplate, ...]: ...
    def get_node(self, node_id: UUID) -> MetricNode | None: ...
    def list_sources(self, root_node_id: UUID) -> tuple[MetricSourceCandidate, ...]: ...


class BusinessMetricRepository(Protocol):
    def site_configuration_version(self) -> int: ...
    def installed_for_node(self, node_id: UUID) -> tuple[MetricInstallation, ...]: ...
    def save_plan(self, plan: MetricInstallationPlan) -> MetricInstallationPlan: ...
    def apply_installation(
        self,
        installation: MetricInstallation,
        *,
        actor: str,
        idempotency_key: str,
    ) -> MetricInstallation: ...
    def get_installation(self, installation_id: UUID) -> MetricInstallation | None: ...


class BusinessMetricDelivery:
    """The sole configuration seam for a business metric installation."""

    def __init__(self, catalog: BusinessMetricCatalog, repository: BusinessMetricRepository) -> None:
        self._catalog = catalog
        self._repository = repository
        # A preview is not a runtime write.  Production repositories additionally
        # retain immutable plan evidence in schema 043; this cache keeps the
        # in-memory seam equally zero-write for unit tests.
        self._plans: dict[UUID, MetricInstallationPlan] = {}

    def catalog(self, *, node_id: UUID) -> tuple[MetricTemplateSummary, ...]:
        installed_ids = {item.template_id for item in self._repository.installed_for_node(node_id)}
        return tuple(
            MetricTemplateSummary(
                template_id=item.template_id,
                revision=item.revision,
                display_name=item.display_name,
                output_entity_definition_id=item.output_entity_definition_id,
                installed=item.template_id in installed_ids,
            )
            for item in self._catalog.list_templates(node_id)
        )

    def preview(self, request: PreviewMetricInstallation) -> MetricInstallationPlan:
        if not request.actor:
            raise BusinessMetricError("BUSINESS_METRIC_ACTOR_INVALID", "An actor is required")
        plan = self._compile_preview(request)
        self._plans[plan.id] = plan
        return self._repository.save_plan(plan)

    def apply(self, command: ApplyMetricInstallation) -> MetricInstallation:
        persistent_apply = getattr(self._repository, "apply_plan", None)
        if callable(persistent_apply):
            return persistent_apply(command, self._catalog)
        plan = self._plans.get(command.plan_id)
        if plan is None:
            raise BusinessMetricError("BUSINESS_METRIC_PLAN_MISSING", "Installation plan was not found")
        if command.expected_digest != plan.digest:
            raise BusinessMetricError("BUSINESS_METRIC_PLAN_DIGEST_MISMATCH", "Plan digest does not match")
        if plan.blockers:
            raise BusinessMetricError("BUSINESS_METRIC_PLAN_BLOCKED", "Blocked plans cannot be installed")
        refreshed = self._compile_preview(
            PreviewMetricInstallation(plan.node_id, plan.template_id, command.actor)
        )
        if refreshed.digest != plan.digest:
            raise BusinessMetricError("BUSINESS_METRIC_PLAN_STALE", "Plan sources or site configuration changed")
        processing_revision_id = uuid5(
            NAMESPACE_URL,
            f"zizu/business-metric-processing/{plan.internal_processing_digest}",
        )
        installation = MetricInstallation(
            id=uuid5(NAMESPACE_URL, f"zizu/business-metric/installation/{plan.digest}"),
            node_id=plan.node_id,
            template_id=plan.template_id,
            template_revision=plan.template_revision,
            entity_instance_id=uuid5(
                NAMESPACE_URL,
                f"zizu/business-metric/entity/{plan.node_id}/{plan.template_id}",
            ),
            processing_revision_id=processing_revision_id,
            timezone=plan.timezone or "",
            site_configuration_version=plan.site_configuration_version,
            state="active",
            plan_digest=plan.digest,
        )
        return self._repository.apply_installation(
            installation, actor=command.actor, idempotency_key=command.idempotency_key
        )

    def inspect(self, *, node_id: UUID) -> tuple[MetricInstallationState, ...]:
        return tuple(
            MetricInstallationState(item.id, item.template_id, item.entity_instance_id, item.state)
            for item in self._repository.installed_for_node(node_id)
        )

    def change_state(self, command: ChangeMetricState) -> MetricInstallation:
        """Task 2 scaffold: state transitions are owned by Task 5."""
        installation = self._repository.get_installation(command.installation_id)
        if installation is None:
            raise BusinessMetricError("BUSINESS_METRIC_STATE_INVALID", "Installation was not found")
        if command.state != installation.state:
            raise BusinessMetricError("BUSINESS_METRIC_STATE_INVALID", "State changes are not available in Task 2")
        return installation

    def _compile_preview(self, request: PreviewMetricInstallation) -> MetricInstallationPlan:
        template = self._required_template(request.template_id)
        node = self._catalog.get_node(request.node_id)
        return _compile_plan_from_state(
            request=request,
            template=template,
            node=node,
            catalog=self._catalog,
            candidates=(
                self._catalog.list_sources(request.node_id) if node is not None else ()
            ),
            site_configuration_version=self._repository.site_configuration_version(),
        )

    def _required_template(self, template_id: str) -> BusinessMetricTemplate:
        template = self._catalog.get_template(template_id)
        if template is None:
            raise BusinessMetricError("BUSINESS_METRIC_TEMPLATE_MISSING", "Template was not found")
        return template


class InMemoryBusinessMetricCatalog:
    def __init__(
        self,
        *,
        templates: tuple[BusinessMetricTemplate, ...],
        nodes: tuple[MetricNode, ...],
        sources: tuple[MetricSourceCandidate, ...],
    ) -> None:
        self._templates = {item.template_id: item for item in templates}
        self._nodes = {item.id: item for item in nodes}
        self.sources = sources

    def get_template(self, template_id: str) -> BusinessMetricTemplate | None:
        return self._templates.get(template_id)

    def list_templates(self, node_id: UUID) -> tuple[BusinessMetricTemplate, ...]:
        node = self.get_node(node_id)
        if node is None:
            return ()
        return tuple(item for item in self._templates.values() if item.target_node_type == node.node_type)

    def get_node(self, node_id: UUID) -> MetricNode | None:
        return self._nodes.get(node_id)

    def list_sources(self, root_node_id: UUID) -> tuple[MetricSourceCandidate, ...]:
        members = _subtree_ids(root_node_id, self._nodes)
        return tuple(item for item in self.sources if item.node_id in members)

    def replace_sources(self, sources: tuple[MetricSourceCandidate, ...]) -> None:
        self.sources = sources


class InMemoryBusinessMetricRepository:
    def __init__(self, *, site_configuration_version: int) -> None:
        self._site_configuration_version = site_configuration_version
        self._installations: dict[UUID, MetricInstallation] = {}
        self._idempotency: dict[str, UUID] = {}
        self._audits: list[dict[str, str]] = []

    def site_configuration_version(self) -> int:
        return self._site_configuration_version

    def installed_for_node(self, node_id: UUID) -> tuple[MetricInstallation, ...]:
        return tuple(item for item in self._installations.values() if item.node_id == node_id)

    def save_plan(self, plan: MetricInstallationPlan) -> MetricInstallationPlan:
        # The in-memory seam intentionally models preview's zero-write behavior.
        return plan

    def apply_installation(self, installation: MetricInstallation, *, actor: str, idempotency_key: str) -> MetricInstallation:
        known = self._idempotency.get(idempotency_key)
        if known is not None:
            existing = self._installations[known]
            if existing.plan_digest != installation.plan_digest:
                raise BusinessMetricError("BUSINESS_METRIC_IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another plan")
            return existing
        self._installations[installation.id] = installation
        self._idempotency[idempotency_key] = installation.id
        self._audits.append({"action": "installed", "actor": actor, "plan_digest": installation.plan_digest})
        return installation

    def get_installation(self, installation_id: UUID) -> MetricInstallation | None:
        return self._installations.get(installation_id)

    def plan_count(self) -> int:
        return 0

    def installation_count(self) -> int:
        return len(self._installations)

    def audit_count(self) -> int:
        return len(self._audits)


def _compile_plan_from_state(
    *,
    request: PreviewMetricInstallation,
    template: BusinessMetricTemplate,
    node: MetricNode | None,
    catalog: BusinessMetricCatalog,
    candidates: tuple[MetricSourceCandidate, ...],
    site_configuration_version: int,
) -> MetricInstallationPlan:
    timezone: str | None = None
    raw_retention: int | None = None
    blocker: str | None = None
    if node is None or node.node_type != template.target_node_type:
        blocker = "BUSINESS_METRIC_TARGET_INVALID"
    else:
        timezone, raw_retention = _site_contract(node, catalog)
        if timezone is None:
            blocker = "BUSINESS_METRIC_TIMEZONE_INVALID"
        elif raw_retention is None or raw_retention < 0:
            blocker = "BUSINESS_METRIC_RETENTION_INVALID"

    source: ResolvedMetricSource | None = None
    if blocker is None:
        source, blocker = _resolve_source(template, candidates)
    if blocker is not None:
        blockers = ({"code": blocker},)
        digest = _digest(
            {
                "node_id": str(request.node_id),
                "template_id": template.template_id,
                "template_revision": template.revision,
                "template_digest": template.content_digest,
                "timezone": timezone,
                "raw_detail_retention_days": raw_retention,
                "site_configuration_version": site_configuration_version,
                "blockers": list(blockers),
            }
        )
        return MetricInstallationPlan(
            id=uuid5(NAMESPACE_URL, f"zizu/business-metric-plan/{digest}"),
            node_id=request.node_id,
            template_id=template.template_id,
            template_revision=template.revision,
            template_digest=template.content_digest,
            timezone=timezone,
            raw_detail_retention_days=raw_retention,
            site_configuration_version=site_configuration_version,
            sources=(),
            internal_processing_digest=None,
            output_entity_instance_id=None,
            status="blocked",
            blockers=blockers,
            digest=digest,
            planned_by=request.actor,
        )

    assert source is not None and timezone is not None and raw_retention is not None
    compiled = compile_business_metric(
        template,
        MetricSourceResolution(timezone=timezone, sources=(source,)),
    )
    output_entity_id = uuid5(
        NAMESPACE_URL,
        f"zizu/business-metric/entity/{request.node_id}/{template.template_id}",
    )
    digest = _digest(
        {
            "node_id": str(request.node_id),
            "template_id": template.template_id,
            "template_revision": template.revision,
            "template_digest": template.content_digest,
            "timezone": timezone,
            "raw_detail_retention_days": raw_retention,
            "site_configuration_version": site_configuration_version,
            "sources": [_source_content(source)],
            "internal_processing_digest": compiled.content_digest,
            "output_entity_instance_id": str(output_entity_id),
        }
    )
    return MetricInstallationPlan(
        id=uuid5(NAMESPACE_URL, f"zizu/business-metric-plan/{digest}"),
        node_id=request.node_id,
        template_id=template.template_id,
        template_revision=template.revision,
        template_digest=template.content_digest,
        timezone=timezone,
        raw_detail_retention_days=raw_retention,
        site_configuration_version=site_configuration_version,
        sources=(source,),
        internal_processing_digest=compiled.content_digest,
        output_entity_instance_id=output_entity_id,
        status="ready",
        blockers=(),
        digest=digest,
        planned_by=request.actor,
    )


def _site_contract(
    node: MetricNode,
    catalog: BusinessMetricCatalog,
) -> tuple[str | None, int | None]:
    current: MetricNode | None = node
    while current is not None:
        if current.timezone is not None or current.raw_detail_retention_days is not None:
            return current.timezone, current.raw_detail_retention_days
        current = (
            catalog.get_node(current.parent_id)
            if current.parent_id is not None
            else None
        )
    return None, None


def _resolve_source(
    template: BusinessMetricTemplate,
    candidates: tuple[MetricSourceCandidate, ...],
) -> tuple[ResolvedMetricSource | None, str | None]:
    counter_options = tuple(
        item for item in template.sources if item.method is MetricAggregator.COUNTER_DELTA
    )
    fallback_options = tuple(
        item for item in template.sources if item.method is not MetricAggregator.COUNTER_DELTA
    )
    for option in (*counter_options, *fallback_options):
        matches = tuple(item for item in candidates if item.entity_definition_id == option.entity_definition_id)
        if len(matches) > 1:
            return None, "BUSINESS_METRIC_SOURCE_AMBIGUOUS"
        if len(matches) == 1:
            candidate = matches[0]
            return (
                ResolvedMetricSource(
                    candidate.entity_instance_id,
                    candidate.entity_definition_id,
                    option.method,
                    candidate.data_type,
                    candidate.unit,
                    option.method is MetricAggregator.POWER_INTEGRAL,
                ),
                None,
            )
    return None, "BUSINESS_METRIC_SOURCE_MISSING"


def _subtree_ids(root_node_id: UUID, nodes: dict[UUID, MetricNode]) -> set[UUID]:
    result = {root_node_id}
    changed = True
    while changed:
        changed = False
        for node in nodes.values():
            if node.parent_id in result and node.id not in result:
                result.add(node.id)
                changed = True
    return result


def _source_content(source: ResolvedMetricSource) -> dict[str, Any]:
    return {
        "entity_instance_id": str(source.entity_instance_id),
        "entity_definition_id": source.entity_definition_id,
        "method": source.method.value,
        "data_type": source.data_type,
        "unit": source.unit,
        "estimated": source.estimated,
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
