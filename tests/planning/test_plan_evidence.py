"""Tests for plan evidence."""

from __future__ import annotations

import copy
import tempfile
import traceback
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.engine import EngineError, Plan, accept_baseline, apply_plan, build_plan
from luwu.formats import (
    Document,
    _JsonMember,
    _Node,
    _TomlAssignment,
    _TomlHeader,
    _TomlInlineMember,
    _TomlInlineTable,
    parse_document,
)
from luwu.manifest import load_manifest
from luwu.plan_evidence import (
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
        key = "__evidence_change__"
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


class PlanEvidenceV8Tests(unittest.TestCase):
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
            "luwu.plan_evidence._scalar_evidence",
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


class PlanGraphV8Tests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="luwu-test-plan-graph-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.target = self.root / "home" / "config" / "settings.json"
        self.local_input = self.inputs / "local" / "values.json"
        self.baseline = self.inputs / "baselines" / "settings.json"
        (self.source / "templates").mkdir(parents=True)
        self.local_input.parent.mkdir(parents=True)
        self.baseline.parent.mkdir(parents=True)
        self.target.parent.mkdir(parents=True)

        (self.source / "templates" / "settings.json.j2").write_text(
            '{"alpha":{{ inputs.alpha | tojson }},"beta":{{ inputs.beta | tojson }}}\n',
            encoding="utf-8",
        )
        self.local_input.write_bytes(b'{"alpha":"alpha-v1","beta":"beta-v1"}\n')
        self.manifest = self.root / "luwu.toml"
        self.manifest.write_text(
            """
version = 8

[roots]
source = "source"
inputs = "inputs"
target = "home"

[profiles.work]
packages = ["app"]
exclude = []

[packages.app]
dependencies = []
resources = ["settings"]

[inputs.alpha]
layers = [{ file = "local/values.json", path = ["alpha"], format = "json" }]

[inputs.beta]
layers = [{ file = "local/values.json", path = ["beta"], format = "json" }]

[resources.settings]
source = "templates/settings.json.j2"
target = "config/settings.json"
format = "json"
baseline = "baselines/settings.json"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.settings.fields.alpha]
path = ["alpha"]
owner = "source"
input = "alpha"

[resources.settings.fields.beta]
path = ["beta"]
owner = "source"
input = "beta"
""",
            encoding="utf-8",
        )

        initial = accept_baseline(
            self._build_plan(),
            resource="settings",
            fields=("alpha", "beta"),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(initial.outcome, "committed")
        applied = apply_plan(self._build_plan(), confirm=True)
        self.assertEqual(applied.outcome, "committed")

        self.local_input.write_bytes(b'{"alpha":"alpha-v2","beta":"beta-v1"}\n')
        self.plan = self._build_plan()
        projected = self.plan.to_dict()
        resources = projected.get("resources")
        self.assertIsInstance(resources, list)
        assert isinstance(resources, list)
        settings = next(
            item
            for item in resources
            if isinstance(item, dict) and item.get("name") == "settings"
        )
        self.assertEqual(settings.get("status"), "drifted")
        self.assertEqual(settings.get("action"), "forward")

    def _build_plan(self) -> Plan:
        return build_plan(load_manifest(self.manifest, profile="work"))

    def _settings_document(self):
        resource = next(
            item for item in self.plan._resources if item.resource.name == "settings"
        )
        document = resource.desired_document
        self.assertIsNotNone(document)
        assert document is not None
        return document

    def _snapshot_bytes(self) -> tuple[bytes, bytes, bytes]:
        return (
            self.target.read_bytes(),
            self.local_input.read_bytes(),
            self.baseline.read_bytes(),
        )

    def _assert_plan_invalid_without_writes(self) -> None:
        before = self._snapshot_bytes()
        result = None
        error = None
        with (
            patch.object(
                engine_module, "write_file", wraps=engine_module.write_file
            ) as write_file,
            patch.object(
                engine_module, "create_parent", wraps=engine_module.create_parent
            ) as create_parent,
        ):
            try:
                result = apply_plan(self.plan, confirm=True)
            except EngineError as caught:
                error = caught

        error_code = (
            error.code
            if error is not None
            else None
            if result is None
            else result.error_code
        )
        outcome = (
            "raised"
            if error is not None
            else None
            if result is None
            else result.outcome
        )
        with self.subTest(check="plan error"):
            self.assertEqual(error_code, "plan_invalid")
        with self.subTest(check="no writer calls"):
            self.assertEqual(write_file.call_count, 0)
        with self.subTest(check="no parent creation"):
            self.assertEqual(create_parent.call_count, 0)
        with self.subTest(check="target input baseline bytes"):
            self.assertEqual(self._snapshot_bytes(), before)
        if error is None and result is not None:
            with self.subTest(check="mutation not attempted"):
                self.assertEqual(outcome, "not_attempted")

    def _alpha_graph_edges(self):
        root = self._settings_document()._root
        children = root.children
        self.assertIsInstance(children, dict)
        assert isinstance(children, dict)
        members = root.members
        self.assertIsNotNone(members)
        assert members is not None
        alpha_index = next(
            index for index, member in enumerate(members) if member.key == "alpha"
        )
        alpha_member = members[alpha_index]
        original = children["alpha"]
        self.assertIs(alpha_member.value, original)
        return children, members, alpha_index, alpha_member, original

    def _split_equal_alpha_child(self):
        children, members, alpha_index, alpha_member, original = (
            self._alpha_graph_edges()
        )
        clone = copy.deepcopy(original)
        self.assertEqual(clone, original)
        self.assertIsNot(clone, original)

        children["alpha"] = clone
        self.assertIs(members[alpha_index].value, alpha_member.value)
        self.assertIsNot(children["alpha"], alpha_member.value)
        self.assertEqual(children["alpha"], alpha_member.value)
        return clone

    def _merge_equal_alpha_child(self):
        children, members, alpha_index, alpha_member, original = (
            self._alpha_graph_edges()
        )
        clone = copy.deepcopy(original)
        self.assertEqual(clone, original)
        self.assertIsNot(clone, original)

        # First make the parser's two references independent, equal copies,
        # then merge both references onto the equal copy.
        children["alpha"] = clone
        self.assertIsNot(children["alpha"], alpha_member.value)
        self.assertEqual(children["alpha"], alpha_member.value)
        members[alpha_index] = replace(alpha_member, value=clone)

        self.assertIs(children["alpha"], members[alpha_index].value)
        self.assertIsNot(children["alpha"], original)
        self.assertEqual(children["alpha"], original)
        return clone

    def _assert_apply_commits_expected_bytes(self) -> None:
        before_input = self.local_input.read_bytes()
        before_baseline = self.baseline.read_bytes()
        result = apply_plan(self.plan, confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(
            self.target.read_bytes(), b'{"alpha":"alpha-v2","beta":"beta-v1"}\n'
        )
        self.assertEqual(self.local_input.read_bytes(), before_input)
        self.assertEqual(self.baseline.read_bytes(), before_baseline)

    def test_splitting_equal_shared_parser_child_is_accepted(self) -> None:
        self._split_equal_alpha_child()

        self._assert_apply_commits_expected_bytes()

    def test_merging_equal_parser_child_copies_is_accepted(self) -> None:
        self._merge_equal_alpha_child()

        self._assert_apply_commits_expected_bytes()

    def test_split_child_value_change_is_rejected_before_writes(self) -> None:
        clone = self._split_equal_alpha_child()
        clone.value = "tampered-alpha"

        self._assert_plan_invalid_without_writes()

    def test_split_child_span_change_is_rejected_before_writes(self) -> None:
        clone = self._split_equal_alpha_child()
        self.assertIsNotNone(clone.start)
        self.assertIsNotNone(clone.end)
        assert clone.start is not None
        assert clone.end is not None
        clone.start += 1
        clone.end += 1

        self._assert_plan_invalid_without_writes()

    def test_merged_child_value_change_is_rejected_before_writes(self) -> None:
        clone = self._merge_equal_alpha_child()
        clone.value = "tampered-alpha"

        self._assert_plan_invalid_without_writes()

    def test_merged_child_span_change_is_rejected_before_writes(self) -> None:
        clone = self._merge_equal_alpha_child()
        self.assertIsNotNone(clone.start)
        self.assertIsNotNone(clone.end)
        assert clone.start is not None
        assert clone.end is not None
        clone.start += 1
        clone.end += 1

        self._assert_plan_invalid_without_writes()
