from __future__ import annotations

from datetime import datetime, timezone
import os
import unittest
from unittest import mock
from uuid import UUID


# Settings are imported transitively by the authentication adapter.  These are
# test-only values; the in-memory identity repository is the public seam under
# test and no external service is contacted.
os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

import httpx
from fastapi import APIRouter, FastAPI

from app.api.admin import router as admin_router
from app.api.alarm_levels import router as alarm_levels_router
from app.api.alarm_events import router as alarm_event_router
from app.api.alarms import router as alarms_router
from app.api.auth import router as auth_router
from app.api.categories import router as categories_router
from app.api.device_templates import router as device_templates_router
from app.api.entities import router as entities_router
from app.api.fault_maps import router as fault_maps_router
from app.api.health import router as health_router
from app.api.nanomq import router as nanomq_router
from app.api.neuron import router as neuron_router
from app.api.nodes import router as nodes_router
from app.api.rule_templates import router as rule_templates_router
from app.api.rules import router as rules_router
from app.api.security import get_identity
from app.api.solution_delivery import router as solution_delivery_router
from app.api.entity_instances import router as entity_instances_router
from app.api.control_commands import router as control_commands_router
from app.api.rpc import router as rpc_router
from app.api.tags import router as tags_router
from app.api.telemetry import router as telemetry_router
from app.services.identity import (
    AuditEvent,
    Identity,
    InMemoryIdentityRepository,
    UserIdentity,
    hash_password,
)


Operation = tuple[str, str]


def _operations(method: str, *paths: str) -> set[Operation]:
    return {(method, path) for path in paths}


RUNTIME_READ = _operations(
    "GET",
    "/api/v1/health",
    "/api/v1/health/ready",
    "/api/v1/nodes",
    "/api/v1/nodes/{node_id}",
    "/api/v1/nodes/{node_id}/tree",
    "/api/v1/tags",
    "/api/v1/tags/{tag_id}",
    "/api/v1/tags/{tag_id}/history",
    "/api/v1/telemetry",
    "/api/v1/telemetry/export",
    "/api/v1/alarms",
    "/api/v1/alarms/alarm-types",
    "/api/v1/alarms/counts",
    "/api/v1/alarms/entities",
    "/api/v1/alarms/group-counts",
    "/api/v1/entities/{entity_id}/realtime",
    "/api/v1/entities/{entity_id}/history",
)

CONFIGURATION_READ = _operations(
    "GET",
    "/api/v1/nodes/export",
    "/api/v1/tags/export",
    "/api/v1/tags/alarm-config",
    "/api/v1/categories",
    "/api/v1/rules",
    "/api/v1/rules/{rule_id}",
    "/api/v1/rule-templates",
    "/api/v1/rule-templates/{template_id}",
    "/api/v1/entities/export",
    "/api/v1/entities",
    "/api/v1/entities/bindings",
    "/api/v1/entities/{entity_id}",
    "/api/v1/fault-maps",
    "/api/v1/fault-maps/{map_id}",
    "/api/v1/alarm-levels",
    "/api/v1/alarm-levels/{level_id}",
    "/api/v1/alarm-levels/{level_id}/entities",
    "/api/v1/entities/{entity_id}/alarm-levels",
    "/api/v1/device-templates",
    "/api/v1/device-templates/{template_id}",
)

