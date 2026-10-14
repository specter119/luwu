"""Focused contract tests for M7 structured document formatting."""

from __future__ import annotations

import subprocess
import sys
import tomllib
import unittest
from decimal import Decimal

from luwu.errors import LuwuError
from luwu.m7_formats import (
    MISSING,
    M7FormatError,
    get_leaf,
    parse_document,
    patch_document,
    semantic_equal,
    unselected_equal,
)


class TestM7JsonFormats(unittest.TestCase):
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


class TestM7TomlFormats(unittest.TestCase):
    def test_toml_inline_escaped_string_finishes_within_subprocess_timeout(
        self,
    ) -> None:
        cases = (
            (rb'a={text="x\ny"}' + b"\n", "a", "x\ny"),
            (
                rb'value = { text = "escaped\"quote" }' + b"\n",
                "value",
                'escaped"quote',
            ),
        )
        for source, table, expected in cases:
            with self.subTest(source=source):
                script = (
                    "from luwu.m7_formats import parse_document\n"
                    f"document = parse_document({source!r}, format='toml')\n"
                    f"assert document.value[{table!r}]['text'] == {expected!r}\n"
                )

                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=2,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_toml_reads_nested_tables_dotted_keys_and_literal_quoted_keys(self) -> None:
        source = (
            b"[service]\n"
            b'"a.b" = 7\n'
            b'nested.value = "old"\n'
            b'items = [{ label = "one" }, 2]\n'
            b"\n[service.child]\n"
            b"enabled = true\n"
        )
        document = parse_document(source, format="toml")

        self.assertEqual(get_leaf(document, ("service", "a.b")), 7)
        self.assertEqual(get_leaf(document, ("service", "nested", "value")), "old")
        self.assertEqual(get_leaf(document, ("service", "child", "enabled")), True)
        value = document.value
        assert isinstance(value, dict)
        service = value["service"]
        assert isinstance(service, dict)
        self.assertEqual(service["items"], [{"label": "one"}, 2])

    def test_toml_updates_and_deletes_spans_and_inserts_in_declared_table(self) -> None:
        source = (
            b"# header\r\n"
            b"[service]\r\n"
            b'title = "old"  # keep comment\r\n'
            b"remove = 2\r\n"
            b"array = [1,\r\n  2] # retained\r\n"
            b"\r\n[other]\r\n"
            b'name = "keep"\r\n'
        )
        document = parse_document(source, format="toml")

        changed = patch_document(
            document,
            {
                ("service", "title"): "new",
                ("service", "remove"): MISSING,
                ("service", "added"): Decimal("1.25"),
            },
        )
        reparsed = parse_document(changed, format="toml")

        self.assertEqual(get_leaf(reparsed, ("service", "title")), "new")
        self.assertEqual(get_leaf(reparsed, ("service", "remove")), MISSING)
        self.assertEqual(get_leaf(reparsed, ("service", "added")), Decimal("1.25"))
        self.assertEqual(get_leaf(reparsed, ("other", "name")), "keep")
        self.assertIn(b'title = "new"  # keep comment\r\n', changed)
        self.assertIn(b"array = [1,\r\n  2] # retained\r\n", changed)
        self.assertLess(changed.index(b"added = 1.25"), changed.index(b"[other]"))

    def test_toml_insertions_extend_implicit_tables_and_quote_literal_key_segments(
        self,
    ) -> None:
        source = b'"a.b".existing = 1\na.existing = 2\n'
        document = parse_document(source, format="toml")

        changed = patch_document(
            document,
            {
                ("a", "new.field"): Decimal("1E+3"),
                ("a.b", "quoted key"): "value",
            },
        )
        reparsed = parse_document(changed, format="toml")

        self.assertEqual(get_leaf(reparsed, ("a", "existing")), 2)
        self.assertEqual(get_leaf(reparsed, ("a", "new.field")), Decimal("1E+3"))
        self.assertEqual(get_leaf(reparsed, ("a.b", "existing")), 1)
        self.assertEqual(get_leaf(reparsed, ("a.b", "quoted key")), "value")
        self.assertIn(b'a."new.field" = 1E+3', changed)
        self.assertIn(b'"a.b"."quoted key" = "value"', changed)

    def test_toml_updates_literal_quoted_key_span(self) -> None:
        source = b'[service]\n"literal.key" = "old" # keep\n'
        document = parse_document(source, format="toml")

        changed = patch_document(document, {("service", "literal.key"): "new"})

        self.assertEqual(changed, b'[service]\n"literal.key" = "new" # keep\n')

    def test_toml_span_offsets_are_bytes_after_unicode(self) -> None:
        source = '# 🙂\n[service]\nvalue = "old"\n'.encode()

        changed = patch_document(
            parse_document(source, format="toml"),
            {("service", "value"): "new"},
        )

        self.assertEqual(changed, '# 🙂\n[service]\nvalue = "new"\n'.encode())

    def test_toml_inline_tables_support_nested_scalar_patches(self) -> None:
        source = (
            b'config = { model = "old", nested = { flag = true, keep = 2 }, '
            b"dotted.child = 1, arr = [1, 2] }\n"
        )
        document = parse_document(source, format="toml")

        changed = patch_document(
            document,
            {
                ("config", "model"): "new",
                ("config", "nested", "flag"): MISSING,
                ("config", "added"): "value",
                ("config", "new parent", "child"): Decimal("3.5"),
                ("config", "dotted", "new child"): 4,
            },
        )
        reparsed = parse_document(changed, format="toml")

        self.assertEqual(get_leaf(reparsed, ("config", "model")), "new")
        self.assertEqual(get_leaf(reparsed, ("config", "nested", "flag")), MISSING)
        self.assertEqual(get_leaf(reparsed, ("config", "nested", "keep")), 2)
        self.assertEqual(get_leaf(reparsed, ("config", "added")), "value")
        self.assertEqual(
            get_leaf(reparsed, ("config", "new parent", "child")), Decimal("3.5")
        )
        self.assertEqual(get_leaf(reparsed, ("config", "dotted", "child")), 1)
        self.assertEqual(get_leaf(reparsed, ("config", "dotted", "new child")), 4)
        value = reparsed.value
        assert isinstance(value, dict)
        config = value["config"]
        assert isinstance(config, dict)
        self.assertEqual(config["arr"], [1, 2])
        self.assertIn(b"arr = [1, 2]", changed)

    def test_toml_inline_dotted_table_insertion_extends_existing_child(self) -> None:
        source = b"a={b.c=1}\n"
        document = parse_document(source, format="toml")

        changed = patch_document(document, {("a", "b", "d"): 2})
        reparsed = parse_document(changed, format="toml")

        self.assertEqual(get_leaf(reparsed, ("a", "b", "c")), 1)
        self.assertEqual(get_leaf(reparsed, ("a", "b", "d")), 2)
        self.assertIn(b"b.d = 2", changed)

    def test_toml_multiline_quote_runs_remain_observable_during_other_edits(
        self,
    ) -> None:
        cases = (
            (b'a = """abc""""\nb = 1\n', 'abc"'),
            (b'a = """abc"""""\nb = 1\n', 'abc""'),
            (b"a = '''abc''''\nb = 1\n", "abc'"),
            (b"a = '''abc'''''\nb = 1\n", "abc''"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                parsed = tomllib.loads(source.decode("utf-8"))
                self.assertEqual(parsed["a"], expected)
                document = parse_document(source, format="toml")
                self.assertEqual(get_leaf(document, ("a",)), expected)

                changed = patch_document(document, {("b",): 2})
                self.assertEqual(changed, source.replace(b"b = 1", b"b = 2"))
                reparsed = parse_document(changed, format="toml")
                self.assertEqual(get_leaf(reparsed, ("a",)), expected)
                with self.assertRaises(M7FormatError):
                    patch_document(document, {("a",): "replaced"})

    def test_toml_inserts_missing_parent_using_literal_dotted_path(self) -> None:
        source = b"[existing]\nkeep = 1\n"
        document = parse_document(source, format="toml")

        changed = patch_document(document, {("new.parent", "leaf key"): "value"})
        reparsed = parse_document(changed, format="toml")

        self.assertEqual(get_leaf(reparsed, ("new.parent", "leaf key")), "value")
        self.assertTrue(changed.startswith(b'"new.parent"."leaf key" = "value"\n'))
        self.assertIn(source, changed)

    def test_toml_inserts_missing_nested_table_path_inside_existing_table(self) -> None:
        source = b"[service]\nname = 'keep'\n\n[other]\ncount = 1\n"

        changed = patch_document(
            parse_document(source, format="toml"),
            {("service", "new parent", "leaf"): "value"},
        )
        reparsed = parse_document(changed, format="toml")

        self.assertEqual(get_leaf(reparsed, ("service", "new parent", "leaf")), "value")
        self.assertLess(
            changed.index(b'"new parent".leaf = "value"'), changed.index(b"[other]")
        )

    def test_toml_observes_multiline_values_but_rejects_selected_edits(self) -> None:
        document = parse_document(
            b'[service]\ntext = """first\nsecond"""\narray = [1,\n 2]\n',
            format="toml",
        )

        self.assertEqual(get_leaf(document, ("service", "text")), "first\nsecond")
        value = document.value
        assert isinstance(value, dict)
        service = value["service"]
        assert isinstance(service, dict)
        self.assertEqual(service["array"], [1, 2])
        for path, value in (
            (("service", "text"), "changed"),
            (("service", "array"), [3]),
        ):
            with self.subTest(path=path), self.assertRaises(LuwuError):
                patch_document(document, {path: value})

    def test_toml_rejects_nonfinite_numbers_and_invalid_utf8(self) -> None:
        for source in (b"value = nan\n", b"value = inf\n"):
            with self.subTest(source=source), self.assertRaises(LuwuError):
                parse_document(source, format="toml")
        with self.assertRaises(LuwuError) as raised:
            parse_document(b"value = '\xff'\n", format="toml")
        self.assertNotIn("value =", str(raised.exception))

    def test_toml_array_path_and_container_selection_are_rejected(self) -> None:
        document = parse_document(b"items = [1, 2]\n", format="toml")

        with self.assertRaises(LuwuError):
            get_leaf(document, ("items",))
        with self.assertRaises(LuwuError):
            patch_document(document, {("items", "0"): 3})

    def test_insert_rejects_existing_scalar_or_array_ancestors(self) -> None:
        for format_name, source in (
            ("json", b'{"parent": 1}'),
            ("json", b'{"parent": []}'),
            ("toml", b"parent = 1\n"),
            ("toml", b"parent = []\n"),
        ):
            with self.subTest(format=format_name, source=source):
                document = parse_document(source, format=format_name)
                with self.assertRaises(LuwuError):
                    patch_document(document, {("parent", "child"): "value"})

    def test_overlapping_edit_paths_are_rejected(self) -> None:
        document = parse_document(b'{"a": 1, "b": {"child": 2}}', format="json")

        for edits in (
            {("a",): 2, ("a", "child"): 3},
            {("b",): MISSING, ("b", "child"): 3},
        ):
            with self.subTest(edits=edits), self.assertRaises(LuwuError):
                patch_document(document, edits)


class TestM7FormatComparison(unittest.TestCase):
    def test_semantic_equality_is_recursive_and_distinguishes_bool_from_number(
        self,
    ) -> None:
        self.assertTrue(
            semantic_equal(
                {"n": 1, "x": [Decimal("2.0")]}, {"x": [2], "n": Decimal("1.0")}
            )
        )
        self.assertFalse(semantic_equal(True, 1))
        self.assertFalse(semantic_equal({"enabled": True}, {"enabled": 1}))

    def test_unselected_comparison_masks_only_new_selected_ancestors(
        self,
    ) -> None:
        before = parse_document(b'{"unknown":{"kept":[1]},"empty":{}}', format="json")
        after = parse_document(
            b'{"unknown":{"kept":[1]},"empty":{"new":{"leaf":2}}}',
            format="json",
        )
        self.assertTrue(unselected_equal(before, after, (("empty", "new", "leaf"),)))

        missing_before = parse_document(b'{"keep":1}', format="json")
        missing_after = parse_document(
            b'{"keep":1,"created":{"nested":{"leaf":2}}}', format="json"
        )
        self.assertTrue(
            unselected_equal(
                missing_before, missing_after, (("created", "nested", "leaf"),)
            )
        )

        changed_unknown = parse_document(
            b'{"unknown":{"kept":[2]},"empty":{"new":{"leaf":2}}}',
            format="json",
        )
        self.assertFalse(
            unselected_equal(before, changed_unknown, (("empty", "new", "leaf"),))
        )

    def test_unselected_comparison_keeps_unknown_empty_containers_arrays_and_types(
        self,
    ) -> None:
        for before_source, after_source in (
            (
                b'{"selected":1,"unknown":{"empty":{}}}',
                b'{"selected":2,"unknown":{}}',
            ),
            (b'{"selected":1,"unknown":[]}', b'{"selected":2,"unknown":{}}'),
            (
                b'{"selected":1,"unknown":"old"}',
                b'{"selected":2,"unknown":{"nested":{}}}',
            ),
        ):
            with self.subTest(before=before_source, after=after_source):
                before = parse_document(before_source, format="json")
                after = parse_document(after_source, format="json")
                self.assertFalse(unselected_equal(before, after, (("selected",),)))

    def test_selected_path_cannot_hide_an_existing_array_ancestor_change(self) -> None:
        before = parse_document(b'{"parent":[]}', format="json")
        after = parse_document(b'{"parent":{"child":2}}', format="json")

        self.assertFalse(unselected_equal(before, after, (("parent", "child"),)))

    def test_unselected_comparison_does_not_hide_ancestors_without_selected_leaf(
        self,
    ) -> None:
        before = parse_document(b"{}", format="json")
        empty_ancestor = parse_document(b'{"a":{}}', format="json")
        selected_insert = parse_document(b'{"a":{"b":2}}', format="json")
        paths = [("a", "b")]

        self.assertFalse(unselected_equal(before, empty_ancestor, paths))
        self.assertFalse(unselected_equal(empty_ancestor, before, paths))
        self.assertTrue(unselected_equal(before, selected_insert, paths))


if __name__ == "__main__":
    unittest.main()
