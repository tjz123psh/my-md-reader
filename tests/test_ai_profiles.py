"""AiProfileStore transactional save/clear tests (spec §9.3, §9.4, §13.7).

Uses local fakes for the secret store and the settings facade so the tests
never touch a real keyring or dconf. The sentinel key from §13.7 is used to
prove no credential leaks through reprs, metadata or exceptions.
"""

from __future__ import annotations

import dataclasses
import unittest

from mdreader.models.ai import AiConnectionDraft, AiError, AiErrorCode
from mdreader.services.ai_profiles import AiProfileStore, validate_api_key

SENTINEL = "sk-mdreader-test-secret-never-log-7d9f"

REMOTE = "https://api.example/v1"
LOOPBACK = "http://127.0.0.1:8000/v1"
OLD_UUID = "old-profile-uuid-0001"


class FakeSecretStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.unavailable = False
        self.lookup_calls: list[str] = []
        self.fail_clear_for: set[str] = set()

    def store(self, profile_id: str, api_key: str) -> None:
        if self.unavailable:
            raise AiError(AiErrorCode.SECRET_SERVICE_UNAVAILABLE, "keyring down")
        self.data[profile_id] = api_key

    def lookup(self, profile_id: str) -> str:
        self.lookup_calls.append(profile_id)
        if self.unavailable:
            raise AiError(AiErrorCode.SECRET_SERVICE_UNAVAILABLE, "keyring down")
        if profile_id not in self.data:
            raise AiError(AiErrorCode.SECRET_NOT_FOUND, "no secret")
        return self.data[profile_id]

    def clear(self, profile_id: str) -> None:
        if self.unavailable:
            raise AiError(AiErrorCode.SECRET_SERVICE_UNAVAILABLE, "keyring down")
        if profile_id in self.fail_clear_for:
            raise AiError(AiErrorCode.CLEANUP_INCOMPLETE, "clear failed")
        self.data.pop(profile_id, None)


class FakeSettings:
    def __init__(self) -> None:
        self.profile: dict[str, str] | None = None
        self.fail_set = False
        self.fail_clear = False

    def get_ai_profile(self) -> dict[str, str] | None:
        return dict(self.profile) if self.profile is not None else None

    def set_ai_profile(self, values: dict[str, str]) -> bool:
        if self.fail_set:
            return False
        self.profile = dict(values)
        return True

    def clear_ai_profile(self) -> bool:
        if self.fail_clear:
            return False
        self.profile = None
        return True


def make_store(secrets: FakeSecretStore | None = None, settings: FakeSettings | None = None) -> AiProfileStore:
    return AiProfileStore(secrets or FakeSecretStore(), settings or FakeSettings())


