from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.services.data_trunk_contracts import (
    BooleanMapTransform,
    BooleanCodeInput,
    BooleanSetTransform,
    EnumTransform,
    FaultCodeTransform,
    FormulaSource,
    FormulaTransform,
    InputReference,
    InstalledPointProcessing,
    L2Observation,
    PassthroughTransform,
    RawObservation,
    TrunkQuality,
    TypedValue,
    ValueKind,
)
from app.services.data_trunk_conversion import evaluate_processing
from app.services.point_processing_formula import compile_formula


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
            event_time_basis="observed_at",
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
            "configuration_revision": 4,
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
        self.assertEqual(result[0].configuration_revision, 4)
        self.assertEqual(
            result[0].source_order_key,
            f"S:00000000000000000001:{'a' * 64}",
        )

    def test_passthrough_returns_the_same_good_typed_value(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        source = replace(raw, value=TypedValue.integer(0), raw_unit=None)
        base = fixture["installed"][0]
        fixture["current_inputs"] = {InputReference.l0(raw.tag_id): source}
        fixture["installed"] = (
            replace(
                base,
                output_kind=ValueKind.INT,
                output_unit=None,
                transform=PassthroughTransform(InputReference.l0(raw.tag_id)),
            ),
        )

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(source.value, output.value)
        self.assertIs(source.value, output.value)
        self.assertEqual(TrunkQuality.GOOD, output.quality)
        self.assertIsNone(output.reason)

    def test_passthrough_propagates_bad_and_stale_without_a_value(self) -> None:
        for quality, source_reason, expected_reason in (
            (TrunkQuality.BAD, "TYPE_MISMATCH", "TYPE_MISMATCH"),
            (TrunkQuality.STALE, None, "INPUT_STALE"),
        ):
            with self.subTest(quality=quality):
                fixture = self.fixture()
                raw = next(iter(fixture["current_inputs"].values()))
                source = replace(
                    raw,
                    value=TypedValue.integer(1),
                    raw_unit=None,
                    quality=quality,
                    quality_reason=source_reason,
                )
                base = fixture["installed"][0]
                fixture["current_inputs"] = {InputReference.l0(raw.tag_id): source}
                fixture["installed"] = (
                    replace(
                        base,
                        output_kind=ValueKind.INT,
                        output_unit=None,
                        transform=PassthroughTransform(
                            InputReference.l0(raw.tag_id)
                        ),
                    ),
                )

                output = evaluate_processing(**fixture)[0]

                self.assertEqual(TypedValue.integer(None), output.value)
                self.assertEqual(quality, output.quality)
                self.assertEqual(expected_reason, output.reason)

    def test_boolean_map_covers_both_zero_one_polarities(self) -> None:
        for true_when, raw_value, expected in (
            (1, 0, False),
            (1, 1, True),
            (0, 0, True),
            (0, 1, False),
        ):
            with self.subTest(true_when=true_when, raw_value=raw_value):
                fixture = self.fixture()
                raw = next(iter(fixture["current_inputs"].values()))
                source = replace(
                    raw,
                    value=TypedValue.integer(raw_value),
                    raw_unit=None,
                )
                compiled = compile_formula(
                    f"input == {true_when}",
                    sources=(
                        FormulaSource(
                            "input",
                            ValueKind.INT,
                            None,
                            "one",
                            True,
                            None,
                        ),
                    ),
                    result_type=ValueKind.BOOL,
                    result_unit=None,
                )
                base = fixture["installed"][0]
                fixture["current_inputs"] = {InputReference.l0(raw.tag_id): source}
                fixture["installed"] = (
                    replace(
                        base,
                        output_kind=ValueKind.BOOL,
                        output_unit=None,
                        transform=BooleanMapTransform(
                            InputReference.l0(raw.tag_id),
                            true_when,
                            compiled,
                        ),
                    ),
                )

                output = evaluate_processing(**fixture)[0]

                self.assertEqual(TypedValue.boolean(expected), output.value)
                self.assertEqual(TrunkQuality.GOOD, output.quality)

    def test_boolean_map_rejects_out_of_range_good_integer_at_runtime(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        source = replace(raw, value=TypedValue.integer(2), raw_unit=None)
        compiled = compile_formula(
            "input == 1",
            sources=(FormulaSource("input", ValueKind.INT, None, "one", True, None),),
            result_type=ValueKind.BOOL,
            result_unit=None,
        )
        base = fixture["installed"][0]
        fixture["current_inputs"] = {InputReference.l0(raw.tag_id): source}
        fixture["installed"] = (
            replace(
                base,
                output_kind=ValueKind.BOOL,
                output_unit=None,
                transform=BooleanMapTransform(
                    InputReference.l0(raw.tag_id),
                    1,
                    compiled,
                ),
            ),
        )

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(TypedValue.boolean(None), output.value)
        self.assertEqual(TrunkQuality.BAD, output.quality)
        self.assertEqual("BIT_VALUE_OUT_OF_RANGE", output.reason)

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

    def test_frame_identity_changes_event_identity_and_stale_keeps_value(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        fixture["current_inputs"] = {
            InputReference.l0(raw.tag_id): replace(
                raw, quality=TrunkQuality.STALE
            )
        }
        first = evaluate_processing(
            **fixture,
            frame_id=UUID("00000000-0000-0000-0000-000000000901"),
            frame_sequence=9,
        )[0]
        second = evaluate_processing(
            **fixture,
            frame_id=UUID("00000000-0000-0000-0000-000000000902"),
            frame_sequence=10,
        )[0]

        self.assertEqual(TypedValue.float(12.345), first.value)
        self.assertEqual(TrunkQuality.STALE, first.quality)
        self.assertEqual(9, first.frame_sequence)
        self.assertNotEqual(first.event_id, second.event_id)

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

    def test_maps_integer_operating_state_enum(self) -> None:
        fixture = self.fixture()
        raw = next(iter(fixture["current_inputs"].values()))
        base = fixture["installed"][0]
        fixture["current_inputs"] = {
            InputReference.l0(raw.tag_id): replace(
                raw,
                value=TypedValue(ValueKind.INT, 4),
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
                    entries={"4": "GRID_CONNECTED"},
                ),
            ),
        )

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(TypedValue.enum("GRID_CONNECTED"), output.value)
        self.assertEqual(TrunkQuality.GOOD, output.quality)

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

    def test_boolean_set_collects_true_codes_in_canonical_order(self) -> None:
        fixture = self._boolean_set_fixture()

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(
            TypedValue.code_set((
                "pcs.hardware.epo",
                "pcs.grid.low_voltage_ride_through_timeout",
            )),
            output.value,
        )
        self.assertEqual(TrunkQuality.GOOD, output.quality)
        self.assertEqual(88, len(output.source_observation_ids))

    def test_boolean_set_rejects_an_integer_other_than_zero_or_one(self) -> None:
        fixture = self._boolean_set_fixture()
        invalid_key = next(iter(fixture["current_inputs"]))
        fixture["current_inputs"] = {
            **fixture["current_inputs"],
            invalid_key: replace(
                fixture["current_inputs"][invalid_key],
                value=TypedValue.integer(2),
            ),
        }

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(TypedValue.code_set(None), output.value)
        self.assertEqual(
            (TrunkQuality.BAD, "BIT_VALUE_OUT_OF_RANGE"),
            (output.quality, output.reason),
        )

    def test_boolean_set_keeps_value_when_one_required_input_is_stale(self) -> None:
        fixture = self._boolean_set_fixture()
        expected_value = evaluate_processing(**fixture)[0].value
        stale_key = next(iter(fixture["current_inputs"]))
        stale = replace(
            fixture["current_inputs"][stale_key],
            quality=TrunkQuality.STALE,
        )
        fixture["current_inputs"] = {
            **fixture["current_inputs"],
            stale_key: stale,
        }

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(expected_value, output.value)
        self.assertEqual(
            (TrunkQuality.STALE, "INPUT_STALE"),
            (output.quality, output.reason),
        )

    def test_boolean_set_fails_closed_when_one_required_input_is_missing(self) -> None:
        fixture = self._boolean_set_fixture()
        missing_key = next(iter(fixture["current_inputs"]))
        fixture["current_inputs"] = {
            key: value for key, value in fixture["current_inputs"].items()
            if key != missing_key
        }

        output = evaluate_processing(**fixture)[0]

        self.assertEqual(TypedValue.code_set(None), output.value)
        self.assertEqual(
            (TrunkQuality.BAD, "REQUIRED_INPUT_MISSING"),
            (output.quality, output.reason),
        )

    def test_formula_optional_default_marks_result_uncertain(self) -> None:
        primary_ref = InputReference.l2(
            UUID("00000000-0000-0000-0000-000000000401")
        )
        reserve_ref = InputReference.l2(
            UUID("00000000-0000-0000-0000-000000000402")
        )
        sources = {
            "primary": FormulaSource(
                "primary", ValueKind.FLOAT, "kW", "one", True, None
            ),
            "reserve": FormulaSource(
                "reserve", ValueKind.FLOAT, "kW", "one", False, 0.0
            ),
        }
        compiled = compile_formula(
            "primary + reserve",
            sources=tuple(sources.values()),
            result_type=ValueKind.FLOAT,
            result_unit="kW",
        )
        base = self.fixture()["installed"][0]
        installed = replace(
            base,
            output_kind=ValueKind.FLOAT,
            output_unit="kW",
            transform=FormulaTransform(
                sources={
                    "primary": (primary_ref,),
                    "reserve": (),
                },
                source_contracts=sources,
                compiled=compiled,
                schedule_seconds=1,
                control_eligible=False,
            ),
        )
        current = {
            primary_ref: self._l2_input(
                entity_id=primary_ref.source_id,
                event_id=UUID("00000000-0000-0000-0000-000000000501"),
                value=12.0,
                digest="b" * 64,
            )
        }

        output = evaluate_processing(
            installed=(installed,),
            current_inputs=current,
            configuration_revision=5,
            calculated_at=datetime(2026, 8, 17, 0, 0, 2, tzinfo=UTC),
        )[0]

        self.assertEqual(output.value, TypedValue.float(12.0))
        self.assertEqual(output.quality, TrunkQuality.UNCERTAIN)
        self.assertEqual(output.reason, "OPTIONAL_DEFAULT_USED")
        self.assertEqual(output.source_observation_ids, (current[primary_ref].event_id,))

    def test_formula_combines_same_node_l0_inputs_without_promoting_them_to_l2(self) -> None:
        first = next(iter(self.fixture()["current_inputs"].values()))
        second = replace(
            first,
            observation_id=UUID("00000000-0000-0000-0000-000000000102"),
            tag_id=UUID("00000000-0000-0000-0000-000000000012"),
            source_key="ReactivePowerRaw",
            value=TypedValue.float(4.0),
            source_digest="b" * 64,
        )
        references = {
            "active": (InputReference.l0(first.tag_id),),
            "reactive": (InputReference.l0(second.tag_id),),
        }
        contracts = {
            name: FormulaSource(name, ValueKind.FLOAT, "W", "one", True, None)
            for name in references
        }
        compiled = compile_formula(
            "active + reactive",
            sources=tuple(contracts.values()),
            result_type=ValueKind.FLOAT,
            result_unit="W",
        )
        installed = replace(
            self.fixture()["installed"][0],
            output_kind=ValueKind.FLOAT,
            output_unit="W",
            transform=FormulaTransform(
                sources=references,
                source_contracts=contracts,
                compiled=compiled,
                schedule_seconds=1,
                control_eligible=False,
            ),
        )

        output = evaluate_processing(
            installed=(installed,),
            current_inputs={
                InputReference.l0(first.tag_id): first,
                InputReference.l0(second.tag_id): second,
            },
            configuration_revision=5,
            calculated_at=datetime(2026, 8, 17, 0, 0, 2, tzinfo=UTC),
        )[0]

        self.assertEqual(TypedValue.float(12349.0), output.value)
        self.assertEqual(TrunkQuality.GOOD, output.quality)
        self.assertEqual(
            tuple(sorted((first.observation_id, second.observation_id), key=str)),
            output.source_observation_ids,
        )

    @staticmethod
    def _l2_input(
        *,
        entity_id: UUID,
        event_id: UUID,
        value: float,
        digest: str,
    ) -> L2Observation:
        return L2Observation(
            event_id=event_id,
            entity_instance_id=entity_id,
            definition_id="pcs.active_power",
            value=TypedValue.float(value),
            unit="kW",
            quality=TrunkQuality.GOOD,
            reason=None,
            observed_at=datetime(2026, 8, 17, tzinfo=UTC),
            received_at=datetime(2026, 8, 17, 0, 0, 1, tzinfo=UTC),
            calculated_at=datetime(2026, 8, 17, 0, 0, 1, tzinfo=UTC),
            processing_revision_id=UUID(
                "00000000-0000-0000-0000-000000000601"
            ),
            configuration_revision=4,
            source_observation_ids=(),
            source_digest=digest,
            source_order_key=f"L:{event_id}",
            event_time_basis="observed_at",
        )

    def _boolean_set_fixture(self) -> dict[str, Any]:
        base = self.fixture()
        installed = base["installed"][0]
        references = tuple(
            InputReference.l0(UUID(int=0x1000 + index))
            for index in range(88)
        )
        observations = {
            reference: RawObservation(
                observation_id=UUID(int=0x2000 + index),
                node_id=UUID("00000000-0000-0000-0000-000000000001"),
                tag_id=reference.source_id,
                source_key=f"Fault{index:02d}",
                value=TypedValue(
                    ValueKind.INT,
                    1 if index in {0, 87} else 0,
                ),
                raw_unit=None,
                quality=TrunkQuality.GOOD,
                source_timestamp=datetime(2026, 8, 17, tzinfo=UTC),
                received_at=datetime(2026, 8, 17, 0, 0, 1, tzinfo=UTC),
                source_message_id="msg-en9",
                source_sequence=index,
                source_digest=f"{index:064x}",
                event_time_basis="observed_at",
            )
            for index, reference in enumerate(references)
        }
        return {
            "installed": (
                replace(
                    installed,
                    output_kind=ValueKind.CODE_SET,
                    output_unit=None,
                    transform=BooleanSetTransform(
                        inputs=tuple(
                            BooleanCodeInput(
                                input=reference,
                                code=(
                                    "pcs.hardware.epo" if index == 0
                                    else "pcs.grid.low_voltage_ride_through_timeout" if index == 87
                                    else f"EN9_FAULT_{index:02d}"
                                ),
                            )
                            for index, reference in enumerate(references)
                        ),
                    ),
                ),
            ),
            "current_inputs": observations,
            "configuration_revision": 4,
            "calculated_at": datetime(2026, 8, 17, 0, 0, 2, tzinfo=UTC),
        }


if __name__ == "__main__":
    unittest.main()
