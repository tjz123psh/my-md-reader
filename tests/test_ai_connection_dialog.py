"""AI connection dialog controller tests (docs/LLM_PROVIDER_MIGRATION_SPEC.md §7.1/§7.3/§7.4/§9.3/§10.2/§13.7).

Headless by design: only the pure-logic ``ConnectionDialogController`` is
exercised, never a GTK dialog instance. The real widget path belongs to the
process-level GTK smoke (spec §13.8). This file is the Phase 5 red test: the
target module does not exist yet, so every test currently fails with
ImportError.
"""

from __future__ import annotations

import inspect
import unittest

from mdreader.models import AiConnectionDraft, AiError, AiErrorCode, AiModel, AiProfile
from mdreader.services.ai_models import ModelCatalogResult, draft_fingerprint
from mdreader.widgets.ai_connection_dialog import (
    GTK_AVAILABLE,
    KEY_SOURCE_NEW,
    KEY_SOURCE_SAVED,
    KEY_SOURCE_NONE,
    MODELS_EMPTY_MESSAGE,
    MODELS_ERROR_MESSAGES,
    MSG_FETCH_MISSING_KEY,
    MSG_MODEL_ID_INVALID,
    AiConnectionDialog,
    ConnectionDialogController,
    error_message,
)

if GTK_AVAILABLE:
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib  # noqa: E402

PROFILE = AiProfile(
    profile_id="11111111-1111-1111-1111-111111111111",
    provider_kind="openai-compatible",
    api_base_url="https://api.example.com/v1",
    models_url="",
    model_id="gpt-4o",
    auth_mode="bearer",
)

LOOPBACK = "http://127.0.0.1:8000/v1"

FETCH_MISSING_BOTH = "请先填写 API 地址和密钥"
FETCH_MISSING_BASE = "请填写 API 基础地址"
FETCH_MISSING_KEY = "请填写 API Key"
HINT_SAVED = "留空则继续使用已保存密钥"
HINT_RETYPE = "请重新输入 API Key"
HINT_NONE = "无需鉴权，无需输入 API Key"
MODELS_URL_CROSS_ORIGIN = "模型列表地址必须与 API 地址同源"
MODELS_URL_INVALID = "模型列表地址无效"
MODEL_ID_INVALID = "模型 ID 无效"
KEY_INVALID = "API Key 无效，请检查格式"


def controller(profile=PROFILE, **fields) -> ConnectionDialogController:
    c = ConnectionDialogController(profile)
    for name, value in fields.items():
        getattr(c, f"set_{name}")(value)
    return c


def result(*ids: str) -> ModelCatalogResult:
    return ModelCatalogResult(models=tuple(AiModel(model_id=i) for i in ids))


class BaseUrlValidationTests(unittest.TestCase):
    def test_valid_https_has_no_error(self) -> None:
        self.assertIsNone(
            controller(api_base_url="https://api.example.com/v1").base_url_error()
        )

    def test_loopback_http_has_no_error(self) -> None:
        self.assertIsNone(controller(api_base_url=LOOPBACK).base_url_error())

    def test_malformed_urls_map_to_invalid_message(self) -> None:
        for raw in (
            "not a url",
            "https://",
            "https://example.com/v1?q=1",
            "https://user:pw@example.com/v1",
            "https://example.com\\path",
            "https://[::1%25eth0]/v1",
        ):
            with self.subTest(raw=raw):
                error = controller(api_base_url=raw).base_url_error()
                self.assertIsNotNone(error)
                self.assertIn("API 地址无效", error)

    def test_insecure_remote_http_maps_to_https_message(self) -> None:
        error = controller(api_base_url="http://api.example.com/v1").base_url_error()
        self.assertIn("必须使用 HTTPS", error)

    def test_empty_base_url_has_no_field_error(self) -> None:
        # missing input is reported by fetch_error, not as a malformed URL
        self.assertIsNone(controller(api_base_url="").base_url_error())


