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
    @staticmethod
    def _split_css_blocks(css: str) -> dict[str, str]:
        """Parse generated CSS into ``{selector: body}`` blocks.

        The generated CSS only uses flat `selector { body }` blocks (some
        selectors span lines, e.g. `window.x headerbar,\nwindow.x .ai-pane
        headerbar`), so a brace-depth scan is sufficient.
        """
        blocks: dict[str, str] = {}
        i = 0
        while True:
            open_i = css.find("{", i)
            if open_i < 0:
                break
            selector = css[i:open_i].strip()
            depth = 1
            j = open_i + 1
            while depth:
                if css[j] == "{":
                    depth += 1
                elif css[j] == "}":
                    depth -= 1
                j += 1
            blocks[selector] = css[open_i + 1 : j - 1].strip()
            i = j
        return blocks

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

    def test_generated_gtk_css_themes_the_dialog_surface(self) -> None:
        """Regression: the AI connection dialog (Adw.PreferencesDialog, CSS
        node `dialog`) fell back to libadwaita's near-black #1d1d20 in dark
        themes because the theme CSS was scoped to `window.{class}` only.
        Every theme must now emit a `dialog.{class}` rule that redefines the
        libadwaita color variables (--window-bg-color etc.) used by the
        dialog's inner `sheet`, cards and entries, plus an explicit
        `dialog.{class} sheet` rule painting the visible surface.

        Two invariants are asserted block-scoped (not global substring):
        (a) the dialog block itself carries THIS theme's palette variables,
        and (b) the dialog block must NOT paint a background — the outer
        `dialog` node spans the whole parent window and libadwaita uses it
        as the transparent dimming surface; painting it would hide the
        window content behind the dialog."""
        css = build_gtk_theme_css()
        blocks = self._split_css_blocks(css)
        for theme in THEMES:
            block = blocks.get(f"dialog.{theme.css_class}")
            self.assertIsNotNone(
                block, f"missing dialog.{theme.css_class} block"
            )
            self.assertIn(f"color: {theme.ink};", block)
            self.assertIn(f"--window-bg-color: {theme.shell};", block)
            self.assertIn(f"--window-fg-color: {theme.ink};", block)
            self.assertIn(f"--card-bg-color: {theme.paper};", block)
            self.assertIn(f"--dialog-bg-color: {theme.shell};", block)
            self.assertNotIn("background:", block)
            sheet = blocks.get(f"dialog.{theme.css_class} sheet")
            self.assertIsNotNone(
                sheet, f"missing dialog.{theme.css_class} sheet block"
            )
            self.assertIn(f"background: {theme.shell};", sheet)
            self.assertIn(f"color: {theme.ink};", sheet)
            # AlertDialog in-window: the outer `dialog.alert` node is the
            # full-window dimming surface (must NOT be painted), the visible
            # box is its `sheet` child.
            alert = blocks.get(f"dialog.{theme.css_class}.alert")
            self.assertIsNotNone(
                alert, f"missing dialog.{theme.css_class}.alert block"
            )
            self.assertNotIn("background:", alert)
            alert_sheet = blocks.get(f"dialog.{theme.css_class}.alert sheet")
            self.assertIsNotNone(
                alert_sheet,
                f"missing dialog.{theme.css_class}.alert sheet block",
            )
            self.assertIn(f"background: {theme.shell};", alert_sheet)


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

    def test_wayland_uses_gtk_native_input_method(self) -> None:
        environment = {
            "WAYLAND_DISPLAY": "wayland-1",
            "XMODIFIERS": "@im=fcitx",
            "QT_IM_MODULE": "fcitx",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "mdreader.bootstrap._fcitx_gtk4_module_available", return_value=True
        ):
            configure_gtk_input_method()
            self.assertIsNone(os.environ.get("GTK_IM_MODULE"))

    def test_explicit_gtk_input_method_is_preserved(self) -> None:
        with patch.dict(os.environ, {"GTK_IM_MODULE": "wayland"}, clear=True), patch(
            "mdreader.bootstrap._fcitx_gtk4_module_available", return_value=True
        ):
            configure_gtk_input_method()
            self.assertEqual(os.environ["GTK_IM_MODULE"], "wayland")


if __name__ == "__main__":
    unittest.main()
