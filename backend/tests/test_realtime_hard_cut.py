"""The node runtime has one public truth seam: committed frame snapshots/deltas."""
from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RealtimeHardCutTest(unittest.TestCase):
    def test_legacy_realtime_routes_and_clients_are_removed(self) -> None:
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "backend/app/main.py",
                ROOT / "frontend/src/api/client.ts",
                ROOT / "frontend/src/components/NodeTagPanel.tsx",
                ROOT / "frontend/src/components/data-trunk/DataTrunkWorkspace.tsx",
            )
        )
        for legacy in (
            "/ws/telemetry",
            "/ws/entity-observations",
            "connectTelemetryWS",
            "connectEntityObservationWS",
        ):
            self.assertNotIn(legacy, sources)

        self.assertFalse((ROOT / "backend/app/api/websocket.py").exists())
        self.assertFalse(
            (ROOT / "frontend/src/components/NodeRealtimePanel.tsx").exists()
        )

    def test_runtime_surfaces_use_committed_frames_and_show_evidence(self) -> None:
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "frontend/src/components/NodeTagPanel.tsx",
                ROOT / "frontend/src/components/data-trunk/DataTrunkWorkspace.tsx",
                ROOT / "frontend/src/components/data-trunk/NodeTrunkOverview.tsx",
                ROOT / "frontend/src/components/data-trunk/EntityObservationCard.tsx",
            )
        )
        for required in (
            "fetchCommittedFrameSnapshot",
            "connectCommittedFrameStream",
            "frameSequence",
            "configurationRevision",
            "source_timestamp",
            "received_at",
            "source_digest",
        ):
            self.assertIn(required, sources)


if __name__ == "__main__":
    unittest.main()
