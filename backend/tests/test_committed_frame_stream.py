from __future__ import annotations

import unittest
from dataclasses import replace
from uuid import UUID, uuid4


NODE_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NODE_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class CommittedFrameStreamTest(unittest.IsolatedAsyncioTestCase):
    def _module(self):
        from app.services.committed_frame_stream import (
            CommittedFrameStream,
            FrameCursorCodec,
            FrameDelta,
            FrameScope,
            FrameSnapshot,
            ReplayWindow,
        )

        class Repository:
            def __init__(self) -> None:
                self.events: list[FrameDelta] = []
                self.snapshots = {
                    NODE_A: FrameSnapshot(
                        node_id=NODE_A,
                        cursor="",
                        frame_sequence=10,
                        frame_time="2026-08-27T10:00:00+00:00",
                        configuration_revision=46,
                        l0=(),
                        l2=(),
                    )
                }

            def read_snapshot(self, scope):
                return self.snapshots[scope.node_id]

            def replay_window(self):
                if not self.events:
                    return ReplayWindow(oldest_sequence=None, latest_sequence=10)
                return ReplayWindow(
                    oldest_sequence=self.events[0].frame_sequence,
                    latest_sequence=self.events[-1].frame_sequence,
                )

            def replay_after(self, sequence, high_watermark, scope):
                return tuple(
                    item
                    for item in self.events
                    if sequence < item.frame_sequence <= high_watermark
                    and item.node_id == scope.node_id
                )

            def project_event(self, event, scope):
                return event.get(scope.node_id) or replace(
                    next(iter(event.values())),
                    node_id=scope.node_id,
                    l0_changes=(),
                    l2_changes=(),
                )

        repository = Repository()
        codec = FrameCursorCodec()
        return CommittedFrameStream(repository, codec), repository, codec, FrameScope, FrameDelta

    @staticmethod
    def _delta(frame_delta, sequence: int, node_id: UUID = NODE_A):
        return frame_delta(
            node_id=node_id,
            cursor="",
            frame_id=uuid4(),
            frame_sequence=sequence,
            status="COMPLETE",
            frame_time=f"2026-08-27T10:00:{sequence:02d}+00:00",
            configuration_revision=46,
            l0_changes=({"tag_id": f"tag-{sequence}"},),
            l2_changes=({"entity_instance_id": f"entity-{sequence}"},),
            failure=None,
        )

    async def test_snapshot_cursor_is_bound_to_node_scope(self) -> None:
        from app.services.committed_frame_stream import FrameStreamError

        stream, _repository, codec, frame_scope, _frame_delta = self._module()
        snapshot = stream.read_snapshot(frame_scope.for_node(NODE_A))

        self.assertEqual(10, codec.decode(snapshot.cursor, frame_scope.for_node(NODE_A)))
        with self.assertRaisesRegex(FrameStreamError, "FRAME_CURSOR_SCOPE_MISMATCH"):
            codec.decode(snapshot.cursor, frame_scope.for_node(NODE_B))

    async def test_replay_then_live_is_ordered_and_deduplicated(self) -> None:
        stream, repository, codec, frame_scope, frame_delta = self._module()
        scope = frame_scope.for_node(NODE_A)
        repository.events = [
            self._delta(frame_delta, 11),
            self._delta(frame_delta, 12),
        ]
        subscription = await stream.subscribe_after(scope, codec.encode(10, scope))

        await stream.publish({NODE_A: self._delta(frame_delta, 12)})
        await stream.publish({NODE_A: self._delta(frame_delta, 13)})

        received = [
            await subscription.receive(),
            await subscription.receive(),
            await subscription.receive(),
        ]
        self.assertEqual([11, 12, 13], [item.frame_sequence for item in received])
        self.assertTrue(all(item.cursor for item in received))

    async def test_cursor_older_than_replay_horizon_requires_snapshot(self) -> None:
        from app.services.committed_frame_stream import FrameStreamError

        stream, repository, codec, frame_scope, frame_delta = self._module()
        scope = frame_scope.for_node(NODE_A)
        repository.events = [self._delta(frame_delta, 8), self._delta(frame_delta, 9)]

        with self.assertRaisesRegex(FrameStreamError, "FRAME_CURSOR_TOO_OLD"):
            await stream.subscribe_after(scope, codec.encode(3, scope))

    async def test_other_node_event_advances_cursor_as_empty_checkpoint(self) -> None:
        stream, repository, codec, frame_scope, frame_delta = self._module()
        scope = frame_scope.for_node(NODE_A)
        repository.events = []
        subscription = await stream.subscribe_after(scope, codec.encode(10, scope))

        await stream.publish({NODE_B: self._delta(frame_delta, 11, NODE_B)})

        checkpoint = await subscription.receive()
        self.assertEqual(11, checkpoint.frame_sequence)
        self.assertEqual((), checkpoint.l0_changes)
        self.assertEqual((), checkpoint.l2_changes)

    async def test_slow_client_is_failed_without_blocking_other_subscribers(self) -> None:
        from app.services.committed_frame_stream import FrameStreamError

        stream, repository, codec, frame_scope, frame_delta = self._module()
        scope = frame_scope.for_node(NODE_A)
        repository.events = []
        slow = await stream.subscribe_after(scope, codec.encode(10, scope))
        fast = await stream.subscribe_after(scope, codec.encode(10, scope))

        for sequence in range(11, 76):
            await stream.publish({NODE_A: self._delta(frame_delta, sequence)})
            if sequence < 75:
                await fast.receive()

        with self.assertRaisesRegex(FrameStreamError, "FRAME_CLIENT_TOO_SLOW"):
            await slow.receive()
        self.assertEqual(75, (await fast.receive()).frame_sequence)


if __name__ == "__main__":
    unittest.main()
