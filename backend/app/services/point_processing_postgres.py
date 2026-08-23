"""PostgreSQL adapters for immutable L1 point-processing assets and plans."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg2
from psycopg2.extras import Json

from app.services.point_processing import (
    ApplyPointProcessingPlan,
    CurrentPointProcessingContext,
    PointProcessingDelivery,
    PointProcessingApplication,
    PointProcessingCatalog,
    PointProcessingError,
    PointProcessingPlan,
    PointProcessingSource,
    PointProcessingTemplateSummary,
    _template_source_catalog_digest,
)
from app.services.point_processing_dag import (
    PointProcessingDagError,
    validate_processing_dag,
)
from app.services.point_processing_selectors import (
    PointProcessingSelectorError,
    Selector,
    freeze_selector,
)
from app.services.solution_delivery_contracts import DeliveryError, PackageImport
from app.services.solution_point_processings import (
    PointProcessingAsset,
    PointProcessingInput,
    PointProcessingOutput,
    point_processing_assets,
    point_processing_revision_id,
    point_processing_template_id,
)


def _input_id(revision_id: UUID, input_key: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"zizu/point-processing-input/{revision_id}/{input_key}",
    )


def _output_id(revision_id: UUID, output_key: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"zizu/point-processing-output/{revision_id}/{output_key}",
    )


def persist_point_processing_assets(
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
        for asset in point_processing_assets(package):
            template_id = point_processing_template_id(asset)
            cursor.execute(
                """
                INSERT INTO t_point_processing_templates
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
                FROM t_point_processing_templates
                WHERE asset_id = %s AND brand = %s AND model = %s
                FOR UPDATE
                """,
                (asset.asset_id, asset.brand, asset.model),
            )
            template_row = cursor.fetchone()
            if template_row is None:
                raise DeliveryError(
                    "POINT_PROCESSING_CATALOG_UNAVAILABLE",
                    "Point processing template row disappeared",
                )
            template_id = template_row[0]
            if (
                template_row[1] != asset.device_category
                or template_row[2] != asset.display_name
            ):
                raise DeliveryError(
                    "POINT_PROCESSING_TEMPLATE_CONFLICT",
                    "Point processing template identity conflicts with stored content",
                )
            if template_row[3] != asset.status:
                cursor.execute(
                    """
                    UPDATE t_point_processing_templates
                    SET status = %s
                    WHERE id = %s
                    """,
                    (asset.status, template_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_audit_events
                      (id, event, outcome, actor, target, details)
                    VALUES (%s, 'point_processing.template_status', 'allowed',
                            %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        actor,
                        f"point-processing-template:{template_id}",
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
                f"zizu/point-processing-revision/{template_id}/{asset.revision}",
            )
            cursor.execute(
                """
                INSERT INTO t_point_processing_revisions
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
                FROM t_point_processing_revisions
                WHERE template_id = %s AND revision = %s
                """,
                (template_id, asset.revision),
            )
            revision_row = cursor.fetchone()
            if revision_row is None or revision_row[1].strip() != asset.content_digest:
                raise DeliveryError(
                    "POINT_PROCESSING_REVISION_CONFLICT",
                    "Immutable point processing revision has different content",
                )
            revision_id = revision_row[0]
            cursor.execute(
                """
                INSERT INTO t_solution_point_processing_assets
                  (package_record_id, template_revision_id, asset_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (package_record_id, asset_id) DO NOTHING
                """,
                (package.id, revision_id, asset.asset_id),
            )
            cursor.execute(
                """
                SELECT template_revision_id
                FROM t_solution_point_processing_assets
                WHERE package_record_id = %s AND asset_id = %s
                """,
                (package.id, asset.asset_id),
            )
            relation = cursor.fetchone()
            if relation is None or relation[0] != revision_id:
                raise DeliveryError(
                    "POINT_PROCESSING_REVISION_CONFLICT",
                    "Package point processing relation conflicts with stored content",
                )

            input_ids: dict[str, UUID] = {}
            for item in asset.inputs:
                input_id = _input_id(revision_id, item.input_id)
                input_ids[item.input_id] = input_id
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_inputs
                      (id, revision_id, input_key, source_kind, data_type, unit,
                       required, stable_source_key, aliases, expected_group,
                       expected_address, expected_wire_data_type,
                       expected_decimal, expected_read_only)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s)
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
                        item.source_contract.get("group") if item.source_contract else None,
                        item.source_contract.get("address") if item.source_contract else None,
                        item.source_contract.get("wireDataType") if item.source_contract else None,
                        item.source_contract.get("decimal") if item.source_contract else None,
                        item.source_contract.get("readOnly") if item.source_contract else None,
                    ),
                )
                if item.selector is not None:
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_selectors
                          (input_id, scope, node_type, entity_definition_id,
                           cardinality, default_value)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (input_id) DO NOTHING
                        """,
                        (
                            input_id,
                            item.selector["scope"],
                            item.selector["nodeType"],
                            item.selector["entityDefinition"],
                            item.cardinality,
                            Json(item.default_value)
                            if item.default_value is not None
                            else None,
                        ),
                    )

            for output in asset.outputs:
                output_id = _output_id(revision_id, output.output_id)
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_outputs
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
                if transform["kind"] == "numeric":
                    transform_input_id = input_ids[str(transform["input"])]
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
                    transform_input_id = input_ids[str(transform["input"])]
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
                elif transform["kind"] == "fault_codes":
                    transform_input_id = input_ids[str(transform["input"])]
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
                               display_name)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (output_id, raw_code) DO NOTHING
                            """,
                            (
                                output_id,
                                raw_code,
                                entry["code"],
                                entry["name"],
                            ),
                        )
                elif transform["kind"] == "boolean_set":
                    cursor.execute(
                        """
                        INSERT INTO t_boolean_set_transform_rules (output_id)
                        VALUES (%s) ON CONFLICT (output_id) DO NOTHING
                        """,
                        (output_id,),
                    )
                    for entry in transform["entries"]:
                        cursor.execute(
                            """
                            INSERT INTO t_boolean_set_mapping_entries
                              (output_id, input_id, canonical_code,
                               display_name, fault_category)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (output_id, input_id) DO NOTHING
                            """,
                            (
                                output_id,
                                input_ids[entry["input"]],
                                entry["code"],
                                entry["name"],
                                entry["category"],
                            ),
                        )
                else:
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_expressions
                          (output_id, dsl_text, canonical_ast, ast_digest,
                           result_data_type, result_unit, schedule_seconds,
                           control_eligible)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (output_id) DO NOTHING
                        """,
                        (
                            output_id,
                            transform["expression"],
                            Json(_plain(transform["canonicalAst"])),
                            transform["astDigest"],
                            output.data_type,
                            output.unit,
                            transform["scheduleSeconds"],
                            transform["controlEligible"],
                        ),
                    )
    except DeliveryError:
        raise
    except psycopg2.Error as exc:
        raise DeliveryError(
            "POINT_PROCESSING_CATALOG_UNAVAILABLE",
            "Point processing catalog could not be persisted",
        ) from exc


