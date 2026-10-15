"""Tests for toml patch."""

from __future__ import annotations

import subprocess
import sys
import tomllib
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


class TomlFormatsV8Tests(unittest.TestCase):
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


class TomlFormatsV7Tests(unittest.TestCase):
    def test_toml_patch_preserves_comments_and_rejects_tables(self) -> None:
        source = b'# keep\nmodel = "old"\nprovider = "x"\n'

        changed = patch_m6_document(source, format="toml", updates={"model": "new"})
        added = patch_m6_document(source, format="toml", updates={"changelog": "on"})
        deleted = patch_m6_document(source, format="toml", deletes=("provider",))

        self.assertEqual(changed, b'# keep\nmodel = "new"\nprovider = "x"\n')
        self.assertIn(b"# keep\n", added)
        self.assertIn(b'changelog = "on"\n', added)
        self.assertEqual(deleted, b'# keep\nmodel = "old"\n')
        with self.assertRaises(MutationError):
            patch_m6_document(
                b'[provider]\nmodel = "x"\n', format="toml", updates={"model": "y"}
            )

    def test_toml_requires_standard_syntax_before_parsing_or_patching(self) -> None:
        cases = (
            b"model = 01\n",
            b"model = 1__0\n",
            b'model = "\\/"\n',
            b'model = "\\uD800"\n',
        )
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(MutationError) as parse_error:
                    parse_m6_document(source, format="toml")
                self.assertEqual(parse_error.exception.code, "m6_format")
                self.assertNotIn("model =", str(parse_error.exception))

                with self.assertRaises(MutationError) as patch_error:
                    patch_m6_document(source, format="toml", updates={"model": "new"})
                self.assertEqual(patch_error.exception.code, "m6_format")
                self.assertNotIn("model =", str(patch_error.exception))

    def test_toml_uses_toml_unicode_escapes_and_keeps_decimal_precision(self) -> None:
        source = (
            b'# keep\r\nmodel = "\\U0001F642"\r\n'
            b"precise = 0.1000000000000000000000000001"
        )

        parsed = parse_m6_document(source, format="toml")
        changed = patch_m6_document(source, format="toml", updates={"model": "updated"})

        self.assertEqual(parsed["model"], "🙂")
        self.assertEqual(parsed["precise"], Decimal("0.1000000000000000000000000001"))
        self.assertIs(type(parsed["precise"]), Decimal)
        self.assertEqual(
            changed,
            b'# keep\r\nmodel = "updated"\r\nprecise = 0.1000000000000000000000000001',
        )

    def test_toml_hash_inside_string_is_not_an_inline_comment(self) -> None:
        source = b'# keep\r\nmodel = "old#value"\r\nprovider = "x"'

        changed = patch_m6_document(
            source, format="toml", updates={"model": "new#value"}
        )

        self.assertEqual(
            changed,
            b'# keep\r\nmodel = "new#value"\r\nprovider = "x"',
        )

    def test_toml_string_token_consumes_escaped_quotes_and_unicode(self) -> None:
        source = b'model = "say \\"hi\\" # \\U0001F642"\nprovider = "old"\n'

        parsed = parse_m6_document(source, format="toml")
        changed = patch_m6_document(source, format="toml", updates={"provider": "new"})

        self.assertEqual(parsed["model"], 'say "hi" # 🙂')
        self.assertEqual(
            changed,
            b'model = "say \\"hi\\" # \\U0001F642"\nprovider = "new"\n',
        )

    def test_toml_inline_comments_preserve_exact_non_value_bytes(self) -> None:
        source = (
            b'# header\r\nmodel = "say \\"hi\\" # inside" \t# "keep"\r\n'
            b"provider = 'old#value' # literal comment"
        )

        parsed = parse_m6_document(source, format="toml")
        changed = patch_m6_document(
            source,
            format="toml",
            updates={"model": "updated", "provider": "next"},
        )

        self.assertEqual(parsed["model"], 'say "hi" # inside')
        self.assertEqual(parsed["provider"], "old#value")
        self.assertEqual(
            changed,
            b'# header\r\nmodel = "updated" \t# "keep"\r\n'
            b'provider = "next" # literal comment',
        )

    def test_toml_inline_comments_preserve_bool_int_decimal_bytes(self) -> None:
        cases = (
            (
                "bool",
                "enabled",
                "enabled = true# 开关注释  ".encode(),
                True,
                False,
                "enabled = false# 开关注释  ".encode(),
            ),
            (
                "int",
                "count",
                "count = 7  # 计数注释\t  ".encode(),
                7,
                12,
                "count = 12  # 计数注释\t  ".encode(),
            ),
            (
                "Decimal",
                "ratio",
                "ratio = 0.25# 小数注释  ".encode(),
                Decimal("0.25"),
                Decimal("1.125"),
                "ratio = 1.125# 小数注释  ".encode(),
            ),
        )

        for label, field, source, original, updated, expected in cases:
            with self.subTest(value_type=label):
                parsed = parse_m6_document(source, format="toml")
                changed = patch_m6_document(
                    source, format="toml", updates={field: updated}
                )

                self.assertEqual(parsed[field], original)
                self.assertIs(type(parsed[field]), type(original))
                self.assertEqual(changed, expected)

    def test_toml_unsupported_lexical_forms_fail_closed(self) -> None:
        cases = (
            b'"model" = "x"\n',
            b'model = """x\ny"""\n',
            b'model = "x" trailing\n',
            b'model = "x"\nnot-an-assignment\n',
            b'model = ["x"]\n',
            b"model = 1\nmodel = 2\n",
            b"model = inf\n",
            b"model = nan\n",
        )
        for source in cases:
            with self.subTest(source=source), self.assertRaises(MutationError):
                parse_m6_document(source, format="toml")

    def test_toml_patch_updates_and_deletes_without_touching_unselected_bytes(
        self,
    ) -> None:
        source = (
            b'# keep\r\nmodel = "old"\r\n'
            b'remove = "gone" # deleted with its assignment\r\n'
            b"keep = 12345678901234567890"
        )

        changed = patch_m6_document(
            source,
            format="toml",
            updates={"model": "new"},
            deletes=("remove",),
        )

        self.assertEqual(
            changed,
            b'# keep\r\nmodel = "new"\r\nkeep = 12345678901234567890',
        )
