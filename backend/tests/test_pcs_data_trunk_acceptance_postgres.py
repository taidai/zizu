from __future__ import annotations

import os
import importlib.util
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import unittest

import httpx
import psycopg2
from websockets.sync.client import connect as websocket_connect

from tests import test_data_trunk_migration_postgres as data_migration_test


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_SCRIPT = BACKEND_ROOT.parent / "scripts" / "build_reference_delivery.py"
REFERENCE_SPEC = importlib.util.spec_from_file_location(
    "build_reference_delivery",
    REFERENCE_SCRIPT,
)
assert REFERENCE_SPEC is not None and REFERENCE_SPEC.loader is not None
reference_builder = importlib.util.module_from_spec(REFERENCE_SPEC)
REFERENCE_SPEC.loader.exec_module(reference_builder)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the PCS data-trunk public seam",
)
class PcsDataTrunkAcceptancePostgresTest(unittest.TestCase):
    process: subprocess.Popen[str] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("PCS data-trunk tests require a *_test database")
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
                data_migration_test.DataTrunkMigrationPostgresTest._reset_through_037(cursor)
                data_migration_test.DataTrunkMigrationPostgresTest._apply_038(cursor)
                data_migration_test.DataTrunkMigrationPostgresTest._apply_039(cursor)
                data_migration_test.DataTrunkMigrationPostgresTest._apply_040(cursor)
                cls._seed_sources(cursor)

        cls.port = cls._free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.password = secrets.token_urlsafe(24)
        cls.server_env = os.environ.copy()
        cls.server_env.update(
            {
                "PUBLIC_API_BASE_URL": cls.base_url,
                "DEPLOYMENT_MODE": "development",
                "AUTH_REQUIRE_HTTPS": "false",
                "DB_HOST": cls.connection_kwargs["host"],
                "DB_PORT": str(cls.connection_kwargs["port"]),
                "DB_NAME": cls.db_name,
                "DB_USER": cls.connection_kwargs["user"],
                "DB_PASSWORD": cls.connection_kwargs["password"],
                "NEURON_PASSWORD": "test-neuron-password",
                "NANOMQ_API_PASSWORD": "test-nanomq-password",
                "JWT_SECRET": "test-jwt-secret-0000000000000000",
            }
        )
        cls._provision_user("pcs-engineer", "engineer", bootstrap=True)
        cls._provision_user("pcs-operator", "operator")
        cls._start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_server()

    @classmethod
    def _provision_user(cls, username: str, role: str, *, bootstrap: bool = False) -> None:
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
            input=cls.password + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)

    @staticmethod
    def _seed_sources(cursor) -> None:
        nodes = {
            "PCS-01": (
                ("ActivePowerRaw", "FLOAT", "W"),
                ("RunningState", "STRING", None),
                ("FaultCodeText", "STRING", None),
                ("PActKw", "FLOAT", "kW"),
                ("ModeCode", "STRING", None),
                ("AlarmList", "STRING", None),
                ("ActivePowerSetpoint", "FLOAT", "kW"),
                ("ActivePowerReadback", "FLOAT", "kW"),
                ("BmsReady", "BOOL", None),
            ),
            "BMS-01": (("StateOfCharge", "FLOAT", "%"),),
            "PV-01": (("ActivePower", "FLOAT", "kW"),),
            "EVSE-01": (("ActivePower", "FLOAT", "kW"),),
            "METER-01": (("ActivePower", "FLOAT", "kW"),),
        }
        for node_index, (node_key, tags) in enumerate(nodes.items(), start=1):
            node_id = f"87000000-0000-0000-0000-{node_index:012d}"
            cursor.execute(
                "INSERT INTO t_nodes (id, name, source_catalog_key) VALUES (%s,%s,%s)",
                (node_id, node_key, node_key),
            )
            for tag_index, (name, data_type, unit) in enumerate(tags, start=1):
                tag_id = f"87000000-0000-0000-{node_index:04d}-{tag_index:012d}"
                direction = "RW" if name == "ActivePowerSetpoint" else "R"
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id, node_id, name, data_type, unit, read_write, enabled,
                       source_type, source_path, unit_from)
                    VALUES (%s,%s,%s,%s,%s,%s,TRUE,'neuron',%s,%s)
                    """,
                    (
                        tag_id,
                        node_id,
                        name,
                        data_type,
                        unit,
                        direction,
                        f"{node_key}/default/{name}",
                        unit,
                    ),
                )

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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise RuntimeError("PCS data-trunk test server exited early")
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
        raise RuntimeError("PCS data-trunk test server did not become ready")

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
            cls.process = None

    @classmethod
    def _login(cls, client: httpx.Client, username: str) -> str:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": cls.password},
        )
        if response.status_code != 200:
            raise AssertionError(response.text)
        return str(response.json()["access_token"])

    def test_two_brand_pcs_data_trunk_survives_public_protocol_ws_and_restart(self) -> None:
        with httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) as client:
            engineer_token = self._login(client, "pcs-engineer")
            operator_token = self._login(client, "pcs-operator")
            engineer = {"Authorization": f"Bearer {engineer_token}"}
            operator = {"Authorization": f"Bearer {operator_token}"}
            imported = client.post(
                "/api/v1/solution-packages/import",
                headers=engineer,
                files={
                    "archive": (
                        "pv-storage-charging-ems.zizu.zip",
                        reference_builder.build_archive(),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(201, imported.status_code, imported.text)
            templates = client.get(
                "/api/v1/point-processing-templates?device_category=PCS",
                headers=engineer,
            )
            self.assertEqual(200, templates.status_code, templates.text)
            by_asset = {
                item["asset_id"]: item["revision_id"]
                for item in templates.json()["items"]
            }
            self.assertEqual({"pcs.brand-a", "pcs.brand-b"}, set(by_asset))
            node_id = "87000000-0000-0000-0000-000000000001"
            planned = client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                headers=engineer,
                json={
                    "parameters": {
                        "pcs.instances": [
                            {"instance_key": "PCS-01", "device_key": "PCS-01"}
                        ],
                        "bms.instances": [
                            {"instance_key": "BMS-01", "device_key": "BMS-01"}
                        ],
                        "pv.instances": [
                            {"instance_key": "PV-01", "device_key": "PV-01"}
                        ],
                        "evse.instances": [
                            {"instance_key": "EVSE-01", "device_key": "EVSE-01"}
                        ],
                        "meter.instance_key": "METER-01",
                        "meter.device_key": "METER-01",
                    },
                    "secret_references": {
                        "gateway.credentials": "secret://reference/gateway"
                    },
                    "point_processings": [
                        {
                            "node_id": node_id,
                            "template_revision_id": by_asset["pcs.brand-a"],
                        }
                    ],
                },
            )
            self.assertEqual(201, planned.status_code, planned.text)
            self.assertEqual("ready", planned.json()["status"], planned.text)
            installed = client.post(
                f"/api/v1/install-plans/{planned.json()['id']}/apply",
                headers={**engineer, "Idempotency-Key": "pcs-brand-a-install"},
                json={"plan_digest": planned.json()["digest"]},
            )
            self.assertEqual(201, installed.status_code, installed.text)
            entity_ids = {
                item["definition_id"]: item["entity_instance_id"]
                for item in planned.json()["items"]
                if item["kind"] == "entity_binding"
                and item.get("source_kind") == "point_processing"
            }
            self.assertEqual(
                {"pcs.active_power", "pcs.operating_state", "pcs.fault_codes"},
                set(entity_ids),
            )

            def publish(values: dict[str, object]) -> httpx.Response:
                return client.post(
                    "/protocol-simulator/neuron",
                    json={
                        "node": "PCS-01",
                        "group": "default",
                        "timestamp": round(time.time() * 1000),
                        "values": values,
                    },
                )

            first = publish(
                {
                    "ActivePowerRaw": 12345,
                    "RunningState": "2",
                    "FaultCodeText": "E30;E11",
                }
            )
            self.assertEqual(200, first.status_code, first.text)
            expected_values = {
                "pcs.active_power": 12.345,
                "pcs.operating_state": "RUNNING",
                "pcs.fault_codes": ["COMPRESSOR_FAULT", "DC_OVERVOLTAGE"],
            }
            for definition_id, entity_id in entity_ids.items():
                realtime = client.get(
                    f"/api/v1/entity-instances/{entity_id}/realtime",
                    headers=operator,
                )
                self.assertEqual(200, realtime.status_code, realtime.text)
                self.assertEqual(expected_values[definition_id], realtime.json()["value"])
                self.assertEqual(192, realtime.json()["quality"])

            ticket = client.post("/api/v1/auth/ws-ticket", headers=operator)
            self.assertEqual(201, ticket.status_code, ticket.text)
            with websocket_connect(
                f"ws://127.0.0.1:{self.port}/api/v1/ws/entity-observations",
                open_timeout=5,
            ) as websocket:
                import json

                websocket.send(json.dumps({"authenticate": {"ticket": ticket.json()["ticket"]}}))
                self.assertEqual("authenticated", json.loads(websocket.recv())["type"])
                websocket.send(json.dumps({"subscribe": list(entity_ids.values())}))
                self.assertEqual("subscribed", json.loads(websocket.recv())["type"])
                streamed = publish(
                    {
                        "ActivePowerRaw": 13000,
                        "RunningState": "2",
                        "FaultCodeText": "E30;E11",
                    }
                )
                self.assertEqual(200, streamed.status_code, streamed.text)
                events = [json.loads(websocket.recv(timeout=5)) for _ in range(3)]
                self.assertEqual(
                    set(entity_ids.values()),
                    {item["entity_instance_id"] for item in events},
                )
                self.assertEqual(3, len({item["event_id"] for item in events}))

            replacement = client.post(
                f"/api/v1/nodes/{node_id}/point-processing-plans",
                headers=engineer,
                json={"template_revision_id": by_asset["pcs.brand-b"]},
            )
            self.assertEqual(201, replacement.status_code, replacement.text)
            replaced = client.post(
                f"/api/v1/point-processing-plans/{replacement.json()['id']}/apply",
                headers={**engineer, "Idempotency-Key": "pcs-brand-b-replace"},
                json={"plan_digest": replacement.json()["digest"]},
            )
            self.assertEqual(201, replaced.status_code, replaced.text)
            self.assertEqual(
                set(entity_ids.values()),
                set(replaced.json()["output_entity_instance_ids"]),
            )
            brand_b = publish(
                {"PActKw": 13.5, "ModeCode": "R", "AlarmList": "C30,D11"}
            )
            self.assertEqual(200, brand_b.status_code, brand_b.text)
            active_power = client.get(
                f"/api/v1/entity-instances/{entity_ids['pcs.active_power']}/realtime",
                headers=operator,
            )
            self.assertEqual(13.5, active_power.json()["value"])
            expected_brand_b = {
                "pcs.active_power": 13.5,
                "pcs.operating_state": "RUNNING",
                "pcs.fault_codes": [
                    "COMPRESSOR_FAULT",
                    "DC_OVERVOLTAGE",
                ],
            }
            for definition_id, entity_id in entity_ids.items():
                realtime = client.get(
                    f"/api/v1/entity-instances/{entity_id}/realtime",
                    headers=operator,
                )
                self.assertEqual(200, realtime.status_code, realtime.text)
                self.assertEqual(
                    expected_brand_b[definition_id],
                    realtime.json()["value"],
                )
                self.assertEqual(192, realtime.json()["quality"])

            report = client.post(
                f"/api/v1/solution-installations/{replaced.json()['solution_installation_id']}/acceptance-runs",
                headers={**engineer, "Idempotency-Key": "pcs-data-trunk-report"},
                json={},
            )
            self.assertEqual(201, report.status_code, report.text)
            data_item = next(
                item
                for item in report.json()["items"]
                if item["acceptance_id"] == "acceptance.pcs-data-trunk"
            )
            self.assertEqual("passed", data_item["status"], data_item)
            report_body = report.json()

        self._stop_server()
        self._start_server()
        with httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) as client:
            persisted = client.get(
                f"/api/v1/delivery-reports/{report_body['id']}",
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            self.assertEqual(200, persisted.status_code, persisted.text)
            self.assertEqual(report_body, persisted.json())
            after_restart = client.get(
                f"/api/v1/entity-instances/{entity_ids['pcs.active_power']}/realtime",
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            self.assertEqual(200, after_restart.status_code, after_restart.text)
            self.assertEqual(13.5, after_restart.json()["value"])


if __name__ == "__main__":
    unittest.main()
