"""AI connection settings dialog (docs/LLM_PROVIDER_MIGRATION_SPEC.md §7.1/§7.3/§7.4/§9.3/§10.2/§10.3/§13.7).

A native ``Adw.PreferencesDialog`` whose widgets own no network, Secret
Service or GSettings access: every service operation is routed through the
injected :class:`DialogCallbacks` to the window coordinator (spec §4.4.2).

The pure-logic :class:`ConnectionDialogController` lives in the same module
but never imports gi, so unit tests exercise validation, auth-mode and
key-source decisions, model filtering, the draft fingerprint and stale-result
handling headlessly. The GTK import is guarded: with the Adw/Gtk typelibs
missing the module still imports and the controller stays usable; only
``AiConnectionDialog`` degrades to a placeholder that raises on construction
(spec §11.1 optional-runtime degradation).

Credential discipline (spec §4.1/§5.3): the password entry is never
prefilled, the controller holds the typed key only for the dialog's lifetime,
``draft_fingerprint`` never receives or encodes the key value, and closing the
dialog clears both the entry and the controller's reference.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol

from mdreader.models import AiConnectionDraft, AiError, AiErrorCode, AiModel, AiProfile
from mdreader.services.ai_endpoints import (
    EndpointError,
    NormalizedEndpoint,
    build_models_endpoint,
    is_loopback_endpoint,
    normalize_api_base_url,
    same_origin,
)
from mdreader.services.ai_models import (
    ModelCatalogResult,
    draft_fingerprint,
    valid_model_id,
)
from mdreader.services.ai_profiles import validate_api_key

# -- key source enum and user-facing copy (spec §7.1/§7.4/§10.2) -------------

KEY_SOURCE_NEW = "new-key"
KEY_SOURCE_SAVED = "saved-same-origin"
KEY_SOURCE_NONE = "none"

MSG_BASE_URL_INVALID = "API 地址无效，请检查格式（例如 https://api.example.com/v1）"
MSG_BASE_URL_INSECURE = "必须使用 HTTPS，或本机回环地址（http://localhost 或 127.0.0.1）"
MSG_MODELS_URL_CROSS_ORIGIN = "模型列表地址必须与 API 地址同源"
MSG_MODELS_URL_INVALID = "模型列表地址无效"
MSG_MODEL_ID_INVALID = "模型 ID 无效"
MSG_KEY_INVALID = "API Key 无效，请检查格式"
MSG_FETCH_MISSING_BOTH = "请先填写 API 地址和密钥"
MSG_FETCH_MISSING_BASE = "请填写 API 基础地址"
MSG_FETCH_MISSING_KEY = "请填写 API Key"
MSG_KEY_HINT_SAVED = "留空则继续使用已保存密钥"
MSG_KEY_HINT_RETYPE = "请重新输入 API Key"
MSG_KEY_HINT_NONE = "无需鉴权，无需输入 API Key"
MODELS_EMPTY_MESSAGE = "服务返回了 0 个模型，可手动填写模型 ID"
MSG_MODEL_NOT_IN_RESULT = "此模型未出现在本次结果中"
MSG_MODEL_UNVERIFIED = "未验证的手动 ID"
MSG_FALLBACK = "请求失败，请稍后重试"

MODELS_ERROR_MESSAGES: dict[AiErrorCode, str] = {
    AiErrorCode.INVALID_URL: MSG_BASE_URL_INVALID,
    AiErrorCode.INSECURE_REMOTE_URL: MSG_BASE_URL_INSECURE,
    AiErrorCode.CROSS_ORIGIN_MODELS_URL: MSG_MODELS_URL_CROSS_ORIGIN,
    AiErrorCode.AUTHENTICATION_FAILED: "API Key 无效或无权读取模型列表",
    AiErrorCode.PERMISSION_DENIED: "API Key 无效或无权读取模型列表",
    AiErrorCode.ENDPOINT_NOT_FOUND: "此地址没有可用的模型列表接口，可检查地址或手动填写模型",
    AiErrorCode.BILLING_OR_QUOTA_REQUIRED: "账户额度或计费状态不允许请求",
    AiErrorCode.RATE_LIMITED: "请求过于频繁，请稍后重试",
    AiErrorCode.PROVIDER_UNAVAILABLE: "AI 服务暂时不可用，请稍后重试",
    AiErrorCode.TIMEOUT: "获取模型超时，请检查网络和服务地址",
    AiErrorCode.TLS_FAILED: "无法验证服务端安全连接",
    AiErrorCode.NETWORK_FAILED: "网络连接失败，请检查网络后重试",
    AiErrorCode.INVALID_RESPONSE: "服务返回的模型列表格式不兼容",
    AiErrorCode.RESPONSE_TOO_LARGE: "服务返回的模型列表过大",
    AiErrorCode.SECRET_SERVICE_UNAVAILABLE: "系统密钥环不可用，无法读取已保存密钥",
    AiErrorCode.SECRET_NOT_FOUND: "已保存密钥丢失，请重新输入 API Key",
    AiErrorCode.SETTINGS_WRITE_FAILED: "连接未保存，请检查系统设置后重试",
    AiErrorCode.CLEANUP_INCOMPLETE: "连接已保存，但旧密钥清理失败",
    AiErrorCode.REQUEST_REJECTED: "请求被拒绝，请检查输入内容",
    AiErrorCode.AI_RUNTIME_UNAVAILABLE: "AI 运行时不可用",
    AiErrorCode.MODEL_NOT_SELECTED: "请选择或输入模型 ID",
}


def error_message(code: AiErrorCode | str, *, context: str = "models") -> str | None:
    """Map a stable error code to user-facing copy (spec §7.4).

    ``CANCELLED`` maps to None: a cancelled fetch must not append a red error
    (spec §7.4 "cancelled" keeps the draft without an error message). The
    default models context follows the §7.4 table; ``context="chat"`` uses
    the §8.1 wording for authentication/permission failures.
    """
    try:
        key = AiErrorCode(code)
    except ValueError:
        return MSG_FALLBACK
    if key is AiErrorCode.CANCELLED:
        return None
    if context == "chat":
        # Chat-context copy: the generic table describes the /models fetch
        # and misleads when the failure happened while asking (swarm audit
        # M1 — e.g. a timeout said "获取模型超时").
        chat_messages: dict[AiErrorCode, str] = {
            AiErrorCode.AUTHENTICATION_FAILED: "API Key 认证失败，请检查密钥",
            AiErrorCode.PERMISSION_DENIED: "当前密钥无权访问此服务",
            AiErrorCode.ENDPOINT_NOT_FOUND: "此地址没有可用的对话接口，请检查 AI 连接设置",
            AiErrorCode.BILLING_OR_QUOTA_REQUIRED: "账户额度或计费状态不允许请求",
            AiErrorCode.RATE_LIMITED: "请求过于频繁，请稍后重试",
            AiErrorCode.PROVIDER_UNAVAILABLE: "AI 服务暂时不可用，请稍后重试",
            AiErrorCode.TIMEOUT: "提问超时，请检查网络和服务地址",
            AiErrorCode.TLS_FAILED: "无法验证服务端安全连接",
            AiErrorCode.NETWORK_FAILED: "网络连接失败，请检查网络后重试",
            AiErrorCode.INVALID_RESPONSE: "服务返回的对话内容格式不兼容",
            AiErrorCode.RESPONSE_TOO_LARGE: "回答内容过大",
            AiErrorCode.SECRET_SERVICE_UNAVAILABLE: "系统密钥环不可用，无法读取已保存密钥",
            AiErrorCode.SECRET_NOT_FOUND: "已保存密钥丢失，请重新输入 API Key",
            AiErrorCode.REQUEST_REJECTED: "请求被拒绝，请检查输入内容",
        }
        return chat_messages.get(key, MSG_FALLBACK)
    return MODELS_ERROR_MESSAGES.get(key, MSG_FALLBACK)


class DialogCallbacks(Protocol):
    """Window-coordinator callbacks; the dialog routes all service work through
    these so widgets never touch the network or the Secret Service (spec §4.4.2)."""

    def on_fetch_models(self, draft: AiConnectionDraft) -> None: ...

    def on_save(self, draft: AiConnectionDraft) -> None: ...

    def on_clear(self) -> None: ...

    def on_close(self) -> None: ...


class ConnectionDialogController:
    """Pure-logic form state for the connection dialog (spec §7.1/§10.2).

    Owns the form fields — including the current password-entry value for the
    dialog's lifetime — and derives every decision from them: field errors,
    auth-mode resolution, the key-source enum, the draft fingerprint, the
    draft itself and stale-result handling. It never touches GTK, the network
    or the Secret Service.
    """

    def __init__(self, profile: AiProfile | None) -> None:
        self._profile = profile
        self._api_base_url = profile.api_base_url if profile else ""
        self._models_url = profile.models_url if profile else ""
        self._api_key = ""
        self._auth_mode = profile.auth_mode if profile else "bearer"
        self._model_id = profile.model_id if profile else ""
        self._key_revision = 0
        self._generation = 0
        self._last_result: ModelCatalogResult | None = None
        self._last_error: AiError | None = None
        self._catalog_deprecated = False

    # -- form state ----------------------------------------------------------

    def set_api_base_url(self, value: str) -> None:
        if value != self._api_base_url:
            self._api_base_url = value
            self._bump()

    def set_models_url(self, value: str) -> None:
        if value != self._models_url:
            self._models_url = value
            self._bump()

    def set_auth_mode(self, value: str) -> None:
        value = "bearer" if value != "none" else "none"
        if value != self._auth_mode:
            self._auth_mode = value
            self._bump()

    def set_key(self, value: str) -> None:
        if value != self._api_key:
            self._api_key = value
            self._bump()

    def set_model_id(self, value: str) -> None:
        # Model selection does not change the endpoint, so it neither advances
        # the draft revision nor invalidates a fetched catalog (spec §7.1).
        self._model_id = value

    def clear_key(self) -> None:
        """Drop the key reference when the dialog closes (spec §4.1.10/§10.2)."""
        self._api_key = ""

    # -- derived queries -----------------------------------------------------

    @property
    def api_base_url(self) -> str:
        return self._api_base_url

    @property
    def models_url(self) -> str:
        return self._models_url

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def auth_mode(self) -> str:
        return self._auth_mode

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def key_revision(self) -> int:
        return self._key_revision

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def last_result(self) -> ModelCatalogResult | None:
        return self._last_result

    @property
    def last_error(self) -> AiError | None:
        return self._last_error

    def normalized_base(self) -> NormalizedEndpoint | None:
        try:
            return normalize_api_base_url(self._api_base_url)
        except EndpointError:
            return None

    @property
    def is_loopback(self) -> bool:
        endpoint = self.normalized_base()
        return endpoint is not None and is_loopback_endpoint(endpoint)

    @property
    def effective_auth_mode(self) -> str:
        """Remote URLs always resolve to bearer; ``none`` survives only on an
        explicitly chosen, confirmed loopback address (spec §4.1.4/§6.3)."""
        if self._auth_mode == "none" and self.is_loopback:
            return "none"
        return "bearer"

    def auth_switch_available(self) -> bool:
        """The "无需鉴权" switch is only usable once the base URL is a
        confirmed loopback address."""
        return self.is_loopback

    @property
    def key_source(self) -> str:
        """Where the effective key comes from (spec §7.1 fingerprint enum).

        ``new-key`` — typed into the password entry; ``saved-same-origin`` —
        blank entry that may reuse the saved secret; ``none`` — no key (either
        explicit ``auth-mode=none`` on loopback, or a blank bearer draft whose
        origin differs from the saved one and must not reach the network).
        """
        if self.effective_auth_mode == "none":
            return KEY_SOURCE_NONE
        if self._api_key:
            return KEY_SOURCE_NEW
        if self._same_origin_as_saved():
            return KEY_SOURCE_SAVED
        return KEY_SOURCE_NONE

    @property
    def key_hint(self) -> str:
        """Caption under the password entry (spec §10.2): never implies that a
        blank entry switches to ``none``, and never hints at reusing a key on
        a different origin (spec §4.1.4)."""
        if self.effective_auth_mode == "none":
            return MSG_KEY_HINT_NONE
        if self._api_key:
            return ""
        if self._same_origin_as_saved():
            return MSG_KEY_HINT_SAVED
        return MSG_KEY_HINT_RETYPE

    @property
    def fingerprint(self) -> str:
        """Stale-check fingerprint (spec §7.1). Only the key source enum and
        the monotonic revision enter the payload — never the key itself."""
        return draft_fingerprint(
            api_base_url=self._api_base_url,
            models_url=self._models_url,
            auth_mode=self.effective_auth_mode,
            key_source=self.key_source,
            key_revision=self._key_revision,
        )

    # -- validation ----------------------------------------------------------

    def base_url_error(self) -> str | None:
        """Field error for a non-empty invalid API base URL; empty input is
        reported by :meth:`fetch_error` / :meth:`save_error` instead."""
        raw = self._api_base_url.strip()
        if not raw:
            return None
        try:
            normalize_api_base_url(raw)
        except EndpointError as exc:
            if exc.code is AiErrorCode.INSECURE_REMOTE_URL:
                return MSG_BASE_URL_INSECURE
            return MSG_BASE_URL_INVALID
        return None

    def models_url_error(self) -> str | None:
        """Field error for the explicit models URL (spec §6.1/§6.2)."""
        explicit = self._models_url.strip()
        if not explicit:
            return None
        base = self.normalized_base()
        if base is None:
            return None  # the base URL field already reports the problem
        try:
            build_models_endpoint(base, explicit)
        except EndpointError as exc:
            if exc.code is AiErrorCode.CROSS_ORIGIN_MODELS_URL:
                return MSG_MODELS_URL_CROSS_ORIGIN
            return MSG_MODELS_URL_INVALID
        return None

    def model_id_error(self) -> str | None:
        if not self._model_id.strip():
            return None
        if valid_model_id(self._model_id) is None:
            return MSG_MODEL_ID_INVALID
        return None

    def key_error(self) -> str | None:
        """Missing or malformed API key for the effective bearer mode.

        A blank entry is only acceptable when the saved secret may be reused;
        otherwise the user must retype it (spec §4.1.4/§4.1.5). Keys are never
        trimmed or rewritten (spec §6.1)."""
        if self.effective_auth_mode == "none":
            return None
        if not self._api_key:
            if self._same_origin_as_saved():
                return None
            return MSG_FETCH_MISSING_KEY
        try:
            validate_api_key(self._api_key)
        except AiError:
            return MSG_KEY_INVALID
        return None

    def fetch_error(self) -> str | None:
        """Blocking validation before '获取模型' (spec §7.4)."""
        if not self._api_base_url.strip():
            if not self._api_key and self.effective_auth_mode != "none":
                return MSG_FETCH_MISSING_BOTH
            return MSG_FETCH_MISSING_BASE
        base_error = self.base_url_error()
        if base_error:
            return base_error
        key_error = self.key_error()
        if key_error:
            return key_error
        return self.models_url_error()

    def save_error(self) -> str | None:
        """Blocking validation before '保存连接' (spec §9.3 step 1)."""
        if not self._api_base_url.strip():
            return MSG_FETCH_MISSING_BASE
        base_error = self.base_url_error()
        if base_error:
            return base_error
        key_error = self.key_error()
        if key_error:
            return key_error
        models_error = self.models_url_error()
        if models_error:
            return models_error
        return self.model_id_error()

    # -- draft ---------------------------------------------------------------

    def build_draft(self) -> AiConnectionDraft:
        """Current form draft (spec §5.3).

        The API key travels only inside the returned draft for the duration of
        the dialog's life; the dialog drops it on close (spec §11.2).
        """
        endpoint = self.normalized_base()
        base_url = endpoint.url if endpoint is not None else self._api_base_url.strip()
        models_url = ""
        explicit = self._models_url.strip()
        if explicit:
            if endpoint is not None:
                try:
                    models_url = build_models_endpoint(endpoint, explicit)
                except EndpointError:
                    models_url = explicit
            else:
                models_url = explicit
        return AiConnectionDraft(
            api_base_url=base_url,
            models_url=models_url,
            api_key=self._api_key,
            auth_mode=self.effective_auth_mode,
            model_id=self._model_id.strip(),
            keep_existing_secret=self.key_source == KEY_SOURCE_SAVED,
        )

    # -- fetch lifecycle / staleness (spec §7.1) -----------------------------

    def begin_fetch(self) -> int:
        """Mark a fetch start: advance the generation so any older in-flight
        result is dropped when it arrives. Returns the fresh generation the
        coordinator should echo back into :meth:`apply_fetch_result`."""
        self._generation += 1
        return self._generation

    def apply_fetch_result(
        self,
        result: ModelCatalogResult | None,
        error: AiError | None,
        *,
        generation: int,
    ) -> bool:
        """Apply a fetch result only when its generation is still current.

        Returns False (and changes nothing) for a stale callback, so a result
        from an abandoned request can never overwrite newer form state.
        """
        if generation != self._generation:
            return False
        self._last_result = result
        self._last_error = error
        self._catalog_deprecated = False
        return True

    @property
    def has_catalog(self) -> bool:
        return self._last_result is not None and self._last_error is None

    @property
    def catalog_empty(self) -> bool:
        return self.has_catalog and not self._last_result.models

    @property
    def selected_in_catalog(self) -> bool:
        if not self.has_catalog or not self._model_id:
            return False
        return self._model_id in {m.model_id for m in self._last_result.models}

    @property
    def model_hint(self) -> str | None:
        """Caption under the model entry (spec §7.1/§7.3)."""
        if not self._model_id.strip():
            return None
        if self._catalog_deprecated:
            return MSG_MODEL_UNVERIFIED
        if self.has_catalog and not self.selected_in_catalog:
            return MSG_MODEL_NOT_IN_RESULT
        return None

    # -- model list filtering (spec §7.2/§10.3) ------------------------------

    @staticmethod
    def filter_models(models: Sequence[AiModel], query: str) -> tuple[AiModel, ...]:
        """Casefold substring filter; an empty query returns everything and
        original IDs (and objects) are preserved."""
        needle = query.casefold()
        if not needle:
            return tuple(models)
        return tuple(m for m in models if needle in m.model_id.casefold())

    # -- internals -----------------------------------------------------------

    def _bump(self) -> None:
        """Any draft-affecting field change advances the revision so the
        previous fetch result is stale and its generation no longer matches
        (spec §7.1)."""
        self._key_revision += 1
        self._generation += 1
        if self.has_catalog:
            self._catalog_deprecated = True
        self._last_result = None
        self._last_error = None

    def _same_origin_as_saved(self) -> bool:
        """True when the blank password entry may reuse the saved secret: only
        a saved bearer profile whose normalized origin matches the draft
        (spec §4.1.4/§4.1.5)."""
        if self._profile is None or self._profile.auth_mode != "bearer":
            return False
        try:
            draft_ep = normalize_api_base_url(self._api_base_url)
            saved_ep = normalize_api_base_url(self._profile.api_base_url)
        except EndpointError:
            return False
        return same_origin(draft_ep, saved_ep)


# -- GTK dialog (guarded: optional runtime, spec §11.1) ----------------------

try:
    import gi

    gi.require_version("Adw", "1")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gio", "2.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False


if GTK_AVAILABLE:

    class _FieldLabelRow(Adw.PreferencesRow):
        """A PreferencesGroup row hosting a wrapping caption/error label.

        Wrapping the label in a real PreferencesRow fixes the 200% text
        scaling overlap that a bare Gtk.Label child suffers in a group.
        Exposes ``set_label``/``get_label`` so callers treat it like a label.
        """

        __gtype_name__ = "MdReaderFieldLabelRow"

        def __init__(self, *css_classes: str) -> None:
            super().__init__()
            self._label = Gtk.Label(xalign=0, wrap=True)
            for css_class in css_classes:
                self._label.add_css_class(css_class)
            self._label.set_margin_start(16)
            self._label.set_margin_end(16)
            self.set_child(self._label)
            self.set_activatable(False)
            self.set_focusable(False)
            self.set_visible(False)

        def set_label(self, text: str) -> None:
            self._label.set_label(text)

        def get_label(self) -> str:
            return self._label.get_label()

        def set_error(self, error: bool) -> None:
            if error:
                self._label.add_css_class("error")
            else:
                self._label.remove_css_class("error")

    class _ModelRow(GObject.Object):
        """GObject row carrying one catalog entry for :class:`Gio.ListStore`."""

        __gtype_name__ = "MdReaderModelRow"

        def __init__(self, model: AiModel) -> None:
            super().__init__()
            self.model_id = model.model_id
            self.owned_by = model.owned_by

    class AiConnectionDialog(Adw.PreferencesDialog):
        """AI connection settings dialog (spec §10.2/§10.3).

        Widgets only: every service operation goes through
        :class:`DialogCallbacks` to the window coordinator; form logic lives in
        :class:`ConnectionDialogController`.
        """

        def __init__(
            self,
            *,
            profile: AiProfile | None,
            callbacks: DialogCallbacks,
            theme_class: str | None = None,
        ) -> None:
            super().__init__()
            self._callbacks = callbacks
            self._controller = ConnectionDialogController(profile)
            self._has_profile = profile is not None
            self._fetching = False
            self._busy = False
            self._theme_class = theme_class or ""
            self.set_title("AI 连接设置")
            self.set_content_width(520)
            self.set_content_height(640)
            if self._theme_class:
                # Carry the active reading theme's surface class so the
                # generated theme CSS (dialog.theme-<id>) can paint this
                # dialog with the theme palette instead of libadwaita's
                # default near-black (#1d1d20) in dark themes.
                self.add_css_class(self._theme_class)
            self._build_ui(profile)
            self.connect("closed", self._on_closed)
            self._maybe_run_test_hook()

        # -- public API (fixed for the window coordinator) -------------------

        @property
        def draft(self) -> AiConnectionDraft:
            return self._controller.build_draft()

        @property
        def fingerprint(self) -> str:
            return self._controller.fingerprint

        @property
        def selected_model(self) -> str:
            return self._controller.model_id

        @property
        def generation(self) -> int:
            """Current fetch generation; the coordinator captures it before
            starting a fetch and echoes it into :meth:`set_fetch_result`."""
            return self._controller.generation

        def set_fetch_result(
            self,
            result: ModelCatalogResult | None,
            error: AiError | None,
            *,
            generation: int,
        ) -> None:
            """Apply a models fetch outcome; stale generations are dropped
            (spec §7.1)."""
            if not self._controller.apply_fetch_result(result, error, generation=generation):
                return
            self._fetching = False
            self._update_fetch_ui()
            # The 获取模型 button lives on the 模型 page: switch there after
            # any completed fetch so the outcome (list or error) is visible
            # next to the action that triggered it (cross-page feedback,
            # swarm audit F1).
            self.set_visible_page_name("model")
            if error is not None:
                message = error_message(error.code)
                self._show_fetch_status(message or "获取模型失败，请稍后重试", error=True)
                if message is not None:
                    self._show_banner(message)
                return
            if result is not None and os.environ.get("MDREADER_TEST_AI_AUTOFILL") == "1":
                print(f"MDREADER_TEST_AI_MODELS={len(result.models)}", flush=True)
            self._sync_catalog()

        def set_save_result(
            self,
            ok: bool,
            error: AiError | None,
            *,
            cleanup_warnings: tuple[str, ...] = (),
        ) -> None:
            """Outcome of the asynchronous save (spec §9.3)."""
            self._set_busy(False)
            if ok:
                # Cleanup warnings are surfaced by the window coordinator as
                # a toast (_after_ai_saved); showing them here is pointless
                # because the dialog closes on the same frame (swarm audit
                # F3: the banner flashed and vanished before it could be
                # read).
                self.close()
                return
            message = error_message(error.code) if error is not None else "保存失败，请稍后重试"
            self._show_banner(message)

        def set_clear_result(self, ok: bool, error: AiError | None) -> None:
            """Outcome of the asynchronous clear (spec §9.4)."""
            self._set_busy(False)
            if ok:
                self.close()
                return
            message = error_message(error.code) if error is not None else "清除连接失败，请稍后重试"
            self._show_banner(message)

        # -- process-level smoke hook ----------------------------------------

        def _maybe_run_test_hook(self) -> None:
            """Autofill the form from the environment for process-level GTK
            smoke checks (MDREADER_TEST_AI_AUTOFILL=1). Never active in normal
            use; mirrors what a user would type and click."""
            if os.environ.get("MDREADER_TEST_AI_AUTOFILL") != "1":
                return
            base = os.environ.get("MDREADER_TEST_AI_BASE_URL", "")
            models_url = os.environ.get("MDREADER_TEST_AI_MODELS_URL", "")
            key = os.environ.get("MDREADER_TEST_AI_KEY", "")
            model = os.environ.get("MDREADER_TEST_AI_MODEL", "")
            if os.environ.get("MDREADER_TEST_AI_AUTH") == "none":
                self._auth_switch.set_active(True)
            if base:
                self._base_entry.set_text(base)
            if models_url:
                self._models_entry.set_text(models_url)
            if key:
                self._key_entry.set_text(key)
            if model:
                self._model_entry.set_text(model)
            action = os.environ.get("MDREADER_TEST_AI_ACTION", "fetch")
            if action == "fetch":
                GLib.idle_add(self._on_fetch_clicked, None)
            elif action == "save":
                GLib.idle_add(self._on_save_clicked, None)
            if os.environ.get("MDREADER_TEST_AI_MODEL_PAGE") == "1":
                # Open the dialog on the 模型 page so captures can show the
                # long-model-ID field (process-level smoke hook only).
                self.set_visible_page_name("model")

        # -- UI construction -------------------------------------------------

        def _build_ui(self, profile: AiProfile | None) -> None:
            connection_page = Adw.PreferencesPage(title="连接")
            connection_page.set_name("connection")
            model_page = Adw.PreferencesPage(title="模型")
            model_page.set_name("model")
            privacy_page = Adw.PreferencesPage(title="隐私")

            # 连接
            conn_group = Adw.PreferencesGroup()
            connection_page.add(conn_group)

            self._banner = Adw.Banner(title="")
            self._banner.set_revealed(False)
            self._banner.connect(
                "button-clicked", lambda *_: self._banner.set_revealed(False)
            )
            conn_group.add(self._banner)

            self._base_entry = Adw.EntryRow(title="API 基础地址")
            self._base_entry.set_text(self._controller.api_base_url)
            self._set_placeholder(self._base_entry, "https://api.example.com/v1")
            self._base_entry.set_show_apply_button(False)
            self._base_entry.connect("changed", self._on_base_changed)
            conn_group.add(self._base_entry)
            self._base_error = self._new_field_label("error")
            conn_group.add(self._base_error)

            self._key_entry = Adw.PasswordEntryRow(title="API Key")
            self._key_entry.set_show_apply_button(False)
            self._key_entry.connect("changed", self._on_key_changed)
            conn_group.add(self._key_entry)
            self._key_hint = self._new_field_label("caption", "dimmed")
            conn_group.add(self._key_hint)
            self._key_error = self._new_field_label("error")
            conn_group.add(self._key_error)

            self._auth_switch = Adw.SwitchRow(
                title="无需鉴权",
                subtitle="仅对本机回环地址（localhost / 127.0.0.1）可用",
            )
            self._auth_switch.set_active(self._controller.auth_mode == "none")
            self._auth_switch.connect("notify::active", self._on_auth_toggled)
            conn_group.add(self._auth_switch)
            self._sync_auth_switch()

            # 模型
            model_group = Adw.PreferencesGroup()
            model_page.add(model_group)

            self._advanced = Adw.ExpanderRow(
                title="模型列表地址",
                subtitle="高级设置；默认使用 {base}/models",
            )
            self._models_entry = Adw.EntryRow(title="精确模型列表地址")
            self._models_entry.set_text(self._controller.models_url)
            self._set_placeholder(self._models_entry, "https://api.example.com/v1/models")
            self._models_entry.set_show_apply_button(False)
            self._models_entry.connect("changed", self._on_models_changed)
            self._advanced.add_row(self._models_entry)
            self._models_error_row = Adw.ActionRow()
            self._models_error_row.add_css_class("error")
            self._models_error_row.set_activatable(False)
            self._models_error_row.set_visible(False)
            self._advanced.add_row(self._models_error_row)
            model_group.add(self._advanced)

            fetch_row = Adw.ActionRow(title="获取模型")
            self._fetch_spinner = Adw.Spinner()
            self._fetch_spinner.set_visible(False)
            self._fetch_spinner.update_property(
                [Gtk.AccessibleProperty.LABEL], ["正在获取模型"]
            )
            fetch_row.add_suffix(self._fetch_spinner)
            self._fetch_button = Gtk.Button(label="获取模型")
            self._fetch_button.set_tooltip_text("使用当前地址和密钥读取可用模型")
            self._fetch_button.update_property(
                [Gtk.AccessibleProperty.LABEL], ["获取模型"]
            )
            self._fetch_button.connect("clicked", self._on_fetch_clicked)
            fetch_row.add_suffix(self._fetch_button)
            fetch_row.set_activatable_widget(self._fetch_button)
            model_group.add(fetch_row)

            self._fetch_status = self._new_field_label("caption", "dimmed")
            model_group.add(self._fetch_status)

            self._model_entry = Adw.EntryRow(title="当前模型")
            self._model_entry.set_text(self._controller.model_id)
            self._set_placeholder(self._model_entry, "从列表选择，或手动填写模型 ID")
            self._model_entry.set_show_apply_button(False)
            self._model_entry.connect("changed", self._on_model_changed)
            self._model_entry.connect("activate", self._on_model_activate)
            model_group.add(self._model_entry)
            self._model_error = self._new_field_label("error")
            model_group.add(self._model_error)
            self._model_hint = self._new_field_label("caption", "dimmed")
            model_group.add(self._model_hint)

            self._picker_revealer = Gtk.Revealer()
            picker = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self._search_entry = Gtk.SearchEntry()
            self._search_entry.set_placeholder_text("搜索模型…")
            self._search_entry.connect("search-changed", self._on_search_changed)
            picker.append(self._search_entry)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            # Fixed-height list: with only max_content_height the ListView
            # collapses to its natural height (a single row), so the picker
            # looked like a one-line strip. vexpand + min height make the
            # "dropdown" a real ~10-row list (user report: 下拉条做长一点).
            scroller.set_min_content_height(360)
            scroller.set_max_content_height(360)
            self._model_store = Gio.ListStore.new(_ModelRow)
            self._filter = Gtk.CustomFilter.new(self._filter_model, None)
            self._filtered = Gtk.FilterListModel.new(self._model_store, self._filter)
            self._selection = Gtk.SingleSelection.new(self._filtered)
            self._list_view = Gtk.ListView.new(self._selection, self._new_row_factory())
            self._list_view.set_vexpand(True)
            self._list_view.connect("activate", self._on_row_activated)
            escape_controller = Gtk.EventControllerKey()
            escape_controller.connect("key-pressed", self._on_picker_key)
            self._list_view.add_controller(escape_controller)
            scroller.set_child(self._list_view)
            picker.append(scroller)
            self._picker_empty = Gtk.Label(
                label="无匹配模型",
                xalign=0,
                wrap=True,
            )
            self._picker_empty.add_css_class("caption")
            self._picker_empty.add_css_class("dimmed")
            self._picker_empty.set_visible(False)
            picker.append(self._picker_empty)
            self._picker_revealer.set_child(picker)
            self._picker_revealer.set_reveal_child(False)
            model_group.add(self._picker_revealer)

            # 隐私
            privacy_group = Adw.PreferencesGroup()
            privacy_page.add(privacy_group)
            privacy_label = Gtk.Label(
                label="发送问题时，MD Reader 会把当前文档的受限摘录、选区、相对路径、"
                "行号和你的问题发送到你配置的 AI 服务。应用不会把完整工作区自动发送给模型。",
                wrap=True,
                xalign=0,
            )
            privacy_group.add(privacy_label)

            # 操作
            actions_group = Adw.PreferencesGroup()
            connection_page.add(actions_group)

            self._clear_row = Adw.ActionRow(
                title="清除连接",
                subtitle="删除已保存的 API Key 与连接配置",
            )
            self._clear_row.add_css_class("error")
            self._clear_row.set_activatable(True)
            self._clear_row.set_sensitive(profile is not None)
            self._clear_row.connect("activated", self._on_clear_clicked)
            actions_group.add(self._clear_row)

            buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            buttons.set_margin_top(8)
            buttons.set_margin_bottom(8)
            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            buttons.append(spacer)
            self._cancel_button = Gtk.Button(label="取消")
            self._cancel_button.set_tooltip_text("关闭对话框")
            self._cancel_button.connect("clicked", lambda *_: self.close())
            self._save_button = Gtk.Button(label="保存连接")
            self._save_button.add_css_class("suggested-action")
            self._save_button.set_tooltip_text("保存连接配置")
            self._save_button.update_property(
                [Gtk.AccessibleProperty.LABEL], ["保存连接"]
            )
            self._save_button.connect("clicked", self._on_save_clicked)
            buttons.append(self._cancel_button)
            buttons.append(self._save_button)
            actions_group.add(buttons)

            self.add(connection_page)
            self.add(model_page)
            self.add(privacy_page)

            self._sync_auth_switch()
            self._update_key_hint()
            self._update_model_hint()

        @staticmethod
        def _new_row_factory() -> Gtk.SignalListItemFactory:
            factory = Gtk.SignalListItemFactory()

            def on_setup(_factory, item) -> None:
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                box.set_margin_top(6)
                box.set_margin_bottom(6)
                box.set_margin_start(12)
                box.set_margin_end(12)
                id_label = Gtk.Label(
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.END,
                    single_line_mode=True,
                )
                owner_label = Gtk.Label(
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.END,
                    single_line_mode=True,
                )
                owner_label.add_css_class("caption")
                owner_label.add_css_class("dimmed")
                box.append(id_label)
                box.append(owner_label)
                box._id_label = id_label
                box._owner_label = owner_label
                item.set_child(box)

            def on_bind(_factory, item) -> None:
                row = item.get_item()
                child = item.get_child()
                child._id_label.set_label(row.model_id)
                child._id_label.set_tooltip_text(row.model_id)
                child._id_label.update_property(
                    [Gtk.AccessibleProperty.LABEL], [row.model_id]
                )
                child._owner_label.set_label(row.owned_by)
                child._owner_label.set_visible(bool(row.owned_by))

            factory.connect("setup", on_setup)
            factory.connect("bind", on_bind)
            return factory

        # -- widget handlers -------------------------------------------------

        def _on_base_changed(self, _entry) -> None:
            self._controller.set_api_base_url(self._base_entry.get_text())
            self._set_field_error(self._base_error, None)
            self._sync_auth_switch()
            self._reset_fetch()

        def _on_models_changed(self, _entry) -> None:
            self._controller.set_models_url(self._models_entry.get_text())
            self._models_error_row.set_visible(False)
            self._reset_fetch()

        def _on_key_changed(self, _entry) -> None:
            self._controller.set_key(self._key_entry.get_text())
            self._set_field_error(self._key_error, None)
            self._update_key_hint()
            self._reset_fetch()

        def _on_auth_toggled(self, _switch, _param) -> None:
            self._controller.set_auth_mode(
                "none" if self._auth_switch.get_active() else "bearer"
            )
            self._update_key_hint()
            self._reset_fetch()

        def _on_model_changed(self, _entry) -> None:
            self._controller.set_model_id(self._model_entry.get_text())
            self._set_field_error(self._model_error, None)
            self._update_model_hint()

        def _on_model_activate(self, _entry) -> None:
            """Enter in the model field re-opens the picker when a catalog is
            loaded (swarm audit F5: no other path re-opens it)."""
            if self._controller.has_catalog and not self._picker_revealer.get_reveal_child():
                self._picker_revealer.set_reveal_child(True)

        def _on_search_changed(self, _entry) -> None:
            self._filter.changed(Gtk.FilterChange.DIFFERENT)
            # Empty state for a search with no matches (swarm audit L1): a
            # blank list looked like the catalog was empty. Compute matches
            # synchronously from the store — FilterListModel re-evaluates on
            # its own timing, so reading its count here sees stale values.
            has_catalog = self._model_store.get_n_items() > 0
            query = self._search_entry.get_text().casefold()
            has_results = False
            if has_catalog:
                if not query:
                    has_results = True
                else:
                    for i in range(self._model_store.get_n_items()):
                        if self._filter_model(self._model_store.get_item(i), None):
                            has_results = True
                            break
            self._picker_empty.set_visible(has_catalog and not has_results)

        def _on_row_activated(self, _list_view, position: int) -> None:
            row = self._filtered.get_item(position)
            if row is None:
                return
            self._model_entry.set_text(row.model_id)
            self._picker_revealer.set_reveal_child(False)
            # The save button lives on the 连接 page; after picking a model
            # jump back there so the flow fetch -> select -> save is
            # unbroken (swarm audit M2).
            self.set_visible_page_name("connection")
            self._model_entry.grab_focus()

        def _on_picker_key(self, _controller, keyval: int, _keycode: int, _state: int) -> bool:
            if keyval == Gdk.KEY_Escape:
                self._picker_revealer.set_reveal_child(False)
                self._model_entry.grab_focus()
                return True
            return False

        def _on_fetch_clicked(self, _button) -> None:
            if self._busy:
                return
            if self._fetching:
                # 停止获取: advance the generation so the in-flight result is
                # dropped when it arrives. Actual network cancellation happens
                # through on_close — the fixed DialogCallbacks surface has no
                # per-fetch cancel callback (spec §7.1 minimum: discard).
                self._controller.begin_fetch()
                self._fetching = False
                self._update_fetch_ui()
                return
            error = self._controller.fetch_error()
            if error is not None:
                self._show_field_error(error)
                return
            self._controller.begin_fetch()
            self._fetching = True
            self._update_fetch_ui()
            self._clear_field_errors()
            self._hide_banner()
            # Drop the previous catalog immediately so a failed re-fetch can
            # never leave a stale list/status on screen (swarm audit F2).
            self._picker_revealer.set_reveal_child(False)
            self._fetch_status.set_visible(False)
            self._callbacks.on_fetch_models(self.draft)

        def _on_save_clicked(self, _button) -> None:
            if self._busy or self._fetching:
                return  # spec §7.3: no saving while a fetch is in flight
            error = self._controller.save_error()
            if error is not None:
                self._show_field_error(error)
                return
            self._clear_field_errors()
            self._hide_banner()
            self._set_busy(True)
            self._callbacks.on_save(self.draft)

        def _on_clear_clicked(self, _row) -> None:
            if self._busy or self._fetching:
                return
            alert = Adw.AlertDialog.new(
                "清除 AI 连接？",
                "将删除已保存的 API Key 与连接配置，此操作不可撤销。",
            )
            if self._theme_class:
                # Same surface-consistency as the dialog itself: the confirm
                # dialog should not fall back to libadwaita's near-black in
                # the dark themes.
                alert.add_css_class(self._theme_class)
            alert.add_response("cancel", "取消")
            alert.add_response("clear", "清除")
            alert.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
            alert.set_default_response("cancel")
            alert.set_close_response("cancel")
            alert.choose(self, None, self._on_clear_choice, None)

        def _on_clear_choice(self, alert: Adw.AlertDialog, result) -> None:
            if alert.choose_finish(result) != "clear":
                return
            self._set_busy(True)
            self._callbacks.on_clear()

        def _on_closed(self, _dialog) -> None:
            """Release the key reference and let the coordinator cancel any
            in-flight request (spec §10.2/§11.2)."""
            self._controller.clear_key()
            self._key_entry.set_text("")
            self._callbacks.on_close()

        # -- UI state helpers ------------------------------------------------

        def _reset_fetch(self) -> None:
            """A draft field change invalidates the in-flight fetch and the
            previously fetched catalog (spec §7.1)."""
            if self._fetching:
                self._fetching = False
                self._update_fetch_ui()
            self._hide_banner()
            self._picker_revealer.set_reveal_child(False)
            self._picker_empty.set_visible(False)
            self._fetch_status.set_visible(False)
            self._update_model_hint()

        def _update_fetch_ui(self) -> None:
            self._fetch_button.set_label("停止获取" if self._fetching else "获取模型")
            self._fetch_spinner.set_visible(self._fetching)
            self._save_button.set_sensitive(not self._busy and not self._fetching)

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            self._fetch_button.set_sensitive(not busy)
            self._clear_row.set_sensitive(not busy and self._has_profile)
            self._save_button.set_sensitive(not busy and not self._fetching)

        def _show_fetch_status(self, text: str, *, error: bool = False) -> None:
            """Set the model-page status line (near the 获取模型 button).

            ``error=True`` styles it as an error so a failed fetch is visible
            on the page where the fetch action lives (swarm audit F1/F2)."""
            self._fetch_status.set_label(text)
            self._fetch_status.set_error(error)
            self._fetch_status.set_visible(True)

        def _scroll_picker_into_view(self) -> None:
            """Scroll the dialog's internal scrolled window so the model
            picker sits near the bottom edge ("下拉条往底部靠一点").

            Pre-order walk targets the FIRST scrolled window whose content
            actually overflows the viewport — the dialog's own scroller. The
            picker's nested list scroller is deeper in the tree and would
            scroll the list internally instead.
            """

            def find_overflowing(widget: Gtk.Widget):
                if isinstance(widget, Gtk.ScrolledWindow):
                    adjustment = widget.get_vadjustment()
                    if adjustment is not None and adjustment.get_upper() > adjustment.get_page_size():
                        return adjustment
                child = widget.get_first_child()
                while child is not None:
                    found = find_overflowing(child)
                    if found is not None:
                        return found
                    child = child.get_next_sibling()
                return None

            adjustment = find_overflowing(self)
            if adjustment is not None:
                adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())

        def _sync_catalog(self) -> None:
            result = self._controller.last_result
            if result is None:
                return
            self._model_store.remove_all()
            for model in result.models:
                self._model_store.append(_ModelRow(model))
            self._search_entry.set_text("")
            self._picker_empty.set_visible(False)
            self._filter.changed(Gtk.FilterChange.DIFFERENT)
            if result.models:
                self._show_fetch_status(f"共 {len(result.models)} 个模型")
                self._picker_revealer.set_reveal_child(True)
                # Scroll the dialog so the list sits near the bottom edge
                # ("下拉条往底部靠一点"). A timeout lets the revealer
                # transition finish, otherwise the dialog's scroll range is
                # still the pre-reveal size and the scroll is a no-op.
                GLib.timeout_add(400, self._scroll_picker_into_view)
            else:
                self._show_fetch_status(MODELS_EMPTY_MESSAGE)
                self._picker_revealer.set_reveal_child(False)
            self._update_model_hint()

        def _sync_auth_switch(self) -> None:
            available = self._controller.auth_switch_available()
            self._auth_switch.set_sensitive(available)
            if not available and self._auth_switch.get_active():
                # remote/invalid base forces bearer; set_active triggers the
                # notify handler which updates the controller
                self._auth_switch.set_active(False)

        def _update_key_hint(self) -> None:
            hint = self._controller.key_hint
            self._key_hint.set_label(hint)
            self._key_hint.set_visible(bool(hint))

        def _update_model_hint(self) -> None:
            hint = self._controller.model_hint
            self._model_hint.set_label(hint or "")
            self._model_hint.set_visible(bool(hint))

        def _show_field_error(self, message: str) -> None:
            if message in (
                MSG_BASE_URL_INVALID,
                MSG_BASE_URL_INSECURE,
                MSG_FETCH_MISSING_BASE,
                MSG_FETCH_MISSING_BOTH,
            ):
                # The field lives on the 连接 page; jump there so the error
                # is actually visible next to the failing field (swarm audit
                # H3 — the fetch button is on the 模型 page and validation
                # errors used to appear on an invisible page).
                self.set_visible_page_name("connection")
                self._set_field_error(self._base_error, message)
            elif message in (MSG_FETCH_MISSING_KEY, MSG_KEY_INVALID):
                self.set_visible_page_name("connection")
                self._set_field_error(self._key_error, message)
            elif message in (MSG_MODELS_URL_CROSS_ORIGIN, MSG_MODELS_URL_INVALID):
                self.set_visible_page_name("model")
                self._models_error_row.set_title(message)
                self._models_error_row.set_visible(True)
                # The error row lives inside the collapsed "模型列表地址"
                # expander; open it so the message is actually visible
                # (swarm audit F4).
                self._advanced.set_expanded(True)
            elif message == MSG_MODEL_ID_INVALID:
                self.set_visible_page_name("model")
                self._set_field_error(self._model_error, message)
            else:
                self._show_banner(message)

        def _clear_field_errors(self) -> None:
            for label in (self._base_error, self._key_error, self._model_error):
                self._set_field_error(label, None)
            self._models_error_row.set_visible(False)

        def _show_banner(self, message: str) -> None:
            self._banner.set_title(message)
            self._banner.set_revealed(True)

        def _hide_banner(self) -> None:
            self._banner.set_revealed(False)

        @staticmethod
        def _new_field_label(*css_classes: str) -> "_FieldLabelRow":
            # A bare Gtk.Label child of a PreferencesGroup does not receive a
            # proper row allocation, so with large text (200% scaling) the
            # hint overlaps the following row. Wrapping the label in an
            # Adw.PreferencesRow gives it correct height and spacing.
            return _FieldLabelRow(*css_classes)

        @staticmethod
        def _set_placeholder(entry_row, text: str) -> None:
            """Set placeholder text on the Gtk.Entry nested inside an
            Adw.EntryRow (the row itself only exposes a title)."""

            def walk(widget: Gtk.Widget) -> bool:
                if isinstance(widget, Gtk.Entry):
                    widget.set_placeholder_text(text)
                    return True
                child = widget.get_first_child()
                while child is not None:
                    if walk(child):
                        return True
                    child = child.get_next_sibling()
                return False

            walk(entry_row)

        @staticmethod
        def _set_field_error(label: Gtk.Label, message: str | None) -> None:
            if message:
                label.set_label(message)
                label.set_visible(True)
            else:
                label.set_label("")
                label.set_visible(False)

        def _filter_model(self, item: _ModelRow, _data: object) -> bool:
            query = self._search_entry.get_text().casefold()
            if not query:
                return True
            return query in item.model_id.casefold()

else:

    class AiConnectionDialog:
        """GTK placeholder used only when the Adw/Gtk typelibs are missing.

        The module stays importable and the controller stays testable headless;
        only this class is unavailable (spec §11.1 optional-runtime
        degradation)."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("AiConnectionDialog requires the Adw/Gtk typelibs")
