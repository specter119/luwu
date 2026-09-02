import unittest
from typing import cast

from luwu.semantic import (
    ComparisonStatus,
    ComparisonStrategy,
    SemanticComparisonAdapter,
    compare,
)


class SemanticComparisonTests(unittest.TestCase):
    def test_exact_bytes_requires_identical_bytes(self) -> None:
        result = compare(b"profile = 1\n", b"profile = 1\n")

        self.assertEqual(result.status, ComparisonStatus.EXACT)
        self.assertEqual(result.code, "bytes_equal")
        self.assertTrue(result.equivalent)

    def test_exact_bytes_reports_whitespace_and_line_ending_changes(self) -> None:
        result = compare(b"profile = 1\n", b"profile = 1\r\n")

        self.assertEqual(result.status, ComparisonStatus.DIFFERENT)
        self.assertEqual(result.code, "bytes_differ")
        self.assertFalse(result.equivalent)

    def test_exact_bytes_does_not_decode_invalid_utf8(self) -> None:
        result = compare(b"\xff", b"\xff", strategy="exact-bytes")

        self.assertEqual(result.status, ComparisonStatus.EXACT)
        self.assertEqual(result.strategy, ComparisonStrategy.EXACT_BYTES)

    def test_json_reports_exact_valid_bytes(self) -> None:
        result = compare(
            b'{"profile":"developer"}', b'{"profile":"developer"}', strategy="json"
        )

        self.assertEqual(result.status, ComparisonStatus.EXACT)
        self.assertEqual(result.code, "json_bytes_equal")

    def test_json_treats_formatting_and_object_order_as_equivalent(self) -> None:
        desired = b'{"profile":"developer","enabled":true}\n'
        live = b'{\n  "enabled": true,\n  "profile": "developer"\n}'

        result = compare(desired, live, strategy=ComparisonStrategy.JSON)

        self.assertEqual(result.status, ComparisonStatus.EQUIVALENT_BUT_REFORMATTED)
        self.assertEqual(result.code, "json_values_equivalent")
        self.assertTrue(result.equivalent)

    def test_json_preserves_unknown_fields_in_comparison(self) -> None:
        desired = b'{"known": {"unknown": {"nested": [1, 2]}}}'
        live = b'{"known": {"unknown": {"nested": [1, 3]}}}'

        result = compare(desired, live, strategy="json")

        self.assertEqual(result.status, ComparisonStatus.DIFFERENT)
        self.assertEqual(result.code, "json_values_differ")

    def test_json_preserves_unknown_fields_when_they_are_equal(self) -> None:
        desired = b'{"known": 1, "future_field": {"nested": [true, null]}}'
        live = b'{"future_field":{"nested":[true,null]},"known":1}'

        result = compare(desired, live, strategy="json")

        self.assertEqual(result.status, ComparisonStatus.EQUIVALENT_BUT_REFORMATTED)

    def test_json_array_order_is_semantic(self) -> None:
        result = compare(b'{"items":[1,2]}', b'{"items":[2,1]}', strategy="json")

        self.assertEqual(result.status, ComparisonStatus.DIFFERENT)

    def test_json_does_not_treat_boolean_as_number(self) -> None:
        result = compare(b'{"value":true}', b'{"value":1}', strategy="json")

        self.assertEqual(result.status, ComparisonStatus.DIFFERENT)

    def test_json_treats_equal_finite_number_spellings_as_equivalent(self) -> None:
        result = compare(b'{"value":1}', b'{"value":1.0}', strategy="json")

        self.assertEqual(
            result.status,
            ComparisonStatus.EQUIVALENT_BUT_REFORMATTED,
        )

    def test_json_reports_duplicate_object_key_as_unsupported(self) -> None:
        result = compare(
            b'{"profile":1,"profile":2}', b'{"profile":2}', strategy="json"
        )

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "duplicate_object_key")
        self.assertNotIn("profile", result.reason)

    def test_json_reports_nested_duplicate_object_key_as_unsupported(self) -> None:
        result = compare(
            b'{"outer":{"value":1,"value":2}}',
            b'{"outer":{"value":2}}',
            strategy="json",
        )

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "duplicate_object_key")

    def test_json_reports_invalid_utf8_as_unsupported(self) -> None:
        result = compare(b'{"value":\xff}', b'{"value":1}', strategy="json")

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "invalid_utf8")

    def test_json_reports_comments_as_unsupported(self) -> None:
        result = compare(b'{"value":1 // comment\n}', b'{"value":1}', strategy="json")

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "invalid_json")

    def test_json_reports_other_invalid_syntax_as_unsupported(self) -> None:
        result = compare(b'{"value":}', b'{"value":1}', strategy="json")

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "invalid_json")

    def test_json_rejects_non_finite_number_extensions(self) -> None:
        result = compare(b'{"value":NaN}', b'{"value":null}', strategy="json")

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "non_finite_number")

    def test_json_rejects_numbers_outside_supported_decimal_range(self) -> None:
        result = compare(
            b'{"value":1e999999999999999999999999999999}',
            b'{"value":1}',
            strategy="json",
        )

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "json_number_out_of_range")

    def test_json_rejects_isolated_unicode_surrogates(self) -> None:
        result = compare(
            b'{"value":"\\ud800"}',
            b'{"value":"ok"}',
            strategy="json",
        )

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "json_surrogate")

    def test_json_rejects_excessive_nesting_without_recursion_error(self) -> None:
        desired = b"[" * 129 + b"0" + b"]" * 129

        result = compare(desired, desired + b"\n", strategy="json")

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "json_nesting_limit")

    def test_json_requires_strict_json_even_when_bytes_match(self) -> None:
        result = compare(b"// comment\n", b"// comment\n", strategy="json")

        self.assertEqual(result.status, ComparisonStatus.UNSUPPORTED)
        self.assertEqual(result.code, "invalid_json")

    def test_result_serialization_contains_no_configuration_content(self) -> None:
        result = compare(
            b'{"secret":"do-not-return"}', b'{"secret":"changed"}', strategy="json"
        )

        payload = result.to_dict()
        self.assertEqual(
            set(payload), {"strategy", "status", "code", "reason", "equivalent"}
        )
        self.assertNotIn("do-not-return", repr(payload))
        self.assertNotIn("changed", repr(payload))

    def test_adapter_keeps_strategy_fixed_for_reconcile_injection(self) -> None:
        adapter = SemanticComparisonAdapter(ComparisonStrategy.JSON)

        result = adapter.compare(b"[1]", b"[1]\n")

        self.assertEqual(result.strategy, ComparisonStrategy.JSON)
        self.assertEqual(result.status, ComparisonStatus.EQUIVALENT_BUT_REFORMATTED)

    def test_adapter_rejects_non_bytes_inputs(self) -> None:
        adapter = SemanticComparisonAdapter()

        with self.assertRaises(TypeError):
            adapter.compare(cast(bytes, "value"), b"value")


if __name__ == "__main__":
    unittest.main()
