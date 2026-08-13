"""Secret Service (libsecret) key storage for the direct LLM migration.

Contract defined by docs/LLM_PROVIDER_MIGRATION_SPEC.md §4.1/§9.2/§9.3 and
pinned by tests/test_ai_secrets.py.

Rules:

- API keys never enter GSettings, files, logs, command lines, URLs or
  exception details (spec §4.1). Secret attributes carry only the application
  name and profile id, never the key (spec §9.2).
- The Secret GI import is lazy and guarded: a missing typelib or an
  unreachable keyring degrades to SECRET_SERVICE_UNAVAILABLE instead of a
  module-level crash or a plaintext fallback (spec §4.1.3, §11.1).
- Every store/lookup/clear failure is mapped to a stable AiErrorCode; raw
  GI/Secret exceptions are never re-raised, and error details never carry the
  key (spec §4.1.9).
- All methods are blocking and must be called from a background thread, never
  the GTK main thread (spec §4.4.11).
- ``InMemorySecretStore`` is the injected fake for unit tests; it never
  touches the real keyring (spec §13.1.7).
"""

from __future__ import annotations

import typing

from mdreader.models.ai import AiError, AiErrorCode


def secret_runtime_available() -> bool:
    """Probe the Secret GI typelib without ever raising.

    The probe is lazy (never a module-level import) so a missing optional AI
    runtime cannot break reader startup (spec §11.1). It only checks the
    typelib, not whether a keyring daemon is actually reachable.
    """
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret  # noqa: F401

        return True
    except Exception:
        return False


