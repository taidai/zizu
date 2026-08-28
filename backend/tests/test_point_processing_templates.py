"""Standalone L1 point-processing template contract."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

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
