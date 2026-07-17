from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio

from mdreader.services.markdown import MarkdownRenderer


FIXTURE = Path(__file__).parent / "fixtures" / "reader-sample.md"


class MarkdownRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = FIXTURE.read_text(encoding="utf-8")
        cls.rendered = MarkdownRenderer().render(
            cls.source,
            title='Reader <fixture> & "safety"',
            zoom=130,
        )

    def test_extracts_unicode_outline_with_stable_duplicate_slugs(self) -> None:
        self.assertEqual(
            [(item.level, item.title, item.slug) for item in self.rendered.outline],
            [
                (1, "阅读器验收文档", "阅读器验收文档"),
                (2, "Mixed content", "mixed-content"),
                (2, "Mixed content", "mixed-content-1"),
                (3, "代码", "代码"),
            ],
        )

    def test_emits_source_line_metadata(self) -> None:
        self.assertIn('data-source-start="1"', self.rendered.html)
        self.assertRegex(self.rendered.html, r'data-source-end="\d+"')

    def test_raw_html_is_escaped(self) -> None:
        self.assertNotIn("<script>alert", self.rendered.html)
        self.assertIn("&lt;script&gt;alert", self.rendered.html)

    def test_fenced_code_is_highlighted_and_mapped(self) -> None:
        self.assertIn("language-python", self.rendered.html)
        self.assertIn("class=\"highlight", self.rendered.html)
        self.assertIn("print", self.rendered.html)

    def test_fence_without_language_is_rendered_instead_of_crashing(self) -> None:
        rendered = MarkdownRenderer().render("Before\n\n```\necho safe\n```\n")
        self.assertIn("echo safe", rendered.html)
        self.assertIn("<pre", rendered.html)
        self.assertNotIn('<pre class="source-fallback"', rendered.html)

    def test_unexpected_markdown_extension_error_falls_back_to_readable_source(self) -> None:
        renderer = MarkdownRenderer()
        with patch.object(renderer, "_decorate_tokens", side_effect=IndexError):
            rendered = renderer.render("# Still readable\n\nunusual **syntax**\n")
        self.assertIn('class="source-fallback"', rendered.html)
        self.assertIn("# Still readable", rendered.html)
        self.assertIn("unusual **syntax**", rendered.html)

    def test_gfm_table_rule_is_enabled(self) -> None:
        self.assertIn("<table", self.rendered.html)
        self.assertIn("<th", self.rendered.html)

    def test_links_are_hardened(self) -> None:
        self.assertIn('rel="noreferrer noopener"', self.rendered.html)

    def test_title_is_escaped_and_zoom_is_bounded(self) -> None:
        self.assertIn("Reader &lt;fixture&gt; &amp; &quot;safety&quot;", self.rendered.html)
        self.assertIn("--reader-zoom: 1.30", self.rendered.html)

    def test_reader_assets_are_bundled_inline(self) -> None:
        self.assertIn("window.mdReader", self.rendered.html)
        self.assertNotIn("<script src=", self.rendered.html)
        self.assertNotIn("@import url", self.rendered.html)

    def test_registered_gresource_is_used_without_source_assets(self) -> None:
        resource_file = os.environ.get("MDREADER_RESOURCE_FILE")
        if not resource_file:
            self.skipTest("built GResource is only available in the Meson test")

        resource = Gio.Resource.load(resource_file)
        Gio.resources_register(resource)
        try:
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("source assets must not be read"),
            ):
                rendered = MarkdownRenderer().render("# Installed resource")
        finally:
            Gio.resources_unregister(resource)

        self.assertIn("window.mdReader", rendered.html)
        self.assertIn("--reader-zoom: 1.00", rendered.html)

    def test_reader_reports_active_outline_heading(self) -> None:
        self.assertIn("messageHandlers?.outline", self.rendered.html)
        self.assertIn("requestAnimationFrame(reportActiveHeading)", self.rendered.html)
        self.assertIn("window.setTimeout(() =>", self.rendered.html)
        self.assertIn("Math.floor((low + high) / 2)", self.rendered.html)
        self.assertIn("atDocumentEnd", self.rendered.html)

    def test_wheel_bridge_keeps_touchpad_native_and_smooths_discrete_wheels(self) -> None:
        self.assertIn("const discreteWheel = (", self.rendered.html)
        self.assertIn("queueSmoothScroll(delta)", self.rendered.html)
        self.assertIn("requestAnimationFrame(animateSmoothScroll)", self.rendered.html)
        self.assertIn("event.preventDefault();", self.rendered.html)
        self.assertIn("messageHandlers?.zoom", self.rendered.html)
        self.assertIn('}, { passive: false });', self.rendered.html)

    def test_zoom_updates_body_and_uses_an_anchored_scroll_position(self) -> None:
        self.assertIn('target.style.setProperty("--reader-zoom"', self.rendered.html)
        self.assertIn("oldAnchor / oldHeight", self.rendered.html)
        self.assertIn("const zoomStep = 1", self.rendered.html)
        self.assertIn("const zoomImpulse = 5", self.rendered.html)
        self.assertIn("const zoomFrameStepLimit = 2", self.rendered.html)
        self.assertIn("requestAnimationFrame(flushZoomStep)", self.rendered.html)
        self.assertIn('behavior: "auto"', self.rendered.html)

    def test_keyboard_zoom_accelerators_are_not_registered(self) -> None:
        application = (
            Path(__file__).parents[1] / "src/mdreader/application.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('"win.zoom-in":', application)
        self.assertNotIn('"win.zoom-out":', application)
        self.assertNotIn('"win.zoom-reset":', application)

    def test_ai_panel_has_safe_close_modes_and_multiline_prompt(self) -> None:
        source = (
            Path(__file__).parents[1] / "src/mdreader/widgets/ai_panel.py"
        ).read_text(encoding="utf-8")
        self.assertIn("show_end_title_buttons=False", source)
        self.assertIn('action_name="win.hide-ai"', source)
        self.assertIn("Adw.ToggleGroup()", source)
        self.assertIn('name="ask"', source)
        self.assertIn('name="edit"', source)
        self.assertIn("Adw.Banner(", source)
        self.assertIn("Gdk.ModifierType.CONTROL_MASK", source)
        self.assertIn("Gtk.TextView(", source)
        self.assertNotIn("self._edit_toggle", source)

    def test_document_search_uses_a_header_popover_without_key_capture(self) -> None:
        root = Path(__file__).parents[1]
        window = (root / "src/mdreader/window.py").read_text(encoding="utf-8")
        blueprint = (root / "src/resources/ui/window.blp").read_text(
            encoding="utf-8"
        )
        self.assertIn("Gtk.MenuButton search_button", blueprint)
        self.assertIn("def _create_search_popover", window)
        self.assertNotIn("Gtk.SearchBar", window)
        self.assertNotIn("set_key_capture_widget", window)

    def test_remote_images_are_blocked_by_token_policy_and_csp(self) -> None:
        self.assertIn('src="about:blank"', self.rendered.html)
        self.assertIn('data-blocked-source="https://example.com/tracker.png"', self.rendered.html)
        self.assertIn("default-src 'none'", self.rendered.html)
        self.assertIn("img-src file: data:", self.rendered.html)

    def test_obsidian_image_embed_resolves_unique_workspace_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "guide.md"
            document.write_text("placeholder", encoding="utf-8")
            attachment = root / "附件" / "Pasted image.png"
            attachment.parent.mkdir()
            attachment.write_bytes(b"image")

            rendered = MarkdownRenderer().render(
                "![[Pasted image.png|617]]\n",
                document_path=document,
                workspace_root=root,
            )

        self.assertIn("<img", rendered.html)
        self.assertIn("Pasted%20image.png", rendered.html)
        self.assertNotIn("![[", rendered.html)

    def test_obsidian_image_embed_stays_text_when_attachment_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "guide.md"
            document.write_text("placeholder", encoding="utf-8")
            for directory in ("one", "two"):
                attachment = root / directory / "same.png"
                attachment.parent.mkdir()
                attachment.write_bytes(b"image")

            rendered = MarkdownRenderer().render(
                "![[same.png]]\n",
                document_path=document,
                workspace_root=root,
            )

        self.assertIn("![[same.png]]", rendered.html)

    def test_parent_image_path_is_allowed_only_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "docs" / "guide.md"
            document.parent.mkdir()
            document.write_text("placeholder", encoding="utf-8")
            image = root / "assets" / "inside.png"
            image.parent.mkdir()
            image.write_bytes(b"image")

            rendered = MarkdownRenderer().render(
                "![inside](../assets/inside.png)\n",
                document_path=document,
                workspace_root=root,
            )
            blocked = MarkdownRenderer().render(
                "![outside](../../outside.png)\n",
                document_path=document,
                workspace_root=root,
            )

        self.assertNotIn('src="about:blank"', rendered.html)
        self.assertIn('src="../assets/inside.png"', rendered.html)
        self.assertIn("data-blocked-source", blocked.html)

    def test_counts_source_lines(self) -> None:
        self.assertEqual(self.rendered.source_line_count, len(self.source.splitlines()))


if __name__ == "__main__":
    unittest.main()
