from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mdreader.services.workspace import (
    LocalDocumentLinkError,
    WorkspaceError,
    WorkspaceService,
    parse_local_document_uri,
)


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

    def test_scan_skips_an_unreadable_nested_directory(self) -> None:
        blocked = self.root / "blocked"
        blocked.mkdir()
        original_iterdir = Path.iterdir

        def guarded_iterdir(directory: Path):
            if directory == blocked:
                raise PermissionError("permission denied")
            return original_iterdir(directory)

        with patch.object(Path, "iterdir", guarded_iterdir):
            entries = self.service.scan()

        self.assertEqual([entry.name for entry in entries], ["docs", "README.md"])

    def test_scan_still_reports_an_unreadable_workspace_root(self) -> None:
        with patch.object(Path, "iterdir", side_effect=PermissionError("permission denied")):
            with self.assertRaisesRegex(WorkspaceError, "无法读取"):
                self.service.scan()

    def test_local_document_uri_preserves_decoded_path_and_fragment(self) -> None:
        target = parse_local_document_uri(
            (self.root / "docs/guide.markdown").as_uri() + "#%E4%BB%8B%E7%BB%8D"
        )

        self.assertEqual(target.path, self.root / "docs/guide.markdown")
        self.assertEqual(target.fragment, "介绍")

    def test_local_document_uri_rejects_authorities_queries_and_non_file_schemes(self) -> None:
        invalid = (
            "file://example.test/tmp/readme.md",
            "file:///tmp/readme.md?download=1",
            "https://example.test/readme.md",
            "file:relative.md",
        )

        for uri in invalid:
            with self.subTest(uri=uri):
                with self.assertRaises(LocalDocumentLinkError):
                    parse_local_document_uri(uri)

    def test_local_document_uri_rejects_unsafe_fragments(self) -> None:
        for fragment in ("x" * 513, "section%0Aother"):
            with self.subTest(fragment=fragment):
                with self.assertRaises(LocalDocumentLinkError):
                    parse_local_document_uri(f"file:///tmp/readme.md#{fragment}")

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

    def test_snapshot_documents_contain_only_markdown_files(self) -> None:
        snapshot = self.service.scan_snapshot()
        self.assertEqual(
            {document.relative_path for document in snapshot.documents},
            {Path("docs/guide.markdown"), Path("README.md")},
        )

    def test_snapshot_directories_include_root_and_every_descendant_directory(self) -> None:
        snapshot = self.service.scan_snapshot()
        self.assertIn(self.service.root, snapshot.directories)
        self.assertIn(self.service.root / "docs", snapshot.directories)
        self.assertIn(self.service.root / "empty", snapshot.directories)

    def test_snapshot_fingerprint_tracks_document_modification(self) -> None:
        before = self.service.scan_snapshot()
        original = next(
            document
            for document in before.documents
            if document.relative_path == Path("README.md")
        )

        target = self.root / "README.md"
        target.write_text("# Read me - longer than before\n", encoding="utf-8")
        status = target.stat()
        os.utime(target, ns=(status.st_atime_ns, status.st_mtime_ns + 5_000_000_000))

        after = self.service.scan_snapshot()
        modified = next(
            document
            for document in after.documents
            if document.relative_path == Path("README.md")
        )
        self.assertNotEqual(modified.fingerprint, original.fingerprint)
        self.assertEqual(modified.identity, original.identity)

    def test_snapshot_rename_keeps_identity_and_updates_relative_path(self) -> None:
        before = self.service.scan_snapshot()
        original = next(
            document
            for document in before.documents
            if document.relative_path == Path("README.md")
        )

        os.replace(self.root / "README.md", self.root / "renamed.md")

        after = self.service.scan_snapshot()
        renamed = next(
            document
            for document in after.documents
            if document.relative_path == Path("renamed.md")
        )
        self.assertEqual(renamed.identity, original.identity)
        self.assertNotEqual(renamed.relative_path, original.relative_path)

    def test_snapshot_drops_a_deleted_document(self) -> None:
        self.assertTrue(
            any(
                document.relative_path == Path("docs/guide.markdown")
                for document in self.service.scan_snapshot().documents
            )
        )

        os.unlink(self.root / "docs" / "guide.markdown")

        after = self.service.scan_snapshot()
        self.assertFalse(
            any(
                document.relative_path == Path("docs/guide.markdown")
                for document in after.documents
            )
        )

    def test_snapshot_discovers_new_directory_and_document(self) -> None:
        before = self.service.scan_snapshot()
        self.assertNotIn(self.service.root / "nested", before.directories)

        nested = self.root / "nested"
        nested.mkdir()
        (nested / "deep.md").write_text("# Deep\n", encoding="utf-8")

        after = self.service.scan_snapshot()
        self.assertIn(self.service.root / "nested", after.directories)
        self.assertTrue(
            any(
                document.relative_path == Path("nested/deep.md")
                for document in after.documents
            )
        )

    def test_snapshot_skips_an_unreadable_nested_directory(self) -> None:
        blocked = self.root / "blocked"
        blocked.mkdir()
        (blocked / "secret.md").write_text("# Secret\n", encoding="utf-8")
        original_iterdir = Path.iterdir

        def guarded_iterdir(directory: Path):
            if directory == blocked:
                raise PermissionError("permission denied")
            return original_iterdir(directory)

        with patch.object(Path, "iterdir", guarded_iterdir):
            snapshot = self.service.scan_snapshot()

        self.assertNotIn(self.service.root / "blocked", snapshot.directories)
        self.assertFalse(
            any(
                document.relative_path == Path("blocked/secret.md")
                for document in snapshot.documents
            )
        )

    def test_snapshot_still_raises_for_an_unreadable_root(self) -> None:
        with patch.object(Path, "iterdir", side_effect=PermissionError("permission denied")):
            with self.assertRaisesRegex(WorkspaceError, "无法读取"):
                self.service.scan_snapshot()

    def test_snapshot_survives_a_transient_stat_failure_on_one_document(self) -> None:
        broken = self.root / "broken.md"
        broken.write_text("# Broken\n", encoding="utf-8")
        original_stat = Path.stat

        def guarded_stat(path: Path, *args, **kwargs):
            if path == broken:
                raise PermissionError("simulated stat failure")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", guarded_stat):
            snapshot = self.service.scan_snapshot()

        self.assertEqual(
            {document.relative_path for document in snapshot.documents},
            {Path("docs/guide.markdown"), Path("README.md")},
        )
        self.assertFalse(
            any(
                document.relative_path == Path("broken.md")
                for document in snapshot.documents
            )
        )


if __name__ == "__main__":
    unittest.main()
