from __future__ import annotations

import html
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from mdreader.models import OutlineItem, RenderedDocument
from mdreader.services.themes import ReaderTheme, get_theme


class MarkdownUnavailableError(RuntimeError):
    pass


_SPACE_OR_DASH = re.compile(r"[\s\-]+", re.UNICODE)
_OBSIDIAN_IMAGE = re.compile(r"!\[\[([^\]\n]+)\]\]")
_IMAGE_SUFFIXES = frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})


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

    def render(
        self,
        source: str,
        *,
        title: str = "Document",
        zoom: int = 100,
        theme: ReaderTheme | None = None,
        document_path: Path | None = None,
        workspace_root: Path | None = None,
    ) -> RenderedDocument:
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

        prepared_source = self._rewrite_obsidian_images(
            source,
            document_path=document_path,
            workspace_root=workspace_root,
        )
        self._install_fence_renderer(markdown)
        try:
            tokens = markdown.parse(prepared_source)
            outline = self._decorate_tokens(
                tokens,
                document_path=document_path,
                workspace_root=workspace_root,
            )
            body = markdown.renderer.render(tokens, markdown.options, {})
        except Exception:
            # A reader should keep unusual or partially written Markdown
            # readable even if an extension encounters syntax it cannot handle.
            line_count = max(1, len(source.splitlines()))
            outline = []
            body = (
                f'<pre class="source-fallback" data-source-start="1" '
                f'data-source-end="{line_count}">{html.escape(source)}</pre>\n'
            )
        document = self._html_document(
            body, title=title, zoom=zoom, theme=theme or get_theme("")
        )
        return RenderedDocument(
            html=document,
            outline=tuple(outline),
            source_line_count=len(source.splitlines()),
            source=source,
        )

    def _decorate_tokens(
        self,
        tokens: list[object],
        *,
        document_path: Path | None = None,
        workspace_root: Path | None = None,
    ) -> list[OutlineItem]:
        outline: list[OutlineItem] = []
        slugger = _Slugger()

        for index, token in enumerate(tokens):
            mapping = getattr(token, "map", None)
            if mapping and getattr(token, "nesting", 0) == 1:
                token.attrSet("data-source-start", str(mapping[0] + 1))
                token.attrSet("data-source-end", str(mapping[1]))

            self._harden_links(
                token,
                document_path=document_path,
                workspace_root=workspace_root,
            )

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
    def _harden_links(
        cls,
        token: object,
        *,
        document_path: Path | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        if getattr(token, "type", "") == "link_open":
            token.attrSet("rel", "noreferrer noopener")
        if getattr(token, "type", "") == "image":
            source = token.attrGet("src") or ""
            parsed = urlsplit(source)
            blocked = bool(parsed.scheme or source.startswith(("/", "//")))
            if not blocked and document_path is not None and workspace_root is not None:
                root = workspace_root.resolve(strict=False)
                candidate = (document_path.parent / unquote(parsed.path)).resolve(strict=False)
                blocked = not candidate.is_relative_to(root)
            elif not blocked:
                blocked = ".." in Path(unquote(parsed.path)).parts
            if blocked:
                token.attrSet("src", "about:blank")
                token.attrSet("data-blocked-source", source)
                token.attrSet("title", "Remote or out-of-folder image blocked")
        for child in getattr(token, "children", None) or ():
            cls._harden_links(
                child,
                document_path=document_path,
                workspace_root=workspace_root,
            )

    @staticmethod
    def _install_fence_renderer(markdown: object) -> None:
        def render_fence(tokens: list[object], index: int, options: dict, env: dict) -> str:
            del options, env
            token = tokens[index]
            code = getattr(token, "content", "")
            info = getattr(token, "info", "").strip()
            language = info.split(maxsplit=1)[0] if info else ""
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

    @classmethod
    def _rewrite_obsidian_images(
        cls,
        source: str,
        *,
        document_path: Path | None,
        workspace_root: Path | None,
    ) -> str:
        if document_path is None or workspace_root is None or "![[" not in source:
            return source

        root = workspace_root.resolve(strict=False)
        document = document_path.resolve(strict=False)
        if not document.is_relative_to(root):
            return source

        basename_index: dict[str, tuple[Path, ...]] | None = None

        def image_index() -> dict[str, tuple[Path, ...]]:
            nonlocal basename_index
            if basename_index is not None:
                return basename_index
            collected: dict[str, list[Path]] = {}
            try:
                paths = root.rglob("*")
                for candidate in paths:
                    if not candidate.is_file() or candidate.suffix.lower() not in _IMAGE_SUFFIXES:
                        continue
                    resolved = candidate.resolve(strict=False)
                    if not resolved.is_relative_to(root):
                        continue
                    collected.setdefault(candidate.name, []).append(resolved)
            except OSError:
                pass
            basename_index = {
                name: tuple(dict.fromkeys(candidates))
                for name, candidates in collected.items()
            }
            return basename_index

        def replacement(match: re.Match[str]) -> str:
            raw_target = match.group(1).strip()
            target = raw_target.split("|", maxsplit=1)[0].strip()
            target_path = Path(target)
            if (
                not target
                or target_path.is_absolute()
                or ".." in target_path.parts
                or target_path.suffix.lower() not in _IMAGE_SUFFIXES
            ):
                return match.group(0)

            direct_candidates = (
                document.parent / target_path,
                root / target_path,
            )
            resolved_candidates: list[Path] = []
            for candidate in direct_candidates:
                resolved = candidate.resolve(strict=False)
                if resolved.is_file() and resolved.is_relative_to(root):
                    resolved_candidates.append(resolved)
            if not resolved_candidates and len(target_path.parts) == 1:
                resolved_candidates.extend(image_index().get(target_path.name, ()))

            unique_candidates = tuple(dict.fromkeys(resolved_candidates))
            if len(unique_candidates) != 1:
                return match.group(0)

            relative = Path(os.path.relpath(unique_candidates[0], document.parent))
            destination = quote(relative.as_posix(), safe="/-._~")
            alt = target_path.stem.replace("[", "\\[").replace("]", "\\]")
            return f"![{alt}]({destination})"

        return _OBSIDIAN_IMAGE.sub(replacement, source)

    def _html_document(
        self, body: str, *, title: str, zoom: int, theme: ReaderTheme
    ) -> str:
        css = self._read_asset("reader.css")
        bridge = self._read_asset("bridge.js")
        bounded_zoom = max(75, min(200, zoom))
        return f"""<!doctype html>
<html lang="en" style="{theme.reader_inline_style()}">
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
