"""Chat completions gateway tests for the direct LLM migration
(docs/LLM_PROVIDER_MIGRATION_SPEC.md §8.1/§8.2/§11.4/§11.5/§13.9).

Phase 4 red test for ``mdreader.services.llm``: targets the provider-neutral
OpenAI-compatible chat gateway. The transport is an injected fake client that
records the fetch call and replays a script of on_headers/on_data/on_error
callbacks, so the gateway is exercised with no network and no GTK.

Skip semantics: when the Gio GI typelibs are missing the gateway stream tests
skip with an explicit reason (the gateway needs a real Gio.Cancellable for
the internal cancellation wiring) — a skip is never reported as a pass
(spec §13.1.8).
"""

from __future__ import annotations

import json
import unittest

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio

    _GI_OK = True
except Exception:
    _GI_OK = False

from mdreader.models.ai import AiError, AiErrorCode
from mdreader.models.conversation import ChatMessage
from mdreader.services.llm import (
    CHAT_BODY_LIMIT,
    CHAT_CONNECT_TIMEOUT_MS,
    CHAT_DEADLINE_MS,
    CHAT_IDLE_TIMEOUT_S,
    EDIT_SYSTEM_PROMPT,
    MAX_QUESTION_CHARACTERS,
    MAX_REQUEST_BYTES,
    SYSTEM_PROMPT,
    ChatOutcome,
    OpenAICompatibleGateway,
    build_chat_request,
    validate_question,
)

SENTINEL = "sk-mdreader-test-secret-never-log-7d9f"
ENDPOINT = "https://api.example/v1/chat/completions"


def sse(payload: str) -> bytes:
    """Wrap one JSON payload as an SSE data event."""
    return f"data: {payload}\n\n".encode("utf-8")


def code_of(call) -> AiErrorCode:
    """Return the AiErrorCode raised by callable ``call``."""
    with unittest.TestCase().assertRaises(AiError) as ctx:
        call()
    return ctx.exception.code


