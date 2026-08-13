"""Endpoint policy for the direct LLM migration.

Contract defined by docs/LLM_PROVIDER_MIGRATION_SPEC.md §6.1/§6.2 and pinned
by tests/test_ai_endpoints.py. Pure logic: no GTK, no network, no DNS.

Highlights:

- HTTPS everywhere; HTTP only for exact ``localhost`` or canonical loopback
  IPv4/IPv6 literals parsed with the stdlib ``ipaddress`` module. DNS results
  never participate in the loopback decision.
- Fuzzy IPv4 spellings (``2130706433``, ``127.1``), IPv6 zone IDs, backslashes,
  out-of-range ports, userinfo, query and fragment are rejected.
- A base URL that already ends in ``/models``, ``/chat/completions`` or
  ``/responses`` is rejected: users must enter the version root.
- Endpoints are built deterministically from parsed URI components, never with
  ``urljoin`` or string substitution on the raw user input.
- Origins compare ``(scheme, normalized host, effective port)`` where an
  explicit default port (``https:443``/``http:80``) equals the omitted port.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from mdreader.models.ai import AiError, AiErrorCode

_MAX_URL_LENGTH = 2048
_ENDPOINT_SUFFIXES = ("/models", "/chat/completions", "/responses")
_DEFAULT_PORTS = {"https": 443, "http": 80}


class EndpointError(AiError):
    """Endpoint policy failure carrying a stable :class:`AiErrorCode`."""


@dataclass(frozen=True, slots=True)
class NormalizedEndpoint:
    """A validated, normalized endpoint for display, persistence and origin
    comparison. ``path`` never has a trailing slash; ``port`` is the explicit
    port or None when the default is implied."""

    scheme: str
    host: str
    port: int | None
    path: str
    url: str

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else _DEFAULT_PORTS[self.scheme]


def _fail(code: AiErrorCode, detail: str) -> None:
    raise EndpointError(code, detail)


def _parse_authority(netloc: str) -> tuple[str, str | None] | None:
    """Split ``netloc`` into (host, explicit_port_text_or_None).

    Returns None when the authority is structurally malformed (unterminated
    IPv6 bracket, empty or non-numeric port, repeated ':' in a non-bracketed
    host).
    """
    if netloc.startswith("["):
        close = netloc.find("]")
        if close == -1:
            return None
        host = netloc[1:close]
        rest = netloc[close + 1 :]
        if rest == "":
            return host, None
        if not rest.startswith(":"):
            return None
        port_text = rest[1:]
    else:
        host, sep, rest = netloc.partition(":")
        if not sep:
            return host, None
        port_text = rest
    if port_text == "" or not port_text.isdigit():
        return None
    return host, port_text


def _is_loopback_host(host: str) -> bool:
    """Exact ``localhost`` (ASCII case-insensitive) or a loopback IP literal.
    Hostnames that merely resolve to loopback never qualify, and DNS is never
    consulted."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _render_url(scheme: str, host: str, port: int | None, path: str) -> str:
    host_part = f"[{host}]" if ":" in host else host
    authority = f"{host_part}:{port}" if port is not None else host_part
    return f"{scheme}://{authority}{path}"


