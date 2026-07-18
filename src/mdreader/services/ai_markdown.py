from __future__ import annotations

import html
from dataclasses import dataclass
from urllib.parse import urlsplit

from mdreader.services.themes import DEFAULT_THEME_ID, ReaderTheme, get_theme


@dataclass(frozen=True, slots=True)
class AiMarkdownCell:
    markup: str
    header: bool = False


@dataclass(frozen=True, slots=True)
class AiMarkdownBlock:
    kind: str
    markup: str = ""
    level: int = 0
    language: str = ""
    rows: tuple[tuple[AiMarkdownCell, ...], ...] = ()


class AiMarkdownRenderer:
    """Convert assistant Markdown into safe, native-widget-friendly blocks."""

    def render(
        self,
        source: str,
        *,
        theme: ReaderTheme | None = None,
        dark: bool = False,
    ) -> tuple[AiMarkdownBlock, ...]:
        try:
            from markdown_it import MarkdownIt

            markdown = MarkdownIt(
                "commonmark",
                {"html": False, "linkify": True, "breaks": False},
            ).enable("table")
            tokens = markdown.parse(source)
        except Exception:
            return (AiMarkdownBlock("paragraph", html.escape(source)),) if source else ()

        selected_theme = theme or get_theme("plum-night" if dark else DEFAULT_THEME_ID)
        palette = {
            "accent": selected_theme.syntax_keyword,
            "moss": selected_theme.syntax_string,
            "number": selected_theme.syntax_number,
            "muted": selected_theme.syntax_comment,
            "name": selected_theme.syntax_name,
            "operator": selected_theme.syntax_operator,
            "code_bg": selected_theme.code_bg,
        }
        blocks: list[AiMarkdownBlock] = []
        heading_level = 0
        quote_depth = 0
        list_stack: list[dict[str, int | bool]] = []
        item_stack: list[dict[str, int | str | bool]] = []
        table_rows: list[tuple[AiMarkdownCell, ...]] | None = None
        table_row: list[AiMarkdownCell] | None = None
        table_cell_header = False

        for token in tokens:
            token_type = token.type
            if token_type == "heading_open":
                heading_level = int(token.tag[1:]) if token.tag[1:].isdigit() else 2
            elif token_type == "heading_close":
                heading_level = 0
            elif token_type == "blockquote_open":
                quote_depth += 1
            elif token_type == "blockquote_close":
                quote_depth = max(0, quote_depth - 1)
            elif token_type in {"bullet_list_open", "ordered_list_open"}:
                start = int(token.attrGet("start") or 1)
                list_stack.append({"ordered": token_type == "ordered_list_open", "next": start})
            elif token_type in {"bullet_list_close", "ordered_list_close"}:
                if list_stack:
                    list_stack.pop()
            elif token_type == "list_item_open":
                current = list_stack[-1] if list_stack else {"ordered": False, "next": 1}
                if current["ordered"]:
                    prefix = f"{current['next']}."
                    current["next"] = int(current["next"]) + 1
                else:
                    prefix = "•"
                item_stack.append(
                    {"prefix": prefix, "depth": max(0, len(list_stack) - 1), "used": False}
                )
            elif token_type == "list_item_close":
                if item_stack:
                    item_stack.pop()
            elif token_type == "table_open":
                table_rows = []
            elif token_type == "tr_open":
                table_row = []
            elif token_type in {"th_open", "td_open"}:
                table_cell_header = token_type == "th_open"
            elif token_type == "tr_close":
                if table_rows is not None and table_row is not None:
                    table_rows.append(tuple(table_row))
                table_row = None
            elif token_type == "table_close":
                blocks.append(AiMarkdownBlock("table", rows=tuple(table_rows or ())))
                table_rows = None
            elif token_type == "inline":
                markup = self._inline_markup(token.children or (), palette)
                if table_row is not None:
                    table_row.append(AiMarkdownCell(markup, table_cell_header))
                elif heading_level:
                    blocks.append(AiMarkdownBlock("heading", markup, level=heading_level))
                elif item_stack:
                    item = item_stack[-1]
                    prefix = str(item["prefix"]) if not item["used"] else ""
                    item["used"] = True
                    prefix_markup = f"<b>{html.escape(prefix)}</b>  " if prefix else ""
                    blocks.append(
                        AiMarkdownBlock(
                            "list-item",
                            prefix_markup + markup,
                            level=int(item["depth"]),
                        )
                    )
                elif quote_depth:
                    blocks.append(AiMarkdownBlock("quote", markup, level=quote_depth))
                else:
                    blocks.append(AiMarkdownBlock("paragraph", markup))
            elif token_type in {"fence", "code_block"}:
                info = token.info.strip()
                language = info.split(maxsplit=1)[0] if info else ""
                blocks.append(
                    AiMarkdownBlock(
                        "code",
                        self._code_markup(token.content, language, palette),
                        language=language,
                    )
                )
            elif token_type == "html_block":
                blocks.append(AiMarkdownBlock("paragraph", html.escape(token.content)))
            elif token_type == "hr":
                blocks.append(AiMarkdownBlock("separator"))

        return tuple(blocks)

    @classmethod
    def _inline_markup(cls, tokens: tuple[object, ...] | list[object], palette: dict[str, str]) -> str:
        output: list[str] = []
        link_stack: list[bool] = []
        for token in tokens:
            token_type = token.type
            if token_type == "text":
                output.append(html.escape(token.content))
            elif token_type == "code_inline":
                output.append(
                    '<span font_family="monospace" '
                    f'foreground="{palette["moss"]}" background="{palette["code_bg"]}">'
                    f'{html.escape(token.content)}</span>'
                )
            elif token_type == "strong_open":
                output.append("<b>")
            elif token_type == "strong_close":
                output.append("</b>")
            elif token_type == "em_open":
                output.append("<i>")
            elif token_type == "em_close":
                output.append("</i>")
            elif token_type == "s_open":
                output.append("<s>")
            elif token_type == "s_close":
                output.append("</s>")
            elif token_type in {"softbreak", "hardbreak"}:
                output.append("\n")
            elif token_type == "link_open":
                href = token.attrGet("href") or ""
                safe = cls._safe_link(href)
                link_stack.append(safe)
                if safe:
                    output.append(f'<a href="{html.escape(href, quote=True)}">')
            elif token_type == "link_close":
                if link_stack and link_stack.pop():
                    output.append("</a>")
            elif token_type == "image":
                output.append(f"[图片：{html.escape(token.content or 'image')}]")
            elif token_type == "html_inline":
                output.append(html.escape(token.content))
        while link_stack:
            if link_stack.pop():
                output.append("</a>")
        return "".join(output)

    @classmethod
    def _code_markup(cls, code: str, language: str, palette: dict[str, str]) -> str:
        try:
            from pygments import lex
            from pygments.lexers import TextLexer, get_lexer_by_name
            from pygments.token import Token
            from pygments.util import ClassNotFound

            try:
                lexer = get_lexer_by_name(language) if language else TextLexer()
            except ClassNotFound:
                lexer = TextLexer()

            output: list[str] = []
            for token_type, value in lex(code, lexer):
                escaped = html.escape(value)
                color = ""
                if token_type in Token.Keyword:
                    color = palette["accent"]
                elif token_type in Token.String:
                    color = palette["moss"]
                elif token_type in Token.Number:
                    color = palette["number"]
                elif token_type in Token.Comment:
                    color = palette["muted"]
                elif token_type in Token.Name.Builtin or token_type in Token.Name.Function:
                    color = palette["name"]
                elif token_type in Token.Operator:
                    color = palette["operator"]
                output.append(
                    f'<span foreground="{color}">{escaped}</span>' if color else escaped
                )
            return "".join(output).rstrip("\n")
        except ImportError:
            return html.escape(code).rstrip("\n")

    @staticmethod
    def _safe_link(href: str) -> bool:
        parsed = urlsplit(href)
        return parsed.scheme.lower() in {"http", "https", "mailto"}
