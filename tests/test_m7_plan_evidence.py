"""Focused tests for immutable M7 plan graph evidence."""

from __future__ import annotations

import copy
import traceback
import unittest
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
from unittest.mock import patch

from luwu.m7_formats import (
    Document,
    _JsonMember,
    _Node,
    _TomlAssignment,
    _TomlHeader,
    _TomlInlineMember,
    _TomlInlineTable,
    parse_document,
)
from luwu.m7_plan_evidence import (
    _capture_plan_evidence,
    _matches_plan_evidence,
    _NodeRecord,
    _NodeReference,
    _PlanEvidence,
    _PlanEvidenceError,
    _ScalarEvidence,
)

_PARSER_NODE_TYPES = (
    _Node,
    _JsonMember,
    _TomlAssignment,
    _TomlHeader,
    _TomlInlineMember,
    _TomlInlineTable,
)


class M7PlanEvidenceTests(unittest.TestCase):
    def test_capture_is_immutable_and_contains_no_mutable_source_nodes(self) -> None:
        projection = _projection()
        evidence = _capture_plan_evidence(projection)

        self.assertIs(type(evidence), _PlanEvidence)
        self.assertTrue(_mutable_ids(projection))
        self.assertFalse(_mutable_ids(projection) & _mutable_ids(evidence))
        self.assertFalse(_contains_mutable(evidence))
        self.assertNotIn("settings", repr(evidence))
        evidence_field = "records"
        with self.assertRaises(FrozenInstanceError):
            setattr(evidence, evidence_field, ())

        original = copy.deepcopy(projection)
        metadata = projection["metadata"]
        assert isinstance(metadata, dict)
        metadata["name"] = "changed"
        self.assertTrue(_matches_plan_evidence(original, evidence))
        self.assertFalse(_matches_plan_evidence(projection, evidence))

    def test_independently_reparsed_projection_matches(self) -> None:
        first = _projection()
        second = _projection()
        first_documents = first["documents"]
        second_documents = second["documents"]
        assert isinstance(first_documents, list)
        assert isinstance(second_documents, list)
        first_document = first_documents[0]
        second_document = second_documents[0]
        assert isinstance(first_document, dict)
        assert isinstance(second_document, dict)
        self.assertIsNot(first_document["root"], second_document["root"])

        evidence = _capture_plan_evidence(first)

        self.assertTrue(_matches_plan_evidence(second, evidence))

    def test_equal_node_splitting_and_merging_are_allowed(self) -> None:
        shared = {"items": [1, "two", Decimal("3.00")]}
        captured_graph = {"left": shared, "right": shared}
        evidence = _capture_plan_evidence(captured_graph)

        split_graph = {
            "left": {"items": [1, "two", Decimal("3.00")]},
            "right": {"items": [1, "two", Decimal("3.00")]},
        }
        self.assertTrue(_matches_plan_evidence(split_graph, evidence))

        split_evidence = _capture_plan_evidence(split_graph)
        merged = {"items": [1, "two", Decimal("3.00")]}
        merged_graph = {"left": merged, "right": merged}
        self.assertTrue(_matches_plan_evidence(merged_graph, split_evidence))

        self.assertTrue(
            _matches_plan_evidence(
                {"z": 1, "a": 2}, _capture_plan_evidence({"a": 2, "z": 1})
            )
        )
        self.assertFalse(_matches_plan_evidence([1], _capture_plan_evidence((1,))))

    def test_comparison_memo_pairs_current_and_expected_nodes(self) -> None:
        evidence = _capture_plan_evidence({"left": {"value": 1}, "right": {"value": 2}})
        shared = {"value": 1}

        self.assertFalse(
            _matches_plan_evidence({"left": shared, "right": shared}, evidence)
        )

    def test_exact_scalar_types_and_decimal_representations_are_preserved(self) -> None:
        for value in (None, True, 1, "text", b"bytes", Decimal("1.0")):
            with self.subTest(value_type=type(value).__name__, value=value):
                evidence = _capture_plan_evidence(value)
                self.assertTrue(_matches_plan_evidence(value, evidence))

        self.assertFalse(_matches_plan_evidence(1, _capture_plan_evidence(True)))
        self.assertFalse(_matches_plan_evidence(True, _capture_plan_evidence(1)))
        self.assertFalse(
            _matches_plan_evidence(
                Decimal("1.00"), _capture_plan_evidence(Decimal("1.0"))
            )
        )
        self.assertFalse(
            _matches_plan_evidence(Decimal(0), _capture_plan_evidence(Decimal("-0")))
        )
        self.assertFalse(
            _matches_plan_evidence(Decimal("-0"), _capture_plan_evidence(Decimal(0)))
        )

    def test_matching_scalars_does_not_build_scalar_evidence_records(self) -> None:
        evidence = _capture_plan_evidence({"value": [1, True, Decimal("1.00")]})

        with patch(
            "luwu.m7_plan_evidence._scalar_evidence",
            side_effect=AssertionError("capture helper used during matching"),
        ):
            self.assertTrue(
                _matches_plan_evidence({"value": [1, True, Decimal("1.00")]}, evidence)
            )

    def test_every_parser_node_field_is_compared(self) -> None:
        projection = _projection()
        parser_nodes = _parser_nodes(projection)
        self.assertEqual({type(node) for node in parser_nodes}, set(_PARSER_NODE_TYPES))
        self.assertEqual(
            sum(len(fields(node_type)) for node_type in _PARSER_NODE_TYPES), 35
        )
        evidence = _capture_plan_evidence(projection)

        for node_index, original_node in enumerate(parser_nodes):
            assert isinstance(
                original_node,
                (
                    _Node,
                    _JsonMember,
                    _TomlAssignment,
                    _TomlHeader,
                    _TomlInlineMember,
                    _TomlInlineTable,
                ),
            )
            for node_field in fields(original_node):
                with self.subTest(
                    node_type=type(original_node).__name__, field=node_field.name
                ):
                    current = copy.deepcopy(projection)
                    current_node = _parser_nodes(current)[node_index]
                    changed = _changed_value(getattr(current_node, node_field.name))
                    object.__setattr__(current_node, node_field.name, changed)

                    self.assertFalse(_matches_plan_evidence(current, evidence))

    def test_values_and_spans_in_parser_nodes_change_match_result(self) -> None:
        projection = _projection()
        evidence = _capture_plan_evidence(projection)
        current = copy.deepcopy(projection)
        node = next(node for node in _parser_nodes(current) if type(node) is _Node)
        assert isinstance(node, _Node)
        assert isinstance(node.value, dict)
        node.value = {"changed": True}

        self.assertFalse(_matches_plan_evidence(current, evidence))

        span_projection = _projection()
        span_evidence = _capture_plan_evidence(span_projection)
        span_current = copy.deepcopy(span_projection)
        span_node = next(
            node
            for node in _parser_nodes(span_current)
            if type(node) is _Node and node.start is not None
        )
        assert isinstance(span_node, _Node)
        assert isinstance(span_node.start, int)
        span_node.start += 1

        self.assertFalse(_matches_plan_evidence(span_current, span_evidence))

    def test_deep_shared_dag_capture_and_match_are_iterative(self) -> None:
        captured = {"graph": _deep_dag(1_500)}
        current = {"graph": _deep_dag(1_500)}

        evidence = _capture_plan_evidence(captured)

        self.assertTrue(_matches_plan_evidence(current, evidence))

    def test_mismatch_rejects_without_requiring_later_edges_to_be_read(self) -> None:
        marker = "EVIDENCE_PRIVATE_MARKER"
        evidence = _capture_plan_evidence({"first": 1, "later": "safe"})

        try:
            matched = _matches_plan_evidence(
                {"first": 2, "later": _Unprintable(marker)}, evidence
            )
        except _PlanEvidenceError as error:
            _assert_fixed_error(self, error, marker)
        else:
            self.assertFalse(matched)

    def test_successful_prefix_still_reads_all_later_current_edges(self) -> None:
        marker = "EVIDENCE_PRIVATE_MARKER"
        evidence = _capture_plan_evidence({"first": 1, "later": "safe"})

        with self.assertRaises(_PlanEvidenceError) as raised:
            _matches_plan_evidence(
                {"first": 1, "later": _Unprintable(marker)}, evidence
            )

        _assert_fixed_error(self, raised.exception, marker)

    def test_unsupported_types_cycles_and_nonfinite_decimal_fail_closed(self) -> None:
        class IntSubclass(int):
            pass

        class ListSubclass(list[object]):
            pass

        class DictSubclass(dict[str, object]):
            pass

        class TupleSubclass(tuple[object, ...]):
            pass

        class StrSubclass(str):
            pass

        class BytesSubclass(bytes):
            pass

        class DecimalSubclass(Decimal):
            pass

        class NodeSubclass(_Node):
            pass

        values: tuple[object, ...] = (
            IntSubclass(3),
            ListSubclass([1]),
            DictSubclass({"value": 1}),
            TupleSubclass((1,)),
            StrSubclass("text"),
            BytesSubclass(b"bytes"),
            DecimalSubclass("1.0"),
            NodeSubclass("value"),
            bytearray(b"bytes"),
            Decimal("NaN"),
            {object(): "value"},
        )
        marker = "EVIDENCE_PRIVATE_MARKER"

        for value in values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(_PlanEvidenceError) as raised:
                    _capture_plan_evidence(value)
                _assert_fixed_error(self, raised.exception, marker)

        cycle: list[object] = []
        cycle.append(cycle)
        with self.assertRaises(_PlanEvidenceError) as raised:
            _capture_plan_evidence(cycle)
        _assert_fixed_error(self, raised.exception, marker)

        valid_evidence = _capture_plan_evidence([])
        cycle.clear()
        cycle.append(cycle)
        try:
            matched = _matches_plan_evidence(cycle, valid_evidence)
        except _PlanEvidenceError as error:
            _assert_fixed_error(self, error, marker)
        else:
            self.assertFalse(matched)


