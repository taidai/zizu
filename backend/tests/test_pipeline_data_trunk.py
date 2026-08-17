from __future__ import annotations

import asyncio
import inspect
import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import patch

from app.models.schemas import ParsedMessage
from app.services.data_trunk import RawObservationAdapter, TagMetadata
from app.services.data_trunk_contracts import (
    CommitReceipt,
    DataTrunkError,
    RawObservation,
    TrunkQuality,
    TypedValue,
)
from app.services.normalizer import TagNormalizationRule
from app.services.pipeline import DataPipeline


NODE_ID = UUID("41000000-0000-0000-0000-000000000001")
TAG_ID = UUID("41000000-0000-0000-0000-000000000002")


def _observation(sequence: int) -> RawObservation:
    observed_at = datetime(2026, 8, 17, 2, 0, sequence, tzinfo=UTC)
    return RawObservation(
        observation_id=UUID(f"41000000-0000-0000-0000-{sequence:012d}"),
        node_id=NODE_ID,
        tag_id=TAG_ID,
        source_key="pcs-01/active-power",
        value=TypedValue.float(float(sequence * 1000)),
        raw_unit="W",
        quality=TrunkQuality.GOOD,
        source_timestamp=observed_at,
        received_at=observed_at,
        source_message_id=f"message-{sequence}",
        source_sequence=sequence,
        source_digest=f"{sequence:064x}",
    )


def _receipt(batch: tuple[RawObservation, ...]) -> CommitReceipt:
    return CommitReceipt(
        transaction_id=UUID("41000000-0000-0000-0000-000000000099"),
        accepted_l0_count=len(batch),
        duplicate_l0_count=0,
        l2_event_ids=(),
        late_observation_count=0,
    )


class _FailOnceDataTrunk:
    def __init__(self) -> None:
        self.calls: list[tuple[RawObservation, ...]] = []

    def ingest(self, batch: tuple[RawObservation, ...]) -> CommitReceipt:
        self.calls.append(tuple(batch))
        if len(self.calls) == 1:
            raise DataTrunkError("DATA_TRUNK_UNAVAILABLE", "temporary failure")
        return _receipt(tuple(batch))


class _BlockingDataTrunk:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def ingest(self, batch: tuple[RawObservation, ...]) -> CommitReceipt:
        loop = asyncio.run_coroutine_threadsafe(self._block(), self._loop)
        loop.result(timeout=5)
        return _receipt(tuple(batch))

    async def _block(self) -> None:
        self.started.set()
        await self.release.wait()


class _AlwaysFailDataTrunk:
    def __init__(self, *, failure_record_fails_once: bool = False) -> None:
        self.ingest_calls = 0
        self.failure_calls = 0
        self.failure_record_fails_once = failure_record_fails_once

    def ingest(self, batch: tuple[RawObservation, ...]) -> CommitReceipt:
        self.ingest_calls += 1
        raise DataTrunkError("DATA_TRUNK_UNAVAILABLE", "temporary failure")

    def record_failure(
        self,
        batch: tuple[RawObservation, ...],
        *,
        attempts: int,
        error_code: str,
    ) -> UUID:
        self.failure_calls += 1
        if self.failure_record_fails_once and self.failure_calls == 1:
            raise DataTrunkError("DATA_TRUNK_UNAVAILABLE", "failure ledger unavailable")
        self.recorded = (tuple(batch), attempts, error_code)
        return UUID("41000000-0000-0000-0000-000000000098")


class _DuplicateDataTrunk:
    def ingest(self, batch: tuple[RawObservation, ...]) -> CommitReceipt:
        return CommitReceipt(
            transaction_id=UUID("41000000-0000-0000-0000-000000000097"),
            accepted_l0_count=0,
            duplicate_l0_count=len(batch),
            l2_event_ids=(),
            late_observation_count=0,
            accepted_l0_observation_ids=(),
        )