class PostgresPointProcessingCatalog:
    @staticmethod
    @contextmanager
    def _connection():
        from app.services.telemetry_store import get_connection

        with get_connection() as connection:
            yield connection

    def get_template(self, revision_id: UUID) -> PointProcessingAsset | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self._load_asset(cursor, revision_id)

    def list_templates(
        self,
        device_category: str,
    ) -> tuple[PointProcessingTemplateSummary, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT revision.id
                    FROM t_point_processing_revisions AS revision
                    JOIN t_point_processing_templates AS template
                      ON template.id = revision.template_id
                    WHERE upper(template.device_category) = upper(%s)
                      AND template.status = 'active'
                    ORDER BY template.asset_id, revision.revision, revision.id
                    """,
                    (device_category,),
                )
                revision_ids = tuple(row[0] for row in cursor.fetchall())
                return tuple(
                    PointProcessingTemplateSummary(revision_id, asset)
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

    def list_sources(self, node_id: UUID) -> tuple[PointProcessingSource, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self.list_sources_with_cursor(cursor, node_id)

    def list_selector_members(
        self,
        target_node_id: UUID,
        selector: Selector,
    ) -> tuple[UUID, ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self.list_selector_members_with_cursor(
                    cursor,
                    target_node_id,
                    selector,
                )

    @staticmethod
    def list_selector_members_with_cursor(
        cursor: Any,
        target_node_id: UUID,
        selector: Selector,
    ) -> tuple[UUID, ...]:
        cursor.execute(
            """
                    WITH RECURSIVE descendants(id) AS (
                      SELECT id FROM t_nodes WHERE parent_id = %s
                      UNION ALL
                      SELECT child.id
                      FROM t_nodes AS child
                      JOIN descendants AS parent ON child.parent_id = parent.id
                    )
                    SELECT entity.id
                    FROM descendants
                    JOIN t_nodes AS node ON node.id = descendants.id
                    JOIN t_device_instances AS device ON device.node_id = node.id
                    JOIN t_entity_instances AS entity
                      ON entity.device_instance_id = device.id
                    WHERE node.node_type = %s
                      AND entity.definition_id = %s
                      AND entity.source_kind = 'point_processing'
                      AND EXISTS (
                        SELECT 1
                        FROM t_point_processing_output_bindings AS binding
                        JOIN t_installed_point_processings AS installed
                          ON installed.id = binding.installed_processing_id
                         AND installed.current = TRUE
                        WHERE binding.entity_instance_id = entity.id
                      )
                    ORDER BY entity.id
            """,
            (
                target_node_id,
                selector.node_type,
                selector.entity_definition_id,
            ),
        )
        return tuple(row[0] for row in cursor.fetchall())

    def dependency_edges(self) -> tuple[tuple[UUID, UUID], ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT dependency.source_entity_instance_id,
                                    dependency.target_entity_instance_id
                    FROM t_point_processing_dependencies AS dependency
                    JOIN t_installed_point_processings AS installed
                      ON installed.id = dependency.installed_processing_id
                    WHERE installed.current = TRUE
                    ORDER BY 1, 2
                    """
                )
                return tuple(cursor.fetchall())

    def record_dependencies(
        self,
        edges: tuple[tuple[UUID, UUID], ...],
    ) -> None:
        del edges

    @staticmethod
    def list_sources_with_cursor(
        cursor: Any,
        node_id: UUID,
    ) -> tuple[PointProcessingSource, ...]:
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
                    FROM t_point_processing_output_bindings AS output_binding
                    JOIN t_installed_point_processings AS installed
                      ON installed.id = output_binding.installed_processing_id
                     AND installed.current = TRUE
                    WHERE output_binding.entity_instance_id = entity.id
                  )
            ORDER BY 2, 1
            """,
            (node_id,),
        )
        return tuple(PointProcessingSource(*row) for row in cursor.fetchall())

    @staticmethod
    def _load_asset(cursor: Any, revision_id: UUID) -> PointProcessingAsset | None:
        cursor.execute(
            """
            SELECT template.asset_id, template.display_name,
                   template.device_category, template.brand, template.model,
                   revision.revision, template.status, revision.content_digest
            FROM t_point_processing_revisions AS revision
            JOIN t_point_processing_templates AS template
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
            SELECT input.input_key, input.source_kind,
                   input.stable_source_key, input.aliases,
                   input.data_type, input.unit, input.required,
                   input.expected_group, input.expected_address,
                   input.expected_wire_data_type, input.expected_decimal,
                   input.expected_read_only, selector.scope,
                   selector.node_type, selector.entity_definition_id,
                   selector.cardinality, selector.default_value
            FROM t_point_processing_inputs AS input
            LEFT JOIN t_point_processing_selectors AS selector
              ON selector.input_id = input.id
            WHERE input.revision_id = %s
            ORDER BY input.input_key
            """,
            (revision_id,),
        )
        inputs = tuple(
            PointProcessingInput(
                input_id=item[0],
                source_kind=item[1],
                source_key=item[2],
                aliases=tuple(item[3]),
                data_type=item[4],
                unit=item[5],
                required=item[6],
                source_contract=(
                    None
                    if item[7] is None
                    else MappingProxyType({
                        "group": item[7],
                        "address": item[8],
                        "wireDataType": item[9],
                        "decimal": item[10],
                        "readOnly": item[11],
                    })
                ),
                cardinality=item[15] or "one",
                selector=(
                    None
                    if item[12] is None
                    else MappingProxyType(
                        {
                            "scope": item[12],
                            "nodeType": item[13],
                            "entityDefinition": item[14],
                        }
                    )
                ),
                default_value=item[16],
            )
            for item in cursor.fetchall()
        )
        cursor.execute(
            """
            SELECT id, output_key, entity_definition_id, data_type, unit,
                   freshness_seconds
            FROM t_point_processing_outputs
            WHERE revision_id = %s
            ORDER BY entity_definition_id
            """,
            (revision_id,),
        )
        outputs = tuple(
            PointProcessingOutput(
                output_id=output_row[1],
                entity_definition_id=output_row[2],
                data_type=output_row[3],
                unit=output_row[4],
                freshness_seconds=float(output_row[5]),
                transform=MappingProxyType(
                    PostgresPointProcessingCatalog._load_transform(
                        cursor,
                        output_row[0],
                    )
                ),
            )
            for output_row in cursor.fetchall()
        )
        return PointProcessingAsset(
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
            SELECT dsl_text, canonical_ast, ast_digest,
                   schedule_seconds, control_eligible
            FROM t_point_processing_expressions
            WHERE output_id = %s
            """,
            (output_id,),
        )
        expression = cursor.fetchone()
        if expression is not None:
            return {
                "kind": "formula",
                "expression": expression[0],
                "canonicalAst": MappingProxyType(expression[1]),
                "astDigest": expression[2].strip(),
                "scheduleSeconds": expression[3],
                "controlEligible": expression[4],
            }
        cursor.execute(
            """
            SELECT input.input_key, rule.scale, rule."offset",
                   rule.minimum, rule.maximum
            FROM t_numeric_transform_rules AS rule
            JOIN t_point_processing_inputs AS input ON input.id = rule.input_id
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
            JOIN t_point_processing_inputs AS input ON input.id = rule.input_id
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
            SELECT rule.output_id
            FROM t_boolean_set_transform_rules AS rule
            WHERE rule.output_id = %s
            """,
            (output_id,),
        )
        if cursor.fetchone() is not None:
            cursor.execute(
                """
                SELECT input.input_key, entry.canonical_code,
                       entry.display_name, entry.fault_category
                FROM t_boolean_set_mapping_entries AS entry
                JOIN t_point_processing_inputs AS input
                  ON input.id = entry.input_id
                WHERE entry.output_id = %s
                ORDER BY entry.canonical_code
                """,
                (output_id,),
            )
            return {
                "kind": "boolean_set",
                "entries": tuple(
                    MappingProxyType(
                        {
                            "input": item[0],
                            "code": item[1],
                            "name": item[2],
                            "category": item[3],
                        }
                    )
                    for item in cursor.fetchall()
                ),
            }
        cursor.execute(
            """
            SELECT input.input_key, rule.delimiter
            FROM t_fault_code_transform_rules AS rule
            JOIN t_point_processing_inputs AS input ON input.id = rule.input_id
            WHERE rule.output_id = %s
            """,
            (output_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise PointProcessingError(
                "POINT_PROCESSING_CATALOG_INVALID",
                "Point processing output has no transform",
            )
        cursor.execute(
            """
            SELECT raw_code, canonical_code, display_name
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


class PostgresPointProcessingRepository:
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
    ) -> CurrentPointProcessingContext | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT installed.id, installed.solution_installation_id,
                           installed.revision_id,
                           site.entity_identity_installation_id
                    FROM t_installed_point_processings AS installed
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
                    FROM t_point_processing_input_bindings AS binding
                    JOIN t_point_processing_inputs AS input
                      ON input.id = binding.input_id
                    WHERE binding.installed_processing_id = %s
                    ORDER BY input.input_key
                    """,
                    (installed[0],),
                )
                input_ids = dict(cursor.fetchall())
                cursor.execute(
                    """
                    SELECT input.input_key, member.entity_instance_id
                    FROM t_point_processing_selector_members AS member
                    JOIN t_point_processing_inputs AS input
                      ON input.id = member.input_id
                    WHERE member.installed_processing_id = %s
                    ORDER BY input.input_key, member.ordinal
                    """,
                    (installed[0],),
                )
                selector_ids: dict[str, list[UUID]] = {}
                for input_key, entity_id in cursor.fetchall():
                    selector_ids.setdefault(input_key, []).append(entity_id)
                cursor.execute(
                    """
                    SELECT output.output_key, binding.entity_instance_id
                    FROM t_point_processing_output_bindings AS binding
                    JOIN t_point_processing_outputs AS output
                      ON output.id = binding.output_id
                    WHERE binding.installed_processing_id = %s
                    ORDER BY output.output_key
                    """,
                    (installed[0],),
                )
                output_ids = dict(cursor.fetchall())
                return CurrentPointProcessingContext(
                    entity_identity_installation_id=installed[3],
                    solution_installation_id=installed[1],
                    revision_id=installed[2],
                    input_source_ids=input_ids,
                    output_entity_ids=output_ids,
                    selector_source_ids={
                        key: tuple(values)
                        for key, values in selector_ids.items()
                    },
                )

    def save_plan(self, plan: PointProcessingPlan) -> PointProcessingPlan:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_plans
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
                        "SELECT digest FROM t_point_processing_plans WHERE id = %s",
                        (plan.id,),
                    )
                    stored = cursor.fetchone()
                    if stored is None or stored[0].strip() != plan.digest:
                        raise PointProcessingError(
                            "POINT_PROCESSING_PLAN_CONFLICT",
                            "Point processing plan identity conflicts with stored evidence",
                        )
                    self._persist_plan_items(cursor, plan)
                    return plan
        except PointProcessingError:
            raise
        except psycopg2.Error as exc:
            raise PointProcessingError(
                "DATA_TRUNK_UNAVAILABLE",
                "Point processing plan could not be persisted",
            ) from exc

    @staticmethod
    def _persist_plan_items(cursor: Any, plan: PointProcessingPlan) -> None:
        for item in plan.items:
            input_id = None
            output_id = None
            layer = "L1"
            source_kind = None
            selected_tag_id = None
            selected_entity_id = None
            if item["kind"] == "l0_point":
                layer = "L0"
            elif item["kind"] == "input_binding":
                cursor.execute(
                    """
                    SELECT id, source_kind
                    FROM t_point_processing_inputs
                    WHERE revision_id = %s AND input_key = %s
                    """,
                    (plan.template_revision_id, item["input_id"]),
                )
                relation = cursor.fetchone()
                if relation is None:
                    raise PointProcessingError(
                        "POINT_PROCESSING_PLAN_STALE",
                        "Point processing input relation is missing",
                    )
                input_id, source_kind = relation
                selected = item.get("selected_source_id")
                if selected is not None:
                    if source_kind == "l0":
                        cursor.execute(
                            "SELECT 1 FROM t_tags WHERE id = %s",
                            (UUID(selected),),
                        )
                        if cursor.fetchone() is not None:
                            selected_tag_id = UUID(selected)
                        else:
                            source_kind = None
                    else:
                        selected_entity_id = UUID(selected)
            elif item["kind"] == "selector_binding":
                cursor.execute(
                    """
                    SELECT id FROM t_point_processing_inputs
                    WHERE revision_id = %s AND input_key = %s
                    """,
                    (plan.template_revision_id, item["input_id"]),
                )
                relation = cursor.fetchone()
                if relation is None:
                    raise PointProcessingError(
                        "POINT_PROCESSING_PLAN_STALE",
                        "Point processing selector input relation is missing",
                    )
                input_id = relation[0]
            elif item["kind"] == "dag_validation":
                layer = "L1"
            elif item["kind"] == "output_binding":
                layer = "L2"
                cursor.execute(
                    """
                    SELECT id FROM t_point_processing_outputs
                    WHERE revision_id = %s AND output_key = %s
                    """,
                    (plan.template_revision_id, item["output_id"]),
                )
                relation = cursor.fetchone()
                if relation is None:
                    raise PointProcessingError(
                        "POINT_PROCESSING_PLAN_STALE",
                        "Point processing output relation is missing",
                    )
                output_id = relation[0]
            cursor.execute(
                """
                INSERT INTO t_point_processing_plan_items
                  (plan_id, item_key, layer, action, input_id, output_id, source_kind,
                   selected_tag_id, selected_entity_instance_id,
                   output_entity_instance_id, blocker_code, before_value,
                   after_value)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
                ON CONFLICT (plan_id, item_key) DO NOTHING
                """,
                (
                    plan.id,
                    item["item_key"],
                    layer,
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

    def get_plan(self, plan_id: UUID) -> PointProcessingPlan | None:
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
                    FROM t_point_processing_plans WHERE id = %s
                    """,
                    (plan_id,),
                )
                row = cursor.fetchone()
                return _plan_from_row(row) if row is not None else None

    def apply_plan(
        self,
        command: ApplyPointProcessingPlan,
        catalog: PointProcessingCatalog,
        *,
        transaction: Any | None = None,
        verified_source_catalog_digest: str | None = None,
    ) -> PointProcessingApplication:
        del catalog
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
        external_transaction = transaction is not None
        try:
            with self._connection(transaction) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"point-processing:{command.actor}:{command.idempotency_key}",),
                    )
                    cursor.execute(
                        """
                        SELECT application.id, application.plan_id,
                               application.installed_processing_id,
                               application.solution_installation_id,
                               installed.revision_id,
                               application.site_configuration_version,
                               application.output_entity_instance_ids,
                               application.actor,
                               idempotency.request_digest
                        FROM t_point_processing_idempotency AS idempotency
                        JOIN t_point_processing_applications AS application
                          ON application.id = idempotency.application_id
                        JOIN t_installed_point_processings AS installed
                          ON installed.id = application.installed_processing_id
                        WHERE idempotency.actor = %s
                          AND idempotency.idempotency_key = %s
                        """,
                        (command.actor, command.idempotency_key),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        if existing[8].strip() != request_digest:
                            raise PointProcessingError(
                                "POINT_PROCESSING_IDEMPOTENCY_KEY_REUSED",
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
                        FROM t_point_processing_plans
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (command.plan_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise PointProcessingError(
                            "POINT_PROCESSING_PLAN_NOT_FOUND",
                            "Point processing plan was not found",
                        )
                    plan = _plan_from_row(row)
                    if plan.digest != command.plan_digest:
                        raise PointProcessingError(
                            "POINT_PROCESSING_PLAN_DIGEST_MISMATCH",
                            "Point processing plan digest does not match",
                        )
                    if plan.status != "ready" or plan.blockers:
                        raise PointProcessingError(
                            "POINT_PROCESSING_PLAN_STALE",
                            "Point processing plan is no longer ready",
                        )
                    if current_version != plan.base_site_configuration_version:
                        raise PointProcessingError(
                            "POINT_PROCESSING_PLAN_STALE",
                            "Site configuration changed after planning",
                        )

                    cursor.execute(
                        """
                        LOCK TABLE t_tags, t_entity_instances,
                                   t_entity_instance_bindings,
                                   t_entity_binding_confirmations,
                                   t_point_processing_output_bindings,
                                   t_point_processing_selector_members,
                                   t_point_processing_dependencies,
                                   t_installed_point_processings
                        IN SHARE ROW EXCLUSIVE MODE
                        """
                    )
                    template = PostgresPointProcessingCatalog._load_asset(
                        cursor,
                        plan.template_revision_id,
                    )
                    if template is None or template.status != "active":
                        raise PointProcessingError(
                            "POINT_PROCESSING_PLAN_STALE",
                            "Point processing template changed after planning",
                        )
                    actual_source_digest = verified_source_catalog_digest
                    if actual_source_digest is None:
                        sources = PostgresPointProcessingCatalog.list_sources_with_cursor(
                            cursor,
                            plan.node_id,
                        )
                        actual_source_digest = _template_source_catalog_digest(
                            template,
                            sources,
                            plan.node_id,
                        )
                    selector_digests: dict[str, str] = {}
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
                        try:
                            frozen = freeze_selector(
                                selector=selector,
                                target_node_id=plan.node_id,
                                site_configuration_version=current_version,
                                entity_instance_ids=(
                                    PostgresPointProcessingCatalog
                                    .list_selector_members_with_cursor(
                                        cursor,
                                        plan.node_id,
                                        selector,
                                    )
                                ),
                            )
                        except PointProcessingSelectorError as exc:
                            raise PointProcessingError(
                                "POINT_PROCESSING_SELECTOR_STALE",
                                "Point processing selector members changed after planning",
                            ) from exc
                        if frozen.digest != item.get("selector_digest"):
                            raise PointProcessingError(
                                "POINT_PROCESSING_SELECTOR_STALE",
                                "Point processing selector members changed after planning",
                            )
                        selector_digests[item["input_id"]] = frozen.digest
                    if selector_digests:
                        actual_source_digest = _digest(
                            {
                                "catalog_digest": actual_source_digest,
                                "selector_digests": {
                                    key: selector_digests[key]
                                    for key in sorted(selector_digests)
                                },
                            }
                        )
                    if plan.source_catalog_digest != actual_source_digest:
                        raise PointProcessingError(
                            "POINT_PROCESSING_PLAN_STALE",
                            "Point processing source catalog changed after planning",
                        )
                    cursor.execute(
                        """
                        SELECT DISTINCT dependency.source_entity_instance_id,
                                        dependency.target_entity_instance_id
                        FROM t_point_processing_dependencies AS dependency
                        JOIN t_installed_point_processings AS installed
                          ON installed.id = dependency.installed_processing_id
                        WHERE installed.current = TRUE
                        ORDER BY 1, 2
                        """
                    )
                    existing_edges = tuple(cursor.fetchall())
                    planned_edges = tuple(
                        (UUID(source), UUID(target))
                        for item in plan.items
                        if item.get("kind") == "dag_validation"
                        for source, target in item.get("planned_edges", ())
                    )
                    try:
                        validate_processing_dag(
                            existing_edges=existing_edges,
                            planned_edges=planned_edges,
                            max_depth=8,
                        )
                    except PointProcessingDagError as exc:
                        raise PointProcessingError(
                            "POINT_PROCESSING_DAG_STALE",
                            "Point processing dependency graph changed after planning",
                        ) from exc
                    self._verify_package_ownership(cursor, plan, current_version)
                    self._apply_l0_plan_items(cursor, plan)

                    cursor.execute(
                        """
                        SELECT id
                        FROM t_installed_point_processings
                        WHERE node_id = %s AND current = TRUE
                        FOR UPDATE
                        """,
                        (plan.node_id,),
                    )
                    current_installed = cursor.fetchone()
                    if current_installed is not None:
                        cursor.execute(
                            """
                            UPDATE t_installed_point_processings
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
                        f"zizu/installed-point-processing/{plan.id}",
                    )
                    application_id = uuid5(
                        NAMESPACE_URL,
                        (
                            "zizu/point-processing-application/"
                            f"{command.actor}/{command.idempotency_key}"
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_installed_point_processings
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
                        UPDATE t_solution_installations
                        SET entity_instance_ids = ARRAY(
                          SELECT DISTINCT value
                          FROM unnest(entity_instance_ids || %s::uuid[]) AS value
                          ORDER BY value
                        )
                        WHERE id = %s
                        """,
                        ([str(item) for item in output_ids], solution_installation_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_applications
                          (id, plan_id, installed_processing_id,
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
                        INSERT INTO t_point_processing_idempotency
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
                        "UPDATE t_point_processing_plans SET status = 'applied' WHERE id = %s",
                        (plan.id,),
                    )
                    cursor.execute(
                        """
                        INSERT INTO t_audit_events
                          (id, event, outcome, reason, actor, target, details)
                        VALUES (%s, 'configuration.change', 'applied',
                                'reviewed point processing plan', %s, %s, %s)
                        """,
                        (
                            uuid4(),
                            command.actor,
                            f"POST /api/v1/point-processing-plans/{plan.id}/apply",
                            Json(
                                {
                                    "kind": "point_processing",
                                    "plan_id": str(plan.id),
                                    "plan_digest": plan.digest,
                                    "node_id": str(plan.node_id),
                                    "site_configuration_version": next_version,
                                }
                            ),
                        ),
                    )
                    return PointProcessingApplication(
                        id=application_id,
                        plan_id=plan.id,
                        installed_processing_id=installed_id,
                        solution_installation_id=solution_installation_id,
                        revision_id=plan.template_revision_id,
                        site_configuration_version=next_version,
                        output_entity_instance_ids=output_ids,
                        actor=command.actor,
                    )
        except PointProcessingError:
            raise
        except psycopg2.Error as exc:
            raise PointProcessingError(
                "DATA_TRUNK_UNAVAILABLE",
                "Point processing application could not be committed",
            ) from exc

    @staticmethod
    def _apply_l0_plan_items(cursor: Any, plan: PointProcessingPlan) -> None:
        cursor.execute(
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.columns
              WHERE table_schema = current_schema()
                AND table_name = 't_tags'
                AND column_name = 'tag_type'
            )
            """
        )
        has_tag_type = bool(cursor.fetchone()[0])
        for item in plan.items:
            if item.get("kind") != "l0_point":
                continue
            after = item.get("after")
            if (
                item.get("action") == "block"
                or not isinstance(after, Mapping)
                or after.get("read_only") is not True
            ):
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_BLOCKED",
                    "L0 point plan contains an unsafe item",
                )
            columns = "id, node_id, name, data_type, unit"
            values = "%s, %s, %s, %s, %s"
            if has_tag_type:
                columns += ", tag_type"
                values += ", 'PHYSICAL'"
            cursor.execute(
                f"""
                INSERT INTO t_tags
                  ({columns}, source_type, source_path, unit_from, unit_to,
                   read_write, enabled,
                   wire_data_type, value_data_type, source_address,
                   decimal, read_only, freshness_seconds)
                VALUES (
                  {values},
                  'neuron',
                  (SELECT COALESCE(source_catalog_key, name)
                   FROM t_nodes WHERE id = %s) || '/' || %s || '/' || %s,
                  %s, %s,
                  'R', TRUE, %s, %s, %s, %s, TRUE
                  , %s
                )
                ON CONFLICT (id) DO UPDATE SET
                  name = EXCLUDED.name,
                  data_type = EXCLUDED.data_type,
                  unit = EXCLUDED.unit,
                  source_type = 'neuron',
                  source_path = EXCLUDED.source_path,
                  unit_from = EXCLUDED.unit_from,
                  unit_to = EXCLUDED.unit_to,
                  read_write = 'R',
                  enabled = TRUE,
                  wire_data_type = EXCLUDED.wire_data_type,
                  value_data_type = EXCLUDED.value_data_type,
                  source_address = EXCLUDED.source_address,
                  decimal = EXCLUDED.decimal,
                  read_only = TRUE,
                  freshness_seconds = EXCLUDED.freshness_seconds
                WHERE t_tags.node_id = EXCLUDED.node_id
                """,
                (
                    UUID(str(after["source_id"])),
                    plan.node_id,
                    after["name"],
                    after["value_data_type"],
                    after.get("unit"),
                    plan.node_id,
                    after["group"],
                    after["name"],
                    after.get("unit"),
                    after.get("unit"),
                    after["wire_data_type"],
                    after["value_data_type"],
                    after["source_address"],
                    after.get("decimal"),
                    after["freshness_seconds"],
                ),
            )

    @staticmethod
    def _verify_package_ownership(
        cursor: Any,
        plan: PointProcessingPlan,
        current_version: int,
    ) -> None:
        cursor.execute(
            """
            SELECT 1
            FROM t_solution_point_processing_assets AS asset
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
            raise PointProcessingError(
                "POINT_PROCESSING_PLAN_STALE",
                "Point processing revision does not belong to the installed solution",
            )

    @staticmethod
    def _install_bindings(
        cursor: Any,
        plan: PointProcessingPlan,
        installed_id: UUID,
        actor: str,
    ) -> tuple[UUID, ...]:
        output_entity_ids: list[UUID] = []
        for item in plan.items:
            if item["action"] == "block":
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_BLOCKED",
                    "Point processing plan contains a blocked item",
                )
            if item["kind"] == "input_binding":
                cursor.execute(
                    """
                    SELECT id, source_kind, required
                    FROM t_point_processing_inputs
                    WHERE revision_id = %s AND input_key = %s
                    """,
                    (plan.template_revision_id, item["input_id"]),
                )
                relation = cursor.fetchone()
                if relation is None:
                    raise PointProcessingError(
                        "POINT_PROCESSING_PLAN_STALE",
                        "Point processing input relation is no longer available",
                    )
                if item.get("selected_source_id") is None:
                    if relation[2] is False:
                        continue
                    raise PointProcessingError(
                        "POINT_PROCESSING_PLAN_STALE",
                        "Required point processing input has no selected source",
                    )
                selected_id = UUID(item["selected_source_id"])
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_input_bindings
                      (installed_processing_id, input_id, source_kind,
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
            if item["kind"] == "selector_binding":
                cursor.execute(
                    """
                    SELECT id
                    FROM t_point_processing_inputs
                    WHERE revision_id = %s AND input_key = %s
                    """,
                    (plan.template_revision_id, item["input_id"]),
                )
                relation = cursor.fetchone()
                if relation is None:
                    raise PointProcessingError(
                        "POINT_PROCESSING_PLAN_STALE",
                        "Point processing selector relation is no longer available",
                    )
                for ordinal, selected in enumerate(item["selected_source_ids"]):
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_selector_members
                          (installed_processing_id, input_id, ordinal,
                           entity_instance_id, selector_digest)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            installed_id,
                            relation[0],
                            ordinal,
                            UUID(selected),
                            item["selector_digest"],
                        ),
                    )
                continue
            if item["kind"] == "l0_point":
                continue
            if item["kind"] == "dag_validation":
                for dependency in item.get("planned_dependencies", ()):
                    cursor.execute(
                        """
                        SELECT input.id, output.id
                        FROM t_point_processing_inputs AS input
                        CROSS JOIN t_point_processing_outputs AS output
                        WHERE input.revision_id = %s
                          AND input.input_key = %s
                          AND output.revision_id = %s
                          AND output.output_key = %s
                        """,
                        (
                            plan.template_revision_id,
                            dependency["input_id"],
                            plan.template_revision_id,
                            dependency["output_id"],
                        ),
                    )
                    relation = cursor.fetchone()
                    if relation is None:
                        raise PointProcessingError(
                            "POINT_PROCESSING_PLAN_STALE",
                            "Point processing dependency relation is unavailable",
                        )
                    cursor.execute(
                        """
                        INSERT INTO t_point_processing_dependencies
                          (installed_processing_id, input_id, output_id,
                           source_entity_instance_id, target_entity_instance_id)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            installed_id,
                            relation[0],
                            relation[1],
                            UUID(dependency["source_entity_instance_id"]),
                            UUID(dependency["target_entity_instance_id"]),
                        ),
                    )
                continue
            cursor.execute(
                """
                SELECT id, entity_definition_id, data_type, unit,
                       freshness_seconds
                FROM t_point_processing_outputs
                WHERE revision_id = %s AND output_key = %s
                """,
                (plan.template_revision_id, item["output_id"]),
            )
            relation = cursor.fetchone()
            if relation is None:
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_STALE",
                    "Point processing output relation is no longer available",
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
            if entity is None:
                PostgresPointProcessingRepository._create_output_entity(
                    cursor,
                    plan,
                    entity_id,
                    definition_id=relation[1],
                    data_type=relation[2],
                    unit=relation[3],
                    freshness_seconds=float(relation[4]),
                )
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
                "point_processing",
            ):
                raise PointProcessingError(
                    "POINT_PROCESSING_PLAN_STALE",
                    "Point processing output entity contract changed after planning",
                )
            cursor.execute(
                """
                INSERT INTO t_point_processing_output_bindings
                  (installed_processing_id, output_id, entity_instance_id)
                VALUES (%s, %s, %s)
                """,
                (installed_id, relation[0], entity_id),
            )
            output_entity_ids.append(entity_id)
        return tuple(sorted(output_entity_ids, key=str))

    @staticmethod
    def _create_output_entity(
        cursor: Any,
        plan: PointProcessingPlan,
        entity_id: UUID,
        *,
        definition_id: str,
        data_type: str,
        unit: str | None,
        freshness_seconds: float,
    ) -> None:
        cursor.execute(
            """
            SELECT template.device_category, template.display_name,
                   node.name
            FROM t_point_processing_revisions AS revision
            JOIN t_point_processing_templates AS template
              ON template.id = revision.template_id
            JOIN t_nodes AS node ON node.id = %s
            WHERE revision.id = %s
            """,
            (plan.node_id, plan.template_revision_id),
        )
        contract = cursor.fetchone()
        if contract is None:
            raise PointProcessingError(
                "POINT_PROCESSING_PLAN_STALE",
                "Point processing output target is unavailable",
            )
        device_category, template_name, node_name = contract
        device_id = uuid5(
            NAMESPACE_URL,
            (
                "zizu/point-processing-device/"
                f"{plan.entity_identity_installation_id}/{plan.node_id}/"
                f"{str(device_category).casefold()}"
            ),
        )
        slot_id = f"dynamic.point-processing.{str(device_category).casefold()}"
        instance_key = str(plan.node_id)
        cursor.execute(
            """
            INSERT INTO t_device_instances
              (id, identity_installation_id, slot_id, instance_key,
               device_category, display_name, node_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                device_id,
                plan.entity_identity_installation_id,
                slot_id,
                instance_key,
                str(device_category),
                f"{node_name} 点位加工",
                plan.node_id,
            ),
        )
        cursor.execute(
            """
            SELECT identity_installation_id, slot_id, instance_key,
                   device_category, node_id
            FROM t_device_instances
            WHERE id = %s
            """,
            (device_id,),
        )
        device = cursor.fetchone()
        if device != (
            plan.entity_identity_installation_id,
            slot_id,
            instance_key,
            str(device_category),
            plan.node_id,
        ):
            raise PointProcessingError(
                "POINT_PROCESSING_PLAN_STALE",
                "Point processing output device contract changed after planning",
            )
        cursor.execute(
            """
            INSERT INTO t_entity_instances
              (id, device_instance_id, definition_id, display_name,
               data_type, unit, direction, freshness_seconds, source_kind)
            VALUES (%s, %s, %s, %s, %s, %s, 'R', %s, 'point_processing')
            ON CONFLICT (id) DO NOTHING
            """,
            (
                entity_id,
                device_id,
                definition_id,
                f"{template_name} · {definition_id}",
                data_type,
                unit,
                freshness_seconds,
            ),
        )

    @staticmethod
    def _create_derived_solution_lineage(
        cursor: Any,
        plan: PointProcessingPlan,
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
            raise PointProcessingError(
                "POINT_PROCESSING_PLAN_STALE",
                "An installed solution is required before an independent replacement",
            )
        next_version = current_version + 1
        configuration_digest = _digest(
            {
                "previous_configuration_digest": current[7].strip(),
                "point_processing_plan_digest": plan.digest,
            }
        )
        derived_plan_digest = _digest(
            {
                "kind": "point_processing",
                "point_processing_plan_id": str(plan.id),
                "point_processing_plan_digest": plan.digest,
                "base_site_configuration_version": current_version,
                "site_configuration_version": next_version,
            }
        )
        derived_plan_id = uuid5(
            NAMESPACE_URL,
            f"zizu/derived-point-processing-plan/{derived_plan_digest}",
        )
        derived_installation_id = uuid5(
            NAMESPACE_URL,
            f"zizu/derived-point-processing-installation/{derived_plan_digest}",
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
               entity_plan, alarm_plan, point_processing_plans, digest)
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
                            "kind": "point_processing",
                            "action": "update",
                            "point_processing_plan_id": str(plan.id),
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
            "point_processing_plan_id": str(plan.id),
            "point_processing_plan_digest": plan.digest,
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


def build_postgres_point_processing() -> PointProcessingDelivery:
    from app.services.neuron_client import get_neuron_client
    from app.services.neuron_point_processing_catalog import NeuronPointCatalog

    return PointProcessingDelivery(
        PostgresPointProcessingRepository(),
        PostgresPointProcessingCatalog(),
        point_scanner=NeuronPointCatalog(get_neuron_client()),
    )


def _plan_from_row(row: tuple[Any, ...]) -> PointProcessingPlan:
    return PointProcessingPlan(
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


def _application_from_row(row: tuple[Any, ...]) -> PointProcessingApplication:
    return PointProcessingApplication(
        id=row[0],
        plan_id=row[1],
        installed_processing_id=row[2],
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
