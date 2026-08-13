"""Transactional AI connection profile store (spec §9.3/§9.4).

Coordinates the Secret Service and the non-secret GSettings ``ai-profile``
metadata with the recoverable two-phase order mandated by the spec:

1. validate every draft field (URL policy, auth mode, key, model ID);
2. a newly typed key is stored FIRST under a fresh profile UUID;
3. a blank key may reuse the saved secret only when the draft origin is
   exactly equal to the saved profile origin (scheme, host, effective port);
4. ``auth-mode=none`` is only allowed for an explicitly chosen loopback URL;
5. the whole non-secret metadata is written in one ``set_value`` and its
   boolean result checked;
6. only then is the new profile activated;
7. after activation, an old secret for a rotated UUID is best-effort removed;
8. a settings write failure rolls back the just-created secret and keeps the
   old profile; a failed rollback leaves a pending-cleanup UUID for retry;
9. a failed old-secret deletion is reported as a partial success, never as a
   clean completion.

Credential discipline (spec §4.1/§5.3): the store never sees, persists or
returns API keys — the key lives only inside the injected secret store and the
short-lived local variable in :meth:`save`. Metadata written to settings uses a
fixed six-key allowlist and can never contain a key. ``keep_existing_secret``
on the draft is only a derived intent; this service re-derives the origin
decision itself and never trusts the flag.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from mdreader.models.ai import (
    AiConnectionDraft,
    AiError,
    AiErrorCode,
    AiProfile,
)
from mdreader.services.ai_endpoints import (
    NormalizedEndpoint,
    build_models_endpoint,
    is_loopback_endpoint,
    normalize_api_base_url,
    same_origin,
)
from mdreader.services.ai_models import valid_model_id

_DEFAULT_PROVIDER_KIND = "openai-compatible"
_MAX_API_KEY_LENGTH = 8192


class AiSecretStore(Protocol):
    """Blocking secret store; must be invoked off the GTK main thread."""

    def store(self, profile_id: str, api_key: str) -> None: ...

    def lookup(self, profile_id: str) -> str: ...

    def clear(self, profile_id: str) -> None: ...


class ProfileSettings(Protocol):
    """Non-secret metadata facade (implemented by services.settings)."""

    def get_ai_profile(self) -> dict[str, str] | None: ...

    def set_ai_profile(self, values: dict[str, str]) -> bool: ...

    def clear_ai_profile(self) -> bool: ...


def validate_api_key(key: str) -> None:
    """Validate a freshly typed API key without trimming or rewriting it.

    Rejects leading/trailing whitespace, control characters, newlines and NUL
    (spec §6.1). The 1–8192 length check assumes a non-empty caller-supplied
    key; empty keys are handled by the auth-mode decision, not here.
    """
    if len(key) < 1 or len(key) > _MAX_API_KEY_LENGTH:
        raise AiError(AiErrorCode.REQUEST_REJECTED, "API key length out of range")
    if key != key.strip():
        raise AiError(
            AiErrorCode.REQUEST_REJECTED, "API key must not have surrounding whitespace"
        )
    if any(ord(ch) < 32 for ch in key):
        raise AiError(
            AiErrorCode.REQUEST_REJECTED, "API key contains control characters"
        )


def validate_auth_mode(auth_mode: str, endpoint: NormalizedEndpoint) -> None:
    """Remote URLs force ``bearer``; ``none`` is only for explicit loopback."""
    if auth_mode not in ("bearer", "none"):
        raise AiError(AiErrorCode.REQUEST_REJECTED, f"unknown auth mode: {auth_mode!r}")
    if auth_mode == "none" and not is_loopback_endpoint(endpoint):
        raise AiError(
            AiErrorCode.REQUEST_REJECTED,
            "auth-mode=none is only allowed for loopback URLs",
        )


class AiProfileStore:
    """Transactionally save, load and clear the active AI connection."""

    def __init__(
        self,
        secrets: AiSecretStore,
        settings: ProfileSettings,
        *,
        profile_id_factory=uuid4,
    ) -> None:
        self._secrets = secrets
        self._settings = settings
        self._profile_id_factory = profile_id_factory
        self._pending_cleanup: list[str] = []
        self.cleanup_warnings: list[str] = []

    @property
    def pending_cleanup(self) -> tuple[str, ...]:
        """Profile UUIDs whose rollback secret deletion could not finish."""
        return tuple(self._pending_cleanup)

    def load(self) -> AiProfile | None:
        """Read the current non-secret profile; never touches the keyring.

        Legacy ``opencode-model`` data is deliberately not migrated (spec §9.1).
        """
        metadata = self._settings.get_ai_profile()
        if not metadata or not metadata.get("profile-id") or not metadata.get(
            "api-base-url"
        ):
            return None
        return AiProfile(
            profile_id=metadata.get("profile-id", ""),
            provider_kind=metadata.get("provider-kind", _DEFAULT_PROVIDER_KIND),
            api_base_url=metadata.get("api-base-url", ""),
            models_url=metadata.get("models-url", ""),
            model_id=metadata.get("model-id", ""),
            auth_mode=metadata.get("auth-mode", "bearer"),
        )

    def save(self, draft: AiConnectionDraft) -> AiProfile:
        """Persist the connection transactionally and return the new profile.

        Raises :class:`AiError` with stable codes; on partial success (old
        secret cleanup failed) the profile is returned and the failure is
        recorded in :attr:`cleanup_warnings`.
        """
        self.cleanup_warnings = []
        endpoint, models_url = self._validate_draft(draft)
        current = self.load()
        is_loopback = is_loopback_endpoint(endpoint)
        created_secret = False

        # Resolve the key source and the profile UUID (spec §9.3 steps 2–4).
        if draft.auth_mode == "none":
            if draft.api_key:
                raise AiError(
                    AiErrorCode.REQUEST_REJECTED,
                    "auth-mode=none must not carry an API key",
                )
            profile_id = (
                current.profile_id
                if current is not None and current.auth_mode == "none"
                else str(self._profile_id_factory())
            )
        elif draft.api_key:
            validate_api_key(draft.api_key)
            profile_id = str(self._profile_id_factory())
            self._secrets.store(profile_id, draft.api_key)
            created_secret = True
        elif current is not None and self._same_origin_as_current(draft, current):
            # Blank key, same origin: reuse the saved secret and the UUID.
            try:
                self._secrets.lookup(current.profile_id)
            except AiError as exc:
                if exc.code is AiErrorCode.SECRET_NOT_FOUND:
                    raise AiError(
                        AiErrorCode.SECRET_NOT_FOUND,
                        "saved API key is missing; re-enter the key",
                    ) from exc
                raise
            profile_id = current.profile_id
        else:
            raise AiError(
                AiErrorCode.REQUEST_REJECTED,
                "bearer requires a new API key when the origin changes",
            )

        # One-shot non-secret metadata write (spec §9.3 step 5).
        metadata = self._metadata(profile_id, draft, endpoint, models_url)
        if not self._settings.set_ai_profile(metadata):
            if created_secret:
                try:
                    self._secrets.clear(profile_id)
                except AiError as rollback_exc:
                    self._pending_cleanup.append(profile_id)
                    raise AiError(
                        AiErrorCode.SETTINGS_WRITE_FAILED,
                        "connection not saved; temporary key cleanup failed",
                    ) from rollback_exc
            raise AiError(
                AiErrorCode.SETTINGS_WRITE_FAILED,
                "could not persist the AI profile metadata",
            )

        # Old secret cleanup on rotation / switch to none (spec §9.3 step 7).
        if current is not None and current.profile_id != profile_id:
            self._clear_old_secret(current.profile_id)

        return AiProfile(
            profile_id=profile_id,
            provider_kind=_DEFAULT_PROVIDER_KIND,
            api_base_url=endpoint.url,
            models_url=models_url,
            model_id=draft.model_id,
            auth_mode=draft.auth_mode,
        )

    def clear(self) -> None:
        """Clear the active connection (spec §9.4).

        Secret deletion first; only after it succeeds is the metadata cleared.
        Raises :class:`AiError` on failure; a settings-clear failure after a
        successful secret deletion is a partial failure the caller must show.
        """
        current = self.load()
        if current is None:
            return
        if current.auth_mode == "bearer":
            self._secrets.clear(current.profile_id)  # failure propagates
        if not self._settings.clear_ai_profile():
            raise AiError(
                AiErrorCode.SETTINGS_WRITE_FAILED,
                "secret removed but profile metadata could not be cleared",
            )

    def retry_pending_cleanup(self) -> None:
        """Retry rollback secret deletion for UUIDs recorded earlier."""
        remaining: list[str] = []
        for profile_id in self._pending_cleanup:
            try:
                self._secrets.clear(profile_id)
            except AiError:
                remaining.append(profile_id)
        self._pending_cleanup = remaining

    # -- internals ---------------------------------------------------------

    def _validate_draft(
        self, draft: AiConnectionDraft
    ) -> tuple[NormalizedEndpoint, str]:
        endpoint = normalize_api_base_url(draft.api_base_url)
        validate_auth_mode(draft.auth_mode, endpoint)
        models_url = (
            build_models_endpoint(endpoint, draft.models_url)
            if draft.models_url.strip()
            else ""
        )
        if draft.model_id and valid_model_id(draft.model_id) is None:
            raise AiError(
                AiErrorCode.REQUEST_REJECTED, "invalid model id in draft"
            )
        return endpoint, models_url

    @staticmethod
    def _same_origin_as_current(
        draft: AiConnectionDraft, current: AiProfile
    ) -> bool:
        try:
            draft_ep = normalize_api_base_url(draft.api_base_url)
            current_ep = normalize_api_base_url(current.api_base_url)
        except AiError:
            return False
        return same_origin(draft_ep, current_ep)

    @staticmethod
    def _metadata(
        profile_id: str,
        draft: AiConnectionDraft,
        endpoint: NormalizedEndpoint,
        models_url: str,
    ) -> dict[str, str]:
        return {
            "profile-id": profile_id,
            "provider-kind": _DEFAULT_PROVIDER_KIND,
            "api-base-url": endpoint.url,
            "models-url": models_url,
            "model-id": draft.model_id,
            "auth-mode": draft.auth_mode,
        }

    def _clear_old_secret(self, old_profile_id: str) -> None:
        try:
            self._secrets.clear(old_profile_id)
        except AiError as exc:
            self.cleanup_warnings.append(
                "new connection saved, but the old key cleanup failed"
            )
            if exc.code not in (
                AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
                AiErrorCode.CLEANUP_INCOMPLETE,
            ):
                self._pending_cleanup.append(old_profile_id)
