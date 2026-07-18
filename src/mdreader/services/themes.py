from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw


@dataclass(frozen=True, slots=True)
class ReaderTheme:
    id: str
    name: str
    description: str
    dark: bool
    shell: str
    sidebar: str
    paper: str
    ink: str
    muted: str
    accent: str
    accent_fg: str
    support: str
    code_bg: str
    rule: str
    selection: str
    syntax_keyword: str
    syntax_string: str
    syntax_number: str
    syntax_comment: str
    syntax_name: str
    syntax_operator: str

    @property
    def css_class(self) -> str:
        return f"theme-{self.id}"

    def reader_tokens(self) -> dict[str, str]:
        return {
            "--desk": self.shell,
            "--paper": self.paper,
            "--ink": self.ink,
            "--muted": self.muted,
            "--mulberry": self.accent,
            "--moss": self.support,
            "--code-bg": self.code_bg,
            "--rule": self.rule,
            "--selection": self.selection,
            "--syntax-keyword": self.syntax_keyword,
            "--syntax-string": self.syntax_string,
            "--syntax-number": self.syntax_number,
            "--syntax-comment": self.syntax_comment,
            "--syntax-name": self.syntax_name,
            "--syntax-operator": self.syntax_operator,
        }

    def reader_inline_style(self) -> str:
        return "; ".join(
            f"{name}: {value}" for name, value in self.reader_tokens().items()
        )


THEMES: tuple[ReaderTheme, ...] = (
    ReaderTheme(
        id="warm-paper",
        name="Warm Paper",
        description="Cream paper, graphite ink and mulberry marks",
        dark=False,
        shell="#E8DDCB",
        sidebar="#EFE6D8",
        paper="#F8F3E9",
        ink="#2F2926",
        muted="#74685F",
        accent="#7A4651",
        accent_fg="#FFF8F1",
        support="#68705A",
        code_bg="#E9DFD1",
        rule="rgba(75, 61, 54, 0.18)",
        selection="rgba(122, 70, 81, 0.22)",
        syntax_keyword="#8D4C5B",
        syntax_string="#65724F",
        syntax_number="#9A653F",
        syntax_comment="#8B8178",
        syntax_name="#526F86",
        syntax_operator="#785F76",
    ),
    ReaderTheme(
        id="mist-blue",
        name="Mist Blue",
        description="Cool daylight with blue editorial accents",
        dark=False,
        shell="#DCE5E8",
        sidebar="#E7EEF0",
        paper="#F7FAFA",
        ink="#26343A",
        muted="#66777E",
        accent="#3F6F83",
        accent_fg="#F7FCFE",
        support="#617A70",
        code_bg="#E5EEF0",
        rule="rgba(52, 79, 89, 0.18)",
        selection="rgba(63, 111, 131, 0.22)",
        syntax_keyword="#735C91",
        syntax_string="#4E766C",
        syntax_number="#9A663D",
        syntax_comment="#7E8C91",
        syntax_name="#3F6F83",
        syntax_operator="#6C5F7C",
    ),
    ReaderTheme(
        id="sage-leaf",
        name="Sage Leaf",
        description="Soft green cloth and calm botanical ink",
        dark=False,
        shell="#DDE4D7",
        sidebar="#E8EDE3",
        paper="#F6F7F1",
        ink="#2B342B",
        muted="#687168",
        accent="#4F715E",
        accent_fg="#F7FCF8",
        support="#8A6948",
        code_bg="#E6EBE0",
        rule="rgba(54, 77, 59, 0.18)",
        selection="rgba(79, 113, 94, 0.22)",
        syntax_keyword="#6E557D",
        syntax_string="#4F715E",
        syntax_number="#93653F",
        syntax_comment="#7A857A",
        syntax_name="#426B78",
        syntax_operator="#765E6F",
    ),
    ReaderTheme(
        id="midnight-ink",
        name="Midnight Ink",
        description="Deep blue-black desk with clear cool type",
        dark=True,
        shell="#171C23",
        sidebar="#1D2530",
        paper="#222A35",
        ink="#E7ECF2",
        muted="#AAB5C2",
        accent="#7FB4D4",
        accent_fg="#10202A",
        support="#8FC3A4",
        code_bg="#18202A",
        rule="rgba(219, 230, 240, 0.16)",
        selection="rgba(127, 180, 212, 0.28)",
        syntax_keyword="#C99ADB",
        syntax_string="#9BC7AE",
        syntax_number="#E0AD78",
        syntax_comment="#8795A4",
        syntax_name="#8DC3E0",
        syntax_operator="#D6A6C8",
    ),
    ReaderTheme(
        id="plum-night",
        name="Plum Night",
        description="Warm charcoal with plum and moss highlights",
        dark=True,
        shell="#241B22",
        sidebar="#2C222A",
        paper="#33282F",
        ink="#F0E4EA",
        muted="#BFAEB7",
        accent="#D69AAA",
        accent_fg="#2B1820",
        support="#B8C49B",
        code_bg="#271E25",
        rule="rgba(240, 228, 234, 0.16)",
        selection="rgba(214, 154, 170, 0.28)",
        syntax_keyword="#E3A1B3",
        syntax_string="#B8C49B",
        syntax_number="#E0AD78",
        syntax_comment="#A999A2",
        syntax_name="#9FC4D7",
        syntax_operator="#D7A8CF",
    ),
)

