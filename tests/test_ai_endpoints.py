"""URL policy tests for the direct LLM migration (docs/LLM_PROVIDER_MIGRATION_SPEC.md §6.1, §13.2).

This file is the Phase 0 red test: it targets ``mdreader.services.ai_endpoints``,
which does not exist yet, so every test currently fails with ImportError.
Phase 1 must implement the module to this exact contract and turn these tests
green. No network, no GTK: pure logic only.
"""

from __future__ import annotations

import unittest

from mdreader.services.ai_endpoints import (
    EndpointError,
    NormalizedEndpoint,
    build_chat_endpoint,
    build_models_endpoint,
    is_loopback_endpoint,
    normalize_api_base_url,
    normalize_endpoint_url,
    resolve_redirect,
    same_origin,
)

# Stable error codes defined in the migration spec §5.4.
INVALID_URL = "INVALID_URL"
INSECURE_REMOTE_URL = "INSECURE_REMOTE_URL"
CROSS_ORIGIN_MODELS_URL = "CROSS_ORIGIN_MODELS_URL"
REDIRECT_REJECTED = "REDIRECT_REJECTED"


def code_of(call) -> str:
    """Return the EndpointError code raised by callable ``call``."""
    with unittest.TestCase().assertRaises(EndpointError) as ctx:
        call()
    return ctx.exception.code


class NormalizeValidTests(unittest.TestCase):
    def test_https_version_root(self) -> None:
        ep = normalize_api_base_url("https://api.example/v1")
        self.assertEqual(ep.scheme, "https")
        self.assertEqual(ep.host, "api.example")
        self.assertEqual(ep.path, "/v1")
        self.assertEqual(ep.url, "https://api.example/v1")

    def test_https_custom_path_preserved(self) -> None:
        ep = normalize_api_base_url("https://router.example/api/v1")
        self.assertEqual(ep.url, "https://router.example/api/v1")

    def test_trailing_slash_removed(self) -> None:
        ep = normalize_api_base_url("https://api.example/v1/")
        self.assertEqual(ep.url, "https://api.example/v1")

    def test_root_path_becomes_empty(self) -> None:
        ep = normalize_api_base_url("https://api.example")
        self.assertEqual(ep.path, "")
        self.assertEqual(ep.url, "https://api.example")

    def test_localhost_loopback_http_allowed(self) -> None:
        ep = normalize_api_base_url("http://localhost:8000/v1")
        self.assertEqual(ep.scheme, "http")
        self.assertTrue(is_loopback_endpoint(ep))

    def test_ipv4_loopback_http_allowed(self) -> None:
        ep = normalize_api_base_url("http://127.0.0.1:8000/v1")
        self.assertTrue(is_loopback_endpoint(ep))

    def test_ipv6_loopback_http_allowed(self) -> None:
        ep = normalize_api_base_url("http://[::1]:8000/v1")
        self.assertTrue(is_loopback_endpoint(ep))

    def test_localhost_uppercase_is_loopback(self) -> None:
        ep = normalize_api_base_url("http://LOCALHOST:8000/v1")
        self.assertTrue(is_loopback_endpoint(ep))

    def test_surrounding_whitespace_stripped(self) -> None:
        ep = normalize_api_base_url("  https://api.example/v1  ")
        self.assertEqual(ep.url, "https://api.example/v1")

    def test_maximum_length_2048_accepted(self) -> None:
        prefix = "https://api.example/"
        url = prefix + "a" * (2048 - len(prefix))
        self.assertEqual(len(url), 2048)
        ep = normalize_api_base_url(url)
        self.assertEqual(ep.path, "/" + "a" * (2048 - len(prefix)))