class SaveWithNewKeyTests(unittest.TestCase):
    def test_new_key_stores_secret_then_metadata(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        store = AiProfileStore(secrets, settings)
        profile = store.save(
            AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL, model_id="m-1")
        )
        self.assertEqual(list(secrets.data), [profile.profile_id])
        self.assertEqual(secrets.data[profile.profile_id], SENTINEL)
        assert settings.profile is not None
        self.assertEqual(settings.profile["profile-id"], profile.profile_id)
        self.assertEqual(settings.profile["api-base-url"], REMOTE)
        self.assertEqual(settings.profile["auth-mode"], "bearer")
        self.assertEqual(settings.profile["model-id"], "m-1")
        self.assertEqual(
            set(settings.profile),
            {
                "profile-id", "provider-kind", "api-base-url", "models-url",
                "model-id", "auth-mode",
            },
        )

    def test_metadata_never_contains_the_key(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        store = AiProfileStore(secrets, settings)
        store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL))
        assert settings.profile is not None
        for value in settings.profile.values():
            self.assertNotIn(SENTINEL, value)

    def test_new_key_rotates_uuid_and_clears_old_secret(self) -> None:
        secrets = FakeSecretStore()
        secrets.data[OLD_UUID] = "old-key"
        settings = FakeSettings()
        settings.profile = {
            "profile-id": OLD_UUID, "provider-kind": "openai-compatible",
            "api-base-url": REMOTE, "models-url": "", "model-id": "",
            "auth-mode": "bearer",
        }
        store = AiProfileStore(secrets, settings)
        profile = store.save(
            AiConnectionDraft(api_base_url=REMOTE, api_key="new-key")
        )
        self.assertNotEqual(profile.profile_id, OLD_UUID)
        self.assertEqual(secrets.data, {profile.profile_id: "new-key"})
        self.assertEqual(store.cleanup_warnings, [])

    def test_old_secret_cleanup_failure_is_partial_success(self) -> None:
        secrets = FakeSecretStore()
        secrets.data[OLD_UUID] = "old-key"
        secrets.fail_clear_for.add(OLD_UUID)
        settings = FakeSettings()
        settings.profile = {
            "profile-id": OLD_UUID, "provider-kind": "openai-compatible",
            "api-base-url": REMOTE, "models-url": "", "model-id": "",
            "auth-mode": "bearer",
        }
        store = AiProfileStore(secrets, settings)
        profile = store.save(
            AiConnectionDraft(api_base_url=REMOTE, api_key="new-key")
        )
        self.assertIsNotNone(profile)
        self.assertEqual(len(store.cleanup_warnings), 1)
        self.assertIn(OLD_UUID, secrets.data)  # old secret still present


class BlankKeyReuseTests(unittest.TestCase):
    def seed(self) -> tuple[FakeSecretStore, FakeSettings]:
        secrets = FakeSecretStore()
        secrets.data[OLD_UUID] = SENTINEL
        settings = FakeSettings()
        settings.profile = {
            "profile-id": OLD_UUID, "provider-kind": "openai-compatible",
            "api-base-url": REMOTE, "models-url": "", "model-id": "",
            "auth-mode": "bearer",
        }
        return secrets, settings

    def test_same_origin_blank_key_reuses_saved_secret_and_uuid(self) -> None:
        secrets, settings = self.seed()
        store = AiProfileStore(secrets, settings)
        profile = store.save(
            AiConnectionDraft(
                api_base_url=REMOTE, api_key="", model_id="new-model"
            )
        )
        self.assertEqual(profile.profile_id, OLD_UUID)
        self.assertEqual(secrets.lookup_calls, [OLD_UUID])
        self.assertEqual(list(secrets.data), [OLD_UUID])  # no new secret
        assert settings.profile is not None
        self.assertEqual(settings.profile["model-id"], "new-model")

    def test_same_origin_blank_key_missing_secret_rejected(self) -> None:
        secrets, settings = self.seed()
        del secrets.data[OLD_UUID]  # keyring lost the secret
        store = AiProfileStore(secrets, settings)
        with self.assertRaises(AiError) as ctx:
            store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=""))
        self.assertIs(ctx.exception.code, AiErrorCode.SECRET_NOT_FOUND)
        assert settings.profile is not None  # old profile preserved
        self.assertEqual(settings.profile["profile-id"], OLD_UUID)

    def test_cross_origin_blank_key_rejected_without_lookup(self) -> None:
        secrets, settings = self.seed()
        store = AiProfileStore(secrets, settings)
        with self.assertRaises(AiError) as ctx:
            store.save(
                AiConnectionDraft(api_base_url="https://other.example/v1", api_key="")
            )
        self.assertIs(ctx.exception.code, AiErrorCode.REQUEST_REJECTED)
        self.assertEqual(secrets.lookup_calls, [])  # never looked up old key
        self.assertEqual(list(secrets.data), [OLD_UUID])  # never sent anywhere

    def test_port_change_blank_key_rejected(self) -> None:
        secrets, settings = self.seed()
        store = AiProfileStore(secrets, settings)
        with self.assertRaises(AiError) as ctx:
            store.save(
                AiConnectionDraft(
                    api_base_url="https://api.example:9000/v1", api_key=""
                )
            )
        self.assertIs(ctx.exception.code, AiErrorCode.REQUEST_REJECTED)

    def test_scheme_change_blank_key_rejected(self) -> None:
        secrets = FakeSecretStore()
        secrets.data[OLD_UUID] = SENTINEL
        settings = FakeSettings()
        settings.profile = {
            "profile-id": OLD_UUID, "provider-kind": "openai-compatible",
            "api-base-url": "https://localhost/v1", "models-url": "", "model-id": "",
            "auth-mode": "bearer",
        }
        store = AiProfileStore(secrets, settings)
        with self.assertRaises(AiError) as ctx:
            store.save(
                AiConnectionDraft(api_base_url="http://localhost/v1", api_key="")
            )
        self.assertIs(ctx.exception.code, AiErrorCode.REQUEST_REJECTED)
        self.assertEqual(secrets.lookup_calls, [])


