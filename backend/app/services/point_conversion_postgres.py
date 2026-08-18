"""PostgreSQL adapters for immutable L1 point-conversion assets and plans."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg2
from psycopg2.extras import Json

from app.services.point_conversion import (
    ApplyPointConversionPlan,
    CurrentPointConversionContext,
    PointConversion,
    PointConversionApplication,
    PointConversionCatalog,
    PointConversionError,
    PointConversionPlan,
    PointConversionSource,
    PointConversionTemplateSummary,
    _template_source_catalog_digest,
)
from app.services.solution_delivery_contracts import DeliveryError, PackageImport
from app.services.solution_point_conversions import (
    PointConversionAsset,
    PointConversionInput,
    PointConversionOutput,
    point_conversion_assets,
    point_conversion_revision_id,
    point_conversion_template_id,
)


def _input_id(revision_id: UUID, input_key: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"zizu/point-conversion-input/{revision_id}/{input_key}",
    )


def _output_id(revision_id: UUID, output_key: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"zizu/point-conversion-output/{revision_id}/{output_key}",
    )


def persist_point_conversion_assets(
    cursor: Any,
    package: PackageImport,
    actor: str,
) -> None:
    """Persist the validated L1 catalog inside the package transaction."""
    if not actor.strip():
        raise DeliveryError(
            "PACKAGE_IMPORT_ACTOR_INVALID",
            "Package import actor is required",
        )
    try:
        for asset in point_conversion_assets(package):
            template_id = point_conversion_template_id(asset)
            cursor.execute(
                """
                INSERT INTO t_point_conversion_templates
                  (id, asset_id, device_category, brand, model,
                   display_name, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (asset_id, brand, model) DO NOTHING
                """,
                (
                    template_id,
                    asset.asset_id,
                    asset.device_category,
                    asset.brand,
                    asset.model,
                    asset.display_name,
                    asset.status,
                ),
            )
            cursor.execute(
                """
                SELECT id, device_category, display_name, status
                FROM t_point_conversion_templates
                WHERE asset_id = %s AND brand = %s AND model = %s
                FOR UPDATE
                """,
                (asset.asset_id, asset.brand, asset.model),
            )
            template_row = cursor.fetchone()
            if template_row is None:
                raise DeliveryError(
                    "POINT_CONVERSION_CATALOG_UNAVAILABLE",
                    "Point conversion template row disappeared",
                )
            template_id = template_row[0]
            if (
                template_row[1] != asset.device_category
                or template_row[2] != asset.display_name
            ):
                raise DeliveryError(
                    "POINT_CONVERSION_TEMPLATE_CONFLICT",
                    "Point conversion template identity conflicts with stored content",
                )
            if template_row[3] != asset.status:
                cursor.execute(
                    """
                    UPDATE t_point_conversion_templates
                    SET status = %s
                    WHERE id = %s
                    """,
                    (asset.status, template_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_audit_events
                      (id, event, outcome, actor, target, details)
                    VALUES (%s, 'point_conversion.template_status', 'allowed',
                            %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        actor,
                        f"point-conversion-template:{template_id}",
                        Json(
                            {
                                "asset_id": asset.asset_id,
                                "before": template_row[3],
                                "after": asset.status,
                                "package_record_id": str(package.id),
                            }
                        ),
                    ),
                )

            revision_id = uuid5(
                NAMESPACE_URL,
                f"zizu/point-conversion-revision/{template_id}/{asset.revision}",
            )
            cursor.execute(
                """
                INSERT INTO t_point_conversion_revisions
                  (id, template_id, revision, content_digest, published_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (template_id, revision) DO NOTHING
                """,
                (
                    revision_id,
                    template_id,
                    asset.revision,
                    asset.content_digest,
                    datetime.now(timezone.utc),
                ),
            )
            cursor.execute(
                """
                SELECT id, content_digest
                FROM t_point_conversion_revisions
                WHERE template_id = %s AND revision = %s
                """,
                (template_id, asset.revision),
            )
            revision_row = cursor.fetchone()
            if revision_row is None or revision_row[1].strip() != asset.content_digest:
                raise DeliveryError(
                    "POINT_CONVERSION_REVISION_CONFLICT",
                    "Immutable point conversion revision has different content",
                )
            revision_id = revision_row[0]
            cursor.execute(
                """
                INSERT INTO t_solution_point_conversion_assets
                  (package_record_id, template_revision_id, asset_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (package_record_id, asset_id) DO NOTHING
                """,
                (package.id, revision_id, asset.asset_id),
            )
            cursor.execute(
                """
                SELECT template_revision_id
                FROM t_solution_point_conversion_assets
                WHERE package_record_id = %s AND asset_id = %s
                """,
                (package.id, asset.asset_id),
            )
            relation = cursor.fetchone()
            if relation is None or relation[0] != revision_id:
                raise DeliveryError(
                    "POINT_CONVERSION_REVISION_CONFLICT",
                    "Package point conversion relation conflicts with stored content",
                )

            input_ids: dict[str, UUID] = {}
            for item in asset.inputs:
                input_id = _input_id(revision_id, item.input_id)
                input_ids[item.input_id] = input_id
                cursor.execute(
                    """
                    INSERT INTO t_point_conversion_inputs
                      (id, revision_id, input_key, source_kind, data_type, unit,
                       required, stable_source_key, aliases)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (revision_id, input_key) DO NOTHING
                    """,
                    (
                        input_id,
                        revision_id,
                        item.input_id,
                        item.source_kind,
                        item.data_type,
                        item.unit,
                        item.required,
                        item.source_key,
                        list(item.aliases),
                    ),
                )

            for output in asset.outputs:
                output_id = _output_id(revision_id, output.output_id)
                cursor.execute(
                    """
                    INSERT INTO t_point_conversion_outputs
                      (id, revision_id, output_key, entity_definition_id,
                       data_type, unit, freshness_seconds)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (revision_id, output_key) DO NOTHING
                    """,
                    (
                        output_id,
                        revision_id,
                        output.output_id,
                        output.entity_definition_id,
                        output.data_type,
                        output.unit,
                        output.freshness_seconds,
                    ),
                )
                transform = output.transform
                transform_input_id = input_ids[str(transform["input"])]
                if transform["kind"] == "numeric":
                    cursor.execute(
                        """
                        INSERT INTO t_numeric_transform_rules
                          (output_id, input_id, scale, "offset", minimum, maximum)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (output_id) DO NOTHING
                        """,
                        (
                            output_id,
                            transform_input_id,
                            transform["scale"],
                            transform["offset"],
                            transform["minimum"],
                            transform["maximum"],
                        ),
                    )
                elif transform["kind"] == "enum":
                    cursor.execute(
                        """
                        INSERT INTO t_enum_transform_rules (output_id, input_id)
                        VALUES (%s, %s) ON CONFLICT (output_id) DO NOTHING
                        """,
                        (output_id, transform_input_id),
                    )
                    for raw_value, canonical_value in transform["entries"].items():
                        cursor.execute(
                            """
                            INSERT INTO t_enum_mapping_entries
                              (output_id, raw_value, canonical_value)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (output_id, raw_value) DO NOTHING
                            """,
                            (output_id, raw_value, canonical_value),
                        )
                else:
                    cursor.execute(
                        """
                        INSERT INTO t_fault_code_transform_rules
                          (output_id, input_id, delimiter)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (output_id) DO NOTHING
                        """,
                        (output_id, transform_input_id, transform["delimiter"]),
                    )
                    for raw_code, entry in transform["entries"].items():
                        cursor.execute(
                            """
                            INSERT INTO t_fault_code_mapping_entries
                              (output_id, raw_code, canonical_code,
                               display_name, default_severity)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (output_id, raw_code) DO NOTHING
                            """,
                            (
                                output_id,
                                raw_code,
                                entry["code"],
                                entry["name"],
                                entry["defaultSeverity"],
                            ),
                        )
    except DeliveryError:
        raise
    except psycopg2.Error as exc:
        raise DeliveryError(
            "POINT_CONVERSION_CATALOG_UNAVAILABLE",
            "Point conversion catalog could not be persisted",
        ) from exc


class PostgresPointConversionCatalog:
    @staticmethod
    @contextmanager
    def _connection():
        from app.services.telemetry_store import get_connection

        with get_connection() as connection:
            yield connection

    def get_template(self, revision_id: UUID) -> PointConversionAsset | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self._load_asset(cursor, revision_id)

    def list_templates(
        self,
        device_category: str,
    ) -> tuple[PointConversionTemplateSummary, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT revision.id
                    FROM t_point_conversion_revisions AS revision
                    JOIN t_point_conversion_templates AS template
                      ON template.id = revision.template_id
                    WHERE upper(template.device_category) = upper(%s)
                      AND template.status = 'active'
                    ORDER BY template.asset_id, revision.revision, revision.id
                    """,
                    (device_category,),
                )
                revision_ids = tuple(row[0] for row in cursor.fetchall())
                return tuple(
                    PointConversionTemplateSummary(revision_id, asset)
                    for revision_id in revision_ids
                    if (asset := self._load_asset(cursor, revision_id)) is not None
                )

    def node_source_key(self, node_id: UUID) -> str | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COALESCE(source_catalog_key, name)
                    FROM t_nodes
                    WHERE id = %s AND enabled = TRUE
                      AND (
                        source_catalog_key IS NOT NULL
                        OR NOT EXISTS (
                          SELECT 1 FROM t_nodes duplicate
                          WHERE duplicate.name = t_nodes.name
                            AND duplicate.id <> t_nodes.id
                        )
                      )
                    """,
                    (node_id,),
                )
                row = cursor.fetchone()
                return row[0] if row is not None else None

    def list_sources(self, node_id: UUID) -> tuple[PointConversionSource, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self.list_sources_with_cursor(cursor, node_id)

    @staticmethod
    def list_sources_with_cursor(
        cursor: Any,
        node_id: UUID,
    ) -> tuple[PointConversionSource, ...]:
        cursor.execute(
            """
            SELECT tag.id, 'l0', tag.node_id, tag.name,
                   tag.data_type, COALESCE(tag.unit_to, tag.unit), TRUE
            FROM t_tags AS tag
            WHERE tag.node_id = %s AND tag.enabled = TRUE
            UNION ALL
            SELECT entity.id, 'l2', device.node_id,
                   entity.definition_id, entity.data_type, entity.unit, TRUE
            FROM t_entity_instances AS entity
            JOIN t_device_instances AS device
              ON device.id = entity.device_instance_id
            WHERE EXISTS (
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
                    FROM t_conversion_output_bindings AS output_binding
                    JOIN t_installed_point_conversions AS installed
                      ON installed.id = output_binding.installed_conversion_id
                     AND installed.current = TRUE
                    WHERE output_binding.entity_instance_id = entity.id
                  )
            ORDER BY 2, 1
            """,
            (node_id,),
        )
        return tuple(PointConversionSource(*row) for row in cursor.fetchall())

    @staticmethod
    def _load_asset(cursor: Any, revision_id: UUID) -> PointConversionAsset | None:
        cursor.execute(
            """
            SELECT template.asset_id, template.display_name,
                   template.device_category, template.brand, template.model,
                   revision.revision, template.status, revision.content_digest
            FROM t_point_conversion_revisions AS revision
            JOIN t_point_conversion_templates AS template
              ON template.id = revision.template_id
            WHERE revision.id = %s
            """,
            (revision_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            SELECT input_key, source_kind, stable_source_key, aliases,
                   data_type, unit, required
            FROM t_point_conversion_inputs
            WHERE revision_id = %s
            ORDER BY input_key
            """,
            (revision_id,),
        )
        inputs = tuple(
            PointConversionInput(
                input_id=item[0],
                source_kind=item[1],
                source_key=item[2],
                aliases=tuple(item[3]),
                data_type=item[4],
                unit=item[5],
                required=item[6],
            )
            for item in cursor.fetchall()
        )
        cursor.execute(
            """
            SELECT id, output_key, entity_definition_id, data_type, unit,
                   freshness_seconds
            FROM t_point_conversion_outputs
            WHERE revision_id = %s
            ORDER BY entity_definition_id
            """,
            (revision_id,),
        )
        outputs = tuple(
            PointConversionOutput(
                output_id=output_row[1],
                entity_definition_id=output_row[2],
                data_type=output_row[3],
                unit=output_row[4],
                freshness_seconds=float(output_row[5]),
                transform=MappingProxyType(
                    PostgresPointConversionCatalog._load_transform(
                        cursor,
                        output_row[0],
                    )
                ),
            )
            for output_row in cursor.fetchall()
        )
        return PointConversionAsset(
            asset_id=row[0],
            display_name=row[1],
            device_category=row[2],
            brand=row[3],
            model=row[4],
            revision=row[5],
            status=row[6],
            content_digest=row[7].strip(),
            inputs=inputs,
            outputs=outputs,
        )

    @staticmethod
    def _load_transform(cursor: Any, output_id: UUID) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT input.input_key, rule.scale, rule."offset",
                   rule.minimum, rule.maximum
            FROM t_numeric_transform_rules AS rule
            JOIN t_point_conversion_inputs AS input ON input.id = rule.input_id
            WHERE rule.output_id = %s
            """,
            (output_id,),
        )
        row = cursor.fetchone()
        if row is not None:
            return {
                "kind": "numeric",
                "input": row[0],
                "scale": row[1],
                "offset": row[2],
                "minimum": row[3],
                "maximum": row[4],
            }
        cursor.execute(
            """
            SELECT input.input_key
            FROM t_enum_transform_rules AS rule
            JOIN t_point_conversion_inputs AS input ON input.id = rule.input_id
            WHERE rule.output_id = %s
            """,
            (output_id,),
        )
        row = cursor.fetchone()
        if row is not None:
            cursor.execute(
                """
                SELECT raw_value, canonical_value
                FROM t_enum_mapping_entries
                WHERE output_id = %s ORDER BY raw_value
                """,
                (output_id,),
            )
            return {
                "kind": "enum",
                "input": row[0],
                "entries": MappingProxyType(dict(cursor.fetchall())),
            }
        cursor.execute(
            """
            SELECT input.input_key, rule.delimiter
            FROM t_fault_code_transform_rules AS rule
            JOIN t_point_conversion_inputs AS input ON input.id = rule.input_id
            WHERE rule.output_id = %s
            """,
            (output_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise PointConversionError(
                "POINT_CONVERSION_CATALOG_INVALID",
                "Point conversion output has no transform",
            )
        cursor.execute(
            """
            SELECT raw_code, canonical_code, display_name, default_severity
            FROM t_fault_code_mapping_entries
            WHERE output_id = %s ORDER BY raw_code
            """,
            (output_id,),
        )
        entries = {
            item[0]: MappingProxyType(
                {
                    "code": item[1],
                    "name": item[2],
                    "defaultSeverity": item[3],
                }
            )
            for item in cursor.fetchall()
        }
        return {
            "kind": "fault_codes",
            "input": row[0],
            "delimiter": row[1],
            "entries": MappingProxyType(entries),
        }


class PostgresPointConversionRepository:
    @staticmethod
    @contextmanager
    def _connection(transaction: Any | None = None):
        if transaction is not None:
            yield transaction
            return
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

    def current_context(
        self,
        node_id: UUID,
    ) -> CurrentPointConversionContext | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT installed.id, installed.solution_installation_id,
                           installed.revision_id,
                           site.entity_identity_installation_id
                    FROM t_installed_point_conversions AS installed
                    JOIN t_site_configuration_versions AS site
                      ON site.version = installed.site_configuration_version
                    WHERE installed.node_id = %s AND installed.current = TRUE
                    """,
                    (node_id,),
                )
                installed = cursor.fetchone()
                if installed is None:
                    return None
                cursor.execute(
                    """
                    SELECT input.input_key,
                           COALESCE(binding.l0_tag_id, binding.l2_entity_instance_id)
                    FROM t_conversion_input_bindings AS binding
                    JOIN t_point_conversion_inputs AS input
                      ON input.id = binding.input_id
                    WHERE binding.installed_conversion_id = %s
                    ORDER BY input.input_key
                    """,
                    (installed[0],),
                )
                input_ids = dict(cursor.fetchall())
                cursor.execute(
                    """
                    SELECT output.output_key, binding.entity_instance_id
                    FROM t_conversion_output_bindings AS binding
                    JOIN t_point_conversion_outputs AS output
                      ON output.id = binding.output_id
                    WHERE binding.installed_conversion_id = %s
                    ORDER BY output.output_key
                    """,
                    (installed[0],),
                )
                output_ids = dict(cursor.fetchall())
                return CurrentPointConversionContext(
                    entity_identity_installation_id=installed[3],
                    solution_installation_id=installed[1],
                    revision_id=installed[2],
                    input_source_ids=input_ids,
                    output_entity_ids=output_ids,
                )

    def save_plan(self, plan: PointConversionPlan) -> PointConversionPlan:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO t_point_conversion_plans
                          (id, node_id, template_revision_id,
                           entity_identity_installation_id,
                           solution_installation_id,
                           base_site_configuration_version,
                           source_catalog_digest, status, items, blockers,
                           digest, planned_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            plan.id,
                            plan.node_id,
                            plan.template_revision_id,
                            plan.entity_identity_installation_id,
                            plan.solution_installation_id,
                            plan.base_site_configuration_version,
                            plan.source_catalog_digest,
                            plan.status,
                            Json([_plain(item) for item in plan.items]),
                            Json([_plain(item) for item in plan.blockers]),
                            plan.digest,
                            plan.planned_by,
                        ),
                    )
                    cursor.execute(
                        "SELECT digest FROM t_point_conversion_plans WHERE id = %s",
                        (plan.id,),
                    )
                    stored = cursor.fetchone()
                    if stored is None or stored[0].strip() != plan.digest:
                        raise PointConversionError(
                            "POINT_CONVERSION_PLAN_CONFLICT",
                            "Point conversion plan identity conflicts with stored evidence",
                        )
                    self._persist_plan_items(cursor, plan)
                    return plan
        except PointConversionError:
            raise
        except psycopg2.Error as exc:
            raise PointConversionError(
                "DATA_TRUNK_UNAVAILABLE",
                "Point conversion plan could not be persisted",
            ) from exc

    @staticmethod
    def _persist_plan_items(cursor: Any, plan: PointConversionPlan) -> None:
        for item in plan.items:
            input_id = None
            output_id = None
            source_kind = None
            selected_tag_id = None
            selected_entity_id = None
            if item["kind"] == "input_binding":
                cursor.execute(
                    """
                    SELECT id, source_kind
                    FROM t_point_conversion_inputs
                    WHERE revision_id = %s AND input_key = %s
                    """,
                    (plan.template_revision_id, item["input_id"]),
                )
                relation = cursor.fetchone()
                if relation is None:
                    raise PointConversionError(
                        "POINT_CONVERSION_PLAN_STALE",
                        "Point conversion input relation is missing",
                    )
                input_id, source_kind = relation
                selected = item.get("selected_source_id")
                if selected is not None:
                    if source_kind == "l0":
                        selected_tag_id = UUID(selected)
                    else:
                        selected_entity_id = UUID(selected)
            else:
                cursor.execute(
                    """
                    SELECT id FROM t_point_conversion_outputs
                    WHERE revision_id = %s AND output_key = %s
                    """,
                    (plan.template_revision_id, item["output_id"]),
                )
                relation = cursor.fetchone()
                if relation is None:
                    raise PointConversionError(
                        "POINT_CONVERSION_PLAN_STALE",
                        "Point conversion output relation is missing",
                    )
                output_id = relation[0]
            cursor.execute(
                """
                INSERT INTO t_point_conversion_plan_items
                  (plan_id, item_key, action, input_id, output_id, source_kind,
                   selected_tag_id, selected_entity_instance_id,
                   output_entity_instance_id, blocker_code, before_value,
                   after_value)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
                ON CONFLICT (plan_id, item_key) DO NOTHING
                """,
                (
                    plan.id,
                    item["item_key"],
                    item["action"],
                    input_id,
                    output_id,
                    source_kind,
                    selected_tag_id,
                    selected_entity_id,
                    (
                        UUID(item["output_entity_instance_id"])
                        if item.get("output_entity_instance_id")
                        else None
                    ),
                    item.get("blocker_code"),
                    Json(_plain(item)),
                ),
            )

    def get_plan(self, plan_id: UUID) -> PointConversionPlan | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, node_id, template_revision_id,
                           entity_identity_installation_id,
                           solution_installation_id,
                           base_site_configuration_version,
                           source_catalog_digest, status, items, blockers,
                           digest, planned_by
                    FROM t_point_conversion_plans WHERE id = %s
                    """,
                    (plan_id,),
                )
                row = cursor.fetchone()
                return _plan_from_row(row) if row is not None else None

    def apply_plan(
        self,
        command: ApplyPointConversionPlan,
        catalog: PointConversionCatalog,
        *,
        transaction: Any | None = None,
    ) -> PointConversionApplication:
        del catalog
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
        external_transaction = transaction is not None
        try:
            with self._connection(transaction) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"point-conversion:{command.actor}:{command.idempotency_key}",),
                    )
                    cursor.execute(
                        """
                        SELECT application.id, application.plan_id,
                               application.installed_conversion_id,
                               application.solution_installation_id,
                               installed.revision_id,
                               application.site_configuration_version,
                               application.output_entity_instance_ids,
                               application.actor,
                               idempotency.request_digest
                        FROM t_point_conversion_idempotency AS idempotency
                        JOIN t_point_conversion_applications AS application
                          ON application.id = idempotency.application_id
                        JOIN t_installed_point_conversions AS installed
                          ON installed.id = application.installed_conversion_id
                        WHERE idempotency.actor = %s
                          AND idempotency.idempotency_key = %s
                        """,
                        (command.actor, command.idempotency_key),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        if existing[8].strip() != request_digest:
                            raise PointConversionError(
                                "POINT_CONVERSION_IDEMPOTENCY_KEY_REUSED",
                                "Idempotency key was already used for a different request",
                            )
                        return _application_from_row(existing[:8])

                    cursor.execute(
                        """
                        SELECT current_version
                        FROM t_site_configuration_state
                        WHERE singleton = TRUE
                        FOR UPDATE
                        """
                    )
                    current_version = int(cursor.fetchone()[0])
                    cursor.execute(
                        """
                        SELECT id, node_id, template_revision_id,
                               entity_identity_installation_id,
                               solution_installation_id,
                               base_site_configuration_version,
                               source_catalog_digest, status, items, blockers,
                               digest, planned_by
                        FROM t_point_conversion_plans
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (command.plan_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise PointConversionError(
                            "POINT_CONVERSION_PLAN_NOT_FOUND",
                            "Point conversion plan was not found",
                        )
                    plan = _plan_from_row(row)
                    if plan.digest != command.plan_digest:
                        raise PointConversionError(
                            "POINT_CONVERSION_PLAN_DIGEST_MISMATCH",
                            "Point conversion plan digest does not match",
                        )
                    if plan.status != "ready" or plan.blockers:
                        raise PointConversionError(
                            "POINT_CONVERSION_PLAN_STALE",
                            "Point conversion plan is no longer ready",
                        )
                    if current_version != plan.base_site_configuration_version:
                        raise PointConversionError(
                            "POINT_CONVERSION_PLAN_STALE",
                            "Site configuration changed after planning",
                        )

                    cursor.execute(
                        """
                        LOCK TABLE t_tags, t_entity_instances,
                                   t_entity_instance_bindings,
                                   t_entity_binding_confirmations,
                                   t_conversion_output_bindings,
                                   t_installed_point_conversions
                        IN SHARE MODE
                        """
                    )
                    sources = PostgresPointConversionCatalog.list_sources_with_cursor(
                        cursor,
                        plan.node_id,
                    )
                    template = PostgresPointConversionCatalog._load_asset(
                        cursor,
                        plan.template_revision_id,
                    )
                    if template is None or template.status != "active":
                        raise PointConversionError(
                            "POINT_CONVERSION_PLAN_STALE",
                            "Point conversion template changed after planning",
                        )
                    if plan.source_catalog_digest != _template_source_catalog_digest(
                        template,
                        sources,
                        plan.node_id,
                    ):
                        raise PointConversionError(
                            "POINT_CONVERSION_PLAN_STALE",
                            "Point conversion source catalog changed after planning",
                        )
                    self._verify_package_ownership(cursor, plan, current_version)

                    cursor.execute(
                        """
                        SELECT id
                        FROM t_installed_point_conversions
                        WHERE node_id = %s AND current = TRUE
                        FOR UPDATE
                        """,
                        (plan.node_id,),
                    )
                    current_installed = cursor.fetchone()
                    if current_installed is not None:
                        cursor.execute(
                            """
                            UPDATE t_installed_point_conversions
                            SET current = FALSE
                            WHERE id = %s
                            """,
                            (current_installed[0],),
                        )

                    if external_transaction:
                        solution_installation_id = plan.solution_installation_id
                        next_version = current_version + 1
                    else:
                        solution_installation_id, next_version = (
                            self._create_derived_solution_lineage(
                                cursor,
                                plan,
                                command.actor,
                                current_version,
                            )
                        )

                    installed_id = uuid5(
                        NAMESPACE_URL,
                        f"zizu/installed-point-conversion/{plan.id}",
                    )
                    application_id = uuid5(
                        NAMESPACE_URL,
                        (
                            "zizu/point-conversion-application/"
                            f"{command.actor}/{command.idempotency_key}"
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_installed_point_conversions
                          (id, node_id, revision_id, source_plan_id,
                           solution_installation_id, site_configuration_version,
                           installed_by, current)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                        """,
                        (
                            installed_id,
                            plan.node_id,
                            plan.template_revision_id,
                            plan.id,
                            solution_installation_id,
                            next_version,
                            command.actor,
                        ),
                    )
                    output_ids = self._install_bindings(
                        cursor,
                        plan,
                        installed_id,
                        command.actor,
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_point_conversion_applications
                          (id, plan_id, installed_conversion_id,
                           solution_installation_id, site_configuration_version,
                           actor, output_entity_instance_ids)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            application_id,
                            plan.id,
                            installed_id,
                            solution_installation_id,
                            next_version,
                            command.actor,
                            list(output_ids),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_point_conversion_idempotency
                          (actor, idempotency_key, request_digest, application_id)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            command.actor,
                            command.idempotency_key,
                            request_digest,
                            application_id,
                        ),
                    )
                    cursor.execute(
                        "UPDATE t_point_conversion_plans SET status = 'applied' WHERE id = %s",
                        (plan.id,),
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_audit_events
                          (id, event, outcome, reason, actor, target, details)
                        VALUES (%s, 'configuration.change', 'applied',
                                'reviewed point conversion plan', %s, %s, %s)
                        """,
                        (
                            uuid4(),
                            command.actor,
                            f"POST /api/v1/point-conversion-plans/{plan.id}/apply",
                            Json(
                                {
                                    "kind": "point_conversion",
                                    "plan_id": str(plan.id),
                                    "plan_digest": plan.digest,
                                    "node_id": str(plan.node_id),
                                    "site_configuration_version": next_version,
                                }
                            ),
                        ),
                    )
                    return PointConversionApplication(
                        id=application_id,
                        plan_id=plan.id,
                        installed_conversion_id=installed_id,
                        solution_installation_id=solution_installation_id,
                        revision_id=plan.template_revision_id,
                        site_configuration_version=next_version,
                        output_entity_instance_ids=output_ids,
                        actor=command.actor,
                    )
        except PointConversionError:
            raise
        except psycopg2.Error as exc:
            raise PointConversionError(
                "DATA_TRUNK_UNAVAILABLE",
                "Point conversion application could not be committed",
            ) from exc

    @staticmethod
    def _verify_package_ownership(
        cursor: Any,
        plan: PointConversionPlan,
        current_version: int,
    ) -> None:
        cursor.execute(
            """
            SELECT 1
            FROM t_solution_point_conversion_assets AS asset
            WHERE asset.template_revision_id = %s
              AND asset.package_record_id = COALESCE(
                (
                  SELECT package_record_id
                  FROM t_site_configuration_versions
                  WHERE version = %s
                ),
                (
                  SELECT package_record_id
                  FROM t_solution_install_plans
                  WHERE target_installation_id = %s
                )
              )
            """,
            (
                plan.template_revision_id,
                current_version,
                plan.solution_installation_id,
            ),
        )
        if cursor.fetchone() is None:
            raise PointConversionError(
                "POINT_CONVERSION_PLAN_STALE",
                "Point conversion revision does not belong to the installed solution",
            )

    @staticmethod
    def _install_bindings(
        cursor: Any,
        plan: PointConversionPlan,
        installed_id: UUID,
        actor: str,
    ) -> tuple[UUID, ...]:
        output_entity_ids: list[UUID] = []
        for item in plan.items:
            if item["action"] == "block":
                raise PointConversionError(
                    "POINT_CONVERSION_PLAN_BLOCKED",
                    "Point conversion plan contains a blocked item",
                )
            if item["kind"] == "input_binding":
                cursor.execute(
                    """
                    SELECT id, source_kind
                    FROM t_point_conversion_inputs
                    WHERE revision_id = %s AND input_key = %s
                    """,
                    (plan.template_revision_id, item["input_id"]),
                )
                relation = cursor.fetchone()
                if relation is None or item.get("selected_source_id") is None:
                    raise PointConversionError(
                        "POINT_CONVERSION_PLAN_STALE",
                        "Point conversion input relation is no longer available",
                    )
                selected_id = UUID(item["selected_source_id"])
                cursor.execute(
                    """
                    INSERT INTO t_conversion_input_bindings
                      (installed_conversion_id, input_id, source_kind,
                       l0_tag_id, l2_entity_instance_id, confirmed_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        installed_id,
                        relation[0],
                        relation[1],
                        selected_id if relation[1] == "l0" else None,
                        selected_id if relation[1] == "l2" else None,
                        actor,
                    ),
                )
                continue
            cursor.execute(
                """
                SELECT id, entity_definition_id, data_type, unit
                FROM t_point_conversion_outputs
                WHERE revision_id = %s AND output_key = %s
                """,
                (plan.template_revision_id, item["output_id"]),
            )
            relation = cursor.fetchone()
            if relation is None:
                raise PointConversionError(
                    "POINT_CONVERSION_PLAN_STALE",
                    "Point conversion output relation is no longer available",
                )
            entity_id = UUID(item["output_entity_instance_id"])
            cursor.execute(
                """
                SELECT definition_id, data_type, unit, source_kind
                FROM t_entity_instances WHERE id = %s
                """,
                (entity_id,),
            )
            entity = cursor.fetchone()
            if entity is None or entity != (
                relation[1],
                relation[2],
                relation[3],
                "point_conversion",
            ):
                raise PointConversionError(
                    "POINT_CONVERSION_PLAN_STALE",
                    "Point conversion output entity contract changed after planning",
                )
            cursor.execute(
                """
                INSERT INTO t_conversion_output_bindings
                  (installed_conversion_id, output_id, entity_instance_id)
                VALUES (%s, %s, %s)
                """,
                (installed_id, relation[0], entity_id),
            )
            output_entity_ids.append(entity_id)
        return tuple(sorted(output_entity_ids, key=str))

    @staticmethod
    def _create_derived_solution_lineage(
        cursor: Any,
        plan: PointConversionPlan,
        actor: str,
        current_version: int,
    ) -> tuple[UUID, int]:
        cursor.execute(
            """
            SELECT installation.id, installation.entity_instance_ids,
                   site.package_record_id, site.package_digest,
                   site.parameters, site.secret_references,
                   site.parameter_metadata, site.configuration_digest,
                   site.entity_identity_installation_id,
                   source_plan.parameter_contracts,
                   source_plan.parameter_sources,
                   source_plan.entity_plan,
                   source_plan.alarm_plan
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
        if current is None:
            raise PointConversionError(
                "POINT_CONVERSION_PLAN_STALE",
                "An installed solution is required before an independent replacement",
            )
        next_version = current_version + 1
        configuration_digest = _digest(
            {
                "previous_configuration_digest": current[7].strip(),
                "point_conversion_plan_digest": plan.digest,
            }
        )
        derived_plan_digest = _digest(
            {
                "kind": "point_conversion",
                "point_conversion_plan_id": str(plan.id),
                "point_conversion_plan_digest": plan.digest,
                "base_site_configuration_version": current_version,
                "site_configuration_version": next_version,
            }
        )
        derived_plan_id = uuid5(
            NAMESPACE_URL,
            f"zizu/derived-point-conversion-plan/{derived_plan_digest}",
        )
        derived_installation_id = uuid5(
            NAMESPACE_URL,
            f"zizu/derived-point-conversion-installation/{derived_plan_digest}",
        )
        public_plan = plan.public_dict()
        cursor.execute(
            """
            INSERT INTO t_solution_install_plans
              (id, package_record_id, package_digest,
               base_site_configuration_version, status, items, blockers,
               parameter_contracts, parameters, secret_references,
               parameter_sources, parameter_metadata, configuration_digest,
               target_installation_id, entity_identity_installation_id,
               entity_plan, alarm_plan, point_conversion_plans, digest)
            VALUES (%s, %s, %s, %s, 'ready', %s, '[]', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                derived_plan_id,
                current[2],
                current[3],
                current_version,
                Json(
                    [
                        {
                            "asset_id": str(plan.node_id),
                            "kind": "point_conversion",
                            "action": "update",
                            "point_conversion_plan_id": str(plan.id),
                        }
                    ]
                ),
                Json(current[9]),
                Json(current[4]),
                Json(current[5]),
                Json(current[10]),
                Json(current[6]),
                configuration_digest,
                derived_installation_id,
                current[8],
                Json(current[11]) if current[11] is not None else None,
                Json(current[12]) if current[12] is not None else None,
                Json([public_plan]),
                derived_plan_digest,
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
                current[2],
                current[3],
                next_version,
                list(current[1]),
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
                current[2],
                current[3],
                Json(current[4]),
                Json(current[5]),
                Json(current[6]),
                configuration_digest,
                actor,
                current[8],
            ),
        )
        cursor.execute(
            "UPDATE t_site_configuration_state SET current_version = %s WHERE singleton = TRUE",
            (next_version,),
        )
        details = {
            "plan_id": str(derived_plan_id),
            "point_conversion_plan_id": str(plan.id),
            "point_conversion_plan_digest": plan.digest,
            "configuration_digest": configuration_digest,
        }
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
                current[2],
                current[3],
                next_version,
                Json(details),
            ),
        )
        cursor.execute(
            """
            INSERT INTO t_audit_events
              (id, event, outcome, actor, target, details)
            VALUES (%s, 'solution.install', 'allowed', %s, %s, %s)
            """,
            (
                uuid4(),
                actor,
                f"installation:{derived_installation_id}",
                Json(
                    {
                        **details,
                        "package_record_id": str(current[2]),
                        "package_digest": current[3].strip(),
                        "site_configuration_version": next_version,
                    }
                ),
            ),
        )
        return derived_installation_id, next_version


def build_postgres_point_conversion() -> PointConversion:
    return PointConversion(
        PostgresPointConversionRepository(),
        PostgresPointConversionCatalog(),
    )


def _plan_from_row(row: tuple[Any, ...]) -> PointConversionPlan:
    return PointConversionPlan(
        id=row[0],
        node_id=row[1],
        template_revision_id=row[2],
        entity_identity_installation_id=row[3],
        solution_installation_id=row[4],
        base_site_configuration_version=int(row[5]),
        source_catalog_digest=row[6].strip(),
        status=row[7],
        items=tuple(MappingProxyType(item) for item in row[8]),
        blockers=tuple(MappingProxyType(item) for item in row[9]),
        digest=row[10].strip(),
        planned_by=row[11],
    )


def _application_from_row(row: tuple[Any, ...]) -> PointConversionApplication:
    return PointConversionApplication(
        id=row[0],
        plan_id=row[1],
        installed_conversion_id=row[2],
        solution_installation_id=row[3],
        revision_id=row[4],
        site_configuration_version=int(row[5]),
        output_entity_instance_ids=tuple(row[6]),
        actor=row[7],
    )


def _plain(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
