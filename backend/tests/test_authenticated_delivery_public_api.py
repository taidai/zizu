from __future__ import annotations

import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.auth import router as auth_router
from app.api.security import get_identity
from app.api.solution_delivery import (
    get_solution_delivery,
    router as delivery_router,
)
from app.services.solution_delivery import (
    InMemoryDeliveryRepository,
    SolutionDelivery,
)
from app.services.identity import (
    Identity,
    InMemoryIdentityRepository,
    UserIdentity,
    hash_password,
)
import app.services.identity as identity_module
from app.services.identity import verify_identity_schema
from tests.test_delivery_public_api import build_minimal_package
from tests.test_delivery_public_api import AsgiPublicApiProbe


class AuthenticatedDeliveryPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(cls.password, salt=b"test-auth-salt!!")

    def build_app(self) -> tuple[FastAPI, InMemoryIdentityRepository, SolutionDelivery]:
        repository = InMemoryIdentityRepository(
            [
                UserIdentity(
                    UUID("00000000-0000-0000-0000-000000000001"),
                    "admin",
                    self.password_hash,
                    "admin",
                    "active",
                ),
                UserIdentity(
                    UUID("00000000-0000-0000-0000-000000000002"),
                    "engineer",
                    self.password_hash,
                    "engineer",
                    "active",
                ),
                UserIdentity(
                    UUID("00000000-0000-0000-0000-000000000003"),
                    "operator",
                    self.password_hash,
                    "operator",
                    "active",
                ),
            ]
        )
        identity = Identity(repository)
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app = FastAPI()
        app.include_router(health_router, prefix="/api/v1")
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(delivery_router, prefix="/api/v1")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        app.dependency_overrides[get_identity] = lambda: identity
        return app, repository, delivery

    def test_identity_schema_verifier_rejects_partial_migration(self) -> None:
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def execute(self, *args) -> None:
                return None

            def fetchall(self):
                return [("t_users",), ("t_auth_sessions",)]

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def cursor(self) -> Cursor:
                return Cursor()

        with self.assertRaisesRegex(RuntimeError, "t_audit_events"):
            verify_identity_schema(lambda: Connection())

    async def login(self, client: httpx.AsyncClient, username: str) -> dict:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    async def test_anonymous_package_import_is_rejected_before_delivery(self) -> None:
        app, _, _ = self.build_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            liveness = await client.get("/api/v1/health/live")
            response = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "minimal.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )

        self.assertEqual(liveness.status_code, 200)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(
            response.json(),
            {
                "detail": {
                    "code": "AUTHENTICATION_REQUIRED",
                    "message": "Bearer authentication is required",
                }
            },
        )
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    async def test_admin_can_import_but_engineer_cannot_manage_packages(self) -> None:
        app, _, _ = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            admin = await self.login(client, "admin")
            engineer = await self.login(client, "engineer")
            imported = await client.post(
                "/api/v1/solution-packages/import",
                headers={"Authorization": f"Bearer {admin['access_token']}"},
                files={
                    "archive": (
                        "minimal.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )
            forbidden = await client.post(
                "/api/v1/solution-packages/import",
                headers={"Authorization": f"Bearer {engineer['access_token']}"},
                files={
                    "archive": (
                        "minimal.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )

        self.assertEqual(admin["token_type"], "bearer")
        self.assertEqual(admin["user"]["role"], "admin")
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        self.assertEqual(forbidden.json()["detail"]["code"], "PERMISSION_DENIED")

    async def test_engineer_completes_delivery_and_operator_reads_report(self) -> None:
        app, _, _ = self.build_app()
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
            public_api_probe=AsgiPublicApiProbe(app),
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            admin = await self.login(client, "admin")
            engineer = await self.login(client, "engineer")
            operator = await self.login(client, "operator")
            admin_headers = {"Authorization": f"Bearer {admin['access_token']}"}
            engineer_headers = {
                "Authorization": f"Bearer {engineer['access_token']}"
            }
            operator_headers = {
                "Authorization": f"Bearer {operator['access_token']}"
            }
            engineer_packages = await client.get(
                "/api/v1/solution-packages",
                headers=engineer_headers,
            )
            operator_packages = await client.get(
                "/api/v1/solution-packages",
                headers=operator_headers,
            )
            imported = await client.post(
                "/api/v1/solution-packages/import",
                headers=admin_headers,
                files={
                    "archive": (
                        "minimal.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )
            package = imported.json()
            plan_response = await client.post(
                f"/api/v1/solution-packages/{package['id']}/install-plans",
                headers=engineer_headers,
                json={},
            )
            plan = plan_response.json()
            installed = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                headers={
                    **engineer_headers,
                    "Idempotency-Key": "authenticated-install",
                },
                json={"plan_digest": plan["digest"]},
            )
            repeated_install = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                headers={
                    **engineer_headers,
                    "Idempotency-Key": "authenticated-install",
                },
                json={"plan_digest": plan["digest"]},
            )
            installation = installed.json()
            acceptance = await client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={
                    **engineer_headers,
                    "Idempotency-Key": "authenticated-acceptance",
                },
            )
            report = acceptance.json()
            operator_read = await client.get(
                f"/api/v1/delivery-reports/{report['id']}",
                headers=operator_headers,
            )
            engineer_configuration = await client.get(
                f"/api/v1/site-configuration-versions/"
                f"{installation['site_configuration_version']}",
                headers=engineer_headers,
            )
            operator_configuration = await client.get(
                f"/api/v1/site-configuration-versions/"
                f"{installation['site_configuration_version']}",
                headers=operator_headers,
            )
            operator_plan = await client.post(
                f"/api/v1/solution-packages/{package['id']}/install-plans",
                headers=operator_headers,
                json={},
            )
            operator_acceptance = await client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={
                    **operator_headers,
                    "Idempotency-Key": "operator-must-not-run",
                },
            )

        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(plan_response.status_code, 201, plan_response.text)
        self.assertEqual(installed.status_code, 201, installed.text)
        self.assertEqual(repeated_install.json(), installation)
        self.assertEqual(engineer_packages.status_code, 403, engineer_packages.text)
        self.assertEqual(
            engineer_packages.json()["detail"]["code"],
            "PERMISSION_DENIED",
        )
        self.assertEqual(operator_packages.status_code, 403, operator_packages.text)
        self.assertEqual(acceptance.status_code, 201, acceptance.text)
        self.assertEqual(
            report["actor"],
            "user:00000000-0000-0000-0000-000000000002",
        )
        self.assertEqual(operator_read.status_code, 200, operator_read.text)
        self.assertEqual(operator_read.json(), report)
        self.assertEqual(engineer_configuration.status_code, 200)
        self.assertEqual(operator_configuration.status_code, 403)
        self.assertEqual(operator_plan.status_code, 403, operator_plan.text)
        self.assertEqual(
            operator_plan.json()["detail"]["code"],
            "PERMISSION_DENIED",
        )
        self.assertEqual(operator_acceptance.status_code, 403)

    async def test_invalid_expired_and_revoked_sessions_fail_closed(self) -> None:
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        repository = InMemoryIdentityRepository(
            [
                UserIdentity(
                    UUID("00000000-0000-0000-0000-000000000001"),
                    "admin",
                    self.password_hash,
                    "admin",
                    "active",
                )
            ]
        )
        clock = [now]
        identity = Identity(repository, session_minutes=5, now=lambda: clock[0])
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app = FastAPI()
        app.include_router(health_router, prefix="/api/v1")
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(delivery_router, prefix="/api/v1")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        app.dependency_overrides[get_identity] = lambda: identity
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            invalid = await client.get(
                "/api/v1/solution-packages",
                headers={"Authorization": "Bearer forged"},
            )
            unicode_invalid = await client.get(
                "/api/v1/solution-packages",
                headers=[(b"Authorization", b"Bearer zizu_s1_\xe9")],
            )
            session = await self.login(client, "admin")
            auth = {"Authorization": f"Bearer {session['access_token']}"}
            me = await client.get("/api/v1/auth/me", headers=auth)
            clock[0] = now + timedelta(minutes=6)
            expired = await client.get("/api/v1/solution-packages", headers=auth)

            clock[0] = now
            second = await self.login(client, "admin")
            second_auth = {
                "Authorization": f"Bearer {second['access_token']}"
            }
            logged_out = await client.post("/api/v1/auth/logout", headers=second_auth)
            revoked = await client.get(
                "/api/v1/solution-packages",
                headers=second_auth,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as insecure_client:
                bearer_over_http = await insecure_client.get(
                    "/api/v1/solution-packages",
                    headers=second_auth,
                )

        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["detail"]["code"], "TOKEN_INVALID")
        self.assertEqual(unicode_invalid.status_code, 401)
        self.assertEqual(
            unicode_invalid.json()["detail"]["code"],
            "TOKEN_INVALID",
        )
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(logged_out.status_code, 204, logged_out.text)
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.json()["detail"]["code"], "TOKEN_EXPIRED")
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.json()["detail"]["code"], "SESSION_REVOKED")
        self.assertEqual(bearer_over_http.status_code, 426)
        self.assertEqual(
            bearer_over_http.json()["detail"]["code"],
            "HTTPS_REQUIRED",
        )

    async def test_plain_http_login_is_refused_without_processing_credentials(self) -> None:
        app, repository, _ = self.build_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": self.password},
            )

        self.assertEqual(response.status_code, 426, response.text)
        self.assertEqual(response.json()["detail"]["code"], "HTTPS_REQUIRED")
        self.assertEqual(repository.sessions, {})
        self.assertTrue(
            any(
                event.event == "authentication.transport"
                and event.reason == "https_required"
                for event in repository.audits
            )
        )

    async def test_ambiguous_forwarded_proto_is_never_treated_as_https(self) -> None:
        app, repository, _ = self.build_app()
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        with mock.patch.object(
            __import__("app.api.security", fromlist=["settings"]).settings,
            "auth_trust_proxy_headers",
            True,
        ), mock.patch.object(
            __import__("app.api.security", fromlist=["settings"]).settings,
            "auth_trusted_proxy_cidrs",
            ["127.0.0.1/32"],
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/v1/auth/login",
                    headers={"X-Forwarded-Proto": "https,http"},
                    json={"username": "admin", "password": self.password},
                )

        self.assertEqual(response.status_code, 426, response.text)
        self.assertEqual(response.json()["detail"]["code"], "HTTPS_REQUIRED")
        self.assertEqual(repository.sessions, {})

    async def test_legacy_viewer_requires_explicit_role_migration(self) -> None:
        repository = InMemoryIdentityRepository(
            [
                UserIdentity(
                    UUID("00000000-0000-0000-0000-000000000004"),
                    "legacy-viewer",
                    self.password_hash,
                    "viewer",
                    "role_migration_required",
                )
            ]
        )
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: Identity(repository)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "legacy-viewer", "password": self.password},
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "ROLE_MIGRATION_REQUIRED",
        )
        self.assertEqual(repository.sessions, {})

    async def test_account_change_during_login_does_not_issue_a_session(self) -> None:
        class AccountChangedRepository(InMemoryIdentityRepository):
            def complete_login(self, *args, **kwargs) -> bool:
                current = self.users["admin"]
                self.users["admin"] = UserIdentity(
                    current.id,
                    current.username,
                    current.password_hash,
                    current.role,
                    "disabled",
                    current.auth_version + 1,
                )
                return super().complete_login(*args, **kwargs)

        repository = AccountChangedRepository(
            [
                UserIdentity(
                    UUID("00000000-0000-0000-0000-000000000001"),
                    "admin",
                    self.password_hash,
                    "admin",
                    "active",
                )
            ]
        )
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: Identity(repository)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": self.password},
            )

        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["detail"]["code"], "CREDENTIALS_INVALID")
        self.assertEqual(repository.sessions, {})
        self.assertTrue(
            any(event.reason == "account_changed" for event in repository.audits)
        )

    async def test_bad_credentials_are_redacted_audited_and_rate_limited(self) -> None:
        app, repository, _ = self.build_app()
        transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 1234))
        oversized_secret = "do-not-reflect-" + "x" * 1100
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            oversized = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": oversized_secret},
            )
            invalid_unicode = await client.post(
                "/api/v1/auth/login",
                content=b'{"username":"admin","password":"\\ud800"}',
                headers={"Content-Type": "application/json"},
            )
            unknown = await client.post(
                "/api/v1/auth/login",
                json={"username": "missing-user", "password": "wrong-password"},
            )
            wrong_responses = [
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "wrong-password"},
                )
                for _ in range(5)
            ]
            still_blocked = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": self.password},
            )

        self.assertEqual(oversized.status_code, 401, oversized.text)
        self.assertNotIn(oversized_secret, oversized.text)
        self.assertEqual(invalid_unicode.status_code, 400, invalid_unicode.text)
        self.assertEqual(
            invalid_unicode.json()["detail"]["code"],
            "AUTH_REQUEST_INVALID",
        )
        self.assertEqual(unknown.status_code, 401, unknown.text)
        self.assertEqual(unknown.json()["detail"]["code"], "CREDENTIALS_INVALID")
        self.assertEqual(wrong_responses[-1].status_code, 429)
        self.assertEqual(
            wrong_responses[-1].json()["detail"]["code"],
            "LOGIN_THROTTLED",
        )
        self.assertIn("retry-after", wrong_responses[-1].headers)
        self.assertEqual(still_blocked.status_code, 429)
        serialized = repr(repository.audits)
        self.assertNotIn(oversized_secret, serialized)
        self.assertNotIn(self.password, serialized)
        self.assertTrue(
            any(
                event.event == "identity.login"
                and event.outcome == "denied"
                and event.reason == "credentials_invalid"
                for event in repository.audits
            )
        )
        self.assertTrue(
            any(
                event.event == "identity.login"
                and event.outcome == "denied"
                and event.reason == "request_invalid"
                for event in repository.audits
            )
        )
        ip_subjects = [
            state
            for (subject_type, _), state in repository.login_limits.items()
            if subject_type == "client_ip"
        ]
        self.assertEqual(len(ip_subjects), 1)
        self.assertGreaterEqual(ip_subjects[0][0], 7)

    async def test_successful_other_account_does_not_reset_client_ip_limit(self) -> None:
        app, _, _ = self.build_app()
        transport = httpx.ASGITransport(app=app, client=("192.0.2.20", 1234))
        with mock.patch.object(identity_module, "LOGIN_IP_MAX_FAILURES", 3):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                for username in ("missing-one", "missing-two"):
                    failed = await client.post(
                        "/api/v1/auth/login",
                        json={"username": username, "password": "wrong-password"},
                    )
                    self.assertEqual(failed.status_code, 401, failed.text)
                valid = await self.login(client, "operator")
                blocked = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "missing-three", "password": "wrong-password"},
                )

        self.assertEqual(valid["user"]["role"], "operator")
        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertEqual(blocked.json()["detail"]["code"], "LOGIN_THROTTLED")

    def test_unsupported_legacy_hash_uses_same_kdf_path_as_unknown_user(self) -> None:
        dummy_hash = identity_module._dummy_password_hash()
        legacy_repository = InMemoryIdentityRepository(
            [
                UserIdentity(
                    UUID("00000000-0000-0000-0000-000000000004"),
                    "legacy-viewer",
                    "$2b$12$unsupported-legacy-hash",
                    "viewer",
                    "role_migration_required",
                )
            ]
        )
        calls: list[str] = []

        def record_verification(password: str, encoded: str) -> bool:
            del password
            calls.append(encoded)
            return False

        with mock.patch.object(identity_module, "verify_password", record_verification):
            for username in ("legacy-viewer", "missing-user"):
                with self.assertRaises(identity_module.IdentityError):
                    Identity(legacy_repository).authenticate(username, "wrong-password")

        self.assertEqual(calls, [dummy_hash, dummy_hash])

    async def test_auth_storage_failure_fails_closed_while_liveness_survives(self) -> None:
        class BrokenIdentity:
            def reject_anonymous(self, *args, **kwargs) -> None:
                raise RuntimeError("identity database unavailable")

        app = FastAPI()
        app.include_router(health_router, prefix="/api/v1")
        app.include_router(delivery_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: BrokenIdentity()
        app.dependency_overrides[get_solution_delivery] = lambda: SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            protected = await client.get("/api/v1/solution-packages")
            liveness = await client.get("/api/v1/health/live")

        self.assertEqual(protected.status_code, 503, protected.text)
        self.assertEqual(protected.json()["detail"]["code"], "AUTH_UNAVAILABLE")
        self.assertEqual(liveness.status_code, 200, liveness.text)

    async def test_openapi_declares_bearer_security_for_delivery(self) -> None:
        app, _, _ = self.build_app()
        schema = app.openapi()

        self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])
        self.assertEqual(
            schema["paths"]["/api/v1/solution-packages"]["get"]["security"],
            [{"HTTPBearer": []}],
        )
        self.assertNotIn(
            "security",
            schema["paths"]["/api/v1/health/live"]["get"],
        )


if __name__ == "__main__":
    unittest.main()
