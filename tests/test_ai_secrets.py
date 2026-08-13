"""Secret Service storage tests for the direct LLM migration
(docs/LLM_PROVIDER_MIGRATION_SPEC.md §9.2/§9.3/§13.7).

This file is the Phase 2 red test: it targets ``mdreader.services.ai_secrets``,
which does not exist yet, so every test currently fails with ImportError.
Phase 2 must implement the module to this exact contract and turn these tests
green.

The injected ``InMemorySecretStore`` fake keeps unit tests off the developer's
real keyring (spec §13.1.7). The real-keyring smoke only uses a test-only
profile id and a fake key, and skips — never passes — when no keyring service
is reachable (spec §13.1.8).
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from mdreader.models.ai import AiError, AiErrorCode
from mdreader.services.ai_secrets import (
    AiSecretStore,
    InMemorySecretStore,
    SecretServiceStore,
    secret_runtime_available,
    secret_service_name_owned,
)

# Spec §13.7: the shared fake key. Must never appear in logs, persistence or
# any repr/str produced by the stores or their errors.
SENTINEL = "sk-mdreader-test-secret-never-log-7d9f"


def code_of(call) -> AiErrorCode:
    """Return the AiErrorCode raised by callable ``call``."""
    with unittest.TestCase().assertRaises(AiError) as ctx:
        call()
    return ctx.exception.code


class AiSecretStoreContractTests(unittest.TestCase):
    """Both stores must expose the AiSecretStore protocol surface."""

    def test_both_stores_expose_protocol_methods(self) -> None:
        for cls in (InMemorySecretStore, SecretServiceStore):
            with self.subTest(cls=cls.__name__):
                for method in ("store", "lookup", "clear"):
                    self.assertTrue(
                        callable(getattr(cls, method, None)),
                        f"{cls.__name__} is missing {method}()",
                    )

    def test_protocol_has_required_methods(self) -> None:
        self.assertTrue(callable(AiSecretStore.store))
        self.assertTrue(callable(AiSecretStore.lookup))
        self.assertTrue(callable(AiSecretStore.clear))


class InMemoryStoreTests(unittest.TestCase):
    def test_store_and_lookup_round_trip(self) -> None:
        store = InMemorySecretStore()
        store.store("profile-a", SENTINEL)
        self.assertEqual(store.lookup("profile-a"), SENTINEL)

    def test_multiple_profiles_are_isolated(self) -> None:
        store = InMemorySecretStore()
        store.store("profile-a", "key-a")
        store.store("profile-b", "key-b")
        self.assertEqual(store.lookup("profile-a"), "key-a")
        self.assertEqual(store.lookup("profile-b"), "key-b")

    def test_overwrite_replaces_previous_key(self) -> None:
        store = InMemorySecretStore()
        store.store("profile-a", "old-key")
        store.store("profile-a", SENTINEL)
        self.assertEqual(store.lookup("profile-a"), SENTINEL)

    def test_lookup_missing_raises_secret_not_found(self) -> None:
        store = InMemorySecretStore()
        self.assertIs(
            code_of(lambda: store.lookup("missing-profile")),
            AiErrorCode.SECRET_NOT_FOUND,
        )

    def test_clear_removes_secret(self) -> None:
        store = InMemorySecretStore()
        store.store("profile-a", SENTINEL)
        store.clear("profile-a")
        self.assertIs(
            code_of(lambda: store.lookup("profile-a")),
            AiErrorCode.SECRET_NOT_FOUND,
        )

    def test_clear_is_idempotent(self) -> None:
        store = InMemorySecretStore()
        store.clear("never-stored")  # must not raise
        store.store("profile-a", SENTINEL)
        store.clear("profile-a")
        store.clear("profile-a")  # clearing again is still a success
        self.assertIs(
            code_of(lambda: store.lookup("profile-a")),
            AiErrorCode.SECRET_NOT_FOUND,
        )

    def test_from_mapping_preloads_profiles(self) -> None:
        store = InMemorySecretStore.from_mapping({"profile-a": SENTINEL})
        self.assertEqual(store.lookup("profile-a"), SENTINEL)

    def test_set_unavailable_blocks_all_operations(self) -> None:
        store = InMemorySecretStore()
        store.store("profile-a", SENTINEL)
        store.set_unavailable()
        self.assertIs(
            code_of(lambda: store.store("profile-a", "other-key")),
            AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
        )
        self.assertIs(
            code_of(lambda: store.lookup("profile-a")),
            AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
        )
        self.assertIs(
            code_of(lambda: store.clear("profile-a")),
            AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
        )

    def test_unavailable_is_reversible(self) -> None:
        store = InMemorySecretStore()
        store.store("profile-a", SENTINEL)
        store.set_unavailable()
        store.set_unavailable(False)
        self.assertEqual(store.lookup("profile-a"), SENTINEL)

    def test_repr_and_str_never_contain_key(self) -> None:
        store = InMemorySecretStore()
        store.store("profile-a", SENTINEL)
        self.assertNotIn(SENTINEL, repr(store))
        self.assertNotIn(SENTINEL, str(store))
        store.set_unavailable()
        with self.assertRaises(AiError) as ctx:
            store.lookup("profile-a")
        self.assertNotIn(SENTINEL, repr(ctx.exception))
        self.assertNotIn(SENTINEL, str(ctx.exception))


class SecretServiceStoreStaticTests(unittest.TestCase):
    """Construction-time behavior only; no keyring access happens here."""

    def test_repr_and_str_never_contain_key(self) -> None:
        store = SecretServiceStore()
        self.assertNotIn(SENTINEL, repr(store))
        self.assertNotIn(SENTINEL, str(store))
        custom = SecretServiceStore(
            schema_name="io.github.pang.mdreader.test",
            label="Test label",
            application="io.github.pang.mdreader",
        )
        self.assertNotIn(SENTINEL, repr(custom))
        self.assertNotIn(SENTINEL, str(custom))

    def test_default_configuration_visible_in_repr(self) -> None:
        store = SecretServiceStore()
        rendered = repr(store)
        self.assertIn("io.github.pang.mdreader.ai", rendered)
        self.assertIn("MD Reader AI API Key", rendered)
        self.assertIn("io.github.pang.mdreader", rendered)

    def test_construction_never_touches_the_runtime(self) -> None:
        # SecretServiceStore must be constructible even where the Secret GI
        # typelib is missing; the probe is deferred to the first operation.
        SecretServiceStore(
            schema_name="io.github.pang.mdreader.ai",
            label="MD Reader AI API Key",
            application="io.github.pang.mdreader",
        )

    def test_missing_runtime_degrades_to_unavailable(self) -> None:
        import mdreader.services.ai_secrets as secrets_module

        store = SecretServiceStore()

        def no_secret_runtime():
            raise ImportError("No module named 'gi.repository.Secret'")

        original = secrets_module._require_secret
        secrets_module._require_secret = no_secret_runtime
        try:
            self.assertIs(
                code_of(lambda: store.store("profile-a", SENTINEL)),
                AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
            )
            self.assertIs(
                code_of(lambda: store.lookup("profile-a")),
                AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
            )
            self.assertIs(
                code_of(lambda: store.clear("profile-a")),
                AiErrorCode.SECRET_SERVICE_UNAVAILABLE,
            )
        finally:
            secrets_module._require_secret = original


class RuntimeProbeTests(unittest.TestCase):
    def test_secret_runtime_available_returns_bool_without_raising(self) -> None:
        # On this development machine the Secret GI typelib is installed, so
        # True is expected, but assertIn keeps the assertion environment-agnostic.
        result = secret_runtime_available()
        self.assertIn(result, (True, False))

    def test_secret_service_name_owned_returns_bool_without_raising(self) -> None:
        # Non-activating bus probe: must return a bool and never raise,
        # regardless of whether a keyring daemon is running.
        result = secret_service_name_owned()
        self.assertIsInstance(result, bool)


class RealSecretServiceSmokeTests(unittest.TestCase):
    """Real-keyring smoke (spec §13.1.8): test-only profile id + fake key.

    Skips with ``UNAVAILABLE: 本机无 keyring 服务`` when no Secret Service is
    reachable. A skip is never reported as a pass.
    """

    def test_real_keyring_store_lookup_clear(self) -> None:
        # Fast skip when no provider is running: the non-activating probe
        # avoids auto-starting a daemon (and the ~7-25s prompter timeout).
        if not secret_service_name_owned():
            self.skipTest("UNAVAILABLE: 本机无 keyring 服务")
        store = SecretServiceStore()
        profile_id = f"test-profile-{uuid4().hex}"
        stored = False
        try:
            if not secret_runtime_available():
                self.skipTest("UNAVAILABLE: 本机无 keyring 服务")
            try:
                import gi

                gi.require_version("Secret", "1")
                from gi.repository import Secret

                Secret.Service.get_sync(Secret.ServiceFlags.NONE, None)
            except Exception:
                self.skipTest("UNAVAILABLE: 本机无 keyring 服务")

            store.store(profile_id, SENTINEL)
            stored = True
            self.assertEqual(store.lookup(profile_id), SENTINEL)
            store.clear(profile_id)
            with self.assertRaises(AiError) as ctx:
                store.lookup(profile_id)
            self.assertIs(ctx.exception.code, AiErrorCode.SECRET_NOT_FOUND)
        except AiError as exc:
            if exc.code is AiErrorCode.SECRET_SERVICE_UNAVAILABLE:
                self.skipTest(f"UNAVAILABLE: 本机无 keyring 服务 ({exc.detail})")
            raise
        finally:
            if stored:
                try:
                    store.clear(profile_id)
                except AiError as exc:
                    # Leaving the fake key in the real keyring would violate
                    # spec §4.1; fail loudly instead of reporting a pass.
                    self.fail(
                        f"smoke cleanup failed to remove test secret: {exc.code.value}"
                    )


if __name__ == "__main__":
    unittest.main()
