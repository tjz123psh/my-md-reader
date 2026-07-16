from __future__ import annotations

import os
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
        self.assertIn("atDocumentEnd", self.rendered.html)

    def test_remote_images_are_blocked_by_token_policy_and_csp(self) -> None:
        self.assertIn('src="about:blank"', self.rendered.html)
        self.assertIn('data-blocked-source="https://example.com/tracker.png"', self.rendered.html)
        self.assertIn("default-src 'none'", self.rendered.html)
        self.assertIn("img-src file: data:", self.rendered.html)

    def test_counts_source_lines(self) -> None:
        self.assertEqual(self.rendered.source_line_count, len(self.source.splitlines()))


if __name__ == "__main__":
    unittest.main()
