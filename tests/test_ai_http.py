"""libsoup 3 HTTP transport integration tests for the direct LLM migration
(docs/LLM_PROVIDER_MIGRATION_SPEC.md §4.2/§6.3/§6.4/§7.4/§13.4).

This file is the Phase 3 red test for ``mdreader.services.ai_http``: it drives
the client against a local loopback stub server (ThreadingHTTPServer on
127.0.0.1) from the GLib default main context. No public network is ever
touched.

Skip semantics: when the Soup/Gio GI typelibs or openssl are missing the
affected tests skip with an explicit ``UNAVAILABLE`` reason — a skip is never
reported as a pass (spec §13.1.8).

Credential discipline (spec §4.1/§13.7): the shared fake key
``sk-mdreader-test-secret-never-log-7d9f`` is used and asserted to never
appear in transport error codes, details or statuses.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import gi

    gi.require_version("Soup", "3.0")
    gi.require_version("Gio", "2.0")
    from gi.repository import GLib, Gio  # noqa: E402

    _GI_OK = True
except Exception:
    _GI_OK = False

from mdreader.models.ai import AiError, AiErrorCode
from mdreader.services.ai_http import (
    AiHttpClient,
    map_http_status,
    soup_runtime_available,
)

BEARER = "Bearer sk-mdreader-test-secret-never-log-7d9f"
UA = "MDReader/1.0"

_RUNTIME_OK = _GI_OK and soup_runtime_available()


def make_handler(config: dict) -> type:
    """Build a stub handler bound to ``config``.

    Supported config keys:

    - hits: append a per-request dict (method, path, authorization, content
      type, accept, user agent, body) — the cross-origin/proxy listeners'
      hit lists are the security evidence.
    - status/body/content_type: the non-redirect response.
    - delay: sleep before responding (seconds).
    - redirect_path + redirect_status + location: return a redirect for the
      matching path only.
    - chain: dict path -> Location; any path in the chain redirects (for the
      hop-limit and loop tests).
    - chunked_body: when set, respond with chunked transfer encoding and no
      Content-Length so the size cap can only be enforced on actual bytes.
    - extra_headers: extra response headers (Content-Length is set
      automatically unless the config provides it).
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _record(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            config["hits"].append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "accept": self.headers.get("Accept"),
                    "user_agent": self.headers.get("User-Agent"),
                    "body": body,
                }
            )

        def _send(self, status: int, body: bytes, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header(
                "Content-Type", config.get("content_type", "application/json")
            )
            extra = extra or config.get("extra_headers") or {}
            for name, value in extra.items():
                self.send_header(name, value)
            if "Content-Length" not in extra:
                self.send_header("Content-Length", str(len(body)))
            # One request per connection keeps stub handler threads from
            # lingering on keep-alive sockets after the test server closes.
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _handle(self) -> None:
            self._record()
            delay = config.get("delay", 0.0)
            if delay:
                time.sleep(delay)
            chain = config.get("chain") or {}
            location = chain.get(self.path)
            if location is not None:
                self._send(302, b"", {"Location": location})
                return
            if config.get("redirect_path") == self.path:
                self._send(
                    config.get("redirect_status", 302),
                    b"",
                    {"Location": config.get("location")}
                    if config.get("location") is not None
                    else {},
                )
                return
            chunked_body = config.get("chunked_body")
            if chunked_body is not None:
                self.send_response(config.get("status", 200))
                self.send_header(
                    "Content-Type", config.get("content_type", "application/json")
                )
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(
                        b"%x\r\n" % len(chunked_body)
                        + chunked_body
                        + b"\r\n0\r\n\r\n"
                    )
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self._send(config.get("status", 200), config.get("body", b'{"data": []}'))

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def log_message(self, *args):  # keep stderr quiet
            pass

    return Handler


@contextlib.contextmanager
def server_for(config: dict, handler_cls=None):
    cls = handler_cls or make_handler(config)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    thread = threading.Thread(
        target=httpd.serve_forever,
        kwargs={"poll_interval": 0.02},
        daemon=True,
    )
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_until(predicate, timeout_s: float = 5.0) -> None:
    """Iterate the GLib default main context until ``predicate`` holds."""
    deadline = time.monotonic() + timeout_s
    context = GLib.MainContext.default()
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


class Recorder:
    """Collects the transport's callbacks for one fetch."""

    def __init__(self) -> None:
        self.statuses: list[int] = []
        self.chunks: list[bytes] = []
        self.error: AiError | None = None

    def on_headers(self, status: int) -> None:
        self.statuses.append(status)

    def on_data(self, data: bytes) -> None:
        self.chunks.append(data)

    def on_error(self, error: AiError) -> None:
        self.error = error

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    @property
    def done(self) -> bool:
        # The transport signals end-of-body with a final empty on_data chunk;
        # success is reaching that marker without an on_error.
        return bool(self.statuses) and bool(self.chunks) and self.chunks[-1] == b""


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    authorization: str | None = BEARER,
    body: bytes | None = None,
    max_bytes: int = 2 * 1024 * 1024,
    deadline_ms: int = 5000,
    idle_timeout_s: float = 5.0,
    cancellable=None,
    user_agent: str = UA,
) -> tuple[AiHttpClient, Recorder]:
    client = AiHttpClient(user_agent=user_agent)
    recorder = Recorder()
    client.fetch(
        method=method,
        url=url,
        headers=headers or {"Accept": "application/json"},
        authorization=authorization,
        body=body,
        max_bytes=max_bytes,
        deadline_ms=deadline_ms,
        idle_timeout_s=idle_timeout_s,
        cancellable=cancellable,
        on_headers=recorder.on_headers,
        on_data=recorder.on_data,
        on_error=recorder.on_error,
    )
    run_until(lambda: recorder.error is not None or recorder.done)
    return client, recorder


