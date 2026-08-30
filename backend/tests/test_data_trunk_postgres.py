from __future__ import annotations

import inspect
import unittest

from app.services.data_trunk_outbox import PostgresFrameOutboxRepository
from app.services.data_trunk_postgres import PostgresFrameRepository


class PostgresDataTrunkHardCutTest(unittest.TestCase):
    def test_repository_exposes_only_frame_write_protocol(self) -> None:
        source = inspect.getsource(PostgresFrameRepository)
        self.assertTrue(hasattr(PostgresFrameRepository, "commit_pending"))
        self.assertTrue(hasattr(PostgresFrameRepository, "claim_next"))
        self.assertTrue(hasattr(PostgresFrameRepository, "complete"))
        self.assertFalse(hasattr(PostgresFrameRepository, "transact"))
        self.assertNotIn("t_l2_stream_outbox", source)

    def test_only_replay_safe_coordination_commits_are_asynchronous(self) -> None:
        async_commit = "SET LOCAL synchronous_commit TO OFF"

        self.assertIn(
            async_commit,
            inspect.getsource(PostgresFrameRepository.claim_next),
        )
        self.assertIn(
            async_commit,
            inspect.getsource(PostgresFrameOutboxRepository.claim_unpublished),
        )
        self.assertIn(
            async_commit,
            inspect.getsource(PostgresFrameOutboxRepository._finish),
        )

        # Captured L0 facts and committed L2 results remain synchronously durable.
        self.assertNotIn(
            async_commit,
            inspect.getsource(PostgresFrameRepository.commit_pending),
        )
        self.assertNotIn(
            async_commit,
            inspect.getsource(PostgresFrameRepository.complete),
        )


if __name__ == "__main__":
    unittest.main()
