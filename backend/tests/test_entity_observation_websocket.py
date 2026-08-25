from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.auth import router as auth_router
from app.api.security import get_identity
from app.api.websocket import (
    get_entity_observation_broadcaster,
    get_entity_observation_catalog,
    router as websocket_router,
)
from app.services.data_trunk_outbox import (
    EntityObservationBroadcaster,
    OutboxDispatcher,
    OutboxEvent,
)
from app.services.identity import (
    Identity,
    InMemoryIdentityRepository,
    UserIdentity,
    hash_password,
)


ENTITY_ID = UUID("91000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("91000000-0000-0000-0000-000000000002")


class _EntityCatalog:
    def require(self, instance_ids):
        if tuple(instance_ids) != (ENTITY_ID,):
            raise ValueError("unknown entity")
        return (object(),)


class _Socket:
    def __init__(self) -> None:
        self.messages = []

    async def send_json(self, message) -> None:
        self.messages.append(message)


class _AcceptanceEvidence:
    def __init__(self) -> None:
        self.bindings = []

    def bind(self, application_id, entity_ids, principal):
        application_id = UUID("91000000-0000-0000-0000-000000000020")
        binding = SimpleNamespace(application_id=application_id)
        self.bindings.append((application_id, tuple(entity_ids), principal, binding))
        return binding

    def record_acknowledgement(self, binding, event, runtime_instance_id):
        return None


class EntityObservationBroadcasterTest(unittest.IsolatedAsyncioTestCase):
    async def test_committed_event_redelivery_keeps_stable_event_id(self) -> None:
        broadcaster = EntityObservationBroadcaster()
        socket = _Socket()
        await broadcaster.connect(socket)
        await broadcaster.subscribe(socket, (ENTITY_ID,))
        event = OutboxEvent(
            event_id=EVENT_ID,
            entity_instance_id=ENTITY_ID,
            payload={
                "definition_id": "pcs.active_power",
                "value": 12.345,
                "data_type": "FLOAT",
                "unit": "kW",
                "quality": 192,
                "observed_at": "2026-08-17T00:00:00+00:00",
            },
        )

        await broadcaster.publish(event)
        await broadcaster.publish(event)

        self.assertEqual(len(socket.messages), 2)
        self.assertEqual(
            [item["event_id"] for item in socket.messages],
            [str(EVENT_ID), str(EVENT_ID)],
        )
        self.assertTrue(
            all(item["type"] == "entity_observation" for item in socket.messages)
        )
        self.assertNotIn("topic", repr(socket.messages).lower())
        self.assertNotIn("token", repr(socket.messages).lower())

    async def test_dispatcher_acks_only_after_publish_and_retries_same_event(self) -> None:
        event = OutboxEvent(EVENT_ID, ENTITY_ID, {"value": 12.345})

        class Repository:
            def __init__(self) -> None:
                self.claims = [(event,), (event,)]
                self.attempts = []
                self.published = []

            def claim_unpublished(self, limit):
                self.assert_limit = limit
                return self.claims.pop(0)

            def record_attempt(self, event_id):
                self.attempts.append(event_id)

            def mark_published(self, event_id):
                self.published.append(event_id)

        class Broadcaster:
            def __init__(self) -> None:
                self.calls = 0

            async def publish(self, _event):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("simulated socket failure")

        repository = Repository()
        dispatcher = OutboxDispatcher(repository, Broadcaster())

        self.assertEqual(await dispatcher.run_once(limit=20), 0)
        self.assertEqual(repository.attempts, [EVENT_ID])
        self.assertEqual(repository.published, [])
        self.assertEqual(await dispatcher.run_once(limit=20), 1)
        self.assertEqual(repository.published, [EVENT_ID])

    @unittest.skip("machine acceptance removed by L0/L1/L2 hard cut")
    async def test_authenticated_acceptance_subscription_records_only_client_ack(self) -> None:
        runtime_id = UUID("91000000-0000-0000-0000-000000000003")
        binding = object()

        class Recorder:
            def __init__(self) -> None:
                self.deliveries = []

            def record_acknowledgement(self, actual_binding, event, actual_runtime_id):
                self.deliveries.append(
                    (actual_binding, event.event_id, actual_runtime_id)
                )

        recorder = Recorder()
        try:
            broadcaster = EntityObservationBroadcaster(
                receipt_recorder=recorder,
                runtime_instance_id=runtime_id,
            )
        except TypeError as exc:
            self.fail(f"acceptance receipt recorder is unavailable: {exc}")
        socket = _Socket()
        await broadcaster.connect(socket)
        await broadcaster.subscribe(
            socket,
            (ENTITY_ID,),
            acceptance_binding=binding,
        )
        event = OutboxEvent(EVENT_ID, ENTITY_ID, {"value": 12.345})

        await broadcaster.publish(event)

        self.assertEqual([], recorder.deliveries)
        nonce = socket.messages[-1]["acceptance_ack_nonce"]
        await broadcaster.acknowledge(socket, EVENT_ID, nonce)

        self.assertEqual(
            [(binding, EVENT_ID, runtime_id)],
            recorder.deliveries,
        )

    @unittest.skip("machine acceptance removed by L0/L1/L2 hard cut")
    async def test_acceptance_ack_rejects_event_not_sent_to_socket(self) -> None:
        broadcaster = EntityObservationBroadcaster(
            receipt_recorder=_AcceptanceEvidence(),
        )
        socket = _Socket()
        await broadcaster.connect(socket)
        await broadcaster.subscribe(
            socket,
            (ENTITY_ID,),
            acceptance_binding=object(),
        )

        with self.assertRaisesRegex(ValueError, "ACK_EVENT_NOT_PENDING"):
            await broadcaster.acknowledge(socket, EVENT_ID, "not-a-real-nonce")

    @unittest.skip("machine acceptance removed by L0/L1/L2 hard cut")
    async def test_acceptance_ack_nonce_allows_immediate_ack_and_blocks_forgery(self) -> None:
        runtime_id = UUID("91000000-0000-0000-0000-000000000003")
        application_id = UUID("91000000-0000-0000-0000-000000000020")
        binding = SimpleNamespace(application_id=application_id)

        class Recorder:
            def __init__(self) -> None:
                self.deliveries = []

            def record_acknowledgement(self, actual_binding, event, actual_runtime_id):
                self.deliveries.append((actual_binding, event.event_id, actual_runtime_id))

        class BlockingSocket(_Socket):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def send_json(self, message) -> None:
                self.messages.append(message)
                self.entered.set()
                await self.release.wait()

        recorder = Recorder()
        broadcaster = EntityObservationBroadcaster(
            receipt_recorder=recorder,
            runtime_instance_id=runtime_id,
        )
        socket = BlockingSocket()
        other = _Socket()
        for target in (socket, other):
            await broadcaster.connect(target)
            await broadcaster.subscribe(
                target,
                (ENTITY_ID,),
                acceptance_binding=binding,
            )
        event = OutboxEvent(EVENT_ID, ENTITY_ID, {"value": 12.345})
        publishing = asyncio.create_task(broadcaster.publish(event))
        await socket.entered.wait()
        nonce = socket.messages[-1]["acceptance_ack_nonce"]

        with self.assertRaisesRegex(ValueError, "ACK_EVENT_NOT_PENDING"):
            await broadcaster.acknowledge(other, EVENT_ID, nonce)
        with self.assertRaisesRegex(ValueError, "ACK_EVENT_NOT_PENDING"):
            await broadcaster.acknowledge(
                socket,
                EVENT_ID,
                nonce,
                UUID("91000000-0000-0000-0000-000000000021"),
            )
        await broadcaster.acknowledge(
            socket,
            EVENT_ID,
            nonce,
            application_id,
        )
        self.assertEqual(1, len(recorder.deliveries))

        socket.release.set()
        await publishing
        with self.assertRaisesRegex(ValueError, "ACK_EVENT_NOT_PENDING"):
            await broadcaster.acknowledge(other, EVENT_ID, nonce, application_id)
        with self.assertRaisesRegex(ValueError, "ACK_EVENT_NOT_PENDING"):
            await broadcaster.acknowledge(
                socket,
                EVENT_ID,
                nonce,
                application_id,
            )
        self.assertEqual(1, len(recorder.deliveries))


class EntityObservationWebSocketPublicApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(
            cls.password,
            salt=b"entity-ws-salt!",
        )

    def build_app(self):
        repository = InMemoryIdentityRepository(
            [
                UserIdentity(
                    UUID("91000000-0000-0000-0000-000000000010"),
                    "operator",
                    self.password_hash,
                    "operator",
                    "active",
                )
            ]
        )
        identity = Identity(repository)
        broadcaster = EntityObservationBroadcaster()
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(websocket_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity
        app.dependency_overrides[get_entity_observation_catalog] = _EntityCatalog
        app.dependency_overrides[
            get_entity_observation_broadcaster
        ] = lambda: broadcaster
        return app

    def test_ticket_authenticates_entity_subscription_and_logout_revokes_it(self) -> None:
        app = self.build_app()
        with TestClient(app, base_url="https://testserver") as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": self.password},
            )
            headers = {
                "Authorization": f"Bearer {login.json()['access_token']}"
            }
            ticket = client.post(
                "/api/v1/auth/ws-ticket",
                headers=headers,
            ).json()["ticket"]

            with client.websocket_connect(
                "wss://testserver/api/v1/ws/entity-observations"
            ) as websocket:
                websocket.send_json({"authenticate": {"ticket": ticket}})
                self.assertEqual(
                    websocket.receive_json(),
                    {"type": "authenticated"},
                )
                websocket.send_json({"subscribe": [str(ENTITY_ID)]})
                self.assertEqual(
                    websocket.receive_json(),
                    {
                        "type": "subscribed",
                        "entity_instance_ids": [str(ENTITY_ID)],
                    },
                )
                logout = client.post("/api/v1/auth/logout", headers=headers)
                self.assertEqual(logout.status_code, 204, logout.text)
                websocket.send_json({"subscribe": [str(ENTITY_ID)]})
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(closed.exception.code, 4401)

    @unittest.skip("machine acceptance removed by L0/L1/L2 hard cut")
    def test_acceptance_subscription_is_bound_to_authenticated_principal(self) -> None:
        app = self.build_app()
        application_id = UUID("91000000-0000-0000-0000-000000000020")
        with TestClient(app, base_url="https://testserver") as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": self.password},
            )
            ticket = client.post(
                "/api/v1/auth/ws-ticket",
                headers={
                    "Authorization": f"Bearer {login.json()['access_token']}"
                },
            ).json()["ticket"]
            with client.websocket_connect(
                "wss://testserver/api/v1/ws/entity-observations"
            ) as websocket:
                websocket.send_json({"authenticate": {"ticket": ticket}})
                self.assertEqual({"type": "authenticated"}, websocket.receive_json())
                websocket.send_json({
                    "subscribe": [str(ENTITY_ID)],
                    "acceptance_application_id": str(application_id),
                })
                self.assertEqual("subscribed", websocket.receive_json()["type"])

        [(bound_application, bound_entities, principal, _)] = (
            app.state.acceptance_evidence.bindings
        )
        self.assertEqual(application_id, bound_application)
        self.assertEqual((ENTITY_ID,), bound_entities)
        self.assertEqual("operator", principal.username)
        self.assertEqual("operator", principal.role)

    @unittest.skip("machine acceptance removed by L0/L1/L2 hard cut")
    def test_acceptance_ack_is_accepted_only_after_server_sent_event(self) -> None:
        app = self.build_app()
        application_id = UUID("91000000-0000-0000-0000-000000000020")
        broadcaster = app.dependency_overrides[
            get_entity_observation_broadcaster
        ]()
        with TestClient(app, base_url="https://testserver") as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": self.password},
            )
            ticket = client.post(
                "/api/v1/auth/ws-ticket",
                headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            ).json()["ticket"]
            with client.websocket_connect(
                "wss://testserver/api/v1/ws/entity-observations"
            ) as websocket:
                websocket.send_json({"authenticate": {"ticket": ticket}})
                websocket.receive_json()
                websocket.send_json({
                    "subscribe": [str(ENTITY_ID)],
                    "acceptance_application_id": str(application_id),
                })
                websocket.receive_json()
                event = OutboxEvent(EVENT_ID, ENTITY_ID, {
                    "processing_revision_id": "revision",
                    "site_configuration_version": 1,
                })
                asyncio.run(broadcaster.publish(event))
                frame = websocket.receive_json()
                self.assertEqual("entity_observation", frame["type"])
                websocket.send_json({
                    "acknowledge_acceptance_event": str(EVENT_ID),
                    "acceptance_ack_nonce": frame["acceptance_ack_nonce"],
                    "acceptance_application_id": str(application_id),
                })
                self.assertEqual("acknowledged", websocket.receive_json()["type"])


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL outbox tests",
)
class PostgresEntityObservationOutboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg2

        cls.psycopg2 = psycopg2
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Outbox tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        from app.services.telemetry_store import close_db_pool, init_db_pool
        from tests.test_data_trunk_migration_postgres import (
            DataTrunkMigrationPostgresTest,
        )

        close_db_pool()
        with self.psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                DataTrunkMigrationPostgresTest._reset_through_037(cursor)
                DataTrunkMigrationPostgresTest._apply_038(cursor)
                DataTrunkMigrationPostgresTest._apply_039(cursor)
                DataTrunkMigrationPostgresTest._apply_040(cursor)
                DataTrunkMigrationPostgresTest._apply_041(cursor)
        init_db_pool(min_conn=1, max_conn=4)

    def tearDown(self) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()

    def test_claim_ack_and_redelivery_keep_one_safe_committed_event(self) -> None:
        from app.services.data_trunk_contracts import (
            RawObservation,
            TrunkQuality,
            TypedValue,
        )
        from app.services.data_trunk_outbox import PostgresOutboxRepository
        from app.services.data_trunk_postgres import build_postgres_data_trunk
        from tests.test_point_processing_postgres import (
            PointProcessingPostgresTest,
        )

        helper = PointProcessingPostgresTest(
            "test_solution_install_creates_entities_and_conversion_in_one_transaction"
        )
        helper.connection_kwargs = self.connection_kwargs
        delivery, plan, node_id = helper._plan_reference_solution()
        delivery.apply_install(
            plan.id,
            plan.digest,
            "outbox-solution-install",
            "user:engineer-install",
        )
        with self.psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM t_tags
                    WHERE node_id = %s AND name = 'ActivePowerRaw'
                    """,
                    (node_id,),
                )
                tag_id = cursor.fetchone()[0]
        observed_at = datetime.now(UTC) - timedelta(seconds=1)
        receipt = build_postgres_data_trunk().ingest(
            (
                RawObservation(
                    observation_id=UUID(
                        "91000000-0000-0000-0000-000000000100"
                    ),
                    node_id=node_id,
                    tag_id=tag_id,
                    source_key="ActivePowerRaw",
                    value=TypedValue.float(12_345.0),
                    raw_unit="W",
                    quality=TrunkQuality.GOOD,
                    source_timestamp=observed_at,
                    received_at=observed_at + timedelta(milliseconds=100),
                    source_message_id="safe-source-digest",
                    source_sequence=1,
                    source_digest="c" * 64,
                    event_time_basis="observed_at",
                ),
            )
        )
        self.assertEqual(len(receipt.l2_event_ids), 1)
        repository = PostgresOutboxRepository(
            worker_id=UUID("91000000-0000-0000-0000-000000000200")
        )

        claimed = repository.claim_unpublished(10)
        self.assertEqual([item.event_id for item in claimed], list(receipt.l2_event_ids))
        self.assertEqual(claimed[0].payload["data_type"], "FLOAT")
        self.assertNotIn("topic", repr(claimed).lower())
        self.assertNotIn("token", repr(claimed).lower())
        repository.mark_published(claimed[0].event_id)
        self.assertEqual(repository.claim_unpublished(10), ())

        with self.psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE t_l2_stream_outbox
                    SET published_at = NULL, next_attempt_at = now(),
                        claimed_by = NULL, claimed_until = NULL
                    WHERE event_id = %s
                    """,
                    (claimed[0].event_id,),
                )
        redelivered = repository.claim_unpublished(10)
        self.assertEqual(redelivered[0].event_id, claimed[0].event_id)
        repository.record_attempt(redelivered[0].event_id)
        with self.psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT attempts, claimed_by IS NULL, published_at IS NULL
                    FROM t_l2_stream_outbox WHERE event_id = %s
                    """,
                    (claimed[0].event_id,),
                )
                self.assertEqual(cursor.fetchone(), (1, True, True))


if __name__ == "__main__":
    unittest.main()
