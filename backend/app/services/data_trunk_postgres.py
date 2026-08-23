"""L0、L2、latest、source 与 outbox 的单事务 PostgreSQL adapter。"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg2.extras import Json

from app.services.data_trunk import ConversionEvaluator, DataTrunk
from app.services.data_trunk_contracts import (
    BooleanCodeInput,
    BooleanSetTransform,
    CommitReceipt,
    DataTrunkError,
    EnumTransform,
    FaultCodeTransform,
    InputReference,
    InstalledPointProcessing,
    L2Observation,
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.runtime_identity import RUNTIME_INSTANCE_ID
ConnectionFactory = Callable[[], AbstractContextManager[Any]]
FaultHook = Callable[[str], None]


def verify_data_trunk_contract_gate(
    connection_factory: ConnectionFactory | None = None,
) -> int:
    """Fail startup when an L2 entity no longer has exactly one source."""
    if connection_factory is None:
        from app.services.telemetry_store import get_connection

        connection_factory = get_connection
    with connection_factory() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DO $$
                BEGIN
                  IF to_regclass('public.t_point_processing_expressions') IS NULL
                     OR to_regclass('public.t_point_processing_selectors') IS NULL
                     OR to_regclass('public.t_point_processing_selector_members') IS NULL
                     OR to_regclass('public.t_point_processing_dependencies') IS NULL THEN
                    RAISE EXCEPTION 'schema 042 point-processing contract is incomplete'
                      USING ERRCODE = '55000';
                  END IF;
                END;
                $$
                """
            )
            cursor.execute(
                """
                SELECT assert_entity_instance_single_source(id)
                FROM t_entity_instances
                ORDER BY id
                """
            )
            return len(cursor.fetchall())


@dataclass(frozen=True)
class _ConversionSnapshot:
    installed: tuple[InstalledPointProcessing, ...]
    site_configuration_version: int
    current_inputs: Mapping[InputReference, RawObservation]


@dataclass(frozen=True)
class _FreshnessCandidate:
    observation: L2Observation
    source_event_id: UUID
    source_observed_at: datetime
    source_digest: str


