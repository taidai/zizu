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
            "quality": {"goodCoverage": 0.98, "minimumUsableCoverage": 0.8},
            "allowedLateness": "5m",
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

    def test_semantically_equivalent_templates_have_identical_digest_and_revision(self) -> None:
        first_raw = self.pv_energy_today()
        second_raw = self.pv_energy_today()
        second_raw["sources"] = list(reversed(second_raw["sources"]))
        first_raw["quality"] = {"goodCoverage": 1, "minimumUsableCoverage": 0.8}
        second_raw["quality"] = {"goodCoverage": 1.0, "minimumUsableCoverage": 0.8}

        first_template = parse_business_metric_asset(first_raw)
        second_template = parse_business_metric_asset(second_raw)

        self.assertEqual(first_template.sources, second_template.sources)
        self.assertEqual(first_template.content_digest, second_template.content_digest)
        self.assertEqual(
            compile_business_metric(
                first_template,
                self.counter_resolution(),
            ).processing_revision_id,
            compile_business_metric(
                second_template,
                self.counter_resolution(),
            ).processing_revision_id,
        )

    def test_template_requires_window_specific_automatic_correction_horizon(self) -> None:
        rolling = self.pv_energy_today()
        rolling["window"] = {"kind": "rolling", "duration": "15m"}
        rolling["correction"] = {"automaticHorizon": "6h"}

        self.assertEqual(
            parse_business_metric_asset(rolling).automatic_correction_horizon_seconds,
            6 * 60 * 60,
        )
        for raw in (
            self.pv_energy_today() | {"correction": {"automaticHorizon": "1h"}},
            rolling | {"correction": {"automaticHorizon": "7d"}},
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    BusinessMetricAssetError,
                    "BUSINESS_METRIC_ASSET_INVALID",
                ):
                    parse_business_metric_asset(raw)

    def test_template_freezes_good_and_minimum_usable_coverage_with_allowed_lateness(self) -> None:
        raw = self.pv_energy_today() | {
            "quality": {"goodCoverage": 1, "minimumUsableCoverage": 0.8},
            "allowedLateness": "5m",
        }

        template = parse_business_metric_asset(raw)

        self.assertEqual(template.quality.good_coverage, 1.0)
        self.assertEqual(template.quality.minimum_usable_coverage, 0.8)
        self.assertEqual(template.allowed_lateness_seconds, 5 * 60)
        self.assertEqual(
            template.content_digest,
            parse_business_metric_asset(
                raw | {
                    "quality": {
                        "goodCoverage": 1.0,
                        "minimumUsableCoverage": 0.8,
                    }
                }
            ).content_digest,
        )
        legacy = self.pv_energy_today() | {"quality": {"minimumCoverage": 0.98}}
        with self.assertRaisesRegex(BusinessMetricAssetError, "BUSINESS_METRIC_ASSET_INVALID"):
            parse_business_metric_asset(legacy)

    def test_compilation_rejects_non_uuid_resolved_entity_identity(self) -> None:
        template = parse_business_metric_asset(self.pv_energy_today())
        resolution = MetricSourceResolution(
            timezone="Asia/Shanghai",
            sources=(
                ResolvedMetricSource(
                    entity_instance_id="not-a-uuid",
                    entity_definition_id="pv.energy_total",
                    method="counter_delta",
                    data_type="FLOAT",
                    unit="kWh",
                    estimated=False,
                ),
            ),
        )

        with self.assertRaisesRegex(BusinessMetricAssetError, "BUSINESS_METRIC_ASSET_INVALID"):
            compile_business_metric(template, resolution)


if __name__ == "__main__":
    unittest.main()