class FakeClient:
    """Records the fetch call and replays a script of transport callbacks.

    Mirrors the real transport convention: ``on_data(b"")`` marks EOF and
    ``on_headers`` fires once with the final status.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.script: list[tuple[str, object]] = []

    def fetch(self, **kwargs) -> None:
        self.calls.append(kwargs)
        for kind, value in self.script:
            if kind == "headers":
                kwargs["on_headers"](value)
            elif kind == "data":
                kwargs["on_data"](value)
            elif kind == "error":
                kwargs["on_error"](value)


class ValidateQuestionTests(unittest.TestCase):
    def test_maximum_length_passes(self) -> None:
        validate_question("问" * MAX_QUESTION_CHARACTERS)

    def test_over_maximum_length_is_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: validate_question("a" * (MAX_QUESTION_CHARACTERS + 1))),
            AiErrorCode.REQUEST_REJECTED,
        )

    def test_empty_question_is_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: validate_question("")),
            AiErrorCode.REQUEST_REJECTED,
        )


class BuildChatRequestTests(unittest.TestCase):
    def _build(
        self,
        *,
        messages,
        model_id: str = "gpt-4o-mini",
        system_prompt: str = "system text",
        stream: bool = True,
    ) -> bytes:
        return build_chat_request(
            model_id=model_id,
            system_prompt=system_prompt,
            messages=messages,
            stream=stream,
        )

    def test_schema_has_exactly_three_keys(self) -> None:
        payload = json.loads(
            self._build(messages=(ChatMessage("user", "question"),)).decode("utf-8")
        )
        self.assertEqual(set(payload.keys()), {"model", "messages", "stream"})
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertIs(payload["stream"], True)

    def test_messages_prepend_system_and_keep_roles(self) -> None:
        payload = json.loads(
            self._build(
                messages=(
                    ChatMessage("user", "first"),
                    ChatMessage("assistant", "reply"),
                    ChatMessage("user", "second question"),
                )
            ).decode("utf-8")
        )
        self.assertEqual(
            [message["role"] for message in payload["messages"]],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(payload["messages"][0]["content"], "system text")
        self.assertEqual(payload["messages"][-1]["content"], "second question")

    def test_no_path_workspace_or_target_keys(self) -> None:
        payload = json.loads(
            self._build(messages=(ChatMessage("user", "q"),)).decode("utf-8")
        )
        for forbidden in ("path", "workspace", "target"):
            self.assertNotIn(forbidden, payload)
        for message in payload["messages"]:
            self.assertEqual(set(message.keys()), {"role", "content"})

    def _sized_body(self, pad: int) -> bytes:
        # One ASCII char in an assistant message adds exactly one byte to the
        # serialized body, so padding lands precisely on the 128 KiB boundary.
        return self._build(
            messages=(
                ChatMessage("user", "q"),
                ChatMessage("assistant", "a" * pad),
                ChatMessage("user", "final"),
            )
        )

    def test_body_at_exact_max_size_is_accepted(self) -> None:
        pad = MAX_REQUEST_BYTES - len(self._sized_body(0))
        self.assertGreater(pad, 0)
        self.assertEqual(len(self._sized_body(pad)), MAX_REQUEST_BYTES)

    def test_body_one_byte_over_max_size_is_rejected(self) -> None:
        pad = MAX_REQUEST_BYTES - len(self._sized_body(0))
        self.assertEqual(
            code_of(lambda: self._sized_body(pad + 1)),
            AiErrorCode.REQUEST_REJECTED,
        )

    def test_large_envelope_with_short_question_is_accepted(self) -> None:
        # The 8000-character cap applies to the user question, not to the
        # context envelope (§6.1). A long but legitimate envelope under the
        # 128 KiB body cap must not be rejected by the question check.
        envelope = ChatMessage("user", "这是上下文" * 3000)  # 15k chars
        self.assertEqual(len(envelope.text), 15000)
        self._build(messages=(envelope,))  # must not raise

    def test_missing_user_message_is_rejected(self) -> None:
        for messages in (
            (),
            (ChatMessage("assistant", "x"),),
            (ChatMessage("system", "x"),),
        ):
            with self.subTest(messages=messages):
                self.assertEqual(
                    code_of(lambda messages=messages: self._build(messages=messages)),
                    AiErrorCode.REQUEST_REJECTED,
                )


class SystemPromptTests(unittest.TestCase):
    def test_prompts_never_mention_opencode(self) -> None:
        self.assertNotIn("opencode", SYSTEM_PROMPT.lower())
        self.assertNotIn("opencode", EDIT_SYSTEM_PROMPT.lower())

    def test_ask_prompt_keeps_security_intent(self) -> None:
        for keyword in (
            "untrusted",
            "narrow",
            "sidebar",
            "hidden reasoning",
            "must not claim",
            "no tools",
        ):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, SYSTEM_PROMPT)

    def test_edit_prompt_is_independent_structured_json(self) -> None:
        self.assertNotEqual(SYSTEM_PROMPT, EDIT_SYSTEM_PROMPT)
        for keyword in (
            "startLine",
            "endLine",
            "replacement",
            "untrusted",
            "hidden reasoning",
        ):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, EDIT_SYSTEM_PROMPT)


@unittest.skipUnless(_GI_OK, "Gio GI typelibs unavailable")
class StreamGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.texts: list[str] = []
        self.done: list[ChatOutcome] = []
        self.errors: list[AiError] = []

    def _gateway(
        self, script: list[tuple[str, object]]
    ) -> tuple[OpenAICompatibleGateway, FakeClient]:
        client = FakeClient()
        client.script = script
        return OpenAICompatibleGateway(client=client), client

    def _stream(self, gateway: OpenAICompatibleGateway, **overrides) -> None:
        params = dict(
            endpoint_url=ENDPOINT,
            authorization=f"Bearer {SENTINEL}",
            model_id="gpt-4o-mini",
            system_prompt=SYSTEM_PROMPT,
            messages=(ChatMessage("user", "What is the excerpt about?"),),
            mode="ask",
            secret=SENTINEL,
            cancellable=None,
            on_text=self.texts.append,
            on_done=self.done.append,
            on_error=self.errors.append,
        )
        params.update(overrides)
        gateway.stream(**params)

    def test_stream_success_concatenates_text_and_reports_done(self) -> None:
        script = [
            ("headers", 200),
            ("data", sse('{"choices":[{"delta":{"content":"Hel"}}]}')),
            ("data", sse('{"choices":[{"delta":{"content":"lo"}}]}')),
            ("data", sse('{"choices":[{"delta":{},"finish_reason":"stop"}]}')),
            ("data", b"data: [DONE]\n\n"),
            ("data", b""),
        ]
        gateway, _ = self._gateway(script)
        self._stream(gateway)
        self.assertEqual(self.texts, ["Hel", "lo"])
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.done), 1)
        outcome = self.done[0]
        self.assertTrue(outcome.success)
        self.assertFalse(outcome.truncated)
        self.assertEqual(outcome.finish_reason, "stop")
        self.assertEqual(outcome.full_text, "Hello")

    def test_fetch_uses_chat_completions_parameters(self) -> None:
        gateway, client = self._gateway([("headers", 200), ("data", b"")])
        self._stream(gateway)
        call = client.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], ENDPOINT)
        self.assertEqual(
            call["headers"],
            {
                "Accept": "text/event-stream, application/json",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(call["authorization"], f"Bearer {SENTINEL}")
        self.assertEqual(call["max_bytes"], CHAT_BODY_LIMIT)
        self.assertEqual(call["deadline_ms"], CHAT_DEADLINE_MS)
        self.assertEqual(call["idle_timeout_s"], CHAT_IDLE_TIMEOUT_S)
        self.assertEqual(call["connect_deadline_ms"], CHAT_CONNECT_TIMEOUT_MS)
        self.assertIsNotNone(call["cancellable"])

    def test_401_maps_to_authentication_failed_and_redacts_body(self) -> None:
        script = [
            ("headers", 401),
            ("data", b'{"error": {"message": "bad key ' + SENTINEL.encode() + b'"}}'),
            ("data", b""),
        ]
        gateway, _ = self._gateway(script)
        self._stream(gateway)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.errors[0].code, AiErrorCode.AUTHENTICATION_FAILED)
        self.assertNotIn(SENTINEL, self.errors[0].detail)
        self.assertEqual(self.done, [])

    def test_5xx_maps_to_provider_unavailable(self) -> None:
        gateway, _ = self._gateway([("headers", 500), ("data", b"boom"), ("data", b"")])
        self._stream(gateway)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.errors[0].code, AiErrorCode.PROVIDER_UNAVAILABLE)

    def test_transport_cancel_is_passed_through(self) -> None:
        error = AiError(AiErrorCode.CANCELLED, "request cancelled")
        gateway, _ = self._gateway([("error", error)])
        self._stream(gateway)
        self.assertEqual(len(self.errors), 1)
        self.assertIs(self.errors[0], error)
        self.assertEqual(self.done, [])

    def test_parser_hard_error_cancels_internal_and_reports_once(self) -> None:
        script = [
            ("headers", 200),
            ("data", b"data: not-json\n\n"),
            ("error", AiError(AiErrorCode.NETWORK_FAILED, "late transport error")),
            ("data", b""),
        ]
        gateway, client = self._gateway(script)
        self._stream(gateway)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.errors[0].code, AiErrorCode.INVALID_RESPONSE)
        self.assertTrue(client.calls[0]["cancellable"].is_cancelled())
        self.assertEqual(self.done, [])

    def test_empty_model_is_rejected_without_fetch(self) -> None:
        gateway, client = self._gateway([])
        self._stream(gateway, model_id="")
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.errors[0].code, AiErrorCode.MODEL_NOT_SELECTED)
        self.assertEqual(client.calls, [])

    def test_eof_without_termination_is_stream_ended_early(self) -> None:
        script = [
            ("headers", 200),
            ("data", sse('{"choices":[{"delta":{"content":"partial"}}]}')),
            ("data", b""),
        ]
        gateway, _ = self._gateway(script)
        self._stream(gateway)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.errors[0].code, AiErrorCode.STREAM_ENDED_EARLY)
        self.assertEqual(self.done, [])

    def test_edit_mode_request_has_no_history(self) -> None:
        gateway, client = self._gateway([("headers", 200), ("data", b"")])
        self._stream(
            gateway,
            mode="edit",
            system_prompt=EDIT_SYSTEM_PROMPT,
            messages=(ChatMessage("user", "EDIT REQUEST: replace line 3"),),
        )
        body = json.loads(client.calls[0]["body"].decode("utf-8"))
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], EDIT_SYSTEM_PROMPT)

    def test_edit_mode_parser_applies_256k_text_limit(self) -> None:
        # Three 100 KiB chunks stay under the 256 KiB SSE line/event caps but
        # exceed the Edit 256 KiB text budget (an Ask parser allows 2 MiB).
        part = "x" * (100 * 1024)
        event = '{"choices":[{"delta":{"content":"%s"}}]}' % part
        script = [
            ("headers", 200),
            ("data", sse(event)),
            ("data", sse(event)),
            ("data", sse(event)),
            ("data", b""),
        ]
        gateway, _ = self._gateway(script)
        self._stream(
            gateway,
            mode="edit",
            messages=(ChatMessage("user", "EDIT REQUEST"),),
        )
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.errors[0].code, AiErrorCode.RESPONSE_TOO_LARGE)

    def test_request_body_is_bounded_and_path_free(self) -> None:
        gateway, client = self._gateway([("headers", 200), ("data", b"")])
        self._stream(gateway)
        body = client.calls[0]["body"]
        self.assertLessEqual(len(body), MAX_REQUEST_BYTES)
        payload = json.loads(body.decode("utf-8"))
        for forbidden in ("path", "workspace", "target"):
            self.assertNotIn(forbidden, payload)

    def test_user_cancellation_is_wired_to_internal_cancellable(self) -> None:
        gateway, client = self._gateway([("headers", 200), ("data", b"")])
        user_cancellable = Gio.Cancellable()
        user_cancellable.cancel()
        self._stream(gateway, cancellable=user_cancellable)
        self.assertTrue(client.calls[0]["cancellable"].is_cancelled())


if __name__ == "__main__":
    unittest.main()