def code_of(recorder: Recorder) -> AiErrorCode:
    assert recorder.error is not None
    return recorder.error.code


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AiHttpSuccessTests(unittest.TestCase):
    def test_soup_runtime_probe_returns_bool(self) -> None:
        self.assertIn(soup_runtime_available(), (True, False))

    def test_get_with_bearer_sends_expected_headers(self) -> None:
        config = {"hits": [], "status": 200, "body": b'{"data": [{"id": "m"}]}'}
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/v1/models")
        self.assertIsNone(recorder.error)
        self.assertEqual(recorder.statuses, [200])
        self.assertEqual(recorder.body, b'{"data": [{"id": "m"}]}')
        self.assertEqual(len(config["hits"]), 1)
        hit = config["hits"][0]
        self.assertEqual(hit["authorization"], BEARER)
        self.assertEqual(hit["user_agent"], UA)
        self.assertEqual(hit["accept"], "application/json")
        self.assertEqual(hit["method"], "GET")
        self.assertEqual(hit["path"], "/v1/models")

    def test_none_mode_does_not_send_authorization(self) -> None:
        config = {"hits": [], "status": 200, "body": b'{"data": []}'}
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/models", authorization=None)
        self.assertIsNone(recorder.error)
        self.assertIsNone(config["hits"][0]["authorization"])

    def test_post_sends_body_and_content_type(self) -> None:
        payload = b'{"model": "m", "stream": true}'
        config = {"hits": [], "status": 200, "body": b'{"choices": []}'}
        with server_for(config) as port:
            _, recorder = fetch(
                f"http://127.0.0.1:{port}/chat/completions",
                method="POST",
                body=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        self.assertIsNone(recorder.error)
        self.assertEqual(recorder.statuses, [200])
        hit = config["hits"][0]
        self.assertEqual(hit["method"], "POST")
        self.assertEqual(hit["body"], payload)
        self.assertEqual(hit["content_type"], "application/json")

    def test_error_details_never_contain_the_key(self) -> None:
        # Force a transport error (deadline) and check the key is absent from
        # every surfaced string form.
        config = {"hits": [], "delay": 5.0, "status": 200, "body": b"{}"}
        with server_for(config) as port:
            _, recorder = fetch(
                f"http://127.0.0.1:{port}/models", deadline_ms=150, max_bytes=100
            )
        self.assertIs(recorder.error.code, AiErrorCode.TIMEOUT)
        self.assertNotIn(BEARER, str(recorder.error))
        self.assertNotIn(BEARER, recorder.error.detail)


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AiHttpStatusClassificationTests(unittest.TestCase):
    def test_map_http_status_covers_all_required_codes(self) -> None:
        expected = {
            401: AiErrorCode.AUTHENTICATION_FAILED,
            403: AiErrorCode.PERMISSION_DENIED,
            404: AiErrorCode.ENDPOINT_NOT_FOUND,
            405: AiErrorCode.ENDPOINT_NOT_FOUND,
            402: AiErrorCode.BILLING_OR_QUOTA_REQUIRED,
            429: AiErrorCode.RATE_LIMITED,
            400: AiErrorCode.REQUEST_REJECTED,
            422: AiErrorCode.REQUEST_REJECTED,
            500: AiErrorCode.PROVIDER_UNAVAILABLE,
            503: AiErrorCode.PROVIDER_UNAVAILABLE,
            418: AiErrorCode.PROVIDER_UNAVAILABLE,
            409: AiErrorCode.PROVIDER_UNAVAILABLE,
        }
        for status, code in expected.items():
            with self.subTest(status=status):
                self.assertIs(map_http_status(status), code)

    def test_http_error_statuses_are_delivered_as_responses(self) -> None:
        # Non-2xx HTTP statuses are responses, not transport failures: the
        # transport delivers them via on_headers and the caller classifies.
        for status in (401, 403, 404, 405, 429, 500):
            config = {"hits": [], "status": status, "body": b'{"error": "x"}'}
            with server_for(config) as port:
                _, recorder = fetch(f"http://127.0.0.1:{port}/models")
            with self.subTest(status=status):
                self.assertIsNone(recorder.error)
                self.assertEqual(recorder.statuses, [status])
                self.assertEqual(recorder.body, b'{"error": "x"}')


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AiHttpTimeoutAndCancelTests(unittest.TestCase):
    def test_slow_response_exceeds_deadline_maps_to_timeout(self) -> None:
        config = {"hits": [], "delay": 5.0, "status": 200, "body": b"{}"}
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/models", deadline_ms=200)
        self.assertIs(code_of(recorder), AiErrorCode.TIMEOUT)

    def test_user_cancellation_maps_to_cancelled(self) -> None:
        config = {"hits": [], "delay": 5.0, "status": 200, "body": b"{}"}
        with server_for(config) as port:
            cancellable = Gio.Cancellable()
            client = AiHttpClient()
            recorder = Recorder()

            def cancel_later() -> bool:
                cancellable.cancel()
                return False

            client.fetch(
                method="GET",
                url=f"http://127.0.0.1:{port}/models",
                headers={"Accept": "application/json"},
                authorization=BEARER,
                max_bytes=1024 * 1024,
                deadline_ms=10000,
                idle_timeout_s=5.0,
                cancellable=cancellable,
                on_headers=recorder.on_headers,
                on_data=recorder.on_data,
                on_error=recorder.on_error,
            )
            GLib.timeout_add(100, cancel_later)
            run_until(lambda: recorder.error is not None)
        self.assertIs(code_of(recorder), AiErrorCode.CANCELLED)

    def test_already_cancelled_cancellable_fails_immediately(self) -> None:
        config = {"hits": [], "status": 200, "body": b"{}"}
        with server_for(config) as port:
            cancellable = Gio.Cancellable()
            cancellable.cancel()
            client = AiHttpClient()
            recorder = Recorder()
            client.fetch(
                method="GET",
                url=f"http://127.0.0.1:{port}/models",
                headers={"Accept": "application/json"},
                authorization=BEARER,
                max_bytes=1024 * 1024,
                deadline_ms=5000,
                idle_timeout_s=5.0,
                cancellable=cancellable,
                on_headers=recorder.on_headers,
                on_data=recorder.on_data,
                on_error=recorder.on_error,
            )
            run_until(lambda: recorder.error is not None)
        self.assertIs(code_of(recorder), AiErrorCode.CANCELLED)
        self.assertEqual(config["hits"], [], "no request may be sent")


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AiHttpBodyLimitTests(unittest.TestCase):
    def test_body_exactly_at_limit_is_accepted(self) -> None:
        body = b"x" * 100
        config = {"hits": [], "status": 200, "body": body}
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/models", max_bytes=100)
        self.assertIsNone(recorder.error)
        self.assertEqual(recorder.body, body)

    def test_body_one_byte_over_limit_is_rejected(self) -> None:
        config = {"hits": [], "status": 200, "body": b"x" * 101}
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/models", max_bytes=100)
        self.assertIs(code_of(recorder), AiErrorCode.RESPONSE_TOO_LARGE)

    def test_chunked_body_over_limit_proves_cap_is_by_actual_bytes(self) -> None:
        # No Content-Length at all: only the bytes actually read can trip the
        # cap (spec §4.2.7: never trust Content-Length).
        config = {"hits": [], "status": 200, "chunked_body": b"y" * 200}
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/models", max_bytes=100)
        self.assertIs(code_of(recorder), AiErrorCode.RESPONSE_TOO_LARGE)

    def test_content_length_lie_is_not_enough_to_avoid_the_cap(self) -> None:
        # The stub declares a Content-Length but streams more bytes than the
        # cap; the transport must still stop by actual accumulated bytes.
        body = b"z" * 200
        config = {
            "hits": [],
            "status": 200,
            "body": body,
            "extra_headers": {"Content-Length": "200"},
        }
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/models", max_bytes=100)
        self.assertIs(code_of(recorder), AiErrorCode.RESPONSE_TOO_LARGE)


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AiHttpRedirectTests(unittest.TestCase):
    def test_get_follows_same_origin_301_302_303_307_308(self) -> None:
        for status in (301, 302, 303, 307, 308):
            config = {
                "hits": [],
                "status": 200,
                "body": b'{"data": []}',
                "redirect_path": "/models",
                "redirect_status": status,
                "location": "/final",
            }
            with server_for(config) as port:
                _, recorder = fetch(f"http://127.0.0.1:{port}/models")
            with self.subTest(status=status):
                self.assertIsNone(recorder.error)
                self.assertEqual(recorder.statuses, [200])
                self.assertEqual(
                    [hit["path"] for hit in config["hits"]], ["/models", "/final"]
                )
                # Authorization is re-attached on the redirect hop.
                self.assertEqual(config["hits"][1]["authorization"], BEARER)

    def test_relative_location_is_resolved_and_followed(self) -> None:
        config = {
            "hits": [],
            "status": 200,
            "body": b'{"data": []}',
            "redirect_path": "/v1/models",
            "redirect_status": 302,
            "location": "final-list",
        }
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/v1/models")
        self.assertIsNone(recorder.error)
        self.assertEqual(
            [hit["path"] for hit in config["hits"]], ["/v1/models", "/v1/final-list"]
        )

    def test_missing_location_is_rejected(self) -> None:
        config = {
            "hits": [],
            "status": 200,
            "body": b"{}",
            "redirect_path": "/models",
            "redirect_status": 302,
            "location": None,
        }
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/models")
        self.assertIs(code_of(recorder), AiErrorCode.REDIRECT_REJECTED)
        self.assertEqual(len(config["hits"]), 1, "no follow hop may be sent")

    def test_non_followable_3xx_statuses_are_rejected(self) -> None:
        for status in (300, 304, 305, 306, 399):
            config = {
                "hits": [],
                "status": 200,
                "body": b"{}",
                "redirect_path": "/models",
                "redirect_status": status,
                "location": "/final",
            }
            with server_for(config) as port:
                _, recorder = fetch(f"http://127.0.0.1:{port}/models")
            with self.subTest(status=status):
                self.assertIs(code_of(recorder), AiErrorCode.REDIRECT_REJECTED)
                self.assertEqual(
                    len(config["hits"]), 1, "no follow hop may be sent"
                )

    def test_post_follows_only_307_and_308(self) -> None:
        payload = b'{"model": "m"}'
        for status in (307, 308):
            config = {
                "hits": [],
                "status": 200,
                "body": b"{}",
                "redirect_path": "/chat/completions",
                "redirect_status": status,
                "location": "/final",
            }
            with server_for(config) as port:
                _, recorder = fetch(
                    f"http://127.0.0.1:{port}/chat/completions",
                    method="POST",
                    body=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
            with self.subTest(status=status):
                self.assertIsNone(recorder.error)
                self.assertEqual(recorder.statuses, [200])
                self.assertEqual(
                    [hit["method"] for hit in config["hits"]], ["POST", "POST"]
                )
                # method and body preserved on the follow hop.
                self.assertEqual(config["hits"][1]["body"], payload)
                self.assertEqual(config["hits"][1]["authorization"], BEARER)

    def test_post_rejects_301_302_303_without_sending_next_hop(self) -> None:
        for status in (301, 302, 303):
            config = {
                "hits": [],
                "status": 200,
                "body": b"{}",
                "redirect_path": "/chat/completions",
                "redirect_status": status,
                "location": "/final",
            }
            with server_for(config) as port:
                _, recorder = fetch(
                    f"http://127.0.0.1:{port}/chat/completions",
                    method="POST",
                    body=b'{"model": "m"}',
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
            with self.subTest(status=status):
                self.assertIs(code_of(recorder), AiErrorCode.REDIRECT_REJECTED)
                self.assertEqual(
                    [hit["path"] for hit in config["hits"]], ["/chat/completions"]
                )

    def test_fourth_redirect_hop_is_rejected(self) -> None:
        chain = {"/r0": "/r1", "/r1": "/r2", "/r2": "/r3", "/r3": "/r4"}
        config = {"hits": [], "chain": chain, "status": 200, "body": b'{"data": []}'}
        with server_for(config) as port:
            _, recorder = fetch(f"http://127.0.0.1:{port}/r0")
        self.assertIs(code_of(recorder), AiErrorCode.REDIRECT_REJECTED)
        self.assertEqual(
            [hit["path"] for hit in config["hits"]], ["/r0", "/r1", "/r2", "/r3"]
        )
        # /r4 was never contacted: the 4th redirect (hops already 3) is refused.

    def test_cross_origin_redirect_is_rejected_and_second_listener_never_gets_auth(
        self,
    ) -> None:
        config_a = {
            "hits": [],
            "status": 200,
            "body": b'{"data": []}',
            "redirect_path": "/models",
            "redirect_status": 302,
        }
        config_b = {"hits": [], "status": 200, "body": b'{"data": []}'}
        with server_for(config_a) as port_a, server_for(config_b) as port_b:
            config_a["location"] = f"http://127.0.0.1:{port_b}/models"
            _, recorder = fetch(f"http://127.0.0.1:{port_a}/models")
        self.assertIs(code_of(recorder), AiErrorCode.REDIRECT_REJECTED)
        self.assertEqual(
            config_b["hits"],
            [],
            "the cross-origin listener must never be contacted at all",
        )


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AiHttpProxyBypassTests(unittest.TestCase):
    def test_loopback_http_bypasses_environment_proxies(self) -> None:
        proxy_config = {"hits": [], "status": 200, "body": b"{}"}
        app_config = {"hits": [], "status": 200, "body": b'{"data": []}'}
        with server_for(proxy_config) as proxy_port, server_for(app_config) as app_port:
            old = {
                name: os.environ.get(name)
                for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
            }
            try:
                os.environ["http_proxy"] = f"http://127.0.0.1:{proxy_port}"
                os.environ["https_proxy"] = f"http://127.0.0.1:{proxy_port}"
                os.environ.pop("no_proxy", None)
                os.environ.pop("NO_PROXY", None)
                _, recorder = fetch(f"http://127.0.0.1:{app_port}/models")
            finally:
                for name, value in old.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        self.assertIsNone(recorder.error)
        self.assertEqual(len(app_config["hits"]), 1)
        self.assertEqual(
            proxy_config["hits"],
            [],
            "the proxy listener must never be contacted by a loopback request",
        )


@unittest.skipUnless(
    _RUNTIME_OK and shutil.which("openssl"),
    "UNAVAILABLE: Soup/Gio GI typelib or openssl missing",
)
class AiHttpTlsTests(unittest.TestCase):
    """Local TLS failure mapping via a self-signed loopback server.

    No public network is involved: the certificate is generated on the fly
    and the client's default validation must reject it with TLS_FAILED (the
    app never offers a "disable TLS verification" switch, spec §4.2.4).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cert = os.path.join(cls._temp.name, "cert.pem")
        key = os.path.join(cls._temp.name, "key.pem")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                key,
                "-out",
                cert,
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        cls._cert, cls._key = cert, key

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_self_signed_certificate_maps_to_tls_failed(self) -> None:
        config = {"hits": [], "status": 200, "body": b"{}"}
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self._cert, self._key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            _, recorder = fetch(f"https://127.0.0.1:{port}/models", deadline_ms=5000)
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertIs(code_of(recorder), AiErrorCode.TLS_FAILED)


@unittest.skipUnless(_RUNTIME_OK, "UNAVAILABLE: Soup/Gio GI typelib missing")
class AiHttpInvalidUrlTests(unittest.TestCase):
    def test_invalid_url_is_reported_without_a_network_round_trip(self) -> None:
        client = AiHttpClient()
        recorder = Recorder()
        client.fetch(
            method="GET",
            url="http://example.com/models",  # remote HTTP is policy-rejected
            headers={"Accept": "application/json"},
            authorization=BEARER,
            max_bytes=1024 * 1024,
            deadline_ms=1000,
            idle_timeout_s=5.0,
            cancellable=None,
            on_headers=recorder.on_headers,
            on_data=recorder.on_data,
            on_error=recorder.on_error,
        )
        run_until(lambda: recorder.error is not None)
        self.assertIs(code_of(recorder), AiErrorCode.INSECURE_REMOTE_URL)


if __name__ == "__main__":
    unittest.main()
