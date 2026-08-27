from __future__ import annotations

from datetime import UTC, datetime
import unittest
from uuid import UUID

from app.models.schemas import ParsedMessage
from app.services.data_trunk import RawObservationAdapter, TagMetadata
from app.services.data_trunk_contracts import ValueKind


NODE_ID = UUID("52000000-0000-0000-0000-000000000001")
TAG_ID = UUID("52000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 27, 13, 30, tzinfo=UTC)


class RawObservationAdapterTest(unittest.TestCase):
    def _convert(self, raw_value: object):
        parsed = ParsedMessage(
            node_name="pcs",
            timestamp_ms=int(NOW.timestamp() * 1000),
            event_time_basis="received_at",
            tags={"ActivePower": raw_value},
        )
        return RawObservationAdapter().from_parsed(
            parsed,
            {
                "ActivePower": TagMetadata(
                    node_id=NODE_ID,
                    tag_id=TAG_ID,
                    stable_source_key="pcs/ActivePower",
                    data_type="INT",
                    unit="kW",
                    timestamp_trusted=False,
                )
            },
            received_at=NOW,
            source_message_id="message",
            source_sequence=None,
        )

    def test_integral_json_float_obeys_configured_int_contract(self) -> None:
        observations = self._convert(0.0)

        self.assertEqual(1, len(observations))
        self.assertEqual(ValueKind.INT, observations[0].value.kind)
        self.assertEqual(0, observations[0].value.value)

    def test_fractional_json_float_is_rejected_for_int_contract(self) -> None:
        self.assertEqual((), self._convert(0.5))


if __name__ == "__main__":
    unittest.main()