class AuthModeTests(unittest.TestCase):
    def test_remote_none_forced_back_to_bearer(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", auth_mode="none")
        self.assertEqual(c.effective_auth_mode, "bearer")
        self.assertFalse(c.auth_switch_available())

    def test_loopback_explicit_none_allowed(self) -> None:
        c = controller(api_base_url=LOOPBACK, auth_mode="none")
        self.assertEqual(c.effective_auth_mode, "none")
        self.assertTrue(c.auth_switch_available())

    def test_loopback_none_needs_no_key(self) -> None:
        c = controller(api_base_url=LOOPBACK, auth_mode="none")
        self.assertEqual(c.key_source, KEY_SOURCE_NONE)
        self.assertEqual(c.key_hint, HINT_NONE)
        self.assertIsNone(c.key_error())

    def test_remote_bearer_blank_same_origin_reuses_saved_key(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", auth_mode="bearer")
        self.assertEqual(c.key_source, KEY_SOURCE_SAVED)
        self.assertEqual(c.key_hint, HINT_SAVED)

    def test_remote_bearer_blank_cross_origin_requires_new_key(self) -> None:
        c = controller(api_base_url="https://other.example.com/v1", auth_mode="bearer")
        self.assertEqual(c.key_source, KEY_SOURCE_NONE)
        self.assertEqual(c.key_hint, HINT_RETYPE)

    def test_typed_key_is_new_key_source(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        self.assertEqual(c.key_source, KEY_SOURCE_NEW)

    def test_blank_key_without_profile_requires_retype(self) -> None:
        c = controller(None, api_base_url="https://api.example.com/v1")
        self.assertEqual(c.key_hint, HINT_RETYPE)

    def test_saved_none_profile_never_reuses_a_key(self) -> None:
        none_profile = AiProfile(
            profile_id="22222222-2222-2222-2222-222222222222",
            provider_kind="openai-compatible",
            api_base_url=LOOPBACK,
            models_url="",
            model_id="local-model",
            auth_mode="none",
        )
        c = controller(none_profile, api_base_url=LOOPBACK)
        self.assertEqual(c.key_source, KEY_SOURCE_NONE)


class ModelsUrlValidationTests(unittest.TestCase):
    def test_cross_origin_models_url_rejected(self) -> None:
        c = controller(
            api_base_url="https://api.example.com/v1",
            models_url="https://other.example.com/v1/models",
        )
        self.assertEqual(c.models_url_error(), MODELS_URL_CROSS_ORIGIN)

    def test_same_origin_models_url_accepted(self) -> None:
        c = controller(
            api_base_url="https://api.example.com/v1",
            models_url="https://api.example.com/v1/custom-models",
        )
        self.assertIsNone(c.models_url_error())

    def test_empty_models_url_uses_default(self) -> None:
        self.assertIsNone(controller(api_base_url="https://api.example.com/v1").models_url_error())

    def test_malformed_models_url_rejected(self) -> None:
        c = controller(
            api_base_url="https://api.example.com/v1",
            models_url="not a url",
        )
        self.assertEqual(c.models_url_error(), MODELS_URL_INVALID)

    def test_models_url_error_deferred_while_base_invalid(self) -> None:
        c = controller(api_base_url="broken", models_url="https://api.example.com/x")
        self.assertIsNone(c.models_url_error())


class ModelIdValidationTests(unittest.TestCase):
    def test_valid_model_id_accepted(self) -> None:
        self.assertIsNone(controller(model_id="gpt-4o").model_id_error())

    def test_whitespace_model_id_rejected(self) -> None:
        self.assertEqual(controller(model_id="gpt 4o").model_id_error(), MODEL_ID_INVALID)

    def test_control_char_model_id_rejected(self) -> None:
        self.assertEqual(controller(model_id="gpt\x00").model_id_error(), MODEL_ID_INVALID)

    def test_empty_model_id_allowed(self) -> None:
        self.assertIsNone(controller(model_id="").model_id_error())


class KeyValidationTests(unittest.TestCase):
    def test_remote_bearer_missing_key(self) -> None:
        c = controller(api_base_url="https://other.example.com/v1")
        self.assertEqual(c.key_error(), FETCH_MISSING_KEY)

    def test_malformed_key_rejected_without_rewriting(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key=" sk-test")
        self.assertEqual(c.key_error(), KEY_INVALID)
        self.assertEqual(c.api_key, " sk-test")  # never trimmed silently

    def test_valid_key_accepted(self) -> None:
        self.assertIsNone(controller(api_base_url="https://api.example.com/v1", key="sk-test").key_error())

    def test_blank_same_origin_key_is_not_an_error(self) -> None:
        self.assertIsNone(controller(api_base_url="https://api.example.com/v1").key_error())


class FetchValidationTests(unittest.TestCase):
    def test_both_address_and_key_missing(self) -> None:
        c = controller(None, api_base_url="")
        self.assertEqual(c.fetch_error(), FETCH_MISSING_BOTH)

    def test_only_address_missing(self) -> None:
        c = controller(None, api_base_url="", key="sk-test")
        self.assertEqual(c.fetch_error(), FETCH_MISSING_BASE)

    def test_remote_key_missing(self) -> None:
        c = controller(None, api_base_url="https://api.example.com/v1")
        self.assertEqual(c.fetch_error(), FETCH_MISSING_KEY)

    def test_loopback_none_needs_no_key(self) -> None:
        c = controller(None, api_base_url=LOOPBACK, auth_mode="none")
        self.assertIsNone(c.fetch_error())

    def test_insecure_remote_url_blocks_fetch(self) -> None:
        c = controller(None, api_base_url="http://api.example.com/v1", key="sk-test")
        self.assertIn("必须使用 HTTPS", c.fetch_error())

    def test_cross_origin_models_url_blocks_fetch(self) -> None:
        c = controller(
            api_base_url="https://api.example.com/v1",
            key="sk-test",
            models_url="https://other.example.com/v1/models",
        )
        self.assertEqual(c.fetch_error(), MODELS_URL_CROSS_ORIGIN)

    def test_valid_draft_allows_fetch(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        self.assertIsNone(c.fetch_error())


class SaveValidationTests(unittest.TestCase):
    def test_save_requires_valid_model_id(self) -> None:
        c = controller(
            api_base_url="https://api.example.com/v1",
            key="sk-test",
            model_id="bad id",
        )
        self.assertEqual(c.save_error(), MODEL_ID_INVALID)

    def test_save_allows_empty_model_id(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        self.assertIsNone(c.save_error())

    def test_save_rejects_missing_key_on_cross_origin(self) -> None:
        c = controller(api_base_url="https://other.example.com/v1", model_id="m")
        self.assertEqual(c.save_error(), FETCH_MISSING_KEY)


class ErrorCopyTests(unittest.TestCase):
    def test_models_error_table_covers_spec_rows(self) -> None:
        expected = {
            AiErrorCode.AUTHENTICATION_FAILED: "API Key 无效或无权读取模型列表",
            AiErrorCode.PERMISSION_DENIED: "API Key 无效或无权读取模型列表",
            AiErrorCode.ENDPOINT_NOT_FOUND: "此地址没有可用的模型列表接口，可检查地址或手动填写模型",
            AiErrorCode.BILLING_OR_QUOTA_REQUIRED: "账户额度或计费状态不允许请求",
            AiErrorCode.RATE_LIMITED: "请求过于频繁，请稍后重试",
            AiErrorCode.PROVIDER_UNAVAILABLE: "AI 服务暂时不可用，请稍后重试",
            AiErrorCode.TIMEOUT: "获取模型超时，请检查网络和服务地址",
            AiErrorCode.TLS_FAILED: "无法验证服务端安全连接",
            AiErrorCode.INVALID_RESPONSE: "服务返回的模型列表格式不兼容",
        }
        for code, copy in expected.items():
            with self.subTest(code=code):
                self.assertEqual(error_message(code), copy)

    def test_every_table_entry_maps_through_error_message(self) -> None:
        for code, copy in MODELS_ERROR_MESSAGES.items():
            with self.subTest(code=code):
                self.assertEqual(error_message(code), copy)

    def test_cancelled_maps_to_no_message(self) -> None:
        self.assertIsNone(error_message(AiErrorCode.CANCELLED))

    def test_unknown_code_gets_fallback(self) -> None:
        self.assertEqual(error_message("NO_SUCH_CODE"), "请求失败，请稍后重试")

    def test_empty_catalog_message(self) -> None:
        self.assertEqual(MODELS_EMPTY_MESSAGE, "服务返回了 0 个模型，可手动填写模型 ID")

    def test_chat_context_distinguishes_auth_failures(self) -> None:
        self.assertEqual(
            error_message(AiErrorCode.AUTHENTICATION_FAILED, context="chat"),
            "API Key 认证失败，请检查密钥",
        )
        self.assertEqual(
            error_message(AiErrorCode.PERMISSION_DENIED, context="chat"),
            "当前密钥无权访问此服务",
        )

    def test_chat_context_never_reuses_models_fetch_copy(self) -> None:
        """Regression (swarm audit M1): asking-time failures used the
        /models fetch copy ("获取模型超时…"), misleading the user."""
        cases = {
            AiErrorCode.TIMEOUT: "提问超时",
            AiErrorCode.ENDPOINT_NOT_FOUND: "对话接口",
            AiErrorCode.INVALID_RESPONSE: "对话内容格式",
            AiErrorCode.NETWORK_FAILED: "网络连接失败",
        }
        for code, needle in cases.items():
            with self.subTest(code=code):
                copy = error_message(code, context="chat")
                self.assertIn(needle, copy)
                self.assertNotIn("获取模型", copy)


class ModelFilterTests(unittest.TestCase):
    MODELS = (
        AiModel("gpt-4o", "openai"),
        AiModel("GPT-4o-mini", "openai"),
        AiModel("llama-3.1", "meta"),
    )

    def test_case_insensitive_substring(self) -> None:
        found = ConnectionDialogController.filter_models(self.MODELS, "GPT-4O")
        self.assertEqual([m.model_id for m in found], ["gpt-4o", "GPT-4o-mini"])

    def test_empty_query_returns_all(self) -> None:
        self.assertEqual(ConnectionDialogController.filter_models(self.MODELS, ""), self.MODELS)

    def test_no_match_returns_empty(self) -> None:
        self.assertEqual(ConnectionDialogController.filter_models(self.MODELS, "claude"), ())

    def test_original_ids_and_objects_are_preserved(self) -> None:
        found = ConnectionDialogController.filter_models(self.MODELS, "gpt")
        self.assertEqual([m.model_id for m in found], ["gpt-4o", "GPT-4o-mini"])
        self.assertEqual(found[0].owned_by, "openai")


class FingerprintTests(unittest.TestCase):
    def test_identical_state_identical_fingerprint(self) -> None:
        a = controller(api_base_url="https://api.example.com/v1", key="sk-aaa")
        b = controller(api_base_url="https://api.example.com/v1", key="sk-bbb")
        # same revision and key_source, different secret value: equal
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_each_field_change_changes_fingerprint(self) -> None:
        cases = (
            ("api_base_url", "https://other.example.com/v1"),
            ("models_url", "https://api.example.com/v1/custom-models"),
            ("auth_mode", "none"),
            ("key", "sk-other"),
        )
        base = controller(api_base_url="https://api.example.com/v1", key="sk-test").fingerprint
        for setter, value in cases:
            with self.subTest(setter=setter):
                changed = controller(api_base_url="https://api.example.com/v1", key="sk-test")
                getattr(changed, f"set_{setter}")(value)
                self.assertNotEqual(base, changed.fingerprint)

    def test_model_selection_does_not_change_fingerprint(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        before = c.fingerprint
        c.set_model_id("llama-3.1")
        self.assertEqual(before, c.fingerprint)

    def test_fingerprint_function_has_no_secret_parameter(self) -> None:
        # the fingerprint path must never accept the key (spec §7.1)
        params = inspect.signature(draft_fingerprint).parameters
        self.assertNotIn("secret", params)
        self.assertNotIn("api_key", params)


class StaleResultTests(unittest.TestCase):
    def test_fetch_result_applied_with_current_generation(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        gen = c.begin_fetch()
        self.assertTrue(c.apply_fetch_result(result("gpt-4o"), None, generation=gen))
        self.assertTrue(c.has_catalog)
        self.assertEqual([m.model_id for m in c.last_result.models], ["gpt-4o"])

    def test_field_change_drops_old_generation_result(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        gen = c.begin_fetch()
        c.set_api_base_url("https://other.example.com/v1")
        self.assertFalse(c.apply_fetch_result(result("gpt-4o"), None, generation=gen))
        self.assertFalse(c.has_catalog)

    def test_second_fetch_drops_first_in_flight_result(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        first = c.begin_fetch()
        second = c.begin_fetch()
        self.assertNotEqual(first, second)
        self.assertFalse(c.apply_fetch_result(result("old"), None, generation=first))
        self.assertTrue(c.apply_fetch_result(result("new"), None, generation=second))
        self.assertEqual([m.model_id for m in c.last_result.models], ["new"])

    def test_stale_catalog_selection_degrades_to_unverified_manual_id(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        gen = c.begin_fetch()
        c.apply_fetch_result(result("gpt-4o"), None, generation=gen)
        self.assertIsNone(c.model_hint)  # verified by the fresh catalog
        c.set_api_base_url("https://other.example.com/v1")  # stale the catalog
        self.assertFalse(c.has_catalog)
        self.assertEqual(c.model_id, "gpt-4o")  # kept, never silently switched
        self.assertEqual(c.model_hint, "未验证的手动 ID")

    def test_saved_model_missing_from_fresh_result(self) -> None:
        c = controller(None, api_base_url="https://api.example.com/v1", key="sk-test")
        gen = c.begin_fetch()
        c.apply_fetch_result(result("other-model"), None, generation=gen)
        c.set_model_id("gpt-4o")
        self.assertEqual(c.model_hint, "此模型未出现在本次结果中")

    def test_fetch_error_never_exposes_a_catalog(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        gen = c.begin_fetch()
        c.apply_fetch_result(None, AiError(AiErrorCode.RATE_LIMITED), generation=gen)
        self.assertFalse(c.has_catalog)
        self.assertIsNotNone(c.last_error)

    def test_cancelled_result_has_no_error_message(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        gen = c.begin_fetch()
        c.apply_fetch_result(None, AiError(AiErrorCode.CANCELLED), generation=gen)
        self.assertIsNone(error_message(c.last_error.code))

    def test_empty_catalog_is_success_not_error(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        gen = c.begin_fetch()
        c.apply_fetch_result(result(), None, generation=gen)
        self.assertTrue(c.has_catalog)
        self.assertTrue(c.catalog_empty)


class DraftTests(unittest.TestCase):
    def test_draft_carries_typed_key(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        draft = c.build_draft()
        self.assertIsInstance(draft, AiConnectionDraft)
        self.assertEqual(draft.api_key, "sk-test")
        self.assertFalse(draft.keep_existing_secret)
        self.assertEqual(draft.auth_mode, "bearer")

    def test_blank_same_origin_draft_reuses_saved_secret(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1")
        draft = c.build_draft()
        self.assertEqual(draft.api_key, "")
        self.assertTrue(draft.keep_existing_secret)

    def test_blank_cross_origin_draft_never_reuses(self) -> None:
        c = controller(api_base_url="https://other.example.com/v1")
        draft = c.build_draft()
        self.assertFalse(draft.keep_existing_secret)

    def test_loopback_none_draft_has_no_key(self) -> None:
        c = controller(api_base_url=LOOPBACK, auth_mode="none")
        draft = c.build_draft()
        self.assertEqual(draft.api_key, "")
        self.assertEqual(draft.auth_mode, "none")
        self.assertFalse(draft.keep_existing_secret)

    def test_draft_normalizes_base_url(self) -> None:
        c = controller(api_base_url="  https://api.example.com/v1/  ")
        draft = c.build_draft()
        self.assertEqual(draft.api_base_url, "https://api.example.com/v1")

    def test_draft_models_url_normalized_when_valid(self) -> None:
        c = controller(
            api_base_url="https://api.example.com/v1",
            models_url="https://api.example.com/v1/custom-models",
        )
        draft = c.build_draft()
        self.assertEqual(draft.models_url, "https://api.example.com/v1/custom-models")

    def test_draft_total_for_invalid_base(self) -> None:
        c = controller(None, api_base_url="broken")
        draft = c.build_draft()
        self.assertEqual(draft.api_base_url, "broken")
        self.assertEqual(draft.auth_mode, "bearer")


class KeyReleaseTests(unittest.TestCase):
    def test_clear_key_releases_reference(self) -> None:
        c = controller(api_base_url="https://api.example.com/v1", key="sk-test")
        c.clear_key()
        self.assertEqual(c.api_key, "")


@unittest.skipUnless(GTK_AVAILABLE, "Adw/Gtk typelibs not available")
class DialogInterfaceTests(unittest.TestCase):
    def test_dialog_exposes_fixed_public_api(self) -> None:
        for name in ("set_fetch_result", "set_save_result", "set_clear_result"):
            self.assertTrue(callable(getattr(AiConnectionDialog, name)), name)
        for name in ("draft", "fingerprint", "selected_model"):
            self.assertIsInstance(getattr(AiConnectionDialog, name, None), property, name)
        for name in ("on_fetch_models", "on_save", "on_clear", "on_close"):
            self.assertTrue(callable(getattr(__import__(
                "mdreader.widgets.ai_connection_dialog",
                fromlist=["DialogCallbacks"],
            ).DialogCallbacks, name)), name)


@unittest.skipUnless(GTK_AVAILABLE, "Adw/Gtk typelibs not available")
class DialogCatalogRenderTests(unittest.TestCase):
    """Widget-level: a fetch result must render through _sync_catalog.

    Regression (2026-08-13): the 200% field-label refactor replaced the
    status Gtk.Label with _FieldLabelRow (which exposes set_label/get_label,
    not the Gtk.Label set_text API), but _sync_catalog still called
    set_text — so a successful fetch crashed before the model list could
    show ("获取模型什么东西都出不来"). These tests construct the real dialog
    and feed a fetch result through the public set_fetch_result path.
    """

    def setUp(self) -> None:
        try:
            import gi

            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk

            Gtk.init()
        except Exception as exc:  # no display (headless CI)
            self._gtk_skip = f"no GTK display: {exc}"
        else:
            self._gtk_skip = None

    def _make_dialog(self) -> "AiConnectionDialog":
        class Callbacks:
            def on_fetch_models(self, draft): ...
            def on_save(self, draft): ...
            def on_clear(self): ...
            def on_close(self): ...

        return AiConnectionDialog(profile=None, callbacks=Callbacks())

    def test_dialog_accepts_theme_class(self) -> None:
        """The dialog must carry the active reading theme's surface class so
        the generated theme CSS (dialog.theme-<id>) can paint it with the
        theme palette (regression: the dialog fell back to libadwaita's
        near-black background in the dark themes)."""
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)

        class Callbacks:
            def on_fetch_models(self, draft): ...
            def on_save(self, draft): ...
            def on_clear(self): ...
            def on_close(self): ...

        themed = AiConnectionDialog(
            profile=None, callbacks=Callbacks(), theme_class="theme-midnight-ink"
        )
        self.assertTrue(themed.has_css_class("theme-midnight-ink"))
        # a dialog without a theme class stays un-themed
        plain = AiConnectionDialog(profile=None, callbacks=Callbacks())
        self.assertFalse(plain.has_css_class("theme-midnight-ink"))

    def test_field_error_switches_to_the_failing_page(self) -> None:
        """Regression (swarm audit H3): fetch validation errors lived on the
        connection page while the fetch button is on the model page — the
        error was invisible. The dialog must jump to the failing field."""
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        dialog = self._make_dialog()
        dialog.set_visible_page_name("model")
        dialog._show_field_error(MSG_FETCH_MISSING_KEY)
        self.assertEqual(dialog.get_visible_page_name(), "connection")
        dialog._show_field_error(MSG_MODEL_ID_INVALID)
        self.assertEqual(dialog.get_visible_page_name(), "model")

    def test_selecting_a_model_returns_to_the_connection_page(self) -> None:
        """Regression (swarm audit M2): after picking a model from the list
        the save button (connection page) was out of reach; the dialog must
        jump back so fetch -> select -> save stays unbroken."""
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        dialog = self._make_dialog()
        dialog.set_fetch_result(
            ModelCatalogResult(models=(AiModel("model-a"), AiModel("model-b"))),
            None,
            generation=dialog.generation,
        )
        dialog._on_row_activated(None, 0)
        self.assertEqual(dialog.get_visible_page_name(), "connection")
        self.assertEqual(dialog._model_entry.get_text(), "model-a")

    def test_search_without_matches_shows_empty_hint(self) -> None:
        """Regression (swarm audit L1): an empty search result looked like an
        empty catalog; a "无匹配模型" hint must appear."""
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        dialog = self._make_dialog()
        dialog.set_fetch_result(
            ModelCatalogResult(models=(AiModel("model-a"), AiModel("model-b"))),
            None,
            generation=dialog.generation,
        )
        # Drive the handler directly: Gtk.SearchEntry only emits
        # "search-changed" for a focused entry, and grab_focus does not
        # take on a standalone (unparented-window) widget. The signal wiring
        # itself is standard GTK; the behavior under test is the handler's
        # empty-hint logic.
        dialog._search_entry.set_text("zzz")
        dialog._on_search_changed(dialog._search_entry)
        self.assertTrue(dialog._picker_empty.get_visible())
        dialog._search_entry.set_text("")
        dialog._on_search_changed(dialog._search_entry)
        self.assertFalse(dialog._picker_empty.get_visible())

    def test_fetch_result_renders_catalog(self) -> None:
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        dialog = self._make_dialog()
        result = ModelCatalogResult(models=(AiModel("model-a"), AiModel("model-b")))
        dialog.set_fetch_result(result, None, generation=dialog.generation)
        # White-box: _fetch_status/_model_store/_picker_revealer are the
        # widgets _sync_catalog drives; asserting their state proves the
        # render path completed instead of crashing on the label API.
        self.assertEqual(dialog._fetch_status.get_label(), "共 2 个模型")
        self.assertTrue(dialog._fetch_status.get_visible())
        self.assertEqual(dialog._model_store.get_n_items(), 2)
        self.assertTrue(dialog._picker_revealer.get_reveal_child())

    def test_fetch_empty_catalog_renders_message(self) -> None:
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        dialog = self._make_dialog()
        dialog.set_fetch_result(
            ModelCatalogResult(models=()), None, generation=dialog.generation
        )
        self.assertEqual(dialog._fetch_status.get_label(), MODELS_EMPTY_MESSAGE)
        self.assertTrue(dialog._fetch_status.get_visible())
        self.assertEqual(dialog._model_store.get_n_items(), 0)
        self.assertFalse(dialog._picker_revealer.get_reveal_child())

    def test_fetch_error_shows_banner(self) -> None:
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        dialog = self._make_dialog()
        error = AiError(AiErrorCode.AUTHENTICATION_FAILED, "test")
        dialog.set_fetch_result(None, error, generation=dialog.generation)
        self.assertTrue(dialog._banner.get_revealed())
        self.assertIn("API Key", dialog._banner.get_title())

    def test_stale_fetch_result_is_dropped(self) -> None:
        if self._gtk_skip:
            self.skipTest(self._gtk_skip)
        dialog = self._make_dialog()
        stale_generation = dialog.generation - 1
        result = ModelCatalogResult(models=(AiModel("model-a"),))
        dialog.set_fetch_result(result, None, generation=stale_generation)
        self.assertIsNone(dialog._controller.last_result)
        self.assertEqual(dialog._model_store.get_n_items(), 0)
