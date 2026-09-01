from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from types import MappingProxyType
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")

from app.api.committed_frames import get_committed_frame_stream, router
from app.api.security import get_identity
from app.core.config import settings
from app.services.committed_frame_stream import FrameDelta, FrameSnapshot


NODE_ID = uuid4()
NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


class _Identity:
    def authorize(self, principal, capability, **_kwargs):
        if capability != "runtime.read":
            raise AssertionError(capability)
        return principal

    def revalidate_session(self, principal, **_kwargs):
        return principal


class _Subscription:
    def __init__(self, delta: FrameDelta) -> None:
        self.delta = delta
        self.delivered = False

    async def receive(self) -> FrameDelta:
        if not self.delivered:
            self.delivered = True
            return self.delta
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _Stream:
    def __init__(self) -> None:
        self.snapshot = FrameSnapshot(
            node_id=NODE_ID,
            cursor="cursor-10",
            frame_sequence=10,
            frame_time=NOW.isoformat(),
            configuration_revision=46,
            l0=(MappingProxyType({"tag_id": str(uuid4()), "value": 1.0}),),
            l2=(),
        )
        self.delta = FrameDelta(
            node_id=NODE_ID,
            cursor="cursor-11",
            frame_id=uuid4(),
            frame_sequence=11,
            status="COMPLETE",
            frame_time=NOW.isoformat(),
            configuration_revision=46,
            l0_changes=(),
            l2_changes=(),
            failure=None,
        )
        self.subscriptions = []
        self.unsubscribed = []

    def read_snapshot(self, scope):
        self.last_scope = scope
        return self.snapshot

    async def subscribe_after(self, scope, cursor):
        self.last_subscription = (scope, cursor)
        subscription = _Subscription(self.delta)
        self.subscriptions.append(subscription)
        return subscription

    async def unsubscribe(self, subscription):
        self.unsubscribed.append(subscription)


class CommittedFramePublicApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = _Stream()
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_committed_frame_stream] = lambda: self.stream
        app.dependency_overrides[get_identity] = lambda: _Identity()
        self.client = TestClient(app)
        self.settings_patch = patch.multiple(
            settings,
            deployment_mode="development",
            allow_insecure_anonymous_access=True,
            auth_require_https=False,
        )
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.client.close)

    def test_snapshot_requires_runtime_read_and_returns_cursor(self) -> None:
        response = self.client.get(
            "/api/v1/runtime/frame-snapshot",
            params={"node_id": str(NODE_ID)},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("frame_snapshot", response.json()["type"])
        self.assertEqual("cursor-10", response.json()["cursor"])
        self.assertIsNone(response.json()["frame_status"])
        self.assertIsNone(response.json()["failure"])
        self.assertEqual(0, response.json()["backlog_frames"])
        self.assertEqual(NODE_ID, self.stream.last_scope.node_id)

    def test_websocket_replays_after_authenticated_cursor(self) -> None:
        with self.client.websocket_connect("/api/v1/ws/data-frames") as socket:
            socket.send_json({"authenticate": {"ticket": "insecure-development"}})
            self.assertEqual({"type": "authenticated"}, socket.receive_json())
            socket.send_json(
                {
                    "subscribe": {
                        "node_id": str(NODE_ID),
                        "after": "cursor-10",
                    }
                }
            )
            self.assertEqual("subscribed", socket.receive_json()["type"])
            self.assertEqual(11, socket.receive_json()["frame_sequence"])
        self.assertEqual("cursor-10", self.stream.last_subscription[1])

    def test_websocket_rejects_missing_ticket(self) -> None:
        with self.client.websocket_connect("/api/v1/ws/data-frames") as socket:
            socket.send_json({"subscribe": {}})
            with self.assertRaises(WebSocketDisconnect) as closed:
                socket.receive_json()
        self.assertEqual(4401, closed.exception.code)


if __name__ == "__main__":
    unittest.main()
