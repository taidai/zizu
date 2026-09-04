from __future__ import annotations

import os
from pathlib import Path
import unittest
from datetime import datetime, timezone
from uuid import uuid4

import psycopg2

from app.services.alarm_configuration import (
    AlarmConfiguration,
    AlarmConfigurationError,
    AlarmRule,
    ApplyAlarmConfigurationPlan,
    EntitySelection,
    PlanAlarmConfiguration,
)
from app.services.alarm_configuration_postgres import PostgresAlarmConfigurationRepository
from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog, PostgresAlarmRepository
from app.services.alarm_runtime import AlarmObservation, AlarmRuntime
from app.services.alarm_http_notifications import (
    HttpNotificationDraft,
    HttpSendResult,
    SecretCodec,
)
from app.services.alarm_http_notification_postgres import (
    PostgresAlarmHttpNotificationRepository,
)
from cryptography.fernet import Fernet
from tests import test_alarm_configuration_postgres
from tests import test_node_data_trunk_hard_cut_migration_postgres


@unittest.skipUnless(os.environ.get("ZIZU_POSTGRES_TEST") == "1", "set ZIZU_POSTGRES_TEST=1")
class AlarmConfigurationL2PostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kwargs = {
            "host": os.environ["DB_HOST"], "port": int(os.environ["DB_PORT"]),
            "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        if not cls.kwargs["dbname"].endswith("_test"):
            raise RuntimeError("requires *_test database")

