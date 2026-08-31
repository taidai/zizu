from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

from app.services.data_trunk_contracts import ClaimedFrame, TrunkQuality
from app.services.data_trunk_postgres import PostgresFrameRepository


class _RowsCursor:
    def __init__(self, *result_sets: list[tuple[object, ...]]) -> None:
        self._result_sets = list(result_sets)
        self._rows: list[tuple[object, ...]] = []

    def execute(self, _query: str, _parameters: object) -> None:
        self._rows = self._result_sets.pop(0)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class FailureStaleOutputsTest(unittest.TestCase):
    def test_empty_bad_latest_is_not_mistaken_for_a_typed_baseline(self) -> None:
        now = datetime.now(UTC)
        entity_id = uuid4()
        claimed = ClaimedFrame(
            frame_id=uuid4(),
            frame_sequence=42,
            capture_beat=42,
            shot_at=now,
            configuration_revision=7,
            attempt_count=3,
            processing_owner=uuid4(),
            processing_token=uuid4(),
            lease_until=now + timedelta(seconds=30),
            created_at=now - timedelta(seconds=60),
        )
        cursor = _RowsCursor(
            [
                (
                    entity_id,
                    "pcs.active_power",
                    "FLOAT",
                    "kW",
                    uuid4(),
                    uuid4(),
                    now,
                    now,
                    now,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "a" * 64,
                    int(TrunkQuality.BAD),
                    "REQUIRED_INPUT_MISSING",
                )
            ],
            [],
        )

        outputs = PostgresFrameRepository._failure_stale_outputs(
            cursor, claimed, frozenset({entity_id}), now
        )

        self.assertEqual(1, len(outputs))
        self.assertIsNone(outputs[0].value.value)
        self.assertEqual(TrunkQuality.STALE, outputs[0].quality)
        self.assertEqual("FRAME_PROCESSING_FAILED_NO_BASELINE", outputs[0].reason)

    def test_previous_typed_value_is_preserved_when_latest_state_is_empty(self) -> None:
        now = datetime.now(UTC)
        entity_id = uuid4()
        baseline_event_id = uuid4()
        claimed = ClaimedFrame(
            frame_id=uuid4(), frame_sequence=43, capture_beat=43, shot_at=now,
            configuration_revision=7, attempt_count=3,
            processing_owner=uuid4(), processing_token=uuid4(),
            lease_until=now + timedelta(seconds=30),
            created_at=now - timedelta(seconds=60),
        )
        cursor = _RowsCursor(
            [
                (
                    entity_id, "pcs.active_power", "FLOAT", "kW", uuid4(),
                    uuid4(), now, now, now,
                    None, None, None, None, None, None, "a" * 64,
                    int(TrunkQuality.BAD), "REQUIRED_INPUT_MISSING",
                )
            ],
            [
                (
                    entity_id, baseline_event_id,
                    12.5, None, None, None, None, None, "b" * 64,
                )
            ],
        )

        outputs = PostgresFrameRepository._failure_stale_outputs(
            cursor, claimed, frozenset({entity_id}), now
        )

        self.assertEqual(12.5, outputs[0].value.value)
        self.assertEqual("FRAME_PROCESSING_FAILED", outputs[0].reason)
        self.assertEqual((baseline_event_id,), outputs[0].source_observation_ids)


if __name__ == "__main__":
    unittest.main()
