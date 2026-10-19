"""Tests for json patch."""

from __future__ import annotations

import unittest

from luwu.errors import LuwuError
from luwu.formats import (
    MISSING,
    FormatError,
    get_leaf,
    parse_document,
    patch_document,
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

        with self.assertRaises(FormatError) as number_error:
            patch_document(document, {("a",): 10**5000})
        self.assertEqual(number_error.exception.code, "patch")
        self.assertIsNone(number_error.exception.__cause__)

        with self.assertRaises(FormatError) as path_error:
            patch_document(document, {("a",) * 1200: 1})
        self.assertEqual(path_error.exception.code, "path")
        self.assertIsNone(path_error.exception.__cause__)
