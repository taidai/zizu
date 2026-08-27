from __future__ import annotations

import inspect
import unittest

from app.services.data_trunk_postgres import PostgresFrameRepository


class FrameBulkWriteHardCutTest(unittest.TestCase):
    def test_frame_transactions_own_bulk_history_and_latest_writes(self) -> None:
        source = inspect.getsource(PostgresFrameRepository)
        self.assertIn("_insert_frame_l0", source)
        self.assertIn("_advance_frame_l0_latest", source)
        self.assertIn("_insert_frame_l2", source)
        self.assertIn("_advance_frame_l2_latest", source)
        self.assertNotIn("_insert_outbox", source)


if __name__ == "__main__":
    unittest.main()
