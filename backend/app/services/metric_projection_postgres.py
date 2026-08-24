"""Recoverable PostgreSQL runtime for windowed business-metric projections."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from threading import Lock
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg2.extras import Json
from psycopg2 import InterfaceError, OperationalError
from loguru import logger

from app.services.business_metric_contracts import (
    CompiledMetricRevision,
    MetricAggregator,
    MetricCounterContract,
    MetricLifecycle,
    MetricSourceResolution,
    ResolvedMetricSource,
    WindowKind,
)
from app.services.data_trunk_contracts import (
    L2Observation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.data_trunk_postgres import PostgresDataTrunkRepository
from app.services.metric_projection import (
    CounterContract,
    MetricProjectionState,
    MetricWindow,
    ProjectionDecision,
    aligned_daily_window,
    project_metric,
    rolling_window,
)
from app.services.runtime_identity import RUNTIME_INSTANCE_ID
from app.services.solution_business_metrics import (
    compile_business_metric,
    parse_business_metric_asset,
)
from app.services.business_metrics import _producer_contract_digest


ConnectionFactory = Callable[[], AbstractContextManager[Any]]
FaultHook = Callable[[str], None]


@dataclass(frozen=True)
class ProjectionReceipt:
    projection_count: int = 0
    completed_count: int = 0
    corrected_count: int = 0
    invalid_count: int = 0
    error_count: int = 0


@dataclass(frozen=True)
class RecomputeReceipt:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class _InstalledMetric:
    installation_id: UUID
    output_entity_id: UUID
    output_definition_id: str
    output_kind: ValueKind
    output_unit: str | None
    processing_revision_id: UUID
    site_configuration_version: int
    revision: CompiledMetricRevision
    window_kind: WindowKind
    rolling_window_seconds: int | None
    method: MetricAggregator
    allowed_lateness_seconds: int
    correction_horizon_seconds: int
    maximum_sample_gap_seconds: int
    counter_contract: CounterContract | None


@dataclass(frozen=True)
class _PersistedEvent:
    observation: L2Observation
    persisted_observed_at: datetime
    effective_observed_at: datetime
    time_basis: str
    contract_error: str | None = None


@dataclass(frozen=True)
class _WindowResult:
    revision: int
    content_digest: str


@dataclass(frozen=True)
class _ProjectionCheckpoint:
    window: MetricWindow
    watermark_at: datetime | None
    updated_at: datetime
    last_commit_sequence: int


@dataclass(frozen=True)
class _SourceBounds:
    earliest: datetime
    latest: datetime
    new_effective_at: tuple[datetime, ...]
    last_commit_sequence: int
    has_formal_result: bool


class MetricProjection:
    """Hide scans, locks, checkpoints, closure and immutable result publication."""

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
        self._hint_lock = Lock()
        self._pending_event_ids: set[UUID] = set()

    def observe_committed(
        self,
        event_ids: tuple[UUID, ...],
    ) -> ProjectionReceipt:
        """Treat committed IDs only as a wake-up; ``advance`` always rescans PG."""
        if not isinstance(event_ids, tuple) or any(
            not isinstance(event_id, UUID) for event_id in event_ids
        ):
            raise TypeError("committed metric event IDs must be a tuple of UUIDs")
        if not event_ids:
            return ProjectionReceipt()
        with self._hint_lock:
            self._pending_event_ids.update(event_ids)
        return ProjectionReceipt()

    def advance(self, *, now: datetime | None = None) -> ProjectionReceipt:
        instant = _utc(
            self._clock() if now is None else now,
            "metric projection tick",
        )
        with self._hint_lock:
            self._pending_event_ids.clear()
        installation_rows = self._load_installations()
        total = ProjectionReceipt()
        for installation_id, source_rows in installation_rows:
            try:
                installation = self._compile_installation(source_rows)
                receipt = self._advance_installation(installation, instant)
            except (InterfaceError, OperationalError):
                raise
            except Exception as exc:
                # A malformed source contract or one installation's failed write
                # must not prevent other advisory-lock domains from advancing.
                # Connection-factory/global availability failures occur before
                # this boundary and remain visible to the caller.
                logger.error(
                    "metric projection installation {} failed: {}: {}",
                    installation_id,
                    type(exc).__name__,
                    exc,
                )
                receipt = ProjectionReceipt(error_count=1)
            total = ProjectionReceipt(
                projection_count=total.projection_count + receipt.projection_count,
                completed_count=total.completed_count + receipt.completed_count,
                corrected_count=total.corrected_count + receipt.corrected_count,
                invalid_count=total.invalid_count + receipt.invalid_count,
                error_count=total.error_count + receipt.error_count,
            )
        return total

    def recompute(self, command: object) -> RecomputeReceipt:
        del command
        return RecomputeReceipt(
            accepted=False,
            reason="AUDITED_RECOMPUTE_REQUIRES_TASK_5",
        )

    def _load_installations(
        self,
    ) -> tuple[tuple[UUID, tuple[tuple[Any, ...], ...]], ...]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT installed.id, installed.entity_instance_id,
                           output.definition_id, output.data_type, output.unit,
                           processing.revision_id,
                           installed.site_configuration_version,
                           installed.frozen_timezone, revision.content,
                           binding.entity_instance_id,
                           binding.entity_definition_id, binding.method,
                           binding.data_type, binding.unit, binding.estimated,
                           binding.direction,
                           binding.maximum_sample_gap_seconds,
                           binding.producer_contract_digest,
                           binding.counter_maximum, binding.counter_bit_width,
                           binding.counter_reset_on_decrease,
                           binding.counter_rollover_on_decrease
                    FROM t_installed_business_metrics AS installed
                    JOIN t_business_metric_revisions AS revision
                      ON revision.id = installed.template_revision_id
                    JOIN t_installed_point_processings AS processing
                      ON processing.id = installed.installed_processing_id
                    JOIN t_entity_instances AS output
                      ON output.id = installed.entity_instance_id
                    JOIN t_business_metric_source_bindings AS binding
                      ON binding.installed_metric_id = installed.id
                    LEFT JOIN LATERAL (
                      SELECT audit.resulting_state
                      FROM t_business_metric_audit AS audit
                      WHERE audit.installed_metric_id = installed.id
                        AND (
                          (audit.action = 'disabled'
                           AND audit.resulting_state = 'disabled')
                          OR
                          (audit.action IN ('installed','upgraded','enabled')
                           AND audit.resulting_state = 'active')
                        )
                      ORDER BY audit.created_at DESC, audit.id DESC
                      LIMIT 1
                    ) AS lifecycle ON TRUE
                    WHERE COALESCE(lifecycle.resulting_state, installed.state) = 'active'
                      AND installed.installation_revision = (
                        SELECT max(newer.installation_revision)
                        FROM t_installed_business_metrics AS newer
                        WHERE newer.node_id = installed.node_id
                          AND newer.entity_instance_id = installed.entity_instance_id
                      )
                    ORDER BY installed.id, binding.ordinal
                    """
                )
                rows = cursor.fetchall()

        grouped: dict[UUID, list[tuple[Any, ...]]] = {}
        for row in rows:
            grouped.setdefault(UUID(str(row[0])), []).append(row)
        return tuple(
            (installation_id, tuple(source_rows))
            for installation_id, source_rows in sorted(
                grouped.items(), key=lambda item: str(item[0])
            )
        )

    @staticmethod
    def _compile_installation(
        source_rows: tuple[tuple[Any, ...], ...],
    ) -> _InstalledMetric:
        if not source_rows:
            raise RuntimeError("business metric installation has no frozen sources")
        first = source_rows[0]
        installation_id = UUID(str(first[0]))
        template = parse_business_metric_asset(first[8])
        resolved_sources = tuple(
                ResolvedMetricSource(
                    entity_instance_id=UUID(str(row[9])),
                    entity_definition_id=row[10],
                    method=MetricAggregator(row[11]),
                    data_type=row[12],
                    unit=row[13],
                    estimated=bool(row[14]),
                    direction=row[15],
                    maximum_sample_gap_seconds=int(row[16]),
                    producer_contract_digest=(row[17].strip() if row[17] else None),
                    counter_contract=(
                        None
                        if row[18] is None
                        else MetricCounterContract(
                            maximum=Decimal(row[18]),
                            bit_width=row[19],
                            reset_on_decrease=bool(row[20]),
                            rollover_on_decrease=bool(row[21]),
                        )
                    ),
                )
                for row in source_rows
        )
        compiled = compile_business_metric(
            template,
            MetricSourceResolution(first[7], resolved_sources),
        )
        stored_processing_revision_id = UUID(str(first[5]))
        if compiled.processing_revision_id != stored_processing_revision_id:
            raise RuntimeError("business metric compiled revision changed after install")
        if len(resolved_sources) != 1:
            raise RuntimeError("business metric runtime requires one frozen source")
        method = resolved_sources[0].method
        counter_contract = _counter_contract(resolved_sources[0])
        maximum_gap = resolved_sources[0].maximum_sample_gap_seconds
        if maximum_gap is None:
            raise RuntimeError("business metric source freshness is not frozen")
        return _InstalledMetric(
            installation_id=installation_id,
            output_entity_id=UUID(str(first[1])),
            output_definition_id=first[2],
            output_kind=ValueKind(first[3]),
            output_unit=first[4],
            processing_revision_id=stored_processing_revision_id,
            site_configuration_version=int(first[6]),
            revision=compiled,
            window_kind=template.window_kind,
            rolling_window_seconds=template.rolling_window_seconds,
            method=method,
            allowed_lateness_seconds=template.allowed_lateness_seconds,
            correction_horizon_seconds=(
                template.automatic_correction_horizon_seconds
            ),
            maximum_sample_gap_seconds=maximum_gap,
            counter_contract=counter_contract,
        )

    def _advance_installation(
        self,
        installation: _InstalledMetric,
        now: datetime,
    ) -> ProjectionReceipt:
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (str(installation.installation_id),),
                    )
                    checkpoint = self._load_projection_checkpoint(
                        cursor, installation
                    )
                    bounds = self._load_source_bounds(
                        cursor,
                        installation,
                        checkpoint,
                    )
                    if bounds is None:
                        connection.commit()
                        return ProjectionReceipt()
                    windows = _recovery_windows(
                        installation,
                        now,
                        bounds,
                        checkpoint,
                    )
                    if not windows:
                        connection.commit()
                        return ProjectionReceipt()
                    query_start = min(window.start for window in windows)
                    if installation.method is MetricAggregator.COUNTER_DELTA:
                        query_start -= max(
                            (window.end - window.start for window in windows),
                            default=timedelta(0),
                        )
                    query_end = max(window.end for window in windows)
                    events = self._load_events(
                        cursor,
                        installation,
                        query_start,
                        query_end,
                    )
                    observations = tuple(item.observation for item in events)
                    watermark = bounds.latest
                    latest_results = self._latest_results(
                        cursor,
                        installation,
                        windows,
                    )
                    completed = corrected = invalid = 0
                    for window in windows:
                        if watermark <= window.end + timedelta(
                            seconds=installation.allowed_lateness_seconds
                        ):
                            continue
                        latest = latest_results.get((window.start, window.end))
                        if latest is None and not any(
                            window.contains(item.effective_observed_at)
                            for item in events
                        ):
                            continue
                        decision = project_metric(
                            _projection_state(installation),
                            observations,
                            _decision_instant(installation, window),
                        )
                        source_summary = _source_summary(
                            installation,
                            decision,
                            events,
                        )
                        content_digest = _decision_digest(decision, source_summary)
                        if latest is not None and latest.content_digest == content_digest:
                            continue
                        if latest is not None and now - window.end > timedelta(
                            seconds=installation.correction_horizon_seconds
                        ):
                            continue
                        revision = 1 if latest is None else latest.revision + 1
                        lifecycle = (
                            MetricLifecycle.INVALID
                            if decision.lifecycle is MetricLifecycle.INVALID
                            else (
                                MetricLifecycle.COMPLETED
                                if latest is None
                                else MetricLifecycle.CORRECTED
                            )
                        )
                        self._persist_result(
                            cursor,
                            installation,
                            decision,
                            events,
                            source_summary,
                            content_digest,
                            revision,
                            lifecycle,
                            now,
                        )
                        if lifecycle is MetricLifecycle.COMPLETED:
                            completed += 1
                        elif lifecycle is MetricLifecycle.CORRECTED:
                            corrected += 1
                        else:
                            invalid += 1

                    projection_changed = False
                    target = max(windows, key=lambda item: (item.start, item.end))
                    for projection_window in _projection_roll_path(
                        installation,
                        checkpoint,
                        target,
                    ):
                        if (
                            checkpoint is not None
                            and projection_window.start > checkpoint.window.start
                        ):
                            projection_changed = self._reset_projection_window(
                                cursor,
                                installation,
                                projection_window,
                                watermark,
                                bounds.last_commit_sequence,
                                now,
                            ) or projection_changed
                        current = project_metric(
                            _projection_state(installation),
                            observations,
                            _decision_instant(installation, projection_window),
                        )
                        projection_changed = self._upsert_projection(
                            cursor,
                            installation,
                            current,
                            events,
                            (
                                watermark
                                if checkpoint is None or checkpoint.watermark_at is None
                                else max(watermark, checkpoint.watermark_at)
                            ),
                            bounds.last_commit_sequence,
                            now,
                        ) or projection_changed
                    self._fault_hook("checkpoint")
                    connection.commit()
                return ProjectionReceipt(
                    projection_count=int(projection_changed),
                    completed_count=completed,
                    corrected_count=corrected,
                    invalid_count=invalid,
                )
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _load_projection_checkpoint(
        cursor: Any,
        installation: _InstalledMetric,
    ) -> _ProjectionCheckpoint | None:
        cursor.execute(
            """
            SELECT window_started_at, window_ended_at, watermark_at, updated_at,
                   last_commit_sequence
            FROM t_business_metric_projections
            WHERE installed_metric_id = %s
            """,
            (installation.installation_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _ProjectionCheckpoint(
            window=MetricWindow(
                row[0],
                row[1],
                include_start=installation.window_kind is WindowKind.ALIGNED_DAILY,
                include_end=installation.window_kind is WindowKind.ROLLING,
            ),
            watermark_at=(None if row[2] is None else _utc(row[2], "watermark")),
            updated_at=_utc(row[3], "projection updated_at"),
            last_commit_sequence=int(row[4]),
        )

    @staticmethod
    def _load_source_bounds(
        cursor: Any,
        installation: _InstalledMetric,
        checkpoint: _ProjectionCheckpoint | None,
    ) -> _SourceBounds | None:
        source = installation.revision.sources[0]
        if checkpoint is None:
            cursor.execute(
                """
                SELECT min(
                         CASE event_time_basis
                           WHEN 'observed_at' THEN observed_at
                           WHEN 'received_at' THEN received_at
                           WHEN 'calculated_at' THEN calculated_at
                           ELSE received_at
                         END
                       ),
                       max(
                         CASE event_time_basis
                           WHEN 'observed_at' THEN observed_at
                           WHEN 'received_at' THEN received_at
                           WHEN 'calculated_at' THEN calculated_at
                           ELSE received_at
                         END
                       ),
                       array_agg(
                         CASE event_time_basis
                           WHEN 'observed_at' THEN observed_at
                           WHEN 'received_at' THEN received_at
                           WHEN 'calculated_at' THEN calculated_at
                           ELSE received_at
                         END ORDER BY commit_sequence
                       ),
                       max(commit_sequence)
                FROM t_l2_observations
                WHERE entity_instance_id = %s
                """,
                (source.entity_instance_id,),
            )
            row = cursor.fetchone()
            if row is None or row[0] is None:
                return None
            return _SourceBounds(
                earliest=_utc(row[0], "earliest source event"),
                latest=_utc(row[1], "latest source event"),
                new_effective_at=tuple(
                    _utc(item, "new source event") for item in row[2]
                ),
                last_commit_sequence=int(row[3]),
                has_formal_result=False,
            )

        cursor.execute(
            """
            SELECT CASE event_time_basis
                     WHEN 'observed_at' THEN observed_at
                     WHEN 'received_at' THEN received_at
                     WHEN 'calculated_at' THEN calculated_at
                     ELSE received_at
                   END,
                   commit_sequence
            FROM t_l2_observations
            WHERE entity_instance_id = %s
              AND commit_sequence > %s
            ORDER BY commit_sequence
            """,
            (
                source.entity_instance_id,
                checkpoint.last_commit_sequence,
            ),
        )
        rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM t_business_metric_window_results
              WHERE installed_metric_id = %s
            )
            """,
            (installation.installation_id,),
        )
        has_formal_result = bool(cursor.fetchone()[0])
        if not rows:
            fallback = checkpoint.watermark_at or checkpoint.window.start
            return _SourceBounds(
                earliest=checkpoint.window.start,
                latest=fallback,
                new_effective_at=(),
                last_commit_sequence=checkpoint.last_commit_sequence,
                has_formal_result=has_formal_result,
            )
        effective = tuple(_utc(row[0], "new source event") for row in rows)
        latest = max(effective)
        if checkpoint.watermark_at is not None:
            latest = max(latest, checkpoint.watermark_at)
        earliest = min(checkpoint.window.start, min(effective))
        if not has_formal_result:
            cursor.execute(
                """
                SELECT min(
                  CASE event_time_basis
                    WHEN 'observed_at' THEN observed_at
                    WHEN 'received_at' THEN received_at
                    WHEN 'calculated_at' THEN calculated_at
                    ELSE received_at
                  END
                )
                FROM t_l2_observations
                WHERE entity_instance_id = %s
                """,
                (source.entity_instance_id,),
            )
            first_row = cursor.fetchone()
            if first_row is not None and first_row[0] is not None:
                earliest = _utc(first_row[0], "earliest source event")
        return _SourceBounds(
            earliest=earliest,
            latest=latest,
            new_effective_at=effective,
            last_commit_sequence=int(rows[-1][1]),
            has_formal_result=has_formal_result,
        )

    @staticmethod
    def _load_events(
        cursor: Any,
        installation: _InstalledMetric,
        started_at: datetime,
        ended_at: datetime,
    ) -> tuple[_PersistedEvent, ...]:
        source = installation.revision.sources[0]
        cursor.execute(
            """
            SELECT observation.event_id, observation.observed_at,
                   observation.received_at, observation.calculated_at,
                   observation.value_float, observation.value_int,
                   observation.value_numeric,
                   observation.quality, observation.reason,
                   observation.processing_revision_id,
                   observation.site_configuration_version,
                   observation.source_digest, observation.source_order_key,
                   observation.event_time_basis,
                   producer.output_id, producer.entity_definition_id,
                   producer.data_type, producer.unit,
                   producer.freshness_seconds,
                   producer.revision_content_digest
            FROM t_l2_observations AS observation
            LEFT JOIN LATERAL (
              SELECT output.id AS output_id, output.entity_definition_id,
                     output.data_type, output.unit, output.freshness_seconds,
                     revision.content_digest AS revision_content_digest
              FROM t_point_processing_output_bindings AS output_binding
              JOIN t_installed_point_processings AS installed
                ON installed.id = output_binding.installed_processing_id
               AND installed.revision_id = observation.processing_revision_id
              JOIN t_point_processing_outputs AS output
                ON output.id = output_binding.output_id
               AND output.revision_id = installed.revision_id
              JOIN t_point_processing_revisions AS revision
                ON revision.id = output.revision_id
              WHERE output_binding.entity_instance_id = observation.entity_instance_id
              ORDER BY installed.installed_at DESC, installed.id DESC,
                       output.output_key, output.id
              LIMIT 1
            ) AS producer ON TRUE
            WHERE observation.entity_instance_id = %s
              AND CASE observation.event_time_basis
                    WHEN 'observed_at' THEN observation.observed_at
                    WHEN 'received_at' THEN observation.received_at
                    WHEN 'calculated_at' THEN observation.calculated_at
                    ELSE observation.received_at
                  END >= %s
              AND CASE observation.event_time_basis
                    WHEN 'observed_at' THEN observation.observed_at
                    WHEN 'received_at' THEN observation.received_at
                    WHEN 'calculated_at' THEN observation.calculated_at
                    ELSE observation.received_at
                  END <= %s
            ORDER BY observation.observed_at, observation.source_order_key,
                     observation.event_id
            """,
            (source.entity_instance_id, started_at, ended_at),
        )
        loaded: list[_PersistedEvent] = []
        for row in cursor.fetchall():
            persisted_observed_at = _utc(row[1], "source observed_at")
            received_at = _utc(row[2], "source received_at")
            event_time_basis = row[13]
            effective_observed_at = {
                "observed_at": persisted_observed_at,
                "received_at": received_at,
                "calculated_at": _utc(row[3], "source calculated_at"),
                "unknown": received_at,
            }[event_time_basis]
            producer_digest = None
            if row[14] is not None:
                producer_digest = _producer_contract_digest(
                    processing_revision_id=UUID(str(row[9])),
                    output_id=UUID(str(row[14])),
                    revision_content_digest=row[19].strip(),
                    entity_definition_id=row[15],
                    data_type=row[16],
                    unit=row[17],
                    freshness_seconds=row[18],
                )
            contract_error = (
                None
                if source.producer_contract_digest is not None
                and producer_digest == source.producer_contract_digest
                and row[15] == source.entity_definition_id
                and row[16] == source.data_type
                and row[17] == source.unit
                else "SOURCE_CONTRACT_MISMATCH"
            )
            quality = (
                TrunkQuality(int(row[7]))
                if contract_error is None
                else TrunkQuality.BAD
            )
            actual_data_type = row[16] if row[16] in {"FLOAT", "INT"} else source.data_type
            raw_value = row[6]
            if raw_value is None:
                raw_value = row[4] if actual_data_type == "FLOAT" else row[5]
            value = (
                TypedValue.float(raw_value)
                if actual_data_type == "FLOAT"
                else TypedValue.integer(raw_value)
            )
            observation = L2Observation(
                event_id=UUID(str(row[0])),
                entity_instance_id=source.entity_instance_id,
                definition_id=row[15] or "producer-contract-missing",
                value=value,
                unit=row[17],
                quality=quality,
                reason=contract_error or row[8],
                observed_at=effective_observed_at,
                received_at=received_at,
                calculated_at=_utc(row[3], "source calculated_at"),
                processing_revision_id=UUID(str(row[9])),
                site_configuration_version=int(row[10]),
                source_observation_ids=(),
                source_digest=row[11].strip(),
                source_order_key=row[12],
                event_time_basis=event_time_basis,
            )
            loaded.append(
                _PersistedEvent(
                    observation=observation,
                    persisted_observed_at=persisted_observed_at,
                    effective_observed_at=effective_observed_at,
                    time_basis=event_time_basis,
                    contract_error=contract_error,
                )
            )
        return tuple(
            sorted(
                loaded,
                key=lambda item: (
                    item.effective_observed_at,
                    item.observation.source_order_key,
                    item.observation.event_id,
                ),
            )
        )

    @staticmethod
    def _latest_results(
        cursor: Any,
        installation: _InstalledMetric,
        windows: tuple[MetricWindow, ...],
    ) -> dict[tuple[datetime, datetime], _WindowResult]:
        if not windows:
            return {}
        earliest = min(window.start for window in windows)
        latest = max(window.end for window in windows)
        cursor.execute(
            """
            SELECT DISTINCT ON (window_started_at, window_ended_at)
                   window_started_at, window_ended_at, revision, content_digest
            FROM t_business_metric_window_results
            WHERE installed_metric_id = %s
              AND window_started_at >= %s
              AND window_ended_at <= %s
            ORDER BY window_started_at, window_ended_at, revision DESC
            """,
            (installation.installation_id, earliest, latest),
        )
        return {
            (_utc(row[0], "result window start"), _utc(row[1], "result window end")):
                _WindowResult(int(row[2]), row[3].strip())
            for row in cursor.fetchall()
        }

    def _persist_result(
        self,
        cursor: Any,
        installation: _InstalledMetric,
        decision: ProjectionDecision,
        events: tuple[_PersistedEvent, ...],
        source_summary: Mapping[str, Any],
        content_digest: str,
        revision: int,
        lifecycle: MetricLifecycle,
        now: datetime,
    ) -> None:
        result_event: L2Observation | None = None
        result_event_id: UUID | None = None
        if lifecycle is not MetricLifecycle.INVALID:
            result_event_id = uuid5(
                NAMESPACE_URL,
                "zizu/business-metric/result/"
                f"{installation.installation_id}/{decision.window.start.isoformat()}/"
                f"{decision.window.end.isoformat()}/{revision}",
            )
            result_event = L2Observation(
                event_id=result_event_id,
                entity_instance_id=installation.output_entity_id,
                definition_id=installation.output_definition_id,
                value=_result_value(installation.output_kind, decision.value),
                unit=installation.output_unit,
                quality=decision.quality,
                reason=decision.reason,
                observed_at=decision.window.end,
                received_at=now,
                calculated_at=now,
                processing_revision_id=installation.processing_revision_id,
                site_configuration_version=installation.site_configuration_version,
                source_observation_ids=decision.source_event_ids,
                source_digest=content_digest,
                source_order_key=(
                    f"M:{decision.window.end.isoformat()}:{revision:010d}"
                ),
                event_time_basis="observed_at",
            )
            PostgresDataTrunkRepository._ensure_runtime(cursor)
            PostgresDataTrunkRepository._insert_l2(cursor, (result_event,))
            self._fault_hook("l2")
            PostgresDataTrunkRepository._advance_l2_latest(cursor, (result_event,))
            PostgresDataTrunkRepository._insert_sources(cursor, (result_event,))
            self._fault_hook("source")
            PostgresDataTrunkRepository._insert_outbox(cursor, (result_event,))
            self._fault_hook("outbox")

        source_rows = _source_rows(decision, events)
        first = source_rows[0] if source_rows else None
        last = source_rows[-1] if source_rows else None
        cursor.execute(
            """
            INSERT INTO t_business_metric_window_results
              (installed_metric_id, window_started_at, window_ended_at,
               revision, lifecycle, calculation_method, quality, coverage,
               estimated, source_count, first_source_event_id,
               first_source_observed_at, first_source_effective_at,
               last_source_event_id, last_source_observed_at,
               last_source_effective_at, result_event_id, result_observed_at,
               result_entity_instance_id, content_digest, source_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                installation.installation_id,
                decision.window.start,
                decision.window.end,
                revision,
                lifecycle.value,
                installation.method.value,
                int(decision.quality),
                decision.coverage,
                decision.estimated,
                len(source_rows),
                None if first is None else first.observation.event_id,
                None if first is None else first.persisted_observed_at,
                None if first is None else first.effective_observed_at,
                None if last is None else last.observation.event_id,
                None if last is None else last.persisted_observed_at,
                None if last is None else last.effective_observed_at,
                result_event_id,
                None if result_event is None else result_event.observed_at,
                None if result_event is None else installation.output_entity_id,
                content_digest,
                Json(dict(source_summary)),
            ),
        )
        self._fault_hook("result")

    @staticmethod
    def _reset_projection_window(
        cursor: Any,
        installation: _InstalledMetric,
        window: MetricWindow,
        watermark: datetime,
        last_commit_sequence: int,
        updated_at: datetime,
    ) -> bool:
        state = {
            "lifecycle": MetricLifecycle.PROVISIONAL.value,
            "windowStartedAt": window.start.isoformat(),
            "windowEndedAt": window.end.isoformat(),
            "value": None,
            "reason": None,
            "sourceEventIds": [],
            "sourceSummary": {},
            "peakAt": None,
            "peakEventId": None,
        }
        cursor.execute(
            """
            UPDATE t_business_metric_projections
            SET window_started_at = %s, window_ended_at = %s,
                watermark_at = GREATEST(watermark_at, %s),
                coverage = 0, quality = %s, estimated = %s,
                last_commit_sequence = %s, state = %s, updated_at = %s
            WHERE installed_metric_id = %s
            RETURNING installed_metric_id
            """,
            (
                window.start,
                window.end,
                watermark,
                int(TrunkQuality.BAD),
                installation.revision.sources[0].estimated,
                last_commit_sequence,
                Json(state),
                updated_at,
                installation.installation_id,
            ),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _upsert_projection(
        cursor: Any,
        installation: _InstalledMetric,
        decision: ProjectionDecision,
        events: tuple[_PersistedEvent, ...],
        watermark: datetime | None,
        last_commit_sequence: int,
        updated_at: datetime,
    ) -> bool:
        source_summary = _source_summary(installation, decision, events)
        state = {
            "lifecycle": MetricLifecycle.PROVISIONAL.value,
            "windowStartedAt": decision.window.start.isoformat(),
            "windowEndedAt": decision.window.end.isoformat(),
            "value": None if decision.value is None else _decimal_text(decision.value),
            "reason": next(
                (
                    item.contract_error
                    for item in events
                    if item.contract_error is not None
                    and item.observation.event_id in decision.source_event_ids
                ),
                decision.reason,
            ),
            "sourceEventIds": [str(item) for item in decision.source_event_ids],
            "sourceSummary": source_summary,
            "peakAt": None if decision.peak_at is None else decision.peak_at.isoformat(),
            "peakEventId": (
                None if decision.peak_event_id is None else str(decision.peak_event_id)
            ),
        }
        cursor.execute(
            """
            INSERT INTO t_business_metric_projections
              (installed_metric_id, window_started_at, window_ended_at,
               watermark_at, coverage, quality, estimated,
               last_commit_sequence, state, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (installed_metric_id) DO UPDATE SET
              window_started_at = EXCLUDED.window_started_at,
              window_ended_at = EXCLUDED.window_ended_at,
              watermark_at = EXCLUDED.watermark_at,
              coverage = EXCLUDED.coverage,
              quality = EXCLUDED.quality,
              estimated = EXCLUDED.estimated,
              last_commit_sequence = EXCLUDED.last_commit_sequence,
              state = EXCLUDED.state,
              updated_at = EXCLUDED.updated_at
            WHERE ROW(
                    t_business_metric_projections.window_started_at,
                    t_business_metric_projections.window_ended_at,
                    t_business_metric_projections.watermark_at,
                    t_business_metric_projections.coverage,
                    t_business_metric_projections.quality,
                    t_business_metric_projections.estimated,
                    t_business_metric_projections.last_commit_sequence,
                    t_business_metric_projections.state
                  ) IS DISTINCT FROM ROW(
                    EXCLUDED.window_started_at,
                    EXCLUDED.window_ended_at,
                    EXCLUDED.watermark_at,
                    EXCLUDED.coverage,
                    EXCLUDED.quality,
                    EXCLUDED.estimated,
                    EXCLUDED.last_commit_sequence,
                    EXCLUDED.state
                  )
            RETURNING installed_metric_id
            """,
            (
                installation.installation_id,
                decision.window.start,
                decision.window.end,
                watermark,
                decision.coverage,
                int(decision.quality),
                decision.estimated,
                last_commit_sequence,
                Json(state),
                updated_at,
            ),
        )
        return cursor.fetchone() is not None


def _projection_state(installation: _InstalledMetric) -> MetricProjectionState:
    return MetricProjectionState(
        revision=installation.revision,
        counter_contract=installation.counter_contract,
        maximum_sample_gap_seconds=installation.maximum_sample_gap_seconds,
    )


def _counter_contract(
    source: ResolvedMetricSource,
) -> CounterContract | None:
    if source.method is not MetricAggregator.COUNTER_DELTA:
        return None
    contract = source.counter_contract
    if contract is None:
        return None
    return CounterContract(
        maximum=contract.maximum,
        bit_width=contract.bit_width,
        reset_on_decrease=contract.reset_on_decrease,
        rollover_on_decrease=contract.rollover_on_decrease,
    )


def _projection_instant(installation: _InstalledMetric, now: datetime) -> datetime:
    if installation.window_kind is WindowKind.ALIGNED_DAILY:
        return now
    return now.replace(second=0, microsecond=0)


def _decision_instant(
    installation: _InstalledMetric,
    window: MetricWindow,
) -> datetime:
    if installation.window_kind is WindowKind.ALIGNED_DAILY:
        return window.start + timedelta(microseconds=1)
    return window.end


def _recovery_windows(
    installation: _InstalledMetric,
    now: datetime,
    bounds: _SourceBounds,
    checkpoint: _ProjectionCheckpoint | None,
) -> tuple[MetricWindow, ...]:
    current = _window_for_instant(installation, now)
    if checkpoint is None or not bounds.has_formal_result:
        first = _window_for_instant(installation, bounds.earliest)
    else:
        first = checkpoint.window
    advancement_limit = 64 if installation.window_kind is WindowKind.ALIGNED_DAILY else 360
    advancement = _window_sequence(
        installation,
        first,
        current,
        limit=advancement_limit,
    )
    affected: dict[tuple[datetime, datetime], MetricWindow] = {
        (window.start, window.end): window for window in advancement
    }
    if checkpoint is not None and bounds.new_effective_at:
        horizon_start = now - timedelta(
            seconds=installation.correction_horizon_seconds
        )
        for effective_at in bounds.new_effective_at:
            if installation.window_kind is WindowKind.ALIGNED_DAILY:
                late_window = aligned_daily_window(
                    effective_at,
                    installation.revision.timezone,
                )
                if late_window.end >= horizon_start:
                    affected[(late_window.start, late_window.end)] = late_window
                continue
            seconds = installation.rolling_window_seconds
            if seconds is None:
                raise RuntimeError("rolling metric has no frozen duration")
            first_end = effective_at.replace(second=0, microsecond=0)
            if first_end < effective_at:
                first_end += timedelta(minutes=1)
            last_end = min(
                current.end,
                effective_at + timedelta(seconds=seconds),
            )
            candidate = rolling_window(first_end, seconds)
            for _ in range(361):
                if candidate.end > last_end:
                    break
                if candidate.end >= horizon_start:
                    affected[(candidate.start, candidate.end)] = candidate
                candidate = _next_window(installation, candidate)
    return tuple(affected[key] for key in sorted(affected))


def _window_for_instant(
    installation: _InstalledMetric,
    instant: datetime,
) -> MetricWindow:
    if installation.window_kind is WindowKind.ALIGNED_DAILY:
        return aligned_daily_window(instant, installation.revision.timezone)
    seconds = installation.rolling_window_seconds
    if seconds is None:
        raise RuntimeError("rolling metric has no frozen duration")
    end = instant.replace(second=0, microsecond=0)
    if end < instant:
        end += timedelta(minutes=1)
    return rolling_window(end, seconds)


def _next_window(
    installation: _InstalledMetric,
    window: MetricWindow,
) -> MetricWindow:
    if installation.window_kind is WindowKind.ALIGNED_DAILY:
        return aligned_daily_window(
            window.end + timedelta(microseconds=1),
            installation.revision.timezone,
        )
    seconds = installation.rolling_window_seconds
    if seconds is None:
        raise RuntimeError("rolling metric has no frozen duration")
    return rolling_window(window.end + timedelta(minutes=1), seconds)


def _window_sequence(
    installation: _InstalledMetric,
    first: MetricWindow,
    last: MetricWindow,
    *,
    limit: int,
) -> tuple[MetricWindow, ...]:
    if first.start > last.start:
        return (last,)
    windows: list[MetricWindow] = []
    current = first
    for _ in range(limit):
        windows.append(current)
        if current.start == last.start and current.end == last.end:
            break
        current = _next_window(installation, current)
        if current.start > last.start:
            break
    return tuple(windows)


def _projection_roll_path(
    installation: _InstalledMetric,
    checkpoint: _ProjectionCheckpoint | None,
    target: MetricWindow,
) -> tuple[MetricWindow, ...]:
    if checkpoint is None:
        return (target,)
    if target.start <= checkpoint.window.start:
        return (checkpoint.window,)
    return _window_sequence(
        installation,
        _next_window(installation, checkpoint.window),
        target,
        limit=360,
    )


def _source_summary(
    installation: _InstalledMetric,
    decision: ProjectionDecision,
    events: tuple[_PersistedEvent, ...],
) -> dict[str, Any]:
    by_id = {item.observation.event_id: item for item in events}
    selected = tuple(
        by_id[event_id]
        for event_id in decision.source_event_ids
        if event_id in by_id
    )
    ordered = tuple(
        sorted(
            selected,
            key=lambda item: (
                item.effective_observed_at,
                item.observation.source_order_key,
                item.observation.event_id,
            ),
        )
    )
    event_content = [
        {
            "eventId": str(item.observation.event_id),
            "observedAt": item.persisted_observed_at.isoformat(),
            "effectiveObservedAt": item.effective_observed_at.isoformat(),
            "timeBasis": item.time_basis,
            "value": _canonical_value(item.observation.value.value),
            "quality": int(item.observation.quality),
            "sourceDigest": item.observation.source_digest,
            "producerContractError": item.contract_error,
        }
        for item in ordered
    ]
    event_content_digest = hashlib.sha256(
        json.dumps(
            event_content,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    first = ordered[0] if ordered else None
    last = ordered[-1] if ordered else None
    return {
        "method": installation.method.value,
        "templateDigest": installation.revision.template_digest,
        "bindingDigest": installation.revision.source_digest,
        "sourceEntityInstanceIds": [
            str(item.entity_instance_id) for item in installation.revision.sources
        ],
        "runtimeInstanceId": str(RUNTIME_INSTANCE_ID),
        "eventCount": len(ordered),
        "sourceEventIds": [str(item.observation.event_id) for item in ordered],
        "firstObservedAt": (
            None if first is None else first.persisted_observed_at.isoformat()
        ),
        "lastObservedAt": (
            None if last is None else last.persisted_observed_at.isoformat()
        ),
        "firstEffectiveObservedAt": (
            None if first is None else first.effective_observed_at.isoformat()
        ),
        "lastEffectiveObservedAt": (
            None if last is None else last.effective_observed_at.isoformat()
        ),
        "eventContentDigest": event_content_digest,
        "timeBasis": {
            str(item.observation.event_id): item.time_basis for item in ordered
        },
        "effectiveObservedAt": {
            str(item.observation.event_id): item.effective_observed_at.isoformat()
            for item in ordered
        },
    }


def _decision_digest(
    decision: ProjectionDecision,
    source_summary: Mapping[str, Any],
) -> str:
    stable_source_summary = dict(source_summary)
    stable_source_summary.pop("runtimeInstanceId", None)
    content = {
        "windowStartedAt": decision.window.start.isoformat(),
        "windowEndedAt": decision.window.end.isoformat(),
        "quality": int(decision.quality),
        "coverage": decision.coverage,
        "estimated": decision.estimated,
        "value": None if decision.value is None else _decimal_text(decision.value),
        "reason": decision.reason,
        "peakAt": None if decision.peak_at is None else decision.peak_at.isoformat(),
        "peakEventId": (
            None if decision.peak_event_id is None else str(decision.peak_event_id)
        ),
        "sourceSummary": stable_source_summary,
    }
    return hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _source_rows(
    decision: ProjectionDecision,
    events: tuple[_PersistedEvent, ...],
) -> tuple[_PersistedEvent, ...]:
    selected_ids = set(decision.source_event_ids)
    return tuple(
        sorted(
            (item for item in events if item.observation.event_id in selected_ids),
            key=lambda item: (
                item.effective_observed_at,
                item.observation.source_order_key,
                item.observation.event_id,
            ),
        )
    )


def _result_value(kind: ValueKind, value: Decimal | None) -> TypedValue:
    if value is None:
        raise RuntimeError("formal metric result requires a value")
    if kind is ValueKind.FLOAT:
        return TypedValue.float(value)
    if kind is ValueKind.INT:
        return TypedValue.integer(int(value))
    raise RuntimeError("business metric output must be numeric")


def _decimal_text(value: Decimal) -> str:
    decimal = Decimal(value)
    if decimal.is_zero():
        return "0"
    return format(decimal.normalize(), "f")


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


_default_runtime: MetricProjection | None = None
_default_runtime_lock = Lock()


def get_default_metric_projection() -> MetricProjection:
    global _default_runtime
    with _default_runtime_lock:
        if _default_runtime is None:
            _default_runtime = MetricProjection()
        return _default_runtime


__all__ = [
    "MetricProjection",
    "ProjectionReceipt",
    "RecomputeReceipt",
    "get_default_metric_projection",
]
