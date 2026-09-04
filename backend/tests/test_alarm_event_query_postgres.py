"""Read-path regression against session-local tables, never live alarm rows."""
from __future__ import annotations

from contextlib import contextmanager
import asyncio
import os
import time
import unittest
from unittest.mock import patch
from uuid import UUID

import psycopg2
from psycopg2.extras import register_uuid


@unittest.skipUnless(
    os.environ.get("ZIZU_ALARM_QUERY_TEST") == "1",
    "set ZIZU_ALARM_QUERY_TEST=1 for temporary-table PostgreSQL query tests",
)
class AlarmEventQueryPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.core.config import settings

        register_uuid()
        self.fetched_rows: list[int] = []
        fetched_rows = self.fetched_rows

        class MeasuredCursor(psycopg2.extensions.cursor):
            def fetchall(self):
                rows = super().fetchall()
                fetched_rows.append(len(rows))
                return rows

        self.connection = psycopg2.connect(
            host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
            user=settings.db_user, password=settings.db_password,
            cursor_factory=MeasuredCursor,
        )
        self.addCleanup(self.connection.close)
        with self.connection.cursor() as cursor:
            # All writes target temporary tables on this connection. Rollback /
            # connection close discards them; no migration or live reset is run.
            cursor.execute("""
                CREATE TEMP TABLE t_alarm_events (
                    id uuid, definition_id uuid, definition_version text,
                    entity_instance_id uuid, state text, severity text,
                    pending_at timestamptz, active_at timestamptz,
                    acknowledged_at timestamptz, acknowledged_by text,
                    acknowledgement_note text, recovery_candidate_since timestamptz,
                    recovered_at timestamptz, first_observation jsonb,
                    last_observation jsonb, recovery_observation jsonb,
                    archived_at timestamptz, archived_by text
                );
                CREATE TEMP TABLE t_alarm_definitions (
                    id uuid, asset_id text, entity_instance_id uuid
                );
                CREATE TEMP TABLE t_entity_instances (id uuid, node_id uuid, display_name text);
                CREATE TEMP TABLE t_nodes (id uuid, name text);
                CREATE TEMP TABLE t_alarm_rule_set_revisions (
                    rule_set_key text, rules jsonb, revision integer
                );
                INSERT INTO pg_temp.t_nodes VALUES ('00000000-0000-0000-0000-000000000001', 'PCS test');
                INSERT INTO pg_temp.t_entity_instances VALUES (
                    '00000000-0000-0000-0000-000000000002',
                    '00000000-0000-0000-0000-000000000001', 'Power test'
                );
                INSERT INTO pg_temp.t_alarm_definitions VALUES (
                    '00000000-0000-0000-0000-000000000003', 'alarm.test.power',
                    '00000000-0000-0000-0000-000000000002'
                );
                INSERT INTO pg_temp.t_alarm_events (
                    id,definition_id,definition_version,entity_instance_id,state,severity,
                    pending_at,active_at,recovered_at,last_observation
                )
                SELECT md5(i::text)::uuid, '00000000-0000-0000-0000-000000000003', '1',
                    '00000000-0000-0000-0000-000000000002', 'recovered','WARNING',
                    '2026-09-01'::timestamptz + i * interval '1 second',
                    '2026-09-01'::timestamptz + i * interval '1 second',
                    '2026-09-02'::timestamptz,
                    jsonb_build_object('evidence', repeat('history', 200))
                FROM generate_series(1,6200) i;
                INSERT INTO pg_temp.t_alarm_events (
                    id,definition_id,definition_version,entity_instance_id,state,severity,pending_at,active_at
                ) VALUES
                    ('00000000-0000-0000-0000-000000000011','00000000-0000-0000-0000-000000000003','1','00000000-0000-0000-0000-000000000002','active_unacknowledged','CRITICAL','2026-08-30','2026-08-30'),
                    ('00000000-0000-0000-0000-000000000012','00000000-0000-0000-0000-000000000003','1','00000000-0000-0000-0000-000000000002','active_acknowledged','WARNING','2026-08-31','2026-08-31'),
                    ('00000000-0000-0000-0000-000000000013','00000000-0000-0000-0000-000000000003','1','00000000-0000-0000-0000-000000000002','pending','MAJOR','2026-08-29',NULL),
                    ('00000000-0000-0000-0000-000000000014','00000000-0000-0000-0000-000000000003','1','00000000-0000-0000-0000-000000000002','normal','CRITICAL','2026-09-03',NULL);
                UPDATE pg_temp.t_alarm_events
                SET archived_at='2026-09-03'::timestamptz,
                    archived_by='user:archivist'
                WHERE id=md5('1')::uuid;
            """)

        @contextmanager
        def connection():
            yield self.connection

        self.patcher = patch("app.services.telemetry_store.get_connection", connection)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_public_page_transfers_only_selected_events_not_entire_history(self) -> None:
        from app.api.alarm_events import list_alarm_events
        from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog, PostgresAlarmRepository
        from app.services.alarm_runtime import AlarmRuntime

        runtime = AlarmRuntime(PostgresAlarmDefinitionCatalog(), PostgresAlarmRepository())
        response = await list_alarm_events(
            page=1, page_size=50, state="open", severity=None,
            entity_instance_id=None, runtime=runtime,
        )
        self.assertEqual(3, response["total"])
        self.assertEqual(["active_acknowledged", "active_unacknowledged", "pending"],
                         [item["state"] for item in response["items"]])
        self.assertEqual({"active": 2, "unacknowledged": 1, "critical": 1}, response["summary"])
        self.assertLessEqual(max(self.fetched_rows), 50, "A 50-row page must not transfer 6,200 historical observations")

        for state, severity, entity, page, total, states in (
            ("recovered", None, None, 1, 6199, ["recovered", "recovered"]),
            ("archived", None, None, 1, 1, ["recovered"]),
            ("open", "CRITICAL", None, 1, 1, ["active_unacknowledged"]),
            ("active_acknowledged", None, None, 1, 1, ["active_acknowledged"]),
            (None, None, UUID(int=99), 1, 0, []),
            (None, None, None, 4000, 6202, []),
            (None, None, None, 1, 6202, ["active_acknowledged", "active_unacknowledged"]),
        ):
            with self.subTest(state=state, severity=severity, page=page, entity=entity):
                self.fetched_rows.clear()
                result = await list_alarm_events(
                    page=page, page_size=2, state=state, severity=severity,
                    entity_instance_id=entity, runtime=runtime,
                )
                self.assertEqual(total, result["total"])
                self.assertEqual(states, [item["state"] for item in result["items"]])
                self.assertEqual(response["summary"], result["summary"])
                self.assertLessEqual(max(self.fetched_rows), 2)

    async def test_slow_alarm_read_does_not_block_other_async_work(self) -> None:
        from app.api.alarm_events import list_alarm_events
        from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog, PostgresAlarmRepository
        from app.services.alarm_runtime import AlarmRuntime

        class DelayedCursor(psycopg2.extensions.cursor):
            def execute(self, *args, **kwargs):
                # Simulate a slow DB boundary, while still executing real SQL.
                time.sleep(0.04)
                return super().execute(*args, **kwargs)

        self.connection.cursor_factory = DelayedCursor
        ticks = []

        async def heartbeat():
            while True:
                await asyncio.sleep(0.005)
                ticks.append(True)

        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        try:
            result = await list_alarm_events(
                page=1, page_size=50, state="open", severity=None,
                entity_instance_id=None,
                runtime=AlarmRuntime(PostgresAlarmDefinitionCatalog(), PostgresAlarmRepository()),
            )
            self.assertEqual(3, result["total"])
            self.assertGreater(len(ticks), 0, "Alarm database reads must not stall the data-frame event loop")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