class PostgresDataTrunkRepository:
    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        fault_hook: FaultHook | None = None,
        state_heartbeat_seconds: float = 60.0,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault_hook = fault_hook or (lambda _stage: None)
        if state_heartbeat_seconds <= 0:
            raise ValueError("state heartbeat must be positive")
        self._state_heartbeat_seconds = state_heartbeat_seconds

    def transact(
        self,
        raw_observations: tuple[RawObservation, ...],
        evaluator: ConversionEvaluator,
    ) -> CommitReceipt:
        transaction_id = uuid4()
        accepted: tuple[RawObservation, ...] = ()
        produced: tuple[L2Observation, ...] = ()
        late_l0 = 0
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        accepted = self._insert_l0(cursor, raw_observations)
                        calculated_at = self._clock()
                        accepted = self._derive_l0_freshness(
                            cursor,
                            accepted,
                            calculated_at=calculated_at,
                        )
                        advanced_l0, late_l0 = self._advance_l0_latest(
                            cursor,
                            accepted,
                        )
                        if accepted:
                            snapshot = self._load_conversion_snapshot(
                                cursor,
                                accepted,
                                calculated_at=calculated_at,
                            )
                            produced = self._evaluate_batch(
                                snapshot,
                                accepted,
                                evaluator,
                                advanced_observations=advanced_l0,
                                calculated_at=calculated_at,
                            )
                            produced = self._select_history_observations(
                                cursor,
                                produced,
                            )
                            self._ensure_runtime(cursor)
                            self._insert_l2(cursor, produced)
                            advanced = self._advance_l2_latest(cursor, produced)
                            self._insert_sources(cursor, produced)
                            self._fault_hook("source")
                            self._insert_outbox(cursor, advanced)
                            self._fault_hook("outbox")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_TRUNK_UNAVAILABLE",
                "DATA_TRUNK_UNAVAILABLE",
            ) from exc

        return CommitReceipt(
            transaction_id=transaction_id,
            accepted_l0_count=len(accepted),
            duplicate_l0_count=len(raw_observations) - len(accepted),
            l2_event_ids=tuple(item.event_id for item in produced),
            late_observation_count=late_l0,
            accepted_l0_observation_ids=tuple(
                item.observation_id for item in accepted
            ),
        )

    def record_failure(
        self,
        raw_observations: tuple[RawObservation, ...],
        *,
        attempts: int,
        error_code: str,
    ) -> UUID:
        source_digest = hashlib.sha256(
            "\n".join(sorted(item.source_digest for item in raw_observations)).encode(
                "ascii"
            )
        ).hexdigest()
        safe_code = (
            error_code
            if error_code.isascii()
            and error_code.replace("_", "").isalnum()
            and error_code.upper() == error_code
            and len(error_code) <= 64
            else "DATA_TRUNK_WRITE_FAILED"
        )
        failure_id = uuid5(
            NAMESPACE_URL,
            f"zizu:ingestion-failure:{source_digest}:{attempts}:{safe_code}",
        )
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO t_ingestion_failures
                              (id, source_digest, stage, safe_summary, attempts)
                            VALUES (%s, %s, 'l0', %s, %s)
                            ON CONFLICT (id) DO NOTHING
                            """,
                            (
                                str(failure_id),
                                source_digest,
                                Json(
                                    {
                                        "code": safe_code,
                                        "observation_count": len(raw_observations),
                                    }
                                ),
                                attempts,
                            ),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_TRUNK_UNAVAILABLE",
                "DATA_TRUNK_UNAVAILABLE",
            ) from exc
        return failure_id

    def acceptance_evidence(
        self,
        *,
        solution_installation_id: UUID,
        entity_definition_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        required = tuple(sorted(set(entity_definition_ids)))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT output.entity_definition_id,
                                    binding.entity_instance_id,
                                    installed.revision_id,
                                    installed.site_configuration_version
                    FROM t_installed_point_processings AS installed
                    JOIN t_point_processing_output_bindings AS binding
                      ON binding.installed_processing_id = installed.id
                    JOIN t_point_processing_outputs AS output
                      ON output.id = binding.output_id
                    WHERE installed.solution_installation_id = %s
                      AND output.entity_definition_id = ANY(%s)
                    ORDER BY output.entity_definition_id, binding.entity_instance_id
                    """,
                    (str(solution_installation_id), list(required)),
                )
                bindings = cursor.fetchall()
                entity_ids = tuple(sorted({row[1] for row in bindings}, key=str))
                if entity_ids:
                    cursor.execute(
                        """
                        SELECT count(*),
                               count(DISTINCT observation.event_id),
                               count(DISTINCT source.l0_observation_id),
                               count(DISTINCT observation.site_configuration_version),
                               count(DISTINCT outbox.event_id)
                        FROM t_l2_observations AS observation
                        LEFT JOIN t_l2_observation_sources AS source
                          ON source.l2_event_id = observation.event_id
                         AND source.l2_observed_at = observation.observed_at
                         AND source.source_kind = 'l0'
                        LEFT JOIN t_l2_stream_outbox AS outbox
                          ON outbox.event_id = observation.event_id
                        WHERE observation.entity_instance_id = ANY(%s)
                        """,
                        (list(entity_ids),),
                    )
                    (
                        l2_count,
                        committed_count,
                        source_count,
                        _site_version_count,
                        outbox_count,
                    ) = cursor.fetchone()
                    cursor.execute(
                        """
                        SELECT count(DISTINCT latest.entity_instance_id),
                               count(DISTINCT latest.entity_instance_id)
                                 FILTER (WHERE latest.quality = %s),
                               count(DISTINCT latest.entity_instance_id)
                                 FILTER (
                                   WHERE latest.observed_at <= latest.received_at
                                     AND latest.received_at <= latest.calculated_at
                                 )
                        FROM t_l2_latest AS latest
                        WHERE latest.entity_instance_id = ANY(%s)
                        """,
                        (int(TrunkQuality.GOOD), list(entity_ids)),
                    )
                    (
                        latest_count,
                        good_latest_count,
                        ordered_timestamp_count,
                    ) = cursor.fetchone()
                else:
                    l2_count = committed_count = source_count = outbox_count = 0
                    latest_count = good_latest_count = ordered_timestamp_count = 0
        return {
            "required_entity_definitions": list(required),
            "observed_entity_definitions": sorted({row[0] for row in bindings}),
            "entity_instance_ids": [str(item) for item in entity_ids],
            "processing_revision_ids": sorted({str(row[2]) for row in bindings}),
            "site_configuration_versions": sorted({int(row[3]) for row in bindings}),
            "l0_observation_count": int(source_count),
            "l2_observation_count": int(l2_count),
            "l2_latest_count": int(latest_count),
            "source_observation_count": int(source_count),
            "committed_event_count": int(committed_count),
            "outbox_event_count": int(outbox_count),
            "good_latest_count": int(good_latest_count),
            "ordered_timestamp_count": int(ordered_timestamp_count),
        }

    def mark_expired_outputs_stale(self, now: datetime) -> int:
        try:
            with self._connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        candidates = self._load_expired_outputs(cursor, now)
                        observations = tuple(
                            item.observation for item in candidates
                        )
                        self._ensure_runtime(cursor)
                        self._insert_l2(cursor, observations)
                        advanced = self._advance_l2_latest(cursor, observations)
                        advanced_ids = {item.event_id for item in advanced}
                        self._insert_freshness_sources(
                            cursor,
                            tuple(
                                item
                                for item in candidates
                                if item.observation.event_id in advanced_ids
                            ),
                        )
                        self._fault_hook("source")
                        self._insert_outbox(cursor, advanced)
                        self._fault_hook("outbox")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except DataTrunkError:
            raise
        except Exception as exc:
            raise DataTrunkError(
                "DATA_TRUNK_UNAVAILABLE",
                "DATA_TRUNK_UNAVAILABLE",
            ) from exc
        return len(advanced)
    @staticmethod
    def _load_expired_outputs(
        cursor,
        now: datetime,
    ) -> tuple[_FreshnessCandidate, ...]:
        cursor.execute(
            """
            SELECT
              latest.entity_instance_id,
              latest.event_id,
              latest.observed_at,
              latest.source_digest,
              latest.processing_revision_id,
              latest.site_configuration_version,
              output.entity_definition_id,
              output.data_type,
              output.unit,
              latest.observed_at
                + output.freshness_seconds * INTERVAL '1 second'
                AS freshness_deadline
            FROM t_l2_latest AS latest
            JOIN t_point_processing_output_bindings AS output_binding
              ON output_binding.entity_instance_id = latest.entity_instance_id
            JOIN t_installed_point_processings AS installed
              ON installed.id = output_binding.installed_processing_id
             AND installed.current = TRUE
            JOIN t_point_processing_outputs AS output
              ON output.id = output_binding.output_id
            WHERE latest.quality <> %s
              AND latest.observed_at
                + output.freshness_seconds * INTERVAL '1 second' <= %s
            ORDER BY latest.entity_instance_id
            FOR UPDATE OF latest SKIP LOCKED
            """,
            (int(TrunkQuality.STALE), now),
        )
        candidates: list[_FreshnessCandidate] = []
        for row in cursor.fetchall():
            (
                entity_instance_id,
                source_event_id,
                source_observed_at,
                source_digest,
                revision_id,
                site_configuration_version,
                definition_id,
                output_type,
                output_unit,
                deadline,
            ) = row
            freshness_material = "|".join(
                (
                    str(entity_instance_id),
                    str(source_event_id),
                    deadline.isoformat(),
                )
            )
            freshness_digest = hashlib.sha256(
                freshness_material.encode("utf-8")
            ).hexdigest()
            observation = L2Observation(
                event_id=uuid5(
                    NAMESPACE_URL,
                    f"data-trunk-freshness|{freshness_material}",
                ),
                entity_instance_id=UUID(str(entity_instance_id)),
                definition_id=definition_id,
                value=TypedValue(ValueKind(output_type), None),
                unit=output_unit,
                quality=TrunkQuality.STALE,
                reason="FRESHNESS_EXPIRED",
                observed_at=deadline,
                received_at=now,
                calculated_at=now,
                processing_revision_id=UUID(str(revision_id)),
                site_configuration_version=site_configuration_version,
                source_observation_ids=(),
                source_digest=freshness_digest,
                source_order_key=f"A:{deadline.isoformat()}:{freshness_digest}",
            )
            candidates.append(
                _FreshnessCandidate(
                    observation=observation,
                    source_event_id=UUID(str(source_event_id)),
                    source_observed_at=source_observed_at,
                    source_digest=source_digest,
                )
            )
        return tuple(candidates)

    @staticmethod
    def _insert_freshness_sources(
        cursor,
        candidates: tuple[_FreshnessCandidate, ...],
    ) -> None:
        for candidate in candidates:
            cursor.execute(
                """
                INSERT INTO t_l2_observation_sources
                  (l2_event_id, l2_observed_at, source_kind,
                   source_l2_event_id, source_l2_observed_at, source_digest)
                VALUES (%s, %s, 'freshness', %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    str(candidate.observation.event_id),
                    candidate.observation.observed_at,
                    str(candidate.source_event_id),
                    candidate.source_observed_at,
                    candidate.source_digest,
                ),
            )

    @staticmethod
    def _insert_l0(cursor, observations: tuple[RawObservation, ...]) -> tuple[RawObservation, ...]:
        accepted: list[RawObservation] = []
        for observation in observations:
            cursor.execute(
                """
                INSERT INTO t_l0_observation_dedup
                  (observation_id, tag_id, observed_at, source_digest,
                   source_message_id, source_sequence)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_digest) DO NOTHING
                RETURNING observation_id
                """,
                (
                    str(observation.observation_id),
                    str(observation.tag_id),
                    observation.source_timestamp,
                    observation.source_digest,
                    observation.source_message_id,
                    observation.source_sequence,
                ),
            )
            if cursor.fetchone() is None:
                continue
            compatibility, raw = _raw_columns(observation.value)
            cursor.execute(
                """
                INSERT INTO t_telemetry
                  (ts, node_id, tag_id,
                   value_float, value_int, value_bool, value_str,
                   is_virtual, quality, observation_id, source_message_id,
                   source_sequence, source_digest, raw_unit,
                   raw_value_float, raw_value_int, raw_value_bool,
                   raw_value_text)
                VALUES (
                  %s, %s, %s,
                  %s, %s, %s, %s,
                  FALSE, %s, %s, %s,
                  %s, %s, %s,
                  %s, %s, %s, %s
                )
                """,
                (
                    observation.source_timestamp,
                    str(observation.node_id),
                    str(observation.tag_id),
                    *compatibility,
                    int(observation.quality),
                    str(observation.observation_id),
                    observation.source_message_id,
                    observation.source_sequence,
                    observation.source_digest,
                    observation.raw_unit,
                    *raw,
                ),
            )
            accepted.append(observation)
        return tuple(accepted)

    @staticmethod
    def _advance_l0_latest(
        cursor,
        observations: tuple[RawObservation, ...],
    ) -> tuple[tuple[RawObservation, ...], int]:
        late = 0
        advanced: list[RawObservation] = []
        for observation in observations:
            compatibility, raw = _raw_columns(observation.value)
            order_key = _raw_order_key(observation)
            cursor.execute(
                """
                INSERT INTO t_telemetry_latest
                  (node_id, tag_id, ts,
                   value_float, value_int, value_bool, value_str,
                   is_virtual, quality, updated_at, observation_id,
                   source_message_id, source_sequence, source_digest,
                   source_order_key, raw_unit, raw_value_float,
                   raw_value_int, raw_value_bool, raw_value_text)
                VALUES (
                  %s, %s, %s,
                  %s, %s, %s, %s,
                  FALSE, %s, now(), %s,
                  %s, %s, %s,
                  %s, %s, %s,
                  %s, %s, %s
                )
                ON CONFLICT (node_id, tag_id) DO UPDATE SET
                  ts = EXCLUDED.ts,
                  value_float = EXCLUDED.value_float,
                  value_int = EXCLUDED.value_int,
                  value_bool = EXCLUDED.value_bool,
                  value_str = EXCLUDED.value_str,
                  is_virtual = EXCLUDED.is_virtual,
                  quality = EXCLUDED.quality,
                  updated_at = now(),
                  observation_id = EXCLUDED.observation_id,
                  source_message_id = EXCLUDED.source_message_id,
                  source_sequence = EXCLUDED.source_sequence,
                  source_digest = EXCLUDED.source_digest,
                  source_order_key = EXCLUDED.source_order_key,
                  raw_unit = EXCLUDED.raw_unit,
                  raw_value_float = EXCLUDED.raw_value_float,
                  raw_value_int = EXCLUDED.raw_value_int,
                  raw_value_bool = EXCLUDED.raw_value_bool,
                  raw_value_text = EXCLUDED.raw_value_text
                WHERE
                  EXCLUDED.ts > t_telemetry_latest.ts
                  OR (
                    EXCLUDED.ts = t_telemetry_latest.ts
                    AND EXCLUDED.source_order_key
                      > COALESCE(t_telemetry_latest.source_order_key, '')
                  )
                RETURNING observation_id
                """,
                (
                    str(observation.node_id),
                    str(observation.tag_id),
                    observation.source_timestamp,
                    *compatibility,
                    int(observation.quality),
                    str(observation.observation_id),
                    observation.source_message_id,
                    observation.source_sequence,
                    observation.source_digest,
                    order_key,
                    observation.raw_unit,
                    *raw,
                ),
            )
            if cursor.fetchone() is None:
                late += 1
            else:
                advanced.append(observation)
        return tuple(advanced), late

    @staticmethod
    def _derive_l0_freshness(
        cursor,
        observations: tuple[RawObservation, ...],
        *,
        calculated_at: datetime,
    ) -> tuple[RawObservation, ...]:
        if not observations:
            return ()
        cursor.execute(
            """
            SELECT id, freshness_seconds
            FROM t_tags
            WHERE id = ANY(%s::uuid[])
            """,
            ([str(item.tag_id) for item in observations],),
        )
        freshness_by_tag = {
            UUID(str(tag_id)): freshness_seconds
            for tag_id, freshness_seconds in cursor.fetchall()
        }
        return tuple(
            replace(
                observation,
                quality=min(observation.quality, TrunkQuality.STALE),
            )
            if (
                freshness_by_tag.get(observation.tag_id) is not None
                and observation.source_timestamp
                + timedelta(
                    seconds=float(freshness_by_tag[observation.tag_id])
                )
                <= calculated_at
            )
            else observation
            for observation in observations
        )

    @staticmethod
    def _load_conversion_snapshot(
        cursor,
        observations: tuple[RawObservation, ...],
        *,
        calculated_at: datetime,
    ) -> _ConversionSnapshot:
        tag_ids = tuple(sorted({item.tag_id for item in observations}, key=str))

        cursor.execute(
            """
            SELECT
              installed.id,
              installed.revision_id,
              input_binding.l0_tag_id,
              output_binding.entity_instance_id,
              output.id,
              output.entity_definition_id,
              output.data_type,
              output.unit,
              output.freshness_seconds,
              input.unit,
              numeric.scale,
              numeric."offset",
              numeric.minimum,
              numeric.maximum,
              enum_rule.output_id IS NOT NULL,
              fault_rule.delimiter
            FROM t_installed_point_processings AS installed
            JOIN t_point_processing_outputs AS output
              ON output.revision_id = installed.revision_id
            JOIN t_point_processing_output_bindings AS output_binding
              ON output_binding.installed_processing_id = installed.id
             AND output_binding.output_id = output.id
            LEFT JOIN t_numeric_transform_rules AS numeric
              ON numeric.output_id = output.id
            LEFT JOIN t_enum_transform_rules AS enum_rule
              ON enum_rule.output_id = output.id
            LEFT JOIN t_fault_code_transform_rules AS fault_rule
              ON fault_rule.output_id = output.id
            JOIN t_point_processing_inputs AS input
              ON input.id = COALESCE(
                numeric.input_id,
                enum_rule.input_id,
                fault_rule.input_id
              )
            JOIN t_point_processing_input_bindings AS input_binding
              ON input_binding.installed_processing_id = installed.id
             AND input_binding.input_id = input.id
             AND input_binding.source_kind = 'l0'
            WHERE installed.current = TRUE
              AND input_binding.l0_tag_id = ANY(%s::uuid[])
              AND num_nonnulls(
                numeric.output_id,
                enum_rule.output_id,
                fault_rule.output_id
              ) = 1
            ORDER BY output_binding.entity_instance_id, installed.id
            """,
            ([str(tag_id) for tag_id in tag_ids],),
        )
        installed_items: list[InstalledPointProcessing] = []
        rows = cursor.fetchall()
        for row in rows:
            (
                installation_id,
                revision_id,
                input_tag_id,
                entity_instance_id,
                output_id,
                definition_id,
                output_type,
                output_unit,
                freshness_seconds,
                input_unit,
                scale,
                offset,
                minimum,
                maximum,
                has_enum_rule,
                fault_delimiter,
            ) = row
            input_reference = InputReference.l0(UUID(str(input_tag_id)))
            if scale is not None:
                if output_type != ValueKind.FLOAT.value:
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "numeric transform output must be FLOAT",
                    )
                item = InstalledPointProcessing.numeric(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    input_tag_id=UUID(str(input_tag_id)),
                    output_entity_instance_id=UUID(str(entity_instance_id)),
                    output_definition_id=definition_id,
                    scale=scale,
                    offset=offset,
                    input_unit=input_unit,
                    output_unit=output_unit,
                    minimum=minimum,
                    maximum=maximum,
                )
            elif has_enum_rule:
                if output_type != ValueKind.ENUM.value:
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "enum transform output must be ENUM",
                    )
                cursor.execute(
                    """
                    SELECT raw_value, canonical_value
                    FROM t_enum_mapping_entries
                    WHERE output_id = %s
                    ORDER BY raw_value
                    """,
                    (str(output_id),),
                )
                item = InstalledPointProcessing(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    entity_instance_id=UUID(str(entity_instance_id)),
                    entity_definition_id=definition_id,
                    output_kind=ValueKind.ENUM,
                    output_unit=output_unit,
                    freshness_seconds=freshness_seconds,
                    transform=EnumTransform(
                        input=input_reference,
                        entries=dict(cursor.fetchall()),
                    ),
                )
            else:
                if (
                    output_type != ValueKind.CODE_SET.value
                    or fault_delimiter is None
                ):
                    raise DataTrunkError(
                        "POINT_PROCESSING_CONFIGURATION_INVALID",
                        "fault-code transform output must be CODE_SET",
                    )
                cursor.execute(
                    """
                    SELECT raw_code, canonical_code
                    FROM t_fault_code_mapping_entries
                    WHERE output_id = %s
                    ORDER BY raw_code
                    """,
                    (str(output_id),),
                )
                item = InstalledPointProcessing(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    entity_instance_id=UUID(str(entity_instance_id)),
                    entity_definition_id=definition_id,
                    output_kind=ValueKind.CODE_SET,
                    output_unit=output_unit,
                    freshness_seconds=freshness_seconds,
                    transform=FaultCodeTransform(
                        input=input_reference,
                        delimiter=fault_delimiter,
                        entries=dict(cursor.fetchall()),
                    ),
                )
            installed_items.append(
                replace(item, freshness_seconds=freshness_seconds)
            )

        cursor.execute(
            """
            SELECT installed.id, installed.revision_id,
                   output_binding.entity_instance_id, output.id,
                   output.entity_definition_id, output.data_type,
                   output.unit, output.freshness_seconds
            FROM t_installed_point_processings AS installed
            JOIN t_point_processing_outputs AS output
              ON output.revision_id = installed.revision_id
            JOIN t_point_processing_output_bindings AS output_binding
              ON output_binding.installed_processing_id = installed.id
             AND output_binding.output_id = output.id
            JOIN t_boolean_set_transform_rules AS boolean_rule
              ON boolean_rule.output_id = output.id
            WHERE installed.current = TRUE
              AND EXISTS (
                SELECT 1
                FROM t_boolean_set_mapping_entries AS entry
                JOIN t_point_processing_input_bindings AS input_binding
                  ON input_binding.installed_processing_id = installed.id
                 AND input_binding.input_id = entry.input_id
                 AND input_binding.source_kind = 'l0'
                WHERE entry.output_id = output.id
                  AND input_binding.l0_tag_id = ANY(%s::uuid[])
              )
            ORDER BY output_binding.entity_instance_id, installed.id
            """,
            ([str(tag_id) for tag_id in tag_ids],),
        )
        boolean_tag_ids: set[UUID] = set()
        for (
            installation_id,
            revision_id,
            entity_instance_id,
            output_id,
            definition_id,
            output_type,
            output_unit,
            freshness_seconds,
        ) in cursor.fetchall():
            if output_type != ValueKind.CODE_SET.value:
                raise DataTrunkError(
                    "POINT_PROCESSING_CONFIGURATION_INVALID",
                    "boolean-set transform output must be CODE_SET",
                )
            cursor.execute(
                """
                SELECT input_binding.l0_tag_id, entry.canonical_code
                FROM t_boolean_set_mapping_entries AS entry
                JOIN t_point_processing_input_bindings AS input_binding
                  ON input_binding.installed_processing_id = %s
                 AND input_binding.input_id = entry.input_id
                 AND input_binding.source_kind = 'l0'
                WHERE entry.output_id = %s
                ORDER BY entry.canonical_code
                """,
                (str(installation_id), str(output_id)),
            )
            boolean_inputs = tuple(
                BooleanCodeInput(
                    input=InputReference.l0(UUID(str(input_tag_id))),
                    code=canonical_code,
                )
                for input_tag_id, canonical_code in cursor.fetchall()
            )
            boolean_tag_ids.update(item.input.source_id for item in boolean_inputs)
            installed_items.append(
                InstalledPointProcessing(
                    installation_id=UUID(str(installation_id)),
                    revision_id=UUID(str(revision_id)),
                    entity_instance_id=UUID(str(entity_instance_id)),
                    entity_definition_id=definition_id,
                    output_kind=ValueKind.CODE_SET,
                    output_unit=output_unit,
                    freshness_seconds=freshness_seconds,
                    transform=BooleanSetTransform(inputs=boolean_inputs),
                )
            )

        current_inputs: dict[InputReference, RawObservation] = {}
        if boolean_tag_ids:
            cursor.execute(
                """
                SELECT latest.node_id, latest.tag_id, tag.name,
                       latest.raw_value_bool, latest.raw_unit, latest.quality,
                       latest.ts, latest.updated_at, latest.observation_id,
                       latest.source_message_id, latest.source_sequence,
                       latest.source_digest, tag.freshness_seconds
                FROM t_telemetry_latest AS latest
                JOIN t_tags AS tag ON tag.id = latest.tag_id
                WHERE latest.tag_id = ANY(%s::uuid[])
                ORDER BY latest.tag_id
                """,
                ([str(tag_id) for tag_id in sorted(boolean_tag_ids, key=str)],),
            )
            for row in cursor.fetchall():
                (
                    node_id,
                    input_tag_id,
                    source_key,
                    raw_value_bool,
                    raw_unit,
                    quality,
                    source_timestamp,
                    received_at,
                    observation_id,
                    source_message_id,
                    source_sequence,
                    source_digest,
                    input_freshness_seconds,
                ) = row
                effective_quality = TrunkQuality(quality)
                if (
                    input_freshness_seconds is not None
                    and source_timestamp
                    + timedelta(seconds=float(input_freshness_seconds))
                    <= calculated_at
                ):
                    effective_quality = min(effective_quality, TrunkQuality.STALE)
                current_inputs[InputReference.l0(UUID(str(input_tag_id)))] = RawObservation(
                    observation_id=UUID(str(observation_id)),
                    node_id=UUID(str(node_id)),
                    tag_id=UUID(str(input_tag_id)),
                    source_key=source_key,
                    value=TypedValue(ValueKind.BOOL, raw_value_bool),
                    raw_unit=raw_unit,
                    quality=effective_quality,
                    source_timestamp=source_timestamp,
                    received_at=received_at,
                    source_message_id=source_message_id,
                    source_sequence=source_sequence,
                    source_digest=source_digest.strip(),
                )

        cursor.execute(
            """
            SELECT current_version
            FROM t_site_configuration_state
            WHERE singleton = TRUE
            """
        )
        site_row = cursor.fetchone()
        if site_row is None:
            raise DataTrunkError(
                "POINT_PROCESSING_CONFIGURATION_INVALID",
                "site configuration state is unavailable",
            )
        return _ConversionSnapshot(
            installed=tuple(installed_items),
            site_configuration_version=site_row[0],
            current_inputs=current_inputs,
        )

    @staticmethod
    def _evaluate_batch(
        snapshot: _ConversionSnapshot,
        observations: tuple[RawObservation, ...],
        evaluator: ConversionEvaluator,
        *,
        advanced_observations: tuple[RawObservation, ...],
        calculated_at: datetime,
    ) -> tuple[L2Observation, ...]:
        produced: list[L2Observation] = []
        advanced_inputs = {
            InputReference.l0(observation.tag_id): observation
            for observation in advanced_observations
        }
        for observation in sorted(
            observations,
            key=lambda item: (
                item.source_timestamp,
                _raw_order_key(item),
                str(item.tag_id),
            ),
        ):
            input_reference = InputReference.l0(observation.tag_id)
            affected = tuple(
                item
                for item in snapshot.installed
                if not isinstance(item.transform, BooleanSetTransform)
                and item.transform.input == input_reference
            )
            if not affected:
                continue
            produced.extend(
                evaluator(
                    installed=affected,
                    current_inputs={input_reference: observation},
                    site_configuration_version=(
                        snapshot.site_configuration_version
                    ),
                    calculated_at=calculated_at,
                )
            )
        boolean_affected = tuple(
            item for item in snapshot.installed
            if isinstance(item.transform, BooleanSetTransform)
            and any(entry.input in advanced_inputs for entry in item.transform.inputs)
        )
        if boolean_affected:
            produced.extend(
                evaluator(
                    installed=boolean_affected,
                    current_inputs=snapshot.current_inputs,
                    site_configuration_version=snapshot.site_configuration_version,
                    calculated_at=calculated_at,
                )
            )
        return tuple(produced)

    def _select_history_observations(
        self,
        cursor,
        observations: tuple[L2Observation, ...],
    ) -> tuple[L2Observation, ...]:
        """Keep numeric samples; deduplicate state until change or heartbeat."""
        selected: list[L2Observation] = []
        previous: dict[UUID, tuple[tuple[object, ...], int, datetime]] = {}
        for observation in observations:
            if observation.value.kind not in {ValueKind.ENUM, ValueKind.CODE_SET}:
                selected.append(observation)
                continue
            state = previous.get(observation.entity_instance_id)
            if state is None:
                cursor.execute(
                    """
                    SELECT value_float, value_int, value_bool, value_text,
                           value_codes, quality, observed_at
                    FROM t_l2_latest
                    WHERE entity_instance_id = %s
                    FOR UPDATE
                    """,
                    (str(observation.entity_instance_id),),
                )
                row = cursor.fetchone()
                if row is not None:
                    state = (
                        _normalized_l2_columns(row[:5]),
                        int(row[5]),
                        row[6],
                    )
            current = (
                _normalized_l2_columns(_l2_columns(observation.value)),
                int(observation.quality),
                observation.observed_at,
            )
            if state is None or (
                current[0] != state[0]
                or current[1] != state[1]
                or (
                    current[2] >= state[2]
                    and (current[2] - state[2]).total_seconds()
                    >= self._state_heartbeat_seconds
                )
            ):
                selected.append(observation)
                previous[observation.entity_instance_id] = current
            else:
                previous[observation.entity_instance_id] = state
        return tuple(selected)

    @staticmethod
    def _ensure_runtime(cursor) -> None:
        from app.api.health import _VERSION as platform_version

        cursor.execute(
            """
            INSERT INTO t_runtime_instances (id, started_at, platform_version)
            VALUES (%s, now(), %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (str(RUNTIME_INSTANCE_ID), platform_version),
        )

    @staticmethod
    def _insert_l2(cursor, observations: tuple[L2Observation, ...]) -> None:
        for observation in observations:
            values = _l2_columns(observation.value)
            cursor.execute(
                """
                INSERT INTO t_l2_observations
                  (observed_at, event_id, entity_instance_id,
                   received_at, calculated_at, value_float, value_int,
                   value_bool, value_text, value_codes, quality, reason,
                   processing_revision_id, site_configuration_version,
                   source_digest, source_order_key,
                   producing_runtime_instance_id)
                VALUES (
                  %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s,
                  %s, %s, %s
                )
                ON CONFLICT (event_id, observed_at) DO NOTHING
                """,
                (
                    observation.observed_at,
                    str(observation.event_id),
                    str(observation.entity_instance_id),
                    observation.received_at,
                    observation.calculated_at,
                    *values,
                    int(observation.quality),
                    observation.reason,
                    str(observation.processing_revision_id),
                    observation.site_configuration_version,
                    observation.source_digest,
                    observation.source_order_key,
                    str(RUNTIME_INSTANCE_ID),
                ),
            )

    @staticmethod
    def _advance_l2_latest(
        cursor,
        observations: tuple[L2Observation, ...],
    ) -> tuple[L2Observation, ...]:
        advanced: list[L2Observation] = []
        for observation in observations:
            values = _l2_columns(observation.value)
            cursor.execute(
                """
                INSERT INTO t_l2_latest
                  (entity_instance_id, event_id, observed_at,
                   received_at, calculated_at, value_float, value_int,
                   value_bool, value_text, value_codes, quality, reason,
                   processing_revision_id, site_configuration_version,
                   source_digest, source_order_key,
                   producing_runtime_instance_id)
                VALUES (
                  %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s,
                  %s, %s, %s
                )
                ON CONFLICT (entity_instance_id) DO UPDATE SET
                  event_id = EXCLUDED.event_id,
                  observed_at = EXCLUDED.observed_at,
                  received_at = EXCLUDED.received_at,
                  calculated_at = EXCLUDED.calculated_at,
                  value_float = EXCLUDED.value_float,
                  value_int = EXCLUDED.value_int,
                  value_bool = EXCLUDED.value_bool,
                  value_text = EXCLUDED.value_text,
                  value_codes = EXCLUDED.value_codes,
                  quality = EXCLUDED.quality,
                  reason = EXCLUDED.reason,
                  processing_revision_id = EXCLUDED.processing_revision_id,
                  site_configuration_version = EXCLUDED.site_configuration_version,
                  source_digest = EXCLUDED.source_digest,
                  source_order_key = EXCLUDED.source_order_key,
                  producing_runtime_instance_id =
                    EXCLUDED.producing_runtime_instance_id
                WHERE
                  EXCLUDED.observed_at > t_l2_latest.observed_at
                  OR (
                    EXCLUDED.observed_at = t_l2_latest.observed_at
                    AND EXCLUDED.source_order_key > t_l2_latest.source_order_key
                  )
                RETURNING event_id
                """,
                (
                    str(observation.entity_instance_id),
                    str(observation.event_id),
                    observation.observed_at,
                    observation.received_at,
                    observation.calculated_at,
                    *values,
                    int(observation.quality),
                    observation.reason,
                    str(observation.processing_revision_id),
                    observation.site_configuration_version,
                    observation.source_digest,
                    observation.source_order_key,
                    str(RUNTIME_INSTANCE_ID),
                ),
            )
            if cursor.fetchone() is not None:
                advanced.append(observation)
        return tuple(advanced)

    @staticmethod
    def _insert_sources(cursor, observations: tuple[L2Observation, ...]) -> None:
        for observation in observations:
            if not observation.source_observation_ids:
                continue
            cursor.execute(
                """
                SELECT observation_id, source_digest
                FROM t_l0_observation_dedup
                WHERE observation_id = ANY(%s::uuid[])
                ORDER BY observation_id
                """,
                ([str(item) for item in observation.source_observation_ids],),
            )
            sources = cursor.fetchall()
            if len(sources) != len(observation.source_observation_ids):
                raise DataTrunkError(
                    "POINT_PROCESSING_SOURCE_MISSING",
                    "L2 source observation is unavailable",
                )
            for source_id, source_digest in sources:
                cursor.execute(
                    """
                    INSERT INTO t_l2_observation_sources
                      (l2_event_id, l2_observed_at, source_kind,
                       l0_observation_id, source_digest)
                    VALUES (%s, %s, 'l0', %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(observation.event_id),
                        observation.observed_at,
                        str(source_id),
                        source_digest,
                    ),
                )

    @staticmethod
    def _insert_outbox(cursor, observations: tuple[L2Observation, ...]) -> None:
        for observation in observations:
            payload_value = observation.value.value
            if isinstance(payload_value, tuple):
                payload_value = list(payload_value)
            payload = {
                "event_id": str(observation.event_id),
                "entity_instance_id": str(observation.entity_instance_id),
                "definition_id": observation.definition_id,
                "value_kind": observation.value.kind.value,
                "value": payload_value,
                "unit": observation.unit,
                "quality": int(observation.quality),
                "reason": observation.reason,
                "observed_at": observation.observed_at.isoformat(),
                "received_at": observation.received_at.isoformat(),
                "calculated_at": observation.calculated_at.isoformat(),
                "processing_revision_id": str(
                    observation.processing_revision_id
                ),
                "site_configuration_version": (
                    observation.site_configuration_version
                ),
                "source_digest": observation.source_digest,
                "producing_runtime_instance_id": str(RUNTIME_INSTANCE_ID),
            }
            cursor.execute(
                """
                INSERT INTO t_l2_stream_outbox
                  (event_id, entity_instance_id, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    str(observation.event_id),
                    str(observation.entity_instance_id),
                    Json(payload),
                ),
            )


