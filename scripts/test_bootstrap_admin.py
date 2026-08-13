from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock

from scripts.bootstrap_admin import (
    BootstrapAdminError,
    bootstrap_admin,
    parse_args,
    provision_identity,
    PostgresAdminStore,
    read_password,
)
from app.services.identity import verify_password


class RecordingAdminStore:
    def __init__(self, outcome: str = "created") -> None:
        self.outcome = outcome
        self.provisioned: tuple[str, str] | None = None

    def provision(
        self,
        username: str,
        password_hash: str,
        role: str,
        *,
        bootstrap_admin: bool,
    ) -> str:
        self.provisioned = (username, password_hash)
        self.role = role
        self.bootstrap_admin = bootstrap_admin
        return self.outcome


class LegacyAdminStore(RecordingAdminStore):
    def provision(
        self,
        username: str,
        password_hash: str,
        role: str,
        *,
        bootstrap_admin: bool,
    ) -> str:
        self.provisioned = (username, password_hash)
        self.role = role
        self.bootstrap_admin = bootstrap_admin
        return "password_migrated"


class BootstrapAdminTest(unittest.TestCase):
    def test_provisions_admin_with_identity_password_hash(self) -> None:
        store = RecordingAdminStore()
        password = "correct horse battery staple!9"

        result = bootstrap_admin(store, "site-admin", password)

        self.assertEqual(result, "created")
        self.assertIsNotNone(store.provisioned)
        username, stored_hash = store.provisioned or ("", "")
        self.assertEqual(username, "site-admin")
        self.assertNotIn(password, stored_hash)
        self.assertTrue(verify_password(password, stored_hash))

    def test_rejects_password_cli_argument_without_echoing_value(self) -> None:
        secret = "must-not-appear"

        with self.assertRaises(BootstrapAdminError) as raised:
            parse_args(["--password", secret])

        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("stdin", str(raised.exception).lower())

    def test_reads_noninteractive_password_from_stdin_once(self) -> None:
        password = "noninteractive admin passphrase!7"

        resolved = read_password(
            password_stdin=True,
            stdin=io.StringIO(password + "\n"),
        )

        self.assertEqual(resolved, password)

    def test_rejects_short_password_before_persistence(self) -> None:
        store = RecordingAdminStore()

        with self.assertRaisesRegex(BootstrapAdminError, "14"):
            bootstrap_admin(store, "admin", "too-short")

        self.assertIsNone(store.provisioned)

    def test_rejects_malformed_username_before_persistence(self) -> None:
        store = RecordingAdminStore()

        with self.assertRaisesRegex(BootstrapAdminError, "username"):
            bootstrap_admin(
                store,
                "admin@example.com; DROP TABLE t_users",
                "correct horse battery staple!9",
            )

        self.assertIsNone(store.provisioned)

    def test_can_migrate_an_existing_legacy_admin_password(self) -> None:
        store = LegacyAdminStore()
        password = "replacement administrator passphrase!7"

        result = bootstrap_admin(store, "admin", password)

        self.assertEqual(result, "password_migrated")
        self.assertTrue(verify_password(password, store.provisioned[1]))

    def test_explicitly_provisions_engineer_after_bootstrap(self) -> None:
        store = RecordingAdminStore()

        result = provision_identity(
            store,
            "site-engineer",
            "engineer replacement passphrase!7",
            role="engineer",
            bootstrap_admin=False,
        )

        self.assertEqual(result, "created")
        self.assertEqual(store.role, "engineer")
        self.assertFalse(store.bootstrap_admin)

    def test_explicit_offline_admin_recovery_resets_password_and_sessions(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            ("00000000-0000-0000-0000-000000000001", "admin", "admin", "active")
        ]
        cursor.fetchone.return_value = ("pbkdf2_sha256$600000$salt$digest",)
        store = PostgresAdminStore(connection_factory=lambda: connection)

        outcome = store.provision(
            "admin",
            "pbkdf2_sha256$600000$new-salt$new-digest",
            "admin",
            bootstrap_admin=False,
        )

        self.assertEqual(outcome, "password_reset")
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("auth_version = auth_version + 1", statements)
        self.assertIn("identity.provision", statements)


if __name__ == "__main__":
    unittest.main()
