"""Business-metric templates compile only from declarative, frozen inputs."""
from __future__ import annotations

import unittest
from uuid import UUID

from app.services.solution_business_metrics import (
    BusinessMetricAssetError,
    MetricSourceResolution,
    ResolvedMetricSource,
    compile_business_metric,
    parse_business_metric_asset,
)


class SolutionBusinessMetricTest(unittest.TestCase):
    @staticmethod
    def pv_energy_today() -> dict[str, object]:
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
                },
                {
                    "method": "power_integral",
                    "entityDefinition": "pv.active_power",
                    "priority": 2,
                },
            ],
            "quality": {"minimumCoverage": 0.98},
            "correction": {"automaticHorizon": "7d"},
            "capabilities": {"controlEligible": False},
        }

    @staticmethod
    def counter_resolution() -> MetricSourceResolution:
        return MetricSourceResolution(
            timezone="Asia/Shanghai",
            sources=(
                ResolvedMetricSource(
                    entity_instance_id=UUID("00000000-0000-0000-0000-000000000101"),
                    entity_definition_id="pv.energy_total",
                    method="counter_delta",
                    data_type="FLOAT",
                    unit="kWh",
                    estimated=False,
                ),
            ),
        )

    def test_daily_energy_template_compiles_to_stable_point_processing_revision(self) -> None:
        template = parse_business_metric_asset(self.pv_energy_today())

        first = compile_business_metric(template, self.counter_resolution())
        second = compile_business_metric(template, self.counter_resolution())

        self.assertEqual(first.processing_revision_id, second.processing_revision_id)
        self.assertEqual(first.temporal_semantics, "windowed")
        self.assertFalse(first.control_eligible)
        self.assertEqual(first.point_processing_asset.outputs[0].entity_definition_id, "site.pv_energy_today")
        self.assertEqual(first.point_processing_asset.inputs[0].source_key, "pv.energy_total")
        self.assertEqual(first.point_processing_asset.outputs[0].transform["kind"], "business_metric")

    def test_template_rejects_expression_and_unknown_aggregator(self) -> None:
        expression = self.pv_energy_today() | {"expression": "eval(source)"}
        unknown_aggregator = self.pv_energy_today()
        unknown_aggregator["sources"] = [
            {
                "method": "median",
                "entityDefinition": "pv.energy_total",
                "priority": 1,
            }
        ]

        for raw in (expression, unknown_aggregator):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    BusinessMetricAssetError,
                    "BUSINESS_METRIC_ASSET_INVALID",
                ):
                    parse_business_metric_asset(raw)

    def test_compilation_rejects_mutable_source_resolution(self) -> None:
        template = parse_business_metric_asset(self.pv_energy_today())
        frozen = self.counter_resolution()
        mutable_resolution = MetricSourceResolution(
            timezone=frozen.timezone,
            sources=list(frozen.sources),
        )

        with self.assertRaisesRegex(
            BusinessMetricAssetError,
            "BUSINESS_METRIC_ASSET_INVALID",
        ):
            compile_business_metric(template, mutable_resolution)

    def test_template_freezes_optional_energy_flow_direction(self) -> None:
        raw = self.pv_energy_today() | {
            "flow": {"direction": "negative", "normalize": True}
        }

        template = parse_business_metric_asset(raw)

        self.assertEqual(template.flow_direction.value, "negative")
        self.assertTrue(template.normalize_flow_direction)


if __name__ == "__main__":
    unittest.main()
