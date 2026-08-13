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


class ConversationState:
    """Bounded, in-memory Ask history (spec §8.3).

    Only successfully completed user/assistant pairs may enter through
    :meth:`record_success`; failed, cancelled and partial replies never have a
    write path. History is never persisted. Eviction removes the oldest
    complete pair when the message count or total character budget is exceeded.
    """

    def __init__(self, *, max_messages: int = 12, max_characters: int = 48_000) -> None:
        self._max_messages = max_messages
        self._max_characters = max_characters
        self._messages: list[ChatMessage] = []

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return tuple(self._messages)

    def record_success(self, user_text: str, assistant_text: str) -> None:
        """Atomically append one successful user/assistant pair, then trim."""
        self._messages.append(ChatMessage("user", user_text))
        self._messages.append(ChatMessage("assistant", assistant_text))
        self._trim()

    def reset(self) -> None:
        """Clear history (document, model or connection switch, or explicit reset)."""
        self._messages.clear()

    def _trim(self) -> None:
        while len(self._messages) > self._max_messages:
            del self._messages[:2]
        while sum(len(message.text) for message in self._messages) > self._max_characters:
            if len(self._messages) < 2:
                break
            del self._messages[:2]


def commit_ask_if_successful(
    state: ConversationState,
    question: str,
    *,
    success: bool,
    full_text: str,
) -> bool:
    """Atomically record one Ask turn only on explicit success (spec §11.4).

    Failed, cancelled, truncated and otherwise incomplete replies never enter
    the history: callers pass ``success`` from the stream outcome (True only
    for ``finish_reason=stop`` or the ``[DONE]`` compatible success with
    non-empty text). Returns whether the pair was recorded.
    """
    if not success:
        return False
    state.record_success(question, full_text)
    return True
