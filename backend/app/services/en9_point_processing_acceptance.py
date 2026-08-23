"""Machine-verifiable, immutable EN9 point-processing acceptance reports."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg2.extras import Json


ConnectionFactory = Callable[[], AbstractContextManager[Any]]
MINIMUM_OBSERVATION_SECONDS = 1800.0


@dataclass(frozen=True)
class EN9StreamBinding:
    application_id: UUID
    revision_id: UUID
    site_configuration_version: int
    entity_instance_ids: frozenset[UUID]
    user_id: UUID
    session_id: UUID


class PostgresEN9StreamEvidence:
    """Bind authenticated WS subscriptions and append actual delivery receipts."""

    def __init__(self, connection_factory: ConnectionFactory | None = None) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory

    def bind(self, application_id: UUID, entity_instance_ids, principal) -> EN9StreamBinding:
        requested = frozenset(entity_instance_ids)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT installed.revision_id,
                           installed.site_configuration_version,
                           application.output_entity_instance_ids,
                           installed.current,
                           state.current_version
                    FROM t_point_processing_applications AS application
                    JOIN t_installed_point_processings AS installed
                      ON installed.id = application.installed_processing_id
                    JOIN t_site_configuration_state AS state ON TRUE
                    WHERE application.id = %s
                    """,
                    (str(application_id),),
                )
                row = cursor.fetchone()
        if row is None:
            raise ValueError("EN9_APPLICATION_NOT_FOUND")
        revision_id, site_version, output_ids, current, current_version = row
        expected = frozenset(UUID(str(item)) for item in output_ids)
        if (
            not current
            or int(site_version) != int(current_version)
            or requested != expected
        ):
            raise ValueError("EN9_ACCEPTANCE_SUBSCRIPTION_INVALID")
        return EN9StreamBinding(
            application_id=application_id,
            revision_id=UUID(str(revision_id)),
            site_configuration_version=int(site_version),
            entity_instance_ids=expected,
            user_id=principal.user_id,
            session_id=principal.session_id,
        )

    def register_runtime(self, runtime_instance_id: UUID) -> None:
        from app.api.health import _VERSION as platform_version

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_runtime_instances (id, started_at, platform_version)
                    VALUES (%s, now(), %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (str(runtime_instance_id), platform_version),
                )
            connection.commit()

    def record_health(
        self,
        runtime_instance_id: UUID,
        *,
        pipeline_running: bool,
        mqtt_connected: bool,
        last_message_at: datetime | None,
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_runtime_health_samples
                      (runtime_instance_id, pipeline_running, mqtt_connected,
                       last_message_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        str(runtime_instance_id), pipeline_running,
                        mqtt_connected, last_message_at,
                    ),
                )
            connection.commit()

    def record_acknowledgement(self, binding, event, runtime_instance_id: UUID) -> None:
        if (
            not isinstance(binding, EN9StreamBinding)
            or event.entity_instance_id not in binding.entity_instance_ids
            or event.payload.get("processing_revision_id") != str(binding.revision_id)
            or int(event.payload.get("site_configuration_version", -1))
            != binding.site_configuration_version
        ):
            raise ValueError("EN9_ACCEPTANCE_STREAM_EVENT_INVALID")
        receipt_id = uuid5(
            NAMESPACE_URL,
            "/".join((
                "zizu/en9-ws-receipt",
                str(binding.application_id),
                str(event.event_id),
                str(binding.session_id),
                str(runtime_instance_id),
            )),
        )
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO t_en9_acceptance_ws_receipts
                          (id, application_id, event_id, entity_instance_id,
                           user_id, session_id, runtime_instance_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (
                          application_id, event_id, session_id,
                          runtime_instance_id
                        ) DO NOTHING
                        """,
                        (
                            str(receipt_id), str(binding.application_id),
                            str(event.event_id), str(event.entity_instance_id),
                            str(binding.user_id), str(binding.session_id),
                            str(runtime_instance_id),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise


@dataclass(frozen=True)
class AcceptanceCheck:
    code: str
    passed: bool
    evidence: Mapping[str, int | float | str | bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class AcceptanceReport:
    id: UUID
    application_id: UUID
    required_input_count: int
    output_entity_count: int
    observed_for_seconds: float
    passed: bool
    checks: tuple[AcceptanceCheck, ...]
    generated_at: datetime
    digest: str

    def public_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "application_id": str(self.application_id),
            "required_input_count": self.required_input_count,
            "output_entity_count": self.output_entity_count,
            "observed_for_seconds": self.observed_for_seconds,
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


def run_en9_acceptance(
    application_id: UUID,
    observed_for_seconds: float,
    *,
    connection_factory: ConnectionFactory | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AcceptanceReport:
    """Evaluate persisted EN9 evidence and append an immutable report."""
    if observed_for_seconds < MINIMUM_OBSERVATION_SECONDS:
        raise ValueError("EN9_ACCEPTANCE_WINDOW_TOO_SHORT")
    if connection_factory is None:
        from app.services.telemetry_store import get_connection

        connection_factory = get_connection
    generated_at = (clock or (lambda: datetime.now(UTC)))()
    window_started_at = generated_at - timedelta(
        seconds=observed_for_seconds
    )
    with connection_factory() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT installed.id, installed.revision_id,
                       installed.site_configuration_version,
                       installed.current, template.asset_id, revision.revision,
                       state.current_version, application.applied_at
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
                applied_at,
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
                         WHERE tag.freshness_seconds IS NOT NULL
                           AND latest.ts + (
                             tag.freshness_seconds * INTERVAL '1 second'
                           ) > %s
                       )
                FROM t_point_processing_input_bindings AS binding
                JOIN t_telemetry_latest AS latest
                  ON latest.tag_id = binding.l0_tag_id
                JOIN t_tags AS tag ON tag.id = binding.l0_tag_id
                WHERE binding.installed_processing_id = %s
                """,
                (generated_at, str(installed_id)),
            )
            l0_latest_count, l0_good_count, l0_fresh_count = cursor.fetchone()
            cursor.execute(
                """
                SELECT count(*)
                FROM t_point_processing_output_bindings AS binding
                WHERE binding.installed_processing_id = %s
                  AND EXISTS (
                    SELECT 1 FROM t_l2_latest AS latest
                    WHERE latest.entity_instance_id = binding.entity_instance_id
                      AND latest.quality = 192
                      AND latest.processing_revision_id = %s
                      AND latest.site_configuration_version = %s
                  )
                """,
                (str(installed_id), str(revision_id), site_version),
            )
            l2_latest_count = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT count(DISTINCT binding.entity_instance_id)
                FROM t_point_processing_output_bindings AS binding
                WHERE binding.installed_processing_id = %s
                  AND EXISTS (
                    SELECT 1 FROM t_l2_stream_outbox AS outbox
                    WHERE outbox.entity_instance_id = binding.entity_instance_id
                      AND outbox.payload->>'processing_revision_id' = %s
                      AND (outbox.payload->>'site_configuration_version')::bigint = %s
                      AND outbox.created_at BETWEEN %s AND %s
                  )
                """,
                (
                    str(installed_id), str(revision_id), site_version,
                    window_started_at, generated_at,
                ),
            )
            outbox_count = cursor.fetchone()[0]
            cursor.execute(
                """
                WITH expected_sources AS (
                  SELECT rule.output_id, binding.l0_tag_id
                  FROM t_numeric_transform_rules AS rule
                  JOIN t_point_processing_input_bindings AS binding
                    ON binding.input_id = rule.input_id
                   AND binding.installed_processing_id = %s
                  UNION ALL
                  SELECT rule.output_id, binding.l0_tag_id
                  FROM t_enum_transform_rules AS rule
                  JOIN t_point_processing_input_bindings AS binding
                    ON binding.input_id = rule.input_id
                   AND binding.installed_processing_id = %s
                  UNION ALL
                  SELECT mapping.output_id, binding.l0_tag_id
                  FROM t_boolean_set_mapping_entries AS mapping
                  JOIN t_point_processing_input_bindings AS binding
                    ON binding.input_id = mapping.input_id
                   AND binding.installed_processing_id = %s
                )
                SELECT output.entity_definition_id,
                       output_binding.entity_instance_id,
                       array_agg(
                         observation.calculated_at
                         ORDER BY observation.calculated_at
                       )
                FROM t_point_processing_output_bindings AS output_binding
                JOIN t_point_processing_outputs AS output
                  ON output.id = output_binding.output_id
                JOIN t_l2_observations AS observation
                  ON observation.entity_instance_id = output_binding.entity_instance_id
                 AND observation.processing_revision_id = %s
                 AND observation.site_configuration_version = %s
                 AND observation.calculated_at BETWEEN %s AND %s
                 AND observation.observed_at BETWEEN %s AND %s
                 AND observation.producing_runtime_instance_id IS NOT NULL
                 AND observation.quality = 192
                 AND abs(extract(epoch FROM (
                       observation.observed_at - observation.calculated_at
                     ))) <= 90
                 AND CASE output.entity_definition_id
                       WHEN 'pcs.active_power' THEN observation.value_float IS NOT NULL
                       WHEN 'pcs.operating_state' THEN observation.value_text IS NOT NULL
                       WHEN 'pcs.fault_codes' THEN observation.value_codes IS NOT NULL
                       ELSE FALSE
                     END
                 AND (
                   SELECT count(*)
                   FROM t_l2_observation_sources AS source
                   WHERE source.l2_event_id = observation.event_id
                     AND source.l2_observed_at = observation.observed_at
                 ) = CASE output.entity_definition_id
                       WHEN 'pcs.fault_codes' THEN 88 ELSE 1 END
                 AND (
                   SELECT count(*) FROM expected_sources AS expected
                   WHERE expected.output_id = output.id
                 ) = CASE output.entity_definition_id
                       WHEN 'pcs.fault_codes' THEN 88 ELSE 1 END
                 AND (
                   SELECT count(DISTINCT l0_meta.tag_id)
                   FROM t_l2_observation_sources AS linked_source
                   JOIN t_l0_observation_dedup AS l0_meta
                     ON l0_meta.observation_id = linked_source.l0_observation_id
                   JOIN t_telemetry AS l0
                     ON l0.observation_id = l0_meta.observation_id
                    AND l0.ts = l0_meta.observed_at
                    AND l0.tag_id = l0_meta.tag_id
                   JOIN expected_sources AS expected
                     ON expected.output_id = output.id
                    AND expected.l0_tag_id = l0_meta.tag_id
                   WHERE linked_source.l2_event_id = observation.event_id
                     AND linked_source.l2_observed_at = observation.observed_at
                     AND linked_source.source_kind = 'l0'
                     AND linked_source.source_digest = l0.source_digest
                     AND l0.quality = 192
                     AND abs(extract(epoch FROM (
                           l0.ts - observation.observed_at
                         ))) <= 90
                 ) = CASE output.entity_definition_id
                       WHEN 'pcs.fault_codes' THEN 88 ELSE 1 END
                WHERE output_binding.installed_processing_id = %s
                GROUP BY output.entity_definition_id,
                         output_binding.entity_instance_id
                """,
                (
                    str(installed_id), str(installed_id), str(installed_id),
                    str(revision_id), site_version,
                    window_started_at, generated_at,
                    window_started_at - timedelta(seconds=90),
                    generated_at + timedelta(seconds=90),
                    str(installed_id),
                ),
            )
            continuity_rows = cursor.fetchall()
            l2_history_count = len(continuity_rows)
            cursor.execute(
                """
                SELECT count(DISTINCT l0_meta.tag_id),
                       (
                         SELECT count(*)
                         FROM t_l2_observation_sources AS all_source
                         WHERE all_source.l2_event_id = latest.event_id
                           AND all_source.l2_observed_at = latest.observed_at
                       ), latest.producing_runtime_instance_id
                FROM t_point_processing_output_bindings AS binding
                JOIN t_point_processing_outputs AS output ON output.id = binding.output_id
                JOIN t_boolean_set_mapping_entries AS mapping
                  ON mapping.output_id = output.id
                JOIN t_point_processing_input_bindings AS input_binding
                  ON input_binding.installed_processing_id = binding.installed_processing_id
                 AND input_binding.input_id = mapping.input_id
                JOIN t_l2_latest AS latest
                  ON latest.entity_instance_id = binding.entity_instance_id
                 AND latest.processing_revision_id = %s
                 AND latest.site_configuration_version = %s
                JOIN t_l2_observation_sources AS source
                  ON source.l2_event_id = latest.event_id
                 AND source.l2_observed_at = latest.observed_at
                 AND source.source_kind = 'l0'
                JOIN t_l0_observation_dedup AS l0_meta
                  ON l0_meta.observation_id = source.l0_observation_id
                 AND l0_meta.tag_id = input_binding.l0_tag_id
                JOIN t_telemetry AS l0
                  ON l0.observation_id = l0_meta.observation_id
                 AND l0.ts = l0_meta.observed_at
                 AND l0.tag_id = l0_meta.tag_id
                 AND l0.quality = 192
                 AND l0.source_digest = source.source_digest
                 AND abs(extract(epoch FROM (
                       l0.ts - latest.observed_at
                     ))) <= 90
                WHERE binding.installed_processing_id = %s
                  AND output.entity_definition_id = 'pcs.fault_codes'
                GROUP BY latest.event_id, latest.observed_at,
                         latest.producing_runtime_instance_id
                """,
                (str(revision_id), site_version, str(installed_id)),
            )
            (
                fault_source_count,
                fault_total_source_count,
                fault_runtime_instance_id,
            ) = cursor.fetchone()
            cursor.execute(
                """
                SELECT rule.scale, rule."offset", latest.value_float,
                       source.raw_value_float, latest.quality, source.quality,
                       latest.source_digest, evidence.source_digest,
                       latest.event_id, latest.producing_runtime_instance_id
                FROM t_point_processing_output_bindings AS binding
                JOIN t_point_processing_outputs AS output ON output.id = binding.output_id
                JOIN t_numeric_transform_rules AS rule ON rule.output_id = output.id
                JOIN t_point_processing_input_bindings AS input_binding
                  ON input_binding.installed_processing_id = binding.installed_processing_id
                 AND input_binding.input_id = rule.input_id
                JOIN t_l2_latest AS latest
                  ON latest.entity_instance_id = binding.entity_instance_id
                 AND latest.processing_revision_id = %s
                 AND latest.site_configuration_version = %s
                JOIN t_l2_observation_sources AS evidence
                  ON evidence.l2_event_id = latest.event_id
                 AND evidence.l2_observed_at = latest.observed_at
                 AND evidence.source_kind = 'l0'
                JOIN t_l0_observation_dedup AS source_meta
                  ON source_meta.observation_id = evidence.l0_observation_id
                JOIN t_telemetry AS source
                  ON source.observation_id = source_meta.observation_id
                 AND source.ts = source_meta.observed_at
                 AND source.tag_id = source_meta.tag_id
                 AND source.tag_id = input_binding.l0_tag_id
                 AND source.quality = 192
                 AND source.source_digest = evidence.source_digest
                 AND abs(extract(epoch FROM (
                       source.ts - latest.observed_at
                     ))) <= 90
                WHERE binding.installed_processing_id = %s
                  AND output.entity_definition_id = 'pcs.active_power'
                """,
                (str(revision_id), site_version, str(installed_id)),
            )
            power = cursor.fetchone()
            cursor.execute(
                """
                SELECT latest.value_text, mapping.canonical_value,
                       COALESCE(source.raw_value_text, source.raw_value_int::text),
                       latest.quality, source.quality, latest.source_digest,
                       evidence.source_digest, latest.event_id,
                       latest.producing_runtime_instance_id
                FROM t_point_processing_output_bindings AS binding
                JOIN t_point_processing_outputs AS output ON output.id = binding.output_id
                JOIN t_enum_transform_rules AS rule ON rule.output_id = output.id
                JOIN t_point_processing_input_bindings AS input_binding
                  ON input_binding.installed_processing_id = binding.installed_processing_id
                 AND input_binding.input_id = rule.input_id
                JOIN t_l2_latest AS latest
                  ON latest.entity_instance_id = binding.entity_instance_id
                 AND latest.processing_revision_id = %s
                 AND latest.site_configuration_version = %s
                JOIN t_l2_observation_sources AS evidence
                  ON evidence.l2_event_id = latest.event_id
                 AND evidence.l2_observed_at = latest.observed_at
                 AND evidence.source_kind = 'l0'
                JOIN t_l0_observation_dedup AS source_meta
                  ON source_meta.observation_id = evidence.l0_observation_id
                JOIN t_telemetry AS source
                  ON source.observation_id = source_meta.observation_id
                 AND source.ts = source_meta.observed_at
                 AND source.tag_id = source_meta.tag_id
                 AND source.tag_id = input_binding.l0_tag_id
                 AND source.quality = 192
                 AND source.source_digest = evidence.source_digest
                 AND abs(extract(epoch FROM (
                       source.ts - latest.observed_at
                     ))) <= 90
                JOIN t_enum_mapping_entries AS mapping
                  ON mapping.output_id = output.id
                 AND mapping.raw_value = COALESCE(
                       source.raw_value_text, source.raw_value_int::text
                     )
                WHERE binding.installed_processing_id = %s
                  AND output.entity_definition_id = 'pcs.operating_state'
                """,
                (str(revision_id), site_version, str(installed_id)),
            )
            state = cursor.fetchone()
            cursor.execute(
                """
                SELECT count(*)
                FROM t_ingestion_failures
                WHERE created_at >= %s
                """,
                (window_started_at,),
            )
            ingestion_failure_count = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT count(*),
                       count(DISTINCT entity_instance_id),
                       count(DISTINCT runtime_instance_id),
                       count(DISTINCT user_id),
                       count(DISTINCT session_id),
                       min(delivered_at), max(delivered_at)
                FROM t_en9_acceptance_ws_receipts
                WHERE application_id = %s
                  AND delivered_at >= %s
                  AND delivered_at <= %s
                """,
                (str(application_id), window_started_at, generated_at),
            )
            (
                ws_receipt_count,
                ws_entity_count,
                ws_runtime_count,
                ws_user_count,
                ws_session_count,
                ws_first_at,
                ws_last_at,
            ) = cursor.fetchone()
            cursor.execute(
                """
                SELECT entity_instance_id,
                       array_agg(delivered_at ORDER BY delivered_at)
                FROM t_en9_acceptance_ws_receipts
                WHERE application_id = %s
                  AND delivered_at BETWEEN %s AND %s
                GROUP BY entity_instance_id
                """,
                (str(application_id), window_started_at, generated_at),
            )
            ws_entity_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT receipt.runtime_instance_id, runtime.started_at,
                       min(receipt.delivered_at), max(receipt.delivered_at),
                       count(DISTINCT receipt.entity_instance_id),
                       array_agg(
                         DISTINCT receipt.entity_instance_id
                         ORDER BY receipt.entity_instance_id
                       ),
                       count(DISTINCT observation.entity_instance_id)
                         FILTER (WHERE observation.producing_runtime_instance_id
                                       = receipt.runtime_instance_id),
                       max(observation.calculated_at)
                         FILTER (WHERE observation.producing_runtime_instance_id
                                       = receipt.runtime_instance_id)
                FROM t_en9_acceptance_ws_receipts AS receipt
                JOIN t_runtime_instances AS runtime
                  ON runtime.id = receipt.runtime_instance_id
                LEFT JOIN t_l2_observations AS observation
                  ON observation.event_id = receipt.event_id
                 AND observation.observed_at BETWEEN %s AND %s
                WHERE receipt.application_id = %s
                  AND receipt.delivered_at BETWEEN %s AND %s
                GROUP BY receipt.runtime_instance_id, runtime.started_at
                ORDER BY min(receipt.delivered_at), runtime.started_at
                """,
                (
                    window_started_at - timedelta(seconds=90),
                    generated_at + timedelta(seconds=90),
                    str(application_id), window_started_at, generated_at,
                ),
            )
            runtime_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT sample.runtime_instance_id, sample.sampled_at,
                       sample.pipeline_running, sample.mqtt_connected,
                       sample.last_message_at
                FROM t_runtime_health_samples AS sample
                WHERE sample.sampled_at BETWEEN %s AND %s
                  AND sample.runtime_instance_id = ANY(%s::uuid[])
                ORDER BY sample.sampled_at
                """,
                (
                    window_started_at, generated_at,
                    [str(row[0]) for row in runtime_rows],
                ),
            )
            health_rows = cursor.fetchall()

    continuity_counts = [len(row[2]) for row in continuity_rows]
    continuity_max_gap = max(
        (
            max(
                (right - left).total_seconds()
                for left, right in zip(times, times[1:])
            )
            if len(times) > 1 else float("inf")
        )
        for _, _, times in continuity_rows
    ) if continuity_rows else float("inf")
    continuity_ok = bool(
        len(continuity_rows) == 3
        and all(
            len(times) >= int(observed_for_seconds // 60)
            and times[0] <= window_started_at + timedelta(seconds=90)
            and times[-1] >= generated_at - timedelta(seconds=90)
            for _, _, times in continuity_rows
        )
        and continuity_max_gap <= 90
    )
    power_expected = (
        float(power[3]) * float(power[0]) + float(power[1])
        if power and power[2] is not None and power[3] is not None
        else None
    )
    power_tolerance = (
        max(1e-9, abs(power_expected) * 1e-6)
        if power_expected is not None else 0.0
    )
    power_value_ok = bool(
        power_expected is not None
        and abs(float(power[2]) - power_expected) <= power_tolerance
        and int(power[4]) == 192
        and int(power[5]) == 192
        and power[6]
        and power[7]
        and power[9] is not None
    )
    state_value_ok = bool(
        state
        and state[0] == state[1]
        and state[2] is not None
        and int(state[3]) == 192
        and int(state[4]) == 192
        and state[5]
        and state[6]
        and state[8] is not None
    )
    ws_window_ok = bool(
        ws_first_at is not None
        and ws_last_at is not None
        and ws_first_at <= window_started_at + timedelta(seconds=90)
        and ws_last_at >= generated_at - timedelta(seconds=90)
    )
    ws_entity_max_gap = max(
        (
            max(
                (
                    (right - left).total_seconds()
                    for left, right in zip(times, times[1:])
                ),
                default=float("inf"),
            )
            for _, times in ws_entity_rows
        ),
        default=float("inf"),
    )
    ws_entity_continuity_ok = bool(
        len(ws_entity_rows) == 3
        and all(
            len(times) >= int(observed_for_seconds // 60)
            and times[0] <= window_started_at + timedelta(seconds=90)
            and times[-1] >= generated_at - timedelta(seconds=90)
            for _, times in ws_entity_rows
        )
        and ws_entity_max_gap <= 90
    )
    runtime_details = [
        {
            "runtime": str(row[0]),
            "started_at": row[1].isoformat(),
            "first_receipt_at": row[2].isoformat(),
            "last_receipt_at": row[3].isoformat(),
            "entities": int(row[4]),
            "entity_ids": [str(item) for item in row[5]],
            "produced_entities": int(row[6]),
            "last_produced_at": row[7].isoformat() if row[7] else None,
        }
        for row in runtime_rows
    ]
    restart_transition_ok = any(
        older[3] < newer[1] <= newer[2]
        and older[7] is not None
        and older[7] < newer[1]
        and int(older[4]) == 3
        and int(newer[4]) == 3
        and int(older[6]) == 3
        and int(newer[6]) == 3
        for older, newer in zip(runtime_rows, runtime_rows[1:])
    )
    newest_runtime_at_end = bool(
        runtime_rows
        and runtime_rows[-1][3] >= generated_at - timedelta(seconds=90)
        and runtime_rows[-1][7] is not None
        and runtime_rows[-1][7] >= generated_at - timedelta(seconds=90)
    )
    latest_outputs_from_new_runtime = bool(
        runtime_rows
        and power
        and state
        and power[9] == runtime_rows[-1][0]
        and state[8] == runtime_rows[-1][0]
        and fault_runtime_instance_id == runtime_rows[-1][0]
    )
    runtime_ids = {row[0] for row in runtime_rows}
    healthy_runtime_ids = {row[0] for row in health_rows}
    health_details = []
    for index, runtime in enumerate(runtime_rows):
        interval_start = (
            window_started_at if index == 0 else runtime[1]
        )
        interval_end = (
            runtime_rows[index + 1][1]
            if index + 1 < len(runtime_rows) else generated_at
        )
        samples = [
            row for row in health_rows
            if row[0] == runtime[0]
            and interval_start <= row[1] <= interval_end
        ]
        times = [row[1] for row in samples]
        maximum_gap = max(
            (
                (right - left).total_seconds()
                for left, right in zip(times, times[1:])
            ),
            default=float("inf"),
        )
        old_runtime_continued = any(
            row[0] == runtime[0] and row[1] > interval_end
            for row in health_rows
        ) if index + 1 < len(runtime_rows) else False
        interval_ok = bool(
            samples
            and all(
                row[2] and row[3]
                and row[4] is not None
                and abs((row[1] - row[4]).total_seconds()) <= 90
                for row in samples
            )
            and times[0] <= interval_start + timedelta(seconds=90)
            and times[-1] >= interval_end - timedelta(seconds=90)
            and maximum_gap <= 90
            and not old_runtime_continued
        )
        health_details.append({
            "runtime": str(runtime[0]),
            "interval_start": interval_start.isoformat(),
            "interval_end": interval_end.isoformat(),
            "samples": len(samples),
            "maximum_gap_seconds": (
                maximum_gap if maximum_gap != float("inf") else -1.0
            ),
            "old_runtime_continued": old_runtime_continued,
            "passed": interval_ok,
        })
    health_window_ok = bool(
        runtime_rows
        and healthy_runtime_ids == runtime_ids
        and all(item["passed"] for item in health_details)
    )
    health_max_gap = max(
        (float(item["maximum_gap_seconds"]) for item in health_details),
        default=-1.0,
    )
    continuity_details = [
        {
            "definition": row[0],
            "entity": str(row[1]),
            "samples": len(row[2]),
            "first_at": row[2][0].isoformat() if row[2] else None,
            "last_at": row[2][-1].isoformat() if row[2] else None,
            "max_gap_seconds": max(
                (
                    (right - left).total_seconds()
                    for left, right in zip(row[2], row[2][1:])
                ),
                default=-1.0,
            ),
        }
        for row in continuity_rows
    ]
    ws_entity_details = [
        {
            "entity": str(entity_id),
            "receipts": len(times),
            "first_at": times[0].isoformat() if times else None,
            "last_at": times[-1].isoformat() if times else None,
            "max_gap_seconds": max(
                (
                    (right - left).total_seconds()
                    for left, right in zip(times, times[1:])
                ),
                default=-1.0,
            ),
        }
        for entity_id, times in ws_entity_rows
    ]
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
            "EN9_L2_CONTINUOUS_HISTORY",
            l2_latest_count == 3 and outbox_count == 3 and continuity_ok,
            {
                "latest": l2_latest_count,
                "history_entities": l2_history_count,
                "outbox": outbox_count,
                "minimum_samples": min(continuity_counts, default=0),
                "maximum_gap_seconds": (
                    continuity_max_gap
                    if continuity_max_gap != float("inf") else -1.0
                ),
                "entities_json": json.dumps(
                    continuity_details,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "revision_id": str(revision_id),
                "site_configuration_version": int(site_version),
                "producing_runtime": str(fault_runtime_instance_id),
            },
        ),
        AcceptanceCheck(
            "EN9_FAULT_SOURCE_EVIDENCE",
            fault_source_count == 88 and fault_total_source_count == 88,
            {
                "sources": fault_source_count,
                "total_sources": fault_total_source_count,
                "revision_id": str(revision_id),
                "site_configuration_version": int(site_version),
            },
        ),
        AcceptanceCheck(
            "EN9_POWER_VALUE",
            power_value_ok,
            {
                "matches_transform": power_value_ok,
                "tolerance": power_tolerance,
                "l2_source_digest": str(power[6]) if power else "",
                "l0_source_digest": str(power[7]) if power else "",
                "event_id": str(power[8]) if power else "",
                "producing_runtime": str(power[9]) if power else "",
            },
        ),
        AcceptanceCheck(
            "EN9_STATE_ENUM",
            state_value_ok,
            {
                "raw_mapping_matches": state_value_ok,
                "l2_source_digest": str(state[5]) if state else "",
                "l0_source_digest": str(state[6]) if state else "",
                "event_id": str(state[7]) if state else "",
                "producing_runtime": str(state[8]) if state else "",
            },
        ),
        AcceptanceCheck(
            "EN9_AUTHENTICATED_WS_RECEIPTS",
            ws_receipt_count >= int(observed_for_seconds // 60) * 3
            and ws_entity_count == 3
            and ws_user_count >= 1
            and ws_session_count >= 1
            and ws_window_ok
            and ws_entity_continuity_ok,
            # ACK evidence must itself remain continuous; endpoint totals are
            # insufficient because a client could reconnect only twice.
            {
                "receipts": ws_receipt_count,
                "entities": ws_entity_count,
                "authenticated_users": ws_user_count,
                "authenticated_sessions": ws_session_count,
                "window_covered": ws_window_ok,
                "per_entity_continuous": ws_entity_continuity_ok,
                "maximum_gap_seconds": (
                    ws_entity_max_gap
                    if ws_entity_max_gap != float("inf") else -1.0
                ),
                "entities_json": json.dumps(
                    ws_entity_details,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ),
        AcceptanceCheck(
            "EN9_RESTART_CONTINUITY",
            bool(current)
            and int(site_version) == int(current_site_version)
            and applied_at <= window_started_at
            and ws_runtime_count >= 2
            and restart_transition_ok
            and newest_runtime_at_end
            and latest_outputs_from_new_runtime
            and ingestion_failure_count == 0,
            {
                "current": bool(current),
                "site_version": int(site_version),
                "runtime_instances": ws_runtime_count,
                "ordered_restart_transition": restart_transition_ok,
                "new_runtime_at_window_end": newest_runtime_at_end,
                "latest_outputs_from_new_runtime": latest_outputs_from_new_runtime,
                "runtimes_json": json.dumps(
                    runtime_details,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "window_ingestion_failures": ingestion_failure_count,
            },
        ),
        AcceptanceCheck(
            "EN9_RUNTIME_MQTT_COLLECTION_HEALTH",
            health_window_ok,
            {
                "samples": len(health_rows),
                "runtime_instances": len(healthy_runtime_ids),
                "maximum_gap_seconds": (
                    health_max_gap
                    if health_max_gap != float("inf") else -1.0
                ),
                "window_covered": health_window_ok,
                "runtime_intervals_json": json.dumps(
                    health_details,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ),
    )
    evidence = {
        "application_id": str(application_id),
        "required_input_count": int(input_count),
        "output_entity_count": int(output_count),
        "observed_for_seconds": float(observed_for_seconds),
        "generated_at": generated_at.isoformat(),
        "checks": [
            {"code": item.code, "passed": item.passed, "evidence": dict(item.evidence)}
            for item in checks
        ],
    }
    digest = hashlib.sha256(json.dumps(
        evidence,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    report = AcceptanceReport(
        id=uuid5(NAMESPACE_URL, f"zizu/en9-acceptance/{application_id}/{digest}"),
        application_id=application_id,
        required_input_count=int(input_count),
        output_entity_count=int(output_count),
        observed_for_seconds=float(observed_for_seconds),
        passed=all(item.passed for item in checks),
        checks=checks,
        generated_at=generated_at,
        digest=digest,
    )
    with connection_factory() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_en9_acceptance_reports
                      (id, application_id, status, observed_for_seconds,
                       generated_at, evidence, digest)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (application_id, digest) DO NOTHING
                    """,
                    (
                        str(report.id), str(application_id),
                        "passed" if report.passed else "failed",
                        observed_for_seconds, generated_at, Json(evidence), digest,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return report


def get_en9_acceptance_report(
    report_id: UUID,
    *,
    connection_factory: ConnectionFactory | None = None,
) -> dict[str, object]:
    if connection_factory is None:
        from app.services.telemetry_store import get_connection

        connection_factory = get_connection
    with connection_factory() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, application_id, status, observed_for_seconds,
                       generated_at, evidence, digest
                FROM t_en9_acceptance_reports WHERE id = %s
                """,
                (str(report_id),),
            )
            row = cursor.fetchone()
    if row is None:
        raise ValueError("EN9_ACCEPTANCE_REPORT_NOT_FOUND")
    return {
        "id": str(row[0]),
        "application_id": str(row[1]),
        "passed": row[2] == "passed",
        "observed_for_seconds": float(row[3]),
        "generated_at": row[4].isoformat(),
        "digest": row[6].strip(),
        **dict(row[5]),
    }


def get_latest_en9_acceptance_state(
    node_id: UUID,
    *,
    connection_factory: ConnectionFactory | None = None,
) -> dict[str, object]:
    if connection_factory is None:
        from app.services.telemetry_store import get_connection

        connection_factory = get_connection
    with connection_factory() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT application.id, application.plan_id,
                       application.installed_processing_id,
                       application.solution_installation_id,
                       installed.revision_id,
                       application.site_configuration_version,
                       application.output_entity_instance_ids,
                       report.id
                FROM t_point_processing_applications AS application
                JOIN t_installed_point_processings AS installed
                  ON installed.id = application.installed_processing_id
                LEFT JOIN LATERAL (
                  SELECT id
                  FROM t_en9_acceptance_reports
                  WHERE application_id = application.id
                  ORDER BY created_at DESC, id DESC
                  LIMIT 1
                ) AS report ON TRUE
                WHERE installed.node_id = %s AND installed.current = TRUE
                ORDER BY application.applied_at DESC, application.id DESC
                LIMIT 1
                """,
                (str(node_id),),
            )
            row = cursor.fetchone()
    if row is None:
        return {"application": None, "latest_report": None}
    application = {
        "id": str(row[0]),
        "plan_id": str(row[1]),
        "installed_processing_id": str(row[2]),
        "solution_installation_id": str(row[3]),
        "revision_id": str(row[4]),
        "site_configuration_version": int(row[5]),
        "output_entity_instance_ids": [str(item) for item in row[6]],
    }
    report = (
        get_en9_acceptance_report(
            UUID(str(row[7])),
            connection_factory=connection_factory,
        )
        if row[7] is not None else None
    )
    return {"application": application, "latest_report": report}