CONFIGURATION_WRITE = {
    *_operations("POST", "/api/v1/nodes"),
    *_operations(
        "PUT",
        "/api/v1/nodes/{node_id}",
        "/api/v1/tags/{tag_id}",
        "/api/v1/tags/batch",
    ),
    *_operations(
        "DELETE",
        "/api/v1/nodes/{node_id}",
        "/api/v1/tags/{tag_id}",
    ),
    *_operations(
        "POST",
        "/api/v1/tags",
        "/api/v1/tags/import-neuron",
        "/api/v1/categories",
        "/api/v1/rules",
        "/api/v1/rules/evaluate",
        "/api/v1/rules/{rule_id}/simulate",
        "/api/v1/rules/{rule_id}/dry-run",
        "/api/v1/rule-templates",
        "/api/v1/rule-templates/{template_id}/apply",
        "/api/v1/entities/seed",
        "/api/v1/entities/bindings/auto-bind",
        "/api/v1/entities/import",
        "/api/v1/entities",
        "/api/v1/entities/bindings/batch",
        "/api/v1/entities/{entity_id}/bindings",
        "/api/v1/fault-maps",
        "/api/v1/alarm-levels",
        "/api/v1/alarm-levels/{level_id}/entities",
        "/api/v1/device-templates",
        "/api/v1/device-templates/{template_id}/apply",
    ),
    *_operations(
        "PUT",
        "/api/v1/categories/{category_id}",
        "/api/v1/rules/{rule_id}",
        "/api/v1/rule-templates/{template_id}",
        "/api/v1/entities/{entity_id}",
        "/api/v1/fault-maps/{map_id}",
        "/api/v1/alarm-levels/{level_id}",
        "/api/v1/device-templates/{template_id}",
    ),
    *_operations(
        "DELETE",
        "/api/v1/categories/{category_id}",
        "/api/v1/rules/{rule_id}",
        "/api/v1/rule-templates/{template_id}",
        "/api/v1/entities/bindings/batch",
        "/api/v1/entities/{entity_id}",
        "/api/v1/entities/{entity_id}/bindings/{binding_id}",
        "/api/v1/fault-maps/{map_id}",
        "/api/v1/alarm-levels/{level_id}",
        "/api/v1/alarm-levels/{level_id}/entities/{binding_id}",
        "/api/v1/device-templates/{template_id}",
    ),
}

ALARM_ACKNOWLEDGE = _operations(
    "PUT",
    "/api/v1/alarms/{alarm_id}/acknowledge",
)

# ADR-0004 removes these two compatibility writes in Ticket #14.  Until then
# they are never operator capabilities and remain independently visible here.
LEGACY_ALARM_WRITE = {
    ("POST", "/api/v1/alarms"),
    ("PUT", "/api/v1/alarms/{alarm_id}/resolve"),
}

TICKET_03_CAPABILITIES: dict[Operation, str] = {
    **{operation: "runtime.read" for operation in RUNTIME_READ},
    **{operation: "configuration.read" for operation in CONFIGURATION_READ},
    **{operation: "configuration.write" for operation in CONFIGURATION_WRITE},
    **{operation: "alarm.acknowledge" for operation in ALARM_ACKNOWLEDGE},
    **{operation: "legacy_alarm.write" for operation in LEGACY_ALARM_WRITE},
}

# Ticket #4 owns the control/system-management REST surface below, plus the
# WebSocket surface (which intentionally does not appear in OpenAPI).
ISSUE_04_REST = {
    *_operations(
        "GET",
        "/api/v1/pipeline/config",
        "/api/v1/mqtt-config",
        "/api/v1/neuron/nodes",
        "/api/v1/neuron/groups",
        "/api/v1/neuron/tags",
        "/api/v1/neuron/status",
        "/api/v1/nanomq/status",
        "/api/v1/nanomq/clients",
        "/api/v1/nanomq/subscriptions",
        "/api/v1/nanomq/routes",
        "/api/v1/nanomq/acl",
        "/api/v1/nanomq/config",
    ),
    *_operations(
        "PUT",
        "/api/v1/pipeline/config",
        "/api/v1/mqtt-config",
        "/api/v1/nanomq/config",
    ),
    *_operations(
        "POST",
        "/api/v1/query",
        "/api/v1/admin/truncate",
        "/api/v1/neuron/nodes",
        "/api/v1/neuron/nodes/{name}/start",
        "/api/v1/neuron/nodes/{name}/stop",
        "/api/v1/neuron/groups",
        "/api/v1/neuron/tags",
        "/api/v1/entities/{entity_id}/write",
        "/api/v1/nanomq/subscribe",
        "/api/v1/nanomq/acl",
        "/api/v1/nanomq/restart",
    ),
    *_operations(
        "DELETE",
        "/api/v1/neuron/nodes/{name}",
        "/api/v1/neuron/groups/{node}/{name}",
        "/api/v1/neuron/tags/{node}/{group}/{name}",
    ),
}

