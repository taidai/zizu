from __future__ import annotations

import json
from pathlib import Path
import unittest


FIXTURE = Path(__file__).parent / "fixtures" / "en9_pcs_catalog.json"
TYPE_CODES = {
    "INT16": 3,
    "UINT16": 4,
    "BIT": 11,
}


class FakeNeuron:
    def __init__(self, *, interval: int = 1000) -> None:
        self.interval = interval
        self.mutating_calls: list[tuple] = []
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.tags = [
            {
                "name": point["sourceRawName"],
                "address": point["address"],
                "attribute": 1,
                "type": TYPE_CODES[point["wireDataType"]],
                "decimal": point["decimal"],
            }
            for point in fixture["points"]
        ]

    def get_groups(self, node_name: str) -> list[dict]:
        self.node_name = node_name
        return [{"name": "data", "interval": self.interval}]

    def get_tags(self, node_name: str, group_name: str) -> list[dict]:
        self.group_name = group_name
        return list(self.tags)


class NeuronPointProcessingCatalogTest(unittest.TestCase):
    def test_scan_normalizes_exact_en9_read_only_catalog_without_mutation(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog

        neuron = FakeNeuron()

        scan = NeuronPointCatalog(neuron).scan("EN9-PCS")

        self.assertEqual("EN9-PCS", scan.node_name)
        self.assertEqual(1000, scan.group_interval_ms)
        self.assertEqual(90, len(scan.points))
        self.assertTrue(all(point.read_only for point in scan.points))
        self.assertEqual((), scan.blockers)
        self.assertEqual(64, len(scan.digest))
        self.assertEqual([], neuron.mutating_calls)

    def test_scan_reports_duplicate_address_and_missing_interval_as_blockers(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog

        neuron = FakeNeuron(interval=0)
        neuron.tags.append(dict(neuron.tags[0]))

        scan = NeuronPointCatalog(neuron).scan("EN9-PCS")

        self.assertEqual(
            {
                "NEURON_GROUP_INTERVAL_MISSING",
                "NEURON_POINT_ADDRESS_DUPLICATE",
                "NEURON_POINT_NAME_DUPLICATE",
            },
            {item["code"] for item in scan.blockers},
        )
        self.assertEqual([], neuron.mutating_calls)


if __name__ == "__main__":
    unittest.main()
