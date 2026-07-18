from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .document import DocumentSelection


@dataclass(frozen=True, slots=True)
class DocumentContext:
    relative_path: Path
    selection: DocumentSelection
    excerpt: str
    excerpt_start_line: int
    excerpt_end_line: int
    source_hash: str

    @property
    def location(self) -> str:
        if self.selection.start_line:
            return (
                f"{self.relative_path} · 第 {self.selection.start_line}–"
                f"{self.selection.end_line} 行"
            )
        return str(self.relative_path)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    text: str
