"""Regression checks for the owner-only database provisioning command."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("provision_database_roles.py")
SPEC = importlib.util.spec_from_file_location("provision_database_roles", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class ProvisionDatabaseRolesTest(unittest.TestCase):
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
