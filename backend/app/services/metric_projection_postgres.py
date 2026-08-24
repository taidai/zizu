"""Recoverable PostgreSQL runtime for windowed business-metric projections."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import math
from threading import Lock
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg2.extras import Json

from app.services.business_metric_contracts import (
    CompiledMetricRevision,
    MetricAggregator,
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


ConnectionFactory = Callable[[], AbstractContextManager[Any]]
FaultHook = Callable[[str], None]


@dataclass(frozen=True)
class ProjectionReceipt:
    projection_count: int = 0
    completed_count: int = 0
    corrected_count: int = 0
    invalid_count: int = 0


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


@dataclass(frozen=True)
class _WindowResult:
    revision: int
    content_digest: str


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
        return self.advance(now=self._clock())

    def advance(self, *, now: datetime) -> ProjectionReceipt:
        instant = _utc(now, "metric projection tick")
        installations = self._load_installations()
        total = ProjectionReceipt()
        for installation in installations:
            receipt = self._advance_installation(installation, instant)
            total = ProjectionReceipt(
                projection_count=total.projection_count + receipt.projection_count,
                completed_count=total.completed_count + receipt.completed_count,
                corrected_count=total.corrected_count + receipt.corrected_count,
                invalid_count=total.invalid_count + receipt.invalid_count,
            )
        return total

    def recompute(self, command: object) -> RecomputeReceipt:
        del command
        return RecomputeReceipt(
            accepted=False,
            reason="AUDITED_RECOMPUTE_REQUIRES_TASK_5",
        )

    def _load_installations(self) -> tuple[_InstalledMetric, ...]:
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
                           binding.direction, source.freshness_seconds
                    FROM t_installed_business_metrics AS installed
                    JOIN t_business_metric_revisions AS revision
                      ON revision.id = installed.template_revision_id
                    JOIN t_installed_point_processings AS processing
                      ON processing.id = installed.installed_processing_id
                    JOIN t_entity_instances AS output
                      ON output.id = installed.entity_instance_id
                    JOIN t_business_metric_source_bindings AS binding
                      ON binding.installed_metric_id = installed.id
                    JOIN t_entity_instances AS source
                      ON source.id = binding.entity_instance_id
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
        installations: list[_InstalledMetric] = []
        for installation_id, source_rows in grouped.items():
            first = source_rows[0]
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
            counter_contract = _counter_contract(resolved_sources[0].data_type, method)
            freshness = first[16]
            maximum_gap = max(1, int(math.ceil(float(freshness))))
            installations.append(
                _InstalledMetric(
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
            )
        return tuple(installations)

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
                    events = self._load_events(cursor, installation)
                    observations = tuple(item.observation for item in events)
                    watermark = max(
                        (item.effective_observed_at for item in events),
                        default=None,
                    )
                    projection_now = _projection_instant(installation, now)
                    current = project_metric(
                        _projection_state(installation),
                        observations,
                        projection_now,
                    )
                    completed = corrected = invalid = 0
                    for window in _candidate_windows(installation, now):
                        if watermark is None or watermark <= window.end + timedelta(
                            seconds=installation.allowed_lateness_seconds
                        ):
                            continue
                        latest = self._latest_result(cursor, installation, window)
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

                    projection_changed = self._upsert_projection(
                        cursor,
                        installation,
                        current,
                        events,
                        watermark,
                    )
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
    def _load_events(
        cursor: Any,
        installation: _InstalledMetric,
    ) -> tuple[_PersistedEvent, ...]:
        source = installation.revision.sources[0]
        cursor.execute(
            """
            SELECT observation.event_id, observation.observed_at,
                   observation.received_at, observation.calculated_at,
                   observation.value_float, observation.value_int,
                   observation.quality, observation.reason,
                   observation.processing_revision_id,
                   observation.site_configuration_version,
                   observation.source_digest, observation.source_order_key
            FROM t_l2_observations AS observation
            WHERE observation.entity_instance_id = %s
            ORDER BY observation.observed_at, observation.source_order_key,
                     observation.event_id
            """,
            (source.entity_instance_id,),
        )
        loaded: list[_PersistedEvent] = []
        for row in cursor.fetchall():
            persisted_observed_at = _utc(row[1], "source observed_at")
            received_at = _utc(row[2], "source received_at")
            observed_is_trusted = persisted_observed_at < received_at
            effective_observed_at = (
                persisted_observed_at if observed_is_trusted else received_at
            )
            quality = TrunkQuality(int(row[6]))
            raw_value = row[4] if source.data_type == "FLOAT" else row[5]
            value = (
                TypedValue.float(None if raw_value is None else float(raw_value))
                if source.data_type == "FLOAT"
                else TypedValue.integer(None if raw_value is None else int(raw_value))
            )
            observation = L2Observation(
                event_id=UUID(str(row[0])),
                entity_instance_id=source.entity_instance_id,
                definition_id=source.entity_definition_id,
                value=value,
                unit=source.unit,
                quality=quality,
                reason=row[7],
                observed_at=effective_observed_at,
                received_at=received_at,
                calculated_at=_utc(row[3], "source calculated_at"),
                processing_revision_id=UUID(str(row[8])),
                site_configuration_version=int(row[9]),
                source_observation_ids=(),
                source_digest=row[10].strip(),
                source_order_key=row[11],
            )
            loaded.append(
                _PersistedEvent(
                    observation=observation,
                    persisted_observed_at=persisted_observed_at,
                    effective_observed_at=effective_observed_at,
                    time_basis=("observed_at" if observed_is_trusted else "received_at"),
                )
            )
        return tuple(loaded)

    @staticmethod
    def _latest_result(
        cursor: Any,
        installation: _InstalledMetric,
        window: MetricWindow,
    ) -> _WindowResult | None:
        cursor.execute(
            """
            SELECT revision, content_digest
            FROM t_business_metric_window_results
            WHERE installed_metric_id = %s
              AND window_started_at = %s
              AND window_ended_at = %s
            ORDER BY revision DESC LIMIT 1
            """,
            (installation.installation_id, window.start, window.end),
        )
        row = cursor.fetchone()
        return _WindowResult(int(row[0]), row[1].strip()) if row else None

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
               first_source_observed_at, last_source_event_id,
               last_source_observed_at, result_event_id, result_observed_at,
               result_entity_instance_id, content_digest, source_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                None if last is None else last.observation.event_id,
                None if last is None else last.persisted_observed_at,
                result_event_id,
                None if result_event is None else result_event.observed_at,
                None if result_event is None else installation.output_entity_id,
                content_digest,
                Json(dict(source_summary)),
            ),
        )
        self._fault_hook("result")

    @staticmethod
    def _upsert_projection(
        cursor: Any,
        installation: _InstalledMetric,
        decision: ProjectionDecision,
        events: tuple[_PersistedEvent, ...],
        watermark: datetime | None,
    ) -> bool:
        source_summary = _source_summary(installation, decision, events)
        state = {
            "lifecycle": decision.lifecycle.value,
            "value": None if decision.value is None else str(decision.value),
            "reason": decision.reason,
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
               watermark_at, coverage, quality, estimated, state, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (installed_metric_id) DO UPDATE SET
              window_started_at = EXCLUDED.window_started_at,
              window_ended_at = EXCLUDED.window_ended_at,
              watermark_at = EXCLUDED.watermark_at,
              coverage = EXCLUDED.coverage,
              quality = EXCLUDED.quality,
              estimated = EXCLUDED.estimated,
              state = EXCLUDED.state,
              updated_at = EXCLUDED.updated_at
            WHERE ROW(
                    t_business_metric_projections.window_started_at,
                    t_business_metric_projections.window_ended_at,
                    t_business_metric_projections.watermark_at,
                    t_business_metric_projections.coverage,
                    t_business_metric_projections.quality,
                    t_business_metric_projections.estimated,
                    t_business_metric_projections.state
                  ) IS DISTINCT FROM ROW(
                    EXCLUDED.window_started_at,
                    EXCLUDED.window_ended_at,
                    EXCLUDED.watermark_at,
                    EXCLUDED.coverage,
                    EXCLUDED.quality,
                    EXCLUDED.estimated,
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
                Json(state),
                decision.updated_at,
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
    data_type: str,
    method: MetricAggregator,
) -> CounterContract | None:
    if method is not MetricAggregator.COUNTER_DELTA:
        return None
    # Schema 043 does not yet persist reset/rollover policy.  Use only the
    # storage type's upper bound and classify every decrease as ambiguous.
    maximum = Decimal((1 << 63) - 1) if data_type == "INT" else Decimal("1e308")
    return CounterContract(maximum=maximum)


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


def _candidate_windows(
    installation: _InstalledMetric,
    now: datetime,
) -> tuple[MetricWindow, ...]:
    horizon = installation.correction_horizon_seconds
    if installation.window_kind is WindowKind.ALIGNED_DAILY:
        count = max(1, math.ceil(horizon / 86400) + 1)
        unique = {
            (
                window.start,
                window.end,
            ): window
            for offset in range(count + 1)
            for window in (
                aligned_daily_window(
                    now - timedelta(days=offset),
                    installation.revision.timezone,
                ),
            )
        }
        return tuple(unique[key] for key in sorted(unique))

    seconds = installation.rolling_window_seconds
    if seconds is None:
        raise RuntimeError("rolling metric has no frozen duration")
    latest_end = now.replace(second=0, microsecond=0)
    minutes = max(1, math.ceil(horizon / 60) + 1)
    return tuple(
        rolling_window(latest_end - timedelta(minutes=offset), seconds)
        for offset in range(minutes, -1, -1)
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
    return {
        "method": installation.method.value,
        "templateDigest": installation.revision.template_digest,
        "sourceDigest": installation.revision.source_digest,
        "runtimeInstanceId": str(RUNTIME_INSTANCE_ID),
        "sourceEventIds": [str(item.observation.event_id) for item in selected],
        "timeBasis": {
            str(item.observation.event_id): item.time_basis for item in selected
        },
        "effectiveObservedAt": {
            str(item.observation.event_id): item.effective_observed_at.isoformat()
            for item in selected
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
        "value": None if decision.value is None else str(decision.value),
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
                item.persisted_observed_at,
                item.observation.event_id,
            ),
        )
    )


def _result_value(kind: ValueKind, value: Decimal | None) -> TypedValue:
    if value is None:
        raise RuntimeError("formal metric result requires a value")
    if kind is ValueKind.FLOAT:
        return TypedValue.float(float(value))
    if kind is ValueKind.INT:
        return TypedValue.integer(int(value))
    raise RuntimeError("business metric output must be numeric")


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
