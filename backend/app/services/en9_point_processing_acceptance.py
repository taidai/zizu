"""Read-only, machine-verifiable EN9 point-processing acceptance report."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID


ConnectionFactory = Callable[[], AbstractContextManager[Any]]


@dataclass(frozen=True)
class AcceptanceCheck:
    code: str
    passed: bool
    evidence: Mapping[str, int | float | str | bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class AcceptanceReport:
    application_id: UUID
    required_input_count: int
    output_entity_count: int
    observed_for_seconds: float
    passed: bool
    checks: tuple[AcceptanceCheck, ...]
    generated_at: datetime

    def public_dict(self) -> dict[str, object]:
        return {
            "application_id": str(self.application_id),
            "required_input_count": self.required_input_count,
            "output_entity_count": self.output_entity_count,
            "observed_for_seconds": self.observed_for_seconds,
            "passed": self.passed,
            "generated_at": self.generated_at.isoformat(),
            "checks": [
                {
                    "code": item.code,
                    "passed": item.passed,
                    "evidence": dict(item.evidence),
                }
                for item in self.checks
            ],
        }


def run_en9_acceptance(
    application_id: UUID,
    observed_for_seconds: float,
    *,
    connection_factory: ConnectionFactory | None = None,
    ws_authenticated: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> AcceptanceReport:
    """Evaluate persisted EN9 evidence without changing configuration or runtime."""
    if observed_for_seconds < 0:
        raise ValueError("observed_for_seconds cannot be negative")
    if connection_factory is None:
        from app.services.telemetry_store import get_connection

        connection_factory = get_connection
    generated_at = (clock or (lambda: datetime.now(UTC)))()
    with connection_factory() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT installed.id, installed.revision_id,
                       installed.site_configuration_version,
                       installed.current, template.asset_id, revision.revision,
                       state.current_version
                FROM t_point_processing_applications AS application
                JOIN t_installed_point_processings AS installed
                  ON installed.id = application.installed_processing_id
                JOIN t_point_processing_revisions AS revision
                  ON revision.id = installed.revision_id
                JOIN t_point_processing_templates AS template
                  ON template.id = revision.template_id
                JOIN t_site_configuration_state AS state ON TRUE
                WHERE application.id = %s
                """,
                (str(application_id),),
            )
            installation = cursor.fetchone()
            if installation is None:
                raise ValueError("EN9_APPLICATION_NOT_FOUND")
            (
                installed_id,
                revision_id,
                site_version,
                current,
                asset_id,
                revision_number,
                current_site_version,
            ) = installation
            cursor.execute(
                """
                SELECT
                  (SELECT count(*) FROM t_point_processing_input_bindings
                   WHERE installed_processing_id = %s),
                  (SELECT count(*) FROM t_point_processing_output_bindings
                   WHERE installed_processing_id = %s),
                  (SELECT count(*)
                   FROM t_boolean_set_mapping_entries AS mapping
                   JOIN t_point_processing_outputs AS output
                     ON output.id = mapping.output_id
                   WHERE output.revision_id = %s)
                """,
                (str(installed_id), str(installed_id), str(revision_id)),
            )
            input_count, output_count, fault_input_count = cursor.fetchone()
            cursor.execute(
                """
                SELECT count(*),
                       count(*) FILTER (WHERE latest.quality = 192),
                       count(*) FILTER (
                         WHERE latest.ts >= %s - (%s * INTERVAL '1 second')
                       )
                FROM t_point_processing_input_bindings AS binding
                JOIN t_telemetry_latest AS latest
                  ON latest.tag_id = binding.l0_tag_id
                WHERE binding.installed_processing_id = %s
                """,
                (generated_at, observed_for_seconds, str(installed_id)),
            )
            l0_latest_count, l0_good_count, l0_fresh_count = cursor.fetchone()
            cursor.execute(
                """
                SELECT count(DISTINCT output_binding.entity_instance_id),
                       count(DISTINCT observation.entity_instance_id),
                       count(DISTINCT outbox.entity_instance_id)
                FROM t_point_processing_output_bindings AS output_binding
                LEFT JOIN t_l2_latest AS latest
                  ON latest.entity_instance_id = output_binding.entity_instance_id
                 AND latest.quality = 192
                LEFT JOIN t_l2_observations AS observation
                  ON observation.entity_instance_id = output_binding.entity_instance_id
                LEFT JOIN t_l2_stream_outbox AS outbox
                  ON outbox.entity_instance_id = output_binding.entity_instance_id
                WHERE output_binding.installed_processing_id = %s
                  AND latest.entity_instance_id IS NOT NULL
                """,
                (str(installed_id),),
            )
            l2_latest_count, l2_history_count, outbox_count = cursor.fetchone()
            cursor.execute(
                """
                SELECT count(DISTINCT source.l0_observation_id)
                FROM t_point_processing_output_bindings AS binding
                JOIN t_point_processing_outputs AS output ON output.id = binding.output_id
                JOIN t_l2_latest AS latest
                  ON latest.entity_instance_id = binding.entity_instance_id
                JOIN t_l2_observation_sources AS source
                  ON source.l2_event_id = latest.event_id
                 AND source.l2_observed_at = latest.observed_at
                 AND source.source_kind = 'l0'
                WHERE binding.installed_processing_id = %s
                  AND output.entity_definition_id = 'pcs.fault_codes'
                """,
                (str(installed_id),),
            )
            fault_source_count = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT rule.scale,
                       latest.value_float,
                       source.raw_value_float
                FROM t_point_processing_output_bindings AS binding
                JOIN t_point_processing_outputs AS output ON output.id = binding.output_id
                JOIN t_numeric_transform_rules AS rule ON rule.output_id = output.id
                JOIN t_point_processing_input_bindings AS input_binding
                  ON input_binding.installed_processing_id = binding.installed_processing_id
                 AND input_binding.input_id = rule.input_id
                JOIN t_telemetry_latest AS source ON source.tag_id = input_binding.l0_tag_id
                JOIN t_l2_latest AS latest
                  ON latest.entity_instance_id = binding.entity_instance_id
                WHERE binding.installed_processing_id = %s
                  AND output.entity_definition_id = 'pcs.active_power'
                """,
                (str(installed_id),),
            )
            power = cursor.fetchone()
            cursor.execute(
                """
                SELECT latest.value_text,
                       EXISTS (
                         SELECT 1 FROM t_enum_mapping_entries AS mapping
                         WHERE mapping.output_id = output.id
                           AND mapping.canonical_value = latest.value_text
                       )
                FROM t_point_processing_output_bindings AS binding
                JOIN t_point_processing_outputs AS output ON output.id = binding.output_id
                JOIN t_l2_latest AS latest
                  ON latest.entity_instance_id = binding.entity_instance_id
                WHERE binding.installed_processing_id = %s
                  AND output.entity_definition_id = 'pcs.operating_state'
                """,
                (str(installed_id),),
            )
            state = cursor.fetchone()

    power_scale_ok = bool(power and float(power[0]) == 1.0)
    power_sign_ok = bool(
        power
        and power[1] is not None
        and power[2] is not None
        and (float(power[1]) == 0 or float(power[2]) == 0
             or (float(power[1]) > 0) == (float(power[2]) > 0))
    )
    checks = (
        AcceptanceCheck(
            "EN9_CONFIGURATION_COUNTS",
            asset_id == "pcs.en9" and revision_number == 1
            and input_count == 90 and output_count == 3 and fault_input_count == 88,
            {"inputs": input_count, "outputs": output_count, "fault_inputs": fault_input_count},
        ),
        AcceptanceCheck(
            "EN9_L0_QUALITY_FRESHNESS",
            l0_latest_count == 90 and l0_good_count == 90 and l0_fresh_count == 90,
            {"latest": l0_latest_count, "good": l0_good_count, "fresh": l0_fresh_count},
        ),
        AcceptanceCheck(
            "EN9_L2_LATEST_HISTORY_OUTBOX",
            l2_latest_count == 3 and l2_history_count == 3 and outbox_count == 3,
            {"latest": l2_latest_count, "history": l2_history_count, "outbox": outbox_count},
        ),
        AcceptanceCheck(
            "EN9_FAULT_SOURCE_EVIDENCE",
            fault_source_count == 88,
            {"sources": fault_source_count},
        ),
        AcceptanceCheck(
            "EN9_POWER_SCALE_AND_SIGN",
            power_scale_ok and power_sign_ok,
            {"scale_is_one": power_scale_ok, "sign_preserved": power_sign_ok},
        ),
        AcceptanceCheck(
            "EN9_STATE_ENUM",
            bool(state and state[0] and state[1]),
            {"mapped": bool(state and state[1])},
        ),
        AcceptanceCheck(
            "EN9_AUTHENTICATED_WS",
            ws_authenticated,
            {"authenticated": ws_authenticated},
        ),
        AcceptanceCheck(
            "EN9_RESTART_CONTINUITY",
            bool(current) and int(site_version) == int(current_site_version),
            {"current": bool(current), "site_version": int(site_version)},
        ),
    )
    return AcceptanceReport(
        application_id=application_id,
        required_input_count=int(input_count),
        output_entity_count=int(output_count),
        observed_for_seconds=float(observed_for_seconds),
        passed=all(item.passed for item in checks),
        checks=checks,
        generated_at=generated_at,
    )
