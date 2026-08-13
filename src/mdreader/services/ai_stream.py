"""Incremental SSE and standard JSON-completion parser for chat responses.

Contract: docs/LLM_PROVIDER_MIGRATION_SPEC.md §8.4 (SSE), §8.5 (JSON
completion) and §13.5 (test matrix). Pure Python stdlib: no GTK, no network,
no Content-Encoding — the HTTP transport hands decoded bytes to
:meth:`StreamParser.feed`.

Chunk protocol
--------------
- ``kind="text"``: one assistant text delta in ``text``.
- ``kind="done"``: completion determination locked (from ``finish_reason``,
  ``[DONE]`` compatible success, or a valid JSON completion). ``success`` is
  True only for ``finish_reason=stop`` (or the ``[DONE]``-without-finish
  compatibility case) with non-empty text; ``truncated`` marks a non-stop
  finish (``length``/``content_filter``/``tool_calls``/...).
- ``kind="error"``: server-reported error object ``{"error": ...}``;
  ``error_code`` is an ``AiErrorCode`` string value. The parser locks into a
  failed state and ``finish`` replays the same chunk.

Error transport
---------------
Protocol violations raise :class:`AiError` with ``INVALID_RESPONSE`` or
``RESPONSE_TOO_LARGE``, from either :meth:`feed` or :meth:`finish`. Premature
EOF without ``[DONE]``, any finish reason or a valid JSON completion yields an
``error`` chunk with ``STREAM_ENDED_EARLY`` from :meth:`finish`.

The complete assistant text is always available as ``parser.text`` after the
stream ends. SSE mode also streams it via ``text`` chunks; JSON completion mode
buffers to ``finish`` (bounded by the shared 8 MiB pre-parser cap) and exposes
the content only through ``parser.text``.

Layered limits (spec §8.4)
--------------------------
- pre-parser response bytes: 8 MiB
- one unterminated SSE event: 256 KiB
- one ``data:``/comment line value: 256 KiB
- Ask UTF-8 text: 2 MiB; Edit UTF-8 text: 256 KiB

Comment, reasoning and unknown-field bytes count toward the response, event
and line caps so hiding them cannot bypass a limit. Every limit is an
exclusive ceiling: exactly reaching it is allowed, one more byte raises
``RESPONSE_TOO_LARGE``.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass

from mdreader.models.ai import AiError, AiErrorCode

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_ASK_TEXT_BYTES = 2 * 1024 * 1024
MAX_EDIT_TEXT_BYTES = 256 * 1024
SNIFF_BYTES = 4 * 1024


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One parser output item.

    - ``kind="text"``: assistant text delta in ``text``.
    - ``kind="done"``: completion determination locked; ``finish_reason``,
      ``truncated`` and ``success`` describe it.
    - ``kind="error"``: server-reported error object; ``error_code`` is the
      ``AiErrorCode`` string value.
    """

    kind: str
    text: str = ""
    error_code: str = ""
    finish_reason: str | None = None
    truncated: bool = False
    success: bool = False