class AuthModeTests(unittest.TestCase):
    def test_remote_none_auth_mode_rejected(self) -> None:
        store = make_store()
        with self.assertRaises(AiError) as ctx:
            store.save(
                AiConnectionDraft(
                    api_base_url=REMOTE, api_key="", auth_mode="none"
                )
            )
        self.assertIs(ctx.exception.code, AiErrorCode.REQUEST_REJECTED)

    def test_loopback_explicit_none_saves_without_secret(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        store = AiProfileStore(secrets, settings)
        profile = store.save(
            AiConnectionDraft(
                api_base_url=LOOPBACK, api_key="", auth_mode="none"
            )
        )
        self.assertEqual(profile.auth_mode, "none")
        self.assertEqual(secrets.data, {})
        assert settings.profile is not None
        self.assertEqual(settings.profile["auth-mode"], "none")

    def test_loopback_none_with_key_rejected(self) -> None:
        store = make_store()
        with self.assertRaises(AiError) as ctx:
            store.save(
                AiConnectionDraft(
                    api_base_url=LOOPBACK, api_key=SENTINEL, auth_mode="none"
                )
            )
        self.assertIs(ctx.exception.code, AiErrorCode.REQUEST_REJECTED)

    def test_loopback_bearer_blank_without_saved_secret_rejected(self) -> None:
        store = make_store()
        with self.assertRaises(AiError):
            store.save(
                AiConnectionDraft(api_base_url=LOOPBACK, api_key="", auth_mode="bearer")
            )

    def test_loopback_bearer_with_key_saved(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        store = AiProfileStore(secrets, settings)
        profile = store.save(
            AiConnectionDraft(api_base_url=LOOPBACK, api_key=SENTINEL)
        )
        self.assertEqual(secrets.data[profile.profile_id], SENTINEL)


class TransactionFailureTests(unittest.TestCase):
    def test_settings_write_failure_rolls_back_new_secret(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        settings.fail_set = True
        store = AiProfileStore(secrets, settings)
        with self.assertRaises(AiError) as ctx:
            store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL))
        self.assertIs(ctx.exception.code, AiErrorCode.SETTINGS_WRITE_FAILED)
        self.assertEqual(secrets.data, {})  # new secret rolled back

    def test_rollback_clear_failure_records_pending_cleanup(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        settings.fail_set = True
        secrets.fail_clear_for.add("new-uuid-0001")
        store = AiProfileStore(
            secrets, settings, profile_id_factory=lambda: "new-uuid-0001"
        )
        with self.assertRaises(AiError) as ctx:
            store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL))
        self.assertIs(ctx.exception.code, AiErrorCode.SETTINGS_WRITE_FAILED)
        self.assertEqual(store.pending_cleanup, ("new-uuid-0001",))
        # Retry succeeds once the store can clear again.
        secrets.fail_clear_for.clear()
        store.retry_pending_cleanup()
        self.assertEqual(store.pending_cleanup, ())

    def test_settings_failure_keeps_old_profile(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        settings.profile = {
            "profile-id": OLD_UUID, "provider-kind": "openai-compatible",
            "api-base-url": REMOTE, "models-url": "", "model-id": "old",
            "auth-mode": "bearer",
        }
        secrets.data[OLD_UUID] = "old-key"
        settings.fail_set = True
        store = AiProfileStore(secrets, settings)
        with self.assertRaises(AiError):
            store.save(AiConnectionDraft(api_base_url=REMOTE, api_key="new-key"))
        assert settings.profile is not None
        self.assertEqual(settings.profile["profile-id"], OLD_UUID)
        self.assertEqual(settings.profile["model-id"], "old")


class ClearTests(unittest.TestCase):
    def seed(self) -> tuple[FakeSecretStore, FakeSettings]:
        secrets = FakeSecretStore()
        secrets.data[OLD_UUID] = SENTINEL
        settings = FakeSettings()
        settings.profile = {
            "profile-id": OLD_UUID, "provider-kind": "openai-compatible",
            "api-base-url": REMOTE, "models-url": "", "model-id": "",
            "auth-mode": "bearer",
        }
        return secrets, settings

    def test_clear_removes_secret_then_settings(self) -> None:
        secrets, settings = self.seed()
        store = AiProfileStore(secrets, settings)
        store.clear()
        self.assertEqual(secrets.data, {})
        self.assertIsNone(settings.profile)

    def test_clear_secret_failure_keeps_profile(self) -> None:
        secrets, settings = self.seed()
        secrets.unavailable = True
        store = AiProfileStore(secrets, settings)
        with self.assertRaises(AiError) as ctx:
            store.clear()
        self.assertIs(ctx.exception.code, AiErrorCode.SECRET_SERVICE_UNAVAILABLE)
        assert settings.profile is not None  # profile preserved for retry

    def test_clear_settings_failure_is_partial(self) -> None:
        secrets, settings = self.seed()
        settings.fail_clear = True
        store = AiProfileStore(secrets, settings)
        with self.assertRaises(AiError) as ctx:
            store.clear()
        self.assertIs(ctx.exception.code, AiErrorCode.SETTINGS_WRITE_FAILED)
        self.assertEqual(secrets.data, {})  # secret already gone
        assert settings.profile is not None  # metadata remains -> partial failure

    def test_clear_no_profile_is_noop(self) -> None:
        store = make_store()
        store.clear()  # must not raise
        self.assertEqual(store.load(), None)


class LoadTests(unittest.TestCase):
    def test_load_none_when_unset(self) -> None:
        store = make_store()
        self.assertIsNone(store.load())

    def test_load_returns_profile_when_set(self) -> None:
        settings = FakeSettings()
        settings.profile = {
            "profile-id": OLD_UUID, "provider-kind": "openai-compatible",
            "api-base-url": REMOTE, "models-url": "", "model-id": "m-9",
            "auth-mode": "bearer",
        }
        store = AiProfileStore(FakeSecretStore(), settings)
        profile = store.load()
        assert profile is not None
        self.assertEqual(profile.api_base_url, REMOTE)
        self.assertEqual(profile.model_id, "m-9")

    def test_legacy_opencode_model_is_not_migrated(self) -> None:
        # A settings state that only carries the legacy opencode-model key
        # (no ai-profile) yields no profile and leaves settings untouched.
        settings = FakeSettings()
        settings.profile = None
        store = AiProfileStore(FakeSecretStore(), settings)
        self.assertIsNone(store.load())
        self.assertIsNone(settings.profile)


class KeyValidationTests(unittest.TestCase):
    def test_leading_whitespace_rejected_without_trim(self) -> None:
        with self.assertRaises(AiError):
            validate_api_key("  key")
        with self.assertRaises(AiError):
            validate_api_key("key  ")

    def test_newline_and_nul_rejected(self) -> None:
        with self.assertRaises(AiError):
            validate_api_key("key\n")
        with self.assertRaises(AiError):
            validate_api_key("key\x00")

    def test_overlong_key_rejected(self) -> None:
        with self.assertRaises(AiError):
            validate_api_key("k" * 8193)


class CredentialLeakTests(unittest.TestCase):
    """Prove the sentinel key never appears in reprs, metadata or exceptions."""

    def test_draft_repr_and_str_hide_the_key(self) -> None:
        draft = AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL)
        self.assertNotIn(SENTINEL, repr(draft))
        self.assertNotIn(SENTINEL, str(draft))

    def test_profile_repr_hides_the_key(self) -> None:
        store = make_store()
        profile = store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL))
        self.assertNotIn(SENTINEL, repr(profile))
        self.assertNotIn(SENTINEL, str(profile))

    def test_exception_repr_hides_the_key(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        settings.fail_set = True
        store = AiProfileStore(secrets, settings)
        try:
            store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL))
            self.fail("expected AiError")
        except AiError as exc:
            self.assertNotIn(SENTINEL, str(exc))
            self.assertNotIn(SENTINEL, repr(exc))
            self.assertNotIn(SENTINEL, exc.detail)

    def test_settings_metadata_values_never_contain_sentinel(self) -> None:
        secrets, settings = FakeSecretStore(), FakeSettings()
        store = AiProfileStore(secrets, settings)
        store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL))
        assert settings.profile is not None
        blob = repr(settings.profile)
        self.assertNotIn(SENTINEL, blob)

    def test_asdict_of_draft_is_a_documented_hazard(self) -> None:
        # dataclasses.asdict() does not honour repr=False: it WOULD expose the
        # key. The codebase must never call asdict() on drafts; this test pins
        # the hazard so a future change cannot silently assume it is safe.
        draft = AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL)
        dumped = dataclasses.asdict(draft)
        self.assertIn(SENTINEL, dumped["api_key"])


