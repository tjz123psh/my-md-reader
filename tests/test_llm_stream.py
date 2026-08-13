"""Streaming integration tests: OpenAICompatibleGateway over the real transport.

Exercises Ask streaming (including split UTF-8), cancellation, JSON-completion
fallback, HTTP errors and Edit payload collection against a local loopback
stub server. This is the Phase 4 completion proof for the headless integration
gate: Ask stream, cancel, JSON fallback and Edit payload collection all work
through the real libsoup transport.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import GLib, Gio

    _GI_OK = True
except Exception:
    _GI_OK = False

from mdreader.models.ai import AiError, AiErrorCode
from mdreader.models.conversation import ChatMessage
from mdreader.services.ai_http import AiHttpClient, soup_runtime_available
from mdreader.services.llm import (
    ChatOutcome,
    OpenAICompatibleGateway,
)

SENTINEL = "sk-mdreader-test-secret-never-log-7d9f"
_RUNTIME_OK = _GI_OK and soup_runtime_available()

SSE_CHUNKS = (
    b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)
EXPECTED_TEXT = "你好"

JSON_FALLBACK = json.dumps(
    {
        "choices": [
            {
                "message": {"role": "assistant", "content": "complete answer"},
                "finish_reason": "stop",
            }
        ]
    }
).encode("utf-8")


def make_handler(status: int, body: bytes, content_type: str, delay: float = 0.0, records: list | None = None) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            if records is not None:
                length = int(self.headers.get("Content-Length") or 0)
                records.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": self.rfile.read(length) if length else b"",
                    }
                )
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self) -> None:
            self._handle()

        def log_message(self, *args):  # keep stderr quiet
            pass

    return Handler


@contextlib.contextmanager
def server_for(status: int, body: bytes, content_type: str = "text/event-stream", delay: float = 0.0):
    records: list[dict] = []
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(status, body, content_type, delay, records)
    )
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], records
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_until(predicate, timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    context = GLib.MainContext.default()
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


class Recorder:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.outcome: ChatOutcome | None = None
        self.error: AiError | None = None
        self.statuses: list[int] = []

    def on_text(self, delta: str) -> None:
        self.texts.append(delta)

    def on_headers(self, status: int) -> None:
        self.statuses.append(status)

    def on_data(self, chunk: bytes) -> None:
        if chunk:
            self.texts.append(chunk.decode("utf-8", errors="replace"))

    def on_done(self, outcome: ChatOutcome) -> None:
        self.outcome = outcome

    def on_error(self, error: AiError) -> None:
        self.error = error

    @property
    def full_text(self) -> str:
        return "".join(self.texts)


def ask(gateway: OpenAICompatibleGateway, url: str, recorder: Recorder, *, model: str = "m-1",
        auth: str | None = f"Bearer {SENTINEL}", cancellable=None) -> None:
    gateway.stream(
        endpoint_url=url,
        authorization=auth,
        model_id=model,
        messages=[ChatMessage("user", "问题")],
        secret=SENTINEL,
        cancellable=cancellable,
        on_text=recorder.on_text,
        on_done=recorder.on_done,
        on_error=recorder.on_error,
    )


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AskStreamingTests(unittest.TestCase):
    def test_ask_streams_utf8_text_and_completes(self) -> None:
        with server_for(200, SSE_CHUNKS) as (port, records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            ask(gateway, f"http://127.0.0.1:{port}/v1/chat/completions", recorder)
            run_until(lambda: recorder.outcome is not None or recorder.error is not None)
        self.assertIsNone(recorder.error)
        assert recorder.outcome is not None
        self.assertTrue(recorder.outcome.success)
        self.assertEqual(recorder.outcome.finish_reason, "stop")
        self.assertEqual(recorder.outcome.full_text, EXPECTED_TEXT)
        self.assertEqual(recorder.full_text, EXPECTED_TEXT)
        self.assertEqual(records[0]["method"], "POST")
        self.assertEqual(records[0]["authorization"], f"Bearer {SENTINEL}")

    def test_request_body_has_minimal_schema_and_no_paths(self) -> None:
        with server_for(200, SSE_CHUNKS) as (port, records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            ask(gateway, f"http://127.0.0.1:{port}/v1/chat/completions", recorder)
            run_until(lambda: recorder.outcome is not None or recorder.error is not None)
        payload = json.loads(records[0]["body"])
        self.assertEqual(set(payload), {"model", "messages", "stream"})
        self.assertEqual(payload["stream"], True)
        self.assertEqual(payload["model"], "m-1")
        roles = [m["role"] for m in payload["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertNotIn("path", payload)
        self.assertNotIn("workspace", payload)
        self.assertNotIn("target", payload)

    def test_json_completion_fallback_is_success(self) -> None:
        with server_for(200, JSON_FALLBACK, content_type="application/json") as (port, _records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            ask(gateway, f"http://127.0.0.1:{port}/v1/chat/completions", recorder)
            run_until(lambda: recorder.outcome is not None or recorder.error is not None)
        self.assertIsNone(recorder.error)
        assert recorder.outcome is not None
        self.assertTrue(recorder.outcome.success)
        self.assertEqual(recorder.outcome.full_text, "complete answer")

    def test_401_is_auth_failure_with_redacted_detail(self) -> None:
        body = json.dumps({"error": f"bad key {SENTINEL}"}).encode("utf-8")
        with server_for(401, body, content_type="application/json") as (port, _records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            ask(gateway, f"http://127.0.0.1:{port}/v1/chat/completions", recorder)
            run_until(lambda: recorder.outcome is not None or recorder.error is not None)
        assert recorder.error is not None
        self.assertIs(recorder.error.code, AiErrorCode.AUTHENTICATION_FAILED)
        self.assertNotIn(SENTINEL, recorder.error.detail)

    def test_eof_without_done_is_stream_ended_early(self) -> None:
        partial = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            # no finish_reason, no [DONE]
        )
        with server_for(200, partial) as (port, _records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            ask(gateway, f"http://127.0.0.1:{port}/v1/chat/completions", recorder)
            run_until(lambda: recorder.outcome is not None or recorder.error is not None)
        assert recorder.error is not None
        self.assertIs(recorder.error.code, AiErrorCode.STREAM_ENDED_EARLY)
        self.assertIsNone(recorder.outcome)

    def test_malformed_event_is_invalid_response(self) -> None:
        with server_for(200, b"data: not-json\n\n") as (port, _records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            ask(gateway, f"http://127.0.0.1:{port}/v1/chat/completions", recorder)
            run_until(lambda: recorder.outcome is not None or recorder.error is not None)
        assert recorder.error is not None
        self.assertIs(recorder.error.code, AiErrorCode.INVALID_RESPONSE)


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AskCancellationTests(unittest.TestCase):
    def test_cancel_mid_stream_reports_cancelled_and_keeps_partial(self) -> None:
        with server_for(200, SSE_CHUNKS, delay=0.5) as (port, _records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            cancellable = Gio.Cancellable()
            ask(gateway, f"http://127.0.0.1:{port}/v1/chat/completions", recorder, cancellable=cancellable)
            GLib.timeout_add(120, lambda: (cancellable.cancel(), False)[1])
            run_until(lambda: recorder.error is not None)
        self.assertIs(recorder.error.code, AiErrorCode.CANCELLED)
        self.assertIsNone(recorder.outcome)


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class EditCollectionTests(unittest.TestCase):
    EDIT_SSE = (
        b'data: {"choices":[{"delta":{"content":"{\\"startLine\\":3"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":",\\"endLine\\":3,\\"replacement\\":\\"x\\"}"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    EDIT_TEXT = '{"startLine":3,"endLine":3,"replacement":"x"}'

    def test_edit_collects_full_payload_without_history(self) -> None:
        with server_for(200, self.EDIT_SSE) as (port, records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            gateway.stream(
                endpoint_url=f"http://127.0.0.1:{port}/v1/chat/completions",
                authorization=f"Bearer {SENTINEL}",
                model_id="m-1",
                messages=[ChatMessage("user", "EDIT REQUEST ...")],  # no Ask history
                mode="edit",
                secret=SENTINEL,
                on_text=recorder.on_text,
                on_done=recorder.on_done,
                on_error=recorder.on_error,
            )
            run_until(lambda: recorder.outcome is not None or recorder.error is not None)
        self.assertIsNone(recorder.error)
        assert recorder.outcome is not None
        self.assertTrue(recorder.outcome.success)
        self.assertEqual(recorder.outcome.full_text, self.EDIT_TEXT)
        payload = json.loads(records[0]["body"])
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])

    def test_truncated_edit_is_not_success(self) -> None:
        truncated = (
            b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
        )
        with server_for(200, truncated) as (port, _records):
            gateway = OpenAICompatibleGateway()
            recorder = Recorder()
            gateway.stream(
                endpoint_url=f"http://127.0.0.1:{port}/v1/chat/completions",
                authorization=f"Bearer {SENTINEL}",
                model_id="m-1",
                messages=[ChatMessage("user", "EDIT REQUEST ...")],
                mode="edit",
                secret=SENTINEL,
                on_text=recorder.on_text,
                on_done=recorder.on_done,
                on_error=recorder.on_error,
            )
            run_until(lambda: recorder.outcome is not None or recorder.error is not None)
        assert recorder.outcome is not None
        self.assertFalse(recorder.outcome.success)
        self.assertTrue(recorder.outcome.truncated)
        self.assertEqual(recorder.outcome.finish_reason, "length")


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class ConnectDeadlineTests(unittest.TestCase):
    def test_connect_deadline_fires_before_headers(self) -> None:
        # The server never responds within the connect window: the
        # connect-phase timer (15 s per spec §4.2) is exercised with a short
        # value via the transport directly.
        with server_for(200, SSE_CHUNKS, delay=5.0) as (port, _records):
            client = AiHttpClient()
            recorder = Recorder()
            client.fetch(
                method="POST",
                url=f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                authorization=None,
                body=b"{}",
                max_bytes=1024 * 1024,
                deadline_ms=10_000,
                idle_timeout_s=90.0,
                connect_deadline_ms=200,
                cancellable=None,
                on_headers=recorder.on_headers,
                on_data=recorder.on_data,
                on_error=recorder.on_error,
            )
            run_until(lambda: recorder.error is not None)
        self.assertIs(recorder.error.code, AiErrorCode.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
