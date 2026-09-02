from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg2

from app.services.alarm_configuration import (
    AlarmConfiguration,
    AlarmRule,
    ApplyAlarmConfigurationPlan,
    EntitySelection,
    PlanAlarmConfiguration,
)
from app.services.alarm_configuration_postgres import (
    PostgresAlarmConfigurationRepository,
)
from app.services.alarm_http_notification_postgres import (
    PostgresAlarmHttpNotificationRepository,
)
from app.services.alarm_http_notifications import (
    HttpNotificationDraft,
    HttpSendResult,
    SecretCodec,
)
from app.services.alarm_postgres import (
    PostgresAlarmDefinitionCatalog,
    PostgresAlarmRepository,
)
from app.services.alarm_runtime import AlarmObservation, AlarmRuntime
from tests import test_alarm_configuration_postgres
from tests import test_node_data_trunk_hard_cut_migration_postgres


MIGRATION_060 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_060_alarm_http_notifications.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run alarm runtime PostgreSQL tests",
)
class AlarmRuntimePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": os.environ["DB_NAME"],
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        if not cls.kwargs["dbname"].endswith("_test"):
            raise RuntimeError("Alarm runtime tests require a *_test database")

    def setUp(self) -> None:
        self.node_id = uuid4()
        self.entity_id = uuid4()
        with psycopg2.connect(**self.kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration = test_node_data_trunk_hard_cut_migration_postgres.NodeDataTrunkHardCutMigrationPostgresTest
                migration._reset_through_043(cursor)
                installation_id, _ = test_alarm_configuration_postgres._PostgresAlarmConfigurationTestBase._insert_installed_site(cursor)
                device_id = uuid4()
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,source_catalog_key) VALUES (%s,'PCS-01','PCS-01')",
                    (str(self.node_id),),
                )
                cursor.execute(
                    "INSERT INTO t_device_instances(id,identity_installation_id,slot_id,instance_key,device_category,display_name,node_id) VALUES (%s,%s,'pcs','PCS-01','pcs','PCS 01',%s)",
                    (str(device_id), installation_id, str(self.node_id)),
                )
                cursor.execute("SET session_replication_role=replica")
                cursor.execute(
                    "INSERT INTO t_entity_instances(id,device_instance_id,definition_id,display_name,data_type,unit,direction,freshness_seconds,source_kind) VALUES (%s,%s,'pcs.activePower','有功功率','FLOAT','kW','R',30,'point_processing')",
                    (str(self.entity_id), str(device_id)),
                )
                cursor.execute("SET session_replication_role=origin")
                migration._apply_044(cursor)
                cursor.execute(MIGRATION_060.read_text(encoding="utf-8"))

    def test_activation_and_recovery_enqueue_committed_context_and_rollback_together(self) -> None:
        connection_factory = lambda: psycopg2.connect(**self.kwargs)
        notifications = PostgresAlarmHttpNotificationRepository(
            connection_factory,
            SecretCodec(Fernet.generate_key().decode("ascii")),
        )
        config = notifications.create_config(
            HttpNotificationDraft(
                "值班群",
                None,
                "POST",
                "https://receiver.invalid/hook",
                (),
                (),
                "application/json",
                '{"type":{{event.type}}}',
                5,
            ),
            "operator:test",
        )
        notifications.record_test(
            config.id,
            config.current_digest,
            HttpSendResult(True, "delivered", 204, 1, None, None, None),
            "operator:test",
        )
        notifications.set_enabled(config.id, True, "operator:test")
        configuration = AlarmConfiguration(
            PostgresAlarmConfigurationRepository(connection_factory)
        )
        rule_set = configuration.create_rule_set(
            key="pcs-power",
            name="PCS 功率",
            rules=(
                AlarmRule(
                    "high",
                    "功率越限",
                    "MAJOR",
                    {"operator": "gt", "value": 100},
                    0,
                    {"operator": "lte", "value": 90},
                    0,
                    60,
                    "kW",
                    http_notification_config_id=config.id,
                ),
            ),
            actor="operator:test",
        )
        plan = configuration.plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(self.entity_id,)),
                rule_set.rule_set_id,
                rule_set.revision,
                "operator:test",
            )
        )
        applied = configuration.apply(
            ApplyAlarmConfigurationPlan(
                plan.id,
                plan.digest,
                "alarm-runtime-bound-1",
                "operator:test",
            )
        )
        definition_id = applied.definition_ids[0]

        from app.services.telemetry_store import close_db_pool, init_db_pool

        init_db_pool(1, 2)
        try:
            runtime = AlarmRuntime(
                PostgresAlarmDefinitionCatalog(),
                PostgresAlarmRepository(),
            )
            started = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)

            def observation(value: float, moment: datetime) -> AlarmObservation:
                return AlarmObservation(
                    definition_id=definition_id,
                    entity_instance_id=self.entity_id,
                    observed_at=moment,
                    value=value,
                    quality=192,
                    source_kind="committed_l2",
                    source_ref="frame:test",
                    evidence={
                        "node_id": str(self.node_id),
                        "node_name": "PCS-01",
                        "node_path": "储能/PCS-01",
                        "entity_name": "有功功率",
                        "entity_unit": "kW",
                        "alarm_name": "功率越限",
                    },
                )

            activated = runtime.submit(observation(101, started))
            recovered = runtime.submit(
                observation(90, started + timedelta(seconds=1))
            )
            self.assertTrue(activated.notification_created)
            self.assertTrue(recovered.notification_created)

            with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT outbox.transition_code,outbox.status,
                           outbox.context_snapshot,transition.id=outbox.transition_id
                    FROM t_alarm_notification_outbox outbox
                    JOIN t_alarm_transitions transition
                      ON transition.id=outbox.transition_id
                    ORDER BY outbox.created_at,outbox.id
                    """
                )
                rows = cursor.fetchall()
            self.assertEqual(
                ["ALARM_ACTIVATED", "ALARM_RECOVERED"],
                [row[0] for row in rows],
            )
            self.assertTrue(all(row[1] == "pending" for row in rows))
            self.assertTrue(all(row[3] for row in rows))
            self.assertEqual("PCS-01", rows[0][2]["node.name"])
            self.assertEqual(101, rows[0][2]["entity.value"])

            with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_events")
                event_count = cursor.fetchone()[0]
                cursor.execute(
                    """
                    CREATE OR REPLACE FUNCTION fail_test_alarm_notification()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'test outbox failure'; END $$;
                    CREATE TRIGGER trg_fail_test_alarm_notification
                    BEFORE INSERT ON t_alarm_notification_outbox
                    FOR EACH ROW EXECUTE FUNCTION fail_test_alarm_notification();
                    """
                )
            with self.assertRaises(psycopg2.Error):
                runtime.submit(
                    observation(101, started + timedelta(seconds=120))
                )
            with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_events")
                self.assertEqual(event_count, cursor.fetchone()[0])
        finally:
            close_db_pool()


if __name__ == "__main__":
    unittest.main()
