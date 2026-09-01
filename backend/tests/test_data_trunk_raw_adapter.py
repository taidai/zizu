from __future__ import annotations

from datetime import UTC, datetime
import unittest
from uuid import UUID

from app.models.schemas import ParsedMessage
from app.services.data_trunk import RawObservationAdapter, TagMetadata
from app.services.data_trunk_contracts import TrunkQuality, ValueKind


NODE_ID = UUID("52000000-0000-0000-0000-000000000001")
TAG_ID = UUID("52000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 27, 13, 30, tzinfo=UTC)


class RawObservationAdapterTest(unittest.TestCase):
    def _convert(
        self,
        raw_value: object,
        data_type: str = "INT",
        wire_data_type: str | None = "INT16",
    ):
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
                    data_type=data_type,
                    wire_data_type=wire_data_type,
                    unit="kW",
                    timestamp_trusted=False,
                )
            },
            received_at=NOW,
            source_message_id="message",
            source_sequence=None,
        )

    def test_integral_json_float_is_retained_and_marked_type_mismatch(self) -> None:
        observations = self._convert(0.0)

        self.assertEqual(1, len(observations))
        self.assertEqual(ValueKind.FLOAT, observations[0].value.kind)
        self.assertEqual(0.0, observations[0].value.value)
        self.assertEqual(TrunkQuality.BAD, observations[0].quality)
        self.assertEqual("TYPE_MISMATCH", observations[0].quality_reason)

    def test_fractional_json_float_is_retained_for_int_contract(self) -> None:
        observation = self._convert(0.5)[0]

        self.assertEqual(ValueKind.FLOAT, observation.value.kind)
        self.assertEqual(0.5, observation.value.value)
        self.assertEqual(TrunkQuality.BAD, observation.quality)
        self.assertEqual("TYPE_MISMATCH", observation.quality_reason)

    def test_numeric_bit_keeps_zero_and_one_as_good_integers(self) -> None:
        disabled = self._convert(0, "INT", "BIT")
        enabled = self._convert(1, "INT", "BIT")

        self.assertEqual(ValueKind.INT, disabled[0].value.kind)
        self.assertEqual(0, disabled[0].value.value)
        self.assertEqual(TrunkQuality.GOOD, disabled[0].quality)
        self.assertIsNone(disabled[0].quality_reason)
        self.assertEqual(ValueKind.INT, enabled[0].value.kind)
        self.assertEqual(1, enabled[0].value.value)
        self.assertEqual(TrunkQuality.GOOD, enabled[0].quality)
        self.assertIsNone(enabled[0].quality_reason)

    def test_out_of_range_bit_is_retained_as_bad_integer(self) -> None:
        observation = self._convert(2, "INT", "BIT")[0]

        self.assertEqual(ValueKind.INT, observation.value.kind)
        self.assertEqual(2, observation.value.value)
        self.assertEqual(TrunkQuality.BAD, observation.quality)
        self.assertEqual("BIT_VALUE_OUT_OF_RANGE", observation.quality_reason)

    def test_bit_string_and_boolean_literals_are_retained_as_type_mismatch(self) -> None:
        text = self._convert("0", "INT", "BIT")[0]
        boolean = self._convert(False, "INT", "BIT")[0]

        self.assertEqual((ValueKind.STRING, "0"), (text.value.kind, text.value.value))
        self.assertEqual(TrunkQuality.BAD, text.quality)
        self.assertEqual("TYPE_MISMATCH", text.quality_reason)
        self.assertEqual((ValueKind.BOOL, False), (boolean.value.kind, boolean.value.value))
        self.assertEqual(TrunkQuality.BAD, boolean.quality)
        self.assertEqual("TYPE_MISMATCH", boolean.quality_reason)

    def test_actual_boolean_remains_good_for_boolean_point(self) -> None:
        observation = self._convert(False, "BOOL", "BOOL")[0]

        self.assertEqual(ValueKind.BOOL, observation.value.kind)
        self.assertIs(False, observation.value.value)
        self.assertEqual(TrunkQuality.GOOD, observation.quality)
        self.assertIsNone(observation.quality_reason)

    def test_integer_zero_and_boolean_false_have_distinct_source_digests(self) -> None:
        integer = self._convert(0, "INT", "BIT")[0]
        boolean = self._convert(False, "INT", "BIT")[0]

        self.assertNotEqual(integer.source_digest, boolean.source_digest)


if __name__ == "__main__":
    unittest.main()
