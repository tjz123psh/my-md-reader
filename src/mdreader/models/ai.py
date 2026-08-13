"""AI domain models for the direct LLM migration.

Contract defined by docs/LLM_PROVIDER_MIGRATION_SPEC.md §5.3/§5.4. These are
pure value objects with no GTK, network or Secret Service dependency.

Credential discipline (spec §4.1/§5.3):

- ``AiProfile`` never contains an API key.
- ``AiConnectionDraft.api_key`` uses ``repr=False, compare=False`` so draft
  equality never compares keys and no repr/str can leak one.
- ``AiError`` detail strings must be redacted by callers before surfacing to
  logs or UI; the code is always safe.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Iterable

from .conversation import ChatMessage


class AiErrorCode(str, enum.Enum):
    """Stable error codes (spec §5.4). UI copy must never parse English messages."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    INVALID_URL = "INVALID_URL"
    INSECURE_REMOTE_URL = "INSECURE_REMOTE_URL"
    CROSS_ORIGIN_MODELS_URL = "CROSS_ORIGIN_MODELS_URL"
    REDIRECT_REJECTED = "REDIRECT_REJECTED"
    AI_RUNTIME_UNAVAILABLE = "AI_RUNTIME_UNAVAILABLE"
    SECRET_SERVICE_UNAVAILABLE = "SECRET_SERVICE_UNAVAILABLE"
    SECRET_NOT_FOUND = "SECRET_NOT_FOUND"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    ENDPOINT_NOT_FOUND = "ENDPOINT_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    TLS_FAILED = "TLS_FAILED"
    NETWORK_FAILED = "NETWORK_FAILED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    STREAM_ENDED_EARLY = "STREAM_ENDED_EARLY"
    CANCELLED = "CANCELLED"
    MODEL_NOT_SELECTED = "MODEL_NOT_SELECTED"
    BILLING_OR_QUOTA_REQUIRED = "BILLING_OR_QUOTA_REQUIRED"
    SETTINGS_WRITE_FAILED = "SETTINGS_WRITE_FAILED"
    CLEANUP_INCOMPLETE = "CLEANUP_INCOMPLETE"
    BUSY = "BUSY"


class AiPanelState(str, enum.Enum):
    """Typed AI panel availability injected by the window coordinator (spec
    §10.4). The panel never probes executables or environment itself."""

    UNCONFIGURED = "UNCONFIGURED"
    READY_NO_DOCUMENT = "READY_NO_DOCUMENT"
    READY = "READY"
    RUNNING = "RUNNING"
    AUTH_ERROR = "AUTH_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    SECRET_ERROR = "SECRET_ERROR"


class AiError(Exception):
    """Typed AI pipeline failure carrying a stable :class:`AiErrorCode`.

    ``detail`` is for internal debugging only; it must never contain an API key
    or full response body, and must be redacted before reaching logs or UI.
    """

    def __init__(self, code: AiErrorCode | str, detail: str = "") -> None:
        super().__init__(str(code))
        self.code = AiErrorCode(code)
        self.detail = detail

    def __str__(self) -> str:
        # Never include the detail in the generic string form; callers choose
        # explicitly when a redacted detail is safe to show.
        return f"{self.code.value}"


@dataclass(frozen=True, slots=True)
class AiModel:
    """A single model entry returned by ``GET /models`` or typed by hand."""

    model_id: str
    owned_by: str = ""


@dataclass(frozen=True, slots=True)
class AiProfile:
    """Persisted non-secret connection metadata (spec §9.1). Never holds a key."""

    profile_id: str
    provider_kind: str  # first version: "openai-compatible"
    api_base_url: str  # normalized, no trailing slash
    models_url: str  # "" means default {base}/models
    model_id: str
    auth_mode: str  # "bearer" or loopback-only "none"


@dataclass(frozen=True, slots=True)
class AiConnectionDraft:
    """Unsaved form draft used by the connection dialog.

    ``api_key`` is excluded from repr and equality on purpose: never log or
    compare drafts to detect key changes. The UI must track key changes with
    its own monotonic revision instead (spec §5.3).
    """

    api_base_url: str
    models_url: str = ""
    api_key: str = field(repr=False, compare=False, default="")
    auth_mode: str = "bearer"
    model_id: str = ""
    keep_existing_secret: bool = False  # derived intent only; services re-check origin


@dataclass(frozen=True, slots=True)
class AiRequest:
    """One chat request. Ask carries bounded success history; Edit carries none."""

    mode: str  # "ask" or "edit"
    model_id: str
    messages: tuple[ChatMessage, ...] = ()
    stream: bool = True

    @classmethod
    def from_messages(
        cls,
        mode: str,
        model_id: str,
        messages: Iterable[ChatMessage],
        *,
        stream: bool = True,
    ) -> "AiRequest":
        return cls(mode, model_id, tuple(messages), stream)
