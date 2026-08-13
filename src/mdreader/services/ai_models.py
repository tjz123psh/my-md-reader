"""OpenAI-compatible ``GET /models`` response parser.

Contract defined by docs/LLM_PROVIDER_MIGRATION_SPEC.md §7.2/§13.3. Pure
logic only: no network, no GTK. The caller performs the HTTP fetch and hands
the raw response body here.

Rules:

- Root must be a JSON object whose ``data`` is an array.
- Invalid entries are skipped, but an all-invalid catalog is INVALID_RESPONSE,
  never an empty success; ``data: []`` is a successful empty catalog.
- Model IDs are trimmed, 1-256 chars, and must not contain whitespace, newline,
  NUL or Unicode Cc/Cf characters (invisible/bidi spoofing hazard).
- Duplicate IDs are dropped (first occurrence wins); the catalog is sorted by
  Unicode casefold but the original IDs are kept unchanged.
- Bodies larger than ``max_body_bytes`` are RESPONSE_TOO_LARGE before any
  decoding; more than ``max_models`` entries is INVALID_RESPONSE, never
  silently truncated.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from mdreader.models.ai import AiError, AiErrorCode, AiModel
from mdreader.services.ai_endpoints import normalize_api_base_url

MODELS_BODY_LIMIT = 2 * 1024 * 1024  # spec §4.2: models response cap 2 MiB
MODELS_TIMEOUT_MS = 20_000  # spec §4.2: models total timeout 20 s
_ERROR_BODY_KEEP = 512  # spec §7.4: server error body kept for diagnostics


@dataclass(frozen=True, slots=True)
class ModelCatalogResult:
    """Parsed model catalog, display-sorted by casefold; original IDs kept."""

    models: tuple[AiModel, ...]


def valid_model_id(value: object) -> str | None:
    """Return the canonical id if ``value`` is an acceptable model ID, else None.

    Manual IDs typed by the user are validated with the same rules as remote
    model IDs (spec §7.3): no whitespace, newline, NUL or Unicode Cc/Cf
    characters; 1–256 characters after trimming.
    """
    if not isinstance(value, str):
        return None
    # Any whitespace/control/invisible character rejects the id outright; a
    # stripped-but-padded id would still be a visibility and bidi hazard.
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        return None
    trimmed = value.strip()
    if not trimmed or len(trimmed) > 256:
        return None
    return trimmed


def parse_models_response(
    body: bytes, *, max_models: int = 2000, max_body_bytes: int = 2 * 1024 * 1024
) -> ModelCatalogResult:
    """Parse an OpenAI-compatible ``GET /models`` response body.

    Raises :class:`AiError` with ``RESPONSE_TOO_LARGE`` when ``body`` exceeds
    ``max_body_bytes``, or ``INVALID_RESPONSE`` for any structural, decoding,
    count or all-invalid-catalog failure. An empty ``data`` array is a valid
    empty catalog, never an error.
    """
    if len(body) > max_body_bytes:
        raise AiError(
            AiErrorCode.RESPONSE_TOO_LARGE,
            f"/models body is {len(body)} bytes, limit is {max_body_bytes}",
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AiError(AiErrorCode.INVALID_RESPONSE, "models body is not valid UTF-8") from exc
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        # RecursionError covers pathological nesting; all are malformed JSON.
        raise AiError(AiErrorCode.INVALID_RESPONSE, "models response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AiError(AiErrorCode.INVALID_RESPONSE, "models response root must be a JSON object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise AiError(AiErrorCode.INVALID_RESPONSE, 'models response "data" must be an array')
    if len(data) > max_models:
        raise AiError(
            AiErrorCode.INVALID_RESPONSE,
            f"models response lists {len(data)} entries, limit is {max_models}",
        )

    models: list[AiModel] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = valid_model_id(entry.get("id"))
        if model_id is None or model_id in seen:
            continue
        seen.add(model_id)
        owned_by = entry.get("owned_by", "")
        if not isinstance(owned_by, str):
            owned_by = ""
        models.append(AiModel(model_id=model_id, owned_by=owned_by))

    if not models:
        # Distinguish "empty success" from "everything was invalid": the
        # latter is a malformed catalog, never an empty success.
        if data:
            raise AiError(AiErrorCode.INVALID_RESPONSE, "all model entries are invalid")
        return ModelCatalogResult(models=())

    models.sort(key=lambda model: model.model_id.casefold())
    return ModelCatalogResult(models=tuple(models))


def redact_error_body(body: bytes, *, secret: str = "") -> str:
    """Redact a server error body for diagnostics (spec §7.4).

    Strips HTML tags, removes control characters, scrubs the caller's secret
    if echoed back by the server, and keeps at most 512 characters. The result
    is safe for logs and UI; never returns the raw body.
    """
    text = body.decode("utf-8", errors="replace")
    if secret:
        text = text.replace(secret, "***")
    text = re.sub(r"<[^>]*>", "", text)
    text = "".join(ch for ch in text if ord(ch) >= 32 and ch != "\x7f")
    return text[:_ERROR_BODY_KEEP]


def draft_fingerprint(
    *,
    api_base_url: str,
    models_url: str,
    auth_mode: str,
    key_source: str,
    key_revision: int,
) -> str:
    """Stable draft fingerprint for staleness checks (spec §7.1).

    The fingerprint covers the normalized base URL, the explicit models URL,
    the auth mode, the key *source* (a small enum such as ``new-key`` /
    ``saved-same-origin`` / ``none``) and a monotonic key *revision* the dialog
    advances on every password change. The API key itself is never hashed,
    serialized or recorded here — no secret field may enter the payload.
    """
    try:
        normalized = normalize_api_base_url(api_base_url).url
    except AiError:
        normalized = api_base_url.strip()
    payload = "|".join(
        (normalized, models_url.strip(), auth_mode, key_source, str(key_revision))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ModelFetchClient(Protocol):
    """Minimal transport surface :func:`fetch_models_catalog` relies on.

    Implemented by ``ai_http.AiHttpClient``; tests inject a fake. End of body
    is signalled by a final ``on_data(b"")`` chunk; ``on_headers`` fires once
    with the final response status after any manual redirect hops.
    """

    def fetch(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        authorization: str | None,
        body: bytes | None,
        max_bytes: int,
        deadline_ms: int,
        idle_timeout_s: float,
        cancellable: object | None,
        on_headers: Callable[[int], None],
        on_data: Callable[[bytes], None],
        on_error: Callable[[AiError], None],
    ) -> None: ...


def fetch_models_catalog(
    client: ModelFetchClient,
    *,
    endpoint_url: str,
    authorization: str | None,
    secret: str = "",
    cancellable: object | None = None,
    on_result: Callable[[ModelCatalogResult], None],
    on_error: Callable[[AiError], None],
) -> None:
    """Fetch and parse ``GET {base}/models`` with the injected transport.

    ``endpoint_url`` must already pass the endpoint policy (the caller builds
    it with ``build_models_endpoint``). A 2xx response is parsed into a
    :class:`ModelCatalogResult` — ``data: []`` is a successful empty catalog,
    never an error (spec §7.2). Non-2xx statuses are classified per §7.4 and
    the redacted error body is attached as the detail; network/TLS/timeout/
    cancellation errors arrive from the transport already typed.
    """
    body_buffer = bytearray()
    status_code = 0

    def on_data(chunk: bytes) -> None:
        nonlocal status_code
        if not chunk:  # EOF marker from the transport
            _on_done()
            return
        body_buffer.extend(chunk)

    def _on_done() -> None:
        if status_code >= 200 and status_code < 300:
            try:
                on_result(parse_models_response(bytes(body_buffer)))
            except AiError as exc:
                on_error(exc)
            return
        detail = redact_error_body(bytes(body_buffer), secret=secret)
        code = _classify_models_status(status_code)
        on_error(AiError(code, detail or f"HTTP {status_code}"))

    def on_headers(status: int) -> None:
        nonlocal status_code
        status_code = status

    client.fetch(
        method="GET",
        url=endpoint_url,
        headers={"Accept": "application/json"},
        authorization=authorization,
        body=None,
        max_bytes=MODELS_BODY_LIMIT,
        deadline_ms=MODELS_TIMEOUT_MS,
        idle_timeout_s=MODELS_TIMEOUT_MS / 1000,
        cancellable=cancellable,
        on_headers=on_headers,
        on_data=on_data,
        on_error=on_error,
    )


def _classify_models_status(status: int) -> AiErrorCode:
    """Map a non-2xx models-list status to a stable code (spec §7.4)."""
    if status in (401, 403):
        return AiErrorCode.AUTHENTICATION_FAILED
    if status in (404, 405):
        return AiErrorCode.ENDPOINT_NOT_FOUND
    if status == 402:
        return AiErrorCode.BILLING_OR_QUOTA_REQUIRED
    if status == 429:
        return AiErrorCode.RATE_LIMITED
    if 500 <= status <= 599:
        return AiErrorCode.PROVIDER_UNAVAILABLE
    return AiErrorCode.REQUEST_REJECTED
