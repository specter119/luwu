"""Immutable evidence and full graph matching for M7 plan projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import NoReturn

from .m7_formats import (
    _JsonMember,
    _Node,
    _TomlAssignment,
    _TomlHeader,
    _TomlInlineMember,
    _TomlInlineTable,
)


class _PlanEvidenceError(ValueError):
    """A fixed-message error for unsupported or cyclic plan graphs."""

    def __init__(self) -> None:
        super().__init__("plan graph evidence is invalid")


@dataclass(frozen=True, slots=True)
class _ScalarEvidence:
    kind: str = field(repr=False)
    value: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class _NodeReference:
    index: int = field(repr=False)


type _EvidenceEdge = _ScalarEvidence | _NodeReference


@dataclass(frozen=True, slots=True)
class _NodeRecord:
    kind: str = field(repr=False)
    edges: tuple[_EvidenceEdge, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _PlanEvidence:
    root: _EvidenceEdge = field(repr=False)
    records: tuple[_NodeRecord, ...] = field(repr=False)


@dataclass(slots=True)
class _CaptureFrame:
    value: object
    identity: int
    index: int
    kind: str
    children: tuple[object, ...]
    offset: int = 0
    edges: list[_EvidenceEdge] = field(default_factory=list)


def _capture_plan_evidence(projection: object) -> _PlanEvidence:
    """Copy a complete built-in projection into independent immutable records."""

    records: list[_NodeRecord | None] = []
    references: dict[int, int] = {}
    active: set[int] = set()
    retained: list[object] = []
    frames: list[_CaptureFrame] = []

    def edge_for(value: object) -> _EvidenceEdge:
        scalar = _scalar_evidence(value)
        if scalar is not None:
            return scalar

        identity = id(value)
        if identity in active:
            _raise_invalid()
        existing = references.get(identity)
        if existing is not None:
            return _NodeReference(existing)

        kind, children = _describe_node(value)
        index = len(records)
        references[identity] = index
        active.add(identity)
        retained.append(value)
        records.append(None)
        frames.append(_CaptureFrame(value, identity, index, kind, children))
        return _NodeReference(index)

    root = edge_for(projection)
    while frames:
        frame = frames[-1]
        if frame.offset == len(frame.children):
            records[frame.index] = _NodeRecord(frame.kind, tuple(frame.edges))
            active.remove(frame.identity)
            frames.pop()
            continue

        child = frame.children[frame.offset]
        frame.offset += 1
        frame.edges.append(edge_for(child))

    completed = tuple(record for record in records if record is not None)
    if len(completed) != len(records):
        _raise_invalid()
    return _PlanEvidence(root, completed)


def _matches_plan_evidence(projection: object, evidence: _PlanEvidence) -> bool:
    """Re-read and compare the complete current graph against captured evidence."""

    if type(evidence) is not _PlanEvidence:
        _raise_invalid()

    active: set[int] = set()
    compared: set[tuple[int, int]] = set()
    retained: list[object] = []
    pending: list[tuple[object, _EvidenceEdge, bool]] = [
        (projection, evidence.root, False)
    ]

    while pending:
        current, expected, exiting = pending.pop()
        identity = id(current)
        if exiting:
            if identity not in active:
                _raise_invalid()
            active.remove(identity)
            continue

        scalar_kind = _scalar_kind(current)
        if type(expected) is _ScalarEvidence:
            if scalar_kind is not None:
                if not _scalar_matches(current, scalar_kind, expected):
                    return False
                continue

            _describe_node(current)
            return False

        if type(expected) is not _NodeReference:
            _raise_invalid()
        if scalar_kind is not None:
            return False

        kind, children = _describe_node(current)
        index = expected.index
        if type(index) is not int or not 0 <= index < len(evidence.records):
            _raise_invalid()
        record = evidence.records[index]
        if type(record) is not _NodeRecord or type(record.edges) is not tuple:
            _raise_invalid()
        if kind != record.kind or len(children) != len(record.edges):
            return False

        if identity in active:
            _raise_invalid()
        pair = (identity, index)
        if pair in compared:
            continue
        compared.add(pair)
        active.add(identity)
        retained.append(current)
        pending.append((current, expected, True))

        pending.extend(
            (children[position], record.edges[position], False)
            for position in range(len(children) - 1, -1, -1)
        )

    return True


def _scalar_evidence(value: object) -> _ScalarEvidence | None:
    kind = _scalar_kind(value)
    if kind is None:
        return None
    if kind == "decimal":
        assert isinstance(value, Decimal)
        scalar_value: object = _decimal_representation(value)
    elif kind == "none":
        scalar_value = None
    else:
        scalar_value = value
    return _ScalarEvidence(kind, scalar_value)


def _scalar_kind(value: object) -> str | None:
    if value is None:
        return "none"

    value_type = type(value)
    if value_type is bool and isinstance(value, bool):
        return "bool"
    if value_type is int and isinstance(value, int):
        return "int"
    if value_type is str and isinstance(value, str):
        return "str"
    if value_type is bytes and isinstance(value, bytes):
        return "bytes"
    if value_type is Decimal and isinstance(value, Decimal):
        if not value.is_finite():
            _raise_invalid()
        return "decimal"
    return None


def _scalar_matches(value: object, kind: str, expected: _ScalarEvidence) -> bool:
    if kind != expected.kind:
        return False
    if kind == "decimal":
        assert isinstance(value, Decimal)
        return _decimal_representation(value) == expected.value
    if kind == "none":
        return expected.value is None
    return value == expected.value


def _decimal_representation(value: Decimal) -> tuple[int, tuple[int, ...], int]:
    decimal_tuple = value.as_tuple()
    exponent = decimal_tuple.exponent
    if type(exponent) is not int:
        _raise_invalid()
    return decimal_tuple.sign, decimal_tuple.digits, exponent


def _describe_node(value: object) -> tuple[str, tuple[object, ...]]:
    value_type = type(value)
    if value_type is list and isinstance(value, list):
        return "list", tuple(value)
    if value_type is tuple and isinstance(value, tuple):
        return "tuple", tuple(value)
    if value_type is dict and isinstance(value, dict):
        items_failed = False
        items: tuple[tuple[object, object], ...] = ()
        try:
            items = tuple(value.items())
        except RuntimeError:
            items_failed = True
        if items_failed:
            _raise_invalid()
        if any(type(key) is not str for key, _ in items):
            _raise_invalid()
        ordered = tuple(sorted(items, key=lambda item: item[0]))
        children = tuple(part for pair in ordered for part in pair)
        return "dict", children
    if value_type is _Node and isinstance(value, _Node):
        return "_Node", (
            value.value,
            value.start,
            value.end,
            value.children,
            value.members,
            value.open_end,
            value.close_start,
            value.assignment,
        )
    if value_type is _JsonMember and isinstance(value, _JsonMember):
        return "_JsonMember", (
            value.key,
            value.key_start,
            value.key_end,
            value.colon,
            value.value,
            value.comma,
        )
    if value_type is _TomlAssignment and isinstance(value, _TomlAssignment):
        return "_TomlAssignment", (
            value.path,
            value.line_start,
            value.statement_end,
            value.value_start,
            value.value_end,
            value.multiline,
            value.inline_owner,
        )
    if value_type is _TomlHeader and isinstance(value, _TomlHeader):
        return "_TomlHeader", (value.path, value.line_start, value.array)
    if value_type is _TomlInlineMember and isinstance(value, _TomlInlineMember):
        return "_TomlInlineMember", (
            value.path,
            value.key_start,
            value.key_end,
            value.value_start,
            value.value_end,
            value.comma,
        )
    if value_type is _TomlInlineTable and isinstance(value, _TomlInlineTable):
        return "_TomlInlineTable", (
            value.path,
            value.open_end,
            value.close_start,
            value.members,
            value.multiline,
        )
    _raise_invalid()


def _raise_invalid() -> NoReturn:
    raise _PlanEvidenceError() from None
