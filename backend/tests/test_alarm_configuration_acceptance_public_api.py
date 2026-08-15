from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Barrier
import unittest
from uuid import UUID, uuid4

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

import httpx
import psycopg2
from psycopg2.extras import Json
from fastapi import FastAPI

from app.api import alarm_configurations as alarm_configuration_api
from app.api.auth import router as auth_router
from app.api.security import get_identity
from app.services.alarm_configuration import (
    AlarmConfigurationPlanItem,
    AppliedAlarmConfiguration,
)
from app.services.alarm_configuration_acceptance import (
    AlarmConfigurationAcceptance,
    AlarmConfigurationAcceptanceError,
    InMemoryAlarmConfigurationAcceptanceRepository,
    RunAlarmConfigurationAcceptance,
)
from app.services.alarm_runtime import (
    AcknowledgeAlarm,
    AlarmDefinition,
    AlarmObservation,
    AlarmRuntime,
    InMemoryAlarmDefinitionCatalog,
    InMemoryAlarmRepository,
)
from app.services.identity import (
    Identity,
    InMemoryIdentityRepository,
    UserIdentity,
    hash_password,
)


APPLICATION_IDS = (
    UUID("82000000-0000-0000-0000-000000000001"),
    UUID("82000000-0000-0000-0000-000000000002"),
)
INSTALLATION_IDS = (
    UUID("82000000-0000-0000-0000-000000000011"),
    UUID("82000000-0000-0000-0000-000000000012"),
)
DEFINITION_IDS = (
    UUID("82000000-0000-0000-0000-000000000021"),
    UUID("82000000-0000-0000-0000-000000000022"),
)
ENTITY_IDS = (
    UUID("82000000-0000-0000-0000-000000000031"),
    UUID("82000000-0000-0000-0000-000000000032"),
)


class _InMemoryApplicationAcceptance:
    def __init__(
        self,
        acceptance: AlarmConfigurationAcceptance,
        applications: tuple[AppliedAlarmConfiguration, ...],
    ) -> None:
        self._acceptance = acceptance
        self._applications = {application.id: application for application in applications}
        self.run_calls = 0

    def run(self, command: RunAlarmConfigurationAcceptance):
        self.run_calls += 1
        applied = self._applications.get(command.application_id)
        if applied is None:
            raise AlarmConfigurationAcceptanceError("ALARM_ACCEPTANCE_APPLICATION_NOT_FOUND")
        return self._acceptance.run(command, applied)

    def get(self, report_id: UUID):
        return self._acceptance.get(report_id)

    def progress(self):
        latest = max(
            self._applications.values(),
            key=lambda application: (application.applied_at, str(application.id)),
        )
        return self._acceptance.progress(latest)


class AlarmConfigurationAcceptancePublicApiTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(cls.password, salt=b"acceptance-api!")

    def build_app(self) -> tuple[FastAPI, _InMemoryApplicationAcceptance]:
        started_at = datetime(2026, 8, 16, 10, tzinfo=timezone.utc)
        definitions = tuple(
            AlarmDefinition(
                id=definition_id,
                asset_id=f"site.alarm.pcs.{index}",
                version="1",
                entity_instance_id=ENTITY_IDS[index],
                entity_definition_id="pcs.activePower",
                trigger={"op": "gt", "value": 100},
                trigger_duration_seconds=1,
                recovery={"op": "lte", "value": 90},
                recovery_duration_seconds=0,
                severity="MAJOR",
                notification_throttle_seconds=0,
            )
            for index, definition_id in enumerate(DEFINITION_IDS)
        )
        runtime = AlarmRuntime(
            InMemoryAlarmDefinitionCatalog(definitions),
            InMemoryAlarmRepository(),
        )
        for index, definition_id in enumerate(DEFINITION_IDS):
            offset = index * 10
            for seconds, value in ((offset, 101), (offset + 1, 101)):
                outcome = runtime.submit(AlarmObservation(
                    definition_id=definition_id,
                    entity_instance_id=ENTITY_IDS[index],
                    observed_at=started_at + timedelta(seconds=seconds),
                    value=value,
                    quality=192,
                    source_kind="acceptance-public-api",
                    source_ref="PCS-public",
                    evidence={
                        "safe_sample": seconds,
                        "nested": {
                            "address": "10.20.30.40",
                            "host": "internal-broker.local",
                        },
                        "endpoint": "mqtt://internal-broker.local/private",
                    },
                ))
            runtime.acknowledge(AcknowledgeAlarm(
                event_id=outcome.event_id,
                actor="user:operator",
                acknowledged_at=started_at + timedelta(seconds=offset + 2),
            ))
            runtime.submit(AlarmObservation(
                definition_id=definition_id,
                entity_instance_id=ENTITY_IDS[index],
                observed_at=started_at + timedelta(seconds=offset + 3),
                value=90,
                quality=192,
                source_kind="acceptance-public-api",
                source_ref="PCS-public",
                evidence={
                    "safe_sample": offset + 3,
                    "nested": {
                        "address": "10.20.30.40",
                        "host": "internal-broker.local",
                    },
                    "endpoint": "mqtt://internal-broker.local/private",
                },
            ))

        applications = tuple(
            AppliedAlarmConfiguration(
                id=application_id,
                plan_id=UUID(int=100 + index),
                installation_id=INSTALLATION_IDS[index],
                site_configuration_version=8 + index,
                definition_ids=(DEFINITION_IDS[index],),
                audit_event_id=UUID(int=200 + index),
                applied_at=started_at,
                items=(AlarmConfigurationPlanItem(
                    definition_key=f"site.alarm.pcs.{index}",
                    entity_instance_id=ENTITY_IDS[index],
                    rule_id="overpower",
                    action="add",
                    before=None,
                    after={"version": "new"},
                    blockers=(),
                ),),
            )
            for index, application_id in enumerate(APPLICATION_IDS)
        )
        application_acceptance = _InMemoryApplicationAcceptance(
            AlarmConfigurationAcceptance(
                runtime=runtime,
                repository=InMemoryAlarmConfigurationAcceptanceRepository(),
            ),
            applications,
        )
        identity = Identity(InMemoryIdentityRepository([
            UserIdentity(UUID(int=1), "admin", self.password_hash, "admin", "active"),
            UserIdentity(UUID(int=2), "engineer", self.password_hash, "engineer", "active"),
            UserIdentity(UUID(int=3), "operator", self.password_hash, "operator", "active"),
        ]))
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(alarm_configuration_api.router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity
        provider = getattr(
            alarm_configuration_api,
            "get_alarm_configuration_acceptance",
            None,
        )
        if provider is not None:
            app.dependency_overrides[provider] = lambda: application_acceptance
        return app, application_acceptance

    async def login(self, client: httpx.AsyncClient, username: str) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": self.password},
        )
        self.assertEqual(200, response.status_code, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def test_authorization_and_public_report_contract(self) -> None:
        app, _acceptance = self.build_app()
        transport = httpx.ASGITransport(app=app)
        first_path = f"/api/v1/alarm-configuration-applications/{APPLICATION_IDS[0]}/acceptance"
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            anonymous_post = await client.post(first_path, headers={"Idempotency-Key": "anonymous"})
            anonymous_get = await client.get(f"/api/v1/alarm-configuration-reports/{UUID(int=900)}")
            headers = {
                role: await self.login(client, role)
                for role in ("admin", "engineer", "operator")
            }
            operator_post = await client.post(
                first_path,
                headers={**headers["operator"], "Idempotency-Key": "operator"},
            )
            missing_key = await client.post(first_path, headers=headers["engineer"])
            engineer = await client.post(
                first_path,
                headers={**headers["engineer"], "Idempotency-Key": "engineer-run"},
            )
            self.assertEqual(200, engineer.status_code, engineer.text)
            operator_get = await client.get(
                f"/api/v1/alarm-configuration-reports/{engineer.json()['id']}",
                headers=headers["operator"],
            )
            engineer_get = await client.get(
                f"/api/v1/alarm-configuration-reports/{engineer.json()['id']}",
                headers=headers["engineer"],
            )
            admin = await client.post(
                f"/api/v1/alarm-configuration-applications/{APPLICATION_IDS[1]}/acceptance",
                headers={**headers["admin"], "Idempotency-Key": "admin-run"},
            )
            admin_get = await client.get(
                f"/api/v1/alarm-configuration-reports/{admin.json()['id']}",
                headers=headers["admin"],
            )

        self.assertEqual((401, 401), (anonymous_post.status_code, anonymous_get.status_code))
        self.assertEqual(403, operator_post.status_code, operator_post.text)
        self.assertEqual(422, missing_key.status_code, missing_key.text)
        self.assertEqual(
            "ALARM_CONFIGURATION_REQUEST_INVALID",
            missing_key.json()["detail"]["code"],
        )
        for response in (engineer, operator_get, engineer_get, admin, admin_get):
            self.assertEqual(200, response.status_code, response.text)
        report = operator_get.json()
        self.assertEqual(str(APPLICATION_IDS[0]), report["application_id"])
        self.assertEqual("passed", report["status"])
        self.assertEqual("ALARM_ACCEPTANCE_PASSED", report["items"][0]["code"])
        self.assertEqual(64, len(report["digest"]))
        serialized = operator_get.text.lower()
        for forbidden in (
            "raw_tag", "topic", "neuron", "token", "secret",
            "address", "host", "endpoint", "10.20.30.40", "internal-broker",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_idempotency_replays_same_application_and_rejects_another(self) -> None:
        app, _acceptance = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            first = await client.post(
                f"/api/v1/alarm-configuration-applications/{APPLICATION_IDS[0]}/acceptance",
                headers={**headers, "Idempotency-Key": "shared-acceptance-key"},
            )
            replay = await client.post(
                f"/api/v1/alarm-configuration-applications/{APPLICATION_IDS[0]}/acceptance",
                headers={**headers, "Idempotency-Key": "shared-acceptance-key"},
            )
            reused = await client.post(
                f"/api/v1/alarm-configuration-applications/{APPLICATION_IDS[1]}/acceptance",
                headers={**headers, "Idempotency-Key": "shared-acceptance-key"},
            )

        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(409, reused.status_code, reused.text)
        self.assertEqual(
            "IDEMPOTENCY_KEY_REUSED",
            reused.json()["detail"]["code"],
        )

    async def test_latest_progress_is_read_only_and_server_classified(self) -> None:
        app, acceptance = self.build_app()
        transport = httpx.ASGITransport(app=app)
        path = "/api/v1/alarm-configuration-applications/latest/acceptance-progress"
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            anonymous = await client.get(path)
            operator_headers = await self.login(client, "operator")
            engineer_headers = await self.login(client, "engineer")
            operator = await client.get(path, headers=operator_headers)
            first = await client.get(path, headers=engineer_headers)
            second = await client.get(path, headers=engineer_headers)

        self.assertEqual(401, anonymous.status_code, anonymous.text)
        self.assertEqual(403, operator.status_code, operator.text)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(), second.json())
        progress = first.json()
        self.assertEqual(str(APPLICATION_IDS[1]), progress["application_id"])
        self.assertTrue(progress["ready_to_report"])
        self.assertEqual("passed", progress["items"][0]["stage"])
        self.assertEqual("overpower", progress["items"][0]["rule_name"])
        self.assertNotIn("id", progress)
        self.assertNotIn("digest", progress)
        self.assertEqual(0, acceptance.run_calls)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the isolated Postgres acceptance seam",
)
class AlarmConfigurationAcceptancePostgresTest(unittest.TestCase):
    """Upgrade and transaction proofs against a disposable *_test database."""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.test_alarm_configuration_postgres import (
            _PostgresAlarmConfigurationTestBase,
        )

        cls._base = _PostgresAlarmConfigurationTestBase
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
        cls.migration_036 = (
            Path(__file__).resolve().parents[2]
            / "init-db"
            / "migration_036_alarm_configuration_acceptance.sql"
        )

    def setUp(self) -> None:
        base = self._base()
        base.connection_kwargs = self.connection_kwargs
        base._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self.installation_id, _ = base._insert_installed_site(cursor)
                self.entity_ids = base._insert_entities(cursor, self.installation_id)
                base._apply_alarm_migrations(cursor, include_acceptance=False)

    def _repository(self):
        from app.services.alarm_configuration_postgres import (
            PostgresAlarmConfigurationRepository,
        )

        return PostgresAlarmConfigurationRepository(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )

    def _create_applied_configuration(self):
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmRule,
            ApplyAlarmConfigurationPlan,
            EntitySelection,
            PlanAlarmConfiguration,
        )

        service = AlarmConfiguration(self._repository())
        revision = service.create_rule_set(
            key=f"acceptance-{uuid4()}",
            name="Acceptance PG",
            rules=(AlarmRule(
                id="high",
                name="High active power",
                severity="WARNING",
                trigger={"operator": "gt", "value": 90},
                trigger_duration_seconds=0,
                recovery={"operator": "lt", "value": 80},
                recovery_duration_seconds=0,
                notification_throttle_seconds=0,
                unit="kW",
            ),),
            actor="user:engineer",
        )
        plan = service.plan(PlanAlarmConfiguration(
            installation_id=UUID(self.installation_id),
            selection=EntitySelection(entity_instance_ids=(UUID(self.entity_ids[0]),)),
            rule_set_id=revision.rule_set_id,
            rule_set_revision=revision.revision,
            planned_by="user:planner",
        ))
        result = service.apply(ApplyAlarmConfigurationPlan(
            plan_id=plan.id,
            plan_digest=plan.digest,
            idempotency_key=f"apply-{uuid4()}",
            actor="user:engineer",
        ))
        return plan, result

    def _create_034_applied_configuration(self):
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmRule,
            AppliedAlarmConfiguration,
            EntitySelection,
            PlanAlarmConfiguration,
        )

        service = AlarmConfiguration(self._repository())
        revision = service.create_rule_set(
            key=f"upgrade-{uuid4()}",
            name="Upgrade from 034",
            rules=(AlarmRule(
                id="high",
                name="High active power",
                severity="WARNING",
                trigger={"operator": "gt", "value": 90},
                trigger_duration_seconds=0,
                recovery={"operator": "lt", "value": 80},
                recovery_duration_seconds=0,
                notification_throttle_seconds=0,
                unit="kW",
            ),),
            actor="user:engineer",
        )
        plan = service.plan(PlanAlarmConfiguration(
            installation_id=UUID(self.installation_id),
            selection=EntitySelection(entity_instance_ids=(UUID(self.entity_ids[0]),)),
            rule_set_id=revision.rule_set_id,
            rule_set_revision=revision.revision,
            planned_by="user:planner",
        ))
        applied = AppliedAlarmConfiguration(
            id=uuid4(),
            plan_id=plan.id,
            installation_id=UUID(self.installation_id),
            site_configuration_version=1,
            definition_ids=tuple(uuid4() for _item in plan.items),
            audit_event_id=uuid4(),
            applied_at=datetime.now(timezone.utc),
            items=plan.items,
        )
        from app.services.alarm_configuration_postgres import _result_json

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE t_alarm_configuration_plans
                    SET status = 'applied', applied_by = 'user:engineer',
                        applied_result = %s, applied_at = %s
                    WHERE id = %s
                    """,
                    (Json(_result_json(applied)), applied.applied_at, plan.id),
                )
        return plan, applied

    def _apply_036(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(self.migration_036.read_text(encoding="utf-8"))

    def _insert_recovered_evidence(self, result) -> None:
        definition_id = result.definition_ids[0]
        event_id = uuid4()
        times = [datetime(2026, 8, 16, 12, index, tzinfo=timezone.utc) for index in range(4)]
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                audit_ids = []
                for code in ("ALARM_ACTIVATED", "ALARM_ACKNOWLEDGED", "ALARM_RECOVERED"):
                    cursor.execute(
                        """
                        INSERT INTO t_audit_events (event, outcome, actor, target)
                        VALUES (%s, 'allowed', 'user:operator', %s) RETURNING id
                        """,
                        (code, f"alarm:{event_id}"),
                    )
                    audit_ids.append(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO t_alarm_events
                      (id, definition_id, definition_version, entity_instance_id,
                       state, severity, pending_at, active_at, acknowledged_at,
                       acknowledged_by, recovered_at)
                    SELECT %s, definition.id, definition.definition_version,
                           definition.entity_instance_id, 'recovered',
                           definition.severity, %s, %s, %s, 'user:operator', %s
                    FROM t_alarm_definitions definition WHERE definition.id = %s
                    """,
                    (event_id, times[0], times[1], times[2], times[3], definition_id),
                )
                transitions = (
                    (None, "active_unacknowledged", times[1], "ALARM_ACTIVATED", audit_ids[0]),
                    ("active_unacknowledged", "active_acknowledged", times[2], "ALARM_ACKNOWLEDGED", audit_ids[1]),
                    ("active_acknowledged", "recovered", times[3], "ALARM_RECOVERED", audit_ids[2]),
                )
                for from_state, to_state, occurred_at, code, audit_id in transitions:
                    cursor.execute(
                        """
                        INSERT INTO t_alarm_transitions
                          (event_id, audit_event_id, from_state, to_state,
                           occurred_at, code, evidence, actor)
                        VALUES (%s, %s, %s, %s, %s, %s,
                                '{"nested":{"address":"10.20.30.40",
                                             "host":"internal.local"},
                                  "endpoint":"mqtt://internal/private"}',
                                'user:operator')
                        """,
                        (event_id, audit_id, from_state, to_state, occurred_at, code),
                    )

    def _clone_application(self, plan, result) -> UUID:
        clone_plan_id = uuid4()
        clone_application_id = uuid4()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT canonical_plan, applied_result FROM t_alarm_configuration_plans WHERE id = %s",
                    (plan.id,),
                )
                canonical, applied = cursor.fetchone()
                canonical.update({"id": str(clone_plan_id), "digest": "f" * 64})
                applied.update({"id": str(clone_application_id), "plan_id": str(clone_plan_id)})
                cursor.execute(
                    """
                    INSERT INTO t_alarm_configuration_plans
                      (id, source_installation_id, base_site_configuration_version,
                       rule_set_id, rule_set_revision, canonical_plan, digest, status,
                       planned_by, applied_by, applied_result, applied_at, application_id)
                    SELECT %s, source_installation_id, base_site_configuration_version,
                           rule_set_id, rule_set_revision, %s, %s, 'applied',
                           planned_by, applied_by, %s, applied_at, %s
                    FROM t_alarm_configuration_plans WHERE id = %s
                    """,
                    (
                        clone_plan_id, Json(canonical), "f" * 64,
                        Json(applied), clone_application_id, plan.id,
                    ),
                )
        return clone_application_id

    def test_036_applies_to_fresh_schema_and_replays(self) -> None:
        self._apply_036()
        self._apply_036()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('t_alarm_configuration_reports')")
                self.assertEqual("t_alarm_configuration_reports", cursor.fetchone()[0])

    def test_036_upgrades_existing_applied_plan_and_replays(self) -> None:
        plan, result = self._create_034_applied_configuration()
        self._apply_036()
        self._apply_036()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT application_id FROM t_alarm_configuration_plans WHERE id = %s",
                    (plan.id,),
                )
                self.assertEqual(result.id, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_alarm_configuration_plans_append_only' AND NOT tgisinternal"
                )
                self.assertEqual(1, cursor.fetchone()[0])

    def test_036_rejects_mismatched_existing_application_evidence(self) -> None:
        plan, _result = self._create_034_applied_configuration()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER trg_alarm_configuration_plans_append_only ON t_alarm_configuration_plans")
                cursor.execute(
                    "UPDATE t_alarm_configuration_plans SET applied_result = jsonb_set(applied_result, '{plan_id}', to_jsonb(%s::text)) WHERE id = %s",
                    (str(uuid4()), plan.id),
                )
                cursor.execute(
                    "CREATE TRIGGER trg_alarm_configuration_plans_append_only BEFORE UPDATE OR DELETE ON t_alarm_configuration_plans FOR EACH ROW EXECUTE FUNCTION enforce_alarm_configuration_plan_append_only()"
                )
                with self.assertRaisesRegex(psycopg2.errors.RaiseException, "invalid existing applied alarm configuration evidence"):
                    cursor.execute(self.migration_036.read_text(encoding="utf-8"))
                cursor.execute("ROLLBACK")

    def test_036_constraint_rejects_application_and_plan_mismatch(self) -> None:
        self._apply_036()
        plan, _result = self._create_applied_configuration()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT canonical_plan, applied_result FROM t_alarm_configuration_plans WHERE id = %s",
                    (plan.id,),
                )
                canonical, applied = cursor.fetchone()
                clone_plan_id = uuid4()
                canonical.update({"id": str(clone_plan_id), "digest": "e" * 64})
                applied.update({"id": str(uuid4()), "plan_id": str(uuid4())})
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    cursor.execute(
                        """
                        INSERT INTO t_alarm_configuration_plans
                          (id, source_installation_id, base_site_configuration_version,
                           rule_set_id, rule_set_revision, canonical_plan, digest,
                           status, planned_by, applied_by, applied_result,
                           applied_at, application_id)
                        SELECT %s, source_installation_id,
                               base_site_configuration_version, rule_set_id,
                               rule_set_revision, %s, %s, 'applied', planned_by,
                               applied_by, %s, applied_at, %s
                        FROM t_alarm_configuration_plans WHERE id = %s
                        """,
                        (
                            clone_plan_id, Json(canonical), "e" * 64,
                            Json(applied), uuid4(), plan.id,
                        ),
                    )

    def test_real_concurrency_replay_conflict_and_single_binding(self) -> None:
        self._apply_036()
        plan, result = self._create_applied_configuration()
        self._insert_recovered_evidence(result)
        other_application_id = self._clone_application(plan, result)
        from app.services.alarm_configuration_acceptance import (
            AlarmConfigurationAcceptanceError,
            RunAlarmConfigurationAcceptance,
        )
        from app.services.alarm_configuration_acceptance_postgres import (
            PostgresAlarmConfigurationAcceptance,
        )

        service = PostgresAlarmConfigurationAcceptance(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )
        command = RunAlarmConfigurationAcceptance(
            result.id, "user:engineer", "shared-real-key"
        )
        start = Barrier(2)

        def run(_index: int):
            start.wait(timeout=5)
            return service.run(command)

        with ThreadPoolExecutor(max_workers=2) as executor:
            reports = list(executor.map(run, (0, 1)))
        self.assertEqual(reports[0], reports[1])
        with self.assertRaisesRegex(AlarmConfigurationAcceptanceError, "IDEMPOTENCY_KEY_REUSED"):
            service.run(RunAlarmConfigurationAcceptance(
                other_application_id, "user:engineer", "shared-real-key"
            ))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_configuration_reports")
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarm_configuration_acceptance_idempotency")
                self.assertEqual(1, cursor.fetchone()[0])

    def test_latest_progress_reads_complete_evidence_without_report_writes(self) -> None:
        self._apply_036()
        _plan, result = self._create_applied_configuration()
        self._insert_recovered_evidence(result)
        from app.services.alarm_configuration_acceptance_postgres import (
            PostgresAlarmConfigurationAcceptance,
        )

        service = PostgresAlarmConfigurationAcceptance(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )
        first = service.progress()
        second = service.progress()

        self.assertEqual(result.id, first.application_id)
        self.assertEqual(first, second)
        self.assertTrue(first.ready_to_report)
        self.assertEqual("passed", first.items[0].stage)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_configuration_reports")
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarm_configuration_acceptance_idempotency")
                self.assertEqual(0, cursor.fetchone()[0])

    def test_second_insert_commit_failure_rolls_back_report(self) -> None:
        self._apply_036()
        _plan, result = self._create_applied_configuration()
        self._insert_recovered_evidence(result)
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE OR REPLACE FUNCTION fail_acceptance_binding_commit()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'injected acceptance binding commit failure'; END;
                    $$;
                    CREATE CONSTRAINT TRIGGER test_acceptance_binding_commit_failure
                    AFTER INSERT ON t_alarm_configuration_acceptance_idempotency
                    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
                    EXECUTE FUNCTION fail_acceptance_binding_commit();
                    """
                )
        from app.services.alarm_configuration_acceptance import (
            AlarmConfigurationAcceptanceError,
            RunAlarmConfigurationAcceptance,
        )
        from app.services.alarm_configuration_acceptance_postgres import (
            PostgresAlarmConfigurationAcceptance,
        )

        service = PostgresAlarmConfigurationAcceptance(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )
        with self.assertRaisesRegex(AlarmConfigurationAcceptanceError, "PERSISTENCE_UNAVAILABLE"):
            service.run(RunAlarmConfigurationAcceptance(
                result.id, "user:engineer", "commit-failure"
            ))
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_configuration_reports")
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_alarm_configuration_acceptance_idempotency")
                self.assertEqual(0, cursor.fetchone()[0])

    def test_report_and_binding_update_delete_truncate_are_rejected(self) -> None:
        self._apply_036()
        _plan, result = self._create_applied_configuration()
        self._insert_recovered_evidence(result)
        from app.services.alarm_configuration_acceptance import RunAlarmConfigurationAcceptance
        from app.services.alarm_configuration_acceptance_postgres import PostgresAlarmConfigurationAcceptance

        service = PostgresAlarmConfigurationAcceptance(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )
        service.run(RunAlarmConfigurationAcceptance(result.id, "user:engineer", "immutable"))
        statements = (
            "UPDATE t_alarm_configuration_reports SET actor = actor",
            "DELETE FROM t_alarm_configuration_reports",
            "UPDATE t_alarm_configuration_acceptance_idempotency SET actor = actor",
            "DELETE FROM t_alarm_configuration_acceptance_idempotency",
            "TRUNCATE t_alarm_configuration_reports, t_alarm_configuration_acceptance_idempotency",
        )
        for statement in statements:
            connection = psycopg2.connect(**self.connection_kwargs)
            try:
                with connection.cursor() as cursor:
                    with self.assertRaisesRegex(psycopg2.errors.RaiseException, "append-only"):
                        cursor.execute(statement)
                connection.rollback()
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
