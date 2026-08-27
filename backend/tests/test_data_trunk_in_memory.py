from __future__ import annotations

import inspect
import unittest

from app.services import data_trunk


class DataTrunkHardCutTest(unittest.TestCase):
    def test_only_frame_runtime_write_seam_is_public(self) -> None:
        source = inspect.getsource(data_trunk.DataTrunk)
        self.assertTrue(hasattr(data_trunk.DataTrunk, "accept"))
        self.assertTrue(hasattr(data_trunk.DataTrunk, "capture_tick"))
        self.assertTrue(hasattr(data_trunk.DataTrunk, "process_next"))
        self.assertFalse(hasattr(data_trunk.DataTrunk, "ingest"))
        self.assertFalse(hasattr(data_trunk, "InMemoryDataTrunkRepository"))
        self.assertNotIn("record_failure", source)
        self.assertNotIn("advance_freshness", source)


if __name__ == "__main__":
    unittest.main()
