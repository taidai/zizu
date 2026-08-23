from __future__ import annotations

import unittest

from uuid import UUID

from app.services.data_trunk_contracts import (
    FormulaSource,
    FormulaTransform,
    InputReference,
    ValueKind,
)
from app.services.point_processing_formula import (
    FormulaCompileError,
    compile_formula,
)


class PointProcessingFormulaTest(unittest.TestCase):
    def test_compiles_collection_sum_to_canonical_ast(self) -> None:
        compiled = compile_formula(
            "sum(pcs_power)",
            sources=(
                FormulaSource(
                    name="pcs_power",
                    data_type=ValueKind.FLOAT,
                    unit="kW",
                    cardinality="many",
                    required=True,
                    default_value=None,
                ),
            ),
            result_type=ValueKind.FLOAT,
            result_unit="kW",
        )

        self.assertEqual(
            compiled.ast,
            {"call": "sum", "args": [{"input": "pcs_power"}]},
        )
        self.assertEqual(len(compiled.digest), 64)

    def test_rejects_attribute_and_dynamic_calls(self) -> None:
        sources = (
            FormulaSource(
                name="pcs_power",
                data_type=ValueKind.FLOAT,
                unit="kW",
                cardinality="one",
                required=True,
                default_value=None,
            ),
        )

        for text in ('__import__("os")', "pcs_power.__class__", 'open("x")'):
            with self.subTest(text=text):
                with self.assertRaises(FormulaCompileError) as raised:
                    compile_formula(
                        text,
                        sources=sources,
                        result_type=ValueKind.FLOAT,
                        result_unit="kW",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "POINT_PROCESSING_FORMULA_INVALID",
                )

    def test_rejects_addition_without_explicit_unit_conversion(self) -> None:
        with self.assertRaises(FormulaCompileError) as raised:
            compile_formula(
                "kw + watts",
                sources=(
                    FormulaSource("kw", ValueKind.FLOAT, "kW", "one", True, None),
                    FormulaSource("watts", ValueKind.FLOAT, "W", "one", True, None),
                ),
                result_type=ValueKind.FLOAT,
                result_unit="kW",
            )

        self.assertEqual(raised.exception.code, "POINT_PROCESSING_UNIT_MISMATCH")

    def test_convert_makes_compatible_units_explicit(self) -> None:
        compiled = compile_formula(
            'kw + convert(watts, "kW")',
            sources=(
                FormulaSource("kw", ValueKind.FLOAT, "kW", "one", True, None),
                FormulaSource("watts", ValueKind.FLOAT, "W", "one", True, None),
            ),
            result_type=ValueKind.FLOAT,
            result_unit="kW",
        )

        self.assertEqual(compiled.result_unit, "kW")

    def test_convert_annotates_a_finite_threshold_constant(self) -> None:
        compiled = compile_formula(
            'power > convert(100, "kW")',
            sources=(
                FormulaSource("power", ValueKind.FLOAT, "kW", "one", True, None),
            ),
            result_type=ValueKind.BOOL,
            result_unit=None,
        )

        self.assertEqual(compiled.result_kind, ValueKind.BOOL)

    def test_rejects_non_finite_optional_default(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    FormulaSource(
                        "reserve",
                        ValueKind.FLOAT,
                        "kW",
                        "one",
                        False,
                        value,
                    )

    def test_rejects_duplicate_members_inside_one_frozen_selector(self) -> None:
        contract = FormulaSource(
            "pcs_power", ValueKind.FLOAT, "kW", "many", True, None
        )
        compiled = compile_formula(
            "sum(pcs_power)",
            sources=(contract,),
            result_type=ValueKind.FLOAT,
            result_unit="kW",
        )
        reference = InputReference.l2(
            UUID("00000000-0000-0000-0000-000000000401")
        )

        with self.assertRaises(ValueError):
            FormulaTransform(
                sources={"pcs_power": (reference, reference)},
                source_contracts={"pcs_power": contract},
                compiled=compiled,
                schedule_seconds=1,
                control_eligible=False,
            )


if __name__ == "__main__":
    unittest.main()
