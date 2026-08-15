from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

import httpx
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

    def run(self, command: RunAlarmConfigurationAcceptance):
        applied = self._applications.get(command.application_id)
        if applied is None:
            raise AlarmConfigurationAcceptanceError("ALARM_ACCEPTANCE_APPLICATION_NOT_FOUND")
        return self._acceptance.run(command, applied)

    def get(self, report_id: UUID):
        return self._acceptance.get(report_id)


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
                    evidence={"safe_sample": seconds},
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
                evidence={"safe_sample": offset + 3},
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
        for forbidden in ("raw_tag", "topic", "neuron", "token", "secret"):
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


if __name__ == "__main__":
    unittest.main()
