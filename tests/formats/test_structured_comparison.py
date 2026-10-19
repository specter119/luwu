"""Tests for structured comparison."""

from __future__ import annotations

import unittest
from decimal import Decimal

from luwu.formats import (
    parse_document,
    semantic_equal,
    unselected_equal,
    unselected_scalar_changes,
)


class StructuredComparisonV8Tests(unittest.TestCase):
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


class UnselectedScalarChangeTests(unittest.TestCase):
    def _changes(self, left: bytes, right: bytes, paths=()):
        return unselected_scalar_changes(
            parse_document(left, format="json"),
            parse_document(right, format="json"),
            paths,
        )

    def test_changed_and_added_scalars_are_listed_by_path(self) -> None:
        self.assertEqual(
            self._changes(
                b'{"a":{"x":1},"keep":true}',
                b'{"a":{"x":2,"y":"new"},"keep":true,"t":{"u":{"v":null}}}',
            ),
            {("a", "x"): 2, ("a", "y"): "new", ("t", "u", "v"): None},
        )

    def test_selected_paths_are_masked(self) -> None:
        self.assertEqual(
            self._changes(b'{"s":1,"o":1}', b'{"s":2,"o":1}', (("s",),)), {}
        )

    def test_anything_but_scalar_changes_is_not_representable(self) -> None:
        for left, right in (
            (b'{"a":1,"b":2}', b'{"a":1}'),
            (b'{"a":[1]}', b'{"a":[2]}'),
            (b'{"a":1}', b'{"a":1,"b":[1]}'),
            (b'{"a":1}', b'{"a":1,"b":{}}'),
            (b'{"a":1}', b'{"a":{"b":1}}'),
            (b'{"a":{"b":1}}', b'{"a":1}'),
        ):
            with self.subTest(left=left, right=right):
                self.assertIsNone(self._changes(left, right))