class IntegrationWithSettingsFacadeTests(unittest.TestCase):
    """Wire AiProfileStore against the real SettingsStore facade (memory mode
    outside Meson, real GSettings inside the Meson env)."""

    def test_save_load_clear_roundtrip_with_real_facade(self) -> None:
        from mdreader.services.settings import SettingsStore

        settings = SettingsStore()
        # Force the in-memory fallback so the test is deterministic even when
        # the active schema predates ai-profile (stale ~/.local install).
        settings._settings = None
        store = AiProfileStore(FakeSecretStore(), settings)
        profile = store.save(
            AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL, model_id="m-7")
        )
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.profile_id, profile.profile_id)
        self.assertEqual(loaded.api_base_url, REMOTE)
        self.assertEqual(loaded.model_id, "m-7")
        store.clear()
        self.assertIsNone(store.load())

    def test_metadata_written_via_facade_never_contains_key(self) -> None:
        from mdreader.services.settings import SettingsStore

        settings = SettingsStore()
        settings._settings = None
        store = AiProfileStore(FakeSecretStore(), settings)
        store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL))
        stored = settings.get_ai_profile()
        assert stored is not None
        self.assertNotIn(SENTINEL, repr(stored))

    def test_old_schema_without_ai_profile_degrades_without_crashing(self) -> None:
        from mdreader.services.settings import SettingsStore

        # On a machine whose installed schema predates ai-profile, GSettings
        # get/set on the missing key aborts the process; the facade must probe
        # has_key first and fail honestly.
        settings = SettingsStore()
        if settings._settings is not None and settings._ai_profile_supported():
            self.skipTest("active schema already supports ai-profile")
        if settings._settings is None:
            self.skipTest("memory fallback always supports ai-profile")
        store = AiProfileStore(FakeSecretStore(), settings)
        with self.assertRaises(AiError) as ctx:
            store.save(AiConnectionDraft(api_base_url=REMOTE, api_key=SENTINEL))
        self.assertIs(ctx.exception.code, AiErrorCode.SETTINGS_WRITE_FAILED)
        self.assertIsNone(settings.get_ai_profile())


if __name__ == "__main__":
    unittest.main()
