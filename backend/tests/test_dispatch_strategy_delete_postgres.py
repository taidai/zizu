import os
import unittest
from uuid import uuid4

import psycopg2

from tests.test_dispatch_strategy_postgres import DispatchStrategyPostgresFixture, ROOT
from app.services.dispatch_strategy_postgres import StrategyRepositoryError


@unittest.skipUnless(os.environ.get('ZIZU_POSTGRES_TEST') == '1', 'requires isolated PostgreSQL')
class DispatchStrategyDeleteTest(DispatchStrategyPostgresFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        migration = ROOT / 'init-db/migration_064_dispatch_strategy_delete.sql'
        if migration.exists():
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(migration.read_text(encoding='utf-8'))
                cursor.execute(migration.read_text(encoding='utf-8'))

    def published(self):
        strategy = self.repository.create_strategy(self._draft(), 'test')
        revision = self.repository.publish(strategy.id, strategy.draft.content_digest, self.configuration_revision, 'test')
        return strategy, revision

    def intent(self, strategy, revision, status):
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO t_dispatch_control_intents(id,strategy_id,revision_id,evaluation_key,action_id,ordinal,entity_instance_id,expected_value,status,snapshot_evidence) VALUES(%s,%s,%s,'test','power-target',0,%s,'1',%s,'{}')", (uuid4(), strategy.id, revision.id, self.output_id, status))

    def test_delete_removes_only_selected_strategy_and_its_records(self):
        strategy, revision = self.published()
        other = self.repository.create_strategy(self._draft('keep'), 'test')
        self.intent(strategy, revision, 'CANCELLED')
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO t_dispatch_strategy_events(occurred_at,id,strategy_id,revision_id,event_kind,trigger_kind,trigger_key,configuration_revision,snapshot_evidence) VALUES(now(),%s,%s,%s,'BLOCKED','FIXED_TICK','test',%s,'{}')", (uuid4(), strategy.id, revision.id, self.configuration_revision))
        self.repository.delete_strategy(strategy.id)
        self.assertEqual([other.id], [item.id for item in self.repository.list_strategies()])
        with self._connection() as connection, connection.cursor() as cursor:
            for table in ('t_dispatch_strategy_revisions', 't_dispatch_control_intents', 't_dispatch_strategy_events', 't_dispatch_strategy_owners'):
                cursor.execute(f'SELECT count(*) FROM {table} WHERE strategy_id=%s', (strategy.id,))
                self.assertEqual(0, cursor.fetchone()[0])
            cursor.execute('SELECT count(*) FROM t_dispatch_strategy_bindings WHERE revision_id=%s', (revision.id,))
            self.assertEqual(0, cursor.fetchone()[0])
            cursor.execute('SELECT count(*) FROM t_entity_instances WHERE id=%s', (self.output_id,))
            self.assertEqual(1, cursor.fetchone()[0])
        with self.assertRaisesRegex(StrategyRepositoryError, 'STRATEGY_NOT_FOUND'):
            self.repository.delete_strategy(strategy.id)

    def test_enabled_or_inflight_strategy_cannot_be_deleted(self):
        strategy, revision = self.published()
        self.repository.enable(strategy.id, revision.id, 'test')
        with self.assertRaisesRegex(StrategyRepositoryError, 'STRATEGY_DELETE_ENABLED'):
            self.repository.delete_strategy(strategy.id)
        self.repository.disable(strategy.id, 'test')
        for status in ('PENDING', 'IN_FLIGHT'):
            self.intent(strategy, revision, status)
            with self.assertRaisesRegex(StrategyRepositoryError, 'STRATEGY_DELETE_IN_FLIGHT'):
                self.repository.delete_strategy(strategy.id)
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute('DELETE FROM t_dispatch_control_intents WHERE strategy_id=%s', (strategy.id,))
        self.assertEqual(strategy.id, self.repository.get_strategy(strategy.id).id)

    def test_published_revision_stays_immutable_outside_strategy_delete(self):
        strategy, revision = self.published()
        with self.assertRaises(psycopg2.Error):
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute('DELETE FROM t_dispatch_strategy_revisions WHERE id=%s', (revision.id,))
        self.assertIsNotNone(self.repository.get_revision(revision.id))

    def test_terminal_intent_cannot_hide_an_unfinished_control_command(self):
        strategy, revision = self.published()
        self.intent(strategy, revision, 'FAILED')
        command_id, audit_id = uuid4(), uuid4()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO t_audit_events(id,event,outcome) VALUES(%s,'test:sent','dispatched')", (audit_id,))
            cursor.execute("INSERT INTO t_control_commands(id,actor,source_type,capability,entity_instance_id,expected_value,data_type,policy_snapshot,status,code,idempotency_key,request_digest,audit_event_id) VALUES(%s,'test','strategy','control.write',%s,'1','FLOAT','{}','dispatched','DISPATCHED',%s,%s,%s)", (command_id, self.output_id, str(command_id), 'a' * 64, audit_id))
            cursor.execute('UPDATE t_dispatch_control_intents SET control_command_id=%s WHERE strategy_id=%s', (command_id, strategy.id))
        with self.assertRaisesRegex(StrategyRepositoryError, 'STRATEGY_DELETE_IN_FLIGHT'):
            self.repository.delete_strategy(strategy.id)
