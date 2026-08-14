"""Owner rollback validation contract without a real target database."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("validate_release_rollback.py")
SPEC = importlib.util.spec_from_file_location("validate_release_rollback", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
rollback = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollback)


class _Cursor:
    def __init__(self, lock_schema: str = "032") -> None:
        self.statement = ""
        self.lock_schema = lock_schema

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str, _parameters=None) -> None:
        self.statement = statement

    def fetchone(self):
        if "current_user" in self.statement:
            return ("zizu_owner",)
        if "FROM t_release_locks" in self.statement:
            return (
                "0.4.78",
                "registry.example/zizu@sha256:" + "a" * 64,
                "registry.example/caddy@sha256:" + "c" * 64,
                "linux/arm64",
                self.lock_schema,
                4,
                "org.zizu.ems",
                "1.0.0",
                "d" * 64,
            )
        if "schema_migrations" in self.statement:
            return (self.lock_schema,)
        if "t_site_configuration_state" in self.statement:
            return (4, "d" * 64, {"neuron.password": "secret://site/neuron"})
        raise AssertionError(self.statement)


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_value = cursor

    def cursor(self):
        return self.cursor_value

    def close(self) -> None:
        return None


class ValidateReleaseRollbackTest(unittest.TestCase):
    def _manifest(self) -> dict:
        return {
            "platform_version": "0.4.78",
            "schema_version": "032",
            "edge_proxy_image": "registry.example/caddy@sha256:" + "c" * 64,
            "images": {
                "linux/amd64": "registry.example/zizu@sha256:" + "b" * 64,
                "linux/arm64": "registry.example/zizu@sha256:" + "a" * 64,
            },
        }

    def _run(self, cursor: _Cursor) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release.json"
            migrations = root / "migrations"
            migrations.mkdir()
            (migrations / "migration_032_release_locks.sql").write_text("-- test", encoding="utf-8")
            release.write_text(json.dumps(self._manifest()), encoding="utf-8")
            output = io.StringIO()
            with (
                patch.object(rollback, "required", side_effect=lambda name: {
                    "DB_OWNER_USER": "zizu_owner", "DB_HOST": "db", "DB_PORT": "5432",
                    "DB_NAME": "zizu_test", "DB_OWNER_PASSWORD": "test-owner-password",
                }[name]),
                patch.object(rollback, "optional", side_effect=lambda _name, fallback: fallback),
                patch.object(rollback.psycopg2, "connect", return_value=_Connection(cursor)),
                contextlib.redirect_stdout(output),
            ):
                result = rollback.main([
                    "--release", str(release), "--migrations-dir", str(migrations),
                    "--architecture", "linux/arm64", "--lock-id",
                    "00000000-0000-0000-0000-000000000001",
                ])
        return result, json.loads(output.getvalue())

    def test_accepts_only_an_exact_current_schema_and_site_lock(self) -> None:
        result, output = self._run(_Cursor())
        self.assertEqual(result, 0)
        self.assertEqual(output["status"], "rollback_compatible")
        self.assertEqual(output["architecture"], "linux/arm64")

    def test_rejects_rollback_across_a_schema_boundary(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "schema versions"):
            self._run(_Cursor(lock_schema="031"))


if __name__ == "__main__":
    unittest.main()
