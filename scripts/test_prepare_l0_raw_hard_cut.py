"""CLI contract tests for the one-shot L0 raw hard cut."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import patch
from uuid import UUID

from scripts.prepare_l0_raw_hard_cut import main
from app.services.l0_raw_cutover import (
    CutoverBlocker,
    CutoverError,
    CutoverReport,
)


NODE_ID = UUID("10000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("10000000-0000-0000-0000-000000000002")
OUTPUT_ID = UUID("10000000-0000-0000-0000-000000000003")


class PrepareL0RawHardCutCliTest(unittest.TestCase):
    def invoke(self, arguments: list[str], **patches):
        stdout, stderr = StringIO(), StringIO()
        with (
            patch("scripts.prepare_l0_raw_hard_cut._connect", return_value=object()),
            patch.multiple("scripts.prepare_l0_raw_hard_cut", **patches),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_inspect_reports_blockers_and_returns_nonzero(self) -> None:
        report = CutoverReport(
            deterministic_output_ids=(),
            blockers=(
                CutoverBlocker(
                    node_id=NODE_ID,
                    processing_revision_id=REVISION_ID,
                    output_id=OUTPUT_ID,
                    code="BIT_FORMULA_REQUIRES_REVIEW",
                ),
            ),
            digest="a" * 64,
        )

        code, stdout, stderr = self.invoke(
            ["--inspect"], inspect_cutover=lambda _connection: report
        )

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("BIT_FORMULA_REQUIRES_REVIEW", payload["blockers"][0]["code"])
        self.assertEqual("a" * 64, payload["digest"])

    def test_apply_digest_mismatch_never_reports_a_write(self) -> None:
        def reject(*_args, **_kwargs):
            raise CutoverError("CUTOVER_DIGEST_MISMATCH")

        code, stdout, stderr = self.invoke(
            ["--apply", "--expected-digest", "b" * 64, "--actor", "release-v0.6.8"],
            apply_cutover=reject,
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("CUTOVER_DIGEST_MISMATCH", json.loads(stderr)["code"])

    def test_writer_active_blocks_apply_before_any_partial_success(self) -> None:
        def reject(*_args, **_kwargs):
            raise CutoverError("CUTOVER_WRITER_ACTIVE")

        code, stdout, stderr = self.invoke(
            ["--apply", "--expected-digest", "c" * 64, "--actor", "release-v0.6.8"],
            apply_cutover=reject,
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("CUTOVER_WRITER_ACTIVE", json.loads(stderr)["code"])

    def test_configuration_revision_mismatch_blocks_runtime_clear(self) -> None:
        def reject(*_args, **_kwargs):
            raise CutoverError("CUTOVER_CONFIGURATION_REVISION_MISMATCH")

        code, stdout, stderr = self.invoke(
            ["--clear-runtime", "--expected-config-revision", "7"],
            clear_runtime_test_data=reject,
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "CUTOVER_CONFIGURATION_REVISION_MISMATCH",
            json.loads(stderr)["code"],
        )

    def test_successful_apply_prints_only_new_immutable_revision_ids(self) -> None:
        new_revision = UUID("10000000-0000-0000-0000-000000000004")
        code, stdout, stderr = self.invoke(
            ["--apply", "--expected-digest", "d" * 64, "--actor", "release-v0.6.8"],
            apply_cutover=lambda *_args, **_kwargs: (new_revision,),
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(
            {"status": "applied", "processing_revision_ids": [str(new_revision)]},
            json.loads(stdout),
        )


if __name__ == "__main__":
    unittest.main()
