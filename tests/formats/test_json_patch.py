"""Tests for json patch."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from luwu.errors import LuwuError, MutationError
from luwu.m6_formats import parse_m6_document, patch_m6_document
from luwu.m7_formats import (
    MISSING,
    M7FormatError,
    get_leaf,
    parse_document,
    patch_document,
)
from luwu.reverse_sync import SourcePatch, build_source_patch


class SelectiveLiteralJsonPatchTests(unittest.TestCase):
    def test_replace_preserves_unselected_and_undeclared_lexical_bytes(self) -> None:
        source = (
            b" \r\n{\r\n"
            b'  "setting" : 1.00,\r\n'
            b'  "\\u0072untime" : 1e+0,\r\n'
            b'  "undeclared" : "\\u0061",\r\n'
            b'  "nested" : {"value": [1, {"punctuation": "}, {, :"}]}\r\n'
            b"}\r\n"
        )
        live = (
            b'{"setting": 1, "runtime": 2, "undeclared": "a", '
            b'"nested": {"value": [1, {"punctuation": "}, {, :"}]}}'
        )

        patch = self._build(source, live, "runtime")

        self.assertEqual(patch.data, source.replace(b"1e+0", b"2", 1))
        self.assertEqual(
            patch.to_dict()["changes"],
            [
                {
                    "key": "runtime",
                    "operation": "replace",
                    "separator_adjusted": False,
                }
            ],
        )
        self.assertIsInstance(patch, SourcePatch)
        self.assertNotIn("1e+0", repr(patch))
        self.assertNotIn("undeclared", json.dumps(patch.to_dict()))

    def test_replace_handles_nested_value_and_crlf_without_final_newline(self) -> None:
        source = (
            b'{\r\n  "keep": 1.00,\r\n'
            b'  "runtime": {"nested": [1, "x, }"]},\r\n'
            b'  "undeclared": "\\u0061"\r\n}'
        )
        live = b'{"runtime":{"nested":[2,"y"]},"keep":1,"undeclared":"a"}'

        patch = self._build(source, live, "runtime")

        self.assertEqual(
            patch.data,
            b'{\r\n  "keep": 1.00,\r\n'
            b'  "runtime": {"nested":[2,"y"]},\r\n'
            b'  "undeclared": "\\u0061"\r\n}',
        )
        self.assertFalse(patch.data.endswith(b"\n"))

    def test_delete_handles_first_middle_last_and_unique_members(self) -> None:
        cases = (
            (
                "first",
                b'{"first": 1, "middle": 2, "last": 3}',
                b'{"middle": 2, "last": 3}',
                b'{"middle": 2, "last": 3}',
                True,
            ),
            (
                "middle",
                b'{"first": 1, "middle": 2, "last": 3}',
                b'{"first": 1, "last": 3}',
                b'{"first": 1, "last": 3}',
                True,
            ),
            (
                "last",
                b'{"first": 1, "middle": 2, "last": 3}',
                b'{"first": 1, "middle": 2}',
                b'{"first": 1, "middle": 2}',
                True,
            ),
            (
                "only",
                b'{"only": 1}',
                b"{}",
                b"{}",
                False,
            ),
        )
        for selected, source, live, expected, separator_adjusted in cases:
            with self.subTest(selected=selected):
                patch = self._build(source, live, selected)
                self.assertEqual(patch.data, expected)
                self.assertEqual(
                    patch.to_dict()["changes"][0],
                    {
                        "key": selected,
                        "operation": "delete",
                        "separator_adjusted": separator_adjusted,
                    },
                )

    def test_add_handles_empty_and_nonempty_objects(self) -> None:
        cases = (
            (
                b"{}",
                b'{"new": 2}',
                b'{"new":2}',
                False,
            ),
            (
                b"{\r\n}",
                b'{"new": 2}',
                b'{\r\n"new":2}',
                False,
            ),
            (
                b'{"keep": 1}',
                b'{"keep": 1, "new": 2}',
                b'{"keep": 1, "new":2}',
                True,
            ),
        )
        for source, live, expected, separator_adjusted in cases:
            with self.subTest(source=source):
                patch = self._build(source, live, "new")
                self.assertEqual(patch.data, expected)
                self.assertEqual(
                    patch.to_dict()["changes"][0],
                    {
                        "key": "new",
                        "operation": "add",
                        "separator_adjusted": separator_adjusted,
                    },
                )

    def test_multiple_edits_keep_existing_member_bytes(self) -> None:
        source = (
            b'{"remove": 1.00, "keep": "\\u0061", '
            b'"replace": 1e+0, "undeclared": {"text": "x, }"}}'
        )
        live = (
            b'{"keep": "a", "replace": 2, "undeclared": {"text": "x, }"}, "add": true}'
        )

        patch = self._build(source, live, "remove", "replace", "add")

        self.assertEqual(
            patch.data,
            b'{"keep": "\\u0061", "replace": 2, '
            b'"undeclared": {"text": "x, }"}, "add":true}',
        )
        self.assertIn(b'"keep": "\\u0061"', patch.data)
        self.assertIn(b'"undeclared": {"text": "x, }"}', patch.data)
        self.assertEqual(
            patch.to_dict()["changes"],
            [
                {
                    "key": "remove",
                    "operation": "delete",
                    "separator_adjusted": True,
                },
                {
                    "key": "replace",
                    "operation": "replace",
                    "separator_adjusted": False,
                },
                {
                    "key": "add",
                    "operation": "add",
                    "separator_adjusted": True,
                },
            ],
        )

    def test_rejects_dynamic_nested_or_alias_inputs_before_any_patch(self) -> None:
        valid_live = b'{"runtime": 2}'
        cases = (
            (
                b'{"runtime": {{ runtime_value }}}',
                valid_live,
                {"runtime": "live"},
                {"runtime": "live"},
                {"runtime": "runtime"},
            ),
            (
                b'{"runtime": 1}',
                valid_live,
                {"runtime": "live"},
                {"runtime": "live"},
                {"runtime": "settings.runtime"},
            ),
            (
                b'{"runtime": 1, "\\u0072untime": 2}',
                valid_live,
                {"runtime": "live"},
                {"runtime": "live"},
                {"runtime": "runtime"},
            ),
            (
                b'{"runtime": 1,}',
                valid_live,
                {"runtime": "live"},
                {"runtime": "live"},
                {"runtime": "runtime"},
            ),
        )
        for source, live, fields, owners, reverse_sync in cases:
            with self.subTest(source=source, reverse_sync=reverse_sync):
                with self.assertRaises(MutationError) as context:
                    build_source_patch(
                        source,
                        live,
                        fields=fields,
                        owners=owners,
                        reverse_sync=reverse_sync,
                        selected_fields=("runtime",),
                    )
                self.assertIn(
                    context.exception.code,
                    {"reverse_sync_unsupported", "reverse_sync_mapping"},
                )

    def test_rejects_invalid_live_json_without_returning_a_patch(self) -> None:
        with self.assertRaises(MutationError) as context:
            self._build(b'{"runtime": 1}', b'{"runtime": 2,}', "runtime")
        self.assertEqual(context.exception.code, "reverse_sync_unsupported")

    def _build(self, source: bytes, live: bytes, *selected_fields: str) -> SourcePatch:
        fields = {name: "live" for name in selected_fields}
        return build_source_patch(
            source,
            live,
            fields=fields,
            owners=fields,
            reverse_sync={name: name for name in selected_fields},
            selected_fields=selected_fields,
        )


class JsonFormatsV8Tests(unittest.TestCase):
    def test_json_reads_nested_values_with_literal_tuple_paths(self) -> None:
        document = parse_document(
            b'{"a.b":{"x":1,"keep":[true,{"safe":null}]},"a":{"b":"v"}}',
            format="json",
        )

        self.assertEqual(get_leaf(document, ("a.b", "x")), 1)
        self.assertEqual(get_leaf(document, ("a", "b")), "v")
        self.assertEqual(get_leaf(document, ("missing", "child")), MISSING)
        value = document.value
        assert isinstance(value, dict)
        dotted_key = value["a.b"]
        assert isinstance(dotted_key, dict)
        self.assertEqual(dotted_key["keep"], [True, {"safe": None}])

    def test_json_update_changes_only_the_selected_scalar_bytes(self) -> None:
        source = b'{"keep":"\xf0\x9f\x99\x82", "name" : "old"}\r\n'
        document = parse_document(source, format="json")

        changed = patch_document(document, {("name",): "new"})

        self.assertEqual(changed, b'{"keep":"\xf0\x9f\x99\x82", "name" : "new"}\r\n')

    def test_jsonc_keeps_comments_while_updating_inserting_and_deleting(self) -> None:
        source = (
            b"{\n"
            b"  // object comment\n"
            b'  "section": {"model": "old", "remove": true, "keep": [1, 2]},\n'
            b'  "unknown": {"nested": false},\n'
            b"}\n"
        )
        document = parse_document(source, format="jsonc")

        changed = patch_document(
            document,
            {
                ("section", "model"): "new",
                ("section", "remove"): MISSING,
                ("section", "added"): 3,
                ("created", "nested", "flag"): True,
            },
        )
        reparsed = parse_document(changed, format="jsonc")

        self.assertEqual(get_leaf(reparsed, ("section", "model")), "new")
        self.assertEqual(get_leaf(reparsed, ("section", "remove")), MISSING)
        self.assertEqual(get_leaf(reparsed, ("section", "added")), 3)
        self.assertEqual(get_leaf(reparsed, ("created", "nested", "flag")), True)
        self.assertEqual(reparsed.value["unknown"], {"nested": False})
        self.assertIn(b"// object comment", changed)
        self.assertIn(b'"keep": [1, 2]', changed)

    def test_jsonc_deleting_last_member_preserves_a_valid_trailing_comma(self) -> None:
        source = b'{"first": 1, "remove": 2,}'

        changed = patch_document(
            parse_document(source, format="jsonc"), {("remove",): MISSING}
        )

        self.assertEqual(changed, b'{"first": 1, }')
        self.assertEqual(parse_document(changed, format="jsonc").value, {"first": 1})

    def test_jsonc_insert_does_not_duplicate_a_comment_after_colon(self) -> None:
        source = b'{"first": /* keep once */ 1}'

        changed = patch_document(
            parse_document(source, format="jsonc"), {("added",): 2}
        )

        self.assertEqual(changed.count(b"/* keep once */"), 1)
        self.assertEqual(
            parse_document(changed, format="jsonc").value,
            {"first": 1, "added": 2},
        )

    def test_jsonc_delete_preserves_comment_between_key_and_value(self) -> None:
        source = b'{"a" /*KEEP*/ : 1,"b":2}'

        changed = patch_document(
            parse_document(source, format="jsonc"), {("a",): MISSING}
        )

        self.assertIn(b"/*KEEP*/", changed)
        self.assertEqual(get_leaf(parse_document(changed, format="jsonc"), ("b",)), 2)

    def test_json_inserts_into_empty_nested_objects_and_keeps_boolean_type(
        self,
    ) -> None:
        document = parse_document(b'{"empty": {\n  }, "flag": 0}', format="json")

        changed = patch_document(
            document,
            {("empty", "new.key"): "🙂", ("flag",): True},
        )
        reparsed = parse_document(changed, format="json")

        self.assertEqual(get_leaf(reparsed, ("empty", "new.key")), "🙂")
        self.assertIs(get_leaf(reparsed, ("flag",)), True)
        self.assertIn(b'"new.key": "\xf0\x9f\x99\x82"', changed)

    def test_json_rejects_comments_trailing_commas_duplicates_and_nonfinite_values(
        self,
    ) -> None:
        invalid = (
            b'{"key": 1 // comment\n}',
            b'{"key": 1,}',
            b'{"key": 1, "\\u006bey": 2}',
            b'{"key": NaN}',
            b'{"key": 1e999999999999999999999999}',
        )
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(LuwuError):
                parse_document(source, format="json")

    def test_sensitive_keys_are_rejected_recursively_in_objects_and_arrays(
        self,
    ) -> None:
        for format_name, source in (
            ("json", b'{"safe":[{"clientSecret":"sentinel"}]}'),
            ("json", b'{"safe":{"\\u0074oken":"sentinel"}}'),
            ("jsonc", b'{"safe": {"nested": {"api-key": "sentinel"}}}'),
            ("toml", b"[safe.nested]\nprivate_key = 'sentinel'\n"),
            ("toml", b'[safe]\n"api\\u005fkey" = "sentinel"\n'),
        ):
            with (
                self.subTest(format=format_name),
                self.assertRaises(LuwuError) as raised,
            ):
                parse_document(source, format=format_name)
            self.assertNotIn("sentinel", str(raised.exception))
            self.assertNotIn("private_key", str(raised.exception))

    def test_array_members_and_container_values_are_not_leaf_edits(self) -> None:
        document = parse_document(b'{"items":[1,2],"object":{"x":1}}', format="json")

        for path in (("items",), ("object",)):
            with self.subTest(path=path), self.assertRaises(LuwuError):
                get_leaf(document, path)
        with self.assertRaises(LuwuError):
            patch_document(document, {("items", "0"): 3})

    def test_patch_rejects_out_of_range_integer_and_overdeep_path_safely(self) -> None:
        document = parse_document(b'{"a":1}', format="json")

        with self.assertRaises(M7FormatError) as number_error:
            patch_document(document, {("a",): 10**5000})
        self.assertEqual(number_error.exception.code, "m7_patch")
        self.assertIsNone(number_error.exception.__cause__)

        with self.assertRaises(M7FormatError) as path_error:
            patch_document(document, {("a",) * 1200: 1})
        self.assertEqual(path_error.exception.code, "m7_path")
        self.assertIsNone(path_error.exception.__cause__)


class JsonFormatsV7Tests(unittest.TestCase):
    def test_json_patch_preserves_unselected_bytes_and_absent_null(self) -> None:
        source = b'{"model": "old", "nullable": null, "keep": 1}\n'

        changed = patch_m6_document(source, format="json", updates={"model": "new"})
        deleted = patch_m6_document(source, format="json", deletes=("nullable",))

        self.assertEqual(changed, b'{"model": "new", "nullable": null, "keep": 1}\n')
        self.assertEqual(deleted, b'{"model": "old", "keep": 1}\n')

    def test_json_duplicate_or_invalid_input_fails_closed(self) -> None:
        with self.assertRaises(MutationError):
            patch_m6_document(
                b'{"model": 1, "model": 2}', format="json", updates={"model": 3}
            )
        with self.assertRaises(MutationError):
            patch_m6_document(
                b'{"model": "\xff"}', format="json", updates={"model": "x"}
            )

    def test_json_numbers_keep_decimal_precision_and_types(self) -> None:
        source = b'{"precise": 0.10000000000000001, "whole": 1, "flag": true}\n'

        parsed = parse_m6_document(source, format="json")
        changed = patch_m6_document(
            source,
            format="json",
            updates={"precise": Decimal("1234567890.1234567890123456789")},
        )

        self.assertEqual(parsed["precise"], Decimal("0.10000000000000001"))
        self.assertEqual(type(parsed["whole"]), Decimal)
        self.assertIs(parsed["flag"], True)
        self.assertIn(b"1234567890.1234567890123456789", changed)
