"""Model catalog parser tests for the direct LLM migration (docs/LLM_PROVIDER_MIGRATION_SPEC.md §7.2/§13.3).

This file is the Phase 1 red test: it targets ``mdreader.services.ai_models``,
which does not exist yet, so every test currently fails with ImportError.
Phase 1 must implement the module to this exact contract and turn these tests
green. No network, no GTK: pure logic only.
"""

from __future__ import annotations

import json
import unittest

from mdreader.models.ai import AiError, AiErrorCode, AiModel
from mdreader.services.ai_models import ModelCatalogResult, parse_models_response

TWO_MIB = 2 * 1024 * 1024


def code_of(call) -> AiErrorCode:
    """Return the AiErrorCode raised by callable ``call``."""
    with unittest.TestCase().assertRaises(AiError) as ctx:
        call()
    return ctx.exception.code


def body(entries) -> bytes:
    """Serialize a /models response body with the given data entries."""
    return json.dumps({"data": entries}).encode("utf-8")


class ParseStandardTests(unittest.TestCase):
    def test_parses_standard_data(self) -> None:
        result = parse_models_response(
            body(
                [
                    {"id": "llama-3.1", "owned_by": "meta"},
                    {"id": "gpt-4o", "owned_by": "openai"},
                ]
            )
        )
        self.assertEqual(
            result.models,
            (
                AiModel(model_id="gpt-4o", owned_by="openai"),
                AiModel(model_id="llama-3.1", owned_by="meta"),
            ),
        )

    def test_missing_owned_by_defaults_to_empty(self) -> None:
        result = parse_models_response(body([{"id": "model-a"}]))
        self.assertEqual(result.models, (AiModel(model_id="model-a", owned_by=""),))

    def test_owned_by_non_string_defaults_to_empty(self) -> None:
        result = parse_models_response(
            body(
                [
                    {"id": "model-a", "owned_by": 42},
                    {"id": "model-b", "owned_by": None},
                ]
            )
        )
        self.assertEqual(result.models[0].owned_by, "")
        self.assertEqual(result.models[1].owned_by, "")

    def test_empty_data_is_successful_empty_catalog(self) -> None:
        result = parse_models_response(b'{"data": []}')
        self.assertEqual(result, ModelCatalogResult(models=()))

    def test_exact_duplicate_ids_are_deduplicated(self) -> None:
        result = parse_models_response(
            body(
                [
                    {"id": "model-a", "owned_by": "x"},
                    {"id": "model-a", "owned_by": "y"},
                    {"id": "model-b"},
                ]
            )
        )
        self.assertEqual([m.model_id for m in result.models], ["model-a", "model-b"])
        self.assertEqual(result.models[0].owned_by, "x")  # first occurrence wins

    def test_case_variants_are_distinct_ids(self) -> None:
        result = parse_models_response(body([{"id": "Model-A"}, {"id": "model-a"}]))
        self.assertEqual([m.model_id for m in result.models], ["Model-A", "model-a"])

    def test_models_are_sorted_by_casefold_keeping_original_ids(self) -> None:
        result = parse_models_response(
            body([{"id": "Beta"}, {"id": "alpha"}, {"id": "Alpha"}, {"id": "beta"}])
        )
        # Stable sort on casefold keys: alpha group then beta group, original
        # relative order inside each group; IDs themselves are unchanged.
        self.assertEqual(
            [m.model_id for m in result.models],
            ["alpha", "Alpha", "Beta", "beta"],
        )

    def test_maximum_id_length_is_accepted(self) -> None:
        result = parse_models_response(body([{"id": "m" * 256}]))
        self.assertEqual(result.models, (AiModel(model_id="m" * 256),))


class ParseInvalidStructureTests(unittest.TestCase):
    def test_root_must_be_a_json_object(self) -> None:
        for raw in (b"[1, 2]", b'"models"', b"42", b"null", b"true"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    code_of(lambda raw=raw: parse_models_response(raw)),
                    AiErrorCode.INVALID_RESPONSE,
                )

    def test_missing_data_is_invalid(self) -> None:
        self.assertEqual(
            code_of(lambda: parse_models_response(b'{"object": "list"}')),
            AiErrorCode.INVALID_RESPONSE,
        )

    def test_data_not_an_array_is_invalid(self) -> None:
        for raw in (b'{"data": "x"}', b'{"data": 42}', b'{"data": {}}', b'{"data": null}'):
            with self.subTest(raw=raw):
                self.assertEqual(
                    code_of(lambda raw=raw: parse_models_response(raw)),
                    AiErrorCode.INVALID_RESPONSE,
                )

    def test_non_object_entries_are_skipped(self) -> None:
        result = parse_models_response(body(["model-a", 42, None, {"id": "model-b"}]))
        self.assertEqual([m.model_id for m in result.models], ["model-b"])


