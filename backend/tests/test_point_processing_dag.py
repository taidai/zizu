from __future__ import annotations

import unittest
from uuid import UUID

from app.services.point_processing_dag import (
    PointProcessingDagError,
    validate_processing_dag,
)


def node(index: int) -> UUID:
    return UUID(int=index)


class PointProcessingDagTest(unittest.TestCase):
    def test_returns_stable_topological_order_and_depth(self) -> None:
        summary = validate_processing_dag(
            existing_edges=((node(2), node(4)), (node(1), node(3))),
            planned_edges=((node(3), node(4)),),
        )

        self.assertEqual(summary.order, (node(1), node(2), node(3), node(4)))
        self.assertEqual(summary.max_depth, 3)
        self.assertEqual(len(summary.digest), 64)

    def test_rejects_cycle_created_by_planned_edge(self) -> None:
        with self.assertRaises(PointProcessingDagError) as raised:
            validate_processing_dag(
                existing_edges=((node(1), node(2)), (node(2), node(3))),
                planned_edges=((node(3), node(1)),),
            )

        self.assertEqual(raised.exception.code, "POINT_PROCESSING_DAG_CYCLE")

    def test_rejects_more_than_eight_node_layers(self) -> None:
        with self.assertRaises(PointProcessingDagError) as raised:
            validate_processing_dag(
                existing_edges=tuple((node(index), node(index + 1)) for index in range(1, 8)),
                planned_edges=((node(8), node(9)),),
                max_depth=8,
            )

        self.assertEqual(
            raised.exception.code,
            "POINT_PROCESSING_DAG_DEPTH_EXCEEDED",
        )

    def test_rejects_self_dependency(self) -> None:
        with self.assertRaises(PointProcessingDagError) as raised:
            validate_processing_dag(existing_edges=(), planned_edges=((node(1), node(1)),))

        self.assertEqual(raised.exception.code, "POINT_PROCESSING_DAG_CYCLE")


if __name__ == "__main__":
    unittest.main()
