"""AI panel widget-level UX regression tests (docs/LLM_PROVIDER_MIGRATION_SPEC
§10.4 + swarm UX audit fixes).

Headless-friendly: the widget is only constructed when a GTK display exists;
otherwise the tests skip (never fail).
"""

from __future__ import annotations

import unittest
from pathlib import Path

GTK_AVAILABLE = False
try:
    import gi

    gi.require_version("Adw", "1")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False

if GTK_AVAILABLE:
    from mdreader.models import AiPanelState
    from mdreader.services.themes import get_theme
    from mdreader.widgets.ai_panel import AiPanel


@unittest.skipUnless(GTK_AVAILABLE, "Adw/Gtk typelibs not available")
class PanelComposerBehaviorTests(unittest.TestCase):
    """Regression: Edit-mode Enter must not wipe the user's draft."""

    def setUp(self) -> None:
        try:
            Gtk.init()
        except Exception as exc:  # no display (headless CI)
            self._gtk_skip = f"no GTK display: {exc}"
        else:
            self._gtk_skip = None

    def _make_panel(self):
        sent = []

        def on_send(text, mode):
            sent.append((text, mode))

        panel = AiPanel(
            on_jump_to_selection=lambda line: None,
            on_send=on_send,
            on_cancel=lambda: None,
            current_model="model-a",
            theme=get_theme("warm-paper"),
        )
        panel.set_document(Path("reader-sample.md"))
        panel.set_ai_state(AiPanelState.READY)
        return panel, sent

    def test_edit_mode_enter_keeps_draft_when_no_selection(self) -> None:
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        panel, sent = self._make_panel()
        panel._mode_group.set_active_name("edit")
        panel._prompt_entry.set_text("把标题改成中文")
        panel._on_send_requested(None)
        # the draft must survive and nothing may be sent
        self.assertEqual(panel._prompt_entry.get_text(), "把标题改成中文")
        self.assertEqual(sent, [])

    def test_ask_mode_enter_sends_and_clears(self) -> None:
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        panel, sent = self._make_panel()
        panel._mode_group.set_active_name("ask")
        panel._prompt_entry.set_text("你好")
        panel._on_send_requested(None)
        self.assertEqual(sent, [("你好", False)])
        self.assertEqual(panel._prompt_entry.get_text(), "")


if __name__ == "__main__":
    unittest.main()
