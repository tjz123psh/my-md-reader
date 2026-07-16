from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mdreader.services.workspace import WorkspaceError, WorkspaceService


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "README.md").write_text("# Read me\n", encoding="utf-8")
        (self.root / "notes.txt").write_text("not markdown\n", encoding="utf-8")
        (self.root / ".hidden.md").write_text("hidden\n", encoding="utf-8")
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "guide.markdown").write_text("# Guide\n", encoding="utf-8")
        empty = self.root / "empty"
        empty.mkdir()
        self.service = WorkspaceService(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scan_only_returns_markdown_and_non_empty_directories(self) -> None:
        entries = self.service.scan()
        self.assertEqual([entry.name for entry in entries], ["docs", "README.md"])
        self.assertEqual(entries[0].children[0].relative_path, Path("docs/guide.markdown"))

    def test_resolve_rejects_parent_traversal(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.service.resolve_relative("../outside.md")

    def test_resolve_rejects_absolute_path(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.service.resolve_relative(self.root / "README.md")

    def test_validate_document_rejects_non_markdown(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.service.validate_document("notes.txt")

    def test_validate_document_returns_canonical_file(self) -> None:
        self.assertEqual(
            self.service.validate_document("docs/guide.markdown"),
            (self.root / "docs/guide.markdown").resolve(),
        )

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "outside.md"
            target.write_text("outside\n", encoding="utf-8")
            (self.root / "escape.md").symlink_to(target)
            with self.assertRaises(WorkspaceError):
                self.service.resolve_relative("escape.md")


if __name__ == "__main__":
    unittest.main()
