from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from app.services.solution_business_metrics import parse_business_metric_asset


class BusinessMetricDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        from app.services.business_metrics import (
            BusinessMetricDelivery,
            InMemoryBusinessMetricCatalog,
            InMemoryBusinessMetricRepository,
            MetricNode,
            MetricSourceCandidate,
        )

        self.site_id = UUID("00000000-0000-0000-0000-000000000101")
        self.child_id = UUID("00000000-0000-0000-0000-000000000102")
        self.counter_id = UUID("00000000-0000-0000-0000-000000000201")
        self.template = parse_business_metric_asset(
            {
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
                "quality": {
                    "goodCoverage": 0.98,
                    "minimumUsableCoverage": 0.80,
                },
                "allowedLateness": "5m",
                "correction": {"automaticHorizon": "7d"},
                "capabilities": {"controlEligible": False},
            }
        )
        self.catalog = InMemoryBusinessMetricCatalog(
            templates=(self.template,),
            nodes=(
                MetricNode(self.site_id, "SITE", None, "Asia/Shanghai", 30),
                MetricNode(self.child_id, "INVERTER", self.site_id, None),
            ),
            sources=(
                MetricSourceCandidate(
                    self.counter_id,
                    self.child_id,
                    "pv.energy_total",
                    "FLOAT",
                    "kWh",
                ),
            ),
        )
        self.repository = InMemoryBusinessMetricRepository(site_configuration_version=7)
        self.delivery = BusinessMetricDelivery(self.catalog, self.repository)

    def _preview_request(self):
        from app.services.business_metrics import PreviewMetricInstallation

        return PreviewMetricInstallation(
            node_id=self.site_id,
            template_id="ems.pv-energy-today",
            actor="user:engineer",
        )

    def _apply_command(self, plan):
        from app.services.business_metrics import ApplyMetricInstallation

        return ApplyMetricInstallation(
            plan_id=plan.id,
            expected_digest=plan.digest,
            actor="user:engineer",
            idempotency_key="install-pv-energy-today",
        )

    def test_preview_prefers_unique_counter_and_freezes_timezone_without_writing(self) -> None:
        plan = self.delivery.preview(self._preview_request())

        self.assertEqual(plan.timezone, "Asia/Shanghai")
        self.assertEqual(plan.sources[0].entity_instance_id, self.counter_id)
        self.assertEqual(plan.sources[0].method.value, "counter_delta")
        self.assertFalse(plan.sources[0].estimated)
        self.assertFalse(plan.blockers)
        self.assertEqual(self.repository.plan_count(), 0)
        self.assertEqual(self.repository.installation_count(), 0)

    def test_preview_uses_power_integral_only_when_counter_is_absent(self) -> None:
        from app.services.business_metrics import MetricSourceCandidate

        power_id = UUID("00000000-0000-0000-0000-000000000202")
        self.catalog.replace_sources(
            (
                MetricSourceCandidate(
                    power_id,
                    self.child_id,
                    "pv.active_power",
                    "FLOAT",
                    "kW",
                ),
            )
        )

        plan = self.delivery.preview(self._preview_request())

        self.assertEqual(plan.sources[0].entity_instance_id, power_id)
        self.assertEqual(plan.sources[0].method.value, "power_integral")
        self.assertTrue(plan.sources[0].estimated)
        self.assertFalse(plan.blockers)

    def test_counter_method_wins_even_when_power_has_lower_numeric_priority(self) -> None:
        from dataclasses import replace

        from app.services.business_metric_contracts import (
            MetricAggregator,
            MetricSourceOption,
        )
        from app.services.business_metrics import (
            BusinessMetricDelivery,
            InMemoryBusinessMetricCatalog,
            MetricNode,
            MetricSourceCandidate,
        )

        power_id = UUID("00000000-0000-0000-0000-000000000202")
        template = replace(
            self.template,
            sources=(
                MetricSourceOption(
                    MetricAggregator.POWER_INTEGRAL, "pv.active_power", 1
                ),
                MetricSourceOption(
                    MetricAggregator.COUNTER_DELTA, "pv.energy_total", 2
                ),
            ),
        )
        catalog = InMemoryBusinessMetricCatalog(
            templates=(template,),
            nodes=(
                MetricNode(self.site_id, "SITE", None, "Asia/Shanghai", 30),
                MetricNode(self.child_id, "INVERTER", self.site_id, None),
            ),
            sources=self.catalog.sources
            + (
                MetricSourceCandidate(
                    power_id,
                    self.child_id,
                    "pv.active_power",
                    "FLOAT",
                    "kW",
                ),
            ),
        )

        plan = BusinessMetricDelivery(catalog, self.repository).preview(
            self._preview_request()
        )

        self.assertEqual(plan.sources[0].entity_instance_id, self.counter_id)
        self.assertEqual(plan.sources[0].method.value, "counter_delta")

    def test_preview_marks_ambiguous_counter_as_blocker_without_falling_back(self) -> None:
        from app.services.business_metrics import MetricSourceCandidate

        self.catalog.replace_sources(
            self.catalog.sources
            + (
                MetricSourceCandidate(
                    uuid4(),
                    self.child_id,
                    "pv.energy_total",
                    "FLOAT",
                    "kWh",
                ),
                MetricSourceCandidate(
                    uuid4(),
                    self.child_id,
                    "pv.active_power",
                    "FLOAT",
                    "kW",
                ),
            )
        )

        plan = self.delivery.preview(self._preview_request())

        self.assertEqual(plan.status, "blocked")
        self.assertEqual([item["code"] for item in plan.blockers], ["BUSINESS_METRIC_SOURCE_AMBIGUOUS"])
        self.assertEqual(plan.sources, ())

    def test_apply_rejects_changed_sources_without_partial_write(self) -> None:
        from app.services.business_metrics import BusinessMetricError, MetricSourceCandidate

        plan = self.delivery.preview(self._preview_request())
        self.catalog.replace_sources(
            self.catalog.sources
            + (
                MetricSourceCandidate(
                    uuid4(),
                    self.child_id,
                    "pv.energy_total",
                    "FLOAT",
                    "kWh",
                ),
            )
        )

        with self.assertRaisesRegex(BusinessMetricError, "BUSINESS_METRIC_PLAN_STALE"):
            self.delivery.apply(self._apply_command(plan))

        self.assertEqual(self.repository.installation_count(), 0)
        self.assertEqual(self.repository.audit_count(), 0)

    def test_apply_is_idempotent_and_freezes_plan_contract(self) -> None:
        plan = self.delivery.preview(self._preview_request())

        first = self.delivery.apply(self._apply_command(plan))
        second = self.delivery.apply(self._apply_command(plan))

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.entity_instance_id, second.entity_instance_id)
        self.assertEqual(first.timezone, "Asia/Shanghai")
        self.assertEqual(first.site_configuration_version, 7)
        self.assertEqual(self.repository.installation_count(), 1)
        self.assertEqual(self.repository.audit_count(), 1)

    def test_preview_blocks_non_numeric_or_dimensionally_wrong_source(self) -> None:
        from dataclasses import replace

        from app.services.business_metrics import BusinessMetricDelivery

        original = self.catalog.sources[0]
        cases = (
            replace(original, data_type="BOOL"),
            replace(original, unit="kW"),
        )
        for incompatible in cases:
            with self.subTest(data_type=incompatible.data_type, unit=incompatible.unit):
                self.catalog.replace_sources((incompatible,))
                plan = BusinessMetricDelivery(
                    self.catalog, self.repository
                ).preview(self._preview_request())
                self.assertEqual(plan.status, "blocked")
                self.assertEqual(
                    plan.blockers,
                    ({"code": "BUSINESS_METRIC_SOURCE_INCOMPATIBLE"},),
                )

    def test_preview_turns_invalid_iana_timezone_into_stable_blocker(self) -> None:
        from app.services.business_metrics import (
            BusinessMetricDelivery,
            InMemoryBusinessMetricCatalog,
            MetricNode,
        )

        catalog = InMemoryBusinessMetricCatalog(
            templates=(self.template,),
            nodes=(
                MetricNode(self.site_id, "SITE", None, "Mars/Olympus", 30),
                MetricNode(self.child_id, "INVERTER", self.site_id, None),
            ),
            sources=self.catalog.sources,
        )

        plan = BusinessMetricDelivery(catalog, self.repository).preview(
            self._preview_request()
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            plan.blockers,
            ({"code": "BUSINESS_METRIC_TIMEZONE_INVALID"},),
        )

    def test_preview_turns_path_like_timezone_keys_into_stable_blockers(self) -> None:
        from app.services.business_metrics import (
            BusinessMetricDelivery,
            InMemoryBusinessMetricCatalog,
            MetricNode,
        )

        for timezone in ("../UTC", "/usr/share/zoneinfo/UTC"):
            with self.subTest(timezone=timezone):
                catalog = InMemoryBusinessMetricCatalog(
                    templates=(self.template,),
                    nodes=(
                        MetricNode(self.site_id, "SITE", None, timezone, 30),
                        MetricNode(
                            self.child_id,
                            "INVERTER",
                            self.site_id,
                            None,
                        ),
                    ),
                    sources=self.catalog.sources,
                )

                plan = BusinessMetricDelivery(catalog, self.repository).preview(
                    self._preview_request()
                )

                self.assertEqual(plan.status, "blocked")
                self.assertEqual(
                    plan.blockers,
                    ({"code": "BUSINESS_METRIC_TIMEZONE_INVALID"},),
                )


if __name__ == "__main__":
    unittest.main()
