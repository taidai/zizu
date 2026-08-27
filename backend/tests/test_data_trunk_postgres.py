from __future__ import annotations

import inspect
import unittest

from app.services.data_trunk_postgres import PostgresFrameRepository


class PostgresDataTrunkHardCutTest(unittest.TestCase):
    def test_repository_exposes_only_frame_write_protocol(self) -> None:
        source = inspect.getsource(PostgresFrameRepository)
        self.assertTrue(hasattr(PostgresFrameRepository, "commit_pending"))
        self.assertTrue(hasattr(PostgresFrameRepository, "claim_next"))
        self.assertTrue(hasattr(PostgresFrameRepository, "complete"))
        self.assertFalse(hasattr(PostgresFrameRepository, "transact"))
        self.assertNotIn("t_l2_stream_outbox", source)


if __name__ == "__main__":
    unittest.main()
