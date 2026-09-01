"""Committed terminal-frame snapshots and ordered at-least-once delivery."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID


class FrameStreamError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FrameScope:
    node_id: UUID

    @classmethod
    def for_node(cls, node_id: UUID) -> "FrameScope":
        return cls(node_id=node_id)

    @property
    def digest(self) -> str:
        return hashlib.sha256(f"node:{self.node_id}".encode("ascii")).hexdigest()


class FrameCursorCodec:
    VERSION = 1

    def encode(self, sequence: int, scope: FrameScope) -> str:
        if sequence < 0:
            raise FrameStreamError("FRAME_CURSOR_INVALID")
        payload = json.dumps(
            {"scope": scope.digest, "s": sequence, "v": self.VERSION},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    def decode(self, value: str, scope: FrameScope) -> int:
        try:
            padded = value + "=" * (-len(value) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("ascii"))
            if not isinstance(payload, dict) or payload.get("v") != self.VERSION:
                raise ValueError
            sequence = payload.get("s")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                raise ValueError
            encoded_scope = payload.get("scope")
            if encoded_scope != scope.digest:
                raise FrameStreamError("FRAME_CURSOR_SCOPE_MISMATCH")
            return sequence
        except FrameStreamError:
            raise
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FrameStreamError("FRAME_CURSOR_INVALID") from exc


@dataclass(frozen=True)
class FrameSnapshot:
    node_id: UUID
    cursor: str
    frame_sequence: int
    frame_time: str | None
    configuration_revision: int
    l0: tuple[Mapping[str, Any], ...]
    l2: tuple[Mapping[str, Any], ...]
    frame_status: str | None = None
    failure: Mapping[str, Any] | None = None
    backlog_frames: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "type": "frame_snapshot",
            "node_id": str(self.node_id),
            "cursor": self.cursor,
            "frame_sequence": self.frame_sequence,
            "frame_time": self.frame_time,
            "configuration_revision": self.configuration_revision,
            "frame_status": self.frame_status,
            "failure": None if self.failure is None else dict(self.failure),
            "backlog_frames": self.backlog_frames,
            "l0": [dict(item) for item in self.l0],
            "l2": [dict(item) for item in self.l2],
        }


@dataclass(frozen=True)
class FrameDelta:
    node_id: UUID
    cursor: str
    frame_id: UUID
    frame_sequence: int
    status: str
    frame_time: str
    configuration_revision: int
    l0_changes: tuple[Mapping[str, Any], ...]
    l2_changes: tuple[Mapping[str, Any], ...]
    failure: Mapping[str, Any] | None

    def public_dict(self) -> dict[str, Any]:
        return {
            "type": "frame_delta",
            "cursor": self.cursor,
            "frame_id": str(self.frame_id),
            "frame_sequence": self.frame_sequence,
            "status": self.status,
            "frame_time": self.frame_time,
            "configuration_revision": self.configuration_revision,
            "l0_changes": [dict(item) for item in self.l0_changes],
            "l2_changes": [dict(item) for item in self.l2_changes],
            "failure": None if self.failure is None else dict(self.failure),
        }


@dataclass(frozen=True)
class ReplayWindow:
    oldest_sequence: int | None
    latest_sequence: int


class FrameStreamRepository(Protocol):
    def read_snapshot(self, scope: FrameScope) -> FrameSnapshot: ...

    def replay_window(self) -> ReplayWindow: ...

    def replay_after(
        self, sequence: int, high_watermark: int, scope: FrameScope
    ) -> Sequence[FrameDelta]: ...

    def project_event(self, event: Any, scope: FrameScope) -> FrameDelta: ...


class FrameSubscription:
    def __init__(
        self,
        scope: FrameScope,
        replay: Sequence[FrameDelta],
        last_sequence: int,
    ) -> None:
        self.scope = scope
        self._replay = deque(replay)
        self._live: asyncio.Queue[FrameDelta] = asyncio.Queue(maxsize=64)
        self._last_sequence = max(
            [last_sequence, *(item.frame_sequence for item in replay)]
        )
        self._error: FrameStreamError | None = None

    def enqueue(self, delta: FrameDelta) -> bool:
        if self._error is not None or delta.frame_sequence <= self._last_sequence:
            return self._error is None
        try:
            self._live.put_nowait(delta)
        except asyncio.QueueFull:
            self._error = FrameStreamError("FRAME_CLIENT_TOO_SLOW")
            return False
        self._last_sequence = delta.frame_sequence
        return True

    def fail(self, error: FrameStreamError) -> None:
        self._error = error

    async def receive(self) -> FrameDelta:
        if self._error is not None:
            raise self._error
        if self._replay:
            return self._replay.popleft()
        item = await self._live.get()
        if self._error is not None:
            raise self._error
        return item


class CommittedFrameStream:
    """Hide cursor, replay and live delivery behind one small interface."""

    def __init__(
        self,
        repository: FrameStreamRepository,
        codec: FrameCursorCodec | None = None,
    ) -> None:
        self._repository = repository
        self._codec = codec or FrameCursorCodec()
        self._subscriptions: dict[int, FrameSubscription] = {}
        self._next_subscription_id = 1
        self._lock = asyncio.Lock()

    def read_snapshot(self, scope: FrameScope) -> FrameSnapshot:
        snapshot = self._repository.read_snapshot(scope)
        return replace(
            snapshot,
            cursor=self._codec.encode(snapshot.frame_sequence, scope),
        )

    async def subscribe_after(
        self, scope: FrameScope, cursor: str
    ) -> FrameSubscription:
        sequence = self._codec.decode(cursor, scope)
        async with self._lock:
            window = self._repository.replay_window()
            if sequence < window.latest_sequence and (
                window.oldest_sequence is None
                or sequence < window.oldest_sequence - 1
            ):
                raise FrameStreamError("FRAME_CURSOR_TOO_OLD")
            replay = tuple(
                replace(
                    item,
                    cursor=self._codec.encode(item.frame_sequence, scope),
                )
                for item in self._repository.replay_after(
                    sequence, window.latest_sequence, scope
                )
            )
            subscription = FrameSubscription(scope, replay, sequence)
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            self._subscriptions[subscription_id] = subscription
            return subscription

    async def publish(self, event: Any) -> None:
        async with self._lock:
            failed = []
            for subscription_id, subscription in self._subscriptions.items():
                delta = self._repository.project_event(event, subscription.scope)
                delta = replace(
                    delta,
                    cursor=self._codec.encode(
                        delta.frame_sequence, subscription.scope
                    ),
                )
                if not subscription.enqueue(delta):
                    failed.append(subscription_id)
            for subscription_id in failed:
                self._subscriptions.pop(subscription_id, None)

    async def unsubscribe(self, subscription: FrameSubscription) -> None:
        async with self._lock:
            for subscription_id, current in tuple(self._subscriptions.items()):
                if current is subscription:
                    self._subscriptions.pop(subscription_id, None)
                    break
