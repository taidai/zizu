from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.services.data_trunk_contracts import (
    InputReference,
    InstalledPointConversion,
    RawObservation,
    TrunkQuality,
    TypedValue,
)
from app.services.data_trunk_conversion import evaluate_conversion


class PcsNumericConversionTest(unittest.TestCase):
    @staticmethod
    def fixture() -> dict[str, Any]:
        raw = RawObservation(
            observation_id=UUID("00000000-0000-0000-0000-000000000101"),
            node_id=UUID("00000000-0000-0000-0000-000000000001"),
            tag_id=UUID("00000000-0000-0000-0000-000000000011"),
            source_key="ActivePowerRaw",
            value=TypedValue.float(12345.0),
            raw_unit="W",
            quality=TrunkQuality.GOOD,
            source_timestamp=datetime(2026, 8, 17, tzinfo=UTC),
            received_at=datetime(2026, 8, 17, 0, 0, 1, tzinfo=UTC),
            source_message_id="msg-1",
            source_sequence=1,
            source_digest="a" * 64,
        )
        installed = InstalledPointConversion.numeric(
            installation_id=UUID("00000000-0000-0000-0000-000000000201"),
            revision_id=UUID("00000000-0000-0000-0000-000000000202"),
            input_tag_id=raw.tag_id,
            output_entity_instance_id=UUID("00000000-0000-0000-0000-000000000301"),
            output_definition_id="pcs.active_power",
            scale=0.001,
            offset=0.0,
            input_unit="W",
            output_unit="kW",
            minimum=-500.0,
            maximum=500.0,
        )
        return {
            "installed": (installed,),
            "current_inputs": {InputReference.l0(raw.tag_id): raw},
            "site_configuration_version": 4,
            "calculated_at": datetime(2026, 8, 17, 0, 0, 2, tzinfo=UTC),
        }

    def test_scales_raw_watts_to_stable_kw_entity(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        result = evaluate_conversion(**fixture)

        self.assertEqual(result[0].value, TypedValue.float(12.345))
        self.assertEqual(result[0].unit, "kW")
        self.assertEqual(result[0].quality, TrunkQuality.GOOD)
        self.assertEqual(result[0].source_observation_ids, (raw.observation_id,))
        self.assertEqual(result[0].site_configuration_version, 4)
        self.assertEqual(
            result[0].source_order_key,
            f"S:00000000000000000001:{'a' * 64}",
        )

    def test_numeric_conversion_marks_wrong_runtime_unit_bad_without_value(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        wrong_unit = replace(raw, raw_unit="A")
        fixture["current_inputs"] = {InputReference.l0(raw.tag_id): wrong_unit}

        output = evaluate_conversion(**fixture)[0]

        self.assertEqual(output.value, TypedValue.float(None))
        self.assertEqual(
            (output.quality, output.reason),
            (TrunkQuality.BAD, "UNIT_MISMATCH"),
        )
        self.assertEqual(output.source_observation_ids, (raw.observation_id,))

    def test_same_inputs_produce_same_event_id(self) -> None:
        first = evaluate_conversion(**self.fixture())
        second = evaluate_conversion(**self.fixture())

        self.assertEqual(first[0].event_id, second[0].event_id)

    def test_raw_observation_is_immutable(self) -> None:
        raw = next(iter(self.fixture()["current_inputs"].values()))

        with self.assertRaises(FrozenInstanceError):
            raw.raw_unit = "A"


if __name__ == "__main__":
    unittest.main()
