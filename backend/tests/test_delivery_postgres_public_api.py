from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg2


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = BACKEND_ROOT.parent / "init-db" / "migration_020_solution_delivery.sql"


def build_minimal_package(*, package_id: str = "org.zizu.postgres-liveness") -> bytes:
    acceptance = (
        "schemaVersion: zizu.acceptance/v1alpha1\n"
        "id: acceptance.platform-liveness\n"
        "kind: platform_liveness\n"
        "required: true\n"
        "timeout: 5s\n"
    ).encode()
    manifest = (
        "schemaVersion: zizu.solution/v1alpha1\n"
        f"id: {package_id}\n"
        "version: 1.0.0\n"
        "displayName: Postgres liveness\n"
        "platform:\n"
        "  version: \">=0.4.77,<0.5.0\"\n"
        "assets:\n"
        "  - id: acceptance.platform-liveness\n"
        "    kind: acceptance\n"
        "    path: acceptance/liveness.yaml\n"
        f"    sha256: \"{hashlib.sha256(acceptance).hexdigest()}\"\n"
        "acceptance:\n"
        "  - acceptance.platform-liveness\n"
    ).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("solution.yaml", manifest)
        package.writestr("acceptance/liveness.yaml", acceptance)
    return archive.getvalue()


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the isolated Postgres public seam",
)
class DeliveryPostgresPublicApiTest(unittest.TestCase):
    process: subprocess.Popen[str] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Postgres delivery tests require a *_test database")
        cls.port = cls._free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_env = os.environ.copy()
        cls.server_env["PUBLIC_API_BASE_URL"] = cls.base_url

        with psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ["DB_PORT"]),
            dbname=cls.db_name,
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
        ) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))
        try:
            cls._start_server()
        except Exception:
            cls._stop_server()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_server()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @classmethod
    def _start_server(cls) -> None:
        cls.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.postgres_delivery_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
                "--log-level",
                "warning",
            ],
            cwd=BACKEND_ROOT,
            env=cls.server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                output = cls.process.stdout.read() if cls.process.stdout else ""
                raise RuntimeError(f"delivery test server exited early:\n{output}")
            try:
                response = httpx.get(
                    f"{cls.base_url}/api/v1/health/live",
                    timeout=0.5,
                    trust_env=False,
                )
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("delivery test server did not become ready")

    @classmethod
    def _stop_server(cls) -> None:
        if cls.process is None or cls.process.poll() is not None:
            return
        cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)
        finally:
            if cls.process.stdout:
                cls.process.stdout.close()
            cls.process = None

    def test_public_delivery_survives_process_restart(self) -> None:
        with httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) as client:
            imported = client.post(
                "/api/v1/solution-packages/import",
                files={"archive": ("minimal.zizu.zip", build_minimal_package(), "application/zip")},
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            package = imported.json()
            planned = client.post(
                f"/api/v1/solution-packages/{package['id']}/install-plans",
                json={},
            )
            self.assertEqual(planned.status_code, 201, planned.text)
            repeated_plan = client.post(
                f"/api/v1/solution-packages/{package['id']}/install-plans",
                json={},
            )
            self.assertEqual(repeated_plan.status_code, 201, repeated_plan.text)
            self.assertEqual(repeated_plan.json(), planned.json())
            plan = planned.json()
            request = {"plan_digest": plan["digest"]}
            headers = {"Idempotency-Key": "postgres-install-once"}
            installed = client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
                headers=headers,
            )
            repeated = client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
                headers=headers,
            )
            self.assertEqual(installed.status_code, 201, installed.text)
            self.assertEqual(repeated.json(), installed.json())
            installation = installed.json()
            repeated_with_new_key = client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
                headers={"Idempotency-Key": "postgres-install-new-key"},
            )
            self.assertEqual(repeated_with_new_key.status_code, 201)
            self.assertEqual(repeated_with_new_key.json(), installation)
            run = client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={"Idempotency-Key": "postgres-accept-once"},
            )
            self.assertEqual(run.status_code, 201, run.text)
            report = run.json()
            repeated_run = client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={"Idempotency-Key": "postgres-accept-once"},
            )
            self.assertEqual(repeated_run.status_code, 201, repeated_run.text)
            self.assertEqual(repeated_run.json(), report)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["actor"], "anonymous-bootstrap")
            self.assertIn("started_at", report)
            self.assertIn("finished_at", report)
            self.assertGreaterEqual(report["duration_ms"], 0)
            self.assertGreaterEqual(report["items"][0]["duration_ms"], 0)

            second_import = client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "concurrent.zizu.zip",
                        build_minimal_package(package_id="org.zizu.concurrent-pg"),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(second_import.status_code, 201, second_import.text)
            concurrent_plan_response = client.post(
                f"/api/v1/solution-packages/{second_import.json()['id']}/install-plans",
                json={},
            )
            concurrent_plan = concurrent_plan_response.json()

        def apply_concurrently() -> httpx.Response:
            return httpx.post(
                f"{self.base_url}/api/v1/install-plans/{concurrent_plan['id']}/apply",
                json={"plan_digest": concurrent_plan["digest"]},
                headers={"Idempotency-Key": "postgres-concurrent-install"},
                timeout=10,
                trust_env=False,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = list(executor.map(lambda _: apply_concurrently(), range(2)))
        self.assertEqual([response.status_code for response in concurrent_results], [201, 201])
        self.assertEqual(concurrent_results[0].json(), concurrent_results[1].json())

        with psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ["DB_PORT"]),
            dbname=self.db_name,
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version, previous_version FROM t_site_configuration_versions ORDER BY version")
                versions = cursor.fetchall()
                cursor.execute("SELECT count(*) FROM t_solution_delivery_audit")
                audit_count = cursor.fetchone()[0]
                cursor.execute("SELECT count(*) FROM t_solution_install_plans")
                plan_count = cursor.fetchone()[0]
        self.assertEqual(versions, [(0, None), (1, 0), (2, 1)])
        self.assertEqual(audit_count, 2)
        self.assertEqual(plan_count, 2)

        self._stop_server()
        self._start_server()

        with httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) as client:
            packages = client.get("/api/v1/solution-packages")
            installations = client.get("/api/v1/solution-installations")
            persisted_report = client.get(f"/api/v1/delivery-reports/{report['id']}")
            repeated_after_restart = client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
                headers=headers,
            )
            repeated_acceptance_after_restart = client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={"Idempotency-Key": "postgres-accept-once"},
            )
            fresh_acceptance_after_restart = client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={"Idempotency-Key": "postgres-accept-after-restart"},
            )

        persisted_packages = packages.json()
        self.assertEqual(persisted_packages["total"], 2)
        self.assertIn(package, persisted_packages["items"])
        self.assertEqual(installations.json()["items"][0], installation)
        self.assertEqual(installations.json()["total"], 2)
        self.assertEqual(persisted_report.json(), report)
        self.assertEqual(repeated_after_restart.json(), installation)
        self.assertEqual(repeated_acceptance_after_restart.status_code, 201)
        self.assertEqual(repeated_acceptance_after_restart.json(), report)
        self.assertEqual(fresh_acceptance_after_restart.status_code, 201)
        self.assertNotEqual(fresh_acceptance_after_restart.json()["id"], report["id"])


if __name__ == "__main__":
    unittest.main()
