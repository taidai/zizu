from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

from app.services.data_trunk_contracts import (
    RawObservation,
    TrunkQuality,
    TypedValue,
)
from app.services.data_trunk_postgres import PostgresDataTrunkRepository


NODE_ID = UUID("51000000-0000-0000-0000-000000000001")
TAG_ID = UUID("51000000-0000-0000-0000-000000000002")


def _observation(sequence: int) -> RawObservation:
    observed_at = datetime(2026, 8, 26, 3, 0, sequence, tzinfo=UTC)
    return RawObservation(
        observation_id=UUID(f"51000000-0000-0000-0000-{sequence:012d}"),
        node_id=NODE_ID,
        tag_id=TAG_ID,
        source_key="PCS-01/data/交流总有功功率",
        value=TypedValue.float(float(sequence)),
        raw_unit="kW",
        quality=TrunkQuality.GOOD,
        source_timestamp=observed_at,
        received_at=observed_at,
        source_message_id=f"message-{sequence}",
        source_sequence=sequence,
        source_digest=f"{sequence:064x}",
        event_time_basis="received_at",
    )


class _RejectPerRowCursor:
    def execute(self, *_args, **_kwargs) -> None:
        raise AssertionError("L0 batches must not use one database call per observation")


class _BoundedLatestCursor:
    def __init__(self) -> None:
        self.execute_count = 0
        self.statements: list[str] = []

    def execute(self, statement: str, *_args, **_kwargs) -> None:
        self.execute_count += 1
        self.statements.append(statement)
        if self.execute_count > 2:
            raise AssertionError("L0 latest must use a bounded number of database calls")

    def fetchone(self):
        return ("accepted",)

    def fetchall(self):
        return []


class DataTrunkBulkWriteTest(unittest.TestCase):
    def test_l0_history_conflict_returns_only_rows_inserted_into_history(self) -> None:
        observations = (_observation(1), _observation(2))
        captured: dict[str, str] = {}

        def execute_values_contract(_cursor, statement, _rows, **_kwargs):
            captured["sql"] = statement
            return [(str(observations[1].observation_id),)]

        with patch(
            "app.services.data_trunk_postgres.execute_values",
            side_effect=execute_values_contract,
        ):
            accepted = PostgresDataTrunkRepository._insert_l0(
                _RejectPerRowCursor(), observations
            )

        normalized = " ".join(captured["sql"].split())
        self.assertIn(
            "ON CONFLICT (tag_id, ts, source_digest) "
            "WHERE source_digest IS NOT NULL DO NOTHING",
            normalized,
        )
        self.assertEqual((observations[1],), accepted)

    def test_l0_history_batch_is_written_in_one_database_call(self) -> None:
        observations = (_observation(1), _observation(2))
        returned_rows = [(str(item.observation_id),) for item in observations]

        with patch(
            "app.services.data_trunk_postgres.execute_values",
            return_value=returned_rows,
            create=True,
        ):
            accepted = PostgresDataTrunkRepository._insert_l0(
                _RejectPerRowCursor(), observations
            )

        self.assertEqual(observations, accepted)

    def test_l0_latest_batch_keeps_advanced_and_late_results(self) -> None:
        observations = (_observation(1), _observation(2))

        with patch(
            "app.services.data_trunk_postgres.execute_values",
            return_value=[(str(observations[1].observation_id),)],
        ):
            advanced, late = PostgresDataTrunkRepository._advance_l0_latest(
                _BoundedLatestCursor(), observations
            )

        self.assertEqual(observations, advanced)
        self.assertEqual(0, late)

    def test_l0_latest_conflict_counts_the_tag_batch_as_late(self) -> None:
        observations = (_observation(1), _observation(2))

        with patch(
            "app.services.data_trunk_postgres.execute_values",
            return_value=[],
        ):
            advanced, late = PostgresDataTrunkRepository._advance_l0_latest(
                _BoundedLatestCursor(), observations
            )

        self.assertEqual((), advanced)
        self.assertEqual(2, late)

    def test_l0_latest_serializes_each_tag_before_reading_latest(self) -> None:
        observations = (_observation(1), _observation(2))
        cursor = _BoundedLatestCursor()

        with patch(
            "app.services.data_trunk_postgres.execute_values",
            return_value=[(str(observations[1].observation_id),)],
        ):
            PostgresDataTrunkRepository._advance_l0_latest(cursor, observations)

        self.assertIn("pg_advisory_xact_lock", cursor.statements[0])
        self.assertIn("FROM t_telemetry_latest", cursor.statements[1])


if __name__ == "__main__":
    unittest.main()