def _raw_order_key(observation: RawObservation) -> str:
    if observation.source_sequence is None:
        return f"D:{observation.source_digest}"
    return f"S:{observation.source_sequence:020d}:{observation.source_digest}"


def _raw_columns(
    value: TypedValue,
) -> tuple[
    tuple[float | None, int | None, bool | None, str | None],
    tuple[float | None, int | None, bool | None, str | None],
]:
    raw_float: float | None = None
    raw_int: int | None = None
    raw_bool: bool | None = None
    raw_text: str | None = None
    if value.kind is ValueKind.FLOAT and isinstance(value.value, (int, float)):
        raw_float = float(value.value)
    elif value.kind is ValueKind.INT and isinstance(value.value, int):
        raw_int = value.value
    elif value.kind is ValueKind.BOOL and isinstance(value.value, bool):
        raw_bool = value.value
    elif value.kind in {ValueKind.STRING, ValueKind.ENUM} and isinstance(
        value.value,
        str,
    ):
        raw_text = value.value
    else:
        raise DataTrunkError(
            "RAW_OBSERVATION_INVALID",
            "Raw observation has no supported typed value",
        )
    columns = (raw_float, raw_int, raw_bool, raw_text)
    return columns, columns


def _l2_columns(
    value: TypedValue,
) -> tuple[
    float | None,
    int | None,
    bool | None,
    str | None,
    list[str] | None,
]:
    if value.value is None:
        return None, None, None, None, None
    if value.kind is ValueKind.FLOAT:
        return float(value.value), None, None, None, None
    if value.kind is ValueKind.INT:
        return None, int(value.value), None, None, None
    if value.kind is ValueKind.BOOL:
        return None, None, bool(value.value), None, None
    if value.kind in {ValueKind.STRING, ValueKind.ENUM}:
        return None, None, None, str(value.value), None
    if value.kind is ValueKind.CODE_SET:
        return None, None, None, None, list(value.value)
    raise DataTrunkError(
        "POINT_PROCESSING_VALUE_INVALID",
        "Unsupported L2 typed value",
    )


def _normalized_l2_columns(values: tuple[object, ...]) -> tuple[object, ...]:
    return (*values[:4], None if values[4] is None else tuple(values[4]))


def build_postgres_data_trunk() -> DataTrunk:
    return DataTrunk(PostgresDataTrunkRepository())
