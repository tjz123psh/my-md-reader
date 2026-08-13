from __future__ import annotations

import unittest

import gi

gi.require_version("Gio", "2.0")
from gi.repository import GLib

from mdreader.services.settings import AI_PROFILE_FIELDS, SettingsStore

# Spec §13.7 sentinel: must never appear in persisted metadata or logs.
SENTINEL = "sk-mdreader-test-secret-never-log-7d9f"

# The exact six non-secret metadata fields mandated by spec §9.1.
EXPECTED_FIELDS = frozenset(
    {
        "profile-id",
        "provider-kind",
        "api-base-url",
        "models-url",
        "model-id",
        "auth-mode",
    }
)

PROFILE = {
    "profile-id": "6e8c0a1b-9f00-4c1a-8b2d-000000000001",
    "provider-kind": "openai-compatible",
    "api-base-url": "https://example.invalid/v1",
    "models-url": "",
    "model-id": "model-a",
    "auth-mode": "bearer",
}


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SettingsStore()
        # Unit tests must not write the developer's real dconf database when
        # the build-tree schema is available in the test environment.
        self.settings._settings = None

    def test_sidebar_widths_are_clamped(self) -> None:
        self.settings.set_sidebar_width("library-sidebar-width", 120)
        self.assertEqual(self.settings.get_sidebar_width("library-sidebar-width", 260), 180)
        self.settings.set_sidebar_width("ai-sidebar-width", 999)
        self.assertEqual(self.settings.get_sidebar_width("ai-sidebar-width", 360), 720)

    def test_ai_profile_unset_returns_none(self) -> None:
        self.assertIsNone(self.settings.get_ai_profile())

    def test_ai_profile_set_returns_true_and_roundtrips(self) -> None:
        self.assertTrue(self.settings.set_ai_profile(dict(PROFILE)))
        self.assertEqual(self.settings.get_ai_profile(), PROFILE)

    def test_ai_profile_clear_returns_none_afterwards(self) -> None:
        self.settings.set_ai_profile(dict(PROFILE))
        self.assertTrue(self.settings.clear_ai_profile())
        self.assertIsNone(self.settings.get_ai_profile())

    def test_ai_profile_filters_to_spec_fields_only(self) -> None:
        self.assertEqual(AI_PROFILE_FIELDS, EXPECTED_FIELDS)
        dirty = {
            "profile-id": "u-1",
            "provider-kind": "openai-compatible",
            "api-base-url": "https://example.invalid/v1",
            "models-url": "",
            "model-id": "model-a",
            "auth-mode": "bearer",
            "api-key": SENTINEL,
            "temperature": "0.7",
            "extra": "dropped",
        }
        self.assertTrue(self.settings.set_ai_profile(dirty))
        stored = self.settings.get_ai_profile()
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(set(stored), EXPECTED_FIELDS)
        self.assertNotIn("api-key", stored)
        self.assertNotIn("temperature", stored)
        self.assertNotIn("extra", stored)

    def test_ai_profile_ignores_non_string_values(self) -> None:
        self.assertTrue(
            self.settings.set_ai_profile({"profile-id": "u-1", "auth-mode": 123})
        )
        self.assertEqual(self.settings.get_ai_profile(), {"profile-id": "u-1"})

    def test_opencode_model_legacy_key_still_readable(self) -> None:
        # Legacy key remains readable and is never written by ai-profile paths.
        self.assertEqual(self.settings.get_string("opencode-model"), "")
        self.settings.set_string("opencode-model", "legacy-model")
        self.assertTrue(self.settings.set_ai_profile(dict(PROFILE)))
        self.assertEqual(self.settings.get_string("opencode-model"), "legacy-model")
        self.assertIsNotNone(self.settings.get_ai_profile())

    def test_sentinel_secret_never_enters_ai_profile(self) -> None:
        self.assertTrue(
            self.settings.set_ai_profile(
                {"api-key": SENTINEL, "api_base_url": SENTINEL, "profile-id": "u-1"}
            )
        )
        dumped = self.settings.get_ai_profile()
        self.assertIsNotNone(dumped)
        assert dumped is not None
        self.assertNotIn("api-key", dumped)
        self.assertNotIn("api_base_url", dumped)
        self.assertNotIn(SENTINEL, str(dumped))
        self.assertNotIn(SENTINEL, str(self.settings._memory))

    def test_ai_profile_real_mode_uses_single_set_value(self) -> None:
        class FakeSettings:
            """Mimics the Gio.Settings subset the facade relies on."""

            def __init__(self) -> None:
                self.stored: dict[str, str] = {}
                self.result = True
                self.set_value_calls = 0

            def get_value(self, key: str):
                assert key == "ai-profile"
                return GLib.Variant("a{ss}", self.stored)

            def set_value(self, key: str, variant) -> bool:
                assert key == "ai-profile"
                self.set_value_calls += 1
                if self.result:
                    self.stored = variant.unpack()
                return self.result

        settings = SettingsStore()
        settings._settings = FakeSettings()
        # Unknown fields are filtered before the single set_value write.
        self.assertTrue(
            settings.set_ai_profile({"profile-id": "u-1", "extra": "x"})
        )
        self.assertEqual(settings._settings.set_value_calls, 1)
        self.assertEqual(settings.get_ai_profile(), {"profile-id": "u-1"})
        self.assertTrue(settings.set_ai_profile({"profile-id": "u-2"}))
        self.assertEqual(settings.get_ai_profile(), {"profile-id": "u-2"})
        # The underlying boolean result is propagated verbatim.
        settings._settings.result = False
        self.assertFalse(settings.set_ai_profile({"profile-id": "u-3"}))
        self.assertEqual(settings.get_ai_profile(), {"profile-id": "u-2"})
        settings._settings.result = True
        self.assertTrue(settings.clear_ai_profile())
        self.assertIsNone(settings.get_ai_profile())


if __name__ == "__main__":
    unittest.main()
