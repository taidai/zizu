"""One-shot hard cut from legacy BIT-as-BOOL L0 values to raw INT values."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg2.extras import Json

from app.services.configuration_revision import ConfigurationRevisionError
from app.services.configuration_revision_postgres import PostgresConfigurationRevisions
from app.services.point_processing_postgres import (
    PostgresPointProcessingCatalog,
    persist_point_processing_template,
)
from app.services.point_processing_templates import (
    PointProcessingTemplateError,
    canonical_point_processing_content,
    parse_point_processing_template,
)


@dataclass(frozen=True)
class CutoverBlocker:
    node_id: UUID
    processing_revision_id: UUID
    output_id: UUID
    code: str


@dataclass(frozen=True)
class CutoverReport:
    deterministic_output_ids: tuple[UUID, ...]
    blockers: tuple[CutoverBlocker, ...]
    digest: str


class CutoverError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _InstalledBitInput:
    installed_id: UUID
    node_id: UUID
    revision_id: UUID
    input_id: UUID
    input_key: str


@dataclass(frozen=True)
class _CutoverPlan:
    report: CutoverReport
    inputs_by_installation: dict[UUID, tuple[_InstalledBitInput, ...]]
    output_ids_by_revision: dict[UUID, dict[str, UUID]]


def _stable_digest(
    deterministic_output_ids: tuple[UUID, ...],
    blockers: tuple[CutoverBlocker, ...],
) -> str:
    payload = {
        "deterministic_output_ids": [
            str(item) for item in sorted(deterministic_output_ids, key=str)
        ],
        "blockers": [
            {
                "node_id": str(item.node_id),
                "processing_revision_id": str(item.processing_revision_id),
                "output_id": str(item.output_id),
                "code": item.code,
            }
            for item in sorted(
                blockers,
                key=lambda item: (
                    str(item.node_id),
                    str(item.processing_revision_id),
                    str(item.output_id),
                    item.code,
                ),
            )
        ],
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _references_input(transform: Any, input_key: str) -> bool:
    kind = str(transform.get("kind", ""))
    if kind == "formula":
        expression = str(transform.get("expression", ""))
        return re.search(rf"(?<![\w.]){re.escape(input_key)}(?![\w.])", expression) is not None
    if kind == "boolean_set":
        return any(str(entry.get("input")) == input_key for entry in transform["entries"])
    return str(transform.get("input", "")) == input_key


def _inspect_plan(connection: Any) -> _CutoverPlan:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT installed.id,installed.node_id,installed.revision_id,
                   input.id,input.input_key
            FROM t_installed_point_processings AS installed
            JOIN t_point_processing_input_bindings AS binding
              ON binding.installed_processing_id=installed.id
             AND binding.source_kind='l0'
            JOIN t_point_processing_inputs AS input ON input.id=binding.input_id
            JOIN t_tags AS tag ON tag.id=binding.l0_tag_id
            WHERE installed.current=TRUE
              AND upper(COALESCE(tag.wire_data_type,''))='BIT'
              AND input.data_type='BOOL'
            ORDER BY installed.node_id,installed.revision_id,input.input_key
            """
        )
        bit_inputs = tuple(_InstalledBitInput(*row) for row in cursor.fetchall())
        revision_ids = tuple(sorted({item.revision_id for item in bit_inputs}, key=str))
        output_ids_by_revision: dict[UUID, dict[str, UUID]] = {}
        if revision_ids:
            cursor.execute(
                """
                SELECT revision_id,output_key,id
                FROM t_point_processing_outputs
                WHERE revision_id=ANY(%s::uuid[])
                ORDER BY revision_id,output_key
                """,
                (list(revision_ids),),
            )
            for revision_id, output_key, output_id in cursor.fetchall():
                output_ids_by_revision.setdefault(revision_id, {})[output_key] = output_id

        by_installation: dict[UUID, list[_InstalledBitInput]] = {}
        for item in bit_inputs:
            by_installation.setdefault(item.installed_id, []).append(item)

        deterministic: set[UUID] = set()
        blockers: set[CutoverBlocker] = set()
        for inputs in by_installation.values():
            revision_id = inputs[0].revision_id
            template = PostgresPointProcessingCatalog._load_template(
                cursor,
                revision_id,
                include_internal=True,
            )
            if template is None:
                raise CutoverError("CUTOVER_PROCESSING_REVISION_MISSING")
            output_ids = output_ids_by_revision.get(revision_id, {})
            referenced_inputs: set[str] = set()
            for output in template.outputs:
                output_id = output_ids.get(output.output_id)
                if output_id is None:
                    raise CutoverError("CUTOVER_PROCESSING_OUTPUT_MISSING")
                for item in inputs:
                    if not _references_input(output.transform, item.input_key):
                        continue
                    referenced_inputs.add(item.input_key)
                    exact_identity = (
                        output.transform.get("kind") == "formula"
                        and str(output.transform.get("expression", "")).strip()
                        == item.input_key
                        and output.data_type == "BOOL"
                        and output.unit is None
                    )
                    if exact_identity:
                        deterministic.add(output_id)
                    else:
                        blockers.add(
                            CutoverBlocker(
                                item.node_id,
                                revision_id,
                                output_id,
                                "BIT_FORMULA_REQUIRES_REVIEW",
                            )
                        )
            for item in inputs:
                if item.input_key not in referenced_inputs:
                    fallback_output = next(iter(output_ids.values()), revision_id)
                    blockers.add(
                        CutoverBlocker(
                            item.node_id,
                            revision_id,
                            fallback_output,
                            "BIT_INPUT_REQUIRES_REVIEW",
                        )
                    )

    deterministic_tuple = tuple(sorted(deterministic, key=str))
    blockers_tuple = tuple(
        sorted(
            blockers,
            key=lambda item: (
                str(item.node_id),
                str(item.processing_revision_id),
                str(item.output_id),
                item.code,
            ),
        )
    )
    return _CutoverPlan(
        CutoverReport(
            deterministic_tuple,
            blockers_tuple,
            _stable_digest(deterministic_tuple, blockers_tuple),
        ),
        {key: tuple(value) for key, value in by_installation.items()},
        output_ids_by_revision,
    )


