"""Regression checks for the owner-only database provisioning command."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
import os


SCRIPT = Path(__file__).with_name("provision_database_roles.py")
SPEC = importlib.util.spec_from_file_location("provision_database_roles", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class ProvisionDatabaseRolesTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('ZIZU_TEST_ROLE_PROVISIONING') == '1',
                         'requires an explicitly authorized isolated owner database')
    def test_current_schema_provisions_app_without_retired_release_table(self) -> None:
        provision.main()
        connection = provision.psycopg2.connect(
            host=provision.required('DB_HOST'), port=provision.required('DB_PORT'),
            dbname=provision.required('DB_NAME'), user=provision.required('DB_OWNER_USER'),
            password=provision.required('DB_OWNER_PASSWORD'),
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.t_release_locks')")
                self.assertIsNone(cursor.fetchone()[0])
                cursor.execute("SELECT has_table_privilege(%s, 'public.t_alarms', 'INSERT'), "
                               "has_table_privilege(%s, 'public.t_nodes', 'SELECT')",
                               (provision.required('DB_USER'), provision.required('DB_USER')))
                self.assertEqual((False, True), cursor.fetchone())
        finally:
            connection.close()

    def test_dotenv_value_removes_comment_without_corrupting_secret_hashes(self) -> None:
        self.assertEqual("timescaledb", provision._dotenv_value(" timescaledb  # Compose DNS"))
        self.assertEqual("safe#value", provision._dotenv_value("safe#value"))
        self.assertEqual("safe # value", provision._dotenv_value('"safe # value"  # note'))

    def test_explicit_owner_endpoint_overrides_web_connection_endpoint(self) -> None:
        previous_environment = provision.os.environ.copy()
        previous_file_environment = provision.FILE_ENVIRONMENT
        try:
            provision.os.environ.clear()
            provision.FILE_ENVIRONMENT = {"DB_HOST": "timescaledb", "DB_PORT": "5432"}
            self.assertEqual("timescaledb", provision.optional("DB_OWNER_HOST", provision.required("DB_HOST")))
            provision.os.environ["DB_OWNER_HOST"] = "127.0.0.1"
            self.assertEqual("127.0.0.1", provision.optional("DB_OWNER_HOST", provision.required("DB_HOST")))
        finally:
            provision.os.environ.clear()
            provision.os.environ.update(previous_environment)
            provision.FILE_ENVIRONMENT = previous_file_environment


if __name__ == "__main__":
    unittest.main()
