"""Solution imports retain declarative metric templates, never ordinary formulas."""
from __future__ import annotations

import hashlib
import io
import unittest
import zipfile

import yaml

from app.services.solution_delivery import InMemoryDeliveryRepository, SolutionDelivery
from app.services.solution_delivery_contracts import DeliveryError
from app.services.solution_business_metrics import business_metric_assets
from app.services.solution_point_processings import point_processing_assets


class SolutionPackageArchiveBusinessMetricTest(unittest.TestCase):
    @staticmethod
    def metric_asset() -> dict[str, object]:
        return {
            "schemaVersion": "zizu.business-metric/v1alpha1",
            "id": "ems.pv-energy-today",
            "revision": 1,
            "displayName": "今日光伏发电量",
            "targetNodeType": "SITE",
            "output": {
                "entityDefinition": "site.pv_energy_today",
                "dataType": "FLOAT",
                "unit": "kWh",
                "temporalSemantics": "windowed",
            },
            "window": {"kind": "aligned_daily"},
            "sources": [
                {
                    "method": "counter_delta",
                    "entityDefinition": "pv.energy_total",
                    "priority": 1,
                }
            ],
            "quality": {"minimumCoverage": 0.98},
            "correction": {"automaticHorizon": "7d"},
            "capabilities": {"controlEligible": False},
        }

    @staticmethod
    def _archive(metric: dict[str, object]) -> bytes:
        acceptance = (
            "schemaVersion: zizu.acceptance/v1alpha1\n"
            "id: acceptance.platform-liveness\n"
            "kind: platform_liveness\n"
            "required: true\n"
            "timeout: 5s\n"
        ).encode()
        metric_bytes = yaml.safe_dump(metric, allow_unicode=True, sort_keys=False).encode()
        manifest = {
            "schemaVersion": "zizu.solution/v1alpha1",
            "id": "org.zizu.metric-test",
            "version": "1.0.0",
            "displayName": "Metric test",
            "platform": {"version": ">=0.4.84,<0.5.0"},
            "assets": [
                {
                    "id": "acceptance.platform-liveness",
                    "kind": "acceptance",
                    "path": "acceptance/liveness.yaml",
                    "sha256": hashlib.sha256(acceptance).hexdigest(),
                },
                {
                    "id": "ems.pv-energy-today",
                    "kind": "business_metric_template",
                    "path": "business-metrics/pv-energy-today.yaml",
                    "sha256": hashlib.sha256(metric_bytes).hexdigest(),
                },
            ],
            "acceptance": ["acceptance.platform-liveness"],
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("solution.yaml", yaml.safe_dump(manifest, sort_keys=False))
            package.writestr("acceptance/liveness.yaml", acceptance)
            package.writestr("business-metrics/pv-energy-today.yaml", metric_bytes)
        return archive.getvalue()

    def test_import_retains_normalized_metric_template_without_public_formula_asset(self) -> None:
        package = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.84",
        ).import_package(self._archive(self.metric_asset()), "user:test-engineer")

        metrics = business_metric_assets(package)

        self.assertEqual(tuple(item.template_id for item in metrics), ("ems.pv-energy-today",))
        self.assertEqual(metrics[0].output_entity_definition_id, "site.pv_energy_today")
        self.assertEqual(point_processing_assets(package), ())

    def test_import_rejects_metric_expression_before_retaining_the_asset(self) -> None:
        metric = self.metric_asset() | {"expression": "eval(source)"}

        with self.assertRaisesRegex(DeliveryError, "BUSINESS_METRIC_ASSET_INVALID"):
            SolutionDelivery(
                InMemoryDeliveryRepository(),
                platform_version="0.4.84",
            ).import_package(self._archive(metric), "user:test-engineer")


if __name__ == "__main__":
    unittest.main()