def inspect_cutover(connection: Any) -> CutoverReport:
    """Return a stable, read-only report for the active configuration."""
    return _inspect_plan(connection).report


def _acquire_runtime_locks(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock("
            "hashtextextended('zizu:data-frame-writer',0))"
        )
        if not bool(cursor.fetchone()[0]):
            raise CutoverError("CUTOVER_WRITER_ACTIVE")
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock("
            "hashtextextended('zizu:data-frame-outbox',0))"
        )
        if not bool(cursor.fetchone()[0]):
            raise CutoverError("CUTOVER_OUTBOX_ACTIVE")


def _next_revision(cursor: Any, revision_id: UUID) -> int:
    cursor.execute(
        """
        SELECT COALESCE(max(candidate.revision),0)+1
        FROM t_point_processing_revisions AS source
        JOIN t_point_processing_revisions AS candidate
          ON candidate.template_id=source.template_id
        WHERE source.id=%s
        """,
        (revision_id,),
    )
    return int(cursor.fetchone()[0])


def apply_cutover(
    connection: Any,
    *,
    expected_digest: str,
    actor: str,
) -> tuple[UUID, ...]:
    """Publish immutable boolean-map revisions and replacement installations."""
    if not expected_digest or not actor.strip():
        raise CutoverError("CUTOVER_ARGUMENT_INVALID")
    try:
        _acquire_runtime_locks(connection)
        plan = _inspect_plan(connection)
        if plan.report.blockers:
            raise CutoverError("CUTOVER_BLOCKED")
        if plan.report.digest != expected_digest:
            raise CutoverError("CUTOVER_DIGEST_MISMATCH")
        if not plan.inputs_by_installation:
            connection.commit()
            return ()

        current_configuration = PostgresConfigurationRevisions().current(connection)
        new_by_old_revision: dict[UUID, tuple[UUID, dict[str, UUID], dict[str, UUID]]] = {}
        details: list[dict[str, Any]] = []
        with connection.cursor() as cursor:
            deterministic_ids = set(plan.report.deterministic_output_ids)
            for inputs in plan.inputs_by_installation.values():
                old_revision = inputs[0].revision_id
                if old_revision in new_by_old_revision:
                    continue
                template = PostgresPointProcessingCatalog._load_template(
                    cursor,
                    old_revision,
                    include_internal=True,
                )
                if template is None:
                    raise CutoverError("CUTOVER_PROCESSING_REVISION_MISSING")
                raw = canonical_point_processing_content(template)
                raw["revision"] = _next_revision(cursor, old_revision)
                affected_input_keys = {
                    item.input_key
                    for install_inputs in plan.inputs_by_installation.values()
                    if install_inputs[0].revision_id == old_revision
                    for item in install_inputs
                }
                for raw_input in raw["inputs"]:
                    if raw_input["id"] in affected_input_keys:
                        raw_input["dataType"] = "INT"
                old_output_ids = plan.output_ids_by_revision.get(old_revision, {})
                for raw_output in raw["outputs"]:
                    old_output_id = old_output_ids.get(raw_output["id"])
                    if old_output_id in deterministic_ids:
                        raw_output["transform"] = {
                            "kind": "boolean_map",
                            "input": raw_output["transform"]["expression"].strip(),
                            "trueWhen": 1,
                        }
                parsed = parse_point_processing_template(raw)
                cursor.execute(
                    """
                    SELECT template.reuse_scope,template.owner_node_id
                    FROM t_point_processing_revisions AS revision
                    JOIN t_point_processing_templates AS template
                      ON template.id=revision.template_id
                    WHERE revision.id=%s
                    """,
                    (old_revision,),
                )
                reuse_scope, owner_node_id = cursor.fetchone()
                registered = persist_point_processing_template(
                    cursor,
                    parsed,
                    actor,
                    reuse_scope=reuse_scope,
                    owner_node_id=owner_node_id,
                )
                cursor.execute(
                    "SELECT input_key,id FROM t_point_processing_inputs WHERE revision_id=%s",
                    (registered.revision_id,),
                )
                new_inputs = dict(cursor.fetchall())
                cursor.execute(
                    "SELECT output_key,id FROM t_point_processing_outputs WHERE revision_id=%s",
                    (registered.revision_id,),
                )
                new_outputs = dict(cursor.fetchall())
                new_by_old_revision[old_revision] = (
                    registered.revision_id,
                    new_inputs,
                    new_outputs,
                )
                details.append(
                    {
                        "old_revision_id": str(old_revision),
                        "new_revision_id": str(registered.revision_id),
                    }
                )

            after_digest = hashlib.sha256(
                json.dumps(details, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
            try:
                new_configuration = PostgresConfigurationRevisions().publish(
                    transaction=connection,
                    base_revision=current_configuration,
                    actor=actor,
                    action="l0_raw_bit_hard_cut",
                    resource_kind="platform",
                    resource_id="l0-raw-bit",
                    before_digest=expected_digest,
                    after_digest=after_digest,
                    details={"processing_revisions": details},
                )
            except ConfigurationRevisionError as exc:
                raise CutoverError("CUTOVER_CONFIGURATION_REVISION_MISMATCH") from exc

            for installed_id, inputs in plan.inputs_by_installation.items():
                old_revision = inputs[0].revision_id
                new_revision, _new_inputs, _new_outputs = new_by_old_revision[old_revision]
                migration_plan_id = uuid5(
                    NAMESPACE_URL,
                    "zizu/l0-raw-bit-cutover-plan/"
                    f"{expected_digest}/{installed_id}/{new_revision}",
                )
                new_installed_id = uuid5(
                    NAMESPACE_URL,
                    f"zizu/installed-point-processing/{migration_plan_id}",
                )
                application_id = uuid5(
                    NAMESPACE_URL,
                    f"zizu/l0-raw-bit-cutover-application/{migration_plan_id}",
                )
                plan_items = [
                    {
                        "kind": "l0_raw_bit_hard_cut",
                        "old_installation_id": str(installed_id),
                        "old_revision_id": str(old_revision),
                        "new_revision_id": str(new_revision),
                    }
                ]
                plan_digest = hashlib.sha256(
                    json.dumps(
                        plan_items,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                cursor.execute(
                    """
                    SELECT node_id
                    FROM t_installed_point_processings
                    WHERE id=%s AND current=TRUE AND revision_id=%s
                    FOR UPDATE
                    """,
                    (installed_id, old_revision),
                )
                installed_row = cursor.fetchone()
                if installed_row is None:
                    raise CutoverError("CUTOVER_CONFIGURATION_REVISION_MISMATCH")
                node_id = installed_row[0]
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_plans
                      (id,node_id,template_revision_id,base_configuration_revision,
                       source_catalog_digest,status,items,blockers,digest,planned_by)
                    VALUES(%s,%s,%s,%s,%s,'applied',%s,%s,%s,%s)
                    """,
                    (
                        migration_plan_id,
                        node_id,
                        new_revision,
                        current_configuration,
                        expected_digest,
                        Json(plan_items),
                        Json([]),
                        plan_digest,
                        actor.strip(),
                    ),
                )
                cursor.execute(
                    "UPDATE t_installed_point_processings SET current=FALSE "
                    "WHERE id=%s AND current=TRUE",
                    (installed_id,),
                )
                if cursor.rowcount != 1:
                    raise CutoverError("CUTOVER_CONFIGURATION_REVISION_MISMATCH")
                cursor.execute(
                    """
                    INSERT INTO t_installed_point_processings
                      (id,node_id,revision_id,source_plan_id,
                       configuration_revision,installed_by,current)
                    VALUES(%s,%s,%s,%s,%s,%s,TRUE)
                    """,
                    (
                        new_installed_id,
                        node_id,
                        new_revision,
                        migration_plan_id,
                        new_configuration,
                        actor.strip(),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_input_bindings
                      (installed_processing_id,input_id,source_kind,
                       l0_tag_id,l2_entity_instance_id,confirmed_by,confirmed_at)
                    SELECT %s,new_input.id,binding.source_kind,
                           binding.l0_tag_id,binding.l2_entity_instance_id,
                           %s,CURRENT_TIMESTAMP
                    FROM t_point_processing_input_bindings AS binding
                    JOIN t_point_processing_inputs AS old_input
                      ON old_input.id=binding.input_id
                    JOIN t_point_processing_inputs AS new_input
                      ON new_input.revision_id=%s
                     AND new_input.input_key=old_input.input_key
                    WHERE binding.installed_processing_id=%s
                    """,
                    (new_installed_id, actor.strip(), new_revision, installed_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_output_bindings
                      (installed_processing_id,output_id,entity_instance_id)
                    SELECT %s,new_output.id,binding.entity_instance_id
                    FROM t_point_processing_output_bindings AS binding
                    JOIN t_point_processing_outputs AS old_output
                      ON old_output.id=binding.output_id
                    JOIN t_point_processing_outputs AS new_output
                      ON new_output.revision_id=%s
                     AND new_output.output_key=old_output.output_key
                    WHERE binding.installed_processing_id=%s
                    """,
                    (new_installed_id, new_revision, installed_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_selector_members
                      (installed_processing_id,input_id,ordinal,
                       entity_instance_id,selector_digest)
                    SELECT %s,new_input.id,member.ordinal,
                           member.entity_instance_id,member.selector_digest
                    FROM t_point_processing_selector_members AS member
                    JOIN t_point_processing_inputs AS old_input
                      ON old_input.id=member.input_id
                    JOIN t_point_processing_inputs AS new_input
                      ON new_input.revision_id=%s
                     AND new_input.input_key=old_input.input_key
                    WHERE member.installed_processing_id=%s
                    """,
                    (new_installed_id, new_revision, installed_id),
                )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_dependencies
                      (installed_processing_id,input_id,output_id,
                       source_entity_instance_id,target_entity_instance_id)
                    SELECT %s,new_input.id,new_output.id,
                           dependency.source_entity_instance_id,
                           dependency.target_entity_instance_id
                    FROM t_point_processing_dependencies AS dependency
                    JOIN t_point_processing_inputs AS old_input
                      ON old_input.id=dependency.input_id
                    JOIN t_point_processing_outputs AS old_output
                      ON old_output.id=dependency.output_id
                    JOIN t_point_processing_inputs AS new_input
                      ON new_input.revision_id=%s
                     AND new_input.input_key=old_input.input_key
                    JOIN t_point_processing_outputs AS new_output
                      ON new_output.revision_id=%s
                     AND new_output.output_key=old_output.output_key
                    WHERE dependency.installed_processing_id=%s
                    """,
                    (
                        new_installed_id,
                        new_revision,
                        new_revision,
                        installed_id,
                    ),
                )
                cursor.execute(
                    "SELECT array_agg(entity_instance_id ORDER BY entity_instance_id) "
                    "FROM t_point_processing_output_bindings "
                    "WHERE installed_processing_id=%s",
                    (new_installed_id,),
                )
                output_entity_ids = cursor.fetchone()[0] or []
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_applications
                      (id,plan_id,installed_processing_id,
                       configuration_revision,actor,output_entity_instance_ids)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        application_id,
                        migration_plan_id,
                        new_installed_id,
                        new_configuration,
                        actor.strip(),
                        output_entity_ids,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_point_processing_idempotency
                      (actor,idempotency_key,request_digest,application_id)
                    VALUES(%s,%s,%s,%s)
                    """,
                    (
                        actor.strip(),
                        f"l0-raw-bit-hard-cut/{migration_plan_id}",
                        plan_digest,
                        application_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_audit_events
                      (id,event,outcome,reason,actor,target,details)
                    VALUES(%s,'configuration.change','applied',
                           'immutable L0 raw BIT hard cut',%s,%s,%s)
                    """,
                    (
                        uuid4(),
                        actor.strip(),
                        "l0_raw_bit_hard_cut",
                        Json(
                            {
                                "kind": "l0_raw_bit_hard_cut",
                                "old_installation_id": str(installed_id),
                                "new_installation_id": str(new_installed_id),
                                "old_revision_id": str(old_revision),
                                "new_revision_id": str(new_revision),
                                "configuration_revision": new_configuration,
                            }
                        ),
                    ),
                )

            cursor.execute(
                """
                UPDATE t_tags
                SET data_type='INT',value_data_type='INT'
                WHERE upper(COALESCE(wire_data_type,''))='BIT'
                """
            )
        connection.commit()
        return tuple(
            sorted(
                {value[0] for value in new_by_old_revision.values()},
                key=str,
            )
        )
    except CutoverError:
        connection.rollback()
        raise
    except PointProcessingTemplateError as exc:
        connection.rollback()
        raise CutoverError("CUTOVER_PROCESSING_REVISION_INVALID") from exc
    except Exception as exc:
        connection.rollback()
        raise CutoverError("CUTOVER_APPLY_FAILED") from exc


_RUNTIME_TABLES = (
    "t_alarm_notification_outbox",
    "t_alarm_transitions",
    "t_alarm_events",
    "t_jdm_executions",
    "t_business_metric_acceptance_reports",
    "t_business_metric_window_results",
    "t_business_metric_projections",
    "t_business_metric_recomputations",
    "t_en9_acceptance_ws_receipts",
    "t_point_processing_formula_runs",
    "t_committed_frame_consumers",
    "t_l2_observation_sources",
    "t_data_frame_outbox",
    "t_l2_stream_outbox",
    "t_l2_latest",
    "t_l2_observations",
    "t_telemetry_latest",
    "t_telemetry",
    "t_l0_observation_dedup",
    "t_ingestion_failures",
    "t_data_frames",
    "t_runtime_health_samples",
    "t_runtime_instances",
)


def clear_runtime_test_data(
    connection: Any,
    *,
    expected_configuration_revision: int,
) -> dict[str, int]:
    """Clear runtime evidence only; configuration and domain assets are preserved."""
    try:
        _acquire_runtime_locks(connection)
        actual = PostgresConfigurationRevisions().current(connection)
        if actual != expected_configuration_revision:
            raise CutoverError("CUTOVER_CONFIGURATION_REVISION_MISMATCH")
        deleted: dict[str, int] = {}
        with connection.cursor() as cursor:
            existing: list[str] = []
            for table in _RUNTIME_TABLES:
                cursor.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if cursor.fetchone()[0] is None:
                    continue
                existing.append(table)
                cursor.execute(f'ALTER TABLE public."{table}" DISABLE TRIGGER USER')
            for table in existing:
                cursor.execute(f'DELETE FROM public."{table}"')
                deleted[table] = max(cursor.rowcount, 0)
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table in reversed(existing):
                cursor.execute(f'ALTER TABLE public."{table}" ENABLE TRIGGER USER')
        connection.commit()
        return deleted
    except CutoverError:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        raise CutoverError("CUTOVER_RUNTIME_CLEAR_FAILED") from exc


__all__ = [
    "CutoverBlocker",
    "CutoverError",
    "CutoverReport",
    "apply_cutover",
    "clear_runtime_test_data",
    "inspect_cutover",
]
