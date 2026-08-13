"""Ticket #4 public security seams for control, management, and WebSocket."""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.entities import router as entities_router
from app.api.nanomq import router as nanomq_router
from app.api.neuron import router as neuron_router
from app.api.websocket import router as websocket_router
from app.main import create_app
from app.api.security import get_identity
from app.api import security as security_adapter
from app.core.config import Settings
from app.services.identity import (
    Identity,
    InMemoryIdentityRepository,
    UserIdentity,
    hash_password,
)
from uuid import UUID


class ControlManagementAuthorizationPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(cls.password, salt=b"control-auth!!!")

    def build_role_app(self) -> FastAPI:
        users = [
            UserIdentity(
                UUID(f"00000000-0000-0000-0000-00000000000{index}"),
                role,
                self.password_hash,
                role,
                "active",
            )
            for index, role in enumerate(("admin", "engineer", "operator"), start=1)
        ]
        app = FastAPI()
        for router in (auth_router, admin_router, neuron_router, entities_router):
            app.include_router(router, prefix="/api/v1")
        identity = Identity(InMemoryIdentityRepository(users))
        app.dependency_overrides[get_identity] = lambda: identity
        return app

    async def login(self, client: httpx.AsyncClient, role: str) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": role, "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def test_anonymous_admin_mutation_is_rejected_before_side_effects(self) -> None:
        app = FastAPI()
        app.include_router(admin_router, prefix="/api/v1")
        repository = InMemoryIdentityRepository()
        app.dependency_overrides[get_identity] = lambda: Identity(repository)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            side_effect=AssertionError("anonymous request reached database"),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                response = await client.post(
                    "/api/v1/admin/truncate",
                    json={"table": "t_telemetry", "confirm": "yes"},
                )

        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "AUTHENTICATION_REQUIRED",
        )
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(
            [event.reason for event in repository.audits],
            ["authentication_required"],
        )

    async def test_roles_follow_system_gateway_and_control_capabilities(self) -> None:
        app = self.build_role_app()
        transport = httpx.ASGITransport(app=app)
        fake_neuron = mock.Mock()
        fake_neuron.get_nodes.return_value = []

        with (
            mock.patch(
                "app.services.neuron_client.get_neuron_client",
                return_value=fake_neuron,
            ),
            mock.patch(
                "app.api.entities.write_entity_value",
                return_value={"status": "accepted"},
            ) as write_value,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = {
                    role: await self.login(client, role)
                    for role in ("admin", "engineer", "operator")
                }
                statuses = {}
                for role in headers:
                    statuses[role] = (
                        (await client.get(
                            "/api/v1/pipeline/config", headers=headers[role]
                        )).status_code,
                        (await client.get(
                            "/api/v1/neuron/nodes", headers=headers[role]
                        )).status_code,
                        (await client.post(
                            "/api/v1/entities/00000000-0000-0000-0000-000000000099/write",
                            headers=headers[role],
                            json={"value": 1},
                        )).status_code,
                    )

        self.assertEqual(statuses["admin"], (200, 200, 200))
        self.assertEqual(statuses["engineer"], (403, 200, 200))
        self.assertEqual(statuses["operator"], (403, 403, 200))
        self.assertEqual(write_value.call_count, 3)


if __name__ == "__main__":
    unittest.main()


class ControlManagementOpenApiCoverageTest(unittest.TestCase):
    def test_all_control_and_management_rest_operations_publish_their_policy(self) -> None:
        app = FastAPI()
        for router in (admin_router, neuron_router, nanomq_router, entities_router):
            app.include_router(router, prefix="/api/v1")
        schema = app.openapi()

        system_manage = {
            (method, path)
            for method, paths in {
                "GET": (
                    "/api/v1/pipeline/config", "/api/v1/mqtt-config",
                    "/api/v1/nanomq/status", "/api/v1/nanomq/clients",
                    "/api/v1/nanomq/subscriptions", "/api/v1/nanomq/routes",
                    "/api/v1/nanomq/acl", "/api/v1/nanomq/config",
                ),
                "PUT": (
                    "/api/v1/pipeline/config", "/api/v1/mqtt-config",
                    "/api/v1/nanomq/config",
                ),
                "POST": (
                    "/api/v1/query", "/api/v1/admin/truncate",
                    "/api/v1/nanomq/publish", "/api/v1/nanomq/subscribe",
                    "/api/v1/nanomq/acl", "/api/v1/nanomq/restart",
                ),
            }.items()
            for path in paths
        }
        gateway_manage = {
            (method, path)
            for method, paths in {
                "GET": (
                    "/api/v1/neuron/nodes", "/api/v1/neuron/groups",
                    "/api/v1/neuron/tags", "/api/v1/neuron/status",
                ),
                "POST": (
                    "/api/v1/neuron/nodes", "/api/v1/neuron/nodes/{name}/start",
                    "/api/v1/neuron/nodes/{name}/stop", "/api/v1/neuron/groups",
                    "/api/v1/neuron/tags",
                ),
                "DELETE": (
                    "/api/v1/neuron/nodes/{name}",
                    "/api/v1/neuron/groups/{node}/{name}",
                    "/api/v1/neuron/tags/{node}/{group}/{name}",
                ),
            }.items()
            for path in paths
        }
        expected = {
            **{operation: "system.manage" for operation in system_manage},
            **{operation: "gateway.manage" for operation in gateway_manage},
            ("POST", "/api/v1/entities/{entity_id}/write"): "control.write",
        }
        self.assertEqual(len(expected), 30)

        for (method, path), capability in expected.items():
            with self.subTest(method=method, path=path):
                operation = schema["paths"][path][method.lower()]
                self.assertEqual(operation.get("x-zizu-capability"), capability)
                self.assertEqual(operation.get("security"), [{"HTTPBearer": []}])


class SecurityDefaultConfigurationTest(unittest.TestCase):
    def test_production_rejects_insecure_anonymous_access(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "ALLOW_INSECURE_ANONYMOUS_ACCESS requires DEPLOYMENT_MODE=development",
        ):
            Settings(
                deployment_mode="production",
                allow_insecure_anonymous_access=True,
                db_password="database-secret-value",
                neuron_password="neuron-secret-value",
                nanomq_api_password="nanomq-secret-value",
                jwt_secret="jwt-secret-value-that-is-at-least-32-chars",
            )

    def test_production_does_not_publish_interactive_api_documentation(self) -> None:
        paths = {route.path for route in create_app().routes if hasattr(route, "path")}
        self.assertNotIn("/api/docs", paths)
        self.assertNotIn("/api/redoc", paths)
        self.assertNotIn("/api/openapi.json", paths)


class InsecureDevelopmentModePublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_development_anonymous_access_is_visible_on_every_response(
        self,
    ) -> None:
        app = FastAPI()
        app.include_router(admin_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: Identity(
            InMemoryIdentityRepository()
        )
        transport = httpx.ASGITransport(app=app)

        with (
            mock.patch.object(security_adapter.settings, "deployment_mode", "development"),
            mock.patch.object(
                security_adapter.settings,
                "allow_insecure_anonymous_access",
                True,
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.get("/api/v1/pipeline/config")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.headers["X-ZiZu-Security-Mode"],
            "insecure-development",
        )


class WebSocketTicketPublicApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(cls.password, salt=b"ws-ticket-salt!")

    def test_authenticated_ticket_is_single_use_and_required_before_subscription(
        self,
    ) -> None:
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
        identity = Identity(repository)
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(websocket_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity

        with TestClient(app, base_url="https://testserver") as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": self.password},
            )
            self.assertEqual(login.status_code, 200, login.text)
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            issued = client.post("/api/v1/auth/ws-ticket", headers=headers)
            self.assertEqual(issued.status_code, 201, issued.text)
            self.assertEqual(issued.headers["Cache-Control"], "no-store")
            ticket = issued.json()["ticket"]
            self.assertNotIn(ticket, repr(repository.ws_tickets))
            self.assertTrue(all(len(key) == 64 for key in repository.ws_tickets))

            with client.websocket_connect("wss://testserver/api/v1/ws/telemetry") as websocket:
                websocket.send_json({"authenticate": {"ticket": ticket}})
                self.assertEqual(
                    websocket.receive_json(),
                    {"type": "authenticated"},
                )
                websocket.send_json({"subscribe": []})
                self.assertEqual(
                    websocket.receive_json(),
                    {"type": "subscribed", "tag_count": 0},
                )

            with client.websocket_connect("wss://testserver/api/v1/ws/telemetry") as websocket:
                websocket.send_json({"authenticate": {"ticket": ticket}})
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(closed.exception.code, 4401)

    def test_logout_revokes_an_already_authenticated_websocket(self) -> None:
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
        identity = Identity(repository)
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(websocket_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity

        with TestClient(app, base_url="https://testserver") as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": self.password},
            )
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            issued = client.post("/api/v1/auth/ws-ticket", headers=headers)

            with client.websocket_connect(
                "wss://testserver/api/v1/ws/telemetry"
            ) as websocket:
                websocket.send_json(
                    {"authenticate": {"ticket": issued.json()["ticket"]}}
                )
                self.assertEqual(
                    websocket.receive_json(),
                    {"type": "authenticated"},
                )
                logout = client.post("/api/v1/auth/logout", headers=headers)
                self.assertEqual(logout.status_code, 204, logout.text)

                websocket.send_json({"subscribe": []})
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(closed.exception.code, 4401)

    def test_plain_websocket_is_rejected_before_consuming_ticket(self) -> None:
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
        identity = Identity(repository)
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(websocket_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity

        with TestClient(app, base_url="https://testserver") as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": self.password},
            )
            issued = client.post(
                "/api/v1/auth/ws-ticket",
                headers={
                    "Authorization": f"Bearer {login.json()['access_token']}"
                },
            )
            ticket = issued.json()["ticket"]

            with client.websocket_connect("ws://testserver/api/v1/ws/telemetry") as websocket:
                websocket.send_json({"authenticate": {"ticket": ticket}})
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(closed.exception.code, 4406)

            with client.websocket_connect("wss://testserver/api/v1/ws/telemetry") as websocket:
                websocket.send_json({"authenticate": {"ticket": ticket}})
                self.assertEqual(websocket.receive_json(), {"type": "authenticated"})
