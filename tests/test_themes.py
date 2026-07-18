from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from mdreader.bootstrap import configure_gtk_input_method
from mdreader.services.themes import (
    DEFAULT_THEME_ID,
    THEMES,
    build_gtk_theme_css,
    get_theme,
    normalize_theme_id,
)


class ThemeTests(unittest.TestCase):
    def test_five_named_themes_have_complete_reader_tokens(self) -> None:
        self.assertEqual(len(THEMES), 5)
        self.assertEqual(len({theme.id for theme in THEMES}), 5)
        for theme in THEMES:
            tokens = theme.reader_tokens()
            self.assertEqual(len(tokens), 15)
            self.assertEqual(tokens["--paper"], theme.paper)
            self.assertEqual(tokens["--ink"], theme.ink)

    def test_legacy_theme_values_migrate_to_visible_themes(self) -> None:
        self.assertEqual(normalize_theme_id("system"), DEFAULT_THEME_ID)
        self.assertEqual(normalize_theme_id("warm-light"), "warm-paper")
        self.assertEqual(normalize_theme_id("warm-dark"), "plum-night")
        self.assertEqual(get_theme("missing").id, DEFAULT_THEME_ID)

    def test_generated_gtk_css_scopes_shell_library_and_ai_together(self) -> None:
        css = build_gtk_theme_css()
        for theme in THEMES:
            self.assertIn(f"window.{theme.css_class}", css)
            self.assertIn(f"{theme.css_class} .library-pane", css)
            self.assertIn(f"{theme.css_class} .ai-pane", css)
            self.assertIn(theme.paper, css)


class InputMethodBootstrapTests(unittest.TestCase):
    def test_fcitx_bridge_is_selected_before_gtk_when_user_has_no_override(self) -> None:
        environment = {
            "XMODIFIERS": "@im=fcitx",
            "QT_IM_MODULE": "fcitx",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "mdreader.bootstrap._fcitx_gtk4_module_available", return_value=True
        ):
            configure_gtk_input_method()
            self.assertEqual(os.environ.get("GTK_IM_MODULE"), "fcitx")

    def test_explicit_gtk_input_method_is_preserved(self) -> None:
        with patch.dict(os.environ, {"GTK_IM_MODULE": "wayland"}, clear=True), patch(
            "mdreader.bootstrap._fcitx_gtk4_module_available", return_value=True
        ):
            configure_gtk_input_method()
            self.assertEqual(os.environ["GTK_IM_MODULE"], "wayland")


if __name__ == "__main__":
    unittest.main()
