from __future__ import annotations

import os
import unittest
from uuid import UUID


os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

import httpx
from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.security import get_identity
from app.services.alarm_configuration import (
    AlarmConfiguration,
    AlarmConfigurationError,
    InMemoryAlarmConfigurationRepository,
    ResolvedAlarmEntity,
)
from app.services.identity import (
    Identity,
    InMemoryIdentityRepository,
    UserIdentity,
    hash_password,
)


INSTALLATION_ID = UUID("40000000-0000-0000-0000-000000000001")
DEVICE_ID = UUID("40000000-0000-0000-0000-000000000002")
ENTITY_IDS = (
    UUID("40000000-0000-0000-0000-000000000003"),
    UUID("40000000-0000-0000-0000-000000000004"),
)


class AlarmConfigurationAuthorizationTest(unittest.IsolatedAsyncioTestCase):
    """Public configuration endpoints reject before touching configuration state."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(cls.password, salt=b"alarm-api-auth!!")

    def build_app(
        self,
        *,
        entities: tuple[ResolvedAlarmEntity, ...] | None = None,
    ) -> tuple[FastAPI, InMemoryIdentityRepository, AlarmConfiguration]:
        identity_repository = InMemoryIdentityRepository(
            [
                UserIdentity(UUID("00000000-0000-0000-0000-000000000001"), "admin", self.password_hash, "admin", "active"),
                UserIdentity(UUID("00000000-0000-0000-0000-000000000002"), "engineer", self.password_hash, "engineer", "active"),
                UserIdentity(UUID("00000000-0000-0000-0000-000000000003"), "operator", self.password_hash, "operator", "active"),
            ]
        )
        configuration_repository = InMemoryAlarmConfigurationRepository(
            installation_id=INSTALLATION_ID,
            entities=entities or tuple(
                ResolvedAlarmEntity(
                    id=entity_id,
                    device_instance_id=DEVICE_ID,
                    definition_id="pcs.activePower",
                    display_name=f"PCS {index} active power",
                    data_type="number",
                    unit="kW",
                    confirmation_id=UUID(f"40000000-0000-0000-0000-0000000000{index + 10}"),
                )
                for index, entity_id in enumerate(ENTITY_IDS)
            ),
        )
        configuration = AlarmConfiguration(configuration_repository)
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        # The real router is deliberately optional during RED: before Task 4 it
        # is absent, so this test proves the public route must be registered.
        try:
            from app.api.alarm_configurations import get_alarm_configuration, router
        except ImportError:
            pass
        else:
            app.include_router(router, prefix="/api/v1")
            app.dependency_overrides[get_alarm_configuration] = lambda: configuration
        app.dependency_overrides[get_identity] = lambda: Identity(identity_repository)
        return app, identity_repository, configuration

    async def login(self, client: httpx.AsyncClient, username: str) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def test_anonymous_and_operator_are_rejected_before_configuration_access(self) -> None:
        app, identity_repository, configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            anonymous = await client.get("/api/v1/alarm-configurations")
            operator_headers = await self.login(client, "operator")
            operator_read = await client.get(
                "/api/v1/alarm-configurations", headers=operator_headers
            )
            operator_write = await client.post(
                "/api/v1/alarm-rule-sets",
                headers=operator_headers,
                json={"key": "over-temperature", "name": "Over temperature", "rules": []},
            )

        self.assertEqual(anonymous.status_code, 401, anonymous.text)
        self.assertEqual("AUTHENTICATION_REQUIRED", anonymous.json()["detail"]["code"])
        self.assertEqual(operator_read.status_code, 403, operator_read.text)
        self.assertEqual("PERMISSION_DENIED", operator_read.json()["detail"]["code"])
        self.assertEqual(operator_write.status_code, 403, operator_write.text)
        self.assertEqual("PERMISSION_DENIED", operator_write.json()["detail"]["code"])
        self.assertEqual(configuration.repository.plans, [])
        self.assertEqual(configuration.repository.applied_count, 0)
        self.assertTrue(
            any(
                event.event == "authorization.decision" and event.outcome == "denied"
                for event in identity_repository.audits
            )
        )

    async def test_engineer_creates_revision_plans_four_items_and_replays_apply(self) -> None:
        app, _identity_repository, configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        first_rule = {
            "id": "major-high",
            "name": "Major high",
            "severity": "MAJOR",
            "trigger": {"operator": "gte", "value": 80},
            "trigger_duration_seconds": 5,
            "recovery": {"operator": "lte", "value": 70},
            "recovery_duration_seconds": 5,
            "notification_throttle_seconds": 60,
            "unit": "kW",
        }
        second_rule = {
            **first_rule,
            "id": "critical-high",
            "name": "Critical high",
            "severity": "CRITICAL",
            "trigger": {"operator": "gte", "value": 90},
            "recovery": {"operator": "lte", "value": 80},
        }
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            created = await client.post(
                "/api/v1/alarm-rule-sets",
                headers=headers,
                json={"key": "power-high", "name": "Power high", "rules": [first_rule]},
            )
            self.assertEqual(created.status_code, 201, created.text)
            rule_set_id = created.json()["rule_set_id"]
            revision = await client.post(
                f"/api/v1/alarm-rule-sets/{rule_set_id}/revisions",
                headers=headers,
                json={"rules": [first_rule, second_rule]},
            )
            self.assertEqual(revision.status_code, 201, revision.text)
            self.assertEqual(revision.json()["revision"], 2)
            listed = await client.get("/api/v1/alarm-rule-sets", headers=headers)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(
                [(item["key"], item["revision"]) for item in listed.json()["items"]],
                [("power-high", 1), ("power-high", 2)],
            )
            planned = await client.post(
                "/api/v1/alarm-configuration-plans",
                headers=headers,
                json={
                    "installation_id": str(INSTALLATION_ID),
                    "selection": {"entity_instance_ids": [str(value) for value in ENTITY_IDS]},
                    "rule_set_id": rule_set_id,
                    "rule_set_revision": 2,
                },
            )
            self.assertEqual(planned.status_code, 201, planned.text)
            plan = planned.json()
            self.assertEqual(plan["status"], "ready")
            self.assertEqual([item["action"] for item in plan["items"]], ["add", "add", "add", "add"])
            self.assertNotIn("planned_by", plan)
            self.assertEqual(configuration.repository.plans[-1].planned_by, "user:00000000-0000-0000-0000-000000000002")
            applied = await client.post(
                f"/api/v1/alarm-configuration-plans/{plan['id']}/apply",
                headers={**headers, "Idempotency-Key": "alarm-public-apply-1"},
                json={"plan_digest": plan["digest"]},
            )
            replay = await client.post(
                f"/api/v1/alarm-configuration-plans/{plan['id']}/apply",
                headers={**headers, "Idempotency-Key": "alarm-public-apply-1"},
                json={"plan_digest": plan["digest"]},
            )
            current = await client.get("/api/v1/alarm-configurations", headers=headers)

        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), applied.json())
        self.assertEqual(configuration.repository.applied_count, 1)
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(len(current.json()["definitions"]), 4)
        self.assertNotIn("planned_by", current.text)
        self.assertNotIn("actor", current.text)

    async def test_unknown_rule_set_revision_has_a_stable_not_found_error(self) -> None:
        app, _identity_repository, _configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            response = await client.post(
                "/api/v1/alarm-rule-sets/40000000-0000-0000-0000-000000000099/revisions",
                headers=headers,
                json={"rules": []},
            )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"]["code"], "ALARM_RULE_SET_NOT_FOUND")

    async def test_apply_requires_idempotency_key_and_rejects_reuse(self) -> None:
        app, _identity_repository, configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        rule = {
            "id": "major-high", "name": "Major high", "severity": "MAJOR",
            "trigger": {"operator": "gte", "value": 80}, "trigger_duration_seconds": 0,
            "recovery": {"operator": "lte", "value": 70}, "recovery_duration_seconds": 0,
            "notification_throttle_seconds": 0, "unit": "kW",
        }
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "admin")
            created = await client.post("/api/v1/alarm-rule-sets", headers=headers, json={"key": "one", "name": "One", "rules": [rule]})
            plan = await client.post(
                "/api/v1/alarm-configuration-plans", headers=headers,
                json={"installation_id": str(INSTALLATION_ID), "selection": {"entity_instance_ids": [str(ENTITY_IDS[0])]}, "rule_set_id": created.json()["rule_set_id"], "rule_set_revision": 1},
            )
            plan_id, digest = plan.json()["id"], plan.json()["digest"]
            missing_key = await client.post(f"/api/v1/alarm-configuration-plans/{plan_id}/apply", headers=headers, json={"plan_digest": digest})
            first = await client.post(f"/api/v1/alarm-configuration-plans/{plan_id}/apply", headers={**headers, "Idempotency-Key": "shared-key"}, json={"plan_digest": digest})
            other_plan = await client.post(
                "/api/v1/alarm-configuration-plans", headers=headers,
                json={"installation_id": str(INSTALLATION_ID), "selection": {"entity_instance_ids": [str(ENTITY_IDS[1])]}, "rule_set_id": created.json()["rule_set_id"], "rule_set_revision": 1},
            )
            reused = await client.post(f"/api/v1/alarm-configuration-plans/{other_plan.json()['id']}/apply", headers={**headers, "Idempotency-Key": "shared-key"}, json={"plan_digest": other_plan.json()["digest"]})

        self.assertEqual(missing_key.status_code, 422, missing_key.text)
        self.assertEqual(missing_key.json()["detail"]["code"], "ALARM_CONFIGURATION_REQUEST_INVALID")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(reused.status_code, 409, reused.text)
        self.assertEqual(reused.json()["detail"]["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(configuration.repository.applied_count, 1)

    async def test_request_validation_uses_the_stable_error_envelope(self) -> None:
        app, _identity_repository, _configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            response = await client.post(
                "/api/v1/alarm-configuration-plans/40000000-0000-0000-0000-000000000099/apply",
                headers={**headers, "Idempotency-Key": "validation"},
                json={"plan_digest": "not-a-digest", "actor": "forged"},
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json(),
            {
                "detail": {
                    "code": "ALARM_CONFIGURATION_REQUEST_INVALID",
                    "message": "Alarm configuration request is invalid",
                }
            },
        )

    async def test_persistence_unavailability_has_a_stable_503_envelope(self) -> None:
        app, _identity_repository, configuration = self.build_app()

        def unavailable_context():
            raise AlarmConfigurationError("ALARM_CONFIGURATION_PERSISTENCE_UNAVAILABLE")

        configuration.repository.current_site_context = unavailable_context
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            response = await client.get("/api/v1/alarm-configurations", headers=headers)

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "ALARM_CONFIGURATION_PERSISTENCE_UNAVAILABLE",
        )

    async def test_plan_lookup_persistence_failure_has_a_stable_503_envelope(self) -> None:
        app, _identity_repository, configuration = self.build_app()

        def unavailable_plan(_plan_id):
            raise AlarmConfigurationError("ALARM_CONFIGURATION_PERSISTENCE_FAILED")

        configuration.repository.get_plan = unavailable_plan
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            response = await client.get(
                "/api/v1/alarm-configuration-plans/40000000-0000-0000-0000-000000000099",
                headers=headers,
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json(),
            {
                "detail": {
                    "code": "ALARM_CONFIGURATION_PERSISTENCE_FAILED",
                    "message": "ALARM_CONFIGURATION_PERSISTENCE_FAILED",
                }
            },
        )

    async def test_duplicate_selection_is_rejected_without_creating_a_plan(self) -> None:
        app, _identity_repository, configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        rule = {
            "id": "duplicate", "name": "Duplicate", "severity": "MAJOR",
            "trigger": {"operator": "gte", "value": 80}, "trigger_duration_seconds": 0,
            "recovery": {"operator": "lte", "value": 70}, "recovery_duration_seconds": 0,
            "notification_throttle_seconds": 0, "unit": "kW",
        }
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            created = await client.post(
                "/api/v1/alarm-rule-sets", headers=headers,
                json={"key": "duplicate", "name": "Duplicate", "rules": [rule]},
            )
            response = await client.post(
                "/api/v1/alarm-configuration-plans", headers=headers,
                json={
                    "installation_id": str(INSTALLATION_ID),
                    "selection": {"entity_instance_ids": [str(ENTITY_IDS[0]), str(ENTITY_IDS[0])]},
                    "rule_set_id": created.json()["rule_set_id"],
                    "rule_set_revision": 1,
                },
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "ALARM_RULE_CONFLICT")
        self.assertEqual(configuration.repository.plans, [])

    async def test_plan_blocks_with_literal_stable_codes_without_apply_writes(self) -> None:
        cases = (
            ("ALARM_ENTITY_UNRESOLVED", "number", "kW", "kW", None, {"operator": "gte", "value": 80}, {"operator": "lte", "value": 70}),
            ("ALARM_DATA_TYPE_UNSUPPORTED", "text", "kW", "kW", UUID("40000000-0000-0000-0000-000000000010"), {"operator": "gte", "value": 80}, {"operator": "lte", "value": 70}),
            ("ALARM_UNIT_MISMATCH", "number", "A", "kW", UUID("40000000-0000-0000-0000-000000000010"), {"operator": "gte", "value": 80}, {"operator": "lte", "value": 70}),
            ("ALARM_THRESHOLD_INVALID", "number", "kW", "kW", UUID("40000000-0000-0000-0000-000000000010"), {"operator": "gte", "value": 80}, {"operator": "lte", "value": 80}),
        )
        for index, (expected, data_type, entity_unit, rule_unit, confirmation_id, trigger, recovery) in enumerate(cases):
            entity = ResolvedAlarmEntity(
                id=ENTITY_IDS[0], device_instance_id=DEVICE_ID,
                definition_id="pcs.activePower", display_name="PCS active power",
                data_type=data_type, unit=entity_unit,
                confirmation_id=confirmation_id,
            )
            app, _identity_repository, configuration = self.build_app(entities=(entity,))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
                headers = await self.login(client, "engineer")
                rule = {"id": f"rule-{index}", "name": expected, "severity": "MAJOR", "trigger": trigger, "trigger_duration_seconds": 0, "recovery": recovery, "recovery_duration_seconds": 0, "notification_throttle_seconds": 0, "unit": rule_unit}
                created = await client.post("/api/v1/alarm-rule-sets", headers=headers, json={"key": f"code-{index}", "name": expected, "rules": [rule]})
                plan = await client.post("/api/v1/alarm-configuration-plans", headers=headers, json={"installation_id": str(INSTALLATION_ID), "selection": {"entity_instance_ids": [str(ENTITY_IDS[0])]}, "rule_set_id": created.json()["rule_set_id"], "rule_set_revision": 1})
                self.assertEqual(plan.status_code, 201, plan.text)
                self.assertEqual(plan.json()["status"], "blocked")
                self.assertIn(expected, {blocker["code"] for blocker in plan.json()["blockers"]})
            self.assertEqual(configuration.repository.applied_count, 0)

    async def test_error_contract_maps_digest_stale_rule_conflict_and_audit_without_apply_writes(self) -> None:
        app, _identity_repository, configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        rule = {"id": "same", "name": "Same", "severity": "MAJOR", "trigger": {"operator": "gte", "value": 80}, "trigger_duration_seconds": 0, "recovery": {"operator": "lte", "value": 70}, "recovery_duration_seconds": 0, "notification_throttle_seconds": 0, "unit": "kW"}
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            conflict = await client.post("/api/v1/alarm-rule-sets", headers=headers, json={"key": "conflict", "name": "Conflict", "rules": [rule, rule]})
            created = await client.post("/api/v1/alarm-rule-sets", headers=headers, json={"key": "safe", "name": "Safe", "rules": [rule]})
            planned = await client.post("/api/v1/alarm-configuration-plans", headers=headers, json={"installation_id": str(INSTALLATION_ID), "selection": {"entity_instance_ids": [str(ENTITY_IDS[0])]}, "rule_set_id": created.json()["rule_set_id"], "rule_set_revision": 1})
            plan = planned.json()
            wrong_digest = await client.post(f"/api/v1/alarm-configuration-plans/{plan['id']}/apply", headers={**headers, "Idempotency-Key": "wrong"}, json={"plan_digest": "0" * 64})
            configuration.repository.fail_audit = True
            audit_failure = await client.post(f"/api/v1/alarm-configuration-plans/{plan['id']}/apply", headers={**headers, "Idempotency-Key": "audit"}, json={"plan_digest": plan["digest"]})
            configuration.repository.fail_audit = False
            fresh = await client.post("/api/v1/alarm-configuration-plans", headers=headers, json={"installation_id": str(INSTALLATION_ID), "selection": {"entity_instance_ids": [str(ENTITY_IDS[1])]}, "rule_set_id": created.json()["rule_set_id"], "rule_set_revision": 1})
            success = await client.post(f"/api/v1/alarm-configuration-plans/{fresh.json()['id']}/apply", headers={**headers, "Idempotency-Key": "fresh"}, json={"plan_digest": fresh.json()["digest"]})
            stale = await client.post(f"/api/v1/alarm-configuration-plans/{plan['id']}/apply", headers={**headers, "Idempotency-Key": "stale"}, json={"plan_digest": plan["digest"]})

        self.assertEqual(conflict.status_code, 422, conflict.text)
        self.assertEqual(conflict.json()["detail"]["code"], "ALARM_RULE_CONFLICT")
        self.assertEqual(wrong_digest.status_code, 409, wrong_digest.text)
        self.assertEqual(wrong_digest.json()["detail"]["code"], "ALARM_PLAN_DIGEST_MISMATCH")
        self.assertEqual(audit_failure.status_code, 503, audit_failure.text)
        self.assertEqual(audit_failure.json()["detail"]["code"], "AUDIT_UNAVAILABLE")
        self.assertEqual(success.status_code, 200, success.text)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "ALARM_PLAN_STALE")
        self.assertEqual(configuration.repository.applied_count, 1)

    async def test_legacy_migration_routes_are_explicitly_deferred(self) -> None:
        app, _identity_repository, _configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            preview = await client.get(
                "/api/v1/alarm-configuration-migrations/legacy", headers=headers
            )
            plan = await client.post(
                "/api/v1/alarm-configuration-migrations/legacy/plans",
                headers=headers,
                json={"installation_id": str(INSTALLATION_ID)},
            )

        for response in (preview, plan):
            self.assertEqual(response.status_code, 501, response.text)
            self.assertEqual(
                response.json()["detail"]["code"],
                "ALARM_MIGRATION_NOT_IMPLEMENTED",
            )

    async def test_rule_batch_limit_uses_the_stable_machine_code(self) -> None:
        app, _identity_repository, _configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        rule = {
            "id": "limit", "name": "Limit", "severity": "MAJOR",
            "trigger": {"operator": "gte", "value": 80}, "trigger_duration_seconds": 0,
            "recovery": {"operator": "lte", "value": 70}, "recovery_duration_seconds": 0,
            "notification_throttle_seconds": 0, "unit": "kW",
        }
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            response = await client.post(
                "/api/v1/alarm-rule-sets",
                headers=headers,
                json={"key": "over-limit", "name": "Over limit", "rules": [rule] * 21},
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["detail"]["code"], "ALARM_BATCH_LIMIT_EXCEEDED"
        )

    async def test_nested_rule_bodies_forbid_actor_and_internal_fields(self) -> None:
        app, _identity_repository, _configuration = self.build_app()
        transport = httpx.ASGITransport(app=app)
        rule = {
            "id": "strict", "name": "Strict", "severity": "MAJOR",
            "trigger": {"operator": "gte", "value": 80, "actor": "forged"},
            "trigger_duration_seconds": 0,
            "recovery": {"operator": "lte", "value": 70},
            "recovery_duration_seconds": 0,
            "notification_throttle_seconds": 0, "unit": "kW",
        }
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self.login(client, "engineer")
            response = await client.post(
                "/api/v1/alarm-rule-sets",
                headers=headers,
                json={"key": "strict", "name": "Strict", "rules": [rule]},
            )

        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
