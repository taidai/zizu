from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg2
from psycopg2.extras import register_uuid

from app.services.alarm_http_notifications import (
    HttpNotificationDraft,
    HttpNotificationError,
    HttpSendResult,
    RequestField,
    SecretCodec,
)


MIGRATION_060 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_060_alarm_http_notifications.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run alarm HTTP notification repository tests",
)
class AlarmHttpNotificationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.services.alarm_http_notification_postgres import (
            PostgresAlarmHttpNotificationRepository,
        )

        register_uuid()
        db_name = os.environ.get("DB_NAME", "")
        if not db_name.endswith("_test"):
            raise RuntimeError("Alarm HTTP notification tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        cls.codec = SecretCodec(Fernet.generate_key().decode("ascii"))
        cls.repository_type = PostgresAlarmHttpNotificationRepository

    def setUp(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                DROP TABLE IF EXISTS public.t_alarm_notification_retry_idempotency CASCADE;
                DROP TABLE IF EXISTS public.t_alarm_notification_attempts CASCADE;
                DROP TABLE IF EXISTS public.t_alarm_http_notification_bindings CASCADE;
                DROP TABLE IF EXISTS public.t_alarm_http_notification_configs CASCADE;
                DROP TABLE IF EXISTS public.t_alarm_notification_outbox CASCADE;
                DROP TABLE IF EXISTS public.t_alarm_transitions CASCADE;
                DROP TABLE IF EXISTS public.t_alarm_definitions CASCADE;

                CREATE TABLE public.t_alarm_definitions (id uuid PRIMARY KEY);
                CREATE TABLE public.t_alarm_transitions (id uuid PRIMARY KEY);
                CREATE TABLE public.t_alarm_notification_outbox (
                    id uuid PRIMARY KEY,
                    event_id uuid NOT NULL,
                    definition_id uuid NOT NULL,
                    entity_instance_id uuid NOT NULL,
                    created_at timestamptz NOT NULL,
                    delivered_at timestamptz,
                    delivery_error text
                );
                """
            )
            cursor.execute(MIGRATION_060.read_text(encoding="utf-8"))
        self.repository = self.repository_type(self._connection, self.codec)

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    @staticmethod
    def _draft(**changes) -> HttpNotificationDraft:
        values = {
            "name": "值班群",
            "description": None,
            "method": "POST",
            "url": "https://receiver.invalid/hook?token=hidden",
            "query_params": (
                RequestField("site", "储能站", False),
                RequestField("key", "query-hidden", True),
            ),
            "headers": (
                RequestField("X-Site", "储能站", False),
                RequestField("Authorization", "Bearer hidden", True),
            ),
            "content_type": "application/json",
            "body_template": '{"type":{{event.type}}}',
            "timeout_seconds": 5,
        }
        values.update(changes)
        return HttpNotificationDraft(**values)

    def _create(self):
        return self.repository.create_config(self._draft(), "admin:test")

    def _seed_delivery(self):
        config = self._create()
        self.repository.record_test(
            config.id,
            config.current_digest,
            HttpSendResult(True, "delivered", 204, 1, None, None, None),
            "admin:test",
        )
        self.repository.set_enabled(config.id, True, "admin:test")
        definition_id = uuid4()
        transition_id = uuid4()
        notification_id = uuid4()
        now = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_alarm_definitions(id) VALUES (%s)",
                (definition_id,),
            )
            cursor.execute(
                "INSERT INTO t_alarm_transitions(id) VALUES (%s)",
                (transition_id,),
            )
            cursor.execute(
                """
                INSERT INTO t_alarm_notification_outbox
                  (id,event_id,definition_id,entity_instance_id,created_at,
                   transition_id,transition_code,configuration_id,
                   configuration_name_snapshot,context_snapshot,status,
                   next_attempt_at)
                VALUES (%s,%s,%s,%s,%s,%s,'ALARM_ACTIVATED',%s,%s,%s,
                        'pending',%s)
                """,
                (
                    notification_id,
                    uuid4(),
                    definition_id,
                    uuid4(),
                    now,
                    transition_id,
                    config.id,
                    config.name,
                    '{"notification.id":"%s","event.type":"ALARM_ACTIVATED"}'
                    % notification_id,
                    now,
                ),
            )
        return notification_id, config.id, now

    def test_claim_is_exclusive_and_expired_lease_is_recovered(self) -> None:
        notification_id, _config_id, now = self._seed_delivery()

        first = self.repository.claim_due(worker_id="worker-1", now=now)
        blocked = self.repository.claim_due(worker_id="worker-2", now=now)
        recovered = self.repository.claim_due(
            worker_id="worker-2",
            now=now + timedelta(seconds=31),
        )

        self.assertIsNotNone(first)
        self.assertIsNone(blocked)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(notification_id, recovered.id)
        self.assertEqual("worker-2", recovered.lease_owner)

    def test_complete_attempt_persists_retry_schedule_and_redacted_evidence(self) -> None:
        notification_id, _config_id, now = self._seed_delivery()
        claim = self.repository.claim_due(worker_id="worker-1", now=now)
        assert claim is not None

        self.repository.complete_attempt(
            claim,
            HttpSendResult(
                False,
                "rejected",
                500,
                12,
                "HTTP_NOTIFICATION_DELIVERY_REJECTED",
                "Remote endpoint rejected the request",
                "failure",
                "POST",
                "https://receiver.invalid/***",
            ),
            now,
        )

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,attempt_count,cycle_attempt_count,next_attempt_at,
                       lease_owner,last_target_display,last_http_status
                FROM t_alarm_notification_outbox WHERE id=%s
                """,
                (notification_id,),
            )
            delivery = cursor.fetchone()
            cursor.execute(
                """
                SELECT attempt_no,method,target_display,outcome,http_status
                FROM t_alarm_notification_attempts
                WHERE notification_id=%s
                """,
                (notification_id,),
            )
            attempt = cursor.fetchone()
        self.assertEqual("retry_wait", delivery[0])
        self.assertEqual((1, 1), delivery[1:3])
        self.assertEqual(now + timedelta(seconds=5), delivery[3])
        self.assertIsNone(delivery[4])
        self.assertEqual("https://receiver.invalid/***", delivery[5])
        self.assertEqual(500, delivery[6])
        self.assertEqual(
            (1, "POST", "https://receiver.invalid/***", "rejected", 500),
            attempt,
        )

    def test_database_and_public_model_never_contain_plaintext_secrets(self) -> None:
        created = self._create()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT row_to_json(config)::text FROM t_alarm_http_notification_configs config WHERE id=%s",
                (created.id,),
            )
            serialized = cursor.fetchone()[0]

        self.assertNotIn("token=hidden", serialized)
        self.assertNotIn("query-hidden", serialized)
        self.assertNotIn("Bearer hidden", serialized)
        self.assertNotIn("hidden", str(created))
        self.assertEqual(("key",), tuple(created.secret_query_param_names))
        self.assertEqual(("Authorization",), tuple(created.secret_header_names))

        resolved = self.repository.resolve_config(created.id)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIn("token=hidden", resolved.draft.url)
        self.assertEqual("query-hidden", resolved.draft.query_params[1].value)
        self.assertEqual("Bearer hidden", resolved.draft.headers[1].value)

    def test_successful_test_allows_enable_and_material_edit_invalidates_it(self) -> None:
        created = self._create()
        tested = self.repository.record_test(
            created.id,
            created.current_digest,
            HttpSendResult(True, "delivered", 204, 8, None, None, "ok"),
            "admin:test",
        )
        self.assertEqual(tested.current_digest, tested.tested_digest)
        enabled = self.repository.set_enabled(created.id, True, "admin:test")
        self.assertTrue(enabled.enabled)

        edited = self.repository.update_config(
            created.id,
            self._draft(timeout_seconds=6),
            "admin:test",
        )
        self.assertFalse(edited.enabled)
        self.assertIsNone(edited.tested_digest)
        self.assertIsNone(edited.tested_at)
        with self.assertRaises(HttpNotificationError) as raised:
            self.repository.set_enabled(created.id, True, "admin:test")
        self.assertEqual("HTTP_NOTIFICATION_NOT_TESTED", raised.exception.code)

    def test_descriptive_edit_preserves_test_and_sensitive_blanks_preserve_values(self) -> None:
        created = self._create()
        self.repository.record_test(
            created.id,
            created.current_digest,
            HttpSendResult(True, "delivered", 200, 5, None, None, "ok"),
            "admin:test",
        )
        self.repository.set_enabled(created.id, True, "admin:test")
        edited = self.repository.update_config(
            created.id,
            self._draft(
                name="值班群（主）",
                description="只改说明",
                url="",
                query_params=(
                    RequestField("site", "储能站", False),
                    RequestField("key", "", True),
                ),
                headers=(
                    RequestField("X-Site", "储能站", False),
                    RequestField("Authorization", "", True),
                ),
            ),
            "admin:test",
        )

        self.assertTrue(edited.enabled)
        self.assertEqual(edited.current_digest, edited.tested_digest)
        resolved = self.repository.resolve_config(created.id)
        assert resolved is not None
        self.assertIn("token=hidden", resolved.draft.url)
        self.assertEqual("query-hidden", resolved.draft.query_params[1].value)
        self.assertEqual("Bearer hidden", resolved.draft.headers[1].value)

    def test_delete_detaches_bindings_and_cancels_unfinished_tasks_atomically(self) -> None:
        created = self._create()
        definition_id = uuid4()
        notification_ids = [uuid4() for _ in range(4)]
        statuses = ("pending", "retry_wait", "failed", "delivered")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO t_alarm_definitions(id) VALUES (%s)",
                (definition_id,),
            )
            cursor.execute(
                "INSERT INTO t_alarm_http_notification_bindings(definition_id,configuration_id,created_by) VALUES (%s,%s,'admin:test')",
                (definition_id, created.id),
            )
            for notification_id, state in zip(notification_ids, statuses):
                cursor.execute(
                    """
                    INSERT INTO t_alarm_notification_outbox
                      (id,event_id,definition_id,entity_instance_id,created_at,
                       configuration_id,configuration_name_snapshot,status,
                       attempt_count,cycle_attempt_count,next_attempt_at)
                    VALUES (%s,%s,%s,%s,clock_timestamp(),%s,%s,%s,0,0,clock_timestamp())
                    """,
                    (
                        notification_id,
                        uuid4(),
                        definition_id,
                        uuid4(),
                        created.id,
                        created.name,
                        state,
                    ),
                )
        self.repository.delete_config(created.id, "admin:test")

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_alarm_http_notification_bindings WHERE definition_id=%s",
                (definition_id,),
            )
            self.assertEqual(0, cursor.fetchone()[0])
            cursor.execute(
                "SELECT status,last_error_code,configuration_id FROM t_alarm_notification_outbox ORDER BY id",
            )
            rows = cursor.fetchall()
            self.assertEqual(
                ["cancelled", "cancelled", "cancelled", "delivered"],
                sorted(row[0] for row in rows),
            )
            for status, error_code, configuration_id in rows:
                self.assertIsNone(configuration_id)
                if status == "cancelled":
                    self.assertEqual("HTTP_NOTIFICATION_DELIVERY_CANCELLED", error_code)

    def test_list_deliveries_returns_redacted_history_and_attempts(self) -> None:
        notification_id, _config_id, now = self._seed_delivery()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_alarm_notification_outbox
                SET context_snapshot=%s
                WHERE id=%s
                """,
                (
                    '{"notification.id":"%s","event.type":"ALARM_ACTIVATED",'
                    '"alarm.name":"PCS 故障","alarm.severity":"MAJOR",'
                    '"node.name":"1# PCS","entity.name":"故障状态"}'
                    % notification_id,
                    notification_id,
                ),
            )
        claim = self.repository.claim_due(worker_id="worker-1", now=now)
        assert claim is not None
        self.repository.complete_attempt(
            claim,
            HttpSendResult(
                False,
                "rejected",
                500,
                12,
                "HTTP_NOTIFICATION_DELIVERY_REJECTED",
                "Remote endpoint rejected the request",
                "failure",
                "POST",
                "https://receiver.invalid/***",
            ),
            now,
        )

        result = self.repository.list_deliveries(page=1, page_size=20)

        self.assertEqual(1, result["total"])
        item = result["items"][0]
        self.assertEqual(str(notification_id), item["id"])
        self.assertEqual("PCS 故障", item["alarm_name"])
        self.assertEqual("MAJOR", item["severity"])
        self.assertEqual("1# PCS", item["node_name"])
        self.assertEqual("故障状态", item["entity_name"])
        self.assertEqual("retry_wait", item["status"])
        self.assertEqual(1, len(item["attempts"]))
        self.assertEqual(500, item["attempts"][0]["http_status"])
        self.assertNotIn("hidden", str(result))

    def test_manual_retry_reopens_failed_delivery_and_is_idempotent(self) -> None:
        notification_id, _config_id, _now = self._seed_delivery()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_alarm_notification_outbox
                SET status='failed',attempt_count=4,cycle_attempt_count=4,
                    last_error_code='HTTP_NOTIFICATION_DELIVERY_REJECTED'
                WHERE id=%s
                """,
                (notification_id,),
            )

        first = self.repository.retry_delivery(
            notification_id,
            "engineer",
            "retry-once",
        )
        replay = self.repository.retry_delivery(
            notification_id,
            "engineer",
            "retry-once",
        )

        self.assertEqual(first, replay)
        self.assertEqual("pending", first["status"])
        self.assertEqual(4, first["attempt_count"])
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,attempt_count,cycle_attempt_count,event_id
                FROM t_alarm_notification_outbox WHERE id=%s
                """,
                (notification_id,),
            )
            row = cursor.fetchone()
        self.assertEqual(("pending", 4, 0), row[:3])
        self.assertEqual(first["event_id"], str(row[3]))

    def test_manual_retry_rejects_non_failed_or_missing_configuration(self) -> None:
        notification_id, _config_id, _now = self._seed_delivery()
        with self.assertRaises(HttpNotificationError) as not_failed:
            self.repository.retry_delivery(
                notification_id,
                "engineer",
                "retry-pending",
            )
        self.assertEqual(
            "HTTP_NOTIFICATION_RETRY_NOT_ALLOWED",
            not_failed.exception.code,
        )

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE t_alarm_notification_outbox
                SET status='failed',configuration_id=NULL
                WHERE id=%s
                """,
                (notification_id,),
            )
        with self.assertRaises(HttpNotificationError) as missing:
            self.repository.retry_delivery(
                notification_id,
                "engineer",
                "retry-missing-config",
            )
        self.assertEqual("HTTP_NOTIFICATION_NOT_FOUND", missing.exception.code)

    def test_delete_terminal_deliveries_removes_attempts_atomically(self) -> None:
        first_id, _config_id, now = self._seed_delivery()
        second_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_alarm_notification_outbox SET status='failed' WHERE id=%s",
                (first_id,),
            )
            cursor.execute(
                """
                INSERT INTO t_alarm_notification_attempts
                  (id,notification_id,attempt_no,attempted_at,method,target_display,
                   duration_ms,outcome)
                VALUES (%s,%s,1,%s,'POST','https://receiver.invalid/***',1,'rejected')
                """,
                (uuid4(), first_id, now),
            )
            cursor.execute(
                """
                INSERT INTO t_alarm_notification_outbox
                  (id,event_id,definition_id,entity_instance_id,created_at,status,
                   attempt_count,cycle_attempt_count,next_attempt_at)
                SELECT %s,%s,definition_id,%s,%s,'cancelled',0,0,%s
                FROM t_alarm_notification_outbox WHERE id=%s
                """,
                (second_id, uuid4(), uuid4(), now, now, first_id),
            )

        deleted = self.repository.delete_deliveries(
            (first_id, second_id), "engineer:test"
        )

        self.assertEqual(2, deleted)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_alarm_notification_outbox WHERE id=ANY(%s)",
                ([first_id, second_id],),
            )
            self.assertEqual(0, cursor.fetchone()[0])
            cursor.execute(
                "SELECT count(*) FROM t_alarm_notification_attempts WHERE notification_id=%s",
                (first_id,),
            )
            self.assertEqual(0, cursor.fetchone()[0])

    def test_delete_rejects_unfinished_delivery_without_deleting_anything(self) -> None:
        pending_id, _config_id, _now = self._seed_delivery()
        terminal_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_alarm_notification_outbox
                  (id,event_id,definition_id,entity_instance_id,created_at,status,
                   attempt_count,cycle_attempt_count,next_attempt_at)
                SELECT %s,%s,definition_id,%s,created_at,'delivered',0,0,next_attempt_at
                FROM t_alarm_notification_outbox WHERE id=%s
                """,
                (terminal_id, uuid4(), uuid4(), pending_id),
            )

        with self.assertRaises(HttpNotificationError) as raised:
            self.repository.delete_deliveries(
                (terminal_id, pending_id), "engineer:test"
            )

        self.assertEqual(
            "HTTP_NOTIFICATION_DELIVERY_NOT_TERMINAL", raised.exception.code
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_alarm_notification_outbox WHERE id=ANY(%s)",
                ([terminal_id, pending_id],),
            )
            self.assertEqual(2, cursor.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