class ParseInvalidIdTests(unittest.TestCase):
    def test_id_not_a_string_is_skipped(self) -> None:
        result = parse_models_response(
            body([{"id": 42}, {"id": None}, {"id": ["a"]}, {"id": "ok"}])
        )
        self.assertEqual([m.model_id for m in result.models], ["ok"])

    def test_blank_or_missing_id_is_skipped(self) -> None:
        result = parse_models_response(
            body([{"id": ""}, {"id": "   "}, {"owned_by": "x"}, {"id": "\t"}, {"id": "good"}])
        )
        self.assertEqual([m.model_id for m in result.models], ["good"])

    def test_id_too_long_is_skipped(self) -> None:
        long_id = "m" * 257
        result = parse_models_response(body([{"id": long_id}, {"id": "ok"}]))
        self.assertEqual([m.model_id for m in result.models], ["ok"])

    def test_control_and_invisible_characters_are_rejected(self) -> None:
        bad_ids = [
            "model\u0000-a",  # NUL
            "model\u001f-a",  # Cc control char
            "model\u202e-a",  # Cf right-to-left override
            "model\u200b-a",  # Cf zero-width space
            "model-a\n",  # newline
            " model-a",  # leading space
            "model\u00a0-a",  # no-break space
        ]
        for bad in bad_ids:
            with self.subTest(bad=repr(bad)):
                self.assertEqual(
                    code_of(lambda bad=bad: parse_models_response(body([{"id": bad}]))),
                    AiErrorCode.INVALID_RESPONSE,
                )

    def test_partial_invalid_entries_are_skipped(self) -> None:
        result = parse_models_response(
            body(
                [
                    {"id": "good-a"},
                    {"id": "bad id"},  # whitespace inside
                    "not-an-object",
                    {"id": ""},
                    {"id": "good-b", "owned_by": "p"},
                    {"id": 7},
                ]
            )
        )
        self.assertEqual([m.model_id for m in result.models], ["good-a", "good-b"])

    def test_all_entries_invalid_is_error_not_empty_success(self) -> None:
        for entries in (
            [{"id": ""}],
            [{"id": " bad"}],
            ["not-an-object"],
            [{"id": 42}],
            [{"id": "m" * 257}],
            [{"id": "a\n"}, {"id": "b "}],
        ):
            with self.subTest(entries=entries):
                self.assertEqual(
                    code_of(lambda entries=entries: parse_models_response(body(entries))),
                    AiErrorCode.INVALID_RESPONSE,
                )


class ParseTransportTests(unittest.TestCase):
    def test_invalid_utf8_body_is_invalid_response(self) -> None:
        self.assertEqual(
            code_of(lambda: parse_models_response(b'{"data": []}\xff')),
            AiErrorCode.INVALID_RESPONSE,
        )

    def test_malformed_json_is_invalid_response(self) -> None:
        self.assertEqual(
            code_of(lambda: parse_models_response(b'{"data": [')),
            AiErrorCode.INVALID_RESPONSE,
        )

    def test_body_at_max_size_is_accepted(self) -> None:
        raw = b'{"data": []}' + b" " * (TWO_MIB - len(b'{"data": []}'))
        self.assertEqual(len(raw), TWO_MIB)
        result = parse_models_response(raw)
        self.assertEqual(result.models, ())

    def test_body_over_max_size_is_rejected(self) -> None:
        raw = b'{"data": []}' + b" " * (TWO_MIB + 1 - len(b'{"data": []}'))
        self.assertEqual(len(raw), TWO_MIB + 1)
        self.assertEqual(
            code_of(lambda raw=raw: parse_models_response(raw)),
            AiErrorCode.RESPONSE_TOO_LARGE,
        )

    def test_custom_max_body_bytes_is_enforced(self) -> None:
        self.assertEqual(
            code_of(lambda: parse_models_response(b'{"data": []}', max_body_bytes=8)),
            AiErrorCode.RESPONSE_TOO_LARGE,
        )


