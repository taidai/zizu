"""Transactional PostgreSQL boundary for alarm-configuration acceptance."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator
from uuid import UUID

import psycopg2
from psycopg2.extras import Json, register_uuid

from app.services.alarm_configuration_acceptance import (
    AlarmConfigurationAcceptance,
    AlarmConfigurationAcceptanceError,
    AlarmConfigurationAcceptanceItem,
    AlarmConfigurationAcceptanceProgress,
    AlarmConfigurationAcceptanceReport,
    RunAlarmConfigurationAcceptance,
    _digest,
    _freeze,
    _report_payload,
)
from app.services.alarm_configuration_postgres import (
    load_applied_alarm_configuration,
    load_latest_applied_alarm_configuration,
)
from app.services.alarm_runtime import AlarmEvent, AlarmTransition


ConnectionFactory = Callable[[], Any]


def _item_json(item: AlarmConfigurationAcceptanceItem) -> dict[str, Any]:
    return {
        "definition_id": str(item.definition_id),
        "definition_key": item.definition_key,
        "action": item.action,
        "status": item.status,
        "code": item.code,
        "event_id": None if item.event_id is None else str(item.event_id),
        "event_state": item.event_state,
        "transition_codes": list(item.transition_codes),
        "acknowledgement_audit_event_id": (
            None
            if item.acknowledgement_audit_event_id is None
            else str(item.acknowledgement_audit_event_id)
        ),
        "evidence": dict(item.evidence),
    }


def _report_json(report: AlarmConfigurationAcceptanceReport) -> dict[str, Any]:
    return {
        "id": str(report.id),
        "application_id": str(report.application_id),
        "installation_id": str(report.installation_id),
        "site_configuration_version": report.site_configuration_version,
        "actor": report.actor,
        "status": report.status,
        "items": [_item_json(item) for item in report.items],
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "digest": report.digest,
    }


def _report_from_json(value: dict[str, Any]) -> AlarmConfigurationAcceptanceReport:
    report = AlarmConfigurationAcceptanceReport(
        id=UUID(value["id"]),
        application_id=UUID(value["application_id"]),
        installation_id=UUID(value["installation_id"]),
        site_configuration_version=int(value["site_configuration_version"]),
        actor=value["actor"],
        status=value["status"],
        items=tuple(
            AlarmConfigurationAcceptanceItem(
                definition_id=UUID(item["definition_id"]),
                definition_key=item["definition_key"],
                action=item["action"],
                status=item["status"],
                code=item["code"],
                event_id=(
                    None if item["event_id"] is None else UUID(item["event_id"])
                ),
                event_state=item["event_state"],
                transition_codes=tuple(item["transition_codes"]),
                acknowledgement_audit_event_id=(
                    None
                    if item["acknowledgement_audit_event_id"] is None
                    else UUID(item["acknowledgement_audit_event_id"])
                ),
                evidence=_freeze(item["evidence"]),
            )
            for item in value["items"]
        ),
        started_at=datetime.fromisoformat(value["started_at"]),
        finished_at=datetime.fromisoformat(value["finished_at"]),
        digest=value["digest"],
    )
    if _digest(_report_payload(report)) != report.digest:
        raise AlarmConfigurationAcceptanceError(
            "ALARM_ACCEPTANCE_REPORT_DIGEST_INVALID"
        )
    return report


class _PostgresAcceptanceRuntime:
    """The same event/timeline query model without any mutation methods."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def list(self) -> tuple[AlarmEvent, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT ON (definition_id)
                       id, definition_id, definition_version,
                       entity_instance_id, state, severity, pending_at,
                       active_at, acknowledged_at, acknowledged_by,
                       acknowledgement_note, recovery_candidate_since,
                       recovered_at, first_observation, last_observation,
                       recovery_observation
                FROM t_alarm_events
                ORDER BY definition_id, pending_at DESC, id DESC
                """
            )
            rows = cursor.fetchall()
        return tuple(AlarmEvent(*row) for row in rows)

    def timeline(self, event_id: UUID) -> tuple[AlarmTransition, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id, from_state, to_state, occurred_at, code,
                       evidence, actor, note, audit_event_id
                FROM t_alarm_transitions
                WHERE event_id = %s
                ORDER BY occurred_at, id
                """,
                (event_id,),
            )
            rows = cursor.fetchall()
        return tuple(AlarmTransition(*row) for row in rows)


