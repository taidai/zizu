from __future__ import annotations

import os
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


if __name__ == "__main__":
    unittest.main()