class ParseCountBoundaryTests(unittest.TestCase):
    def _many_entries(self, count: int) -> bytes:
        return body([{"id": f"model-{i:05d}"} for i in range(count)])

    def test_exactly_max_models_is_accepted(self) -> None:
        result = parse_models_response(self._many_entries(2000))
        self.assertEqual(len(result.models), 2000)
        self.assertEqual(result.models[0].model_id, "model-00000")
        self.assertEqual(result.models[-1].model_id, "model-01999")

    def test_over_max_models_is_invalid(self) -> None:
        self.assertEqual(
            code_of(lambda: parse_models_response(self._many_entries(2001))),
            AiErrorCode.INVALID_RESPONSE,
        )

    def test_custom_max_models_is_enforced(self) -> None:
        self.assertEqual(
            code_of(lambda: parse_models_response(self._many_entries(3), max_models=2)),
            AiErrorCode.INVALID_RESPONSE,
        )


class FakeFetchClient:
    """Records the fetch call and drives it to completion synchronously.

    Mirrors the real transport convention: ``on_data(b"")`` marks EOF.
    """

    def __init__(self, *, status: int = 200, data: bytes = b"") -> None:
        self.calls: list[dict] = []
        self.status = status
        self.data = data
        self.transport_error: AiError | None = None

    def fetch(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self.transport_error is not None:
            kwargs["on_error"](self.transport_error)
            return
        kwargs["on_headers"](self.status)
        if self.data:
            kwargs["on_data"](self.data)
        kwargs["on_data"](b"")  # EOF marker


class FetchModelsCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.results: list[ModelCatalogResult] = []
        self.errors: list[AiError] = []

    def fetch(self, client: FakeFetchClient) -> None:
        from mdreader.services.ai_models import fetch_models_catalog

        fetch_models_catalog(
            client,
            endpoint_url="https://api.example/v1/models",
            authorization="Bearer sk-mdreader-test-secret-never-log-7d9f",
            secret="sk-mdreader-test-secret-never-log-7d9f",
            on_result=self.results.append,
            on_error=self.errors.append,
        )

    def test_success_parses_models(self) -> None:
        client = FakeFetchClient(data=body([{"id": "a"}, {"id": "b"}]))
        self.fetch(client)
        self.assertEqual(len(self.errors), 0)
        self.assertEqual([m.model_id for m in self.results[0].models], ["a", "b"])

    def test_empty_data_is_success_not_failure(self) -> None:
        client = FakeFetchClient(data=b'{"data": []}')
        self.fetch(client)
        self.assertEqual(len(self.results), 1)
        self.assertEqual(self.results[0].models, ())
        self.assertEqual(len(self.errors), 0)

    def test_request_uses_get_with_bounded_parameters(self) -> None:
        client = FakeFetchClient(data=b'{"data": []}')
        self.fetch(client)
        call = client.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://api.example/v1/models")
        self.assertEqual(call["headers"], {"Accept": "application/json"})
        self.assertTrue(call["authorization"].startswith("Bearer "))
        self.assertEqual(call["max_bytes"], 2 * 1024 * 1024)
        self.assertEqual(call["deadline_ms"], 20_000)
        self.assertIsNone(call["body"])
        self.assertIn("on_headers", call)
        self.assertNotIn("on_done", call)

    def test_401_maps_to_authentication_failed(self) -> None:
        self.fetch(FakeFetchClient(status=401, data=b"{}"))
        self.assertEqual(self.errors[0].code, AiErrorCode.AUTHENTICATION_FAILED)

    def test_403_maps_to_authentication_failed_for_models(self) -> None:
        self.fetch(FakeFetchClient(status=403, data=b"{}"))
        self.assertEqual(self.errors[0].code, AiErrorCode.AUTHENTICATION_FAILED)

    def test_404_maps_to_endpoint_not_found(self) -> None:
        self.fetch(FakeFetchClient(status=404, data=b"{}"))
        self.assertEqual(self.errors[0].code, AiErrorCode.ENDPOINT_NOT_FOUND)

    def test_429_maps_to_rate_limited(self) -> None:
        self.fetch(FakeFetchClient(status=429, data=b"{}"))
        self.assertEqual(self.errors[0].code, AiErrorCode.RATE_LIMITED)

    def test_500_maps_to_provider_unavailable(self) -> None:
        self.fetch(FakeFetchClient(status=500, data=b"{}"))
        self.assertEqual(self.errors[0].code, AiErrorCode.PROVIDER_UNAVAILABLE)

    def test_malformed_2xx_body_is_invalid_response(self) -> None:
        self.fetch(FakeFetchClient(status=200, data=b"not json"))
        self.assertEqual(self.errors[0].code, AiErrorCode.INVALID_RESPONSE)

    def test_transport_error_is_propagated(self) -> None:
        client = FakeFetchClient(data=b"{}")
        client.transport_error = AiError(AiErrorCode.NETWORK_FAILED, "dns")
        self.fetch(client)
        self.assertEqual(self.errors[0].code, AiErrorCode.NETWORK_FAILED)

    def test_error_body_is_redacted_and_scrubbed(self) -> None:
        client = FakeFetchClient(
            status=401,
            data=b"<html>echoed sk-mdreader-test-secret-never-log-7d9f</html>",
        )
        self.fetch(client)
        self.assertEqual(self.errors[0].code, AiErrorCode.AUTHENTICATION_FAILED)
        self.assertNotIn("sk-mdreader-test-secret-never-log-7d9f", self.errors[0].detail)
        self.assertNotIn("<html>", self.errors[0].detail)


class DraftFingerprintTests(unittest.TestCase):
    def _fp(self, **overrides) -> str:
        from mdreader.services.ai_models import draft_fingerprint

        fields = dict(
            api_base_url="https://api.example/v1",
            models_url="",
            auth_mode="bearer",
            key_source="new-key",
            key_revision=1,
        )
        fields.update(overrides)
        return draft_fingerprint(**fields)

    def test_identical_fields_produce_identical_fingerprint(self) -> None:
        self.assertEqual(self._fp(), self._fp())

    def test_url_change_changes_fingerprint(self) -> None:
        self.assertNotEqual(
            self._fp(), self._fp(api_base_url="https://other.example/v1")
        )

    def test_models_url_change_changes_fingerprint(self) -> None:
        self.assertNotEqual(
            self._fp(), self._fp(models_url="https://api.example/v1/custom-models")
        )

    def test_auth_mode_change_changes_fingerprint(self) -> None:
        self.assertNotEqual(self._fp(), self._fp(auth_mode="none"))

    def test_key_source_change_changes_fingerprint(self) -> None:
        self.assertNotEqual(self._fp(), self._fp(key_source="saved-same-origin"))

    def test_key_revision_change_changes_fingerprint(self) -> None:
        self.assertNotEqual(self._fp(), self._fp(key_revision=2))

    def test_fingerprint_never_accepts_a_secret(self) -> None:
        # The API has no secret parameter at all: the key cannot be hashed,
        # serialized or logged through this path (spec §7.1).
        from mdreader.services.ai_models import draft_fingerprint

        import inspect

        params = inspect.signature(draft_fingerprint).parameters
        self.assertNotIn("secret", params)
        self.assertNotIn("api_key", params)


class RedactErrorBodyTests(unittest.TestCase):
    def test_strips_html_tags(self) -> None:
        from mdreader.services.ai_models import redact_error_body

        self.assertNotIn("<", redact_error_body(b"<h1>error</h1><p>boom</p>"))

    def test_strips_control_characters(self) -> None:
        from mdreader.services.ai_models import redact_error_body

        text = redact_error_body(b"a\x00b\nc\x1bd")
        self.assertNotIn("\x00", text)
        self.assertNotIn("\x1b", text)

    def test_scrubs_the_secret(self) -> None:
        from mdreader.services.ai_models import redact_error_body

        text = redact_error_body(
            b'{"error": "bad key sk-mdreader-test-secret-never-log-7d9f"}',
            secret="sk-mdreader-test-secret-never-log-7d9f",
        )
        self.assertNotIn("sk-mdreader-test-secret-never-log-7d9f", text)

    def test_limits_length(self) -> None:
        from mdreader.services.ai_models import redact_error_body

        text = redact_error_body(b"x" * 5000)
        self.assertEqual(len(text), 512)

    def test_invalid_utf8_does_not_crash(self) -> None:
        from mdreader.services.ai_models import redact_error_body

        text = redact_error_body(b"\xff\xfe\x00garbage")
        self.assertIsInstance(text, str)


if __name__ == "__main__":
    unittest.main()
