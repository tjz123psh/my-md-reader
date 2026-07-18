from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mdreader.services.patches import PatchError, PatchService


class PatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "doc.md"
        self.path.write_text("one\ntwo\nthree\n", encoding="utf-8")
        self.service = PatchService(self.path.parent)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, start: int = 2, end: int = 2, replacement: str = "TWO") -> str:
        return json.dumps({"startLine": start, "endLine": end, "replacement": replacement})

    def base_hash(self, path: Path | None = None) -> str:
        target = path or self.path
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def propose(self, **kwargs):
        return self.service.propose(
            self.path,
            expected_start=2,
            expected_end=2,
            expected_base_hash=self.base_hash(),
            payload=self.payload(),
            **kwargs,
        )

    def test_propose_builds_a_bounded_unified_diff(self) -> None:
        proposal = self.propose()
        self.assertIn("-two", proposal.diff)
        self.assertIn("+TWO", proposal.diff)
        self.assertEqual(proposal.new_content, "one\nTWO\nthree\n")

    def test_rejects_model_line_expansion(self) -> None:
        with self.assertRaises(PatchError):
            self.service.propose(
                self.path,
                expected_start=2,
                expected_end=2,
                expected_base_hash=self.base_hash(),
                payload=self.payload(1, 3),
            )

    def test_apply_and_undo_are_atomic_from_callers_perspective(self) -> None:
        proposal = self.propose()
        self.service.apply(proposal)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "one\nTWO\nthree\n")
        self.assertTrue(self.service.undo())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "one\ntwo\nthree\n")

    def test_apply_rejects_external_change(self) -> None:
        proposal = self.propose()
        self.path.write_text("external\n", encoding="utf-8")
        with self.assertRaises(PatchError):
            self.service.apply(proposal)

    def test_undo_rejects_external_change(self) -> None:
        proposal = self.propose()
        self.service.apply(proposal)
        self.path.write_text("external\n", encoding="utf-8")
        with self.assertRaises(PatchError):
            self.service.undo()

    def test_propose_rejects_file_changed_during_model_response(self) -> None:
        original_hash = self.base_hash()
        self.path.write_text("one\nexternally changed\nthree\n", encoding="utf-8")
        with self.assertRaisesRegex(PatchError, "OpenCode 生成建议期间"):
            self.service.propose(
                self.path,
                expected_start=2,
                expected_end=2,
                expected_base_hash=original_hash,
                payload=self.payload(),
            )

    def test_propose_rejects_target_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.md"
            outside.write_text("one\ntwo\nthree\n", encoding="utf-8")
            with self.assertRaisesRegex(PatchError, "当前工作区之外"):
                self.service.propose(
                    outside,
                    expected_start=2,
                    expected_end=2,
                    expected_base_hash=self.base_hash(outside),
                    payload=self.payload(),
                )

    def test_propose_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.md"
            outside.write_text("one\ntwo\nthree\n", encoding="utf-8")
            link = self.path.parent / "linked.md"
            link.symlink_to(outside)
            with self.assertRaisesRegex(PatchError, "当前工作区之外"):
                self.service.propose(
                    link,
                    expected_start=2,
                    expected_end=2,
                    expected_base_hash=self.base_hash(outside),
                    payload=self.payload(),
                )

    def test_apply_preserves_crlf_line_endings(self) -> None:
        self.path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
        proposal = self.service.propose(
            self.path,
            expected_start=2,
            expected_end=2,
            expected_base_hash=self.base_hash(),
            payload=self.payload(replacement="TWO\nWITH DETAIL"),
        )
        self.service.apply(proposal)
        self.assertEqual(self.path.read_bytes(), b"one\r\nTWO\r\nWITH DETAIL\r\nthree\r\n")


if __name__ == "__main__":
    unittest.main()
