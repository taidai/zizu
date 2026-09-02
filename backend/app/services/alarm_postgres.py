"""PostgreSQL adapters for the ADR-0004 alarm runtime."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from psycopg2.extras import Json

from app.services.alarm_definitions import AlarmDefinitionPlan
from app.services.alarm_runtime import (
    AlarmDefinition,
    AlarmEvent,
    AlarmEventPresentation,
    AlarmNotification,
    AlarmRuntimeError,
    AlarmTransition,
)


@contextmanager
def _connection(transaction: Any | None = None):
    if transaction is not None:
        yield transaction
        return
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def _alarm_rule_name(
    asset_id: str,
    entity_instance_id: UUID,
    revisions: list[tuple[Any, Any]],
) -> str:
    for rule_set_key, rules in revisions:
        prefix = f"alarm.{rule_set_key}.{entity_instance_id}."
        if not asset_id.startswith(prefix):
            continue
        rule_id = asset_id[len(prefix):]
        if isinstance(rules, list):
            for rule in rules:
                if isinstance(rule, dict) and rule.get("id") == rule_id:
                    name = rule.get("name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()
    return asset_id.rsplit(".", 1)[-1] or asset_id


class PostgresAlarmDefinitionCatalog:
    """Load current definitions for dispatch and immutable versions for events."""

    def get(self, definition_id: UUID) -> AlarmDefinition | None:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT definition.id, definition.asset_id, definition.definition_version,
                           definition.entity_instance_id, definition.entity_definition_id,
                           definition.trigger_condition, definition.trigger_duration_seconds,
                           definition.recovery_condition, definition.recovery_duration_seconds,
                           definition.severity, definition.notification_throttle_seconds
                    FROM t_alarm_definitions definition
                    WHERE definition.id = %s
                    """,
                    (definition_id,),
                )
                row = cur.fetchone()
        return AlarmDefinition(*row) if row else None

    def for_entity(self, entity_instance_id: UUID) -> tuple[AlarmDefinition, ...]:
        return self._definitions("WHERE definition.entity_instance_id = %s", (entity_instance_id,))

    def all_definitions(self) -> tuple[AlarmDefinition, ...]:
        return self._definitions("", ())

    def all_versions(self) -> tuple[AlarmDefinition, ...]:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id,asset_id,definition_version,entity_instance_id,
                           entity_definition_id,trigger_condition,
                           trigger_duration_seconds,recovery_condition,
                           recovery_duration_seconds,severity,
                           notification_throttle_seconds
                    FROM t_alarm_definitions
                    ORDER BY entity_instance_id,asset_id,id
                    """
                )
                rows = cur.fetchall()
        return tuple(AlarmDefinition(*row) for row in rows)

    @staticmethod
    def _definitions(where: str, parameters: tuple[Any, ...]) -> tuple[AlarmDefinition, ...]:
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT definition.id, definition.asset_id, definition.definition_version,
                           definition.entity_instance_id, definition.entity_definition_id,
                           definition.trigger_condition, definition.trigger_duration_seconds,
                           definition.recovery_condition, definition.recovery_duration_seconds,
                           definition.severity, definition.notification_throttle_seconds
                    FROM t_alarm_definitions definition
                    JOIN t_alarm_definition_current current
                      ON current.definition_id = definition.id
                    {where}
                    ORDER BY definition.asset_id, definition.id
                    """,
                    parameters,
                )
                rows = cur.fetchall()
        return tuple(AlarmDefinition(*row) for row in rows)

    def install_definitions(
        self,
        plan: AlarmDefinitionPlan,
        transaction: Any | None = None,
    ) -> tuple[UUID, ...]:
        if transaction is None:
            raise RuntimeError(
                "alarm definition installation requires an outer transaction"
            )
        installed_ids: list[UUID] = []
        with _connection(transaction) as conn:
            with conn.cursor() as cur:
                for definition in plan.definitions:
                    digest = hashlib.sha256(
                        json.dumps(
                            {
                                "asset_id": definition.asset_id,
                                "version": definition.version,
                                "entity_instance_id": str(definition.entity_instance_id),
                                "entity_definition_id": definition.entity_definition_id,
                                "trigger": definition.trigger,
                                "trigger_duration_seconds": definition.trigger_duration_seconds,
                                "recovery": definition.recovery,
                                "recovery_duration_seconds": definition.recovery_duration_seconds,
                                "severity": definition.severity,
                                "notification_throttle_seconds": definition.notification_throttle_seconds,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    cur.execute(
                        """
                        SELECT id FROM t_alarm_definitions
                        WHERE asset_id = %s
                          AND entity_instance_id = %s
                          AND definition_version = %s
                          AND entity_definition_id = %s
                          AND trigger_condition = %s
                          AND trigger_duration_seconds = %s
                          AND recovery_condition = %s
                          AND recovery_duration_seconds = %s
                          AND severity = %s
                          AND notification_throttle_seconds = %s
                        ORDER BY created_at, id
                        LIMIT 1
                        """,
                        (
                            definition.asset_id,
                            definition.entity_instance_id,
                            definition.version,
                            definition.entity_definition_id,
                            Json(definition.trigger),
                            definition.trigger_duration_seconds,
                            Json(definition.recovery),
                            definition.recovery_duration_seconds,
                            definition.severity,
                            definition.notification_throttle_seconds,
                        ),
                    )
                    existing = cur.fetchone()
                    definition_id = existing[0] if existing else definition.id
                    if existing is None:
                        cur.execute(
                            """
                            INSERT INTO t_alarm_definitions
                              (id, asset_id, definition_version,
                               configuration_revision, entity_instance_id,
                               entity_definition_id, trigger_condition,
                               trigger_duration_seconds, recovery_condition,
                               recovery_duration_seconds, severity,
                               notification_throttle_seconds, content_digest,
                               content_digest_algorithm)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s,
                                    'sha256-v2-content')
                            """,
                            (
                                definition_id,
                                definition.asset_id,
                                definition.version,
                                definition.configuration_revision,
                                definition.entity_instance_id,
                                definition.entity_definition_id,
                                Json(definition.trigger),
                                definition.trigger_duration_seconds,
                                Json(definition.recovery),
                                definition.recovery_duration_seconds,
                                definition.severity,
                                definition.notification_throttle_seconds,
                                digest,
                            ),
                        )
                    cur.execute(
                        """
                        INSERT INTO t_alarm_definition_current
                          (asset_id, entity_instance_id, definition_id,
                           configuration_revision)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (asset_id, entity_instance_id) DO UPDATE
                        SET definition_id = EXCLUDED.definition_id,
                            configuration_revision = EXCLUDED.configuration_revision
                        WHERE t_alarm_definition_current.configuration_revision
                              <= EXCLUDED.configuration_revision
                        """,
                        (
                            definition.asset_id,
                            definition.entity_instance_id,
                            definition_id,
                            definition.configuration_revision,
                        ),
                    )
                    installed_ids.append(definition_id)
        return tuple(installed_ids)


class PostgresAlarmRepository:
    """Transaction adapter: state, transition audit and outbox commit together."""

    @contextmanager
    def transaction(self):
        with _connection() as conn:
            yield _PostgresAlarmTransaction(conn)

    def find_open(self, definition_id: UUID, entity_instance_id: UUID) -> AlarmEvent | None:
        with self.transaction() as transaction:
            return transaction.find_open(definition_id, entity_instance_id)

    def get_event(self, event_id: UUID) -> AlarmEvent | None:
        with self.transaction() as transaction:
            return transaction.get_event(event_id)

    def list_events(self) -> tuple[AlarmEvent, ...]:
        with self.transaction() as transaction:
            return transaction.list_events()

    def list_open_for_entities(
        self,
        entity_instance_ids: frozenset[UUID],
    ) -> tuple[AlarmEvent, ...]:
        if not entity_instance_ids:
            return ()
        with self.transaction() as transaction:
            return transaction.list_open_for_entities(entity_instance_ids)

    def describe_events(
        self,
        events: tuple[AlarmEvent, ...],
    ) -> dict[UUID, AlarmEventPresentation]:
        if not events:
            return {}
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event.id, node.name, entity.display_name,
                           definition.asset_id, definition.entity_instance_id
                    FROM t_alarm_events event
                    JOIN t_alarm_definitions definition
                      ON definition.id = event.definition_id
                    LEFT JOIN t_entity_instances entity
                      ON entity.id = event.entity_instance_id
                    LEFT JOIN t_nodes node ON node.id = entity.node_id
                    WHERE event.id = ANY(%s::uuid[])
                    """,
                    ([str(event.id) for event in events],),
                )
                rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT rule_set_key, rules
                    FROM t_alarm_rule_set_revisions
                    ORDER BY revision DESC
                    """
                )
                revisions = cur.fetchall()
        result: dict[UUID, AlarmEventPresentation] = {}
        for event_id, node_name, entity_name, asset_id, entity_instance_id in rows:
            result[event_id] = AlarmEventPresentation(
                str(node_name or "未命名节点"),
                str(entity_name or "未命名实体"),
                _alarm_rule_name(str(asset_id), entity_instance_id, revisions),
            )
        return result

    def transitions(self, event_id: UUID) -> tuple[AlarmTransition, ...]:
        with self.transaction() as transaction:
            return transaction.transitions(event_id)

    def save_event(self, event: AlarmEvent) -> AlarmEvent:
        with self.transaction() as transaction:
            return transaction.save_event(event)

    def append_transition(self, transition: AlarmTransition) -> UUID | None:
        with self.transaction() as transaction:
            return transaction.append_transition(transition)

    def last_notification_at(self, definition_id: UUID, entity_instance_id: UUID) -> datetime | None:
        with self.transaction() as transaction:
            return transaction.last_notification_at(definition_id, entity_instance_id)

    def enqueue_notification(self, notification: AlarmNotification) -> None:
        with self.transaction() as transaction:
            transaction.enqueue_notification(notification)

    def notification_configuration(
        self,
        definition_id: UUID,
    ) -> tuple[UUID, str] | None:
        with self.transaction() as transaction:
            return transaction.notification_configuration(definition_id)

    def has_activation_notification(self, event_id: UUID) -> bool:
        with self.transaction() as transaction:
            return transaction.has_activation_notification(event_id)


class _PostgresAlarmTransaction:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def lock_stream(self, definition_id: UUID, entity_instance_id: UUID) -> None:
        """Serialize a first trigger too, when no event row exists to lock yet."""
        with self._connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (str(definition_id), str(entity_instance_id)),
            )

    def begin_committed_frame(
        self,
        consumer_key: str,
        frame_id: UUID,
        frame_sequence: int,
        configuration_revision: int,
    ) -> bool:
        with self._connection.cursor() as cur:
            cur.execute(
                "SELECT current_revision FROM t_configuration_state "
                "WHERE singleton=TRUE FOR SHARE"
            )
            row = cur.fetchone()
            current_revision = None if row is None else int(row[0])
            if current_revision != configuration_revision:
                raise AlarmRuntimeError(
                    "ALARM_FRAME_CONFIGURATION_MISMATCH",
                    "Alarm frame does not belong to the active configuration",
                )
            cur.execute(
                """
                INSERT INTO t_committed_frame_consumers
                  (consumer_key,frame_id,frame_sequence,configuration_revision)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (consumer_key,frame_id) DO NOTHING
                RETURNING frame_id
                """,
                (
                    consumer_key,
                    frame_id,
                    frame_sequence,
                    configuration_revision,
                ),
            )
            return cur.fetchone() is not None

    def find_open(self, definition_id: UUID, entity_instance_id: UUID) -> AlarmEvent | None:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, definition_id, definition_version, entity_instance_id,
                       state, severity, pending_at, active_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note, recovery_candidate_since,
                       recovered_at, first_observation, last_observation, recovery_observation
                FROM t_alarm_events
                WHERE definition_id = %s AND entity_instance_id = %s
                  AND state IN ('pending', 'active_unacknowledged', 'active_acknowledged')
                FOR UPDATE
                """,
                (definition_id, entity_instance_id),
            )
            rows = cur.fetchall()
        if len(rows) > 1:
            raise AlarmRuntimeError(
                "ALARM_EVENT_INTEGRITY_ERROR",
                "Alarm definition and entity instance have more than one open event",
            )
        return _event(rows[0]) if rows else None

    def get_event(self, event_id: UUID) -> AlarmEvent | None:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, definition_id, definition_version, entity_instance_id,
                       state, severity, pending_at, active_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note, recovery_candidate_since,
                       recovered_at, first_observation, last_observation, recovery_observation
                FROM t_alarm_events WHERE id = %s FOR UPDATE
                """,
                (event_id,),
            )
            row = cur.fetchone()
        return _event(row) if row else None

    def list_events(self) -> tuple[AlarmEvent, ...]:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, definition_id, definition_version, entity_instance_id,
                       state, severity, pending_at, active_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note, recovery_candidate_since,
                       recovered_at, first_observation, last_observation, recovery_observation
                FROM t_alarm_events ORDER BY pending_at DESC, id DESC
                """
            )
            rows = cur.fetchall()
        return tuple(_event(row) for row in rows)

    def list_open_for_entities(
        self,
        entity_instance_ids: frozenset[UUID],
    ) -> tuple[AlarmEvent, ...]:
        if not entity_instance_ids:
            return ()
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, definition_id, definition_version, entity_instance_id,
                       state, severity, pending_at, active_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note, recovery_candidate_since,
                       recovered_at, first_observation, last_observation, recovery_observation
                FROM t_alarm_events
                WHERE entity_instance_id = ANY(%s::uuid[])
                  AND state IN ('pending', 'active_unacknowledged', 'active_acknowledged')
                ORDER BY pending_at DESC, id DESC
                """,
                ([str(entity_id) for entity_id in entity_instance_ids],),
            )
            rows = cur.fetchall()
        return tuple(_event(row) for row in rows)

    def transitions(self, event_id: UUID) -> tuple[AlarmTransition, ...]:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT event_id, from_state, to_state, occurred_at, code,
                       evidence, actor, note, audit_event_id, id
                FROM t_alarm_transitions WHERE event_id = %s
                ORDER BY occurred_at, id
                """,
                (event_id,),
            )
            rows = cur.fetchall()
        return tuple(AlarmTransition(*row) for row in rows)

    def save_event(self, event: AlarmEvent) -> AlarmEvent:
        with self._connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM t_alarm_events WHERE id = %s",
                (event.id,),
            )
            exists = cur.fetchone() is not None
            values = (
                event.definition_id,
                event.definition_version,
                event.entity_instance_id,
                event.state,
                event.severity,
                event.pending_at,
                event.active_at,
                event.acknowledged_at,
                event.acknowledged_by,
                event.acknowledgement_note,
                event.recovery_candidate_since,
                event.recovered_at,
                Json(event.first_observation) if event.first_observation is not None else None,
                Json(event.last_observation) if event.last_observation is not None else None,
                Json(event.recovery_observation) if event.recovery_observation is not None else None,
            )
            if not exists:
                cur.execute(
                    """
                    INSERT INTO t_alarm_events
                      (id, definition_id, definition_version, entity_instance_id,
                       state, severity, pending_at, active_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note, recovery_candidate_since,
                       recovered_at, first_observation, last_observation, recovery_observation)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (event.id, *values),
                )
            else:
                cur.execute(
                    """
                    UPDATE t_alarm_events
                    SET state = %s, severity = %s, active_at = %s,
                        acknowledged_at = %s, acknowledged_by = %s,
                        acknowledgement_note = %s, recovery_candidate_since = %s,
                        recovered_at = %s, first_observation = %s,
                        last_observation = %s, recovery_observation = %s
                    WHERE id = %s
                    """,
                    (
                        event.state,
                        event.severity,
                        event.active_at,
                        event.acknowledged_at,
                        event.acknowledged_by,
                        event.acknowledgement_note,
                        event.recovery_candidate_since,
                        event.recovered_at,
                        Json(event.first_observation) if event.first_observation is not None else None,
                        Json(event.last_observation) if event.last_observation is not None else None,
                        Json(event.recovery_observation) if event.recovery_observation is not None else None,
                        event.id,
                    ),
                )
        return event

    def append_transition(self, transition: AlarmTransition) -> UUID:
        audit_event_id = transition.audit_event_id or uuid4()
        with self._connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO t_audit_events
                  (id, event, outcome, reason, actor, target, details)
                VALUES (%s, 'alarm.transition', %s, %s, %s, %s, %s)
                """,
                (
                    audit_event_id,
                    transition.to_state,
                    transition.code,
                    transition.actor,
                    f"alarm-event:{transition.event_id}",
                    Json({"from_state": transition.from_state, "note": transition.note}),
                ),
            )
            cur.execute(
                """
                INSERT INTO t_alarm_transitions
                  (id,event_id, audit_event_id, from_state, to_state, occurred_at,
                   code, evidence, actor, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    transition.id,
                    transition.event_id,
                    audit_event_id,
                    transition.from_state,
                    transition.to_state,
                    transition.occurred_at,
                    transition.code,
                    Json(transition.evidence) if transition.evidence is not None else None,
                    transition.actor,
                    transition.note,
                ),
            )
        return audit_event_id

    def last_notification_at(self, definition_id: UUID, entity_instance_id: UUID) -> datetime | None:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT max(created_at) FROM t_alarm_notification_outbox
                WHERE definition_id = %s AND entity_instance_id = %s
                  AND transition_code = 'ALARM_ACTIVATED'
                """,
                (definition_id, entity_instance_id),
            )
            row = cur.fetchone()
        return row[0] if row and row[0] else None

    def enqueue_notification(self, notification: AlarmNotification) -> None:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO t_alarm_notification_outbox
                  (id,transition_id,transition_code,event_id,definition_id,
                   entity_instance_id,configuration_id,
                   configuration_name_snapshot,context_snapshot,status,
                   next_attempt_at,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)
                ON CONFLICT(transition_id) WHERE transition_id IS NOT NULL
                DO NOTHING
                """,
                (
                    notification.id,
                    notification.transition_id,
                    notification.transition_code,
                    notification.event_id,
                    notification.definition_id,
                    notification.entity_instance_id,
                    notification.configuration_id,
                    notification.configuration_name,
                    Json(notification.context_snapshot),
                    notification.created_at,
                    notification.created_at,
                    notification.created_at,
                ),
            )

    def notification_configuration(
        self,
        definition_id: UUID,
    ) -> tuple[UUID, str] | None:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT binding.configuration_id,config.name
                FROM t_alarm_http_notification_bindings binding
                JOIN t_alarm_http_notification_configs config
                  ON config.id=binding.configuration_id
                WHERE binding.definition_id=%s
                """,
                (definition_id,),
            )
            row = cur.fetchone()
        return None if row is None else (row[0], row[1])

    def has_activation_notification(self, event_id: UUID) -> bool:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS(
                  SELECT 1 FROM t_alarm_notification_outbox
                  WHERE event_id=%s AND transition_code='ALARM_ACTIVATED'
                )
                """,
                (event_id,),
            )
            return bool(cur.fetchone()[0])


def _event(row: tuple[Any, ...]) -> AlarmEvent:
    return AlarmEvent(*row)