class _Unprintable:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __repr__(self) -> str:
        raise AssertionError(self.marker)


def _assert_fixed_error(
    test: unittest.TestCase, error: _PlanEvidenceError, marker: str
) -> None:
    test.assertEqual(str(error), "plan graph evidence is invalid")
    test.assertNotIn(marker, repr(error))
    test.assertNotIn(marker, "".join(traceback.format_exception(error)))
    test.assertIsNone(error.__context__)
    test.assertIsNone(error.__cause__)


def _projection() -> dict[str, object]:
    json_document = parse_document(
        b'{ /* retained */ "settings": {"items": [1, true, {"value": "x"}]}}\n',
        format="jsonc",
    )
    toml_document = parse_document(
        b'title = "Plan"\n'
        b'inline = { server = "localhost", ports = [8000, 8001] }\n'
        b"[owner]\n"
        b'name = "Ada"\n'
        b"[[products]]\n"
        b'name = "hammer"\n',
        format="toml",
    )
    return {
        "metadata": {"name": "settings", "enabled": True},
        "documents": [
            _document_projection(json_document),
            _document_projection(toml_document),
        ],
        "selected": ("settings", "items"),
    }


def _document_projection(document: Document) -> dict[str, object]:
    inline_items = tuple(document._toml_inline_tables.items())
    inline_tables = tuple(sorted(inline_items, key=lambda entry: entry[0]))
    return {
        "data": document.data,
        "value": document.value,
        "format": document._format,
        "root": document._root,
        "toml_headers": document._toml_headers,
        "toml_inline_tables": inline_tables,
    }


