"""libsoup 3 asynchronous HTTP transport for the direct LLM migration.

Contract defined by docs/LLM_PROVIDER_MIGRATION_SPEC.md §4.2/§6.3/§6.4/§7.4/
§13.4 and pinned by tests/test_ai_http.py. Shared by the model-list GET
(client) and the chat-completions POST.

Security invariants (each is exercised by the integration tests):

- Every Soup message carries NO_REDIRECT, so libsoup can never automatically
  carry the Authorization header across a redirect.
- Authorization is attached only to the message about to be sent on the
  current hop, and is re-attached when a fresh message is built for each
  manual redirect hop.
- Redirects are resolved manually: Location is bounded to 2048 characters,
  joined against the current URI, re-validated with the full URL policy
  (normalize_endpoint_url) and accepted only when resolve_redirect() allows
  a same-origin, non-downgrading hop that preserves method semantics. At most
  3 hops are followed; cross-origin, HTTPS->HTTP, missing/oversized Location
  and method-changing POST redirects are rejected before any request is sent.
- Loopback HTTP bypasses environment/system proxies. A Python
  Gio.ProxyResolver subclass was the first choice, but PyGObject 3.56 on
  Python 3.14 mangles the GAsyncReadyCallback user_data in both directions
  (the vfunc receives user_data=None and Gio.Task.new replaces it with a
  closure pointer), which crashes GIO's GProxyAddressEnumerator inside the
  main loop. Instead the client keeps two sessions: loopback requests use a
  session whose proxy resolver is Gio.SimpleProxyResolver(None) (direct for
  every host), while remote HTTPS requests use a session with the GIO default
  resolver so desktop proxy behavior is preserved.
- Body limits are enforced on the bytes actually read, never on
  Content-Length: the response is streamed incrementally and the underlying
  request is cancelled as soon as the cumulative count exceeds max_bytes.
- A per-request internal Gio.Cancellable backs both the total deadline
  (deadline_ms) and the caller's cancellable; a flag records which source
  fired first so TIMEOUT and CANCELLED stay distinct.
- GLib.Error failures are mapped to stable AiErrorCode values (TLS domain ->
  TLS_FAILED, TIMEOUT -> TIMEOUT, CANCELLED -> CANCELLED, everything else ->
  NETWORK_FAILED). Raw exceptions never escape, and error details are bounded
  and never contain the key.

Callback contract: every callback is dispatched on the GLib default main
context via idle_add, never synchronously from the caller's thread. End of
body is signalled by a final on_data(b"") chunk; success is reaching that
marker without an on_error. on_headers fires once with the final response
status, after the redirect loop.
"""

from __future__ import annotations

import ipaddress
from typing import Callable, Mapping
from urllib.parse import urljoin, urlsplit

from mdreader.models.ai import AiError, AiErrorCode
from mdreader.services.ai_endpoints import (
    normalize_endpoint_url,
    resolve_redirect,
)

try:
    import gi

    gi.require_version("Soup", "3.0")
    gi.require_version("Gio", "2.0")
    from gi.repository import GLib, Gio, Soup  # noqa: F401

    _RUNTIME = (Soup, Gio, GLib)
except Exception:
    _RUNTIME = None

_MAX_LOCATION_LENGTH = 2048
_READ_CHUNK_SIZE = 65536
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def soup_runtime_available() -> bool:
    """Probe the Soup/Gio GI typelibs without raising (spec §11.1)."""
    return _RUNTIME is not None


def map_http_status(status: int) -> AiErrorCode:
    """Map a non-2xx HTTP status to a stable :class:`AiErrorCode`
    (spec §7.4/§8.1). Callers pass this the final status from ``on_headers``
    only for non-2xx responses; anything not matched is PROVIDER_UNAVAILABLE.
    """
    if status == 401:
        return AiErrorCode.AUTHENTICATION_FAILED
    if status == 403:
        return AiErrorCode.PERMISSION_DENIED
    if status in (404, 405):
        return AiErrorCode.ENDPOINT_NOT_FOUND
    if status == 402:
        return AiErrorCode.BILLING_OR_QUOTA_REQUIRED
    if status == 429:
        return AiErrorCode.RATE_LIMITED
    if status in (400, 422):
        return AiErrorCode.REQUEST_REJECTED
    return AiErrorCode.PROVIDER_UNAVAILABLE


