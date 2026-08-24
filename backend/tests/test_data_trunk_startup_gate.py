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
        self.assertIn("t_point_processing_expressions", calls[0])
        self.assertIn("t_point_processing_selector_members", calls[0])
        self.assertIn("t_point_processing_dependencies", calls[0])
        self.assertIn("t_point_processing_formula_runs", calls[0])
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

    def test_metric_projection_loop_starts_only_inside_lifespan_and_stops_safely(self) -> None:
        from app import main

        source = inspect.getsource(main.lifespan)
        self.assertEqual(main._data_trunk_tasks, [])
        self.assertIsNone(main._data_trunk_stop)
        self.assertIn('name="business_metric_projection"', source)
        self.assertIn("metric_projection.advance", source)
        metric_loop = source[
            source.index("async def _metric_projection_loop"):
            source.index("async def _runtime_health_loop")
        ]
        self.assertNotIn("datetime.now", metric_loop)
        self.assertLess(source.index("_data_trunk_stop = asyncio.Event()"), source.index('name="business_metric_projection"'))
        self.assertLess(source.index("_data_trunk_stop.set()"), source.index("await asyncio.gather(*_data_trunk_tasks"))


if __name__ == "__main__":
    unittest.main()
