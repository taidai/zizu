from __future__ import annotations

import unittest
from uuid import UUID

from app.services.point_processing_selectors import (
    PointProcessingSelectorError,
    Selector,
    freeze_selector,
)


SITE = UUID("10000000-0000-0000-0000-000000000001")
PCS_1 = UUID("20000000-0000-0000-0000-000000000001")
PCS_2 = UUID("20000000-0000-0000-0000-000000000002")


class PointProcessingSelectorTest(unittest.TestCase):
    @staticmethod
    def selector(cardinality: str = "many") -> Selector:
        return Selector(
            scope="descendants",
            node_type="PCS",
            entity_definition_id="pcs.active_power",
            cardinality=cardinality,
        )

    def test_freezes_sorted_descendant_l2_members(self) -> None:
        frozen = freeze_selector(
            selector=self.selector(),
            target_node_id=SITE,
            site_configuration_version=7,
            entity_instance_ids=(PCS_2, PCS_1),
        )

        self.assertEqual(frozen.entity_instance_ids, (PCS_1, PCS_2))
        self.assertEqual(len(frozen.digest), 64)

    def test_rejects_empty_required_collection(self) -> None:
        with self.assertRaises(PointProcessingSelectorError) as raised:
            freeze_selector(
                selector=self.selector(),
                target_node_id=SITE,
                site_configuration_version=7,
                entity_instance_ids=(),
            )

        self.assertEqual(raised.exception.code, "POINT_PROCESSING_INPUT_MISSING")

    def test_rejects_ambiguous_single_selector(self) -> None:
        with self.assertRaises(PointProcessingSelectorError) as raised:
            freeze_selector(
                selector=self.selector("one"),
                target_node_id=SITE,
                site_configuration_version=7,
                entity_instance_ids=(PCS_1, PCS_2),
            )

        self.assertEqual(raised.exception.code, "POINT_PROCESSING_INPUT_AMBIGUOUS")

    def test_rejects_duplicate_catalog_members(self) -> None:
        with self.assertRaises(PointProcessingSelectorError) as raised:
            freeze_selector(
                selector=self.selector(),
                target_node_id=SITE,
                site_configuration_version=7,
                entity_instance_ids=(PCS_1, PCS_1),
            )

        self.assertEqual(raised.exception.code, "POINT_PROCESSING_SELECTOR_INVALID")


if __name__ == "__main__":
    unittest.main()
