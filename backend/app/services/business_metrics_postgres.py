"""PostgreSQL adapters for the Task 2 business-metric delivery seam.

The adapters intentionally do not perform projection work; their transaction
boundary is limited to immutable plan/application evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg2.extras import Json

from app.services.business_metrics import (
    ApplyMetricInstallation,
    BusinessMetricError,
    MetricInstallation,
    MetricInstallationPlan,
    MetricNode,
    PreviewMetricInstallation,
    MetricSourceCandidate,
    _compile_plan_from_state,
    _digest,
    _source_content,
)
from app.services.business_metric_contracts import (
    BusinessMetricTemplate,
    MetricAggregator,
    MetricSourceResolution,
    ResolvedMetricSource,
)
from app.services.point_processing import (
    ApplyPointProcessingPlan,
    InMemoryPointProcessingCatalog,
    PointProcessingError,
    PointProcessingSource,
    PreviewPointProcessing,
    compile_point_processing_plan,
)
from app.services.point_processing_postgres import (
    PostgresPointProcessingCatalog,
    PostgresPointProcessingRepository,
)
from app.services.solution_business_metrics import (
    compile_business_metric,
    parse_business_metric_asset,
)
from app.services.solution_point_processings import (
    PointProcessingAsset,
    point_processing_revision_id,
    point_processing_template_id,
)


def persist_internal_business_metric_asset(
    cursor: Any,
    asset: PointProcessingAsset,
    *,
    package_record_id: UUID | None = None,
) -> UUID:
    """Persist the private Task-1 compiler output through the existing L1 catalog.

    The `business_metric` transform is a closed declarative object, not a DSL.
    It is encoded in the pre-existing immutable expression storage solely so the
    point-processing catalog and application transaction can carry it.
    """
    if any(output.transform.get("kind") != "business_metric" for output in asset.outputs):
        raise BusinessMetricError("BUSINESS_METRIC_INTERNAL_ASSET_INVALID", "Internal asset has a non-metric transform")
    template_id = point_processing_template_id(asset)
    revision_id = point_processing_revision_id(asset)
    cursor.execute(
        """
        INSERT INTO t_point_processing_templates
          (id, asset_id, device_category, brand, model, display_name, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (asset_id, brand, model) DO NOTHING
        """,
        (template_id, asset.asset_id, asset.device_category, asset.brand, asset.model, asset.display_name, asset.status),
    )
    cursor.execute(
        "SELECT id, display_name, status FROM t_point_processing_templates WHERE asset_id=%s AND brand=%s AND model=%s FOR UPDATE",
        (asset.asset_id, asset.brand, asset.model),
    )
    stored_template = cursor.fetchone()
    if stored_template is None or stored_template[0] != template_id or stored_template[1] != asset.display_name or stored_template[2] != asset.status:
        raise BusinessMetricError("BUSINESS_METRIC_INTERNAL_ASSET_CONFLICT", "Internal processing template identity conflicts")
    cursor.execute(
        """
        INSERT INTO t_point_processing_revisions
          (id, template_id, revision, content_digest, published_at,
           internal_kind)
        VALUES (%s, %s, %s, %s, now(), 'business_metric')
        ON CONFLICT (template_id, revision) DO NOTHING
        """,
        (revision_id, template_id, asset.revision, asset.content_digest),
    )
    cursor.execute(
        """
        SELECT id, content_digest, internal_kind
        FROM t_point_processing_revisions
        WHERE template_id=%s AND revision=%s
        """,
        (template_id, asset.revision),
    )
    stored_revision = cursor.fetchone()
    if (
        stored_revision is None
        or stored_revision[0] != revision_id
        or stored_revision[1].strip() != asset.content_digest
        or stored_revision[2] != "business_metric"
    ):
        raise BusinessMetricError("BUSINESS_METRIC_INTERNAL_ASSET_CONFLICT", "Internal processing revision conflicts")
    if package_record_id is not None:
        cursor.execute(
            """
            INSERT INTO t_solution_point_processing_assets
              (package_record_id, template_revision_id, asset_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (package_record_id, asset_id) DO NOTHING
            """,
            (package_record_id, revision_id, asset.asset_id),
        )
        cursor.execute(
            """
            SELECT template_revision_id
            FROM t_solution_point_processing_assets
            WHERE package_record_id = %s AND asset_id = %s
            """,
            (package_record_id, asset.asset_id),
        )
        relation = cursor.fetchone()
        if relation is None or relation[0] != revision_id:
            raise BusinessMetricError(
                "BUSINESS_METRIC_INTERNAL_ASSET_CONFLICT",
                "Internal processing package relation conflicts",
            )
    from app.services.point_processing_postgres import _input_id, _output_id

    for item in asset.inputs:
        input_id = _input_id(revision_id, item.input_id)
        cursor.execute(
            """
            INSERT INTO t_point_processing_inputs
              (id, revision_id, input_key, source_kind, data_type, unit, required, stable_source_key, aliases)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (revision_id, input_key) DO NOTHING
            """,
            (input_id, revision_id, item.input_id, item.source_kind, item.data_type, item.unit, item.required, item.source_key, list(item.aliases)),
        )
    for output in asset.outputs:
        output_id = _output_id(revision_id, output.output_id)
        transform = _plain(output.transform)
        canonical = json.dumps(transform, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cursor.execute(
            """
            INSERT INTO t_point_processing_outputs
              (id, revision_id, output_key, entity_definition_id, data_type, unit, freshness_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (revision_id, output_key) DO NOTHING
            """,
            (output_id, revision_id, output.output_id, output.entity_definition_id, output.data_type, output.unit, output.freshness_seconds),
        )
        cursor.execute(
            """
            INSERT INTO t_point_processing_expressions
              (output_id, dsl_text, canonical_ast, ast_digest, result_data_type, result_unit, schedule_seconds, control_eligible)
            VALUES (%s, 'internal.business_metric', %s, %s, %s, %s, 1, FALSE)
            ON CONFLICT (output_id) DO NOTHING
            """,
            (output_id, Json(transform), hashlib.sha256(canonical.encode("utf-8")).hexdigest(), output.data_type, output.unit),
        )
    return revision_id


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _capability_content(
    template: BusinessMetricTemplate,
    plan: MetricInstallationPlan,
) -> dict[str, Any]:
    return {
        "temporalSemantics": template.temporal_semantics,
        "controlEligible": False,
        "templateDigest": template.content_digest,
        "sourceEntityInstanceIds": [
            str(item.entity_instance_id) for item in plan.sources
        ],
    }


class PostgresBusinessMetricCatalog:
    @staticmethod
    @contextmanager
    def _connection():
        from app.services.telemetry_store import get_connection

        with get_connection() as connection:
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def get_template(self, template_id: str) -> BusinessMetricTemplate | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT revision.content
                    FROM t_business_metric_revisions AS revision
                    JOIN t_business_metric_templates AS template ON template.id = revision.template_id
                    WHERE template.template_key = %s
                    ORDER BY revision.revision DESC LIMIT 1
                    """,
                    (template_id,),
                )
                row = cursor.fetchone()
                return parse_business_metric_asset(row[0]) if row else None

    def list_templates(self, node_id: UUID) -> tuple[BusinessMetricTemplate, ...]:
        node = self.get_node(node_id)
        if node is None:
            return ()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (template.id) revision.content
                    FROM t_business_metric_templates AS template
                    JOIN t_business_metric_revisions AS revision ON revision.template_id = template.id
                    ORDER BY template.id, revision.revision DESC
                    """
                )
                return tuple(
                    item for item in (parse_business_metric_asset(row[0]) for row in cursor.fetchall())
                    if item.target_node_type == node.node_type
                )

    def get_node(self, node_id: UUID) -> MetricNode | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self.get_node_with_cursor(cursor, node_id)

    @staticmethod
    def get_node_with_cursor(cursor: Any, node_id: UUID) -> MetricNode | None:
        cursor.execute(
            """
            SELECT node.id, node.node_type, node.parent_id,
                   CASE WHEN node.node_type = 'SITE'
                        THEN site.parameters ->> 'timezone' END,
                   CASE WHEN node.node_type = 'SITE'
                        THEN (site.parameters ->> 'raw_detail_retention_days')::integer END
            FROM t_nodes AS node
            JOIN t_site_configuration_state AS state ON state.singleton = TRUE
            JOIN t_site_configuration_versions AS site
              ON site.version = state.current_version
            WHERE node.id = %s AND node.enabled = TRUE
            """,
            (node_id,),
        )
        row = cursor.fetchone()
        return MetricNode(*row) if row else None

    def list_sources(self, root_node_id: UUID) -> tuple[MetricSourceCandidate, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self.list_sources_with_cursor(cursor, root_node_id)

    @staticmethod
    def list_sources_with_cursor(
        cursor: Any,
        root_node_id: UUID,
    ) -> tuple[MetricSourceCandidate, ...]:
        cursor.execute(
            """
            WITH RECURSIVE subtree AS (
              SELECT id FROM t_nodes WHERE id = %s AND enabled = TRUE
              UNION ALL
              SELECT child.id FROM t_nodes AS child
              JOIN subtree ON child.parent_id = subtree.id
              WHERE child.enabled = TRUE
            )
            SELECT entity.id, device.node_id, entity.definition_id,
                   entity.data_type, entity.unit, entity.direction
            FROM t_entity_instances AS entity
            JOIN t_device_instances AS device ON device.id = entity.device_instance_id
            JOIN subtree ON subtree.id = device.node_id
            WHERE entity.active = TRUE AND device.active = TRUE
              AND (
                EXISTS (
                  SELECT 1
                  FROM t_entity_instance_bindings AS binding
                  JOIN t_entity_binding_confirmations AS confirmation
                    ON confirmation.id = binding.confirmation_audit_id
                   AND confirmation.entity_instance_id = entity.id
                   AND confirmation.selected_tag_id = binding.tag_id
                  WHERE binding.entity_instance_id = entity.id
                    AND binding.active = TRUE
                )
                OR EXISTS (
                  SELECT 1
                  FROM t_point_processing_output_bindings AS output_binding
                  JOIN t_installed_point_processings AS installed
                    ON installed.id = output_binding.installed_processing_id
                   AND installed.current = TRUE
                  WHERE output_binding.entity_instance_id = entity.id
                )
              )
            ORDER BY entity.id
            """,
            (root_node_id,),
        )
        return tuple(MetricSourceCandidate(*row) for row in cursor.fetchall())


class _CursorBusinessMetricCatalog:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def get_node(self, node_id: UUID) -> MetricNode | None:
        return PostgresBusinessMetricCatalog.get_node_with_cursor(self._cursor, node_id)


class _PointPlanningRepository:
    def __init__(self, site_configuration_version: int) -> None:
        self._site_configuration_version = site_configuration_version

    def site_configuration_version(self) -> int:
        return self._site_configuration_version

    def current_context(self, _node_id: UUID) -> None:
        return None


class PostgresBusinessMetricRepository:
    @staticmethod
    @contextmanager
    def _connection():
        from app.services.telemetry_store import get_connection

        with get_connection() as connection:
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def site_configuration_version(self) -> int:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_version FROM t_site_configuration_state WHERE singleton = TRUE"
                )
                return int(cursor.fetchone()[0])

    def save_plan(self, plan: MetricInstallationPlan) -> MetricInstallationPlan:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT revision.id, revision.content
                    FROM t_business_metric_revisions AS revision
                    JOIN t_business_metric_templates AS template
                      ON template.id = revision.template_id
                    WHERE template.template_key = %s
                      AND revision.revision = %s
                      AND revision.content_digest = %s
                    """,
                    (plan.template_id, plan.template_revision, plan.template_digest),
                )
                revision = cursor.fetchone()
                if revision is None:
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_TEMPLATE_MISSING",
                        "Template revision is not persisted",
                    )
                source_digest = (
                    _digest([_source_content(item) for item in plan.sources])
                    if plan.sources
                    else None
                )
                cursor.execute(
                    """
                    INSERT INTO t_business_metric_installation_plans
                      (id, node_id, template_revision_id,
                       base_site_configuration_version, frozen_timezone,
                       raw_detail_retention_days, source_digest,
                       internal_processing_digest, previous_installation_id,
                       status, digest, planned_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        plan.id,
                        plan.node_id,
                        revision[0],
                        plan.site_configuration_version,
                        plan.timezone,
                        plan.raw_detail_retention_days,
                        source_digest,
                        plan.internal_processing_digest,
                        plan.previous_installation_id,
                        plan.status,
                        plan.digest,
                        plan.planned_by,
                    ),
                )
                cursor.execute(
                    "SELECT digest FROM t_business_metric_installation_plans WHERE id = %s",
                    (plan.id,),
                )
                stored = cursor.fetchone()
                if stored is None or stored[0].strip() != plan.digest:
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_PLAN_CONFLICT",
                        "Plan identity conflicts with immutable evidence",
                    )
                template = parse_business_metric_asset(revision[1])
                previous_sources: dict[int, dict[str, Any]] = {}
                previous_output: dict[str, Any] | None = None
                previous_capability: dict[str, Any] | None = None
                if plan.previous_installation_id is not None:
                    cursor.execute(
                        """
                        SELECT installed.id, installed.entity_instance_id,
                               installed.template_revision_id,
                               installed.installed_processing_id,
                               entity.definition_id, entity.data_type, entity.unit
                        FROM t_installed_business_metrics AS installed
                        JOIN t_entity_instances AS entity
                          ON entity.id = installed.entity_instance_id
                        WHERE installed.id = %s
                        """,
                        (plan.previous_installation_id,),
                    )
                    previous_row = cursor.fetchone()
                    if previous_row is None:
                        raise BusinessMetricError(
                            "BUSINESS_METRIC_PLAN_STALE",
                            "Previous business metric installation is unavailable",
                        )
                    previous_output = {
                        "installationId": str(previous_row[0]),
                        "entityInstanceId": str(previous_row[1]),
                        "templateRevisionId": str(previous_row[2]),
                        "installedProcessingId": str(previous_row[3]),
                        "entityDefinition": previous_row[4],
                        "dataType": previous_row[5],
                        "unit": previous_row[6],
                    }
                    cursor.execute(
                        """
                        SELECT binding.ordinal, binding.entity_instance_id,
                               binding.entity_definition_id, binding.method,
                               binding.data_type, binding.unit,
                               binding.estimated, binding.direction
                        FROM t_business_metric_source_bindings AS binding
                        JOIN t_entity_instances AS entity
                          ON entity.id = binding.entity_instance_id
                        WHERE binding.installed_metric_id = %s
                        ORDER BY binding.ordinal
                        """,
                        (plan.previous_installation_id,),
                    )
                    previous_sources = {
                        int(row[0]): {
                            "entity_instance_id": str(row[1]),
                            "entity_definition_id": row[2],
                            "method": row[3],
                            "data_type": row[4],
                            "unit": row[5],
                            "estimated": row[6],
                            "direction": row[7],
                        }
                        for row in cursor.fetchall()
                    }
                    cursor.execute(
                        """
                        SELECT content
                        FROM t_entity_capability_contracts
                        WHERE installed_metric_id = %s
                        ORDER BY created_at DESC, id DESC LIMIT 1
                        """,
                        (plan.previous_installation_id,),
                    )
                    capability_row = cursor.fetchone()
                    previous_capability = (
                        dict(capability_row[0]) if capability_row is not None else None
                    )
                items: list[dict[str, Any]] = []
                for ordinal, source in enumerate(plan.sources):
                    after = _source_content(source)
                    before = previous_sources.get(ordinal)
                    items.append(
                        {
                            "item_key": f"source:{ordinal}",
                            "ordinal": ordinal,
                            "item_kind": "source",
                            "action": (
                                "preserve"
                                if before == after
                                else "update"
                                if before is not None
                                else "add"
                            ),
                            "source_entity_instance_id": source.entity_instance_id,
                            "method": source.method.value,
                            "estimated": source.estimated,
                            "blocker_code": None,
                            "before": before,
                            "after": after,
                        }
                    )
                if plan.status == "ready":
                    items.extend(
                        (
                            {
                                "item_key": "output:metric_value",
                                "ordinal": len(items),
                                "item_kind": "output",
                                "action": (
                                    "reuse"
                                    if previous_output is not None
                                    and previous_output["entityInstanceId"]
                                    == str(plan.output_entity_instance_id)
                                    else "update"
                                    if previous_output is not None
                                    else "add"
                                ),
                                "source_entity_instance_id": None,
                                "method": None,
                                "estimated": None,
                                "blocker_code": None,
                                "before": previous_output,
                                "after": {
                                    **dict(revision[1]["output"]),
                                    "entityInstanceId": str(
                                        plan.output_entity_instance_id
                                    ),
                                },
                            },
                            {
                                "item_key": "capability:windowed",
                                "ordinal": len(items) + 1,
                                "item_kind": "capability",
                                "action": (
                                    "preserve"
                                    if previous_capability
                                    == _capability_content(template, plan)
                                    else "update"
                                    if previous_capability is not None
                                    else "add"
                                ),
                                "source_entity_instance_id": None,
                                "method": None,
                                "estimated": None,
                                "blocker_code": None,
                                "before": previous_capability,
                                "after": _capability_content(template, plan),
                            },
                        )
                    )
                for blocker in plan.blockers:
                    items.append(
                        {
                            "item_key": f"blocker:{len(items)}",
                            "ordinal": len(items),
                            "item_kind": "blocker",
                            "action": "block",
                            "source_entity_instance_id": None,
                            "method": None,
                            "estimated": None,
                            "blocker_code": blocker["code"],
                            "before": None,
                            "after": dict(blocker),
                        }
                    )
                for item in items:
                    cursor.execute(
                        """
                        INSERT INTO t_business_metric_plan_items
                          (plan_id, item_key, ordinal, item_kind, action,
                           source_entity_instance_id, method, estimated,
                           blocker_code, before_value, after_value)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (plan_id, item_key) DO NOTHING
                        """,
                        (
                            plan.id,
                            item["item_key"],
                            item["ordinal"],
                            item["item_kind"],
                            item["action"],
                            item["source_entity_instance_id"],
                            item["method"],
                            item["estimated"],
                            item["blocker_code"],
                            (
                                Json(item.get("before"))
                                if item.get("before") is not None
                                else None
                            ),
                            Json(item["after"]),
                        ),
                    )
                return plan

    def installed_for_node(self, node_id: UUID) -> tuple[MetricInstallation, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    self._installation_select()
                    + " WHERE installed.node_id = %s AND processing.current = TRUE "
                    "ORDER BY installed.installed_at",
                    (node_id,),
                )
                return tuple(MetricInstallation(*row) for row in cursor.fetchall())

    def get_installation(self, installation_id: UUID) -> MetricInstallation | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self._installation_with_cursor(cursor, installation_id)

    def apply_installation(
        self,
        installation: MetricInstallation,
        *,
        actor: str,
        idempotency_key: str,
    ) -> MetricInstallation:
        del installation, actor, idempotency_key
        raise BusinessMetricError(
            "BUSINESS_METRIC_PLAN_REQUIRED",
            "PostgreSQL installations must be applied from persisted plan evidence",
        )

    def apply_plan(
        self,
        command: ApplyMetricInstallation,
        catalog: PostgresBusinessMetricCatalog,
    ) -> MetricInstallation:
        del catalog
        if not command.actor.strip() or not command.idempotency_key.strip():
            raise BusinessMetricError(
                "BUSINESS_METRIC_APPLY_INVALID",
                "Actor and idempotency key are required",
            )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"business-metric:{command.actor}:{command.idempotency_key}",),
                )
                plan, template, template_revision_id = self._load_plan(
                    cursor, command.plan_id
                )
                if plan is None or template is None or template_revision_id is None:
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_PLAN_MISSING", "Installation plan was not found"
                    )
                if plan.digest != command.expected_digest:
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_PLAN_DIGEST_MISMATCH",
                        "Plan digest does not match",
                    )
                if plan.status != "ready" or plan.blockers:
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_PLAN_BLOCKED", "Blocked plans cannot be installed"
                    )
                request_digest = _digest(
                    {
                        "plan_id": str(command.plan_id),
                        "expected_digest": command.expected_digest,
                    }
                )
                cursor.execute(
                    """
                    SELECT installed_metric_id, request_digest
                    FROM t_business_metric_audit
                    WHERE actor = %s AND idempotency_key = %s
                    """,
                    (command.actor, command.idempotency_key),
                )
                idempotency = cursor.fetchone()
                if idempotency is not None:
                    if idempotency[1].strip() != request_digest:
                        raise BusinessMetricError(
                            "BUSINESS_METRIC_IDEMPOTENCY_CONFLICT",
                            "Idempotency key belongs to another request",
                        )
                    result = self._installation_with_cursor(cursor, idempotency[0])
                    if result is None:
                        raise BusinessMetricError(
                            "BUSINESS_METRIC_IDEMPOTENCY_CONFLICT",
                            "Idempotency evidence has no installation",
                        )
                    return result
                cursor.execute(
                    """
                    SELECT id
                    FROM t_installed_business_metrics
                    WHERE source_plan_id = %s
                    ORDER BY installed_at, id LIMIT 1
                    """,
                    (plan.id,),
                )
                installed_for_plan = cursor.fetchone()
                if installed_for_plan is not None:
                    result = self._installation_with_cursor(
                        cursor, installed_for_plan[0]
                    )
                    assert result is not None
                    self._insert_audit(
                        cursor,
                        installed_metric_id=result.id,
                        plan_id=plan.id,
                        action="reused",
                        actor=command.actor,
                        evidence={
                            "planId": str(plan.id),
                            "planDigest": plan.digest,
                            "reusedInstallationId": str(result.id),
                        },
                        idempotency_key=command.idempotency_key,
                        request_digest=request_digest,
                        resulting_state=result.state,
                    )
                    return result
                cursor.execute(
                    """
                    SELECT current_version FROM t_site_configuration_state
                    WHERE singleton = TRUE FOR UPDATE
                    """
                )
                current_version = int(cursor.fetchone()[0])
                node = PostgresBusinessMetricCatalog.get_node_with_cursor(
                    cursor, plan.node_id
                )
                candidates = PostgresBusinessMetricCatalog.list_sources_with_cursor(
                    cursor, plan.node_id
                )
                refreshed = _compile_plan_from_state(
                    request=PreviewMetricInstallation(
                        plan.node_id, plan.template_id, command.actor
                    ),
                    template=template,
                    node=node,
                    catalog=_CursorBusinessMetricCatalog(cursor),
                    candidates=candidates,
                    site_configuration_version=current_version,
                    existing_installations=self._installed_for_node_with_cursor(
                        cursor, plan.node_id
                    ),
                )
                if refreshed.digest != plan.digest:
                    if refreshed.blockers and refreshed.blockers[0]["code"] in {
                        "BUSINESS_METRIC_SOURCE_INCOMPATIBLE",
                        "BUSINESS_METRIC_TIMEZONE_INVALID",
                    }:
                        code = refreshed.blockers[0]["code"]
                        raise BusinessMetricError(
                            code,
                            "Business metric source or timezone contract changed",
                        )
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_PLAN_STALE",
                        "Plan sources or site configuration changed",
                    )

                compiled = compile_business_metric(
                    template,
                    MetricSourceResolution(plan.timezone or "", plan.sources),
                )
                if compiled.content_digest != plan.internal_processing_digest:
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_PLAN_STALE",
                        "Internal processing contract changed",
                    )
                (
                    package_record_id,
                    solution_installation_id,
                    identity_installation_id,
                    next_version,
                ) = self._create_solution_lineage(
                    cursor, plan, command.actor, current_version
                )
                persist_internal_business_metric_asset(
                    cursor,
                    compiled.point_processing_asset,
                    package_record_id=package_record_id,
                )
                source_candidates = {item.entity_instance_id: item for item in candidates}
                point_sources = tuple(
                    PointProcessingSource(
                        source.entity_instance_id,
                        "l2",
                        source_candidates[source.entity_instance_id].node_id,
                        source.entity_definition_id,
                        source.data_type,
                        source.unit,
                        True,
                    )
                    for source in plan.sources
                )
                point_catalog = InMemoryPointProcessingCatalog(
                    templates={
                        compiled.processing_revision_id: compiled.point_processing_asset
                    },
                    sources=point_sources,
                )
                point_plan = compile_point_processing_plan(
                    PreviewPointProcessing(
                        node_id=plan.node_id,
                        template_revision_id=compiled.processing_revision_id,
                        input_selections={
                            f"source_{index + 1}": source.entity_instance_id
                            for index, source in enumerate(plan.sources)
                        },
                        actor=command.actor,
                        entity_identity_installation_id=identity_installation_id,
                        planned_output_entity_ids={
                            "metric_value": plan.output_entity_instance_id
                        },
                        solution_installation_id=solution_installation_id,
                    ),
                    point_catalog,
                    _PointPlanningRepository(current_version),
                )
                if point_plan.blockers:
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_INTERNAL_PLAN_BLOCKED",
                        "Internal point-processing plan has blockers",
                    )
                point_repository = PostgresPointProcessingRepository()
                point_repository.save_plan(point_plan, transaction=connection)
                try:
                    application = point_repository.apply_plan(
                        ApplyPointProcessingPlan(
                            plan_id=point_plan.id,
                            plan_digest=point_plan.digest,
                            idempotency_key=(
                                f"business-metric:{command.idempotency_key}"
                            ),
                            actor=command.actor,
                        ),
                        PostgresPointProcessingCatalog(),
                        transaction=connection,
                        verified_source_catalog_digest=point_plan.source_catalog_digest,
                        trusted_business_metric_owner_key=(
                            plan.output_entity_instance_id
                        ),
                    )
                except PointProcessingError as exc:
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_INTERNAL_APPLY_FAILED", str(exc)
                    ) from exc
                if application.site_configuration_version != next_version or tuple(
                    application.output_entity_instance_ids
                ) != (plan.output_entity_instance_id,):
                    raise BusinessMetricError(
                        "BUSINESS_METRIC_INTERNAL_APPLY_FAILED",
                        "Internal application output contract changed",
                    )
                cursor.execute(
                    "UPDATE t_site_configuration_state SET current_version = %s WHERE singleton = TRUE",
                    (next_version,),
                )
                installation_id = uuid5(
                    NAMESPACE_URL, f"zizu/business-metric/installation/{plan.digest}"
                )
                cursor.execute(
                    """
                    INSERT INTO t_installed_business_metrics
                      (id, node_id, entity_instance_id, template_revision_id,
                       installed_processing_id, source_plan_id,
                       site_configuration_version, frozen_timezone,
                       raw_detail_retention_days, state, installed_by,
                       idempotency_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'active', %s, %s)
                    """,
                    (
                        installation_id,
                        plan.node_id,
                        plan.output_entity_instance_id,
                        template_revision_id,
                        application.installed_processing_id,
                        plan.id,
                        next_version,
                        plan.timezone,
                        plan.raw_detail_retention_days,
                        command.actor,
                        command.idempotency_key,
                    ),
                )
                for ordinal, source in enumerate(plan.sources):
                    cursor.execute(
                        """
                        INSERT INTO t_business_metric_source_bindings
                          (installed_metric_id, ordinal, entity_instance_id,
                           entity_definition_id, method, data_type, unit,
                           direction, estimated, source_digest)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            installation_id,
                            ordinal,
                            source.entity_instance_id,
                            source.entity_definition_id,
                            source.method.value,
                            source.data_type,
                            source.unit,
                            source.direction,
                            source.estimated,
                            _digest(_source_content(source)),
                        ),
                    )
                capability = _capability_content(template, plan)
                capability_digest = _digest(capability)
                cursor.execute(
                    """
                    INSERT INTO t_entity_capability_contracts
                      (id, entity_instance_id, installed_metric_id,
                       temporal_semantics, control_eligible, content, digest)
                    VALUES (%s, %s, %s, 'windowed', FALSE, %s, %s)
                    """,
                    (
                        uuid5(
                            NAMESPACE_URL,
                            f"zizu/entity-capability/{installation_id}/{capability_digest}",
                        ),
                        plan.output_entity_instance_id,
                        installation_id,
                        Json(capability),
                        capability_digest,
                    ),
                )
                audit_evidence = {
                    "planId": str(plan.id),
                    "planDigest": plan.digest,
                    "pointProcessingPlanId": str(point_plan.id),
                    "pointProcessingApplicationId": str(application.id),
                    "siteConfigurationVersion": next_version,
                }
                self._insert_audit(
                    cursor,
                    installed_metric_id=installation_id,
                    plan_id=plan.id,
                    action=(
                        "upgraded"
                        if plan.previous_installation_id is not None
                        else "installed"
                    ),
                    actor=command.actor,
                    evidence=audit_evidence,
                    idempotency_key=command.idempotency_key,
                    request_digest=request_digest,
                    resulting_state="active",
                )
                result = self._installation_with_cursor(cursor, installation_id)
                assert result is not None
                return result

    @staticmethod
    def _insert_audit(
        cursor: Any,
        *,
        installed_metric_id: UUID,
        plan_id: UUID,
        action: str,
        actor: str,
        evidence: dict[str, Any],
        idempotency_key: str | None,
        request_digest: str | None,
        resulting_state: str | None,
    ) -> None:
        audit_content = {
            "action": action,
            "actor": actor,
            "evidence": evidence,
            "idempotency_key": idempotency_key,
            "request_digest": request_digest,
            "resulting_state": resulting_state,
        }
        audit_digest = _digest(audit_content)
        cursor.execute(
            """
            INSERT INTO t_business_metric_audit
              (id, installed_metric_id, plan_id, action, actor,
               idempotency_key, request_digest, resulting_state,
               evidence, digest)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                uuid5(
                    NAMESPACE_URL,
                    f"zizu/business-metric-audit/{installed_metric_id}/{audit_digest}",
                ),
                installed_metric_id,
                plan_id,
                action,
                actor,
                idempotency_key,
                request_digest,
                resulting_state,
                Json(evidence),
                audit_digest,
            ),
        )

    @staticmethod
    def _installation_select() -> str:
        return """
            SELECT installed.id, installed.node_id, template.template_key,
                   revision.revision, installed.entity_instance_id,
                   processing.revision_id, installed.frozen_timezone,
                   installed.site_configuration_version, installed.state,
                   plan.digest, processing.id
            FROM t_installed_business_metrics AS installed
            JOIN t_business_metric_revisions AS revision
              ON revision.id = installed.template_revision_id
            JOIN t_business_metric_templates AS template
              ON template.id = revision.template_id
            JOIN t_installed_point_processings AS processing
              ON processing.id = installed.installed_processing_id
            JOIN t_business_metric_installation_plans AS plan
              ON plan.id = installed.source_plan_id
        """

    @classmethod
    def _installation_with_cursor(
        cls, cursor: Any, installation_id: UUID
    ) -> MetricInstallation | None:
        cursor.execute(
            cls._installation_select() + " WHERE installed.id = %s",
            (installation_id,),
        )
        row = cursor.fetchone()
        return MetricInstallation(*row) if row else None

    @classmethod
    def _installed_for_node_with_cursor(
        cls, cursor: Any, node_id: UUID
    ) -> tuple[MetricInstallation, ...]:
        cursor.execute(
            cls._installation_select()
            + " WHERE installed.node_id = %s AND processing.current = TRUE "
            "ORDER BY installed.installed_at",
            (node_id,),
        )
        return tuple(MetricInstallation(*row) for row in cursor.fetchall())

    @staticmethod
    def _load_plan(
        cursor: Any, plan_id: UUID
    ) -> tuple[
        MetricInstallationPlan | None,
        BusinessMetricTemplate | None,
        UUID | None,
    ]:
        cursor.execute(
            """
            SELECT plan.id, plan.node_id, template.template_key,
                   revision.revision, revision.content_digest,
                   plan.frozen_timezone, plan.raw_detail_retention_days,
                   plan.base_site_configuration_version,
                   plan.internal_processing_digest,
                   plan.previous_installation_id,
                   plan.status, plan.digest,
                   plan.planned_by, revision.content, revision.id
            FROM t_business_metric_installation_plans AS plan
            JOIN t_business_metric_revisions AS revision
              ON revision.id = plan.template_revision_id
            JOIN t_business_metric_templates AS template
              ON template.id = revision.template_id
            WHERE plan.id = %s
            FOR SHARE
            """,
            (plan_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None, None, None
        template = parse_business_metric_asset(row[13])
        cursor.execute(
            """
            SELECT item_kind, source_entity_instance_id, method, estimated,
                   blocker_code, after_value
            FROM t_business_metric_plan_items
            WHERE plan_id = %s ORDER BY ordinal
            """,
            (plan_id,),
        )
        sources: list[ResolvedMetricSource] = []
        blockers: list[dict[str, str]] = []
        output_entity_id: UUID | None = None
        for kind, source_id, method, estimated, blocker_code, after in cursor.fetchall():
            if kind == "source":
                sources.append(
                    ResolvedMetricSource(
                        source_id,
                        after["entity_definition_id"],
                        MetricAggregator(method),
                        after["data_type"],
                        after.get("unit"),
                        bool(estimated),
                        after.get("direction", "R"),
                    )
                )
            elif kind == "output":
                output_entity_id = UUID(after["entityInstanceId"])
            elif kind == "blocker":
                blockers.append({"code": blocker_code})
        plan = MetricInstallationPlan(
            id=row[0],
            node_id=row[1],
            template_id=row[2],
            template_revision=int(row[3]),
            template_digest=row[4].strip(),
            timezone=row[5],
            raw_detail_retention_days=row[6],
            site_configuration_version=int(row[7]),
            sources=tuple(sources),
            internal_processing_digest=(row[8].strip() if row[8] else None),
            output_entity_instance_id=output_entity_id,
            status=row[10],
            blockers=tuple(blockers),
            digest=row[11].strip(),
            planned_by=row[12],
            previous_installation_id=row[9],
        )
        return plan, template, row[14]

    @staticmethod
    def _create_solution_lineage(
        cursor: Any,
        plan: MetricInstallationPlan,
        actor: str,
        current_version: int,
    ) -> tuple[UUID, UUID, UUID, int]:
        cursor.execute(
            """
            SELECT installation.entity_instance_ids, site.package_record_id,
                   site.package_digest, site.parameters,
                   site.secret_references, site.parameter_metadata,
                   site.configuration_digest,
                   site.entity_identity_installation_id,
                   source_plan.parameter_contracts,
                   source_plan.parameter_sources,
                   source_plan.entity_plan, source_plan.alarm_plan
            FROM t_site_configuration_versions AS site
            JOIN t_solution_installations AS installation
              ON installation.id = site.installation_id
            JOIN t_solution_install_plans AS source_plan
              ON source_plan.id = installation.plan_id
            WHERE site.version = %s
            """,
            (current_version,),
        )
        current = cursor.fetchone()
        if current is None or current[7] is None:
            raise BusinessMetricError(
                "BUSINESS_METRIC_PLAN_STALE",
                "An installed solution identity is required",
            )
        next_version = current_version + 1
        configuration_digest = _digest(
            {
                "previousConfigurationDigest": current[6].strip(),
                "businessMetricPlanDigest": plan.digest,
            }
        )
        lineage_digest = _digest(
            {
                "kind": "business_metric",
                "planId": str(plan.id),
                "planDigest": plan.digest,
                "baseSiteConfigurationVersion": current_version,
                "siteConfigurationVersion": next_version,
            }
        )
        derived_plan_id = uuid5(
            NAMESPACE_URL, f"zizu/business-metric-solution-plan/{lineage_digest}"
        )
        derived_installation_id = uuid5(
            NAMESPACE_URL,
            f"zizu/business-metric-solution-installation/{lineage_digest}",
        )
        cursor.execute(
            """
            INSERT INTO t_solution_install_plans
              (id, package_record_id, package_digest,
               base_site_configuration_version, status, items, blockers,
               parameter_contracts, parameters, secret_references,
               parameter_sources, parameter_metadata, configuration_digest,
               target_installation_id, entity_identity_installation_id,
               entity_plan, alarm_plan, point_processing_plans, digest)
            VALUES (%s, %s, %s, %s, 'ready', %s, '[]', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, '[]', %s)
            """,
            (
                derived_plan_id,
                current[1],
                current[2],
                current_version,
                Json(
                    [
                        {
                            "asset_id": plan.template_id,
                            "kind": "business_metric",
                            "action": "add",
                            "business_metric_plan_id": str(plan.id),
                        }
                    ]
                ),
                Json(current[8]),
                Json(current[3]),
                Json(current[4]),
                Json(current[9]),
                Json(current[5]),
                configuration_digest,
                derived_installation_id,
                current[7],
                Json(current[10]) if current[10] is not None else None,
                Json(current[11]) if current[11] is not None else None,
                lineage_digest,
            ),
        )
        cursor.execute(
            """
            INSERT INTO t_solution_installations
              (id, plan_id, package_record_id, package_digest,
               site_configuration_version, status, entity_instance_ids)
            VALUES (%s, %s, %s, %s, %s, 'installed', %s)
            """,
            (
                derived_installation_id,
                derived_plan_id,
                current[1],
                current[2],
                next_version,
                list(current[0]),
            ),
        )
        cursor.execute(
            """
            INSERT INTO t_site_configuration_versions
              (version, previous_version, installation_id,
               package_record_id, package_digest, parameters,
               secret_references, parameter_metadata, configuration_digest,
               actor, entity_identity_installation_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                next_version,
                current_version,
                derived_installation_id,
                current[1],
                current[2],
                Json(current[3]),
                Json(current[4]),
                Json(current[5]),
                configuration_digest,
                actor,
                current[7],
            ),
        )
        cursor.execute(
            """
            INSERT INTO t_solution_delivery_audit
              (id, actor, action, installation_id, package_record_id,
               package_digest, site_configuration_version, details)
            VALUES (%s, %s, 'solution.install', %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                actor,
                derived_installation_id,
                current[1],
                current[2],
                next_version,
                Json(
                    {
                        "kind": "business_metric",
                        "plan_id": str(plan.id),
                        "plan_digest": plan.digest,
                        "configuration_digest": configuration_digest,
                    }
                ),
            ),
        )
        return current[1], derived_installation_id, current[7], next_version
