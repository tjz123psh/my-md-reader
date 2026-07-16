from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutlineItem:
    level: int
    title: str
    slug: str
    start_line: int


@dataclass(frozen=True, slots=True)
class DocumentSelection:
    text: str = ""
    start_line: int = 0
    end_line: int = 0
    heading_id: str = ""
    heading_title: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True, slots=True)
class RenderedDocument:
    html: str
    outline: tuple[OutlineItem, ...]
    source_line_count: int
    source: str
