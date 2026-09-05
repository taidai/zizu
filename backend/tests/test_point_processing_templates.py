"""Standalone L1 point-processing template contract."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.services.point_processing_templates import (
    InMemoryPointProcessingTemplates,
    PointProcessingTemplateError,
    canonical_point_processing_content,
    parse_point_processing_template,
)


def template_json() -> dict:
    return {
        "schemaVersion": "zizu.point-processing/v1alpha1",
        "id": "pcs.brand-a",
        "kind": "point_processing_template",
        "displayName": "PCS 品牌 A",
        "deviceCategory": "PCS",
        "brand": "Brand A",
        "model": "PCS-100K",
        "revision": 1,
        "status": "active",
        "inputs": [
            {
                "id": "active_power_raw",
                "sourceKind": "l0",
                "sourceKey": "ActivePowerRaw",
                "aliases": ["ActivePower"],
                "dataType": "FLOAT",
                "unit": "W",
                "required": True,
            }
        ],
        "outputs": [
            {
                "id": "active_power",
                "entityDefinition": "pcs.active_power",
                "dataType": "FLOAT",
                "unit": "kW",
                "freshness": "10s",
                "transform": {
                    "kind": "numeric",
                    "input": "active_power_raw",
                    "scale": 0.001,
                    "offset": 0,
                    "minimum": -1000,
                    "maximum": 1000,
                },
            }
        ],
    }


class PointProcessingTemplateTest(unittest.TestCase):
    @staticmethod
    def passthrough_template(data_type: str, unit: str | None) -> dict:
        raw = template_json()
        raw["inputs"][0]["dataType"] = data_type
        raw["inputs"][0]["unit"] = unit
        raw["outputs"][0]["dataType"] = data_type
        raw["outputs"][0]["unit"] = unit
        raw["outputs"][0]["transform"] = {
            "kind": "passthrough",
            "input": "active_power_raw",
        }
        return raw

    def test_passthrough_accepts_same_typed_int_bool_and_string_without_formula(self) -> None:
        with patch(
            "app.services.point_processing_templates.compile_formula"
        ) as compile_formula:
            for data_type, unit in (("INT", None), ("BOOL", None), ("STRING", "code")):
                with self.subTest(data_type=data_type):
                    parsed = parse_point_processing_template(
                        self.passthrough_template(data_type, unit)
                    )
                    self.assertEqual("passthrough", parsed.outputs[0].transform["kind"])
        compile_formula.assert_not_called()

    def test_passthrough_can_declare_missing_numeric_l0_unit_without_scaling(self) -> None:
        for data_type, unit in (("FLOAT", "kW"), ("INT", "%")):
            with self.subTest(data_type=data_type):
                raw = self.passthrough_template(data_type, None)
                raw["outputs"][0]["unit"] = unit
                parsed = parse_point_processing_template(raw)
                self.assertIsNone(parsed.inputs[0].unit)
                self.assertEqual(unit, parsed.outputs[0].unit)
                self.assertEqual(raw, canonical_point_processing_content(parsed))

    def test_missing_unit_declaration_cannot_relabel_l2_or_nonnumeric_values(self) -> None:
        for data_type, source_kind in (("FLOAT", "l2"), ("BOOL", "l0"), ("STRING", "l0")):
            with self.subTest(data_type=data_type, source_kind=source_kind):
                raw = self.passthrough_template(data_type, None)
                raw["inputs"][0]["sourceKind"] = source_kind
                raw["outputs"][0]["unit"] = "kW"
                with self.assertRaises(PointProcessingTemplateError):
                    parse_point_processing_template(raw)

    def test_unit_declaration_rejects_blank_or_padded_output_unit(self) -> None:
        for unit in ("", "   ", " kW", "kW "):
            with self.subTest(unit=unit):
                raw = self.passthrough_template("FLOAT", None)
                raw["outputs"][0]["unit"] = unit
                with self.assertRaises(PointProcessingTemplateError):
                    parse_point_processing_template(raw)

    def test_passthrough_rejects_type_unit_and_input_contract_mismatches(self) -> None:
        cases = []
        wrong_type = self.passthrough_template("INT", None)
        wrong_type["outputs"][0]["dataType"] = "BOOL"
        cases.append(wrong_type)
        wrong_unit = self.passthrough_template("INT", "raw")
        wrong_unit["outputs"][0]["unit"] = "normalized"
        cases.append(wrong_unit)
        optional = self.passthrough_template("INT", None)
        optional["inputs"][0]["required"] = False
        cases.append(optional)
        many = self.passthrough_template("INT", None)
        many["inputs"][0]["cardinality"] = "many"
        many["inputs"][0]["selector"] = {
            "scope": "descendants",
            "nodeType": "PCS",
            "entityDefinition": "ActivePowerRaw",
        }
        many["inputs"][0]["sourceKind"] = "l2"
        cases.append(many)
        missing = self.passthrough_template("INT", None)
        missing["outputs"][0]["transform"].pop("input")
        cases.append(missing)
        multiple = self.passthrough_template("INT", None)
        multiple["outputs"][0]["transform"]["input"] = [
            "active_power_raw",
            "active_power_raw",
        ]
        cases.append(multiple)

        for raw in cases:
            with self.subTest(raw=raw["outputs"][0]["transform"]):
                with self.assertRaises(PointProcessingTemplateError) as caught:
                    parse_point_processing_template(raw)
                self.assertEqual("POINT_PROCESSING_RULE_INVALID", caught.exception.code)

    def test_controllable_output_requires_bounded_local_passthrough(self) -> None:
        raw = self.passthrough_template("FLOAT", "kW")
        raw["outputs"][0]["control"] = {
            "minimum": 0,
            "maximum": 200,
            "tolerance": 0.1,
            "cooldownSeconds": 5,
            "timeoutSeconds": 15,
            "highRisk": False,
        }

        parsed = parse_point_processing_template(raw)

        self.assertEqual(raw, canonical_point_processing_content(parsed))
        self.assertEqual(200, parsed.outputs[0].control["maximum"])

        for mutation in ("readonly_source", "formula", "unbounded"):
            invalid = copy.deepcopy(raw)
            if mutation == "readonly_source":
                invalid["inputs"][0]["sourceKind"] = "l2"
            elif mutation == "formula":
                invalid["outputs"][0]["transform"] = {
                    "kind": "formula",
                    "expression": "active_power_raw",
                    "scheduleSeconds": 1,
                    "controlEligible": False,
                }
            else:
                invalid["outputs"][0]["control"].pop("maximum")
            with self.subTest(mutation=mutation):
                with self.assertRaises(PointProcessingTemplateError) as caught:
                    parse_point_processing_template(invalid)
                self.assertEqual(
                    "POINT_PROCESSING_CONTROL_INVALID",
                    caught.exception.code,
                )

    def test_boolean_map_accepts_zero_and_one_and_keeps_canonical_shape(self) -> None:
        for true_when in (0, 1):
            with self.subTest(true_when=true_when):
                raw = self.passthrough_template("INT", None)
                raw["outputs"][0]["dataType"] = "BOOL"
                raw["outputs"][0]["transform"] = {
                    "kind": "boolean_map",
                    "input": "active_power_raw",
                    "trueWhen": true_when,
                }

                parsed = parse_point_processing_template(raw)

                self.assertEqual(
                    raw,
                    canonical_point_processing_content(parsed),
                )
                self.assertEqual(
                    64,
                    len(parsed.outputs[0].transform["astDigest"]),
                )

    def test_boolean_map_rejects_ambiguous_or_incompatible_contracts(self) -> None:
        valid = self.passthrough_template("INT", None)
        valid["outputs"][0]["dataType"] = "BOOL"
        valid["outputs"][0]["transform"] = {
            "kind": "boolean_map",
            "input": "active_power_raw",
            "trueWhen": 1,
        }
        cases = []
        for invalid in (True, False, -1, 2, "1"):
            raw = copy.deepcopy(valid)
            raw["outputs"][0]["transform"]["trueWhen"] = invalid
            cases.append(raw)
        wrong_input = copy.deepcopy(valid)
        wrong_input["inputs"][0]["dataType"] = "BOOL"
        cases.append(wrong_input)
        wrong_output = copy.deepcopy(valid)
        wrong_output["outputs"][0]["dataType"] = "INT"
        cases.append(wrong_output)
        input_unit = copy.deepcopy(valid)
        input_unit["inputs"][0]["unit"] = "flag"
        cases.append(input_unit)
        output_unit = copy.deepcopy(valid)
        output_unit["outputs"][0]["unit"] = "flag"
        cases.append(output_unit)
        optional = copy.deepcopy(valid)
        optional["inputs"][0]["required"] = False
        cases.append(optional)

        for raw in cases:
            with self.subTest(transform=raw["outputs"][0]["transform"]):
                with self.assertRaises(PointProcessingTemplateError) as caught:
                    parse_point_processing_template(raw)
                self.assertEqual("POINT_PROCESSING_RULE_INVALID", caught.exception.code)

    def test_boolean_set_accepts_only_unitless_integer_fault_bits(self) -> None:
        valid = self.passthrough_template("INT", None)
        valid["outputs"][0]["dataType"] = "CODE_SET"
        valid["outputs"][0]["transform"] = {
            "kind": "boolean_set",
            "entries": [{
                "input": "active_power_raw",
                "code": "pcs.hardware.epo",
                "name": "EPO 故障",
                "category": "HARDWARE",
            }],
        }

        parsed = parse_point_processing_template(valid)

        self.assertEqual("INT", parsed.inputs[0].data_type)
        self.assertEqual("boolean_set", parsed.outputs[0].transform["kind"])
        bool_input = copy.deepcopy(valid)
        bool_input["inputs"][0]["dataType"] = "BOOL"
        with self.assertRaises(PointProcessingTemplateError) as caught:
            parse_point_processing_template(bool_input)
        self.assertEqual("POINT_PROCESSING_RULE_INVALID", caught.exception.code)

    def test_meter_template_is_a_supported_device_category(self) -> None:
        raw = template_json()
        raw["deviceCategory"] = "METER"

        parsed = parse_point_processing_template(raw)

        self.assertEqual("METER", parsed.device_category)

    def test_canonical_content_round_trips(self) -> None:
        raw = template_json()
        parsed = parse_point_processing_template(raw)

        self.assertEqual(raw, canonical_point_processing_content(parsed))
        self.assertEqual(64, len(parsed.content_digest))

    def test_visual_editor_transform_shapes_match_the_backend_contract(self) -> None:
        cases = (
            ({
                "kind": "numeric", "input": "active_power_raw",
                "scale": 1, "offset": 0,
                "minimum": -1000000000, "maximum": 1000000000,
            }, "FLOAT", "FLOAT"),
            ({
                "kind": "numeric", "input": "active_power_raw",
                "scale": 0.001, "offset": -2,
                "minimum": -500, "maximum": 500,
            }, "FLOAT", "FLOAT"),
            ({
                "kind": "enum", "input": "active_power_raw",
                "entries": {"0": "STOPPED", "1": "RUNNING"},
            }, "STRING", "ENUM"),
            ({
                "kind": "formula", "expression": "active_power_raw",
                "scheduleSeconds": 1, "controlEligible": False,
            }, "FLOAT", "FLOAT"),
        )
        for transform, input_type, output_type in cases:
            with self.subTest(kind=transform["kind"], scale=transform.get("scale")):
                raw = template_json()
                raw["inputs"][0]["dataType"] = input_type
                raw["outputs"][0]["dataType"] = output_type
                raw["outputs"][0]["transform"] = transform
                if transform["kind"] == "formula":
                    raw["outputs"][0]["unit"] = raw["inputs"][0]["unit"]
                parse_point_processing_template(raw)

    def test_revision_is_immutable_and_import_does_not_publish_configuration(self) -> None:
        registry = InMemoryPointProcessingTemplates(configuration_revision=7)
        first = registry.import_template(template_json(), actor="user:engineer")
        changed = copy.deepcopy(template_json())
        changed["displayName"] = "篡改名称"

        with self.assertRaises(PointProcessingTemplateError) as caught:
            registry.import_template(changed, actor="user:engineer")

        self.assertEqual("POINT_PROCESSING_REVISION_IMMUTABLE", caught.exception.code)
        self.assertEqual(7, registry.configuration_revision)
        self.assertEqual(
            template_json(),
            registry.export_template(first.revision_id),
        )

    def test_reference_meter_template_maps_total_power_to_grid_entity(self) -> None:
        path = (
            Path(__file__).resolve().parents[2]
            / "reference-point-processings"
            / "meter-modbus-active-power.zizu-point-processing.json"
        )

        parsed = parse_point_processing_template(
            json.loads(path.read_text(encoding="utf-8"))
        )

        self.assertEqual("METER", parsed.device_category)
        self.assertEqual("1!416409", parsed.inputs[0].source_contract["address"])
        self.assertEqual("INT", parsed.inputs[0].data_type)
        self.assertEqual("kW", parsed.inputs[0].unit)
        self.assertEqual("grid.activePower", parsed.outputs[0].entity_definition_id)
        self.assertEqual("kW", parsed.outputs[0].unit)

    def test_reference_templates_are_canonical_single_json_files(self) -> None:
        directory = Path(__file__).resolve().parents[2] / "reference-point-processings"
        paths = sorted(directory.glob("*.zizu-point-processing.json"))

        self.assertEqual(5, len(paths))
        for path in paths:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                raw,
                canonical_point_processing_content(
                    parse_point_processing_template(raw)
                ),
            )


if __name__ == "__main__":
    unittest.main()