def _is_loopback_url(url: str) -> bool:
    """True when the URL's host is exactly ``localhost`` or a canonical
    loopback IP literal (spec §4.2.2). Only called on already-validated
    URLs, so the host is well-formed."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return bool(ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def _safe_message(exc: Exception) -> str:
    """Short, bounded, key-free description of a GLib/Soup failure."""
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        message = message.strip()
        if message:
            return message[:200]
    return type(exc).__name__


def _classify_error(req: "_Request", exc: Exception) -> AiErrorCode:
    """Map a GLib.Error to a stable code, honouring who cancelled first.

    A deadline expiry or user cancellation surfaces from libsoup as a plain
    G_IO_ERROR_CANCELLED, so the flag recorded by the source that fired first
    is what distinguishes TIMEOUT from CANCELLED (spec §4.2).
    """
    if req.cancel_source == "user":
        return AiErrorCode.CANCELLED
    if req.cancel_source == "deadline":
        return AiErrorCode.TIMEOUT
    domain = getattr(exc, "domain", "")
    code = getattr(exc, "code", -1)
    if domain == "g-tls-error-quark":
        return AiErrorCode.TLS_FAILED
    if domain == "g-io-error-quark":
        io = _RUNTIME[1].IOErrorEnum
        if code == io.CANCELLED.value:
            return AiErrorCode.CANCELLED
        if code == io.TIMED_OUT.value:
            return AiErrorCode.TIMEOUT
    # soup-session-error-quark (TOO_MANY_REDIRECTS, REDIRECT_* etc.) and any
    # other domain are unexpected protocol failures here, since NO_REDIRECT
    # and manual hops prevent libsoup's own redirect machinery from running.
    return AiErrorCode.NETWORK_FAILED


class _Request:
    """Mutable per-fetch state passed as user_data to every async callback."""

    __slots__ = (
        "method",
        "headers",
        "authorization",
        "body",
        "max_bytes",
        "idle_timeout_s",
        "connect_deadline_ms",
        "on_headers",
        "on_data",
        "on_error",
        "user_cancellable",
        "user_signal_id",
        "internal_cancellable",
        "deadline_source",
        "connect_source",
        "deadline_fired",
        "cancel_source",
        "finished",
        "original_ep",
        "current_ep",
        "hops",
        "total_bytes",
        "input_stream",
        "session",
        "message",
    )


class AiHttpClient:
    """Asynchronous libsoup 3 transport for GET model lists and POST chat.

    ``fetch`` never blocks the calling thread: it arms a per-request
    ``Gio.Cancellable`` and a deadline timer, then returns immediately. All
    callbacks run on the GLib default main context. Errors are always
    delivered through ``on_error`` as :class:`AiError` with a stable code and
    a bounded, key-free detail.
    """

    def __init__(self, *, user_agent: str = "MDReader/1.0") -> None:
        self._user_agent = user_agent
        self._runtime = _RUNTIME

    # -- public API ---------------------------------------------------------

    def fetch(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        authorization: str | None,  # "Bearer <key>" or None (none mode)
        body: bytes | None = None,  # POST body; caller passes Content-Type in headers
        max_bytes: int,
        deadline_ms: int,
        idle_timeout_s: float,
        connect_deadline_ms: int | None = None,  # §4.2: e.g. 15 s for chat
        cancellable: "Gio.Cancellable | None",
        on_headers: Callable[[int], None],  # final response status
        on_data: Callable[[bytes], None],  # incremental body bytes; b"" marks EOF
        on_error: Callable[[AiError], None],
    ) -> None:
        if self._runtime is None:
            self._emit(
                on_error,
                AiError(AiErrorCode.AI_RUNTIME_UNAVAILABLE, "Soup runtime unavailable"),
            )
            return
        try:
            original_ep = normalize_endpoint_url(url)
        except AiError as exc:
            self._emit(on_error, exc)
            return
        if cancellable is not None and cancellable.is_cancelled():
            self._emit(
                on_error, AiError(AiErrorCode.CANCELLED, "request cancelled")
            )
            return

        soup, gio, glib = self._runtime
        req = _Request()
        req.method = method
        req.headers = dict(headers)
        req.authorization = authorization
        req.body = body
        req.max_bytes = max_bytes
        req.idle_timeout_s = idle_timeout_s
        req.connect_deadline_ms = connect_deadline_ms
        req.on_headers = on_headers
        req.on_data = on_data
        req.on_error = on_error
        req.user_cancellable = cancellable
        req.user_signal_id = None
        req.internal_cancellable = gio.Cancellable()
        req.deadline_source = None
        req.connect_source = None
        req.deadline_fired = False
        req.cancel_source = None
        req.finished = False
        req.original_ep = original_ep
        req.current_ep = original_ep
        req.hops = 0
        req.total_bytes = 0
        req.input_stream = None
        req.session = None
        req.message = None

        req.deadline_source = glib.timeout_add(deadline_ms, self._on_deadline, req)
        if connect_deadline_ms is not None and connect_deadline_ms > 0:
            req.connect_source = glib.timeout_add(
                connect_deadline_ms, self._on_connect_deadline, req
            )
        if cancellable is not None:
            self._connect_user_cancellation(req)
        self._send_hop(req)

    # -- internal helpers ---------------------------------------------------

    def _emit(self, callback: Callable, *args) -> None:
        if self._runtime is not None:
            GLib.idle_add(callback, *args)
        else:
            # Only reachable when GI itself is unavailable; the caller's own
            # runtime probe normally prevents this path.
            callback(*args)

    def _on_deadline(self, req: _Request) -> bool:
        req.deadline_fired = True
        if req.cancel_source is None:
            req.cancel_source = "deadline"
            req.internal_cancellable.cancel()
        return False  # one-shot

    def _on_connect_deadline(self, req: _Request) -> bool:
        # Connect-phase timeout (§4.2): fires only while no final response
        # headers have arrived; disarmed in _send_cb once headers do.
        if req.cancel_source is None:
            req.cancel_source = "deadline"
            req.internal_cancellable.cancel()
        return False  # one-shot

    def _connect_user_cancellation(self, req: _Request) -> None:
        # Gio.Cancellable.connect(callback) calls the callback with no
        # arguments (g_cancellable_connect marshalling), so the request is
        # captured in the closure.
        cancellable = req.user_cancellable

        def on_cancelled() -> None:
            if req.cancel_source is None:
                req.cancel_source = "user"
                req.internal_cancellable.cancel()

        req.user_signal_id = cancellable.connect(on_cancelled)

    def _new_session(self, url: str, idle_timeout_s: float) -> "Soup.Session":
        soup, gio, _glib = self._runtime
        session = soup.Session()
        session.props.user_agent = self._user_agent
        if _is_loopback_url(url):
            # direct for every host; loopback must never be routed through an
            # environment/system proxy (spec §4.2.3).
            session.props.proxy_resolver = gio.SimpleProxyResolver.new(None, [])
        # Per-fetch session: the temporary idle timeout dies with the request,
        # so there is nothing to restore afterwards.
        session.props.timeout = max(1, int(idle_timeout_s))
        return session

    def _send_hop(self, req: _Request) -> None:
        soup, _gio, glib = self._runtime
        session = self._new_session(req.current_ep.url, req.idle_timeout_s)
        req.session = session
        message = soup.Message.new(req.method, req.current_ep.url)
        message.set_flags(soup.MessageFlags.NO_REDIRECT)
        request_headers = message.get_request_headers()
        for name, value in req.headers.items():
            request_headers.replace(name, value)
        if req.authorization is not None:
            request_headers.replace("Authorization", req.authorization)
        if req.body is not None:
            content_type = req.headers.get("Content-Type") or _DEFAULT_CONTENT_TYPE
            message.set_request_body_from_bytes(
                content_type, glib.Bytes.new(req.body)
            )
        req.message = message
        session.send_async(
            message,
            glib.PRIORITY_DEFAULT,
            req.internal_cancellable,
            self._send_cb,
            req,
        )

    def _send_cb(self, session, result, req: _Request) -> None:
        try:
            input_stream = session.send_finish(result)
        except GLib.Error as exc:
            self._fail(req, AiError(_classify_error(req, exc), _safe_message(exc)))
            return
        except Exception as exc:  # non-GLib marshaling failure
            self._fail(req, AiError(AiErrorCode.NETWORK_FAILED, _safe_message(exc)))
            return
        status = req.message.props.status_code
        if 300 <= status < 400:
            # Only 301/302/303/307/308 may be followed; every other 3xx is
            # rejected by resolve_redirect (spec §6.4 step 2).
            self._handle_redirect(req, status)
            return
        req.input_stream = input_stream
        self._emit(req.on_headers, status)
        # The final response headers have arrived: the connect-phase timer is
        # no longer needed.
        if req.connect_source is not None:
            GLib.source_remove(req.connect_source)
            req.connect_source = None
        self._read_more(req)

    def _handle_redirect(self, req: _Request, status: int) -> None:
        location = req.message.get_response_headers().get_one("Location")
        if location is None or len(location) > _MAX_LOCATION_LENGTH:
            self._fail(
                req,
                AiError(
                    AiErrorCode.REDIRECT_REJECTED,
                    "missing or oversized Location header",
                ),
            )
            return
        try:
            resolved_ep = normalize_endpoint_url(
                urljoin(req.current_ep.url, location)
            )
        except AiError:
            self._fail(
                req,
                AiError(
                    AiErrorCode.REDIRECT_REJECTED, "Location fails URL policy"
                ),
            )
            return
        decision = resolve_redirect(
            status,
            location,
            original=req.original_ep,
            current=req.current_ep,
            method=req.method,
            hops=req.hops,
        )
        if not decision.follow:
            self._fail(
                req,
                AiError(AiErrorCode.REDIRECT_REJECTED, decision.error_detail),
            )
            return
        req.hops += 1
        req.current_ep = resolved_ep
        self._send_hop(req)

    def _read_more(self, req: _Request) -> None:
        req.input_stream.read_bytes_async(
            _READ_CHUNK_SIZE,
            GLib.PRIORITY_DEFAULT,
            req.internal_cancellable,
            self._read_cb,
            req,
        )

    def _read_cb(self, stream, result, req: _Request) -> None:
        try:
            chunk = stream.read_bytes_finish(result)
        except GLib.Error as exc:
            self._fail(req, AiError(_classify_error(req, exc), _safe_message(exc)))
            return
        data = bytes(chunk.get_data())
        if not data:
            # End of body: the final empty chunk is the completion marker.
            self._emit(req.on_data, b"")
            self._finish(req)
            return
        req.total_bytes += len(data)
        if req.total_bytes > req.max_bytes:
            # Enforced on the bytes actually read, never on Content-Length
            # (spec §4.2.7). Cancel the underlying request, then fail.
            req.internal_cancellable.cancel()
            self._fail(
                req,
                AiError(
                    AiErrorCode.RESPONSE_TOO_LARGE,
                    f"response exceeds {req.max_bytes} bytes",
                ),
            )
            return
        self._emit(req.on_data, data)
        self._read_more(req)

    def _fail(self, req: _Request, error: AiError) -> None:
        if req.finished:
            return
        req.finished = True
        self._cleanup(req)
        self._emit(req.on_error, error)

    def _finish(self, req: _Request) -> None:
        if req.finished:
            return
        req.finished = True
        self._cleanup(req)

    def _cleanup(self, req: _Request) -> None:
        if req.deadline_source is not None and not req.deadline_fired:
            # Only remove a still-pending timer; an expired one has already
            # destroyed itself and source_remove would warn.
            GLib.source_remove(req.deadline_source)
        req.deadline_source = None
        if req.connect_source is not None:
            GLib.source_remove(req.connect_source)
            req.connect_source = None
        if req.user_signal_id is not None and req.user_cancellable is not None:
            req.user_cancellable.disconnect(req.user_signal_id)
            req.user_signal_id = None
        # The per-fetch session is discarded; nothing else to restore.