AUTH_OPERATIONS = {
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/ws-ticket"),
}

DELIVERY_OPERATIONS = {
    ("GET", "/api/v1/solution-packages"),
    ("POST", "/api/v1/solution-packages/import"),
    ("POST", "/api/v1/solution-packages/{package_record_id}/install-plans"),
    ("GET", "/api/v1/install-plans/{plan_id}"),
    ("POST", "/api/v1/install-plans/{plan_id}/apply"),
    ("GET", "/api/v1/solution-installations"),
    ("GET", "/api/v1/site-configuration-versions/{version}"),
    (
        "POST",
        "/api/v1/solution-installations/{installation_id}/acceptance-runs",
    ),
    ("GET", "/api/v1/delivery-reports/{report_id}"),
    ("GET", "/api/v1/entity-instances/{entity_instance_id}/realtime"),
}

TICKET_07_CAPABILITIES = {
    ("GET", "/api/v1/entity-instances"): "runtime.read",
    ("GET", "/api/v1/entity-instances/legacy-migration-preview"): "configuration.read",
    ("GET", "/api/v1/entity-instances/{entity_instance_id}/source-failover"): "configuration.read",
    ("POST", "/api/v1/entity-instances/{entity_instance_id}/source-failover"): "configuration.write",
}

TICKET_08_09_CAPABILITIES = {
    ("POST", "/api/v1/entity-instances/{entity_instance_id}/control-confirmations"): "control.write",
    ("POST", "/api/v1/entity-instances/{entity_instance_id}/control-commands"): "control.write",
    ("GET", "/api/v1/control-commands/{command_id}"): "control.write",
    ("POST", "/api/v1/control-commands/{command_id}/reconcile"): "control.write",
    ("POST", "/api/v1/neuron/write"): "control.write",
    ("POST", "/api/v1/devices/{node_id}/rpc"): "control.write",
}

TICKET_12_CAPABILITIES = {
    ("GET", "/api/v1/alarm-events"): "runtime.read",
    ("GET", "/api/v1/alarm-events/{event_id}"): "runtime.read",
    ("GET", "/api/v1/alarm-events/{event_id}/transitions"): "runtime.read",
    ("POST", "/api/v1/alarm-events/{event_id}/acknowledgements"): "alarm.acknowledge",
}

ANONYMOUS_LIVENESS = {("GET", "/api/v1/health/live")}


FULL_API_ROUTERS: tuple[APIRouter, ...] = (
    health_router,
    auth_router,
    nodes_router,
    tags_router,
    telemetry_router,
    admin_router,
    neuron_router,
    categories_router,
    rules_router,
    alarms_router,
    alarm_event_router,
    rule_templates_router,
    entities_router,
    fault_maps_router,
    alarm_levels_router,
    device_templates_router,
    nanomq_router,
    solution_delivery_router,
    entity_instances_router,
    control_commands_router,
    rpc_router,
)


NODE_ID = UUID("10000000-0000-0000-0000-000000000001")
TAG_ID = UUID("10000000-0000-0000-0000-000000000002")
CATEGORY_ID = UUID("20000000-0000-0000-0000-000000000001")
ALARM_ID = UUID("30000000-0000-0000-0000-000000000001")


class FakeDatabaseState:
    """A database boundary fake; identity and authorization remain real."""

    def __init__(
        self,
        *,
        node_config: dict | None = None,
        tag_configuration: bool = False,
    ) -> None:
        self.node_config = node_config
        self.tag_configuration = tag_configuration
        self.executions: list[tuple[str, object]] = []
        self.commits = 0

    def connection(self) -> "FakeConnection":
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, state: FakeDatabaseState) -> None:
        self.state = state

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self.state)

    def commit(self) -> None:
        self.state.commits += 1


