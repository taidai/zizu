"""Regression tests for ZiZu's human-friendly decimal version convention."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import bump_version


REPO_ROOT = Path(__file__).resolve().parents[1]


class BumpVersionTest(unittest.TestCase):
    def test_patch_carries_into_minor_after_nine(self) -> None:
        self.assertEqual((0, 5, 0), bump_version.bump(0, 4, 9, "patch"))

    def test_minor_carries_into_major_after_nine(self) -> None:
        self.assertEqual((1, 0, 0), bump_version.bump(0, 9, 9, "patch"))

    def test_patch_before_nine_increments_normally(self) -> None:
        self.assertEqual((0, 4, 9), bump_version.bump(0, 4, 8, "patch"))

    def test_read_version_rejects_disagreeing_version_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "VERSION"
            second = root / "backend" / "app" / "VERSION"
            second.parent.mkdir(parents=True)
            first.write_text("0.5.0\n", encoding="utf-8")
            second.write_text("0.4.9\n", encoding="utf-8")

            with patch.object(bump_version, "version_files", return_value=[first, second]):
                with self.assertRaisesRegex(ValueError, "disagree"):
                    bump_version.read_version()

    def test_package_metadata_updates_package_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package.json"
            lock = root / "package-lock.json"
            package.write_text('{"name":"zizu","version":"0.4.9"}\n', encoding="utf-8")
            lock.write_text(
                '{"name":"zizu","version":"0.4.9","packages":{"":{"version":"0.4.9"}}}\n',
                encoding="utf-8",
            )

            with (
                patch.object(bump_version, "PACKAGE_JSON", package),
                patch.object(bump_version, "PACKAGE_LOCK_JSON", lock, create=True),
            ):
                bump_version.update_package_json("0.5.0")

            self.assertEqual("0.5.0", json.loads(package.read_text(encoding="utf-8"))["version"])
            lock_data = json.loads(lock.read_text(encoding="utf-8"))
            self.assertEqual("0.5.0", lock_data["version"])
            self.assertEqual("0.5.0", lock_data["packages"][""]["version"])

    def test_repository_version_sources_are_synchronized(self) -> None:
        expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(
            expected,
            (REPO_ROOT / "backend" / "app" / "VERSION").read_text(encoding="utf-8").strip(),
        )


if __name__ == "__main__":
    unittest.main()
