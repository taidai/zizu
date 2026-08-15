from __future__ import annotations

import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import unittest

import httpx
import psycopg2

from tests.test_alarm_configuration_postgres import (
    _PostgresAlarmConfigurationTestBase,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the isolated public acceptance seam",
)
class AlarmConfigurationAcceptancePostgresPublicTest(unittest.TestCase):
    """One public protocol-to-six-alarms-to-report proof across restart."""

    process: subprocess.Popen[str] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Postgres acceptance tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }
        cls.port = cls._free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.password = secrets.token_urlsafe(24)
        cls.server_env = os.environ.copy()
        cls.server_env.update({
            "PUBLIC_API_BASE_URL": cls.base_url,
            "DEPLOYMENT_MODE": "development",
            "AUTH_REQUIRE_HTTPS": "false",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        })

        fixture = _PostgresAlarmConfigurationTestBase()
        fixture.connection_kwargs = cls.connection_kwargs
        fixture._reset_schema_through_032()
        with psycopg2.connect(**cls.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cls.source_installation_id, _ = fixture._insert_installed_site(cursor)
                fixture._insert_entities(cursor, cls.source_installation_id)
                cursor.execute(
                    """
                    UPDATE t_tags tag
                    SET source_type = 'neuron',
                        source_path = node.name || '/default/' || tag.name,
                        range_max = 600
                    FROM t_nodes node
                    WHERE node.id = tag.node_id
                    """
                )
                fixture._apply_alarm_migrations(cursor)
                cursor.execute(
                    """
                    SELECT tag.id, tag.alarm_level, tag.alarm_type,
                           tag.alarm_threshold, tag.fault_map_id
                    FROM t_tags tag ORDER BY tag.id
                    """
                )
                cls.legacy_tag_snapshot = cursor.fetchall()
                cursor.execute("SELECT count(*) FROM t_alarm_levels")
                cls.legacy_level_count = cursor.fetchone()[0]
                cursor.execute("SELECT count(*) FROM t_entity_alarm_bindings")
                cls.legacy_binding_count = cursor.fetchone()[0]
                cursor.execute("SELECT count(*) FROM t_alarms")
                cls.legacy_alarm_count = cursor.fetchone()[0]

        cls._provision_user("acceptance-bootstrap", "admin", bootstrap=True)
        cls._provision_user("acceptance-engineer", "engineer")
        cls._provision_user("acceptance-operator", "operator")
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
            raise AssertionError(
                f"identity provisioning failed for {role}: {result.stderr}"
            )
        if cls.password in result.stdout or cls.password in result.stderr:
            raise AssertionError("identity provisioning reflected the password")

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
                raise RuntimeError("acceptance test server exited early")
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
        raise RuntimeError("acceptance test server did not become ready")

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
    def _login(cls, client: httpx.Client, username: str) -> dict[str, str]:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": cls.password},
        )
        if response.status_code != 200:
            raise AssertionError(response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def _publish(
        self,
        client: httpx.Client,
        *,
        node: str,
        value: float,
        timestamp: int,
    ) -> None:
        response = client.post(
            "/protocol-simulator/neuron",
            json={
                "node": node,
                "group": "default",
                "timestamp": timestamp,
                "values": {"ActivePower": value},
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            {"messages_received": 1, "points_written": 1},
            response.json(),
        )

    def test_public_protocol_alarm_acceptance_survives_restart(self) -> None:
        with httpx.Client(
            base_url=self.base_url,
            timeout=20,
            trust_env=False,
        ) as client:
            engineer = self._login(client, "acceptance-engineer")
            operator = self._login(client, "acceptance-operator")
            engineer_identity = client.get("/api/v1/auth/me", headers=engineer)
            operator_identity = client.get("/api/v1/auth/me", headers=operator)
            self.assertEqual("engineer", engineer_identity.json()["user"]["role"])
            self.assertEqual("operator", operator_identity.json()["user"]["role"])

            entities_response = client.get("/api/v1/entity-instances", headers=operator)
            self.assertEqual(200, entities_response.status_code, entities_response.text)
            entities = sorted(
                (
                    item for item in entities_response.json()["items"]
                    if item["definition_id"] == "pcs.activePower" and item["confirmed"]
                ),
                key=lambda item: item["instance_key"],
            )
            self.assertEqual(["PCS-01", "PCS-02"], [item["instance_key"] for item in entities])

            rules_response = client.post(
                "/api/v1/alarm-rule-sets",
                headers=engineer,
                json={
                    "key": "acceptance-three-severity",
                    "name": "Acceptance three severity",
                    "rules": [
                        {
                            "id": "warning",
                            "name": "Warning active power",
                            "severity": "WARNING",
                            "trigger": {"operator": "gt", "value": 450},
                            "trigger_duration_seconds": 2,
                            "recovery": {"operator": "lte", "value": 430},
                            "recovery_duration_seconds": 2,
                            "notification_throttle_seconds": 0,
                            "unit": "kW",
                        },
                        {
                            "id": "major",
                            "name": "Major active power",
                            "severity": "MAJOR",
                            "trigger": {"operator": "gt", "value": 500},
                            "trigger_duration_seconds": 2,
                            "recovery": {"operator": "lte", "value": 480},
                            "recovery_duration_seconds": 2,
                            "notification_throttle_seconds": 0,
                            "unit": "kW",
                        },
                        {
                            "id": "critical",
                            "name": "Critical active power",
                            "severity": "CRITICAL",
                            "trigger": {"operator": "gt", "value": 550},
                            "trigger_duration_seconds": 2,
                            "recovery": {"operator": "lte", "value": 530},
                            "recovery_duration_seconds": 2,
                            "notification_throttle_seconds": 0,
                            "unit": "kW",
                        },
                    ],
                },
            )
            self.assertEqual(201, rules_response.status_code, rules_response.text)
            revision = rules_response.json()
            self.assertEqual(3, len(revision["rules"]))

            plan_response = client.post(
                "/api/v1/alarm-configuration-plans",
                headers=engineer,
                json={
                    "installation_id": self.source_installation_id,
                    "selection": {
                        "entity_instance_ids": [item["id"] for item in entities],
                    },
                    "rule_set_id": revision["rule_set_id"],
                    "rule_set_revision": revision["revision"],
                },
            )
            self.assertEqual(201, plan_response.status_code, plan_response.text)
            plan = plan_response.json()
            self.assertEqual("ready", plan["status"])
            self.assertEqual(6, len(plan["items"]))
            self.assertEqual([], plan["blockers"])
            inspected_plan = client.get(
                f"/api/v1/alarm-configuration-plans/{plan['id']}",
                headers=engineer,
            )
            self.assertEqual(200, inspected_plan.status_code, inspected_plan.text)
            self.assertEqual(plan, inspected_plan.json())

            applied_response = client.post(
                f"/api/v1/alarm-configuration-plans/{plan['id']}/apply",
                headers={**engineer, "Idempotency-Key": "public-six-definition-apply"},
                json={"plan_digest": plan["digest"]},
            )
            self.assertEqual(200, applied_response.status_code, applied_response.text)
            applied = applied_response.json()
            self.assertEqual(6, len(applied["definition_ids"]))

            base_ms = round(time.time() * 1000) - 40_000
            for node in ("PCS-01", "PCS-02"):
                self._publish(client, node=node, value=400, timestamp=base_ms - 1_000)

            self._publish(client, node="PCS-01", value=650, timestamp=base_ms)
            after_bad_quality = client.get("/api/v1/alarm-events", headers=operator)
            self.assertEqual(200, after_bad_quality.status_code, after_bad_quality.text)
            self.assertEqual(0, after_bad_quality.json()["total"])

            self._publish(client, node="PCS-01", value=580, timestamp=base_ms + 1_000)
            first_pending = client.get("/api/v1/alarm-events", headers=operator)
            self.assertEqual(3, first_pending.json()["total"])
            self.assertEqual(
                {"pending"},
                {item["state"] for item in first_pending.json()["items"]},
            )
            first_pending_ids = {item["id"] for item in first_pending.json()["items"]}

            self._publish(client, node="PCS-01", value=580, timestamp=base_ms + 32_000)
            after_gap = client.get("/api/v1/alarm-events", headers=operator)
            self.assertEqual(0, after_gap.json()["total"])
            self.assertEqual(0, after_gap.json()["summary"]["unacknowledged"])

            for node in ("PCS-01", "PCS-02"):
                self._publish(client, node=node, value=580, timestamp=base_ms + 32_100)
            restarted_pending = client.get("/api/v1/alarm-events", headers=operator)
            self.assertEqual(6, restarted_pending.json()["total"])
            self.assertEqual(
                {"pending"},
                {item["state"] for item in restarted_pending.json()["items"]},
            )
            self.assertTrue(
                first_pending_ids.isdisjoint(
                    {item["id"] for item in restarted_pending.json()["items"]}
                )
            )

            for node in ("PCS-01", "PCS-02"):
                self._publish(client, node=node, value=580, timestamp=base_ms + 34_200)
            active_response = client.get("/api/v1/alarm-events", headers=operator)
            self.assertEqual(200, active_response.status_code, active_response.text)
            active_events = active_response.json()["items"]
            self.assertEqual(6, len(active_events))
            self.assertEqual(
                {"active_unacknowledged"},
                {item["state"] for item in active_events},
            )
            self.assertEqual(
                {"CRITICAL": 2, "MAJOR": 2, "WARNING": 2, "INFO": 0},
                active_response.json()["summary"]["by_severity"],
            )

            acknowledgement_ids = set()
            for event in active_events:
                acknowledgement = client.post(
                    f"/api/v1/alarm-events/{event['id']}/acknowledgements",
                    headers=operator,
                    json={"note": "Public PostgreSQL acceptance"},
                )
                self.assertEqual(200, acknowledgement.status_code, acknowledgement.text)
                self.assertEqual("active_acknowledged", acknowledgement.json()["state"])
                acknowledgement_ids.add(acknowledgement.json()["audit_event_id"])
            self.assertEqual(6, len(acknowledgement_ids))
            self.assertNotIn(None, acknowledgement_ids)

            recovery_ms = round(time.time() * 1000) + 100
            for offset in (0, 2_100):
                for node in ("PCS-01", "PCS-02"):
                    self._publish(
                        client,
                        node=node,
                        value=400,
                        timestamp=recovery_ms + offset,
                    )
            recovered_response = client.get(
                "/api/v1/alarm-events?state=recovered&page_size=200",
                headers=operator,
            )
            self.assertEqual(200, recovered_response.status_code, recovered_response.text)
            recovered_events = recovered_response.json()["items"]
            self.assertEqual(6, len(recovered_events))
            self.assertEqual(
                {"CRITICAL": 2, "MAJOR": 2, "WARNING": 2},
                {
                    severity: sum(item["severity"] == severity for item in recovered_events)
                    for severity in ("CRITICAL", "MAJOR", "WARNING")
                },
            )

            timelines = {}
            for event in recovered_events:
                timeline = client.get(
                    f"/api/v1/alarm-events/{event['id']}/transitions",
                    headers=operator,
                )
                self.assertEqual(200, timeline.status_code, timeline.text)
                timelines[event["id"]] = timeline.json()
                self.assertEqual(
                    [
                        "ALARM_TRIGGER_PENDING",
                        "ALARM_ACTIVATED",
                        "ALARM_ACKNOWLEDGED",
                        "ALARM_RECOVERED",
                    ],
                    [item["code"] for item in timeline.json()["items"]],
                )

            acceptance_response = client.post(
                f"/api/v1/alarm-configuration-applications/{applied['id']}/acceptance",
                headers={**engineer, "Idempotency-Key": "public-six-acceptance"},
            )
            self.assertEqual(200, acceptance_response.status_code, acceptance_response.text)
            report = acceptance_response.json()
            self.assertEqual("passed", report["status"])
            self.assertEqual(6, len(report["items"]))
            self.assertEqual(
                {"ALARM_ACCEPTANCE_PASSED"},
                {item["code"] for item in report["items"]},
            )
            self.assertEqual(
                set(applied["definition_ids"]),
                {item["definition_id"] for item in report["items"]},
            )

        self._stop_server()
        self._start_server()
        with httpx.Client(
            base_url=self.base_url,
            timeout=20,
            trust_env=False,
        ) as restarted:
            operator = self._login(restarted, "acceptance-operator")
            persisted_report = restarted.get(
                f"/api/v1/alarm-configuration-reports/{report['id']}",
                headers=operator,
            )
            self.assertEqual(200, persisted_report.status_code, persisted_report.text)
            self.assertEqual(report, persisted_report.json())
            persisted_events = restarted.get(
                "/api/v1/alarm-events?state=recovered&page_size=200",
                headers=operator,
            )
            self.assertEqual(200, persisted_events.status_code, persisted_events.text)
            self.assertEqual(
                {item["id"]: item for item in recovered_events},
                {item["id"]: item for item in persisted_events.json()["items"]},
            )
            for event_id, timeline in timelines.items():
                persisted_timeline = restarted.get(
                    f"/api/v1/alarm-events/{event_id}/transitions",
                    headers=operator,
                )
                self.assertEqual(200, persisted_timeline.status_code, persisted_timeline.text)
                self.assertEqual(timeline, persisted_timeline.json())

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_definitions")
                self.assertEqual(6, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarm_definition_origins")
                self.assertEqual(6, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM t_solution_installations WHERE id = %s",
                    (applied["installation_id"],),
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT previous_version FROM t_site_configuration_versions WHERE version = %s",
                    (applied["site_configuration_version"],),
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarm_configuration_reports")
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM t_alarm_configuration_acceptance_idempotency"
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarm_notification_outbox")
                self.assertEqual(6, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarms")
                self.assertEqual(self.legacy_alarm_count, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarm_levels")
                self.assertEqual(self.legacy_level_count, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_entity_alarm_bindings")
                self.assertEqual(self.legacy_binding_count, cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT tag.id, tag.alarm_level, tag.alarm_type,
                           tag.alarm_threshold, tag.fault_map_id
                    FROM t_tags tag ORDER BY tag.id
                    """
                )
                self.assertEqual(self.legacy_tag_snapshot, cursor.fetchall())


if __name__ == "__main__":
    unittest.main()