class NormalizeInvalidTests(unittest.TestCase):
    def test_empty_string_rejected(self) -> None:
        self.assertEqual(code_of(lambda: normalize_api_base_url("")), INVALID_URL)

    def test_blank_string_rejected(self) -> None:
        self.assertEqual(code_of(lambda: normalize_api_base_url("   ")), INVALID_URL)

    def test_missing_host_rejected(self) -> None:
        self.assertEqual(code_of(lambda: normalize_api_base_url("https:///v1")), INVALID_URL)

    def test_remote_http_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("http://api.example/v1")),
            INSECURE_REMOTE_URL,
        )

    def test_domain_resolving_to_loopback_not_allowed_http(self) -> None:
        # A plain domain must be rejected for http even though DNS might resolve
        # it to a loopback address; DNS never participates in the decision.
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("http://myhost.localhost/v1")),
            INSECURE_REMOTE_URL,
        )

    def test_fuzzy_decimal_ipv4_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("http://2130706433/v1")), INVALID_URL
        )

    def test_shorthand_ipv4_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("http://127.1/v1")), INVALID_URL
        )

    def test_ipv6_zone_id_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("http://[fe80::1%25eth0]/v1")),
            INVALID_URL,
        )

    def test_backslash_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url(r"https://api.example\evil/v1")),
            INVALID_URL,
        )

    def test_port_out_of_range_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("https://api.example:99999/v1")),
            INVALID_URL,
        )

    def test_non_numeric_port_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("https://api.example:abc/v1")),
            INVALID_URL,
        )

    def test_userinfo_rejected(self) -> None:
        self.assertEqual(
            code_of(
                lambda: normalize_api_base_url("https://user:pass@api.example/v1")
            ),
            INVALID_URL,
        )

    def test_query_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("https://api.example/v1?x=1")),
            INVALID_URL,
        )

    def test_fragment_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("https://api.example/v1#frag")),
            INVALID_URL,
        )

    def test_newline_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("https://api.example/v1\n")),
            INVALID_URL,
        )

    def test_nul_byte_rejected(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("https://api.example/v1\x00")),
            INVALID_URL,
        )

    def test_full_models_url_rejected_as_base(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_api_base_url("https://api.example/v1/models")),
            INVALID_URL,
        )

    def test_full_chat_url_rejected_as_base(self) -> None:
        self.assertEqual(
            code_of(
                lambda: normalize_api_base_url(
                    "https://api.example/v1/chat/completions"
                )
            ),
            INVALID_URL,
        )

    def test_full_responses_url_rejected_as_base(self) -> None:
        self.assertEqual(
            code_of(
                lambda: normalize_api_base_url("https://api.example/v1/responses")
            ),
            INVALID_URL,
        )

    def test_length_2049_rejected(self) -> None:
        prefix = "https://api.example/"
        url = prefix + "a" * (2049 - len(prefix))
        self.assertEqual(len(url), 2049)
        self.assertEqual(code_of(lambda: normalize_api_base_url(url)), INVALID_URL)

    def test_normalize_endpoint_url_accepts_full_endpoint_path(self) -> None:
        ep = normalize_endpoint_url("https://api.example/v1/chat/completions")
        self.assertEqual(ep.url, "https://api.example/v1/chat/completions")

    def test_normalize_endpoint_url_still_rejects_bad_policy(self) -> None:
        self.assertEqual(
            code_of(lambda: normalize_endpoint_url("http://api.example/v1/models")),
            INSECURE_REMOTE_URL,
        )


class SameOriginTests(unittest.TestCase):
    def test_identical_origin(self) -> None:
        a = normalize_api_base_url("https://api.example/v1")
        b = normalize_api_base_url("https://api.example/other")
        self.assertTrue(same_origin(a, b))

    def test_default_https_port_equals_explicit_443(self) -> None:
        a = normalize_api_base_url("https://api.example/v1")
        b = normalize_api_base_url("https://api.example:443/v1")
        self.assertTrue(same_origin(a, b))

    def test_default_http_port_equals_explicit_80(self) -> None:
        a = normalize_api_base_url("http://localhost/v1")
        b = normalize_api_base_url("http://localhost:80/v1")
        self.assertTrue(same_origin(a, b))

    def test_scheme_change_is_cross_origin(self) -> None:
        a = normalize_api_base_url("https://localhost/v1")
        b = normalize_api_base_url("http://localhost/v1")
        self.assertFalse(same_origin(a, b))

    def test_host_change_is_cross_origin(self) -> None:
        a = normalize_api_base_url("https://api.example/v1")
        b = normalize_api_base_url("https://other.example/v1")
        self.assertFalse(same_origin(a, b))

    def test_port_change_is_cross_origin(self) -> None:
        a = normalize_api_base_url("https://api.example:8000/v1")
        b = normalize_api_base_url("https://api.example:8001/v1")
        self.assertFalse(same_origin(a, b))


class EndpointConstructionTests(unittest.TestCase):
    def test_chat_endpoint_appends_segments(self) -> None:
        base = normalize_api_base_url("https://api.example/v1")
        self.assertEqual(
            build_chat_endpoint(base), "https://api.example/v1/chat/completions"
        )

    def test_models_endpoint_default_appends_models(self) -> None:
        base = normalize_api_base_url("https://router.example/api/v1")
        self.assertEqual(
            build_models_endpoint(base), "https://router.example/api/v1/models"
        )

    def test_chat_endpoint_loopback(self) -> None:
        base = normalize_api_base_url("http://127.0.0.1:8000/v1")
        self.assertEqual(
            build_chat_endpoint(base), "http://127.0.0.1:8000/v1/chat/completions"
        )

    def test_explicit_models_url_is_exact_endpoint(self) -> None:
        base = normalize_api_base_url("https://api.example/v1")
        explicit = "https://api.example/v1/custom-models"
        self.assertEqual(build_models_endpoint(base, explicit), explicit)

    def test_explicit_models_url_cross_origin_rejected(self) -> None:
        base = normalize_api_base_url("https://api.example/v1")
        self.assertEqual(
            code_of(
                lambda: build_models_endpoint(base, "https://other.example/v1/models")
            ),
            CROSS_ORIGIN_MODELS_URL,
        )

    def test_explicit_models_url_insecure_http_rejected(self) -> None:
        base = normalize_api_base_url("https://api.example/v1")
        self.assertEqual(
            code_of(
                lambda: build_models_endpoint(base, "http://api.example/v1/models")
            ),
            INSECURE_REMOTE_URL,
        )


class RedirectPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = normalize_api_base_url("https://api.example/v1")
        self.current = NormalizedEndpoint(
            "https", "api.example", None, "/v1/chat/completions",
            "https://api.example/v1/chat/completions",
        )

    def test_301_get_same_origin_follows(self) -> None:
        decision = resolve_redirect(
            301, "/v2/chat/completions", original=self.original,
            current=self.current, method="GET", hops=0,
        )
        self.assertTrue(decision.follow)
        self.assertEqual(decision.url, "https://api.example/v2/chat/completions")

    def test_307_post_same_origin_follows(self) -> None:
        decision = resolve_redirect(
            307, "/v1/chat/completions", original=self.original,
            current=self.current, method="POST", hops=1,
        )
        self.assertTrue(decision.follow)

    def test_301_post_chat_rejected_method_change(self) -> None:
        for status in (301, 302, 303):
            with self.subTest(status=status):
                decision = resolve_redirect(
                    status, "/v1/chat/completions", original=self.original,
                    current=self.current, method="POST", hops=0,
                )
                self.assertFalse(decision.follow)
                self.assertEqual(decision.error_code, REDIRECT_REJECTED)

    def test_300_304_305_306_rejected(self) -> None:
        for status in (300, 304, 305, 306):
            with self.subTest(status=status):
                decision = resolve_redirect(
                    status, "/v1/chat/completions", original=self.original,
                    current=self.current, method="GET", hops=0,
                )
                self.assertFalse(decision.follow)
                self.assertEqual(decision.error_code, REDIRECT_REJECTED)

    def test_unknown_3xx_rejected(self) -> None:
        decision = resolve_redirect(
            399, "/v1/chat/completions", original=self.original,
            current=self.current, method="GET", hops=0,
        )
        self.assertFalse(decision.follow)
        self.assertEqual(decision.error_code, REDIRECT_REJECTED)

    def test_cross_origin_redirect_rejected(self) -> None:
        decision = resolve_redirect(
            307, "https://other.example/v1/chat/completions",
            original=self.original, current=self.current, method="POST", hops=0,
        )
        self.assertFalse(decision.follow)
        self.assertEqual(decision.error_code, REDIRECT_REJECTED)

    def test_https_to_http_downgrade_rejected(self) -> None:
        decision = resolve_redirect(
            307, "http://api.example/v1/chat/completions",
            original=self.original, current=self.current, method="POST", hops=0,
        )
        self.assertFalse(decision.follow)
        self.assertEqual(decision.error_code, REDIRECT_REJECTED)

    def test_missing_location_rejected(self) -> None:
        decision = resolve_redirect(
            307, None, original=self.original,
            current=self.current, method="POST", hops=0,
        )
        self.assertFalse(decision.follow)
        self.assertEqual(decision.error_code, REDIRECT_REJECTED)

    def test_hop_limit_reached_rejected(self) -> None:
        decision = resolve_redirect(
            307, "/v1/chat/completions", original=self.original,
            current=self.current, method="POST", hops=3, max_hops=3,
        )
        self.assertFalse(decision.follow)
        self.assertEqual(decision.error_code, REDIRECT_REJECTED)

    def test_hop_below_limit_follows(self) -> None:
        decision = resolve_redirect(
            307, "/v1/chat/completions", original=self.original,
            current=self.current, method="POST", hops=2, max_hops=3,
        )
        self.assertTrue(decision.follow)

    def test_relative_location_resolves_against_current_uri(self) -> None:
        decision = resolve_redirect(
            307, "chat/completions", original=self.original,
            current=self.current, method="POST", hops=0,
        )
        # The resolved target is re-validated with the full URL policy; a
        # relative path is legal and resolves against the current URI.
        self.assertTrue(decision.follow)
        self.assertTrue(decision.url.startswith("https://api.example/v1/"))

    def test_location_with_query_rejected_by_full_url_policy(self) -> None:
        decision = resolve_redirect(
            307, "chat/completions?replay=1", original=self.original,
            current=self.current, method="POST", hops=0,
        )
        # Spec §6.4: every resolved Location must pass the full URL policy,
        # which rejects query strings — no weakening of the policy for
        # redirect targets.
        self.assertFalse(decision.follow)
        self.assertEqual(decision.error_code, REDIRECT_REJECTED)

    def test_loopback_http_same_origin_follows(self) -> None:
        original = normalize_api_base_url("http://127.0.0.1:8000/v1")
        current = NormalizedEndpoint(
            "http", "127.0.0.1", 8000, "/v1/chat/completions",
            "http://127.0.0.1:8000/v1/chat/completions",
        )
        decision = resolve_redirect(
            307, "/v2/chat/completions", original=original,
            current=current, method="POST", hops=0,
        )
        self.assertTrue(decision.follow)
        self.assertEqual(decision.url, "http://127.0.0.1:8000/v2/chat/completions")


if __name__ == "__main__":
    unittest.main()
