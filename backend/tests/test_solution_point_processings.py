"""PCS point-processing assets are validated through solution import."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import yaml

from app.services.solution_delivery import InMemoryDeliveryRepository, SolutionDelivery
from app.services.solution_delivery_contracts import DeliveryError


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_reference_delivery.py"
SPEC = importlib.util.spec_from_file_location("build_reference_delivery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class SolutionPointProcessingAssetTest(unittest.TestCase):
    def test_imports_three_pcs_templates_with_same_three_outputs(self) -> None:
        from app.services.solution_point_processings import point_processing_assets

        package = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        ).import_package(builder.build_archive(), "user:test-engineer")

        assets = point_processing_assets(package)

        self.assertEqual(
            {item.asset_id for item in assets},
            {"pcs.brand-a", "pcs.brand-b", "pcs.en9"},
        )
        for asset in assets:
            self.assertEqual(
                tuple(output.entity_definition_id for output in asset.outputs),
                (
                    "pcs.active_power",
                    "pcs.fault_codes",
                    "pcs.operating_state",
                ),
            )

    def test_en9_asset_has_exact_read_only_contract(self) -> None:
        from app.services.solution_point_processings import point_processing_assets

        package = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.82",
        ).import_package(builder.build_archive(), "user:test-engineer")
        asset = next(
            item for item in point_processing_assets(package)
            if item.asset_id == "pcs.en9"
        )

        self.assertEqual(90, len(asset.inputs))
        self.assertTrue(all(item.source_kind == "l0" for item in asset.inputs))
        self.assertTrue(all(item.required for item in asset.inputs))
        self.assertEqual(3, len(asset.outputs))
        by_definition = {
            item.entity_definition_id: item for item in asset.outputs
        }
        self.assertEqual(
            1.0,
            by_definition["pcs.active_power"].transform["scale"],
        )
        self.assertEqual(
            88,
            len(by_definition["pcs.fault_codes"].transform["entries"]),
        )
        fixture = json.loads(
            (REPO_ROOT / "backend" / "tests" / "fixtures" / "en9_pcs_catalog.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"totalInputs": 90, "faultInputs": 88, "readOnly": True},
            fixture["contract"],
        )
        self.assertTrue(all(point["readOnly"] for point in fixture["points"]))
        self.assertEqual(0.1, fixture["points"][0]["decimal"])
        self.assertEqual(
            {entry["code"] for entry in by_definition["pcs.fault_codes"].transform["entries"]},
            {point["code"] for point in fixture["points"][2:]},
        )

    def test_rejects_arbitrary_expression_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package"
            shutil.copytree(builder.DEFAULT_SOURCE, source)
            asset_path = source / "point-processings" / "pcs-brand-a.yaml"
            asset = yaml.safe_load(asset_path.read_text(encoding="utf-8"))
            asset["outputs"][0]["transform"] = {
                "kind": "expression",
                "code": "eval(raw)",
            }
            asset_path.write_text(
                yaml.safe_dump(asset, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryError,
                "POINT_PROCESSING_RULE_INVALID",
            ):
                SolutionDelivery(
                    InMemoryDeliveryRepository(),
                    platform_version="0.4.77",
                ).import_package(builder.build_archive(source), "user:test-engineer")

    def test_rejects_reversed_numeric_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package"
            shutil.copytree(builder.DEFAULT_SOURCE, source)
            asset_path = source / "point-processings" / "pcs-brand-a.yaml"
            asset = yaml.safe_load(asset_path.read_text(encoding="utf-8"))
            asset["outputs"][0]["transform"]["minimum"] = 10
            asset["outputs"][0]["transform"]["maximum"] = -10
            asset_path.write_text(
                yaml.safe_dump(asset, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryError,
                "POINT_PROCESSING_RULE_INVALID",
            ):
                SolutionDelivery(
                    InMemoryDeliveryRepository(),
                    platform_version="0.4.77",
                ).import_package(builder.build_archive(source), "user:test-engineer")

    def test_rejects_case_ambiguous_fault_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package"
            shutil.copytree(builder.DEFAULT_SOURCE, source)
            asset_path = source / "point-processings" / "pcs-brand-a.yaml"
            asset = yaml.safe_load(asset_path.read_text(encoding="utf-8"))
            asset["outputs"][2]["transform"]["entries"]["e11"] = {
                "code": "ANOTHER_FAULT",
                "name": "重复原始码",
            }
            asset_path.write_text(
                yaml.safe_dump(asset, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryError,
                "POINT_PROCESSING_RULE_INVALID",
            ):
                SolutionDelivery(
                    InMemoryDeliveryRepository(),
                    platform_version="0.4.77",
                ).import_package(builder.build_archive(source), "user:test-engineer")


if __name__ == "__main__":
    unittest.main()
