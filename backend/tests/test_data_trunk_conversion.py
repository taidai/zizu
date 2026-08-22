from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.services.data_trunk_contracts import (
    EnumTransform,
    FaultCodeTransform,
    InputReference,
    InstalledPointProcessing,
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.data_trunk_conversion import evaluate_processing


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
        installed = InstalledPointProcessing.numeric(
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
        result = evaluate_processing(**fixture)

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

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(output.value, TypedValue.float(None))
        self.assertEqual(
            (output.quality, output.reason),
            (TrunkQuality.BAD, "UNIT_MISMATCH"),
        )
        self.assertEqual(output.source_observation_ids, (raw.observation_id,))

    def test_same_inputs_produce_same_event_id(self) -> None:
        first = evaluate_processing(**self.fixture())
        second = evaluate_processing(**self.fixture())

        self.assertEqual(first[0].event_id, second[0].event_id)

    def test_raw_observation_is_immutable(self) -> None:
        raw = next(iter(self.fixture()["current_inputs"].values()))

        with self.assertRaises(FrozenInstanceError):
            raw.raw_unit = "A"

    def test_maps_operating_state_enum(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        base = fixture["installed"][0]
        fixture["current_inputs"] = {
            InputReference.l0(raw.tag_id): replace(
                raw,
                value=TypedValue(ValueKind.STRING, "2"),
                raw_unit=None,
            )
        }
        fixture["installed"] = (
            replace(
                base,
                output_kind=ValueKind.ENUM,
                output_unit=None,
                transform=EnumTransform(
                    input=base.transform.input,
                    entries={"0": "STOPPED", "2": "RUNNING"},
                ),
            ),
        )

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(output.value, TypedValue.enum("RUNNING"))
        self.assertEqual(output.quality, TrunkQuality.GOOD)

    def test_unknown_enum_is_bad_without_current_value(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        base = fixture["installed"][0]
        fixture["current_inputs"] = {
            InputReference.l0(raw.tag_id): replace(
                raw,
                value=TypedValue(ValueKind.STRING, "99"),
                raw_unit=None,
            )
        }
        fixture["installed"] = (
            replace(
                base,
                output_kind=ValueKind.ENUM,
                output_unit=None,
                transform=EnumTransform(
                    input=base.transform.input,
                    entries={"0": "STOPPED", "2": "RUNNING"},
                ),
            ),
        )

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(output.value, TypedValue.enum(None))
        self.assertEqual(
            (output.quality, output.reason),
            (TrunkQuality.BAD, "UNMAPPED_ENUM"),
        )

    def test_fault_codes_are_deduplicated_sorted_and_keep_unknown(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        base = fixture["installed"][0]
        fixture["current_inputs"] = {
            InputReference.l0(raw.tag_id): replace(
                raw,
                value=TypedValue(ValueKind.STRING, "E30; e11;E30;X99"),
                raw_unit=None,
            )
        }
        fixture["installed"] = (
            replace(
                base,
                output_kind=ValueKind.CODE_SET,
                output_unit=None,
                transform=FaultCodeTransform(
                    input=base.transform.input,
                    delimiter="semicolon",
                    entries={
                        "E30": "COMPRESSOR_FAULT",
                        "E11": "DC_OVERVOLTAGE",
                    },
                ),
            ),
        )

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(
            output.value,
            TypedValue.code_set(
                ("COMPRESSOR_FAULT", "DC_OVERVOLTAGE", "X99")
            ),
        )
        self.assertEqual(
            (output.quality, output.reason),
            (TrunkQuality.UNCERTAIN, "UNMAPPED_FAULT_CODE"),
        )

    def test_out_of_range_numeric_is_bad_and_null(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        fixture["current_inputs"] = {
            InputReference.l0(raw.tag_id): replace(
                raw,
                value=TypedValue.float(900000.0),
            )
        }

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(output.value, TypedValue.float(None))
        self.assertEqual(
            (output.quality, output.reason),
            (TrunkQuality.BAD, "OUT_OF_RANGE"),
        )

    def test_missing_required_input_emits_bad_output_instead_of_guessing(self) -> None:
        fixture = self.fixture()
        fixture["current_inputs"] = {}

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(output.value, TypedValue.float(None))
        self.assertEqual(
            (output.quality, output.reason),
            (TrunkQuality.BAD, "REQUIRED_INPUT_MISSING"),
        )

    def test_bad_source_cannot_keep_a_numeric_current_value(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        fixture["current_inputs"] = {
            InputReference.l0(raw.tag_id): replace(
                raw,
                quality=TrunkQuality.BAD,
            )
        }

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(output.value, TypedValue.float(None))
        self.assertEqual(
            (output.quality, output.reason),
            (TrunkQuality.BAD, "INPUT_BAD"),
        )

    def test_fault_code_transform_rejects_arbitrary_regex_delimiters(self) -> None:
        input_reference = next(
            iter(self.fixture()["installed"])
        ).transform.input

        with self.assertRaisesRegex(ValueError, "unsupported fault-code delimiter"):
            FaultCodeTransform(
                input=input_reference,
                delimiter=".+",
                entries={"E30": "COMPRESSOR_FAULT"},
            )

    def test_fault_code_set_deduplicates_after_brand_mapping(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        base = fixture["installed"][0]
        fixture["current_inputs"] = {
            InputReference.l0(raw.tag_id): replace(
                raw,
                value=TypedValue(ValueKind.STRING, "E30;C30"),
                raw_unit=None,
            )
        }
        fixture["installed"] = (
            replace(
                base,
                output_kind=ValueKind.CODE_SET,
                output_unit=None,
                transform=FaultCodeTransform(
                    input=base.transform.input,
                    delimiter="semicolon",
                    entries={
                        "E30": "COMPRESSOR_FAULT",
                        "C30": "COMPRESSOR_FAULT",
                    },
                ),
            ),
        )

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(
            output.value,
            TypedValue.code_set(("COMPRESSOR_FAULT",)),
        )
        self.assertEqual(output.quality, TrunkQuality.GOOD)

    def test_code_set_value_object_is_canonical(self) -> None:
        self.assertEqual(
            TypedValue.code_set(("B", "A", "B")),
            TypedValue(ValueKind.CODE_SET, ("A", "B")),
        )

    def test_missing_enum_input_emits_typed_bad_output(self) -> None:
        fixture = self.fixture()
        base = fixture["installed"][0]
        fixture["current_inputs"] = {}
        fixture["installed"] = (
            replace(
                base,
                output_kind=ValueKind.ENUM,
                output_unit=None,
                transform=EnumTransform(
                    input=base.transform.input,
                    entries={"0": "STOPPED", "2": "RUNNING"},
                ),
            ),
        )

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(output.value, TypedValue.enum(None))
        self.assertEqual(
            (output.quality, output.reason),
            (TrunkQuality.BAD, "REQUIRED_INPUT_MISSING"),
        )


if __name__ == "__main__":
    unittest.main()
