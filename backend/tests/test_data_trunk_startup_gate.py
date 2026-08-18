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
        self.assertEqual(len(calls), 1)
        self.assertIn("assert_entity_instance_single_source(id)", calls[0])
        self.assertIn("FROM t_entity_instances", calls[0])

    def test_production_startup_invokes_gate_after_migrations(self) -> None:
        from app import main

        source = inspect.getsource(main.lifespan)
        self.assertIn("verify_data_trunk_contract_gate", source)
        self.assertLess(
            source.index("run_migrations()"),
            source.index("verify_data_trunk_contract_gate"),
        )


if __name__ == "__main__":
    unittest.main()