def _parse(raw: str, *, reject_endpoint_suffixes: bool) -> NormalizedEndpoint:
    # Reject control characters (including newline, tab and NUL) anywhere in
    # the raw input before any trimming, then strip surrounding whitespace for
    # paste convenience (spec §6.1 steps 1 and 3).
    if any(ord(ch) < 32 for ch in raw):
        _fail(AiErrorCode.INVALID_URL, "control characters in URL")
    value = raw.strip()
    if not value:
        _fail(AiErrorCode.INVALID_URL, "empty URL")
    if len(value) > _MAX_URL_LENGTH:
        _fail(AiErrorCode.INVALID_URL, "URL longer than 2048 characters")
    if "\\" in value:
        _fail(AiErrorCode.INVALID_URL, "backslash in URL")
    try:
        parts = urlsplit(value)
    except ValueError:
        _fail(AiErrorCode.INVALID_URL, "unparseable URL")

    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        _fail(AiErrorCode.INVALID_URL, "scheme must be https or loopback http")
    if parts.query or parts.fragment:
        _fail(AiErrorCode.INVALID_URL, "query or fragment not allowed")
    netloc = parts.netloc
    if "@" in netloc:
        _fail(AiErrorCode.INVALID_URL, "userinfo not allowed")

    authority = _parse_authority(netloc)
    if authority is None:
        _fail(AiErrorCode.INVALID_URL, "malformed authority")
    host, port_text = authority
    if not host:
        _fail(AiErrorCode.INVALID_URL, "missing host")
    host = host.lower()
    if any(ch.isspace() or ord(ch) < 33 for ch in host):
        _fail(AiErrorCode.INVALID_URL, "invalid characters in host")
    # IPv6 zone identifiers are rejected outright (spec §6.1 step 4). Python's
    # ipaddress module accepts them since 3.14, so a literal check is required.
    if "%" in host:
        _fail(AiErrorCode.INVALID_URL, "IPv6 zone ID not allowed")

    # Validate the host before any scheme decision: a fuzzy IPv4 spelling such
    # as "2130706433" or "127.1" is a structural error (spec §6.1 step 4) even
    # though it may denote a loopback address. Any purely numeric-dotted host
    # must be a canonical dotted quad.
    if re.fullmatch(r"[0-9.]+", host):
        octets = host.split(".")
        canonical = len(octets) == 4 and all(
            part.isdigit()
            and (part == "0" or not part.startswith("0"))
            and int(part) <= 255
            for part in octets
        )
        if not canonical:
            _fail(AiErrorCode.INVALID_URL, "non-canonical IPv4 host")
    if host != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if ":" in host:
                _fail(AiErrorCode.INVALID_URL, "malformed IP literal host")

    if not _is_loopback_host(host) and scheme == "http":
        _fail(AiErrorCode.INSECURE_REMOTE_URL, "http allowed only for loopback")

    port: int | None = None
    if port_text is not None:
        port = int(port_text)
        if port < 1 or port > 65535:
            _fail(AiErrorCode.INVALID_URL, "port out of range")

    path = parts.path
    while path.endswith("/"):
        path = path[:-1]
    if reject_endpoint_suffixes and any(
        path.endswith(suffix) for suffix in _ENDPOINT_SUFFIXES
    ):
        _fail(AiErrorCode.INVALID_URL, "enter the version root URL, not a full endpoint")

    url = _render_url(scheme, host, port, path)
    return NormalizedEndpoint(scheme, host, port, path, url)


def normalize_api_base_url(raw: str) -> NormalizedEndpoint:
    """Normalize and validate a version-root API base URL (spec §6.1)."""
    return _parse(raw, reject_endpoint_suffixes=True)


def normalize_endpoint_url(raw: str) -> NormalizedEndpoint:
    """Normalize a full endpoint URL (e.g. ``{base}/models`` or a redirect
    Location) with the same policy but without the version-root suffix check."""
    return _parse(raw, reject_endpoint_suffixes=False)


def same_origin(a: NormalizedEndpoint, b: NormalizedEndpoint) -> bool:
    """Origin equality: scheme, normalized host and effective port (spec §6.1)."""
    return (a.scheme, a.host, a.effective_port) == (b.scheme, b.host, b.effective_port)


def is_loopback_endpoint(endpoint: NormalizedEndpoint) -> bool:
    return _is_loopback_host(endpoint.host)


def build_chat_endpoint(base: NormalizedEndpoint) -> str:
    """Deterministic ``POST {base}/chat/completions`` URL (spec §6.2)."""
    return f"{base.url}/chat/completions"