class PipelineDataTrunkTest(unittest.IsolatedAsyncioTestCase):
    def test_raw_adapter_is_deterministic_and_keeps_the_unscaled_value(self) -> None:
        parsed = ParsedMessage(
            node_name="PCS-A",
            group="read",
            timestamp_ms=1786932000000,
            tags={"activePower": 12345, "unsupported": [1, 2]},
        )
        adapter = RawObservationAdapter()
        metadata = {
            "activePower": TagMetadata(
                node_id=NODE_ID,
                tag_id=TAG_ID,
                stable_source_key="pcs-01/read/activePower",
                data_type="FLOAT",
                unit="W",
            )
        }

        first = adapter.from_parsed(
            parsed,
            metadata,
            received_at=datetime(2026, 8, 17, 2, 0, tzinfo=UTC),
            source_message_id="safe-message-digest",
            source_sequence=7,
        )
        second = adapter.from_parsed(
            parsed,
            metadata,
            received_at=datetime(2026, 8, 17, 2, 1, tzinfo=UTC),
            source_message_id="safe-message-digest",
            source_sequence=7,
        )

        self.assertEqual(1, len(first))
        self.assertEqual(TypedValue.float(12345.0), first[0].value)
        self.assertEqual("W", first[0].raw_unit)
        self.assertEqual(first[0].observation_id, second[0].observation_id)
        self.assertEqual(first[0].source_digest, second[0].source_digest)

    async def test_on_message_buffers_canonical_raw_not_normalized_telemetry(self) -> None:
        trunk = _FailOnceDataTrunk()
        pipeline = DataPipeline(data_trunk=trunk)
        rule = TagNormalizationRule(
            tag_name="activePower",
            data_type="FLOAT",
            scale_factor=0.001,
            unit_from=None,
            unit_to="kW",
        )
        pipeline._rules = {"activePower": rule}
        pipeline._neuron_tag_map = {
            ("PCS-A", "read", "activePower"): (NODE_ID, TAG_ID, rule)
        }

        await pipeline.on_message(
            SimpleNamespace(
                topic="neuron/PCS-A/telemetry",
                qos=1,
                sequence=9,
                payload=json.dumps(
                    {
                        "node": "PCS-A",
                        "group": "read",
                        "timestamp": 1786932000000,
                        "tags": {"activePower": 12345},
                    }
                ).encode("utf-8"),
            )
        )

        self.assertEqual((9,), pipeline.buffer_sequences())
        self.assertEqual(TypedValue.float(12345.0), pipeline._buffer[0].value)
        projection = pipeline._legacy_projections[pipeline._buffer[0].observation_id]
        self.assertEqual(12.345, projection.value_float)

    async def test_failed_ingest_keeps_exact_buffer_prefix_for_retry(self) -> None:
        trunk = _FailOnceDataTrunk()
        pipeline = DataPipeline(data_trunk=trunk)
        pipeline._buffer.append(_observation(1))

        await pipeline.flush_now()
        self.assertEqual((_observation(1).observation_id,), pipeline.buffer_observation_ids())

        await pipeline.flush_now()
        self.assertEqual((), pipeline.buffer_observation_ids())
        self.assertEqual(trunk.calls[0], trunk.calls[1])

    async def test_concurrent_append_is_not_removed_with_committed_prefix(self) -> None:
        trunk = _BlockingDataTrunk()
        trunk._loop = asyncio.get_running_loop()
        pipeline = DataPipeline(data_trunk=trunk)
        pipeline._buffer.append(_observation(1))

        flush = asyncio.create_task(pipeline.flush_now())
        await trunk.started.wait()
        async with pipeline._buffer_lock:
            pipeline._buffer.append(_observation(2))
        trunk.release.set()
        await flush

        self.assertEqual((2,), pipeline.buffer_sequences())

    async def test_fifth_failure_is_removed_only_after_safe_failure_record(self) -> None:
        trunk = _AlwaysFailDataTrunk(failure_record_fails_once=True)
        pipeline = DataPipeline(data_trunk=trunk)
        pipeline._buffer.append(_observation(1))

        with patch("app.services.pipeline.INGEST_RETRY_DELAYS", (0, 0, 0, 0, 0)):
            for _ in range(5):
                await pipeline.flush_now()

            self.assertEqual((_observation(1).observation_id,), pipeline.buffer_observation_ids())
            self.assertEqual(5, trunk.ingest_calls)
            self.assertEqual(1, trunk.failure_calls)

            await pipeline.flush_now()

        self.assertEqual(5, trunk.ingest_calls)
        self.assertEqual(2, trunk.failure_calls)
        self.assertEqual((), pipeline.buffer_observation_ids())
        self.assertEqual(5, trunk.recorded[1])
        self.assertEqual("DATA_TRUNK_UNAVAILABLE", trunk.recorded[2])

    async def test_lost_receipt_retry_does_not_resubmit_duplicate_legacy_alarm(self) -> None:
        pipeline = DataPipeline(data_trunk=_DuplicateDataTrunk())
        pipeline._buffer.append(_observation(1))
        alarm_batches: list[object] = []
        pipeline._submit_unified_tag_alarms = alarm_batches.append
        pipeline._submit_installed_entity_alarms = alarm_batches.append

        await pipeline.flush_now()

        self.assertEqual((), pipeline.buffer_observation_ids())
        self.assertEqual([], alarm_batches)

    def test_pipeline_has_one_business_write_seam(self) -> None:
        source = inspect.getsource(DataPipeline._do_flush)
        self.assertNotIn("batch_insert_telemetry", source)
        self.assertNotIn("upsert_telemetry_latest", source)
        self.assertEqual(1, source.count("self._data_trunk.ingest"))


if __name__ == "__main__":
    unittest.main()