def _parser_nodes(value: object) -> list[object]:
    result: list[object] = []
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(
            current,
            (
                _Node,
                _JsonMember,
                _TomlAssignment,
                _TomlHeader,
                _TomlInlineMember,
                _TomlInlineTable,
            ),
        ):
            if type(current) not in _PARSER_NODE_TYPES:
                continue
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            result.append(current)
            pending.extend(getattr(current, item.name) for item in fields(current))
        elif type(current) is dict:
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key, child in current.items():
                pending.extend((key, child))
        elif type(current) is list or type(current) is tuple:
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(current)
    return result


def _changed_value(value: object) -> object:
    if value is None:
        return False
    if type(value) is bool:
        return not value
    if type(value) is int:
        return value + 1
    if type(value) is str:
        return value + "-changed"
    if type(value) is bytes:
        return value + b"-changed"
    if type(value) is Decimal:
        return value.next_plus()
    if type(value) is list:
        return [*value, "changed"]
    if type(value) is tuple:
        return (*value, "changed")
    if type(value) is dict:
        changed = dict(value)
        key = "__m7_evidence_change__"
        while key in changed:
            key += "_"
        changed[key] = None
        return changed
    return "changed"


def _mutable_ids(value: object) -> set[int]:
    mutable_types = (list, dict, *_PARSER_NODE_TYPES)
    found: set[int] = set()
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if type(current) in mutable_types:
            identity = id(current)
            found.add(identity)
            if identity in visited:
                continue
            visited.add(identity)
        if type(current) is dict:
            for key, child in current.items():
                pending.extend((key, child))
        elif type(current) is list or type(current) is tuple:
            pending.extend(current)
        elif isinstance(
            current,
            (
                _PlanEvidence,
                _NodeRecord,
                _ScalarEvidence,
                _NodeReference,
                _Node,
                _JsonMember,
                _TomlAssignment,
                _TomlHeader,
                _TomlInlineMember,
                _TomlInlineTable,
            ),
        ):
            pending.extend(getattr(current, item.name) for item in fields(current))
    return found


def _contains_mutable(value: object) -> bool:
    return bool(_mutable_ids(value))


def _deep_dag(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = {"left": value, "right": value}
    return value


if __name__ == "__main__":
    unittest.main()
