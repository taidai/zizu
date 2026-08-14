"""The public PV/storage/charging reference package must remain importable."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from app.services.solution_delivery import InMemoryDeliveryRepository, SolutionDelivery


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_reference_delivery.py"
SPEC = importlib.util.spec_from_file_location("build_reference_delivery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ReferenceEmsPackageTest(unittest.TestCase):
    def test_public_reference_package_is_reproducible_and_importable(self) -> None:
        first = builder.build_archive()
        second = builder.build_archive()
        self.assertEqual(first, second)
        imported = SolutionDelivery(
            InMemoryDeliveryRepository(), platform_version="0.4.77"
        ).import_package(first)
        self.assertEqual(imported.package_id, "org.zizu.pv-storage-charging-ems")
        self.assertEqual(imported.version, "1.0.0")
        self.assertEqual(len(imported.manifest["_entity_slots"]), 5)
        self.assertEqual(len(imported.manifest["_alarm_assets"]), 1)
        self.assertEqual(len(imported.manifest["_policy_assets"]), 1)
        self.assertEqual(
            set(imported.acceptance_ids),
            {
                "acceptance.platform-liveness", "acceptance.pcs", "acceptance.bms",
                "acceptance.pv", "acceptance.evse", "acceptance.meter", "acceptance.meter-history",
                "acceptance.grid-import-lifecycle", "acceptance.policy-grid-import-cap",
                "acceptance.release-lock",
            },
        )


if __name__ == "__main__":
    unittest.main()
