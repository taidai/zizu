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
        self.requested_groups: list[str] = []

    def get_groups(self, node_name: str) -> list[dict]:
        self.node_name = node_name
        return [{"name": "data", "interval": self.interval}]

    def get_tags(self, node_name: str, group_name: str) -> list[dict]:
        self.group_name = group_name
        self.requested_groups.append(group_name)
        return list(self.tags)


class NeuronPointProcessingCatalogTest(unittest.TestCase):
    def test_zero_decimal_keeps_an_integer_register_integer(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog

        neuron = FakeNeuron()
        neuron.tags = [{
            "name": "总有功功率",
            "address": "1!416409",
            "attribute": 1,
            "type": 3,
            "decimal": 0.0,
        }]

        scan = NeuronPointCatalog(neuron).scan("tk_db")

        self.assertEqual("INT", scan.points[0].value_data_type)

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

    def test_scan_preserves_per_point_interval_without_globally_blocking_catalog(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog

        neuron = FakeNeuron(interval=0)
        neuron.tags.append(dict(neuron.tags[0]))

        scan = NeuronPointCatalog(neuron).scan("EN9-PCS")

        self.assertEqual((), scan.blockers)
        self.assertTrue(all(point.group_interval_ms == 0 for point in scan.points))
        self.assertEqual([], neuron.mutating_calls)

    def test_scan_selected_reads_only_the_groups_the_user_chose(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog

        neuron = FakeNeuron()
        neuron.get_groups = lambda _node: [
            {"name": "data", "interval": 1000},
            {"name": "status", "interval": 1000},
            {"name": "control", "interval": 1000},
        ]

        scan = NeuronPointCatalog(neuron).scan_selected(
            "EN9-PCS",
            ("status", "data"),
        )

        self.assertEqual(["data", "status"], neuron.requested_groups)
        self.assertEqual({"data", "status"}, {item.group for item in scan.points})


if __name__ == "__main__":
    unittest.main()
