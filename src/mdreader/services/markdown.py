from __future__ import annotations

import html
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from mdreader.models import OutlineItem, RenderedDocument


class MarkdownUnavailableError(RuntimeError):
    pass


_SPACE_OR_DASH = re.compile(r"[\s\-]+", re.UNICODE)


class _Slugger:
    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def slug(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text).strip().casefold()
        characters = [
            character
            for character in normalized
            if character.isalnum() or character in {" ", "-", "_"}
        ]
        base = _SPACE_OR_DASH.sub("-", "".join(characters)).strip("-") or "section"
        count = self._seen.get(base, 0)
        self._seen[base] = count + 1
        return base if count == 0 else f"{base}-{count}"


class MarkdownRenderer:
    def __init__(self, assets_root: str | Path | None = None) -> None:
        self.assets_root = Path(assets_root) if assets_root is not None else None

    def render(self, source: str, *, title: str = "Document", zoom: int = 100) -> RenderedDocument:
        try:
            from markdown_it import MarkdownIt
            from markdown_it.rules_inline import StateInline
        except ImportError as error:
            raise MarkdownUnavailableError(
                "markdown-it-py is required to render Markdown"
            ) from error

        del StateInline  # Imported as an early dependency/version sanity check.
        markdown = MarkdownIt(
            "commonmark",
            {
                "html": False,
                "linkify": True,
                "typographer": True,
                "breaks": False,
            },
        ).enable("table")

        self._install_fence_renderer(markdown)
        tokens = markdown.parse(source)
        outline = self._decorate_tokens(tokens)
        body = markdown.renderer.render(tokens, markdown.options, {})
        document = self._html_document(body, title=title, zoom=zoom)
        return RenderedDocument(
            html=document,
            outline=tuple(outline),
            source_line_count=len(source.splitlines()),
            source=source,
        )

    def _decorate_tokens(self, tokens: list[object]) -> list[OutlineItem]:
        outline: list[OutlineItem] = []
        slugger = _Slugger()

        for index, token in enumerate(tokens):
            mapping = getattr(token, "map", None)
            if mapping and getattr(token, "nesting", 0) == 1:
                token.attrSet("data-source-start", str(mapping[0] + 1))
                token.attrSet("data-source-end", str(mapping[1]))

            self._harden_links(token)

            if getattr(token, "type", "") != "heading_open":
                continue
            if index + 1 >= len(tokens):
                continue
            inline = tokens[index + 1]
            title = getattr(inline, "content", "").strip()
            level = int(getattr(token, "tag", "h1")[1:])
            slug = slugger.slug(title)
            token.attrSet("id", slug)
            start_line = mapping[0] + 1 if mapping else 0
            outline.append(OutlineItem(level, title, slug, start_line))

        return outline

    @classmethod
    def _harden_links(cls, token: object) -> None:
        if getattr(token, "type", "") == "link_open":
            token.attrSet("rel", "noreferrer noopener")
        if getattr(token, "type", "") == "image":
            source = token.attrGet("src") or ""
            parsed = urlsplit(source)
            parts = Path(parsed.path).parts
            if parsed.scheme or source.startswith(("/", "//")) or ".." in parts:
                token.attrSet("src", "about:blank")
                token.attrSet("data-blocked-source", source)
                token.attrSet("title", "Remote or out-of-folder image blocked")
        for child in getattr(token, "children", None) or ():
            cls._harden_links(child)

    @staticmethod
    def _install_fence_renderer(markdown: object) -> None:
        def render_fence(tokens: list[object], index: int, options: dict, env: dict) -> str:
            del options, env
            token = tokens[index]
            code = getattr(token, "content", "")
            language = getattr(token, "info", "").strip().split(maxsplit=1)[0]
            mapping = getattr(token, "map", None)
            source_attributes = ""
            if mapping:
                source_attributes = (
                    f' data-source-start="{mapping[0] + 1}"'
                    f' data-source-end="{mapping[1]}"'
                )
            try:
                from pygments import highlight
                from pygments.formatters import HtmlFormatter
                from pygments.lexers import TextLexer, get_lexer_by_name
                from pygments.util import ClassNotFound

                try:
                    lexer = get_lexer_by_name(language) if language else TextLexer()
                except ClassNotFound:
                    lexer = TextLexer()
                highlighted = highlight(code, lexer, HtmlFormatter(nowrap=True))
            except ImportError:
                highlighted = html.escape(code)
            language_class = f" language-{html.escape(language)}" if language else ""
            return (
                f'<pre{source_attributes}><code class="highlight{language_class}">'
                f"{highlighted}</code></pre>\n"
            )

        markdown.renderer.rules["fence"] = render_fence

    def _html_document(self, body: str, *, title: str, zoom: int) -> str:
        css = self._read_asset("reader.css")
        bridge = self._read_asset("bridge.js")
        bounded_zoom = max(75, min(200, zoom))
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; frame-src 'none'; object-src 'none'">
  <title>{html.escape(title)}</title>
  <style>{css}</style>
</head>
<body style="--reader-zoom: {bounded_zoom / 100:.2f}">
  <main>{body}</main>
  <script>{bridge}</script>
</body>
</html>
"""

    def _read_asset(self, name: str) -> str:
        if self.assets_root is not None:
            return (self.assets_root / name).read_text(encoding="utf-8")

        resource_path = f"/io/github/pang/mdreader/reader/{name}"
        try:
            data = Gio.resources_lookup_data(resource_path, Gio.ResourceLookupFlags.NONE)
            return data.get_data().decode("utf-8")
        except GLib.Error:
            # Direct source-tree tests do not register the application bundle.
            source_asset = Path(__file__).parents[2] / "resources" / "reader" / name
            return source_asset.read_text(encoding="utf-8")
