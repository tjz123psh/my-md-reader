from __future__ import annotations

import unittest

from mdreader.services.ai_markdown import AiMarkdownRenderer
from mdreader.services.themes import THEMES


class AiMarkdownRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = AiMarkdownRenderer()

    def test_parses_headings_lists_and_inline_emphasis(self) -> None:
        blocks = self.renderer.render(
            "## 原因\n\n- **布局**使用 `@home` 子卷\n- 第二项\n"
        )
        self.assertEqual([block.kind for block in blocks], ["heading", "list-item", "list-item"])
        self.assertEqual(blocks[0].level, 2)
        self.assertIn("<b>布局</b>", blocks[1].markup)
        self.assertIn('font_family="monospace"', blocks[1].markup)
        self.assertNotIn("**", blocks[1].markup)

    def test_parses_pipe_table_into_cells_instead_of_showing_delimiters(self) -> None:
        blocks = self.renderer.render("| 原因 | 说明 |\n| --- | --- |\n| 布局 | 灵活 |\n")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "table")
        self.assertEqual(len(blocks[0].rows), 2)
        self.assertTrue(blocks[0].rows[0][0].header)
        self.assertEqual(blocks[0].rows[1][1].markup, "灵活")

    def test_code_fence_uses_language_and_syntax_colors(self) -> None:
        blocks = self.renderer.render("```python\nname = 42\nprint(name)\n```\n")
        self.assertEqual(blocks[0].kind, "code")
        self.assertEqual(blocks[0].language, "python")
        self.assertIn("foreground=", blocks[0].markup)
        self.assertIn("#9A653F", blocks[0].markup)

    def test_raw_html_is_escaped_and_unsafe_links_are_not_activated(self) -> None:
        blocks = self.renderer.render(
            '<script>alert("x")</script>\n\n[bad](file:///etc/passwd) [good](https://archlinux.org)\n'
        )
        markup = "\n".join(block.markup for block in blocks)
        self.assertIn("&lt;script&gt;", markup)
        self.assertNotIn('<a href="file:', markup)
        self.assertIn('<a href="https://archlinux.org">good</a>', markup)

    def test_dark_palette_is_used_for_inline_code(self) -> None:
        blocks = self.renderer.render("Use `pacman`.", dark=True)
        self.assertIn("#B8C49B", blocks[0].markup)

    def test_each_reader_theme_controls_ai_inline_and_fenced_code_colors(self) -> None:
        for theme in THEMES:
            with self.subTest(theme=theme.id):
                inline = self.renderer.render("Use `pacman`.", theme=theme)
                fenced = self.renderer.render(
                    "```python\nanswer = 42\n```\n", theme=theme
                )
                self.assertIn(theme.syntax_string, inline[0].markup)
                self.assertIn(theme.code_bg, inline[0].markup)
                self.assertIn(theme.syntax_number, fenced[0].markup)


if __name__ == "__main__":
    unittest.main()
