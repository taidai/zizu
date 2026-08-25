import unittest
from uuid import UUID

from app.services.entity_instance_catalog import EntityInstanceDescriptor


class EntityInstanceCatalogContractTest(unittest.TestCase):
    def test_public_descriptor_exposes_direct_node_ownership(self) -> None:
        descriptor = EntityInstanceDescriptor(
            id=UUID(int=1), node_id=UUID(int=2), node_type="pcs",
            node_display_name="PCS 1",
            definition_id="power", display_name="有功功率", data_type="number",
            unit="kW", direction="R", freshness_seconds=30, confirmed=False,
        )

        value = descriptor.public_dict()
        self.assertFalse(value["confirmed"])
        self.assertEqual(value["node_id"], str(UUID(int=2)))
        self.assertEqual(value["node_type"], "pcs")
        self.assertNotIn("device_instance_id", value)
        self.assertNotIn("slot_id", value)
