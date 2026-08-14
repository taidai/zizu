"""Build-definition regression checks for the immutable platform image."""
from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"


class ReleaseImageBuildTest(unittest.TestCase):
    def test_uses_the_primary_official_image_registry(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertNotIn("docker.m.daocloud.io", dockerfile)
        self.assertIn("FROM node:22-alpine AS frontend-builder", dockerfile)
        self.assertIn("FROM python:3.12-slim", dockerfile)


if __name__ == "__main__":
    unittest.main()