_THEME_MAP = {theme.id: theme for theme in THEMES}
_LEGACY_THEME_IDS = {
    "system": "warm-paper",
    "warm-light": "warm-paper",
    "warm-dark": "plum-night",
}
DEFAULT_THEME_ID = "warm-paper"


def normalize_theme_id(theme_id: str) -> str:
    normalized = _LEGACY_THEME_IDS.get(theme_id, theme_id)
    return normalized if normalized in _THEME_MAP else DEFAULT_THEME_ID


def get_theme(theme_id: str) -> ReaderTheme:
    return _THEME_MAP[normalize_theme_id(theme_id)]


def apply_color_scheme(theme: ReaderTheme) -> None:
    scheme = (
        Adw.ColorScheme.FORCE_DARK if theme.dark else Adw.ColorScheme.FORCE_LIGHT
    )
    Adw.StyleManager.get_default().set_color_scheme(scheme)


def build_gtk_theme_css() -> str:
    rules: list[str] = []
    for theme in THEMES:
        root = f"window.{theme.css_class}"
        rules.append(
            f"""
{root} {{
  background: {theme.shell};
  color: {theme.ink};
}}
{root} headerbar,
{root} .library-pane headerbar,
{root} .ai-pane headerbar {{
  background: {theme.shell};
  color: {theme.ink};
}}
{root} .library-pane,
{root} .ai-pane {{
  background: {theme.sidebar};
  color: {theme.ink};
}}
{root} .ai-prompt-frame {{
  background: {theme.paper};
  color: {theme.ink};
  border-color: {theme.rule};
}}
{root} .ai-prompt,
{root} .context-quote,
{root} .user-message,
{root} .assistant-message,
{root} .ai-code-block {{
  color: {theme.ink};
}}
{root} .ai-prompt-placeholder,
{root} .dimmed {{
  color: {theme.muted};
}}
{root} .context-rail {{ border-color: {theme.accent}; }}
{root} .ai-thinking,
{root} .ai-markdown-heading,
{root} .ai-table-header {{ color: {theme.accent}; }}
{root} .ai-code-block,
{root} .ai-table-header {{ background: {theme.code_bg}; }}
{root} .ai-code-block,
{root} .ai-table,
{root} .ai-table-cell {{ border-color: {theme.rule}; }}
{root} .ai-markdown-quote {{
  color: {theme.muted};
  border-color: {theme.support};
}}
{root} button.suggested-action {{
  background: {theme.accent};
  color: {theme.accent_fg};
}}
{root} selection {{ background: {theme.selection}; }}
""".strip()
        )
    return "\n\n".join(rules)