class StreamParser:
    """Incremental parser for SSE and standard JSON chat completions.

    ``feed`` consumes decoded response bytes and returns zero or more chunks.
    ``finish`` must be called once at EOF; it returns the terminal chunk: the
    locked ``done`` chunk, an ``error`` chunk, or ``STREAM_ENDED_EARLY``.

    The first 4 KiB of the response are sniffed to pick the mode: a ``data:``
    or ``:`` prefix means SSE, a JSON object (leading whitespace allowed)
    means the JSON-completion path, which buffers until ``finish``. An
    unrecognized prefix, or no recognizable prefix within 4 KiB, raises
    ``INVALID_RESPONSE`` (spec §8.5).
    """

    def __init__(
        self,
        *,
        mode: str = "ask",
        max_text_bytes: int = MAX_ASK_TEXT_BYTES,
    ) -> None:
        if mode not in ("ask", "edit"):
            raise ValueError(f"unknown mode: {mode!r}")
        # The signature keeps the Ask default; edit mode changes the default
        # to 256 KiB unless the caller overrides max_text_bytes explicitly.
        if mode == "edit" and max_text_bytes == MAX_ASK_TEXT_BYTES:
            max_text_bytes = MAX_EDIT_TEXT_BYTES
        self.mode = mode
        self.max_text_bytes = max_text_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._total_bytes = 0
        self._sniff_budget = SNIFF_BYTES
        self._sniff_text = ""
        self._parsing_mode: str | None = None  # None | "sse" | "json"
        self._json_buf: list[str] = []
        self._line_buf = ""
        self._line_value_bytes = 0
        self._line_has_colon = False
        self._event_data: list[str] = []
        self._event_bytes = 0
        self.text = ""
        self._text_bytes = 0
        self._done_chunk: StreamChunk | None = None
        self._error_chunk: StreamChunk | None = None

    # -- public API -------------------------------------------------------

    def feed(self, chunk: bytes) -> list[StreamChunk]:
        if not isinstance(chunk, bytes):
            raise TypeError("chunk must be bytes")
        if not chunk:
            return []
        self._total_bytes += len(chunk)
        if self._total_bytes > MAX_RESPONSE_BYTES:
            raise AiError(AiErrorCode.RESPONSE_TOO_LARGE, "pre-parser response limit exceeded")
        if self._error_chunk is not None:
            return []
        if self._parsing_mode is None:
            return self._feed_undecided(chunk)
        try:
            text = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise AiError(AiErrorCode.INVALID_RESPONSE, "response is not valid UTF-8") from exc
        if self._parsing_mode == "sse":
            return self._process_sse_text(text)
        self._json_buf.append(text)
        return []

    def finish(self) -> StreamChunk:
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise AiError(AiErrorCode.INVALID_RESPONSE, "truncated UTF-8 at end of stream") from exc
        # The incremental decoder only buffers incomplete characters, so a
        # non-empty tail means a partial multibyte character that completed
        # exactly at EOF; JSON bodies need it, SSE events never do.
        if tail and self._parsing_mode == "json":
            self._json_buf.append(tail)
        if self._error_chunk is not None:
            return self._error_chunk
        if self._done_chunk is not None:
            return self._done_chunk
        if self._parsing_mode == "json":
            return self._finish_json()
        return StreamChunk(kind="error", error_code=AiErrorCode.STREAM_ENDED_EARLY.value)

    # -- mode sniffing ----------------------------------------------------

    def _feed_undecided(self, chunk: bytes) -> list[StreamChunk]:
        if self._sniff_budget > 0:
            sniff = chunk[: self._sniff_budget]
            try:
                sniff_text = self._decoder.decode(sniff, final=False)
            except UnicodeDecodeError as exc:
                raise AiError(AiErrorCode.INVALID_RESPONSE, "response is not valid UTF-8") from exc
            self._sniff_text += sniff_text
            self._sniff_budget -= len(sniff)
            rest = chunk[len(sniff):]
        else:
            rest = chunk
        rest_text = ""
        if rest:
            try:
                rest_text = self._decoder.decode(rest, final=False)
            except UnicodeDecodeError as exc:
                raise AiError(AiErrorCode.INVALID_RESPONSE, "response is not valid UTF-8") from exc
        decision = self._decide_sniff()
        if decision is None and self._sniff_budget <= 0:
            decision = "invalid"
        if decision == "invalid":
            raise AiError(AiErrorCode.INVALID_RESPONSE, "unrecognized response prefix")
        if decision is None:
            return []
        self._parsing_mode = decision
        pending, self._sniff_text = self._sniff_text, ""
        if decision == "json":
            self._json_buf.append(pending)
            if rest_text:
                self._json_buf.append(rest_text)
            return []
        chunks = self._process_sse_text(pending)
        if rest_text:
            chunks.extend(self._process_sse_text(rest_text))
        return chunks

    def _decide_sniff(self) -> str | None:
        """Return "sse"/"json", None while more prefix is needed, or "invalid"."""
        stripped = self._sniff_text.lstrip()
        if not stripped:
            return None
        if stripped.startswith("{"):
            return "json"
        if stripped.startswith("data:") or stripped.startswith(":"):
            return "sse"
        if "data:".startswith(stripped):
            return None  # partial "data" prefix; keep sniffing
        return "invalid"

    # -- SSE line/event processing ----------------------------------------

    def _process_sse_text(self, text: str) -> list[StreamChunk]:
        if not text:
            return []
        chunks: list[StreamChunk] = []
        combined = self._line_buf + text
        self._line_buf = ""
        lines = combined.split("\n")
        self._line_buf = lines.pop()
        for line in lines:
            if line.endswith("\r"):
                line = line[:-1]
            chunks.extend(self._process_sse_line(line))
        # Account only the bytes this call appended to the unterminated
        # line; its earlier part was already accounted when it arrived.
        new_pending = text.rsplit("\n", 1)[-1]
        if new_pending:
            self._accumulate_line(new_pending)
        return chunks

    def _process_sse_line(self, line: str) -> list[StreamChunk]:
        # The incremental accounting belonged to the line that just completed.
        self._line_value_bytes = 0
        self._line_has_colon = False
        if line == "":
            if self._event_data:
                return self._dispatch_event()
            self._event_bytes = 0
            return []
        if line.startswith(":"):
            self._account_event_value(line[1:])
            return []
        field, sep, value = line.partition(":")
        if field == "data":
            self._account_event_value(value)
            if sep and value.startswith(" "):
                value = value[1:]
            self._event_data.append(value)
            return []
        # Other SSE fields (event, id, retry) and unknown fields: their bytes
        # still count toward the caps but they never surface.
        self._account_event_value(value)
        return []

    def _accumulate_line(self, piece: str) -> None:
        """Bound the unterminated line so it cannot buffer without limit."""
        if not piece:
            return
        if self._line_has_colon:
            self._line_value_bytes += len(piece.encode("utf-8"))
        else:
            colon = piece.find(":")
            if colon >= 0:
                self._line_value_bytes += len(piece[colon + 1:].encode("utf-8"))
                self._line_has_colon = True
        if self._line_value_bytes > MAX_LINE_BYTES:
            raise AiError(AiErrorCode.RESPONSE_TOO_LARGE, "SSE line limit exceeded")

    def _account_event_value(self, value: str) -> None:
        n = len(value.encode("utf-8"))
        if n > MAX_LINE_BYTES:
            raise AiError(AiErrorCode.RESPONSE_TOO_LARGE, "SSE line limit exceeded")
        self._event_bytes += n + (1 if self._event_data else 0)
        if self._event_bytes > MAX_EVENT_BYTES:
            raise AiError(AiErrorCode.RESPONSE_TOO_LARGE, "SSE event limit exceeded")

    def _dispatch_event(self) -> list[StreamChunk]:
        data = "\n".join(self._event_data)
        self._event_data = []
        self._event_bytes = 0
        if data == "":
            return []
        if data == "[DONE]":
            if self._done_chunk is not None:
                return []
            done = StreamChunk(kind="done", finish_reason=None, success=self._text_bytes > 0)
            self._done_chunk = done
            return [done]
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AiError(AiErrorCode.INVALID_RESPONSE, "malformed SSE event") from exc
        if not isinstance(obj, dict):
            raise AiError(AiErrorCode.INVALID_RESPONSE, "SSE event must be a JSON object")
        if "error" in obj:
            return self._fail_error_object(obj["error"])
        return self._process_completion_event(obj)

    def _process_completion_event(self, obj: dict) -> list[StreamChunk]:
        chunks: list[StreamChunk] = []
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            if choices == []:
                # An SSE event whose choices list is empty is pure metadata
                # (keepalive/trailing usage): content and finish_reason both
                # live inside choices, so such an event can never advance or
                # contradict the completion. Some providers (e.g. the
                # opencode zen gateway) send {"choices":[]} after [DONE]; a
                # response that never produced text still fails through the
                # no-text success rule at finish(). JSON completion mode
                # (_finish_json) keeps the strict check: an empty-choices
                # JSON body is a degenerate non-streaming response.
                return []
            raise AiError(AiErrorCode.INVALID_RESPONSE, "choices must be a non-empty array")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise AiError(AiErrorCode.INVALID_RESPONSE, "choices[0] must be an object")
        if "delta" in choice and not isinstance(choice["delta"], dict):
            raise AiError(AiErrorCode.INVALID_RESPONSE, "delta must be an object")
        delta = choice.get("delta")
        if isinstance(delta, dict) and "content" in delta:
            content = delta["content"]
            if content is None:
                pass
            elif not isinstance(content, str):
                raise AiError(AiErrorCode.INVALID_RESPONSE, "content must be a string")
            elif content:
                if self._done_chunk is not None:
                    raise AiError(AiErrorCode.INVALID_RESPONSE, "content after completion locked")
                self._append_text(content)
                chunks.append(StreamChunk(kind="text", text=content))
        if "finish_reason" in choice:
            reason = choice["finish_reason"]
            if reason is not None and not isinstance(reason, str):
                raise AiError(AiErrorCode.INVALID_RESPONSE, "finish_reason must be null or string")
            if isinstance(reason, str):
                if self._done_chunk is not None:
                    if reason != self._done_chunk.finish_reason:
                        raise AiError(
                            AiErrorCode.INVALID_RESPONSE, "contradictory finish_reason"
                        )
                else:
                    done = self._make_done(reason)
                    self._done_chunk = done
                    chunks.append(done)
        return chunks

    def _make_done(self, reason: str) -> StreamChunk:
        if reason == "stop":
            # A "stop" with no text is still not a success (spec §8.4:
            # responses that never produced text must not count as success).
            return StreamChunk(kind="done", finish_reason="stop", success=self._text_bytes > 0)
        return StreamChunk(kind="done", finish_reason=reason, success=False, truncated=True)

    def _append_text(self, content: str) -> None:
        n = len(content.encode("utf-8"))
        if self._text_bytes + n > self.max_text_bytes:
            raise AiError(AiErrorCode.RESPONSE_TOO_LARGE, "text limit exceeded")
        self._text_bytes += n
        self.text += content

    def _fail_error_object(self, error: object) -> list[StreamChunk]:
        err = StreamChunk(kind="error", error_code=self._map_error_code(error))
        self._error_chunk = err
        return [err]

    @staticmethod
    def _map_error_code(error: object) -> str:
        candidates: list[str] = []
        if isinstance(error, dict):
            for key in ("code", "type"):
                value = error.get(key)
                if isinstance(value, str):
                    candidates.append(value)
        elif isinstance(error, str):
            candidates.append(error)
        for candidate in candidates:
            try:
                return AiErrorCode(candidate).value
            except ValueError:
                continue
        return AiErrorCode.INVALID_RESPONSE.value

    # -- JSON completion ---------------------------------------------------

    def _finish_json(self) -> StreamChunk:
        raw = "".join(self._json_buf)
        stripped = raw.lstrip()
        if not stripped:
            return StreamChunk(kind="error", error_code=AiErrorCode.STREAM_ENDED_EARLY.value)
        try:
            obj, end = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError:
            # Truncated or otherwise incomplete JSON at EOF: no valid JSON
            # completion was delivered (spec §8.4 STREAM_ENDED_EARLY).
            return StreamChunk(kind="error", error_code=AiErrorCode.STREAM_ENDED_EARLY.value)
        if stripped[end:].strip():
            raise AiError(AiErrorCode.INVALID_RESPONSE, "trailing data after JSON completion")
        if not isinstance(obj, dict):
            raise AiError(AiErrorCode.INVALID_RESPONSE, "JSON completion must be an object")
        if "error" in obj:
            return self._fail_error_object(obj["error"])[0]
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AiError(AiErrorCode.INVALID_RESPONSE, "choices must be a non-empty array")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise AiError(AiErrorCode.INVALID_RESPONSE, "choices[0] must be an object")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise AiError(AiErrorCode.INVALID_RESPONSE, "message must be an object")
        content = message.get("content")
        if not isinstance(content, str):
            raise AiError(AiErrorCode.INVALID_RESPONSE, "message.content must be a string")
        if not content:
            raise AiError(AiErrorCode.INVALID_RESPONSE, "message.content must be non-empty")
        n = len(content.encode("utf-8"))
        if n > self.max_text_bytes:
            raise AiError(AiErrorCode.RESPONSE_TOO_LARGE, "text limit exceeded")
        self._text_bytes = n
        self.text = content
        reason = choice.get("finish_reason")
        if not isinstance(reason, str):
            raise AiError(AiErrorCode.INVALID_RESPONSE, "finish_reason must be a string")
        if reason == "stop":
            return StreamChunk(kind="done", finish_reason="stop", success=True, truncated=False)
        return StreamChunk(kind="done", finish_reason=reason, success=False, truncated=True)