def build_models_endpoint(base: NormalizedEndpoint, explicit: str = "") -> str:
    """Deterministic ``GET {base}/models`` URL, or a same-origin exact override.

    The explicit models URL is an exact endpoint (its own final path is
    allowed) but still rejects userinfo, query, fragment, insecure protocols
    and any origin mismatch against ``base`` (spec §6.1/§7.1).
    """
    if not explicit.strip():
        return f"{base.url}/models"
    explicit_ep = _parse(explicit, reject_endpoint_suffixes=False)
    if not same_origin(base, explicit_ep):
        _fail(AiErrorCode.CROSS_ORIGIN_MODELS_URL, "models URL must share the API origin")
    return explicit_ep.url


@dataclass(frozen=True, slots=True)
class RedirectDecision:
    """One manual redirect hop decision (spec §6.4).

    ``follow`` is True only for a same-origin, non-downgrading hop that
    preserves method semantics and stays within ``max_hops``; ``url`` is then
    the resolved absolute target. Otherwise ``error_code`` carries the stable
    ``REDIRECT_REJECTED`` code and no request must be sent.
    """

    follow: bool
    url: str = ""
    error_code: str = ""
    error_detail: str = ""


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_METHOD_CHANGING_STATUSES = frozenset({301, 302, 303})
_MAX_REDIRECT_LOCATION_LENGTH = 2048


def resolve_redirect(
    status: int,
    location: str | None,
    *,
    original: NormalizedEndpoint,
    current: NormalizedEndpoint,
    method: str,
    hops: int,
    max_hops: int = 3,
) -> RedirectDecision:
    """Decide whether one manual redirect hop may proceed (spec §6.4).

    ``original`` is the very first request's origin; ``current`` is the URI
    that returned this redirect. Only 301/302/303/307/308 enter the branch;
    300/304/305/306 and unknown 3xx are rejected outright. GET may follow any
    of the five, but a non-GET (e.g. POST chat) may only follow 307/308 so the
    method and body are preserved. Every hop is re-validated with the full URL
    policy, must stay on the original origin, and must never downgrade
    HTTPS to HTTP. The HTTP transport still creates a fresh no-redirect
    message per hop and attaches Authorization only on the hop it sends.
    """
    if status not in _REDIRECT_STATUSES:
        return RedirectDecision(
            follow=False,
            error_code=AiErrorCode.REDIRECT_REJECTED.value,
            error_detail=f"status {status} is not a followable redirect",
        )
    if hops >= max_hops:
        return RedirectDecision(
            follow=False,
            error_code=AiErrorCode.REDIRECT_REJECTED.value,
            error_detail="redirect hop limit reached",
        )
    if not location or len(location) > _MAX_REDIRECT_LOCATION_LENGTH:
        return RedirectDecision(
            follow=False,
            error_code=AiErrorCode.REDIRECT_REJECTED.value,
            error_detail="missing or oversized Location header",
        )
    if method.upper() != "GET" and status in _METHOD_CHANGING_STATUSES:
        return RedirectDecision(
            follow=False,
            error_code=AiErrorCode.REDIRECT_REJECTED.value,
            error_detail=f"redirect {status} would change {method} method semantics",
        )
    try:
        resolved = _parse(urljoin(current.url, location), reject_endpoint_suffixes=False)
    except EndpointError:
        return RedirectDecision(
            follow=False,
            error_code=AiErrorCode.REDIRECT_REJECTED.value,
            error_detail="Location fails URL policy",
        )
    if not same_origin(original, resolved):
        return RedirectDecision(
            follow=False,
            error_code=AiErrorCode.REDIRECT_REJECTED.value,
            error_detail="cross-origin redirect rejected",
        )
    if original.scheme == "https" and resolved.scheme == "http":
        return RedirectDecision(
            follow=False,
            error_code=AiErrorCode.REDIRECT_REJECTED.value,
            error_detail="HTTPS to HTTP downgrade rejected",
        )
    return RedirectDecision(follow=True, url=resolved.url)
