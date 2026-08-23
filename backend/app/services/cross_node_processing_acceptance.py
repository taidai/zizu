"""Immutable machine acceptance for an installed cross-node L2 formula."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg2.extras import Json

from app.services.point_processing_dag import (
    PointProcessingDagError,
    validate_processing_dag,
)


ConnectionFactory = Callable[[], AbstractContextManager[Any]]


@dataclass(frozen=True)
class CrossNodeAcceptanceCheck:
    code: str
    passed: bool
    evidence: Mapping[str, int | float | str | bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class CrossNodeAcceptanceReport:
    id: UUID
    application_id: UUID
    output_definition_id: str
    frozen_member_count: int
    source_entity_count: int
    output_value: float | None
    restart_continuity: bool
    authenticated_ws_delivery: bool
    late_input_protected: bool
    passed: bool
    checks: tuple[CrossNodeAcceptanceCheck, ...]
    generated_at: datetime
    digest: str

    def public_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "application_id": str(self.application_id),
            "output_definition_id": self.output_definition_id,
            "frozen_member_count": self.frozen_member_count,
            "source_entity_count": self.source_entity_count,
            "output_value": self.output_value,
            "restart_continuity": self.restart_continuity,
            "authenticated_ws_delivery": self.authenticated_ws_delivery,
            "late_input_protected": self.late_input_protected,
            "passed": self.passed,
            "generated_at": self.generated_at.isoformat(),
            "digest": self.digest,
            "checks": [
                {
                    "code": item.code,
                    "passed": item.passed,
                    "evidence": dict(item.evidence),
                }
                for item in self.checks
            ],
        }


def run_cross_node_processing_acceptance(
    application_id: UUID,
    *,
    connection_factory: ConnectionFactory | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CrossNodeAcceptanceReport:
    """Evaluate persisted production facts and append an idempotent report."""
    if connection_factory is None:
        from app.services.telemetry_store import get_connection

        connection_factory = get_connection
    generated_at = (clock or (lambda: datetime.now(UTC)))()
    with connection_factory() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT installed.id, installed.revision_id,
                           installed.site_configuration_version,
                           installed.current, state.current_version,
                           output.id, output.entity_definition_id,
                           output_binding.entity_instance_id,
                           expression.ast_digest,
                           expression.control_eligible
                    FROM t_point_processing_applications AS application
                    JOIN t_installed_point_processings AS installed
                      ON installed.id = application.installed_processing_id
                    JOIN t_point_processing_output_bindings AS output_binding
                      ON output_binding.installed_processing_id = installed.id
                    JOIN t_point_processing_outputs AS output
                      ON output.id = output_binding.output_id
                    JOIN t_point_processing_expressions AS expression
                      ON expression.output_id = output.id
                    JOIN t_site_configuration_state AS state ON TRUE
                    WHERE application.id = %s
                    """,
                    (str(application_id),),
                )
                rows = cursor.fetchall()
                if len(rows) != 1:
                    raise ValueError("CROSS_NODE_ACCEPTANCE_APPLICATION_INVALID")
                (
                    installed_id,
                    revision_id,
                    site_version,
                    current,
                    current_site_version,
                    output_id,
                    output_definition_id,
                    output_entity_id,
                    ast_digest,
                    control_eligible,
                ) = rows[0]

                cursor.execute(
                    """
                    SELECT entity_instance_id
                    FROM t_point_processing_selector_members
                    WHERE installed_processing_id = %s
                    ORDER BY entity_instance_id
                    """,
                    (installed_id,),
                )
                member_ids = tuple(UUID(str(row[0])) for row in cursor.fetchall())
                cursor.execute(
                    """
                    SELECT source_entity_instance_id, target_entity_instance_id
                    FROM t_point_processing_dependencies
                    WHERE installed_processing_id = %s AND output_id = %s
                    ORDER BY source_entity_instance_id, target_entity_instance_id
                    """,
                    (installed_id, output_id),
                )
                edges = tuple(
                    (UUID(str(source)), UUID(str(target)))
                    for source, target in cursor.fetchall()
                )
                dependency_ids = tuple(source for source, _ in edges)
                try:
                    dag = validate_processing_dag(
                        existing_edges=edges,
                        planned_edges=(),
                        max_depth=8,
                    )
                    dag_depth = dag.max_depth
                    dag_valid = True
                except PointProcessingDagError:
                    dag_depth = 0
                    dag_valid = False

                cursor.execute(
                    """
                    SELECT event_id, observed_at, value_float, quality,
                           processing_revision_id, site_configuration_version,
                           producing_runtime_instance_id
                    FROM t_l2_latest
                    WHERE entity_instance_id = %s
                    """,
                    (output_entity_id,),
                )
                latest = cursor.fetchone()
                if latest is None:
                    latest_event_id = None
                    latest_observed_at = None
                    output_value = None
                    latest_quality = None
                    latest_revision_id = None
                    latest_site_version = None
                    runtime_instance_id = None
                else:
                    (
                        latest_event_id,
                        latest_observed_at,
                        output_value,
                        latest_quality,
                        latest_revision_id,
                        latest_site_version,
                        runtime_instance_id,
                    ) = latest

                cursor.execute(
                    """
                    SELECT count(*),
                           count(DISTINCT producing_runtime_instance_id)
                    FROM t_l2_observations
                    WHERE entity_instance_id = %s
                      AND processing_revision_id = %s
                      AND site_configuration_version = %s
                    """,
                    (output_entity_id, revision_id, site_version),
                )
                history_count, runtime_instance_count = (
                    int(value) for value in cursor.fetchone()
                )

                source_entity_ids: tuple[UUID, ...] = ()
                source_latest_match_count = 0
                outbox_count = 0
                checkpoint_matches = False
                authenticated_ws_receipt_count = 0
                if latest_event_id is not None:
                    cursor.execute(
                        """
                        SELECT DISTINCT source_observation.entity_instance_id
                        FROM t_l2_observation_sources AS source
                        JOIN t_l2_observations AS source_observation
                          ON source_observation.event_id = source.source_l2_event_id
                         AND source_observation.observed_at = source.source_l2_observed_at
                        WHERE source.l2_event_id = %s
                          AND source.l2_observed_at = %s
                          AND source.source_kind = 'l2'
                        ORDER BY source_observation.entity_instance_id
                        """,
                        (latest_event_id, latest_observed_at),
                    )
                    source_entity_ids = tuple(
                        UUID(str(row[0])) for row in cursor.fetchall()
                    )
                    cursor.execute(
                        """
                        SELECT count(DISTINCT source_observation.entity_instance_id)
                        FROM t_l2_observation_sources AS source
                        JOIN t_l2_observations AS source_observation
                          ON source_observation.event_id = source.source_l2_event_id
                         AND source_observation.observed_at = source.source_l2_observed_at
                        JOIN t_l2_latest AS source_latest
                          ON source_latest.entity_instance_id =
                               source_observation.entity_instance_id
                         AND source_latest.event_id = source.source_l2_event_id
                         AND source_latest.observed_at = source.source_l2_observed_at
                        WHERE source.l2_event_id = %s
                          AND source.l2_observed_at = %s
                          AND source.source_kind = 'l2'
                        """,
                        (latest_event_id, latest_observed_at),
                    )
                    source_latest_match_count = int(cursor.fetchone()[0])
                    cursor.execute(
                        "SELECT count(*) FROM t_l2_stream_outbox WHERE event_id = %s",
                        (latest_event_id,),
                    )
                    outbox_count = int(cursor.fetchone()[0])
                    cursor.execute(
                        """
                        SELECT count(*)
                        FROM t_point_processing_formula_runs
                        WHERE installed_processing_id = %s
                          AND output_id = %s
                          AND last_event_id = %s
                        """,
                        (installed_id, output_id, latest_event_id),
                    )
                    checkpoint_matches = int(cursor.fetchone()[0]) == 1
                    cursor.execute(
                        """
                        SELECT count(*)
                        FROM t_en9_acceptance_ws_receipts
                        WHERE application_id = %s
                          AND event_id = %s
                          AND entity_instance_id = %s
                        """,
                        (application_id, latest_event_id, output_entity_id),
                    )
                    authenticated_ws_receipt_count = int(cursor.fetchone()[0])

                cursor.execute(
                    """
                    SELECT count(*)
                    FROM t_l2_observations AS history
                    JOIN t_l2_latest AS latest
                      ON latest.entity_instance_id = history.entity_instance_id
                    WHERE history.entity_instance_id = ANY(%s::uuid[])
                      AND history.observed_at < latest.observed_at
                      AND history.calculated_at > latest.calculated_at
                    """,
                    ([str(item) for item in member_ids],),
                )
                late_source_history_count = int(cursor.fetchone()[0])

                checks = (
                    CrossNodeAcceptanceCheck(
                        "CURRENT_CONFIGURATION",
                        bool(current and int(site_version) == int(current_site_version)),
                        {
                            "installed_site_version": int(site_version),
                            "current_site_version": int(current_site_version),
                        },
                    ),
                    CrossNodeAcceptanceCheck(
                        "SAFE_FORMULA_CONTRACT",
                        bool(ast_digest and not control_eligible),
                        {
                            "ast_digest": str(ast_digest).strip(),
                            "control_eligible": bool(control_eligible),
                        },
                    ),
                    CrossNodeAcceptanceCheck(
                        "FROZEN_MEMBERS_MATCH_DEPENDENCIES",
                        bool(member_ids and member_ids == dependency_ids),
                        {
                            "frozen_member_count": len(member_ids),
                            "dependency_count": len(dependency_ids),
                        },
                    ),
                    CrossNodeAcceptanceCheck(
                        "DAG_VALID",
                        dag_valid and dag_depth <= 8,
                        {"depth": dag_depth, "maximum_depth": 8},
                    ),
                    CrossNodeAcceptanceCheck(
                        "L2_LATEST_AND_HISTORY",
                        bool(
                            latest_event_id
                            and latest_quality == 192
                            and UUID(str(latest_revision_id)) == UUID(str(revision_id))
                            and int(latest_site_version) == int(site_version)
                            and runtime_instance_id
                            and history_count >= 1
                        ),
                        {
                            "history_count": history_count,
                            "latest_quality": int(latest_quality or 0),
                            "runtime_recorded": bool(runtime_instance_id),
                        },
                    ),
                    CrossNodeAcceptanceCheck(
                        "SOURCE_COVERAGE",
                        source_entity_ids == member_ids,
                        {
                            "source_entity_count": len(source_entity_ids),
                            "frozen_member_count": len(member_ids),
                        },
                    ),
                    CrossNodeAcceptanceCheck(
                        "RESTART_CONTINUITY",
                        runtime_instance_count >= 2,
                        {"runtime_instance_count": runtime_instance_count},
                    ),
                    CrossNodeAcceptanceCheck(
                        "AUTHENTICATED_WS_DELIVERY",
                        authenticated_ws_receipt_count >= 1,
                        {
                            "authenticated_receipt_count": (
                                authenticated_ws_receipt_count
                            )
                        },
                    ),
                    CrossNodeAcceptanceCheck(
                        "LATE_L2_DID_NOT_REWIND_LATEST",
                        bool(
                            late_source_history_count >= 1
                            and source_latest_match_count == len(member_ids)
                        ),
                        {
                            "late_source_history_count": late_source_history_count,
                            "current_source_count": source_latest_match_count,
                        },
                    ),
                    CrossNodeAcceptanceCheck(
                        "OUTBOX_AND_CHECKPOINT",
                        outbox_count == 1 and checkpoint_matches,
                        {
                            "outbox_count": outbox_count,
                            "checkpoint_matches": checkpoint_matches,
                        },
                    ),
                )
                passed = all(check.passed for check in checks)
                evidence = {
                    "application_id": str(application_id),
                    "installed_processing_id": str(installed_id),
                    "processing_revision_id": str(revision_id),
                    "site_configuration_version": int(site_version),
                    "output_definition_id": output_definition_id,
                    "output_value": output_value,
                    "frozen_member_ids": [str(item) for item in member_ids],
                    "source_entity_ids": [str(item) for item in source_entity_ids],
                    "checks": [
                        {
                            "code": item.code,
                            "passed": item.passed,
                            "evidence": dict(item.evidence),
                        }
                        for item in checks
                    ],
                }
                canonical = json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                report_id = uuid5(
                    NAMESPACE_URL,
                    f"zizu/cross-node-acceptance/{application_id}/{digest}",
                )
                cursor.execute(
                    """
                    INSERT INTO t_cross_node_processing_acceptance_reports
                      (id, application_id, status, generated_at, evidence, digest)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (application_id, digest) DO NOTHING
                    """,
                    (
                        str(report_id),
                        str(application_id),
                        "passed" if passed else "failed",
                        generated_at,
                        Json(evidence),
                        digest,
                    ),
                )
                cursor.execute(
                    """
                    SELECT id, status, generated_at, evidence, digest
                    FROM t_cross_node_processing_acceptance_reports
                    WHERE application_id = %s AND digest = %s
                    """,
                    (str(application_id), digest),
                )
                stored_id, stored_status, stored_at, stored_evidence, stored_digest = (
                    cursor.fetchone()
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    stored_checks = tuple(
        CrossNodeAcceptanceCheck(
            item["code"], bool(item["passed"]), item["evidence"]
        )
        for item in stored_evidence["checks"]
    )
    return CrossNodeAcceptanceReport(
        id=UUID(str(stored_id)),
        application_id=application_id,
        output_definition_id=stored_evidence["output_definition_id"],
        frozen_member_count=len(stored_evidence["frozen_member_ids"]),
        source_entity_count=len(stored_evidence["source_entity_ids"]),
        output_value=stored_evidence["output_value"],
        restart_continuity=next(
            item.passed for item in stored_checks
            if item.code == "RESTART_CONTINUITY"
        ),
        authenticated_ws_delivery=next(
            item.passed for item in stored_checks
            if item.code == "AUTHENTICATED_WS_DELIVERY"
        ),
        late_input_protected=next(
            item.passed for item in stored_checks
            if item.code == "LATE_L2_DID_NOT_REWIND_LATEST"
        ),
        passed=stored_status == "passed",
        checks=stored_checks,
        generated_at=stored_at,
        digest=stored_digest.strip(),
    )
