from __future__ import annotations

import unittest
from pathlib import Path

from mdreader.services.restore import (
    PresentedAction,
    RestoreRequest,
    make_restore,
    resolve_presented_action,
    restore_matches,
)


class MakeRestoreTests(unittest.TestCase):
    def test_make_restore_returns_none_for_empty_slug(self) -> None:
        self.assertIsNone(make_restore(Path("guide.md"), (1, 2), ""))

    def test_make_restore_returns_none_for_blank_slug(self) -> None:
        self.assertIsNone(make_restore(Path("guide.md"), (1, 2), " \t "))

    def test_make_restore_returns_request_for_non_empty_slug(self) -> None:
        request = make_restore(Path("guide.md"), (1, 2), "introduction")
        assert request is not None
        self.assertEqual(request.relative_path, Path("guide.md"))
        self.assertEqual(request.identity, (1, 2))
        self.assertEqual(request.slug, "introduction")


class RestoreMatchesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = RestoreRequest(Path("guide.md"), (1, 2), "introduction")

    def test_exact_match_is_true(self) -> None:
        self.assertTrue(restore_matches(self.request, Path("guide.md"), (1, 2)))

    def test_identity_mismatch_is_false(self) -> None:
        self.assertFalse(restore_matches(self.request, Path("guide.md"), (1, 99)))

    def test_path_mismatch_is_false(self) -> None:
        self.assertFalse(restore_matches(self.request, Path("other.md"), (1, 2)))

    def test_none_request_is_false(self) -> None:
        self.assertFalse(restore_matches(None, Path("guide.md"), (1, 2)))

    def test_none_arguments_do_not_raise(self) -> None:
        self.assertFalse(restore_matches(None, None, None))
        self.assertFalse(restore_matches(self.request, None, (1, 2)))
        self.assertFalse(restore_matches(self.request, Path("guide.md"), None))


class ResolvePresentedActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.restore = RestoreRequest(Path("guide.md"), (1, 2), "introduction")

    def test_fragment_wins_when_restore_also_matches(self) -> None:
        action = resolve_presented_action(
            (Path("guide.md"), "chapters"),
            self.restore,
            Path("guide.md"),
            (1, 2),
        )
        self.assertEqual(action, PresentedAction("fragment", "chapters"))

    def test_restore_used_when_no_fragment(self) -> None:
        action = resolve_presented_action(
            None,
            self.restore,
            Path("guide.md"),
            (1, 2),
        )
        self.assertEqual(action, PresentedAction("restore", "introduction"))

    def test_fragment_path_mismatch_falls_back_to_restore(self) -> None:
        action = resolve_presented_action(
            (Path("other.md"), "chapters"),
            self.restore,
            Path("guide.md"),
            (1, 2),
        )
        self.assertEqual(action, PresentedAction("restore", "introduction"))

    def test_fragment_path_mismatch_without_restore_match_is_none(self) -> None:
        action = resolve_presented_action(
            (Path("other.md"), "chapters"),
            self.restore,
            Path("third.md"),
            (1, 2),
        )
        self.assertEqual(action, PresentedAction("none"))

    def test_no_match_is_none(self) -> None:
        action = resolve_presented_action(
            None,
            self.restore,
            Path("other.md"),
            (1, 2),
        )
        self.assertEqual(action, PresentedAction("none"))

    def test_identity_mismatch_blocks_restore(self) -> None:
        action = resolve_presented_action(
            None,
            self.restore,
            Path("guide.md"),
            (99, 2),
        )
        self.assertEqual(action, PresentedAction("none"))

    def test_all_none_inputs_do_not_raise_and_return_none(self) -> None:
        action = resolve_presented_action(None, None, None, None)
        self.assertEqual(action, PresentedAction("none"))


if __name__ == "__main__":
    unittest.main()
