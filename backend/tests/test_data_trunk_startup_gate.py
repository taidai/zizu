from __future__ import annotations

from contextlib import contextmanager
import inspect
import os
import unittest


os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")


class DataTrunkStartupGateTest(unittest.TestCase):
    def _connection(self, *, row_count: int = 1):
        calls: list[str] = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, query):
                calls.append(" ".join(query.split()))

            def fetchall(self):
                return [(None,)] * row_count

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return Cursor()

        @contextmanager
        def connection():
            yield Connection()

        return connection, calls

    def test_gate_revalidates_every_entity_against_database_contract(self) -> None:
        from app.services.data_trunk_postgres import verify_data_trunk_contract_gate

        connection, calls = self._connection(row_count=3)

        self.assertEqual(verify_data_trunk_contract_gate(connection), 3)
        self.assertEqual(len(calls), 2)
        self.assertIn("t_configuration_state", calls[0])
        self.assertIn("t_configuration_revisions", calls[0])
        self.assertIn("t_configuration_audit", calls[0])
        self.assertIn("t_point_processing_expressions", calls[0])
        self.assertIn("t_point_processing_selector_members", calls[0])
        self.assertIn("t_point_processing_dependencies", calls[0])
        self.assertIn("t_point_processing_formula_runs", calls[0])
        self.assertIn("column_name = 'node_id'", calls[0])
        self.assertNotIn("t_cross_node_processing_acceptance_reports", calls[0])
        self.assertIn("assert_entity_instance_single_source(id)", calls[1])
        self.assertIn("FROM t_entity_instances", calls[1])

    def test_production_startup_invokes_gate_after_migrations(self) -> None:
        from app import main

        source = inspect.getsource(main.lifespan)
        self.assertIn("verify_data_trunk_contract_gate", source)
        self.assertLess(
            source.index("run_migrations()"),
            source.index("verify_data_trunk_contract_gate"),
        )

    def test_removed_business_metric_projection_is_not_started(self) -> None:
        from app import main

        source = inspect.getsource(main.lifespan)
        self.assertEqual(main._data_trunk_tasks, [])
        self.assertIsNone(main._data_trunk_stop)
        self.assertNotIn("business_metric_projection", source)
        self.assertNotIn("metric_projection.advance", source)

    def test_main_starts_only_frame_runtime_loops(self) -> None:
        from app import main

        source = inspect.getsource(main.lifespan)
        self.assertIn("data_frame_capture", source)
        self.assertIn("data_frame_processor", source)
        self.assertNotIn("data_trunk_freshness", source)
        self.assertNotIn("data_trunk_typed_formulas", source)
        self.assertNotIn("run_formula_tick", source)
        self.assertNotIn("run_rule_tick", source)
        self.assertNotIn("run_aggregation_tick", source)


if __name__ == "__main__":
    unittest.main()