class PostgresAlarmConfigurationAcceptanceRepository:
    """Report/idempotency operations borrowed by one outer transaction."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def save(
        self,
        report: AlarmConfigurationAcceptanceReport,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AlarmConfigurationAcceptanceReport:
        value = _report_json(report)
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_alarm_configuration_reports
                  (id, application_id, installation_id,
                   site_configuration_version, actor, status, report, digest,
                   started_at, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    report.id,
                    report.application_id,
                    report.installation_id,
                    report.site_configuration_version,
                    report.actor,
                    report.status,
                    Json(value),
                    report.digest,
                    report.started_at,
                    report.finished_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO t_alarm_configuration_acceptance_idempotency
                  (actor, idempotency_key, request_digest, report_id)
                VALUES (%s, %s, %s, %s)
                """,
                (report.actor, idempotency_key, request_digest, report.id),
            )
        return report

    def get(self, report_id: UUID) -> AlarmConfigurationAcceptanceReport | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT report FROM t_alarm_configuration_reports WHERE id = %s",
                (report_id,),
            )
            row = cursor.fetchone()
        return None if row is None else _report_from_json(row[0])

    def find_idempotency(
        self,
        actor: str,
        idempotency_key: str,
    ) -> tuple[str, AlarmConfigurationAcceptanceReport] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT idempotency.request_digest, report.report
                FROM t_alarm_configuration_acceptance_idempotency idempotency
                JOIN t_alarm_configuration_reports report
                  ON report.id = idempotency.report_id
                WHERE idempotency.actor = %s
                  AND idempotency.idempotency_key = %s
                """,
                (actor, idempotency_key),
            )
            row = cursor.fetchone()
        return None if row is None else (row[0].strip(), _report_from_json(row[1]))

    def latest_passed_item(
        self,
        definition_id: UUID,
    ) -> tuple[AlarmConfigurationAcceptanceReport, AlarmConfigurationAcceptanceItem] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT stored.report
                FROM t_alarm_configuration_reports stored
                CROSS JOIN LATERAL jsonb_array_elements(stored.report->'items') item
                WHERE stored.status = 'passed'
                  AND item->>'status' = 'passed'
                  AND item->>'definition_id' = %s
                ORDER BY stored.finished_at DESC, stored.id DESC
                LIMIT 1
                """,
                (str(definition_id),),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        report = _report_from_json(row[0])
        item = next(item for item in report.items if item.definition_id == definition_id)
        return report, item


class PostgresAlarmConfigurationAcceptance:
    """Load, observe, classify, and persist through one database transaction."""

    def __init__(self, connection_factory: ConnectionFactory | None = None) -> None:
        register_uuid()
        self._connection_factory = connection_factory

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self._connection_factory is None:
            from app.services.telemetry_store import get_connection

            with get_connection() as connection:
                try:
                    yield connection
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return
        connection = self._connection_factory()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def run(
        self,
        command: RunAlarmConfigurationAcceptance,
    ) -> AlarmConfigurationAcceptanceReport:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                        (command.actor, command.idempotency_key),
                    )
                applied = load_applied_alarm_configuration(
                    connection,
                    command.application_id,
                )
                if applied is None:
                    raise AlarmConfigurationAcceptanceError(
                        "ALARM_ACCEPTANCE_APPLICATION_NOT_FOUND"
                    )
                observer = AlarmConfigurationAcceptance(
                    runtime=_PostgresAcceptanceRuntime(connection),
                    repository=PostgresAlarmConfigurationAcceptanceRepository(connection),
                )
                return observer.run(command, applied)
        except AlarmConfigurationAcceptanceError:
            raise
        except (psycopg2.Error, OSError) as error:
            raise AlarmConfigurationAcceptanceError(
                "ALARM_ACCEPTANCE_PERSISTENCE_UNAVAILABLE"
            ) from error

    def get(self, report_id: UUID) -> AlarmConfigurationAcceptanceReport:
        try:
            with self._connection() as connection:
                report = PostgresAlarmConfigurationAcceptanceRepository(
                    connection
                ).get(report_id)
                if report is None:
                    raise AlarmConfigurationAcceptanceError(
                        "ALARM_ACCEPTANCE_REPORT_NOT_FOUND"
                    )
                return report
        except AlarmConfigurationAcceptanceError:
            raise
        except (psycopg2.Error, OSError) as error:
            raise AlarmConfigurationAcceptanceError(
                "ALARM_ACCEPTANCE_PERSISTENCE_UNAVAILABLE"
            ) from error

    def progress(self) -> AlarmConfigurationAcceptanceProgress:
        try:
            with self._connection() as connection:
                applied = load_latest_applied_alarm_configuration(connection)
                if applied is None:
                    raise AlarmConfigurationAcceptanceError(
                        "ALARM_ACCEPTANCE_APPLICATION_NOT_FOUND"
                    )
                observer = AlarmConfigurationAcceptance(
                    runtime=_PostgresAcceptanceRuntime(connection),
                    repository=PostgresAlarmConfigurationAcceptanceRepository(connection),
                )
                return observer.progress(applied)
        except AlarmConfigurationAcceptanceError:
            raise
        except (psycopg2.Error, OSError) as error:
            raise AlarmConfigurationAcceptanceError(
                "ALARM_ACCEPTANCE_PERSISTENCE_UNAVAILABLE"
            ) from error
