from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import httpx
import psycopg2


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_020 = BACKEND_ROOT.parent / "init-db" / "migration_020_solution_delivery.sql"
MIGRATION_021 = BACKEND_ROOT.parent / "init-db" / "migration_021_identity.sql"
MIGRATION_022 = BACKEND_ROOT.parent / "init-db" / "migration_022_websocket_tickets.sql"
MIGRATION_023 = BACKEND_ROOT.parent / "init-db" / "migration_023_site_configuration_parameters.sql"
MIGRATION_024 = BACKEND_ROOT.parent / "init-db" / "migration_024_entity_instances.sql"
MIGRATION_025 = BACKEND_ROOT.parent / "init-db" / "migration_025_rule_entity_instance_refs.sql"
MIGRATION_026 = BACKEND_ROOT.parent / "init-db" / "migration_026_control_commands.sql"
MIGRATION_027 = BACKEND_ROOT.parent / "init-db" / "migration_027_nullable_control_target.sql"
MIGRATION_028 = BACKEND_ROOT.parent / "init-db" / "migration_028_rule_control_commands.sql"
MIGRATION_029 = BACKEND_ROOT.parent / "init-db" / "migration_029_unified_alarm_runtime.sql"
MIGRATION_030 = BACKEND_ROOT.parent / "init-db" / "migration_030_rule_alarm_and_legacy_gate.sql"
MIGRATION_031 = BACKEND_ROOT.parent / "init-db" / "migration_031_ems_policy_activations.sql"
MIGRATION_032 = BACKEND_ROOT.parent / "init-db" / "migration_032_release_locks.sql"
MIGRATIONS = (
    MIGRATION_020,
    MIGRATION_021,
    MIGRATION_022,
    MIGRATION_023,
    MIGRATION_024,
    MIGRATION_025,
    MIGRATION_026,
    MIGRATION_027,
    MIGRATION_028,
    MIGRATION_029,
    MIGRATION_030,
    MIGRATION_031,
    MIGRATION_032,
)
def build_minimal_package(
    *,
    package_id: str = "org.zizu.postgres-liveness",
    package_version: str = "1.0.0",
    parameters_yaml: str = "",
) -> bytes:
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
        f"version: {package_version}\n"
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
        f"{parameters_yaml}"
    ).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("solution.yaml", manifest)
        package.writestr("acceptance/liveness.yaml", acceptance)
    return archive.getvalue()


def build_entity_package(**kwargs) -> bytes:
    from tests.test_entity_delivery_public_api import build_entity_package as build

    return build(**kwargs)


def build_control_entity_package() -> bytes:
    from tests.test_entity_delivery_public_api import build_control_entity_package as build

    return build()


