from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")

from app.services.data_trunk import DataTrunk, TagMetadata
from app.services.data_trunk_contracts import (
    FrameStatus,
    PendingFrame,
    SourceOrderMode,
    TrunkQuality,
)
from app.services.pipeline import DataPipeline
from app.services.realtime_blackboard import RealtimeBlackboard


NODE_ID = UUID("89000000-0000-0000-0000-000000000001")
TAG_ID = UUID("89000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)


class _CaptureRepository:
    """Replace only transaction A storage; keep parsing and capture behavior real."""

    def __init__(self) -> None:
        self.candidates = []
        self.unfinished_count = 0

    def current_configuration_revision(self) -> int:
        return 7

    def unfinished_frame_count(self) -> int:
        return self.unfinished_count

    def commit_pending(self, candidate):
        self.candidates.append(candidate)
        return PendingFrame(
            frame_id=candidate.frame_id,
            frame_sequence=len(self.candidates),
            capture_beat=candidate.capture_beat,
            shot_at=candidate.shot_at,
            configuration_revision=7,
            status=FrameStatus.PENDING,
        )


def _pipeline(*, timestamp_trusted: bool):
    repository = _CaptureRepository()
    trunk = DataTrunk(
        repository,
        blackboard=RealtimeBlackboard(
            active_input_contracts={
                TAG_ID: (
                    SourceOrderMode.OBSERVED_AT
                    if timestamp_trusted
                    else SourceOrderMode.RECEIVED_AT
                )
            },
            required_tag_ids=frozenset({TAG_ID}),
        ),
        processor=None,
    )
    pipeline = DataPipeline(data_trunk=trunk)
    pipeline._raw_neuron_tag_map = {
        ("PCS", "read", "timeout"): (TagMetadata(
            node_id=NODE_ID,
            tag_id=TAG_ID,
            stable_source_key="PCS/read/timeout",
            data_type="INT",
            wire_data_type="INT16",
            unit="s",
            timestamp_trusted=timestamp_trusted,
        ),)
    }
    return pipeline, trunk, repository


def _message(second: int, *, value: int = 100):
    return SimpleNamespace(
        topic="neuron/PCS/telemetry",
        qos=1,
        payload=json.dumps(
            {
                "node": "PCS",
                "group": "read",
                "timestamp": int(NOW.timestamp() * 1000) + second * 1000,
                "tags": {"timeout": value},
            }
        ).encode(),
    )


class PipelineSameValueFreshnessTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_value_new_timestamp_reaches_each_capture_with_new_evidence(self):
        # Catches value-only dedup in the parser, adapter, or blackboard.
        for trusted in (True, False):
            with self.subTest(timestamp_trusted=trusted):
                pipeline, trunk, repository = _pipeline(timestamp_trusted=trusted)
                for second in range(6):
                    await pipeline.on_message(_message(second))
                    frame = trunk.capture_tick(NOW + timedelta(seconds=second))
                    self.assertIsNotNone(frame)
                    candidate = repository.candidates[-1]
                    self.assertEqual(second + 1, candidate.capture_beat)
                    self.assertEqual(1, len(candidate.changed_l0))
                    cell = candidate.changed_l0[0]
                    self.assertEqual(100, cell.observation.value.value)
                    self.assertEqual("s", cell.observation.raw_unit)
                    self.assertEqual(
                        NOW + timedelta(seconds=second),
                        cell.observation.source_timestamp,
                    )
                    self.assertEqual(second + 1, cell.accepted_beat)
                    self.assertEqual(TrunkQuality.GOOD, cell.effective_quality)

                self.assertEqual(6, len(repository.candidates))
                self.assertEqual(
                    6,
                    len({
                        item.cells[TAG_ID].observation.observation_id
                        for item in repository.candidates
                    }),
                )

    async def test_busy_frame_retains_latest_same_value_sample_without_queueing_old_photos(self):
        # Catches dropping same-valued arrivals while a previous frame is pending.
        pipeline, trunk, repository = _pipeline(timestamp_trusted=True)
        repository.unfinished_count = 1
        for second in range(6):
            await pipeline.on_message(_message(second))
            self.assertIsNone(trunk.capture_tick(NOW + timedelta(seconds=second)))

        repository.unfinished_count = 0
        self.assertIsNotNone(trunk.capture_tick(NOW + timedelta(seconds=6)))
        self.assertEqual(1, len(repository.candidates))
        cell = repository.candidates[0].changed_l0[0]
        self.assertEqual(100, cell.observation.value.value)
        self.assertEqual(NOW + timedelta(seconds=5), cell.observation.source_timestamp)
        self.assertEqual(6, cell.accepted_beat)
        self.assertEqual(TrunkQuality.GOOD, cell.effective_quality)

    async def test_late_trusted_sample_does_not_replace_a_newer_sample(self):
        # Catches weakening source-order checks while preserving unchanged values.
        pipeline, trunk, repository = _pipeline(timestamp_trusted=True)
        await pipeline.on_message(_message(5))
        trunk.capture_tick(NOW + timedelta(seconds=5))
        await pipeline.on_message(_message(4, value=999))

        self.assertIsNone(trunk.capture_tick(NOW + timedelta(seconds=6)))
        self.assertEqual(1, len(repository.candidates))
        self.assertEqual(100, repository.candidates[0].cells[TAG_ID].observation.value.value)


if __name__ == "__main__":
    unittest.main()