class FakeCursor:
    def __init__(self, state: FakeDatabaseState) -> None:
        self.state = state
        self.description: list[tuple[str]] = []
        self._one: tuple | None = None
        self._all: list[tuple] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        normalized = " ".join(query.split())
        self.state.executions.append((normalized, params))
        self.description = []
        self._one = None
        self._all = []

        if "FROM t_nodes n" in normalized and "GROUP BY n.id" in normalized:
            self.description = [
                (name,)
                for name in (
                    "id",
                    "name",
                    "parent_id",
                    "layer",
                    "node_type",
                    "sort_order",
                    "enabled",
                    "config",
                    "source_catalog_key",
                    "created_at",
                    "tag_count",
                )
            ]
            if self.state.node_config is not None:
                self._all = [
                    (
                        NODE_ID,
                        "PCS-01",
                        None,
                        1,
                        "PCS",
                        0,
                        True,
                        self.state.node_config,
                        "stable-device-key-should-not-leak",
                        datetime(2026, 8, 13, tzinfo=timezone.utc),
                        3,
                    )
                ]
            return

        if "FROM t_node_categories ORDER BY name" in normalized:
            self.description = [
                (name,)
                for name in ("id", "name", "node_type", "description", "created_at")
            ]
            self._all = []
            return

        if "FROM t_tags t" in normalized and "LEFT JOIN t_fault_maps" in normalized:
            tag_columns = (
                "id",
                "node_id",
                "name",
                "display_name",
                "data_type",
                "tag_type",
                "unit",
                "scale_factor",
                "value_offset",
                "source_path",
                "source_type",
                "read_write",
                "enabled",
                "description",
                "aggregate_fn",
                "formula",
                "formula_type",
                "sources",
                "node_name",
                "alarm_level",
                "alarm_type",
                "alarm_threshold",
                "fault_map_id",
                "fault_map_name",
                "latest_ts",
                "value_float",
                "value_int",
                "value_bool",
                "value_str",
                "quality",
            )
            self.description = [(name,) for name in tag_columns]
            if self.state.tag_configuration:
                self._all = [
                    (
                        TAG_ID,
                        NODE_ID,
                        "active_power",
                        "Active power",
                        "FLOAT",
                        "PHYSICAL",
                        "kW",
                        1.0,
                        0.0,
                        "neuron/pcs/internal-source-path",
                        "neuron-secret-source-type",
                        "R",
                        True,
                        "safe description",
                        "LAST",
                        "secret-formula-input",
                        "expression",
                        ["secret-source-tag-id"],
                        "PCS-01",
                        "error1",
                        "over-temperature",
                        80.0,
                        UUID("10000000-0000-0000-0000-000000000003"),
                        "secret-fault-map",
                        datetime(2026, 8, 13, tzinfo=timezone.utc),
                        12.5,
                        None,
                        None,
                        None,
                        0,
                    )
                ]
            return

        if normalized.startswith("SELECT COUNT(*) FROM t_tags"):
            self._one = (1 if self.state.tag_configuration else 0,)
            return

        if "INSERT INTO t_node_categories" in normalized:
            if params and params[0] == "will-fail":
                raise RuntimeError("business database write failed")
            self._one = (CATEGORY_ID,)
            return

        if "SET acknowledged = TRUE" in normalized:
            self._one = (ALARM_ID,)
            return

        if "SET resolved_at" in normalized:
            self._one = (ALARM_ID,)
            return

        if "GROUP BY node_id" in normalized and "FROM t_alarms" in normalized:
            self._all = [(NODE_ID, 3)]
            return

        raise AssertionError(f"Unexpected database query: {normalized}")

    def fetchone(self) -> tuple | None:
        return self._one

    def fetchall(self) -> list[tuple]:
        return self._all


class BusinessRestAuthorizationPublicApiTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(cls.password, salt=b"business-auth!!")

    def build_identity(self) -> tuple[Identity, InMemoryIdentityRepository]:
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
        return Identity(repository), repository

    def build_representative_app(
        self,
    ) -> tuple[FastAPI, InMemoryIdentityRepository]:
        identity, repository = self.build_identity()
        app = FastAPI()
        for router in (
            auth_router,
            nodes_router,
            tags_router,
            categories_router,
            alarms_router,
            entities_router,
        ):
            app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity
        return app, repository

    async def login(self, client: httpx.AsyncClient, username: str) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def test_anonymous_business_clients_get_stable_401_and_audit(self) -> None:
        app, repository = self.build_representative_app()
        database = FakeDatabaseState()
        transport = httpx.ASGITransport(app=app)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                runtime = await client.get("/api/v1/nodes")
                configuration = await client.get("/api/v1/categories")

        expected = {
            "detail": {
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Bearer authentication is required",
            }
        }
        self.assertEqual(runtime.status_code, 401, runtime.text)
        self.assertEqual(runtime.json(), expected)
        self.assertEqual(runtime.headers["www-authenticate"], "Bearer")
        self.assertEqual(configuration.status_code, 401, configuration.text)
        self.assertEqual(configuration.json(), expected)
        self.assertEqual(database.executions, [], "authorization must precede database IO")
        self.assertEqual(
            {
                event.target
                for event in repository.audits
                if event.event == "authentication.decision"
                and event.reason == "authentication_required"
            },
            {"/api/v1/nodes", "/api/v1/categories"},
        )

    async def test_each_role_follows_the_business_permission_matrix(self) -> None:
        app, repository = self.build_representative_app()
        database = FakeDatabaseState()
        transport = httpx.ASGITransport(app=app)

        expected_statuses = {
            "admin": (200, 200, 200, 200, 200),
            "engineer": (200, 200, 200, 200, 200),
            "operator": (200, 403, 403, 200, 403),
        }
        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = {
                    role: await self.login(client, role)
                    for role in ("admin", "engineer", "operator")
                }
                for role, expected in expected_statuses.items():
                    with self.subTest(role=role):
                        responses = (
                            await client.get("/api/v1/nodes", headers=headers[role]),
                            await client.get(
                                "/api/v1/categories",
                                headers=headers[role],
                            ),
                            await client.post(
                                "/api/v1/categories",
                                headers=headers[role],
                                json={"name": "Energy", "node_type": "ENERGY"},
                            ),
                            await client.put(
                                f"/api/v1/alarms/{ALARM_ID}/acknowledge",
                                headers=headers[role],
                                json={},
                            ),
                            await client.put(
                                f"/api/v1/alarms/{ALARM_ID}/resolve",
                                headers=headers[role],
                            ),
                        )
                        self.assertEqual(
                            tuple(response.status_code for response in responses),
                            expected,
                            [response.text for response in responses],
                        )

        operator_denials = {
            event.target
            for event in repository.audits
            if event.event == "authorization.decision"
            and event.outcome == "denied"
            and event.actor == "user:00000000-0000-0000-0000-000000000003"
        }
        self.assertEqual(
            operator_denials,
            {"configuration.read", "configuration.write", "legacy_alarm.write"},
        )

    async def test_configuration_write_fails_closed_before_business_when_audit_is_unavailable(
        self,
    ) -> None:
        class RequestedAuditUnavailableRepository(InMemoryIdentityRepository):
            def append_audit(
                self,
                event: AuditEvent,
                *,
                connection: object | None = None,
            ) -> None:
                if event.event == "configuration.change":
                    raise RuntimeError("audit storage unavailable")
                super().append_audit(event, connection=connection)

        repository = RequestedAuditUnavailableRepository(
            [
                UserIdentity(
                    UUID("00000000-0000-0000-0000-000000000002"),
                    "engineer",
                    self.password_hash,
                    "engineer",
                    "active",
                )
            ]
        )
        identity = Identity(repository)
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(categories_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity
        database = FakeDatabaseState()
        transport = httpx.ASGITransport(app=app)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = await self.login(client, "engineer")
                response = await client.post(
                    "/api/v1/categories?trace=must-not-enter-audit",
                    headers={
                        **headers,
                        "X-Request-ID": "audit-request-1",
                        "X-Site-Secret": "header-must-not-enter-audit",
                    },
                    json={
                        "name": "secret-body-must-not-enter-audit",
                        "node_type": "ENERGY",
                    },
                )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json(),
            {
                "detail": {
                    "code": "AUDIT_UNAVAILABLE",
                    "message": "Configuration audit service is unavailable",
                }
            },
        )
        self.assertEqual(database.executions, [])
        self.assertEqual(database.commits, 0)

    async def test_successful_configuration_write_has_minimal_requested_and_success_audit(
        self,
    ) -> None:
        app, repository = self.build_representative_app()
        database = FakeDatabaseState()
        transport = httpx.ASGITransport(app=app)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = await self.login(client, "engineer")
                token = headers["Authorization"]
                response = await client.post(
                    "/api/v1/categories?site_password=query-must-not-enter-audit",
                    headers={
                        **headers,
                        "X-Request-ID": "audit-request-2",
                        "X-Site-Secret": "header-must-not-enter-audit",
                    },
                    json={
                        "name": "body-secret-must-not-enter-audit",
                        "node_type": "ENERGY",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        changes = [
            event for event in repository.audits if event.event == "configuration.change"
        ]
        self.assertEqual([event.outcome for event in changes], ["requested", "success"])
        self.assertEqual(
            [event.actor for event in changes],
            [
                "user:00000000-0000-0000-0000-000000000002",
                "user:00000000-0000-0000-0000-000000000002",
            ],
        )
        self.assertEqual(
            [event.target for event in changes],
            ["POST /api/v1/categories", "POST /api/v1/categories"],
        )
        self.assertEqual(
            [event.request_id for event in changes],
            ["audit-request-2", "audit-request-2"],
        )
        self.assertEqual([event.details for event in changes], [None, None])
        serialized = repr(changes)
        for secret in (
            token,
            "body-secret-must-not-enter-audit",
            "query-must-not-enter-audit",
            "header-must-not-enter-audit",
        ):
            self.assertNotIn(secret, serialized)

    async def test_failed_configuration_write_never_claims_success(self) -> None:
        app, repository = self.build_representative_app()
        database = FakeDatabaseState()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = await self.login(client, "engineer")
                response = await client.post(
                    "/api/v1/categories",
                    headers=headers,
                    json={"name": "will-fail", "node_type": "BROKEN"},
                )

        self.assertEqual(response.status_code, 500, response.text)
        changes = [
            event for event in repository.audits if event.event == "configuration.change"
        ]
        self.assertEqual([event.outcome for event in changes], ["requested"])
        self.assertEqual(database.commits, 0)

    async def test_alarm_acknowledgement_actor_cannot_be_forged_by_request_body(
        self,
    ) -> None:
        app, _ = self.build_representative_app()
        database = FakeDatabaseState()
        transport = httpx.ASGITransport(app=app)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = await self.login(client, "operator")
                forged = await client.put(
                    f"/api/v1/alarms/{ALARM_ID}/acknowledge",
                    headers=headers,
                    json={"ack_user": "forged-admin"},
                )
                response = await client.put(
                    f"/api/v1/alarms/{ALARM_ID}/acknowledge",
                    headers=headers,
                    json={},
                )

        actor = "user:00000000-0000-0000-0000-000000000003"
        self.assertEqual(forged.status_code, 422, forged.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ack_user"], actor)
        acknowledgement_params = next(
            params
            for query, params in database.executions
            if "SET acknowledged = TRUE" in query
        )
        self.assertEqual(acknowledgement_params[0], actor)
        self.assertNotIn("forged-admin", repr(database.executions))

    async def test_operator_runtime_node_representation_does_not_leak_secrets(
        self,
    ) -> None:
        app, _ = self.build_representative_app()
        database = FakeDatabaseState(
            node_config={
                "display": "safe metadata",
                "password": "node-password-should-not-leak",
                "nested": {"api_token": "token-should-not-leak"},
            }
        )
        transport = httpx.ASGITransport(app=app)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = await self.login(client, "operator")
                response = await client.get("/api/v1/nodes", headers=headers)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("node-password-should-not-leak", response.text)
        self.assertNotIn("token-should-not-leak", response.text)
        self.assertNotIn("stable-device-key-should-not-leak", response.text)

    async def test_operator_runtime_tag_representation_omits_configuration_fields(
        self,
    ) -> None:
        app, _ = self.build_representative_app()
        database = FakeDatabaseState(tag_configuration=True)
        transport = httpx.ASGITransport(app=app)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = await self.login(client, "operator")
                response = await client.get("/api/v1/tags", headers=headers)

        self.assertEqual(response.status_code, 200, response.text)
        tag = response.json()["tags"][0]
        self.assertEqual(tag["name"], "active_power")
        self.assertEqual(tag["eng_value"], 12.5)
        self.assertTrue(
            {
                "source_path",
                "source_type",
                "sources",
                "formula",
                "formula_type",
                "aggregate_fn",
                "fault_map_id",
                "fault_map_name",
                "alarm_type",
                "alarm_threshold",
                "scale_factor",
                "value_offset",
            }.isdisjoint(tag),
            tag,
        )

    async def test_operator_entity_runtime_omits_binding_internals_but_engineer_retains_them(
        self,
    ) -> None:
        app, _ = self.build_representative_app()
        realtime = {
            "entity_id": str(CATEGORY_ID),
            "entity_name": "pcs.activePower",
            "entity_display_name": "PCS active power",
            "entity_type": "R",
            "binding_id": "binding-internal-id",
            "tag_id": str(TAG_ID),
            "tag_name": "neuron_internal_active_power",
            "node_id": str(NODE_ID),
            "node_name": "internal-neuron-node",
            "data_type": "FLOAT",
            "unit": "kW",
            "ts": "2026-08-13T12:00:00+00:00",
            "value": 12.5,
            "quality": 192,
        }
        history = {
            "entity_id": str(CATEGORY_ID),
            "entity_name": "pcs.activePower",
            "tag_id": str(TAG_ID),
            "range": "1h",
            "page": 1,
            "page_size": 500,
            "total": 1,
            "total_pages": 1,
            "points": [
                {
                    "ts": "2026-08-13T12:00:00+00:00",
                    "value": 12.5,
                    "quality": 192,
                }
            ],
        }
        transport = httpx.ASGITransport(app=app)

        with (
            mock.patch("app.api.entities.get_entity_realtime", return_value=realtime),
            mock.patch("app.api.entities.get_entity_history", return_value=history),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                operator = await self.login(client, "operator")
                engineer = await self.login(client, "engineer")
                operator_realtime = await client.get(
                    f"/api/v1/entities/{CATEGORY_ID}/realtime",
                    headers=operator,
                )
                operator_history = await client.get(
                    f"/api/v1/entities/{CATEGORY_ID}/history",
                    headers=operator,
                )
                engineer_realtime = await client.get(
                    f"/api/v1/entities/{CATEGORY_ID}/realtime",
                    headers=engineer,
                )
                engineer_history = await client.get(
                    f"/api/v1/entities/{CATEGORY_ID}/history",
                    headers=engineer,
                )

        self.assertEqual(operator_realtime.status_code, 200, operator_realtime.text)
        self.assertEqual(operator_history.status_code, 200, operator_history.text)
        binding_internals = {"binding_id", "tag_id", "tag_name", "node_id", "node_name"}
        self.assertTrue(
            binding_internals.isdisjoint(operator_realtime.json()),
            operator_realtime.json(),
        )
        self.assertNotIn("tag_id", operator_history.json())
        self.assertEqual(operator_realtime.json()["value"], 12.5)
        self.assertEqual(operator_history.json()["points"][0]["value"], 12.5)

        self.assertEqual(engineer_realtime.status_code, 200, engineer_realtime.text)
        self.assertEqual(engineer_history.status_code, 200, engineer_history.text)
        self.assertEqual(engineer_realtime.json()["binding_id"], "binding-internal-id")
        self.assertEqual(engineer_realtime.json()["tag_id"], str(TAG_ID))
        self.assertEqual(engineer_history.json()["tag_id"], str(TAG_ID))

    async def test_alarm_counts_is_the_count_contract_not_alarm_type_metadata(
        self,
    ) -> None:
        app, _ = self.build_representative_app()
        database = FakeDatabaseState()
        transport = httpx.ASGITransport(app=app)

        with mock.patch(
            "app.services.telemetry_store.get_connection",
            database.connection,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
            ) as client:
                headers = await self.login(client, "operator")
                response = await client.get(
                    "/api/v1/alarms/counts",
                    headers=headers,
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"counts": {str(NODE_ID): 3}})


class BusinessRestOpenApiCoverageTest(unittest.TestCase):
    def build_full_api(self) -> FastAPI:
        app = FastAPI()
        for router in FULL_API_ROUTERS:
            app.include_router(router, prefix="/api/v1")
        return app

    def test_all_83_ticket_operations_are_classified_and_secured(self) -> None:
        self.assertEqual(len(RUNTIME_READ), 17)
        self.assertEqual(len(CONFIGURATION_READ), 20)
        self.assertEqual(len(CONFIGURATION_WRITE), 43)
        self.assertEqual(len(ALARM_ACKNOWLEDGE), 1)
        self.assertEqual(len(LEGACY_ALARM_WRITE), 2)
        self.assertEqual(len(TICKET_03_CAPABILITIES), 83)
        self.assertEqual(len(ISSUE_04_REST), 29)
        self.assertEqual(len(TICKET_12_CAPABILITIES), 4)

        partitions = (
            set(TICKET_03_CAPABILITIES),
            ISSUE_04_REST,
            AUTH_OPERATIONS,
            DELIVERY_OPERATIONS,
            set(TICKET_07_CAPABILITIES),
            set(TICKET_08_09_CAPABILITIES),
            set(TICKET_12_CAPABILITIES),
            ANONYMOUS_LIVENESS,
        )
        for index, left in enumerate(partitions):
            for right in partitions[index + 1 :]:
                self.assertTrue(left.isdisjoint(right), left & right)

        schema = self.build_full_api().openapi()
        registered = {
            (method.upper(), path)
            for path, path_item in schema["paths"].items()
            for method in path_item
            if method in {"get", "post", "put", "patch", "delete"}
        }
        expected_registered = set().union(*partitions)
        self.assertEqual(
            registered,
            expected_registered,
            {
                "unclassified": sorted(registered - expected_registered),
                "missing": sorted(expected_registered - registered),
            },
        )
        self.assertEqual(len(registered), 141)

        for (method, path), capability in sorted(TICKET_07_CAPABILITIES.items()):
            operation = schema["paths"][path][method.lower()]
            self.assertEqual(operation.get("x-zizu-capability"), capability)
            self.assertEqual(operation.get("security"), [{"HTTPBearer": []}])

        for (method, path), capability in sorted(TICKET_03_CAPABILITIES.items()):
            with self.subTest(method=method, path=path):
                operation = schema["paths"][path][method.lower()]
                self.assertEqual(
                    operation.get("x-zizu-capability"),
                    capability,
                    "Every Ticket #3 route must publish its server-side policy",
                )
                self.assertEqual(operation.get("security"), [{"HTTPBearer": []}])

        for (method, path), capability in sorted(TICKET_08_09_CAPABILITIES.items()):
            with self.subTest(method=method, path=path):
                operation = schema["paths"][path][method.lower()]
                self.assertEqual(operation.get("x-zizu-capability"), capability)
                self.assertEqual(operation.get("security"), [{"HTTPBearer": []}])

        for (method, path), capability in sorted(TICKET_12_CAPABILITIES.items()):
            with self.subTest(method=method, path=path):
                operation = schema["paths"][path][method.lower()]
                self.assertEqual(operation.get("x-zizu-capability"), capability)
                self.assertEqual(operation.get("security"), [{"HTTPBearer": []}])

    def test_alarm_counts_route_is_wired_to_its_own_operation(self) -> None:
        schema = self.build_full_api().openapi()
        counts = schema["paths"]["/api/v1/alarms/counts"]["get"]
        alarm_types = schema["paths"]["/api/v1/alarms/alarm-types"]["get"]

        self.assertIn("alarm_counts", counts["operationId"])
        self.assertNotEqual(counts["operationId"], alarm_types["operationId"])


if __name__ == "__main__":
    unittest.main()