def build_alarm_entity_package() -> bytes:
    from tests.test_entity_delivery_public_api import build_alarm_entity_package as build

    return build(
        package_version="1.0.1",
        multiple_devices=True,
        manual_failover=True,
    )


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the isolated Postgres public seam",
)
class DeliveryPostgresPublicApiTest(unittest.TestCase):
    process: subprocess.Popen[str] | None = None

    @staticmethod
    def _create_source_catalog_tables(cursor) -> None:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS t_nodes (
                id UUID PRIMARY KEY,
                name TEXT NOT NULL,
                source_catalog_key TEXT,
                node_type TEXT NOT NULL DEFAULT 'DEVICE',
                enabled BOOLEAN NOT NULL DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS t_tags (
                id UUID PRIMARY KEY,
                node_id UUID NOT NULL REFERENCES t_nodes(id),
                name TEXT NOT NULL,
                data_type TEXT NOT NULL,
                unit TEXT,
                unit_to TEXT,
                read_write TEXT NOT NULL DEFAULT 'R',
                source_type TEXT DEFAULT 'NEURON',
                source_path TEXT,
                scale_factor DOUBLE PRECISION DEFAULT 1.0,
                value_offset DOUBLE PRECISION DEFAULT 0.0,
                unit_from TEXT,
                range_min DOUBLE PRECISION,
                range_max DOUBLE PRECISION,
                alarm_level TEXT,
                alarm_type TEXT,
                alarm_threshold DOUBLE PRECISION,
                fault_map_id UUID,
                enabled BOOLEAN NOT NULL DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS t_telemetry (
                ts TIMESTAMPTZ NOT NULL,
                node_id UUID NOT NULL REFERENCES t_nodes(id),
                tag_id UUID NOT NULL REFERENCES t_tags(id),
                value_float DOUBLE PRECISION,
                value_int BIGINT,
                value_bool BOOLEAN,
                value_str TEXT,
                is_virtual BOOLEAN DEFAULT FALSE,
                quality SMALLINT DEFAULT 192
            );
            CREATE TABLE IF NOT EXISTS t_telemetry_latest (
                node_id UUID NOT NULL REFERENCES t_nodes(id),
                tag_id UUID NOT NULL REFERENCES t_tags(id),
                ts TIMESTAMPTZ NOT NULL,
                value_float DOUBLE PRECISION,
                value_int BIGINT,
                value_bool BOOLEAN,
                value_str TEXT,
                is_virtual BOOLEAN DEFAULT FALSE,
                quality SMALLINT DEFAULT 192,
                updated_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (node_id, tag_id)
            );
            CREATE TABLE IF NOT EXISTS t_entities (
                id UUID PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                enabled BOOLEAN NOT NULL DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS t_alarms (
                id UUID PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS t_entity_bindings (
                id UUID PRIMARY KEY,
                entity_id UUID NOT NULL REFERENCES t_entities(id),
                tag_id UUID NOT NULL REFERENCES t_tags(id),
                node_id UUID NOT NULL REFERENCES t_nodes(id),
                enabled BOOLEAN NOT NULL DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS t_rules (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name TEXT NOT NULL,
                rule_type TEXT NOT NULL,
                jdm_content JSONB NOT NULL DEFAULT '{}'::jsonb,
                version INTEGER NOT NULL DEFAULT 1,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Postgres delivery tests require a *_test database")
        cls.port = cls._free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_env = os.environ.copy()
        cls.server_env["PUBLIC_API_BASE_URL"] = cls.base_url
        cls.server_env["DEPLOYMENT_MODE"] = "development"
        cls.server_env["AUTH_REQUIRE_HTTPS"] = "false"
        cls.password = secrets.token_urlsafe(24)

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
                for migration in (MIGRATION_020, MIGRATION_021, MIGRATION_022):
                    cursor.execute(migration.read_text(encoding="utf-8"))
                legacy_package_id = "00000000-0000-0000-0000-000000000501"
                legacy_plan_id = "00000000-0000-0000-0000-000000000502"
                legacy_installation_id = "00000000-0000-0000-0000-000000000503"
                legacy_digest = "a" * 64
                cursor.execute(
                    """
                    INSERT INTO t_solution_packages
                      (id, package_id, version, display_name, digest, status,
                       acceptance_ids, manifest)
                    VALUES (%s, 'org.zizu.legacy-parameterless', '1.0.0',
                            'Legacy parameterless', %s, 'validated', '[]', '{}')
                    """,
                    (legacy_package_id, legacy_digest),
                )
                cursor.execute(
                    """
                    INSERT INTO t_solution_install_plans
                      (id, package_record_id, package_digest,
                       base_site_configuration_version, status, items, blockers,
                       digest)
                    VALUES (%s, %s, %s, 0, 'ready', '[]', '[]', %s)
                    """,
                    (legacy_plan_id, legacy_package_id, legacy_digest, "b" * 64),
                )
                cursor.execute(
                    """
                    INSERT INTO t_solution_installations
                      (id, plan_id, package_record_id, package_digest,
                       site_configuration_version, status)
                    VALUES (%s, %s, %s, %s, 1, 'installed')
                    """,
                    (
                        legacy_installation_id,
                        legacy_plan_id,
                        legacy_package_id,
                        legacy_digest,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_site_configuration_versions
                      (version, previous_version, installation_id,
                       package_record_id, package_digest, actor)
                    VALUES (1, 0, %s, %s, %s, 'user:legacy')
                    """,
                    (legacy_installation_id, legacy_package_id, legacy_digest),
                )
                cursor.execute(
                    "UPDATE t_site_configuration_state SET current_version = 1 "
                    "WHERE singleton = TRUE"
                )
                migration_023 = MIGRATION_023.read_text(encoding="utf-8")
                cursor.execute(migration_023)
                cls._create_source_catalog_tables(cursor)
                migration_024 = MIGRATION_024.read_text(encoding="utf-8")
                cursor.execute(migration_024)
                cursor.execute(
                    "SELECT target_installation_id, entity_identity_installation_id "
                    "FROM t_solution_install_plans WHERE id = %s",
                    (legacy_plan_id,),
                )
                self_ids = cursor.fetchone()
                if tuple(str(value) for value in self_ids) != (
                    legacy_plan_id,
                    legacy_plan_id,
                ):
                    raise AssertionError("legacy entity identity was not backfilled")
                cursor.execute(migration_024)
                cursor.execute(
                    "SELECT configuration_digest FROM t_solution_install_plans "
                    "WHERE id = %s",
                    (legacy_plan_id,),
                )
                if cursor.fetchone()[0].strip() != legacy_digest:
                    raise AssertionError("legacy plan configuration digest was not backfilled")
                cursor.execute(
                    "SELECT configuration_digest FROM t_site_configuration_versions "
                    "WHERE version = 1"
                )
                if cursor.fetchone()[0].strip() != legacy_digest:
                    raise AssertionError("legacy site configuration was not backfilled")
                cursor.execute(migration_023)
                migration_025 = MIGRATION_025.read_text(encoding="utf-8")
                cursor.execute(migration_025)
                cursor.execute(migration_025)
                for migration in (MIGRATION_026, MIGRATION_027, MIGRATION_028):
                    cursor.execute(migration.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_028.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_029.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_029.read_text(encoding="utf-8"))
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 't_control_commands' "
                    "AND column_name = 'origin_evidence'"
                )
                if cursor.fetchone() != ("origin_evidence",):
                    raise AssertionError("rule command origin evidence was not migrated")

                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
                for migration in MIGRATIONS:
                    if migration == MIGRATION_024:
                        cls._create_source_catalog_tables(cursor)
                    cursor.execute(migration.read_text(encoding="utf-8"))
                cursor.execute("SELECT to_regclass('public.t_release_locks')")
                if cursor.fetchone() != ("t_release_locks",):
                    raise AssertionError("release lock migration was not applied")
                cursor.execute(
                    """
                    INSERT INTO t_nodes (id, name, source_catalog_key, enabled)
                    VALUES ('40000000-0000-0000-0000-000000000001',
                            'PCS-01', 'PCS-01', TRUE),
                           ('40000000-0000-0000-0000-000000000003',
                            'PCS-01', NULL, TRUE),
                           ('40000000-0000-0000-0000-000000000005',
                            'PCS standby', 'PCS-01-BACKUP', TRUE);
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, unit, read_write, enabled,
                       source_type, source_path)
                    VALUES ('40000000-0000-0000-0000-000000000002',
                            '40000000-0000-0000-0000-000000000001',
                            'ActivePower', 'FLOAT', 'kW', 'R', TRUE,
                            'neuron', 'PCS-01/default/ActivePower');
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, unit, read_write, enabled)
                    VALUES ('40000000-0000-0000-0000-000000000004',
                            '40000000-0000-0000-0000-000000000003',
                            'ActivePower', 'FLOAT', 'kW', 'R', TRUE);
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, unit, read_write, enabled,
                       source_type, source_path)
                    VALUES ('40000000-0000-0000-0000-000000000006',
                            '40000000-0000-0000-0000-000000000005',
                            'ActivePower', 'FLOAT', 'kW', 'R', TRUE,
                            'neuron', 'PCS-01-BACKUP/default/ActivePower');
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, unit, read_write, enabled,
                       source_type, source_path)
                    VALUES ('40000000-0000-0000-0000-000000000007',
                            '40000000-0000-0000-0000-000000000001',
                            'Setpoint', 'FLOAT', 'kW', 'RW', TRUE,
                            'neuron', 'PCS-01/default/Setpoint'),
                           ('40000000-0000-0000-0000-000000000008',
                            '40000000-0000-0000-0000-000000000001',
                            'Readback', 'FLOAT', 'kW', 'R', TRUE,
                            'neuron', 'PCS-01/default/Readback'),
                           ('40000000-0000-0000-0000-000000000009',
                            '40000000-0000-0000-0000-000000000001',
                            'Ready', 'BOOL', NULL, 'R', TRUE,
                            'neuron', 'PCS-01/default/Ready'),
                           ('40000000-0000-0000-0000-000000000010',
                            '40000000-0000-0000-0000-000000000001',
                            'GridPower', 'FLOAT', 'kW', 'R', TRUE,
                            'neuron', 'PCS-01/default/GridPower');
                    """
                )
                # Exercise the controlled legacy-viewer migration path instead
                # of seeding an already-privileged engineer.
                cursor.execute(
                    """
                    INSERT INTO t_users
                      (id, username, password_hash, role, status, auth_version)
                    VALUES (%s, %s, %s, 'viewer', 'role_migration_required', 1)
                    """,
                    (
                        "00000000-0000-0000-0000-000000000102",
                        "delivery-engineer",
                        "legacy-viewer-password-unusable",
                    ),
                )
        cls._provision_user("delivery-admin", "admin", bootstrap=True)
        cls._provision_user("delivery-engineer", "engineer")
        cls._provision_user("delivery-operator", "operator")
        # Prove that the same offline, audited recovery path can rotate a
        # forgotten administrator password after initial bootstrap.
        cls.admin_password = secrets.token_urlsafe(24)
        cls._provision_user(
            "delivery-admin",
            "admin",
            password=cls.admin_password,
        )
        with psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ["DB_PORT"]),
            dbname=cls.db_name,
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT username, id, role, status FROM t_users ORDER BY username"
                )
                identities = {
                    username: (str(user_id), role, status)
                    for username, user_id, role, status in cursor.fetchall()
                }
        cls.admin_id = identities["delivery-admin"][0]
        cls.engineer_id = identities["delivery-engineer"][0]
        cls.operator_id = identities["delivery-operator"][0]
        if identities["delivery-engineer"][1:] != ("engineer", "active"):
            raise AssertionError("legacy viewer was not explicitly migrated")
        try:
            cls._start_server()
        except Exception:
            cls._stop_server()
            raise

    @classmethod
    def _provision_user(
        cls,
        username: str,
        role: str,
        *,
        bootstrap: bool = False,
        password: str | None = None,
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "scripts.bootstrap_admin",
            "--username",
            username,
            "--password-stdin",
        ]
        if not bootstrap:
            command.extend(("--provision-user", "--role", role))
        result = subprocess.run(
            command,
            cwd=BACKEND_ROOT.parent,
            env=cls.server_env,
            input=(password or cls.password) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"identity provisioning failed for {role}: {result.stderr}"
            )
        supplied_password = password or cls.password
        if supplied_password in result.stdout or supplied_password in result.stderr:
            raise AssertionError("identity provisioning reflected the password")

    @classmethod
    def _login(
        cls,
        client: httpx.Client,
        username: str,
        *,
        password: str | None = None,
    ) -> str:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password or cls.password},
        )
        if response.status_code != 200:
            raise AssertionError(response.text)
        return str(response.json()["access_token"])

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
            # This long public seam makes many Loguru writes.  An unread PIPE
            # can fill and block Uvicorn even though the request itself
            # completed, turning test logging into a false HTTP timeout.
            stdout=subprocess.DEVNULL,
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
            admin_token = self._login(
                client,
                "delivery-admin",
                password=self.admin_password,
            )
            engineer_token = self._login(client, "delivery-engineer")
            operator_token = self._login(client, "delivery-operator")
            admin_auth = {"Authorization": f"Bearer {admin_token}"}
            engineer_auth = {"Authorization": f"Bearer {engineer_token}"}
            operator_auth = {"Authorization": f"Bearer {operator_token}"}
            # An explicit offline password reset invalidates every session
            # created under the previous auth_version, even across processes.
            rotated_admin_password = secrets.token_urlsafe(24)
            self._provision_user(
                "delivery-admin",
                "admin",
                password=rotated_admin_password,
            )
            invalidated_admin = client.get("/api/v1/auth/me", headers=admin_auth)
            admin_token = self._login(
                client,
                "delivery-admin",
                password=rotated_admin_password,
            )
            admin_auth = {"Authorization": f"Bearer {admin_token}"}
            imported = client.post(
                "/api/v1/solution-packages/import",
                headers=admin_auth,
                files={
                    "archive": (
                        "minimal.zizu.zip",
                        build_minimal_package(
                            parameters_yaml=(
                                "parameters:\n"
                                "  - id: site.code\n"
                                "    type: string\n"
                                "    required: true\n"
                                "    description: Stable site code\n"
                                "  - id: neuron.credentials\n"
                                "    type: secret\n"
                                "    required: true\n"
                                "    description: Neuron credential reference\n"
                            )
                        ),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            self.assertEqual(invalidated_admin.status_code, 401)
            self.assertEqual(
                invalidated_admin.json()["detail"]["code"],
                "SESSION_REVOKED",
            )
            package = imported.json()
            planned = client.post(
                f"/api/v1/solution-packages/{package['id']}/install-plans",
                headers=engineer_auth,
                json={
                    "parameters": {"site.code": "PG-EMS-01"},
                    "secret_references": {
                        "neuron.credentials": "secret://site/neuron/credentials"
                    },
                },
            )
            self.assertEqual(planned.status_code, 201, planned.text)
            repeated_plan = client.post(
                f"/api/v1/solution-packages/{package['id']}/install-plans",
                headers=engineer_auth,
                json={
                    "parameters": {"site.code": "PG-EMS-01"},
                    "secret_references": {
                        "neuron.credentials": "secret://site/neuron/credentials"
                    },
                },
            )
            self.assertEqual(repeated_plan.status_code, 201, repeated_plan.text)
            self.assertEqual(repeated_plan.json(), planned.json())
            plan = planned.json()
            request = {"plan_digest": plan["digest"]}
            headers = {
                **engineer_auth,
                "Idempotency-Key": "postgres-install-once",
            }
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
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-install-new-key",
                },
            )
            self.assertEqual(repeated_with_new_key.status_code, 201)
            self.assertEqual(repeated_with_new_key.json(), installation)
            run = client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-accept-once",
                },
            )
            self.assertEqual(run.status_code, 201, run.text)
            report = run.json()
            repeated_run = client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-accept-once",
                },
            )
            self.assertEqual(repeated_run.status_code, 201, repeated_run.text)
            self.assertEqual(repeated_run.json(), report)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["actor"], f"user:{self.engineer_id}")
            self.assertIn("started_at", report)
            self.assertIn("finished_at", report)
            self.assertGreaterEqual(report["duration_ms"], 0)
            self.assertGreaterEqual(report["items"][0]["duration_ms"], 0)

            entity_import = client.post(
                "/api/v1/solution-packages/import",
                headers=admin_auth,
                files={
                    "archive": (
                        "single-pcs.zizu.zip",
                        build_entity_package(
                            multiple_devices=True,
                            manual_failover=True,
                        ),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(entity_import.status_code, 201, entity_import.text)
            entity_plan_response = client.post(
                f"/api/v1/solution-packages/{entity_import.json()['id']}/install-plans",
                headers=engineer_auth,
                json={
                    "parameters": {
                        "pcs.instances": [
                            {
                                "instance_key": "PCS-01",
                                "device_key": "PCS-01",
                                "standby_device_key": "PCS-01-BACKUP",
                            }
                        ],
                    }
                },
            )
            self.assertEqual(entity_plan_response.status_code, 201, entity_plan_response.text)
            entity_plan = entity_plan_response.json()
            self.assertEqual("ready", entity_plan["status"])
            entity_install = client.post(
                f"/api/v1/install-plans/{entity_plan['id']}/apply",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-entity-install",
                },
                json={"plan_digest": entity_plan["digest"]},
            )
            self.assertEqual(entity_install.status_code, 201, entity_install.text)
            entity_installation = entity_install.json()
            entity_instance_id = entity_installation["entity_instance_ids"][0]
            protocol_publish = client.post(
                "/protocol-simulator/neuron",
                json={
                    "node": "PCS-01",
                    "group": "default",
                    "timestamp": round(time.time() * 1000),
                    "values": {"ActivePower": 88.5},
                },
            )
            self.assertEqual(protocol_publish.status_code, 200, protocol_publish.text)
            self.assertEqual(protocol_publish.json()["messages_received"], 1)
            self.assertEqual(protocol_publish.json()["points_written"], 1)
            entity_read = client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime",
                headers=operator_auth,
            )
            self.assertEqual(entity_read.status_code, 200, entity_read.text)
            self.assertEqual(entity_read.json()["value"], 88.5)
            backup_publish = client.post(
                "/protocol-simulator/neuron",
                json={
                    "node": "PCS-01-BACKUP",
                    "group": "default",
                    "timestamp": round(time.time() * 1000),
                    "values": {"ActivePower": 77.5},
                },
            )
            self.assertEqual(backup_publish.status_code, 200, backup_publish.text)
            failover = client.post(
                f"/api/v1/entity-instances/{entity_instance_id}/source-failover",
                headers=engineer_auth,
                json={
                    "expected_current_role": "primary",
                    "target_role": "standby",
                    "reason": "Postgres public seam maintenance",
                },
            )
            self.assertEqual(failover.status_code, 200, failover.text)
            self.assertEqual("standby", failover.json()["current_role"])
            standby_read = client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime",
                headers=operator_auth,
            )
            self.assertEqual(standby_read.status_code, 200, standby_read.text)
            self.assertEqual(77.5, standby_read.json()["value"])
            entity_catalog = client.get(
                "/api/v1/entity-instances",
                headers=operator_auth,
            )
            self.assertEqual(entity_catalog.status_code, 200, entity_catalog.text)
            self.assertEqual(
                [entity_instance_id],
                [item["id"] for item in entity_catalog.json()["items"]],
            )
            stable_rule = client.post(
                "/api/v1/rules",
                headers=engineer_auth,
                json={
                    "name": "PCS instance input",
                    "rule_type": "alarm",
                    "jdm_content": {
                        "_config": {
                            "sourceEntityInstanceIds": [entity_instance_id],
                            "inputMappings": {"power": entity_instance_id},
                        }
                    },
                },
            )
            self.assertEqual(stable_rule.status_code, 200, stable_rule.text)
            with psycopg2.connect(
                host=os.environ["DB_HOST"],
                port=int(os.environ["DB_PORT"]),
                dbname=self.db_name,
                user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"],
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT reference_kind, reference_key, entity_instance_id "
                        "FROM t_rule_entity_instance_refs WHERE rule_id = %s "
                        "ORDER BY reference_kind, reference_key",
                        (stable_rule.json()["id"],),
                    )
                    persisted_refs = cursor.fetchall()
            self.assertEqual(
                [("input", "power", entity_instance_id),
                 ("source", "0", entity_instance_id)],
                persisted_refs,
            )
            entity_acceptance = client.post(
                f"/api/v1/solution-installations/{entity_installation['id']}/acceptance-runs",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-entity-acceptance",
                },
            )
            self.assertEqual(entity_acceptance.status_code, 201, entity_acceptance.text)
            self.assertEqual(entity_acceptance.json()["status"], "passed")

            alarm_import = client.post(
                "/api/v1/solution-packages/import",
                headers=admin_auth,
                files={
                    "archive": (
                        "single-pcs-alarm.zizu.zip",
                        build_alarm_entity_package(),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(alarm_import.status_code, 201, alarm_import.text)
            alarm_plan_response = client.post(
                f"/api/v1/solution-packages/{alarm_import.json()['id']}/install-plans",
                headers=engineer_auth,
                json={
                    "parameters": {
                        "pcs.instances": [
                            {
                                "instance_key": "PCS-01",
                                "device_key": "PCS-01",
                                "standby_device_key": "PCS-01-BACKUP",
                            }
                        ],
                    }
                },
            )
            self.assertEqual(alarm_plan_response.status_code, 201, alarm_plan_response.text)
            alarm_plan = alarm_plan_response.json()
            self.assertEqual("ready", alarm_plan["status"])
            self.assertIsNotNone(alarm_plan["alarm_plan"])
            alarm_install = client.post(
                f"/api/v1/install-plans/{alarm_plan['id']}/apply",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-alarm-install",
                },
                json={"plan_digest": alarm_plan["digest"]},
            )
            self.assertEqual(alarm_install.status_code, 201, alarm_install.text)
            alarm_installation = alarm_install.json()
            self.assertEqual([entity_instance_id], alarm_installation["entity_instance_ids"])

            alarm_started_at = round(time.time() * 1000)
            for value, offset_ms in ((101.0, 0), (101.0, 1_100)):
                published_alarm = client.post(
                    "/protocol-simulator/neuron",
                    json={
                        "node": "PCS-01",
                        "timestamp": alarm_started_at + offset_ms,
                        "values": {"ActivePower": value},
                    },
                )
                self.assertEqual(published_alarm.status_code, 200, published_alarm.text)
            events = client.get("/api/v1/alarm-events", headers=operator_auth)
            self.assertEqual(events.status_code, 200, events.text)
            alarm_event = events.json()["items"][0]
            self.assertEqual("active_unacknowledged", alarm_event["state"])
            alarm_event_id = alarm_event["id"]
            acknowledged_alarm = client.post(
                f"/api/v1/alarm-events/{alarm_event_id}/acknowledgements",
                headers=operator_auth,
                json={"note": "Postgres public lifecycle seam"},
            )
            self.assertEqual(acknowledged_alarm.status_code, 200, acknowledged_alarm.text)
            self.assertEqual("active_acknowledged", acknowledged_alarm.json()["state"])
            for value, offset_ms in ((90.0, 2_100), (90.0, 3_200)):
                published_recovery = client.post(
                    "/protocol-simulator/neuron",
                    json={
                        "node": "PCS-01",
                        "timestamp": alarm_started_at + offset_ms,
                        "values": {"ActivePower": value},
                    },
                )
                self.assertEqual(published_recovery.status_code, 200, published_recovery.text)
            recovered_alarm = client.get(
                f"/api/v1/alarm-events/{alarm_event_id}",
                headers=operator_auth,
            )
            self.assertEqual(recovered_alarm.status_code, 200, recovered_alarm.text)
            self.assertEqual("recovered", recovered_alarm.json()["state"])
            alarm_acceptance = client.post(
                f"/api/v1/solution-installations/{alarm_installation['id']}/acceptance-runs",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-alarm-acceptance",
                },
            )
            self.assertEqual(alarm_acceptance.status_code, 201, alarm_acceptance.text)
            alarm_lifecycle_item = next(
                item
                for item in alarm_acceptance.json()["items"]
                if item["acceptance_id"] == "acceptance.pcs-overpower-lifecycle"
            )
            self.assertEqual("ALARM_LIFECYCLE_CONFIRMED", alarm_lifecycle_item["code"])
            self.assertEqual("passed", alarm_lifecycle_item["status"])

            second_import = client.post(
                "/api/v1/solution-packages/import",
                headers=admin_auth,
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
                headers=engineer_auth,
                json={},
            )
            self.assertEqual(
                concurrent_plan_response.status_code,
                201,
                concurrent_plan_response.text,
            )
            concurrent_plan = concurrent_plan_response.json()

        def apply_concurrently() -> httpx.Response:
            return httpx.post(
                f"{self.base_url}/api/v1/install-plans/{concurrent_plan['id']}/apply",
                json={"plan_digest": concurrent_plan["digest"]},
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-concurrent-install",
                },
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
                cursor.execute(
                    "SELECT version, previous_version "
                    "FROM t_site_configuration_versions ORDER BY version"
                )
                versions = cursor.fetchall()
                cursor.execute("SELECT count(*) FROM t_solution_delivery_audit")
                audit_count = cursor.fetchone()[0]
                cursor.execute("SELECT count(*) FROM t_solution_install_plans")
                plan_count = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT event, outcome, actor, target, details::text
                    FROM t_audit_events
                    WHERE event IN ('solution.install', 'solution.acceptance')
                    ORDER BY created_at, id
                    """
                )
                delivery_audits = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT event, outcome, reason, actor, target, request_id,
                           client_ip::text, details::text
                    FROM t_audit_events ORDER BY created_at, id
                    """
                )
                all_audits = cursor.fetchall()
        self.assertEqual(
            versions,
            [(0, None), (1, 0), (2, 1), (3, 2), (4, 3)],
        )
        self.assertEqual(audit_count, 4)
        self.assertEqual(plan_count, 4)

        self._stop_server()
        self._start_server()

        with httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) as client:
            persisted_admin = client.get("/api/v1/auth/me", headers=admin_auth)
            persisted_engineer = client.get("/api/v1/auth/me", headers=engineer_auth)
            persisted_operator = client.get("/api/v1/auth/me", headers=operator_auth)
            packages = client.get("/api/v1/solution-packages", headers=admin_auth)
            installations = client.get(
                "/api/v1/solution-installations",
                headers=engineer_auth,
            )
            persisted_report = client.get(
                f"/api/v1/delivery-reports/{report['id']}",
                headers=operator_auth,
            )
            repeated_after_restart = client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
                headers=headers,
            )
            persisted_configuration = client.get(
                f"/api/v1/site-configuration-versions/"
                f"{installation['site_configuration_version']}",
                headers=engineer_auth,
            )
            persisted_plan = client.get(
                f"/api/v1/install-plans/{plan['id']}",
                headers=engineer_auth,
            )
            persisted_entity = client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/realtime",
                headers=operator_auth,
            )
            persisted_failover = client.get(
                f"/api/v1/entity-instances/{entity_instance_id}/source-failover",
                headers=engineer_auth,
            )
            persisted_alarm_event = client.get(
                f"/api/v1/alarm-events/{alarm_event_id}",
                headers=operator_auth,
            )
            repeated_entity_install = client.post(
                f"/api/v1/install-plans/{entity_plan['id']}/apply",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-entity-install",
                },
                json={"plan_digest": entity_plan["digest"]},
            )
            repeated_acceptance_after_restart = client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-accept-once",
                },
            )
            fresh_acceptance_after_restart = client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-accept-after-restart",
                },
            )
            control_import = client.post(
                "/api/v1/solution-packages/import",
                headers=admin_auth,
                files={
                    "archive": (
                        "controllable-pcs.zizu.zip",
                        build_control_entity_package(),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(control_import.status_code, 201, control_import.text)
            control_plan_response = client.post(
                f"/api/v1/solution-packages/{control_import.json()['id']}/install-plans",
                headers=engineer_auth,
                json={
                    "parameters": {
                        "pcs.instance_key": "PCS-CONTROL",
                        "pcs.device_key": "PCS-01",
                    }
                },
            )
            self.assertEqual(control_plan_response.status_code, 201, control_plan_response.text)
            control_plan = control_plan_response.json()
            self.assertEqual("ready", control_plan["status"], control_plan)
            control_install = client.post(
                f"/api/v1/install-plans/{control_plan['id']}/apply",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-control-install",
                },
                json={"plan_digest": control_plan["digest"]},
            )
            self.assertEqual(control_install.status_code, 201, control_install.text)
            control_ids = {
                item["definition_id"]: item["entity_instance_id"]
                for item in control_plan["items"]
                if item["kind"] == "entity_binding"
            }
            ready_publish = client.post(
                "/protocol-simulator/neuron",
                json={
                    "node": "PCS-01",
                    "timestamp": round(time.time() * 1000),
                    "values": {"Ready": True, "Readback": 0.0},
                },
            )
            self.assertEqual(ready_publish.status_code, 200, ready_publish.text)
            submitted_control = client.post(
                "/api/v1/neuron/write",
                headers={
                    **operator_auth,
                    "Idempotency-Key": "postgres-control-once",
                },
                json={
                    "node": "PCS-01",
                    "group": "default",
                    "tag": "Setpoint",
                    "value": 20.0,
                },
            )
            self.assertEqual(submitted_control.status_code, 201, submitted_control.text)
            self.assertEqual("failed", submitted_control.json()["status"])
            self.assertEqual("CONTROL_DISPATCH_FAILED", submitted_control.json()["code"])
            self.assertEqual("control.write", submitted_control.json()["capability"])
            self.assertNotIn("neuron", repr(submitted_control.json()).casefold())
            self.assertEqual(
                "/api/v1/entity-instances/{id}/control-commands",
                submitted_control.json()["migration"]["replacement"],
            )
            self.assertEqual(
                f"/api/v1/control-commands/{submitted_control.json()['id']}",
                submitted_control.json()["links"]["command"],
            )

            repeated_legacy_rpc = client.post(
                "/api/v1/devices/40000000-0000-0000-0000-000000000001/rpc",
                headers={
                    **operator_auth,
                    "Idempotency-Key": "postgres-control-once",
                },
                json={
                    "command": "pcs.setpoint",
                    "payload": {"value": 20.0},
                    "topic": "ignored/arbitrary/topic",
                },
            )
            self.assertEqual(repeated_legacy_rpc.status_code, 201, repeated_legacy_rpc.text)
            self.assertEqual(
                repeated_legacy_rpc.json()["id"],
                submitted_control.json()["id"],
            )

            repeated_rpc = client.post(
                "/api/v1/devices/40000000-0000-0000-0000-000000000001/rpc",
                headers={
                    **operator_auth,
                    "Idempotency-Key": "postgres-control-once",
                },
                json={
                    "entity_instance_id": control_ids["pcs.setpoint"],
                    "value": 20.0,
                },
            )
            self.assertEqual(repeated_rpc.status_code, 201, repeated_rpc.text)
            self.assertEqual(repeated_rpc.json()["id"], submitted_control.json()["id"])
            self.assertEqual("compatibility", repeated_rpc.json()["source_type"])

        self.assertEqual(persisted_admin.status_code, 200, persisted_admin.text)
        self.assertEqual(persisted_engineer.status_code, 200, persisted_engineer.text)
        self.assertEqual(persisted_operator.status_code, 200, persisted_operator.text)
        self.assertEqual(
            persisted_configuration.status_code,
            200,
            persisted_configuration.text,
        )
        self.assertEqual(
            persisted_configuration.json()["parameters"],
            {"site.code": "PG-EMS-01"},
        )
        self.assertEqual(
            persisted_configuration.json()["secret_references"],
            {"neuron.credentials": "secret://site/neuron/credentials"},
        )
        self.assertEqual(persisted_plan.status_code, 200, persisted_plan.text)
        self.assertEqual(persisted_plan.json(), plan)
        self.assertEqual(persisted_entity.status_code, 200, persisted_entity.text)
        self.assertEqual(persisted_entity.json()["value"], 77.5)
        self.assertEqual(persisted_failover.status_code, 200, persisted_failover.text)
        self.assertEqual("standby", persisted_failover.json()["current_role"])
        self.assertEqual(1, persisted_failover.json()["switch_count"])
        self.assertEqual(1, len(persisted_failover.json()["audit"]))
        self.assertEqual(repeated_entity_install.json(), entity_installation)
        persisted_packages = packages.json()
        self.assertEqual(persisted_packages["total"], 4)
        self.assertIn(package, persisted_packages["items"])
        self.assertEqual(installations.json()["items"][0], installation)
        self.assertEqual(installations.json()["total"], 4)
        self.assertEqual(persisted_alarm_event.status_code, 200, persisted_alarm_event.text)
        self.assertEqual("recovered", persisted_alarm_event.json()["state"])
        self.assertEqual(persisted_report.json(), report)
        self.assertEqual(repeated_after_restart.json(), installation)
        self.assertEqual(repeated_acceptance_after_restart.status_code, 201)
        self.assertEqual(repeated_acceptance_after_restart.json(), report)
        self.assertEqual(fresh_acceptance_after_restart.status_code, 201)
        self.assertNotEqual(fresh_acceptance_after_restart.json()["id"], report["id"])
        self.assertEqual(
            [(event, outcome) for event, outcome, *_ in delivery_audits],
            [
                ("solution.install", "allowed"),
                ("solution.acceptance", "allowed"),
                ("solution.install", "allowed"),
                ("solution.acceptance", "allowed"),
                ("solution.install", "allowed"),
                ("solution.acceptance", "allowed"),
                ("solution.install", "allowed"),
            ],
        )
        self.assertTrue(
            all(
                actor == f"user:{self.engineer_id}"
                for _, _, actor, _, _ in delivery_audits
            )
        )
        self.assertTrue(
            all(
                '"configuration_digest"' in details
                for event, _, _, _, details in delivery_audits
                if event == "solution.install"
            )
        )
        serialized_audits = repr(all_audits)
        self.assertNotIn(self.password, serialized_audits)
        self.assertNotIn(self.admin_password, serialized_audits)
        self.assertNotIn(rotated_admin_password, serialized_audits)
        self.assertNotIn(admin_token, serialized_audits)
        self.assertNotIn(engineer_token, serialized_audits)
        self.assertNotIn(operator_token, serialized_audits)

        v1_upgrade_package = build_minimal_package(
            package_id="org.zizu.postgres-three-way-upgrade",
            package_version="1.0.0",
            parameters_yaml=(
                "parameters:\n"
                "  - id: site.name\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-name-v1\n"
                "    description: Site display name\n"
                "  - id: site.mode\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-mode-v1\n"
                "    description: Package-owned mode\n"
            ),
        )
        v2_upgrade_package = build_minimal_package(
            package_id="org.zizu.postgres-three-way-upgrade",
            package_version="1.1.0",
            parameters_yaml=(
                "parameters:\n"
                "  - id: site.name\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-name-v2\n"
                "    description: Site display name\n"
                "  - id: site.mode\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-mode-v2\n"
                "    description: Package-owned mode\n"
            ),
        )
        with httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) as client:
            imported_v1 = client.post(
                "/api/v1/solution-packages/import",
                headers=admin_auth,
                files={
                    "archive": (
                        "postgres-three-way-v1.zizu.zip",
                        v1_upgrade_package,
                        "application/zip",
                    )
                },
            )
            self.assertEqual(imported_v1.status_code, 201, imported_v1.text)
            planned_v1 = client.post(
                f"/api/v1/solution-packages/{imported_v1.json()['id']}/install-plans",
                headers=engineer_auth,
                json={"parameters": {"site.name": "customer-override"}},
            )
            self.assertEqual(planned_v1.status_code, 201, planned_v1.text)
            installed_v1 = client.post(
                f"/api/v1/install-plans/{planned_v1.json()['id']}/apply",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-three-way-v1",
                },
                json={"plan_digest": planned_v1.json()["digest"]},
            )
            self.assertEqual(installed_v1.status_code, 201, installed_v1.text)

        self._stop_server()
        self._start_server()

        with httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) as client:
            imported_v2 = client.post(
                "/api/v1/solution-packages/import",
                headers=admin_auth,
                files={
                    "archive": (
                        "postgres-three-way-v2.zizu.zip",
                        v2_upgrade_package,
                        "application/zip",
                    )
                },
            )
            self.assertEqual(imported_v2.status_code, 201, imported_v2.text)
            blocked_plan = client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                headers=engineer_auth,
                json={},
            )
            self.assertEqual(blocked_plan.status_code, 201, blocked_plan.text)
            self.assertEqual("blocked", blocked_plan.json()["status"])
            self.assertIn(
                {
                    "code": "UPGRADE_PARAMETER_CONFLICT",
                    "parameter_id": "site.name",
                    "message": "Package and site override both changed this parameter",
                },
                blocked_plan.json()["blockers"],
            )
            blocked_apply = client.post(
                f"/api/v1/install-plans/{blocked_plan.json()['id']}/apply",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-three-way-v2-blocked",
                },
                json={"plan_digest": blocked_plan.json()["digest"]},
            )
            self.assertEqual(409, blocked_apply.status_code, blocked_apply.text)
            self.assertEqual(
                "INSTALL_PLAN_BLOCKED", blocked_apply.json()["detail"]["code"]
            )
            retained = client.get(
                "/api/v1/site-configuration-versions/"
                f"{installed_v1.json()['site_configuration_version']}",
                headers=engineer_auth,
            )
            self.assertEqual(retained.status_code, 200, retained.text)
            self.assertEqual(
                {"site.name": "customer-override", "site.mode": "package-mode-v1"},
                retained.json()["parameters"],
            )
            resolved_plan = client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                headers=engineer_auth,
                json={"parameters": {"site.name": "engineer-resolved-v2"}},
            )
            self.assertEqual("ready", resolved_plan.json()["status"])
            installed_v2 = client.post(
                f"/api/v1/install-plans/{resolved_plan.json()['id']}/apply",
                headers={
                    **engineer_auth,
                    "Idempotency-Key": "postgres-three-way-v2-resolved",
                },
                json={"plan_digest": resolved_plan.json()["digest"]},
            )
            self.assertEqual(installed_v2.status_code, 201, installed_v2.text)
            resolved_configuration = client.get(
                "/api/v1/site-configuration-versions/"
                f"{installed_v2.json()['site_configuration_version']}",
                headers=engineer_auth,
            )
            self.assertEqual(resolved_configuration.status_code, 200)
            self.assertEqual(
                {"site.name": "engineer-resolved-v2", "site.mode": "package-mode-v2"},
                resolved_configuration.json()["parameters"],
            )


if __name__ == "__main__":
    unittest.main()
