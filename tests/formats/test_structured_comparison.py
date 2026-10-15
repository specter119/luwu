"""Tests for structured comparison."""

from __future__ import annotations

import unittest
from decimal import Decimal

from luwu.m7_formats import (
    parse_document,
    semantic_equal,
    unselected_equal,
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
