"""Incremental SSE and JSON-completion parser tests
(docs/LLM_PROVIDER_MIGRATION_SPEC.md §8.4, §8.5, §13.5).

Targets ``mdreader.services.ai_stream``. Pure logic: no GTK, no network.
Covers the full §13.5 matrix: split chunks, UTF-8 boundaries, multi-line
events, comments, role-only/null/empty content, finish determination locks,
layered size limits, 4 KiB mode sniffing and JSON-completion validation.
"""

from __future__ import annotations

import unittest

from mdreader.models.ai import AiError, AiErrorCode
from mdreader.services.ai_stream import (
    MAX_EVENT_BYTES,
    MAX_LINE_BYTES,
    MAX_RESPONSE_BYTES,
    StreamChunk,
    StreamParser,
)

KIB = 1024
MIB = 1024 * KIB

DONE_STOP = StreamChunk(kind="done", finish_reason="stop", success=True)


class StreamParserSseTests(unittest.TestCase):
    def test_complete_event_yields_text_chunk(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
        self.assertEqual(chunks, [StreamChunk(kind="text", text="hello")])
        self.assertEqual(parser.text, "hello")

    def test_event_json_split_across_tcp_chunks(self) -> None:
        payload = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        parser = StreamParser()
        chunks: list[StreamChunk] = []
        for i in range(0, len(payload), 3):
            chunks.extend(parser.feed(payload[i : i + 3]))
        self.assertEqual(chunks, [StreamChunk(kind="text", text="hello")])

    def test_utf8_bytes_split_across_chunks(self) -> None:
        payload = 'data: {"choices":[{"delta":{"content":"你好😀世界"}}]}\n\n'.encode("utf-8")
        parser = StreamParser()
        chunks: list[StreamChunk] = []
        for byte in payload:
            chunks.extend(parser.feed(bytes([byte])))
        self.assertEqual(chunks, [StreamChunk(kind="text", text="你好😀世界")])
        self.assertEqual(parser.text, "你好😀世界")

    def test_multiple_data_lines_joined_into_one_event(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices": [\ndata: {"delta": {"content": "a"}}]}\n\n')
        self.assertEqual(chunks, [StreamChunk(kind="text", text="a")])

    def test_comment_lines_and_blank_lines_ignored(self) -> None:
        parser = StreamParser()
        payload = b": keepalive\n\n\n" + b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        chunks = parser.feed(payload)
        self.assertEqual(chunks, [StreamChunk(kind="text", text="ok")])

    def test_role_only_chunk_ignored(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
        self.assertEqual(chunks, [])

    def test_content_null_ignored(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(
            b'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}\n\n'
        )
        self.assertEqual(chunks, [])

    def test_empty_string_content_emits_nothing(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices":[{"delta":{"content":""}}]}\n\n')
        self.assertEqual(chunks, [])

    def test_multiple_text_segments_concatenated(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n')
        chunks += parser.feed(b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n')
        self.assertEqual(
            chunks,
            [StreamChunk(kind="text", text="hello "), StreamChunk(kind="text", text="world")],
        )
        self.assertEqual(parser.text, "hello world")

    def test_multiple_events_in_one_feed(self) -> None:
        parser = StreamParser()
        payload = (
            b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
        )
        chunks = parser.feed(payload)
        self.assertEqual(
            chunks, [StreamChunk(kind="text", text="a"), StreamChunk(kind="text", text="b")]
        )
        self.assertEqual(parser.text, "ab")

    def test_crlf_line_endings_accepted(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices":[{"delta":{"content":"crlf"}}]}\r\n\r\n')
        self.assertEqual(chunks, [StreamChunk(kind="text", text="crlf")])

    def test_done_sentinel_after_text_is_compat_success(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n')
        self.assertEqual(chunks, [StreamChunk(kind="text", text="answer")])
        done = parser.feed(b"data: [DONE]\n\n")
        self.assertEqual(done, [StreamChunk(kind="done", finish_reason=None, success=True)])
        self.assertEqual(parser.finish(), done[0])

    def test_done_sentinel_alone_not_success(self) -> None:
        parser = StreamParser()
        done = parser.feed(b"data: [DONE]\n\n")
        self.assertEqual(done, [StreamChunk(kind="done", finish_reason=None, success=False)])

    def test_finish_reason_stop_then_eof(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(
            b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
        )
        self.assertEqual(chunks, [StreamChunk(kind="text", text="answer"), DONE_STOP])
        self.assertEqual(parser.finish(), DONE_STOP)

    def test_finish_reason_variants(self) -> None:
        for reason, success in (
            ("stop", True),
            ("length", False),
            ("content_filter", False),
            ("tool_calls", False),
        ):
            with self.subTest(reason=reason):
                parser = StreamParser()
                event = (
                    'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"'
                    + reason
                    + '"}]}\n\n'
                ).encode()
                chunks = parser.feed(event)
                self.assertEqual(
                    chunks,
                    [
                        StreamChunk(kind="text", text="x"),
                        StreamChunk(
                            kind="done",
                            finish_reason=reason,
                            success=success,
                            truncated=not success,
                        ),
                    ],
                )

    def test_stop_with_no_text_not_success(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        self.assertEqual(
            chunks, [StreamChunk(kind="done", finish_reason="stop", success=False)]
        )

    def test_partial_finish_then_stop_rejected(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"length"}]}\n\n')
        with self.assertRaises(AiError) as ctx:
            parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_contradictory_finish_reason_rejected(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}\n\n')
        with self.assertRaises(AiError) as ctx:
            parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n')
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_same_finish_reason_repeat_allowed(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n')
        self.assertEqual(
            parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'), []
        )

    def test_text_after_finish_rejected(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\n\n')
        with self.assertRaises(AiError) as ctx:
            parser.feed(b'data: {"choices":[{"delta":{"content":"y"}}]}\n\n')
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_done_then_done_sentinel_ignored(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        self.assertEqual(parser.feed(b"data: [DONE]\n\n"), [])

    def test_finish_reason_null_ignored(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n')
        result = parser.finish()
        self.assertEqual(result, StreamChunk(kind="error", error_code="STREAM_ENDED_EARLY"))
        self.assertEqual(parser.text, "x")

    def test_eof_without_terminator_ends_early(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{"content":"partial answer"}}]}\n\n')
        result = parser.finish()
        self.assertEqual(result, StreamChunk(kind="error", error_code="STREAM_ENDED_EARLY"))
        self.assertEqual(parser.text, "partial answer")

    def test_eof_discards_unterminated_final_event(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{"content":"no blank line"}}]}')
        result = parser.finish()
        self.assertEqual(result, StreamChunk(kind="error", error_code="STREAM_ENDED_EARLY"))
        self.assertEqual(parser.text, "")

    def test_error_object_fails_immediately(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"error":{"message":"boom"}}\n\n')
        self.assertEqual(chunks, [StreamChunk(kind="error", error_code="INVALID_RESPONSE")])
        self.assertEqual(parser.feed(b"data: [DONE]\n\n"), [])
        self.assertEqual(parser.finish(), chunks[0])

    def test_error_object_code_mapped(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"error":{"code":"RATE_LIMITED","message":"slow"}}\n\n')
        self.assertEqual(chunks, [StreamChunk(kind="error", error_code="RATE_LIMITED")])

    def test_error_object_unknown_code(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"error":{"code":"some_vendor_code"}}\n\n')
        self.assertEqual(chunks[0].error_code, "INVALID_RESPONSE")

    def test_malformed_json_event_rejected(self) -> None:
        parser = StreamParser()
        with self.assertRaises(AiError) as ctx:
            parser.feed(b"data: not-json\n\n")
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_malformed_event_after_text_not_skipped(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')
        with self.assertRaises(AiError) as ctx:
            parser.feed(b"data: not-json\n\n")
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_non_object_root_rejected(self) -> None:
        for payload in (b"data: [1,2]\n\n", b'data: "str"\n\n', b"data: 42\n\n"):
            with self.subTest(payload=payload):
                parser = StreamParser()
                with self.assertRaises(AiError) as ctx:
                    parser.feed(payload)
                self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_missing_or_empty_choices_rejected(self) -> None:
        for payload in (
            b'data: {"delta":{"content":"x"}}\n\n',
            b'data: {"choices":{}}\n\n',
        ):
            with self.subTest(payload=payload):
                parser = StreamParser()
                with self.assertRaises(AiError) as ctx:
                    parser.feed(payload)
                self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_empty_choices_metadata_tolerated(self) -> None:
        # An SSE event with an empty choices list carries no content and no
        # finish_reason (both live inside choices): it is metadata and must
        # not fail the stream. Regression: the opencode zen gateway sends
        # data: {"choices":[]} after [DONE].
        parser = StreamParser()
        self.assertEqual(parser.feed(b'data: {"choices":[]}\n\n'), [])
        self.assertEqual(
            parser.feed(b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'),
            [
                StreamChunk(kind="text", text="hi"),
                StreamChunk(kind="done", finish_reason="stop", success=True),
            ],
        )

    def test_trailing_empty_choices_after_done_tolerated(self) -> None:
        parser = StreamParser()
        parser.feed(b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n')
        parser.feed(b"data: [DONE]\n\n")
        # the trailing metadata event must be ignored, not raise
        self.assertEqual(parser.feed(b'data: {"choices":[]}\n\n'), [])
        done = parser.finish()
        self.assertTrue(done.success)
        self.assertEqual(parser.text, "hi")

    def test_delta_not_object_rejected(self) -> None:
        parser = StreamParser()
        with self.assertRaises(AiError) as ctx:
            parser.feed(b'data: {"choices":[{"delta":"x"}]}\n\n')
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_content_not_string_rejected(self) -> None:
        for payload in (
            b'data: {"choices":[{"delta":{"content":123}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":["a"]}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":{"a":1}}}]}\n\n',
        ):
            with self.subTest(payload=payload):
                parser = StreamParser()
                with self.assertRaises(AiError) as ctx:
                    parser.feed(payload)
                self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_finish_reason_wrong_type_rejected(self) -> None:
        parser = StreamParser()
        with self.assertRaises(AiError) as ctx:
            parser.feed(b'data: {"choices":[{"delta":{},"finish_reason":42}]}\n\n')
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_reasoning_content_not_shown(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(
            b'data: {"choices":[{"delta":{"reasoning_content":"hidden thinking"}}]}\n\n'
        )
        self.assertEqual(chunks, [])
        self.assertEqual(parser.text, "")
        # reasoning alongside content: only the visible content is surfaced.
        chunks = parser.feed(
            b'data: {"choices":[{"delta":{"reasoning_content":"hidden","content":"shown"}}]}\n\n'
        )
        self.assertEqual(chunks, [StreamChunk(kind="text", text="shown")])

    def test_unknown_extension_fields_not_shown(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(
            b'data: {"choices":[{"delta":{"content":"x","custom_field":123}}],'
            b'"usage":{"total_tokens":5}}\n\n'
        )
        self.assertEqual(chunks, [StreamChunk(kind="text", text="x")])

    def test_only_first_choice_used(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(
            b'data: {"choices":[{"delta":{"content":"first"}},{"delta":{"content":"second"}}]}\n\n'
        )
        self.assertEqual(chunks, [StreamChunk(kind="text", text="first")])
        self.assertEqual(parser.text, "first")


class StreamParserLimitsTests(unittest.TestCase):
    def test_ask_text_cap_exact_boundary(self) -> None:
        content = "a" * (64 * KIB)
        event = ('data:{"choices":[{"delta":{"content":"' + content + '"}}]}\n\n').encode()
        parser = StreamParser(mode="ask")
        for _ in range(32):
            parser.feed(event)
        self.assertEqual(len(parser.text), 2 * MIB)
        with self.assertRaises(AiError) as ctx:
            parser.feed(event)
        self.assertEqual(ctx.exception.code, AiErrorCode.RESPONSE_TOO_LARGE)

    def test_edit_text_cap_exact_boundary(self) -> None:
        content = "a" * (64 * KIB)
        event = ('data:{"choices":[{"delta":{"content":"' + content + '"}}]}\n\n').encode()
        parser = StreamParser(mode="edit")
        for _ in range(4):
            parser.feed(event)
        self.assertEqual(len(parser.text), 256 * KIB)
        with self.assertRaises(AiError) as ctx:
            parser.feed(event)
        self.assertEqual(ctx.exception.code, AiErrorCode.RESPONSE_TOO_LARGE)

    def test_explicit_text_cap_override(self) -> None:
        parser = StreamParser(mode="ask", max_text_bytes=1024)
        event = b'data:{"choices":[{"delta":{"content":"' + b"a" * 1024 + b'"}}]}\n\n'
        parser.feed(event)
        with self.assertRaises(AiError) as ctx:
            parser.feed(event)
        self.assertEqual(ctx.exception.code, AiErrorCode.RESPONSE_TOO_LARGE)

    def test_pre_parser_response_cap_exceeded(self) -> None:
        # A flood of comments must still hit the 8 MiB pre-parser response cap.
        event = b": " + b"a" * 4094 + b"\n\n"
        count = MAX_RESPONSE_BYTES // len(event)
        self.assertGreater(count * len(event) + len(event), MAX_RESPONSE_BYTES)
        parser = StreamParser()
        parser.feed(event * count)
        with self.assertRaises(AiError) as ctx:
            parser.feed(event)
        self.assertEqual(ctx.exception.code, AiErrorCode.RESPONSE_TOO_LARGE)

    def test_single_event_cap_exact_boundary(self) -> None:
        overhead = len('{"choices":[{"delta":{"content":""}}]}')
        n = MAX_EVENT_BYTES - overhead
        payload = '{"choices":[{"delta":{"content":"' + "a" * n + '"}}]}'
        self.assertEqual(len(payload), MAX_EVENT_BYTES)
        parser = StreamParser()
        chunks = parser.feed(b"data:" + payload.encode() + b"\n\n")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].kind, "text")
        self.assertEqual(len(chunks[0].text), n)

    def test_single_event_cap_exceeded_by_second_line(self) -> None:
        overhead = len('{"choices":[{"delta":{"content":""}}]}')
        payload = '{"choices":[{"delta":{"content":"' + "a" * (MAX_EVENT_BYTES - overhead) + '"}}]}'
        parser = StreamParser()
        parser.feed(b"data:" + payload.encode() + b"\n")  # event at exactly 256 KiB
        with self.assertRaises(AiError) as ctx:
            parser.feed(b"data:x\n\n")
        self.assertEqual(ctx.exception.code, AiErrorCode.RESPONSE_TOO_LARGE)

    def test_data_line_cap_exceeded_incrementally(self) -> None:
        # An unterminated line must raise instead of buffering without bound.
        parser = StreamParser()
        with self.assertRaises(AiError) as ctx:
            parser.feed(b"data:" + b"a" * (MAX_LINE_BYTES + 1))
        self.assertEqual(ctx.exception.code, AiErrorCode.RESPONSE_TOO_LARGE)

    def test_data_line_at_exact_cap_then_eof(self) -> None:
        parser = StreamParser()
        parser.feed(b"data:" + b"a" * MAX_LINE_BYTES)
        result = parser.finish()
        self.assertEqual(result, StreamChunk(kind="error", error_code="STREAM_ENDED_EARLY"))

    def test_huge_comment_line_capped(self) -> None:
        parser = StreamParser()
        with self.assertRaises(AiError) as ctx:
            parser.feed(b":" + b"a" * (MAX_LINE_BYTES + 1))
        self.assertEqual(ctx.exception.code, AiErrorCode.RESPONSE_TOO_LARGE)


class StreamParserSniffTests(unittest.TestCase):
    def test_sniff_recognizes_sse_data_prefix(self) -> None:
        parser = StreamParser()
        self.assertEqual(parser.feed(b"dat"), [])  # partial "data" prefix: wait
        chunks = parser.feed(b'a: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
        self.assertEqual(chunks, [StreamChunk(kind="text", text="hi")])

    def test_sniff_recognizes_comment_prefix(self) -> None:
        parser = StreamParser()
        self.assertEqual(parser.feed(b": keepalive\n\n"), [])
        result = parser.finish()
        self.assertEqual(result, StreamChunk(kind="error", error_code="STREAM_ENDED_EARLY"))

    def test_sniff_recognizes_json_object(self) -> None:
        parser = StreamParser()
        body = b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
        self.assertEqual(parser.feed(body), [])
        done = parser.finish()
        self.assertEqual(done, DONE_STOP)
        self.assertEqual(parser.text, "ok")

    def test_sniff_unrecognized_prefix_rejected(self) -> None:
        parser = StreamParser()
        with self.assertRaises(AiError) as ctx:
            parser.feed(b"garbage" * 100)
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_sniff_whitespace_budget_exhausted_rejected(self) -> None:
        parser = StreamParser()
        self.assertEqual(parser.feed(b" " * 4095), [])
        with self.assertRaises(AiError) as ctx:
            parser.feed(b" ")
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_sniff_json_after_4k_whitespace_rejected(self) -> None:
        parser = StreamParser()
        with self.assertRaises(AiError) as ctx:
            parser.feed(b" " * 4096 + b'{"choices":[]}')
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_sniff_whitespace_then_json_accepted(self) -> None:
        parser = StreamParser()
        self.assertEqual(parser.feed(b"\n  "), [])
        parser.feed(b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}')
        done = parser.finish()
        self.assertEqual(done, DONE_STOP)


class StreamParserJsonCompletionTests(unittest.TestCase):
    def test_json_completion_success(self) -> None:
        parser = StreamParser()
        body = b'{"choices":[{"message":{"content":"complete answer"},"finish_reason":"stop"}]}'
        self.assertEqual(parser.feed(body), [])
        done = parser.finish()
        self.assertEqual(done, DONE_STOP)
        self.assertEqual(parser.text, "complete answer")

    def test_json_completion_large_body(self) -> None:
        content = "a" * (128 * KIB)
        body = ('{"choices":[{"message":{"content":"' + content + '"},"finish_reason":"stop"}]}').encode()
        parser = StreamParser()
        parser.feed(body)
        done = parser.finish()
        self.assertTrue(done.success)
        self.assertEqual(parser.text, content)

    def test_json_completion_split_across_chunks(self) -> None:
        body = '{"choices":[{"message":{"content":"你好😀世界"},"finish_reason":"stop"}]}'.encode(
            "utf-8"
        )
        parser = StreamParser()
        for i in range(0, len(body), 7):
            parser.feed(body[i : i + 7])
        done = parser.finish()
        self.assertEqual(done, DONE_STOP)
        self.assertEqual(parser.text, "你好😀世界")

    def test_json_completion_error_object(self) -> None:
        parser = StreamParser()
        parser.feed(b'{"error":{"message":"nope"}}')
        result = parser.finish()
        self.assertEqual(result, StreamChunk(kind="error", error_code="INVALID_RESPONSE"))

    def test_json_completion_truncated_body_ends_early(self) -> None:
        parser = StreamParser()
        parser.feed(b'{"choices":[{"message":{"content":"trunc')
        result = parser.finish()
        self.assertEqual(result, StreamChunk(kind="error", error_code="STREAM_ENDED_EARLY"))

    def test_json_completion_trailing_garbage_rejected(self) -> None:
        parser = StreamParser()
        parser.feed(b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}extra')
        with self.assertRaises(AiError) as ctx:
            parser.finish()
        self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)

    def test_json_completion_non_stop_finish_truncated(self) -> None:
        parser = StreamParser()
        parser.feed(b'{"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}')
        result = parser.finish()
        self.assertEqual(
            result,
            StreamChunk(kind="done", finish_reason="length", success=False, truncated=True),
        )
        self.assertEqual(parser.text, "partial")

    def test_json_completion_text_cap(self) -> None:
        content = "a" * (2 * MIB + 1)
        body = ('{"choices":[{"message":{"content":"' + content + '"},"finish_reason":"stop"}]}').encode()
        parser = StreamParser(mode="ask")
        parser.feed(body)
        with self.assertRaises(AiError) as ctx:
            parser.finish()
        self.assertEqual(ctx.exception.code, AiErrorCode.RESPONSE_TOO_LARGE)

    def test_json_completion_field_validation(self) -> None:
        cases = {
            "root array": b'[{"choices":[]}]',
            "root string": b'"hi"',
            "missing choices": b'{"message":{"content":"x"}}',
            "empty choices": b'{"choices":[]}',
            "choices not array": b'{"choices":{}}',
            "choice not object": b'{"choices":["x"]}',
            "missing message": b'{"choices":[{"finish_reason":"stop"}]}',
            "message not object": b'{"choices":[{"message":"x","finish_reason":"stop"}]}',
            "missing content": b'{"choices":[{"message":{},"finish_reason":"stop"}]}',
            "null content": b'{"choices":[{"message":{"content":null},"finish_reason":"stop"}]}',
            "numeric content": b'{"choices":[{"message":{"content":42},"finish_reason":"stop"}]}',
            "empty content": b'{"choices":[{"message":{"content":""},"finish_reason":"stop"}]}',
            "missing finish_reason": b'{"choices":[{"message":{"content":"x"}}]}',
            "null finish_reason": b'{"choices":[{"message":{"content":"x"},"finish_reason":null}]}',
            "numeric finish_reason": b'{"choices":[{"message":{"content":"x"},"finish_reason":42}]}',
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                parser = StreamParser()
                # Non-object roots are rejected during sniffing (feed), the
                # buffered structural cases during finish(); both must raise.
                with self.assertRaises(AiError) as ctx:
                    parser.feed(body)
                    parser.finish()
                self.assertEqual(ctx.exception.code, AiErrorCode.INVALID_RESPONSE)


class StreamParserMiscTests(unittest.TestCase):
    def test_edit_mode_defaults_text_cap(self) -> None:
        self.assertEqual(StreamParser(mode="edit").max_text_bytes, 256 * KIB)
        self.assertEqual(StreamParser(mode="ask").max_text_bytes, 2 * MIB)

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StreamParser(mode="chat")

    def test_feed_requires_bytes(self) -> None:
        parser = StreamParser()
        with self.assertRaises(TypeError):
            parser.feed("data: not bytes")

    def test_empty_feed_is_noop(self) -> None:
        parser = StreamParser()
        self.assertEqual(parser.feed(b""), [])

    def test_cancel_stops_feed_calls(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
        self.assertEqual(chunks, [StreamChunk(kind="text", text="partial")])
        # Cancellation is transport-level (spec §8.6): the consumer simply
        # stops calling feed(). The parser is pull-driven and produces
        # nothing on its own, so no further chunks can appear.
        self.assertEqual(len(chunks), 1)

    def test_finish_replays_terminal_done_chunk(self) -> None:
        parser = StreamParser()
        chunks = parser.feed(
            b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\n\n'
        )
        done = chunks[-1]
        self.assertEqual(done.kind, "done")
        self.assertEqual(parser.finish(), done)
        self.assertEqual(parser.finish(), done)


if __name__ == "__main__":
    unittest.main()
