from __future__ import annotations

import os
import unittest
from uuid import uuid4

import psycopg2

from app.services.alarm_configuration import (
    AlarmConfiguration,
    AlarmRule,
    ApplyAlarmConfigurationPlan,
    EntitySelection,
    PlanAlarmConfiguration,
)
from app.services.alarm_configuration_postgres import PostgresAlarmConfigurationRepository
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


if __name__ == "__main__":
    unittest.main()
