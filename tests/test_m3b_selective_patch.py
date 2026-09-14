from __future__ import annotations

import json
import unittest

from luwu.errors import MutationError
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


if __name__ == "__main__":
    unittest.main()
