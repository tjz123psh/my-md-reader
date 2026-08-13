"""End-to-end integration: fetch_models_catalog over the real AiHttpClient.

Wires the pure catalog flow (services.ai_models) to the real libsoup 3
transport (services.ai_http) against a local loopback stub server. This is the
Phase 3 completion proof that the injected-client seam works with the actual
transport, including status classification and redacted error bodies.
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
    from gi.repository import GLib

    _GI_OK = True
except Exception:
    _GI_OK = False

from mdreader.models.ai import AiError, AiErrorCode
from mdreader.services.ai_http import AiHttpClient, soup_runtime_available
from mdreader.services.ai_models import ModelCatalogResult, fetch_models_catalog

SENTINEL = "sk-mdreader-test-secret-never-log-7d9f"
_RUNTIME_OK = _GI_OK and soup_runtime_available()


def make_handler(status: int, body: bytes) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            self._respond()

        def log_message(self, *args):  # keep stderr quiet
            pass

    return Handler


@contextlib.contextmanager
def server_for(status: int, body: bytes):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(status, body))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_until(predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    context = GLib.MainContext.default()
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


class Recorder:
    def __init__(self) -> None:
        self.result: ModelCatalogResult | None = None
        self.error: AiError | None = None


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class FetchModelsOverHttpTests(unittest.TestCase):
    def test_success_roundtrip_over_real_transport(self) -> None:
        payload = json.dumps(
            {"object": "list", "data": [{"id": "gpt-4o"}, {"id": "llama-3"}]}
        ).encode("utf-8")
        with server_for(200, payload) as port:
            recorder = Recorder()
            fetch_models_catalog(
                AiHttpClient(),
                endpoint_url=f"http://127.0.0.1:{port}/v1/models",
                authorization="",
                on_result=lambda r: setattr(recorder, "result", r),
                on_error=lambda e: setattr(recorder, "error", e),
            )
            run_until(lambda: recorder.result is not None or recorder.error is not None)
        self.assertIsNone(recorder.error)
        assert recorder.result is not None
        self.assertEqual([m.model_id for m in recorder.result.models], ["gpt-4o", "llama-3"])

    def test_empty_catalog_is_success_over_real_transport(self) -> None:
        with server_for(200, b'{"data": []}') as port:
            recorder = Recorder()
            fetch_models_catalog(
                AiHttpClient(),
                endpoint_url=f"http://127.0.0.1:{port}/v1/models",
                authorization="",
                on_result=lambda r: setattr(recorder, "result", r),
                on_error=lambda e: setattr(recorder, "error", e),
            )
            run_until(lambda: recorder.result is not None or recorder.error is not None)
        self.assertIsNone(recorder.error)
        assert recorder.result is not None
        self.assertEqual(recorder.result.models, ())

    def test_401_classified_without_leaking_secret(self) -> None:
        body = json.dumps({"error": f"bad key {SENTINEL}"}).encode("utf-8")
        with server_for(401, body) as port:
            recorder = Recorder()
            fetch_models_catalog(
                AiHttpClient(),
                endpoint_url=f"http://127.0.0.1:{port}/v1/models",
                authorization=f"Bearer {SENTINEL}",
                secret=SENTINEL,
                on_result=lambda r: setattr(recorder, "result", r),
                on_error=lambda e: setattr(recorder, "error", e),
            )
            run_until(lambda: recorder.result is not None or recorder.error is not None)
        assert recorder.error is not None
        self.assertIs(recorder.error.code, AiErrorCode.AUTHENTICATION_FAILED)
        self.assertNotIn(SENTINEL, recorder.error.detail)
        self.assertIsNone(recorder.result)

    def test_malformed_body_is_invalid_response(self) -> None:
        with server_for(200, b"<html>not json</html>") as port:
            recorder = Recorder()
            fetch_models_catalog(
                AiHttpClient(),
                endpoint_url=f"http://127.0.0.1:{port}/v1/models",
                authorization="",
                on_result=lambda r: setattr(recorder, "result", r),
                on_error=lambda e: setattr(recorder, "error", e),
            )
            run_until(lambda: recorder.result is not None or recorder.error is not None)
        assert recorder.error is not None
        self.assertIs(recorder.error.code, AiErrorCode.INVALID_RESPONSE)


if __name__ == "__main__":
    unittest.main()
