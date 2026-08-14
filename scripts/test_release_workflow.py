"""Static contract for the cloud immutable-image producer."""
from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-images.yml"


class ReleaseWorkflowTest(unittest.TestCase):
    def test_manual_workflow_builds_and_exports_only_digest_pinned_release_artifacts(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("docker/setup-buildx-action", workflow)
        self.assertIn("docker/login-action", workflow)
        self.assertIn("registry: ghcr.io", workflow)
        self.assertIn("scripts/build_release_images.py", workflow)
        self.assertIn("--repository ghcr.io/${{ github.repository }}", workflow)
        self.assertIn("--edge-proxy-image \"${{ inputs.edge_proxy_image }}\"", workflow)
        self.assertIn("scripts/build_reference_delivery.py", workflow)
        self.assertIn("pv-storage-charging-ems.zizu.zip", workflow)
        self.assertIn("sha256sum", workflow)
        self.assertIn("actions/upload-artifact", workflow)
        self.assertNotIn("docker compose", workflow)
        self.assertNotIn("record_release_lock.py", workflow)


if __name__ == "__main__":
    unittest.main()
