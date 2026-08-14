"""PostgreSQL proof for the activation boundary of automatic EMS policies."""
from __future__ import annotations

import os
from pathlib import Path
from threading import Event, Thread
import unittest

import psycopg2


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_031 = BACKEND_ROOT.parent / "init-db" / "migration_031_ems_policy_activations.sql"


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the isolated Postgres policy seam",
)
class PostgresPolicyActivationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Postgres policy tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        with psycopg2.connect(**cls.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
                cursor.execute(MIGRATION_031.read_text(encoding="utf-8"))
        from app.services.telemetry_store import close_db_pool, init_db_pool

        close_db_pool()
        init_db_pool(min_conn=1, max_conn=4)

    @classmethod
    def tearDownClass(cls) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    def test_disable_waits_until_an_already_active_dispatch_boundary_finishes(self) -> None:
        """A successful disable cannot race ahead of an activation share lock."""
        from app.services.ems_policy_runtime import PostgresPolicyActivationRepository

        repository = PostgresPolicyActivationRepository()
        version = 1
        policy_id = "policy.grid-import-cap"
        repository.enable(version, policy_id, "user:engineer")
        evaluation_entered = Event()
        release_evaluation = Event()
        disable_returned = Event()

        def evaluate() -> None:
            with repository.active(version, policy_id) as active:
                self.assertTrue(active)
                evaluation_entered.set()
                self.assertTrue(release_evaluation.wait(timeout=3))

        def disable() -> None:
            repository.disable(version, policy_id, "user:engineer")
            disable_returned.set()

        evaluator = Thread(target=evaluate)
        evaluator.start()
        self.assertTrue(evaluation_entered.wait(timeout=3))
        disabler = Thread(target=disable)
        disabler.start()
        self.assertFalse(disable_returned.wait(timeout=0.1))
        release_evaluation.set()
        evaluator.join(timeout=3)
        disabler.join(timeout=3)
        self.assertFalse(evaluator.is_alive())
        self.assertFalse(disabler.is_alive())
        self.assertTrue(disable_returned.is_set())
        self.assertFalse(repository.enabled(version, policy_id))

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_ems_policy_activations")
                self.assertEqual(0, cursor.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
