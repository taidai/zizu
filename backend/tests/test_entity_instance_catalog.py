import unittest
from uuid import UUID

from app.services.entity_instance_catalog import EntityInstanceDescriptor


class EntityInstanceCatalogContractTest(unittest.TestCase):
    def test_public_descriptor_preserves_confirmation_state(self) -> None:
        descriptor = EntityInstanceDescriptor(
            id=UUID(int=1), device_instance_id=UUID(int=2), slot_id="pcs-1",
            instance_key="pcs-1", device_category="pcs", device_display_name="PCS 1",
            definition_id="power", display_name="有功功率", data_type="number",
            unit="kW", direction="R", freshness_seconds=30, confirmed=False,
        )

        self.assertFalse(descriptor.public_dict()["confirmed"])
