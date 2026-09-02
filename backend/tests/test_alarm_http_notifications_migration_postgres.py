from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg2
from psycopg2.extras import register_uuid


MIGRATION_060 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_060_alarm_http_notifications.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run alarm HTTP notification migration tests",
)
class AlarmHttpNotificationsMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
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

    def _connection(self):
        return psycopg2.connect(**self.connection_kwargs)

    @staticmethod
    def _apply(cursor) -> None:
        cursor.execute(MIGRATION_060.read_text(encoding="utf-8"))

    def test_migration_is_replayable_and_installs_delivery_contract(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply(cursor)
            connection.commit()
            self._apply(cursor)
            connection.commit()
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='t_alarm_notification_outbox'
                  AND column_name IN (
                    'transition_id','transition_code','configuration_id',
                    'configuration_name_snapshot','context_snapshot','status',
                    'attempt_count','cycle_attempt_count','next_attempt_at',
                    'lease_owner','lease_expires_at','last_target_display',
                    'last_http_status','last_error_code','last_error_detail',
                    'last_response_excerpt','cancelled_at','updated_at'
                  )
                ORDER BY column_name
                """
            )
            self.assertEqual(18, len(cursor.fetchall()))
            cursor.execute(
                """
                SELECT to_regclass('public.t_alarm_http_notification_configs'),
                       to_regclass('public.t_alarm_http_notification_bindings'),
                       to_regclass('public.t_alarm_notification_attempts'),
                       to_regclass('public.t_alarm_notification_retry_idempotency')
                """
            )
            self.assertEqual(
                (
                    "t_alarm_http_notification_configs",
                    "t_alarm_http_notification_bindings",
                    "t_alarm_notification_attempts",
                    "t_alarm_notification_retry_idempotency",
                ),
                cursor.fetchone(),
            )

    def test_migration_marks_legacy_rows_as_cancelled(self) -> None:
        legacy_id = uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.t_alarm_notification_outbox
                  (id,event_id,definition_id,entity_instance_id,created_at)
                VALUES (%s,%s,%s,%s,clock_timestamp())
                """,
                (legacy_id, uuid4(), uuid4(), uuid4()),
            )
            self._apply(cursor)
            cursor.execute(
                """
                SELECT status,last_error_code,cancelled_at IS NOT NULL
                FROM public.t_alarm_notification_outbox
                WHERE id=%s
                """,
                (legacy_id,),
            )
            self.assertEqual(
                ("cancelled", "LEGACY_NOTIFICATION_NOT_REPLAYED", True),
                cursor.fetchone(),
            )

    def test_transition_identity_and_context_are_required_for_new_tasks(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._apply(cursor)
            definition_id = uuid4()
            transition_id = uuid4()
            cursor.execute(
                "INSERT INTO public.t_alarm_definitions(id) VALUES (%s)",
                (definition_id,),
            )
            cursor.execute(
                "INSERT INTO public.t_alarm_transitions(id) VALUES (%s)",
                (transition_id,),
            )
            values = (
                uuid4(),
                uuid4(),
                definition_id,
                uuid4(),
                transition_id,
                "ALARM_ACTIVATED",
                '{"event":{"type":"ALARM_ACTIVATED"}}',
            )
            cursor.execute(
                """
                INSERT INTO public.t_alarm_notification_outbox
                  (id,event_id,definition_id,entity_instance_id,created_at,
                   transition_id,transition_code,context_snapshot)
                VALUES (%s,%s,%s,%s,clock_timestamp(),%s,%s,%s::jsonb)
                """,
                values,
            )
            with self.assertRaises(psycopg2.errors.UniqueViolation):
                cursor.execute(
                    """
                    INSERT INTO public.t_alarm_notification_outbox
                      (id,event_id,definition_id,entity_instance_id,created_at,
                       transition_id,transition_code,context_snapshot)
                    VALUES (%s,%s,%s,%s,clock_timestamp(),%s,%s,%s::jsonb)
                    """,
                    (uuid4(), *values[1:]),
                )
            connection.rollback()

        with self._connection() as connection, connection.cursor() as cursor:
            self._apply(cursor)
            definition_id = uuid4()
            transition_id = uuid4()
            cursor.execute(
                "INSERT INTO public.t_alarm_definitions(id) VALUES (%s)",
                (definition_id,),
            )
            cursor.execute(
                "INSERT INTO public.t_alarm_transitions(id) VALUES (%s)",
                (transition_id,),
            )
            with self.assertRaises(psycopg2.errors.CheckViolation):
                cursor.execute(
                    """
                    INSERT INTO public.t_alarm_notification_outbox
                      (id,event_id,definition_id,entity_instance_id,created_at,
                       transition_id,transition_code,context_snapshot)
                    VALUES (%s,%s,%s,%s,clock_timestamp(),%s,'ALARM_ACTIVATED',NULL)
                    """,
                    (uuid4(), uuid4(), definition_id, uuid4(), transition_id),
                )


if __name__ == "__main__":
    unittest.main()
