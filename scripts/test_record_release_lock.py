"""Public owner-job contract for recording a verified release lock."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess


SCRIPT = Path(__file__).with_name("record_release_lock.py")
SPEC = importlib.util.spec_from_file_location("record_release_lock", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_lock)


class _Cursor:
    def __init__(self) -> None:
        self.insert_parameters = None
        self._last_statement = ""

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str, parameters=None) -> None:
        self._last_statement = statement
        if "INSERT INTO t_release_locks" in statement:
            self.insert_parameters = parameters

    def fetchone(self):
        if "current_user" in self._last_statement:
            return ("zizu_owner",)
        if "schema_migrations" in self._last_statement:
            return ("032",)
        if "t_site_configuration_state" in self._last_statement:
            return (4, "org.zizu.pv-storage-charging", "1.0.0", "d" * 64)
        raise AssertionError(self._last_statement)


class _Connection:
    def __init__(self) -> None:
        self.cursor_value = _Cursor()
        self.committed = False

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        raise AssertionError("recording should not roll back")

    def close(self) -> None:
        return None


class RecordReleaseLockTest(unittest.TestCase):
    def test_refuses_to_record_from_a_plain_http_liveness_endpoint(self) -> None:
        with patch.object(release_lock.urllib.request, "urlopen") as probe:
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                release_lock._public_liveness("http://release.example", "0.4.78")
        probe.assert_not_called()

    def test_refuses_a_container_that_does_not_match_the_declared_digest(self) -> None:
        def docker_inspect(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
            target = command[-1]
            output = {
                "registry.example/zizu@sha256:" + "a" * 64: "sha256:" + "a" * 64 + "\n",
                "backend-container": "sha256:" + "b" * 64 + "\n",
            }.get(target, "arm64\n")
            return CompletedProcess(command, 0, output, "")

        with patch.object(release_lock.subprocess, "run", side_effect=docker_inspect):
            with self.assertRaisesRegex(RuntimeError, "not running the declared release image"):
                release_lock._verified_runtime_image(
                    "backend-container",
                    "registry.example/zizu@sha256:" + "a" * 64,
                    "linux/arm64",
                )

    def test_records_the_target_architecture_after_public_liveness_and_schema_match(self) -> None:
        manifest = {
            "platform_version": "0.4.78",
            "schema_version": "032",
            "edge_proxy_image": "registry.example/caddy@sha256:" + "c" * 64,
            "images": {
                "linux/amd64": "registry.example/zizu@sha256:" + "a" * 64,
                "linux/arm64": "registry.example/zizu@sha256:" + "b" * 64,
            },
        }
        connection = _Connection()
        liveness_calls: list[tuple[str, str]] = []
        def docker_inspect(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
            target = command[-1]
            output = {
                manifest["images"]["linux/arm64"]: "sha256:backend-image\n",
                "backend-container": "sha256:backend-image\n",
                manifest["edge_proxy_image"]: "sha256:edge-image\n",
                "edge-container": "sha256:edge-image\n",
            }.get(target)
            if any("Architecture" in part for part in command):
                output = "arm64\n"
            return CompletedProcess(command, 0, output, "")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            manifest_path = temporary / "release.json"
            migrations = temporary / "migrations"
            migrations.mkdir()
            (migrations / "migration_032_release_locks.sql").write_text("-- test", encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            with (
                patch.object(release_lock, "required", side_effect=lambda name: {
                    "DB_OWNER_USER": "zizu_owner",
                    "DB_HOST": "db.example",
                    "DB_PORT": "5432",
                    "DB_NAME": "zizu_test",
                    "DB_OWNER_PASSWORD": "test-owner-password",
                }[name]),
                patch.object(release_lock, "optional", side_effect=lambda _name, fallback: fallback),
                patch.object(release_lock.psycopg2, "connect", return_value=connection),
                patch.object(release_lock.subprocess, "run", side_effect=docker_inspect),
                patch.object(
                    release_lock,
                    "_public_liveness",
                    side_effect=lambda url, version: liveness_calls.append((url, version)),
                ),
                contextlib.redirect_stdout(output),
            ):
                result = release_lock.main(
                    [
                        "--release", str(manifest_path),
                        "--migrations-dir", str(migrations),
                        "--architecture", "linux/arm64",
                        "--public-api", "https://ems.example",
                        "--backend-container", "backend-container",
                        "--edge-container", "edge-container",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertTrue(connection.committed)
        self.assertEqual(liveness_calls, [("https://ems.example", "0.4.78")])
        self.assertEqual(connection.cursor_value.insert_parameters[2], manifest["images"]["linux/arm64"])
        self.assertEqual(connection.cursor_value.insert_parameters[3], "sha256:backend-image")
        self.assertEqual(connection.cursor_value.insert_parameters[5], "sha256:edge-image")
        self.assertEqual(connection.cursor_value.insert_parameters[6], "linux/arm64")
        recorded = json.loads(output.getvalue())
        self.assertEqual(recorded["site_configuration_version"], 4)
        self.assertRegex(recorded["lock_id"], r"^[0-9a-f-]{36}$")


if __name__ == "__main__":
    unittest.main()