def secret_service_name_owned() -> bool:
    """Non-activating probe: is a Secret Service provider currently running?

    Queries the session bus daemon for the ``org.freedesktop.secrets`` name
    owner without ever activating a provider, so a machine with no keyring
    daemon never auto-starts one (product decision 2026-08-13: the app must
    connect to the AI service without requiring a keyring daemon). Any bus
    error counts as not owned, which lets callers fall back to a
    session-only secret store.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.freedesktop.secrets",)),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return bool(result.unpack()[0])
    except Exception:
        return False


def _require_secret() -> typing.Any:
    """Import Secret GI or raise :class:`AiError` SECRET_SERVICE_UNAVAILABLE.

    The raw import failure is never propagated; the caller only ever sees the
    stable error code (spec §4.1.9, §11.1).
    """
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        return Secret
    except Exception as exc:
        raise AiError(
            AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
            f"Secret runtime unavailable: {type(exc).__name__}",
        ) from None


def _probe_secret() -> typing.Any:
    """Return the Secret module or raise SECRET_SERVICE_UNAVAILABLE.

    Guards ``_require_secret`` a second time so a raw probe exception can
    never surface from any operation — runtime unavailability is always
    SECRET_SERVICE_UNAVAILABLE, never an operation-specific code.
    """
    try:
        return _require_secret()
    except AiError:
        raise
    except Exception as exc:
        raise AiError(
            AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
            f"Secret runtime unavailable: {type(exc).__name__}",
        ) from None


def _is_cancelled(exc: Exception) -> bool:
    """True when ``exc`` is a GLib.Error reporting G_IO_ERROR_CANCELLED."""
    try:
        from gi.repository import Gio

        matches = getattr(exc, "matches", None)
        if not callable(matches):
            return False
        return bool(matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED))
    except Exception:
        return False


def _safe_message(exc: Exception) -> str:
    """Short, bounded, key-free description of a GI/GLib failure.

    GLib error messages never echo the stored secret, but they are localized
    and unbounded, so only a stripped 200-character prefix is kept.
    """
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        message = message.strip()
        if message:
            return message[:200]
    return type(exc).__name__


def _map_failure(exc: Exception, code: AiErrorCode, operation: str) -> AiError:
    """Map a raw GI/Secret exception to :class:`AiError`.

    ``detail`` is deliberately bounded and key-free so the API key, the
    password or a raw error body can never leak into an exception or log
    (spec §4.1).
    """
    if _is_cancelled(exc):
        return AiError(AiErrorCode.CANCELLED, f"secret {operation} cancelled")
    return AiError(code, f"secret {operation} failed: {_safe_message(exc)}")


class AiSecretStore(typing.Protocol):
    """store 写入；lookup 缺失抛 SECRET_NOT_FOUND；clear 幂等。
    所有方法阻塞，必须由调用方在后台线程调用，禁止在 GTK 主线程执行。"""

    def store(self, profile_id: str, api_key: str) -> None: ...

    def lookup(self, profile_id: str) -> str: ...

    def clear(self, profile_id: str) -> None: ...


class SecretServiceStore:
    """libsecret-backed :class:`AiSecretStore` (spec §9.2).

    Construction never touches GI or the keyring; the first operation probes
    the runtime and degrades to SECRET_SERVICE_UNAVAILABLE. Methods are
    blocking and accept an optional ``Gio.Cancellable``; cancellation maps to
    CANCELLED.
    """

    def __init__(
        self,
        *,
        schema_name: str = "io.github.pang.mdreader.ai",
        label: str = "MD Reader AI API Key",
        application: str = "io.github.pang.mdreader",
    ) -> None:
        self._schema_name = schema_name
        self._label = label
        self._application = application

    def __repr__(self) -> str:
        return (
            f"SecretServiceStore(schema_name={self._schema_name!r}, "
            f"label={self._label!r}, application={self._application!r})"
        )

    def _schema(self, secret: typing.Any) -> typing.Any:
        """The Secret schema from spec §9.2: attributes never hold the key."""
        return secret.Schema.new(
            self._schema_name,
            secret.SchemaFlags.NONE,
            {
                "application": secret.SchemaAttributeType.STRING,
                "profile-id": secret.SchemaAttributeType.STRING,
            },
        )

    def _attributes(self, profile_id: str) -> dict[str, str]:
        return {
            "application": self._application,
            "profile-id": profile_id,
        }

    def store(
        self,
        profile_id: str,
        api_key: str,
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        """Persist ``api_key`` under ``profile_id`` (spec §9.3 step 2)."""
        try:
            secret = _probe_secret()
            stored = secret.password_store_sync(
                self._schema(secret),
                self._attributes(profile_id),
                secret.COLLECTION_DEFAULT,
                self._label,
                api_key,
                cancellable,
            )
        except AiError:
            raise
        except Exception as exc:
            raise _map_failure(
                exc, AiErrorCode.SECRET_SERVICE_UNAVAILABLE, "store"
            ) from None
        if not stored:
            raise AiError(
                AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
                "secret store failed: password_store_sync returned False",
            )

    def lookup(
        self,
        profile_id: str,
        cancellable: Gio.Cancellable | None = None,
    ) -> str:
        """Return the stored key or raise SECRET_NOT_FOUND."""
        try:
            secret = _probe_secret()
            value = secret.password_lookup_sync(
                self._schema(secret),
                self._attributes(profile_id),
                cancellable,
            )
        except AiError:
            raise
        except Exception as exc:
            raise _map_failure(
                exc, AiErrorCode.SECRET_SERVICE_UNAVAILABLE, "lookup"
            ) from None
        if value is None:
            raise AiError(
                AiErrorCode.SECRET_NOT_FOUND,
                "no secret stored for this profile",
            )
        return value

    def clear(
        self,
        profile_id: str,
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        """Remove the stored key; a missing item counts as success (幂等).

        A failed removal maps to CLEANUP_INCOMPLETE (spec §9.3 steps 7/8).
        """
        try:
            secret = _probe_secret()
            cleared = secret.password_clear_sync(
                self._schema(secret),
                self._attributes(profile_id),
                cancellable,
            )
        except AiError:
            raise
        except Exception as exc:
            raise _map_failure(
                exc, AiErrorCode.CLEANUP_INCOMPLETE, "clear"
            ) from None
        if not cleared:
            raise AiError(
                AiErrorCode.CLEANUP_INCOMPLETE,
                "secret clear failed: password_clear_sync returned False",
            )


class InMemorySecretStore:
    """In-memory :class:`AiSecretStore` fake for tests (spec §13.1.7).

    Never touches the real keyring. ``set_unavailable(True)`` simulates an
    unreachable Secret Service: every operation then raises
    SECRET_SERVICE_UNAVAILABLE, exactly as the real store degrades.
    """

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}
        self._unavailable = False

    @classmethod
    def from_mapping(cls, initial: dict[str, str]) -> "InMemorySecretStore":
        """Build a store preloaded with ``initial`` (profile_id -> key)."""
        store = cls()
        store._secrets.update(initial)
        return store

    def __repr__(self) -> str:
        # Never renders stored keys; only the count/state is shown.
        state = "unavailable" if self._unavailable else f"{len(self._secrets)} stored"
        return f"InMemorySecretStore({state})"

    def set_unavailable(self, unavailable: bool = True) -> None:
        self._unavailable = unavailable

    def _check_available(self) -> None:
        if self._unavailable:
            raise AiError(
                AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
                "Secret Service unavailable",
            )

    def store(self, profile_id: str, api_key: str) -> None:
        self._check_available()
        self._secrets[profile_id] = api_key

    def lookup(self, profile_id: str) -> str:
        self._check_available()
        try:
            return self._secrets[profile_id]
        except KeyError:
            raise AiError(
                AiErrorCode.SECRET_NOT_FOUND,
                "no secret stored for this profile",
            ) from None

    def clear(self, profile_id: str) -> None:
        self._check_available()
        self._secrets.pop(profile_id, None)  # idempotent: missing item is success
