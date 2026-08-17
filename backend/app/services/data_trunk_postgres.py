"""L0、L2、latest、source 与 outbox 的单事务 PostgreSQL adapter。"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg2.extras import Json

from app.services.data_trunk import ConversionEvaluator
from app.services.data_trunk_contracts import (
    CommitReceipt,
    DataTrunkError,
    InputReference,
    InstalledPointConversion,
    L2Observation,
    RawObservation,
    TypedValue,
    ValueKind,
)
ConnectionFactory = Callable[[], AbstractContextManager[Any]]
FaultHook = Callable[[str], None]


@dataclass(frozen=True)
class _ConversionSnapshot:
    installed: tuple[InstalledPointConversion, ...]
    site_configuration_version: int


class PostgresDataTrunkRepository:
    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        fault_hook: FaultHook | None = None,
    ) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault_hook = fault_hook or (lambda _stage: None)

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
                        late_l0 = self._advance_l0_latest(cursor, accepted)
                        if accepted:
                            snapshot = self._load_conversion_snapshot(cursor, accepted)
                            produced = self._evaluate_batch(
                                snapshot,
                                accepted,
                                evaluator,
                                calculated_at=self._clock(),
                            )
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
    def _advance_l0_latest(cursor, observations: tuple[RawObservation, ...]) -> int:
        late = 0
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
        return late

    @staticmethod
    def _load_conversion_snapshot(
        cursor,
        observations: tuple[RawObservation, ...],
    ) -> _ConversionSnapshot:
        tag_ids = tuple(sorted({item.tag_id for item in observations}, key=str))

        cursor.execute(
            """
            SELECT
              installed.id,
              installed.revision_id,
              input_binding.l0_tag_id,
              output_binding.entity_instance_id,
              output.entity_definition_id,
              output.data_type,
              output.unit,
              output.freshness_seconds,
              input.unit,
              numeric.scale,
              numeric."offset",
              numeric.minimum,
              numeric.maximum
            FROM t_installed_point_conversions AS installed
            JOIN t_conversion_input_bindings AS input_binding
              ON input_binding.installed_conversion_id = installed.id
             AND input_binding.source_kind = 'l0'
            JOIN t_point_conversion_inputs AS input
              ON input.id = input_binding.input_id
            JOIN t_numeric_transform_rules AS numeric
              ON numeric.input_id = input.id
            JOIN t_point_conversion_outputs AS output
              ON output.id = numeric.output_id
            JOIN t_conversion_output_bindings AS output_binding
              ON output_binding.installed_conversion_id = installed.id
             AND output_binding.output_id = output.id
            WHERE installed.current = TRUE
              AND input_binding.l0_tag_id = ANY(%s::uuid[])
            ORDER BY output_binding.entity_instance_id, installed.id
            """,
            ([str(tag_id) for tag_id in tag_ids],),
        )
        installed_items: list[InstalledPointConversion] = []
        for row in cursor.fetchall():
            (
                installation_id,
                revision_id,
                input_tag_id,
                entity_instance_id,
                definition_id,
                output_type,
                output_unit,
                freshness_seconds,
                input_unit,
                scale,
                offset,
                minimum,
                maximum,
            ) = row
            if output_type != ValueKind.FLOAT.value:
                raise DataTrunkError(
                    "POINT_CONVERSION_CONFIGURATION_INVALID",
                    "numeric transform output must be FLOAT",
                )
            item = InstalledPointConversion.numeric(
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
            installed_items.append(
                replace(item, freshness_seconds=freshness_seconds)
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
                "POINT_CONVERSION_CONFIGURATION_INVALID",
                "site configuration state is unavailable",
            )
        return _ConversionSnapshot(
            installed=tuple(installed_items),
            site_configuration_version=site_row[0],
        )

    @staticmethod
    def _evaluate_batch(
        snapshot: _ConversionSnapshot,
        observations: tuple[RawObservation, ...],
        evaluator: ConversionEvaluator,
        *,
        calculated_at: datetime,
    ) -> tuple[L2Observation, ...]:
        produced: list[L2Observation] = []
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
                if item.transform.input == input_reference
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
        return tuple(produced)

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
                   conversion_revision_id, site_configuration_version,
                   source_digest, source_order_key)
                VALUES (
                  %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s,
                  %s, %s
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
                    str(observation.conversion_revision_id),
                    observation.site_configuration_version,
                    observation.source_digest,
                    observation.source_order_key,
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
                   conversion_revision_id, site_configuration_version,
                   source_digest, source_order_key)
                VALUES (
                  %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s,
                  %s, %s
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
                  conversion_revision_id = EXCLUDED.conversion_revision_id,
                  site_configuration_version = EXCLUDED.site_configuration_version,
                  source_digest = EXCLUDED.source_digest,
                  source_order_key = EXCLUDED.source_order_key
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
                    str(observation.conversion_revision_id),
                    observation.site_configuration_version,
                    observation.source_digest,
                    observation.source_order_key,
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
                    "POINT_CONVERSION_SOURCE_MISSING",
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
                "conversion_revision_id": str(
                    observation.conversion_revision_id
                ),
                "site_configuration_version": (
                    observation.site_configuration_version
                ),
                "source_digest": observation.source_digest,
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
        "POINT_CONVERSION_VALUE_INVALID",
        "Unsupported L2 typed value",
    )
