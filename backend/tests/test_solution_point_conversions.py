"""PCS point-conversion assets are validated through solution import."""
from __future__ import annotations

import importlib.util
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


class SolutionPointConversionAssetTest(unittest.TestCase):
    def test_imports_two_pcs_templates_with_same_three_outputs(self) -> None:
        from app.services.solution_point_conversions import point_conversion_assets

        package = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        ).import_package(builder.build_archive())

        assets = point_conversion_assets(package)

        self.assertEqual(
            {item.asset_id for item in assets},
            {"pcs.brand-a", "pcs.brand-b"},
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

    def test_rejects_arbitrary_expression_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package"
            shutil.copytree(builder.DEFAULT_SOURCE, source)
            asset_path = source / "point-conversions" / "pcs-brand-a.yaml"
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
                "POINT_CONVERSION_RULE_INVALID",
            ):
                SolutionDelivery(
                    InMemoryDeliveryRepository(),
                    platform_version="0.4.77",
                ).import_package(builder.build_archive(source))

    def test_rejects_reversed_numeric_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package"
            shutil.copytree(builder.DEFAULT_SOURCE, source)
            asset_path = source / "point-conversions" / "pcs-brand-a.yaml"
            asset = yaml.safe_load(asset_path.read_text(encoding="utf-8"))
            asset["outputs"][0]["transform"]["minimum"] = 10
            asset["outputs"][0]["transform"]["maximum"] = -10
            asset_path.write_text(
                yaml.safe_dump(asset, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryError,
                "POINT_CONVERSION_RULE_INVALID",
            ):
                SolutionDelivery(
                    InMemoryDeliveryRepository(),
                    platform_version="0.4.77",
                ).import_package(builder.build_archive(source))

    def test_rejects_case_ambiguous_fault_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package"
            shutil.copytree(builder.DEFAULT_SOURCE, source)
            asset_path = source / "point-conversions" / "pcs-brand-a.yaml"
            asset = yaml.safe_load(asset_path.read_text(encoding="utf-8"))
            asset["outputs"][2]["transform"]["entries"]["e11"] = {
                "code": "ANOTHER_FAULT",
                "name": "重复原始码",
                "defaultSeverity": "INFO",
            }
            asset_path.write_text(
                yaml.safe_dump(asset, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DeliveryError,
                "POINT_CONVERSION_RULE_INVALID",
            ):
                SolutionDelivery(
                    InMemoryDeliveryRepository(),
                    platform_version="0.4.77",
                ).import_package(builder.build_archive(source))


if __name__ == "__main__":
    unittest.main()