    def setUp(self) -> None:
        self.node_id, self.entity_id = uuid4(), uuid4()
        with psycopg2.connect(**self.kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration = test_node_data_trunk_hard_cut_migration_postgres.NodeDataTrunkHardCutMigrationPostgresTest
                migration._reset_through_043(cursor)
                installation_id, _ = test_alarm_configuration_postgres._PostgresAlarmConfigurationTestBase._insert_installed_site(cursor)
                device_id = uuid4()
                cursor.execute("INSERT INTO t_nodes(id,name,source_catalog_key) VALUES (%s,'PCS-01','PCS-01')", (str(self.node_id),))
                cursor.execute("INSERT INTO t_device_instances(id,identity_installation_id,slot_id,instance_key,device_category,display_name,node_id) VALUES (%s,%s,'pcs','PCS-01','pcs','PCS 01',%s)", (str(device_id), installation_id, str(self.node_id)))
                cursor.execute("SET session_replication_role=replica")
                cursor.execute("INSERT INTO t_entity_instances(id,device_instance_id,definition_id,display_name,data_type,unit,direction,freshness_seconds,source_kind) VALUES (%s,%s,'pcs.activePower','有功功率','FLOAT','kW','R',30,'point_processing')", (str(self.entity_id), str(device_id)))
                cursor.execute("SET session_replication_role=origin")
                migration._apply_044(cursor)
                cursor.execute(
                    (
                        Path(__file__).resolve().parents[2]
                        / "init-db"
                        / "migration_060_alarm_http_notifications.sql"
                    ).read_text(encoding="utf-8")
                )
                cursor.execute(
                    (
                        Path(__file__).resolve().parents[2]
                        / "init-db"
                        / "migration_061_alarm_record_archiving.sql"
                    ).read_text(encoding="utf-8")
                )

    def test_apply_writes_definition_and_http_binding_in_one_transaction(self) -> None:
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

        service = AlarmConfiguration(
            PostgresAlarmConfigurationRepository(connection_factory)
        )
        rule_set = service.create_rule_set(
            key="pcs-power",
            name="PCS 功率",
            rules=(
                AlarmRule(
                    "high",
                    "功率越限",
                    "MAJOR",
                    {"operator": "gt", "value": 90},
                    0,
                    {"operator": "lt", "value": 85},
                    0,
                    60,
                    "kW",
                    http_notification_config_id=config.id,
                ),
            ),
            actor="operator:test",
        )
        plan = service.plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(self.entity_id,)),
                rule_set.rule_set_id,
                rule_set.revision,
                "operator:test",
            )
        )
        result = service.apply(
            ApplyAlarmConfigurationPlan(
                plan.id,
                plan.digest,
                "alarm-bound-1",
                "operator:test",
            )
        )

        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT configuration_id FROM t_alarm_http_notification_bindings WHERE definition_id=%s",
                (result.definition_ids[0],),
            )
            self.assertEqual(config.id, cursor.fetchone()[0])
        current = service.repository.current_configuration()
        self.assertEqual(
            str(config.id),
            next(iter(current["definitions"].values()))["payload"]["rule"][
                "http_notification_config_id"
            ],
        )

    def test_disabled_rule_set_archives_without_deleting_revisions(self) -> None:
        service = AlarmConfiguration(
            PostgresAlarmConfigurationRepository(
                lambda: psycopg2.connect(**self.kwargs)
            )
        )
        revision = service.create_rule_set(
            key="archive-safe",
            name="可归档规则",
            rules=(
                AlarmRule(
                    "high",
                    "功率越限",
                    "MAJOR",
                    {"operator": "gt", "value": 90},
                    0,
                    {"operator": "lt", "value": 85},
                    0,
                    60,
                    "kW",
                ),
            ),
            actor="operator:test",
        )

        service.archive_rule_set(revision.rule_set_id, "operator:test")

        self.assertEqual((), service.list_rule_set_revisions())
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT archived_by FROM t_alarm_rule_sets WHERE id=%s",
                (revision.rule_set_id,),
            )
            self.assertEqual("operator:test", cursor.fetchone()[0])
            cursor.execute(
                "SELECT count(*) FROM t_alarm_rule_set_revisions WHERE rule_set_id=%s",
                (revision.rule_set_id,),
            )
            self.assertEqual(1, cursor.fetchone()[0])

    def test_active_rule_set_cannot_be_archived(self) -> None:
        service = AlarmConfiguration(
            PostgresAlarmConfigurationRepository(
                lambda: psycopg2.connect(**self.kwargs)
            )
        )
        revision = service.create_rule_set(
            key="archive-active",
            name="生效规则",
            rules=(
                AlarmRule(
                    "high",
                    "功率越限",
                    "MAJOR",
                    {"operator": "gt", "value": 90},
                    0,
                    {"operator": "lt", "value": 85},
                    0,
                    60,
                    "kW",
                ),
            ),
            actor="operator:test",
        )
        plan = service.plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(self.entity_id,)),
                revision.rule_set_id,
                revision.revision,
                "operator:test",
            )
        )
        service.apply(
            ApplyAlarmConfigurationPlan(
                plan.id,
                plan.digest,
                "archive-active-apply",
                "operator:test",
            )
        )

        with self.assertRaises(AlarmConfigurationError) as raised:
            service.archive_rule_set(revision.rule_set_id, "operator:test")

        self.assertEqual("ALARM_RULE_SET_ACTIVE", str(raised.exception))

    def test_ready_plan_cannot_apply_after_rule_set_is_archived(self) -> None:
        service = AlarmConfiguration(
            PostgresAlarmConfigurationRepository(
                lambda: psycopg2.connect(**self.kwargs)
            )
        )
        revision = service.create_rule_set(
            key="archive-ready-plan",
            name="待发布后归档",
            rules=(
                AlarmRule(
                    "high",
                    "功率越限",
                    "MAJOR",
                    {"operator": "gt", "value": 90},
                    0,
                    {"operator": "lt", "value": 85},
                    0,
                    60,
                    "kW",
                ),
            ),
            actor="operator:test",
        )
        plan = service.plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(self.entity_id,)),
                revision.rule_set_id,
                revision.revision,
                "operator:test",
            )
        )
        service.archive_rule_set(revision.rule_set_id, "operator:test")

        with self.assertRaises(AlarmConfigurationError) as raised:
            service.apply(
                ApplyAlarmConfigurationPlan(
                    plan.id,
                    plan.digest,
                    "archive-ready-plan-apply",
                    "operator:test",
                )
            )

        self.assertEqual("ALARM_RULE_SET_NOT_FOUND", str(raised.exception))
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_alarm_definition_current")
            self.assertEqual(0, cursor.fetchone()[0])

    def test_apply_rechecks_notification_after_plan_and_rolls_back_publication(self) -> None:
        config_id = uuid4()
        digest = "d" * 64
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_alarm_http_notification_configs
                  (id,name,method,encrypted_url,url_display,content_type,
                   current_digest,tested_digest,tested_at,enabled,
                   created_by,updated_by)
                VALUES (%s,'值班群','POST','cipher','https://receiver.invalid/***',
                        'application/json',%s,%s,clock_timestamp(),TRUE,
                        'operator:test','operator:test')
                """,
                (str(config_id), digest, digest),
            )
        service = AlarmConfiguration(
            PostgresAlarmConfigurationRepository(
                lambda: psycopg2.connect(**self.kwargs)
            )
        )
        rule_set = service.create_rule_set(
            key="pcs-power",
            name="PCS 功率",
            rules=(
                AlarmRule(
                    "high",
                    "功率越限",
                    "MAJOR",
                    {"operator": "gt", "value": 90},
                    0,
                    {"operator": "lt", "value": 85},
                    0,
                    60,
                    "kW",
                    http_notification_config_id=config_id,
                ),
            ),
            actor="operator:test",
        )
        plan = service.plan(
            PlanAlarmConfiguration(
                EntitySelection(entity_instance_ids=(self.entity_id,)),
                rule_set.rule_set_id,
                rule_set.revision,
                "operator:test",
            )
        )
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_alarm_http_notification_configs SET enabled=FALSE WHERE id=%s",
                (config_id,),
            )

        with self.assertRaises(AlarmConfigurationError) as raised:
            service.apply(
                ApplyAlarmConfigurationPlan(
                    plan.id,
                    plan.digest,
                    "alarm-bound-stale-1",
                    "operator:test",
                )
            )
        self.assertEqual("HTTP_NOTIFICATION_DISABLED", str(raised.exception))
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_alarm_http_notification_bindings")
            self.assertEqual(0, cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM t_alarm_definitions")
            self.assertEqual(0, cursor.fetchone()[0])

    def test_apply_advances_one_revision_and_installs_l2_alarm(self) -> None:
        service = AlarmConfiguration(PostgresAlarmConfigurationRepository(lambda: psycopg2.connect(**self.kwargs)))
        rule_set = service.create_rule_set(
            key="pcs-power", name="PCS 功率",
            rules=(AlarmRule("high", "功率越限", "MAJOR", {"operator": "gt", "value": 90}, 0, {"operator": "lt", "value": 85}, 0, 60, "kW"),),
            actor="operator:test",
        )
        plan = service.plan(PlanAlarmConfiguration(EntitySelection(entity_instance_ids=(self.entity_id,)), rule_set.rule_set_id, 1, "operator:test"))
        result = service.apply(ApplyAlarmConfigurationPlan(plan.id, plan.digest, "alarm-apply-1", "operator:test"))
        self.assertEqual(result.configuration_revision, plan.base_configuration_revision + 1)
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT entity_instance_id,configuration_revision FROM t_alarm_definitions WHERE id=%s", (result.definition_ids[0],))
            self.assertEqual(cursor.fetchone(), (self.entity_id, result.configuration_revision))
            cursor.execute("SELECT count(*) FROM t_configuration_audit WHERE configuration_revision=%s AND resource_kind='alarm_configuration'", (result.configuration_revision,))
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_empty_revision_disables_group_without_losing_reenable_target(self) -> None:
        service = AlarmConfiguration(PostgresAlarmConfigurationRepository(lambda: psycopg2.connect(**self.kwargs)))
        rule_set = service.create_rule_set(
            key="pcs-power", name="PCS 功率",
            rules=(AlarmRule("high", "功率越限", "MAJOR", {"operator": "gt", "value": 90}, 0, {"operator": "lt", "value": 85}, 0, 60, "kW"),),
            actor="operator:test",
        )
        enabled_plan = service.plan(PlanAlarmConfiguration(EntitySelection(entity_instance_ids=(self.entity_id,)), rule_set.rule_set_id, 1, "operator:test"))
        service.apply(ApplyAlarmConfigurationPlan(enabled_plan.id, enabled_plan.digest, "alarm-enable-1", "operator:test"))
        empty = service.create_rule_set_revision(rule_set_id=rule_set.rule_set_id, rules=(), actor="operator:test")
        disabled_plan = service.plan(PlanAlarmConfiguration(EntitySelection(entity_instance_ids=(self.entity_id,)), empty.rule_set_id, empty.revision, "operator:test"))
        service.apply(ApplyAlarmConfigurationPlan(disabled_plan.id, disabled_plan.digest, "alarm-disable-1", "operator:test"))

        group = service.list_rule_groups()[0]
        self.assertEqual(2, group.latest_revision)
        self.assertEqual(1, group.last_non_empty_revision)
        self.assertEqual((self.entity_id,), group.entity_instance_ids)
        self.assertEqual((), group.enabled_entity_instance_ids)
        self.assertEqual(1, group.last_published_revision)

    def test_unpublished_and_unapplied_revisions_do_not_replace_the_reenable_target(self) -> None:
        from dataclasses import replace

        factory = lambda: psycopg2.connect(**self.kwargs)
        service = AlarmConfiguration(PostgresAlarmConfigurationRepository(factory))
        original = AlarmRule("high", "功率越限", "MAJOR", {"operator": "gt", "value": 90}, 0, {"operator": "lt", "value": 85}, 0, 60, "kW")
        first = service.create_rule_set(key="pcs-power", name="PCS 功率", rules=(original,), actor="operator:test")
        selection = EntitySelection(entity_instance_ids=(self.entity_id,))
        plan = service.plan(PlanAlarmConfiguration(selection, first.rule_set_id, 1, "operator:test"))
        service.apply(ApplyAlarmConfigurationPlan(plan.id, plan.digest, "publish-original", "operator:test"))
        draft = service.create_rule_set_revision(rule_set_id=first.rule_set_id, rules=(replace(original, trigger={"operator": "gt", "value": 120}),), actor="operator:test")
        service.plan(PlanAlarmConfiguration(selection, first.rule_set_id, draft.revision, "operator:test"))
        empty = service.create_rule_set_revision(rule_set_id=first.rule_set_id, rules=(), actor="operator:test")
        stop = service.plan(PlanAlarmConfiguration(selection, first.rule_set_id, empty.revision, "operator:test"))
        service.apply(ApplyAlarmConfigurationPlan(stop.id, stop.digest, "stop-original", "operator:test"))

        restarted = AlarmConfiguration(PostgresAlarmConfigurationRepository(factory))
        group = restarted.list_rule_groups()[0]
        self.assertEqual(2, group.last_non_empty_revision)
        self.assertEqual(1, group.last_published_revision)
        resume = restarted.plan(PlanAlarmConfiguration(selection, first.rule_set_id, group.last_published_revision, "operator:test"))
        restarted.apply(ApplyAlarmConfigurationPlan(resume.id, resume.digest, "resume-original", "operator:test"))
        current = restarted.repository.current_configuration()
        self.assertEqual({"operator": "gt", "value": 90}, next(iter(current["definitions"].values()))["payload"]["rule"]["trigger"])
        self.assertEqual(2, restarted.list_rule_groups()[0].last_non_empty_revision)

    def test_legacy_publication_does_not_override_a_newer_formal_revision(self) -> None:
        from dataclasses import replace

        factory = lambda: psycopg2.connect(**self.kwargs)
        service = AlarmConfiguration(PostgresAlarmConfigurationRepository(factory))
        original = AlarmRule("high", "功率越限", "MAJOR", {"operator": "gt", "value": 90}, 0, {"operator": "lt", "value": 85}, 0, 60, "kW")
        first = service.create_rule_set(key="pcs-power", name="PCS 功率", rules=(original,), actor="operator:test")
        selection = EntitySelection(entity_instance_ids=(self.entity_id,))
        plan = service.plan(PlanAlarmConfiguration(selection, first.rule_set_id, first.revision, "operator:test"))
        service.apply(ApplyAlarmConfigurationPlan(plan.id, plan.digest, "publish-legacy", "operator:test"))
        # Schema 044 retained rule-set plans with the old applied-result field.
        with factory() as connection, connection.cursor() as cursor:
            # Construct historical evidence only in the guarded *_test database.
            # LOCAL also restores the trigger mode if fixture construction fails.
            cursor.execute("SET LOCAL session_replication_role=replica")
            cursor.execute(
                """UPDATE t_alarm_configuration_plans
                   SET applied_result=(applied_result - 'configuration_revision') ||
                       jsonb_build_object('site_configuration_version', applied_result->'configuration_revision')
                   WHERE id=%s""",
                (plan.id,),
            )
            cursor.execute("SET LOCAL session_replication_role=origin")

        formal = service.create_rule_set_revision(rule_set_id=first.rule_set_id, rules=(replace(original, trigger={"operator": "gt", "value": 100}),), actor="operator:test")
        newer = service.plan(PlanAlarmConfiguration(selection, first.rule_set_id, formal.revision, "operator:test"))
        service.apply(ApplyAlarmConfigurationPlan(newer.id, newer.digest, "publish-newer", "operator:test"))
        draft = service.create_rule_set_revision(rule_set_id=first.rule_set_id, rules=(replace(original, trigger={"operator": "gt", "value": 120}),), actor="operator:test")
        empty = service.create_rule_set_revision(rule_set_id=first.rule_set_id, rules=(), actor="operator:test")
        stop = service.plan(PlanAlarmConfiguration(selection, first.rule_set_id, empty.revision, "operator:test"))
        service.apply(ApplyAlarmConfigurationPlan(stop.id, stop.digest, "stop-newer", "operator:test"))

        restarted = AlarmConfiguration(PostgresAlarmConfigurationRepository(factory))
        group = restarted.list_rule_groups()[0]
        self.assertEqual(draft.revision, group.last_non_empty_revision)
        self.assertEqual(formal.revision, group.last_published_revision)
        resume = restarted.plan(PlanAlarmConfiguration(selection, first.rule_set_id, group.last_published_revision, "operator:test"))
        restarted.apply(ApplyAlarmConfigurationPlan(resume.id, resume.digest, "resume-newer", "operator:test"))
        current = restarted.repository.current_configuration()
        self.assertEqual({"operator": "gt", "value": 100}, next(iter(current["definitions"].values()))["payload"]["rule"]["trigger"])

    def test_reenable_reuses_the_existing_immutable_alarm_definition(self) -> None:
        service = AlarmConfiguration(PostgresAlarmConfigurationRepository(lambda: psycopg2.connect(**self.kwargs)))
        rule_set = service.create_rule_set(
            key="pcs-power", name="PCS 功率",
            rules=(AlarmRule("high", "功率越限", "MAJOR", {"operator": "gt", "value": 90}, 0, {"operator": "lt", "value": 85}, 0, 60, "kW"),),
            actor="operator:test",
        )
        enabled_plan = service.plan(PlanAlarmConfiguration(EntitySelection(entity_instance_ids=(self.entity_id,)), rule_set.rule_set_id, 1, "operator:test"))
        first_result = service.apply(ApplyAlarmConfigurationPlan(enabled_plan.id, enabled_plan.digest, "alarm-enable-1", "operator:test"))
        empty = service.create_rule_set_revision(rule_set_id=rule_set.rule_set_id, rules=(), actor="operator:test")
        disabled_plan = service.plan(PlanAlarmConfiguration(EntitySelection(entity_instance_ids=(self.entity_id,)), empty.rule_set_id, empty.revision, "operator:test"))
        service.apply(ApplyAlarmConfigurationPlan(disabled_plan.id, disabled_plan.digest, "alarm-disable-1", "operator:test"))

        reenabled_plan = service.plan(PlanAlarmConfiguration(EntitySelection(entity_instance_ids=(self.entity_id,)), rule_set.rule_set_id, 1, "operator:test"))
        reenabled_result = service.apply(ApplyAlarmConfigurationPlan(reenabled_plan.id, reenabled_plan.digest, "alarm-enable-2", "operator:test"))

        self.assertEqual(first_result.definition_ids, reenabled_result.definition_ids)
        with psycopg2.connect(**self.kwargs) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM t_alarm_definitions")
            self.assertEqual(1, cursor.fetchone()[0])
            cursor.execute("SELECT definition_id FROM t_alarm_definition_current")
            self.assertEqual(first_result.definition_ids[0], cursor.fetchone()[0])

    def test_event_view_resolves_node_entity_and_rule_names(self) -> None:
        from app.services.telemetry_store import close_db_pool, init_db_pool

        service = AlarmConfiguration(PostgresAlarmConfigurationRepository(lambda: psycopg2.connect(**self.kwargs)))
        rule_set = service.create_rule_set(
            key="pcs-power", name="PCS 功率",
            rules=(AlarmRule("high", "功率越限", "MAJOR", {"operator": "gt", "value": 90}, 0, {"operator": "lt", "value": 85}, 0, 60, "kW"),),
            actor="operator:test",
        )
        plan = service.plan(PlanAlarmConfiguration(EntitySelection(entity_instance_ids=(self.entity_id,)), rule_set.rule_set_id, 1, "operator:test"))
        result = service.apply(ApplyAlarmConfigurationPlan(plan.id, plan.digest, "alarm-view-1", "operator:test"))
        definitions = PostgresAlarmDefinitionCatalog()
        runtime = AlarmRuntime(definitions, PostgresAlarmRepository())
        init_db_pool(1, 2)
        try:
            runtime.submit(AlarmObservation(
                definition_id=result.definition_ids[0], entity_instance_id=self.entity_id,
                observed_at=datetime.now(timezone.utc), value=100, quality=192,
                source_kind="committed_l2", source_ref="frame:test", evidence={},
            ))

            event = runtime.list()[0]
            presentation = runtime.describe((event,))[event.id]
            self.assertEqual("PCS-01", presentation.node_name)
            self.assertEqual("有功功率", presentation.entity_name)
            self.assertEqual("功率越限", presentation.alarm_name)
        finally:
            close_db_pool()


if __name__ == "__main__":
    unittest.main()
