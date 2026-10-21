"""Byte-preserving readers and field patchers for public documents."""

from __future__ import annotations

import itertools
import json
import re
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from .errors import LuwuError
from .sensitivity import is_sensitive_name

LeafPath = tuple[str, ...]


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


class FormatError(LuwuError):
    """A fixed-message, metadata-only format or patch failure."""

    _MESSAGES: ClassVar[dict[str, str]] = {
        "format": "document format or syntax is unsupported",
        "encoding": "document is not valid UTF-8",
        "sensitive_input": "document contains a sensitive field name",
        "path": "document path is unsupported",
        "patch": "document patch cannot be applied safely",
    }

    def __init__(self, *, code: str = "format") -> None:
        safe_code = code if code in self._MESSAGES else "format"
        super().__init__(self._MESSAGES[safe_code], code=safe_code)


@dataclass(frozen=True, slots=True)
class _JsonMember:
    key: str
    key_start: int
    key_end: int
    colon: int
    value: _Node
    comma: int | None


@dataclass(frozen=True, slots=True)
class _TomlAssignment:
    path: LeafPath
    line_start: int
    statement_end: int
    value_start: int
    value_end: int
    multiline: bool
    inline_owner: LeafPath | None = None


@dataclass(frozen=True, slots=True)
class _TomlInlineMember:
    path: LeafPath
    key_start: int
    key_end: int
    value_start: int
    value_end: int
    comma: int | None


@dataclass(frozen=True, slots=True)
class _TomlInlineTable:
    path: LeafPath
    open_end: int
    close_start: int
    members: tuple[_TomlInlineMember, ...]
    multiline: bool


@dataclass(frozen=True, slots=True)
class _TomlHeader:
    path: LeafPath
    line_start: int
    array: bool


@dataclass(slots=True)
class _Node:
    value: object
    start: int | None = None
    end: int | None = None
    children: dict[str, _Node] | list[_Node] | None = None
    members: list[_JsonMember] | None = None
    open_end: int | None = None
    close_start: int | None = None
    assignment: _TomlAssignment | None = None


@dataclass(frozen=True, slots=True)
class Document:
    """A parsed full tree and the original bytes used to locate safe patches."""

    data: bytes = field(repr=False)
    value: dict[str, object] = field(repr=False)
    _format: str = field(repr=False, compare=False)
    _root: _Node = field(repr=False, compare=False)
    _toml_headers: tuple[_TomlHeader, ...] = field(
        default=(), repr=False, compare=False
    )
    _toml_inline_tables: dict[LeafPath, _TomlInlineTable] = field(
        default_factory=dict, repr=False, compare=False
    )
    _public_names: frozenset[str] = field(
        default=frozenset(), repr=False, compare=False
    )
    _sensitive_exempt_paths: frozenset[LeafPath] = field(
        default=frozenset(), repr=False, compare=False
    )


_JSON_NUMBER = re.compile(rb"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_BARE_TOML_KEY = re.compile(r"[A-Za-z0-9_-]+")
_MAX_PATH_SEGMENTS = 256
_MAX_NUMBER_LITERAL_LENGTH = 4096


def parse_document(
    data: bytes,
    *,
    format: str,
    public_names: frozenset[str] = frozenset(),
    sensitive_exempt_paths: Collection[LeafPath] = (),
) -> Document:
    """Parse a complete JSON, JSONC, or TOML object without exposing content.

    ``public_names`` are exact keys the manifest declares public; every other
    secret-looking key is rejected. ``sensitive_exempt_paths`` instead exempts
    only the single key at one exact literal path (used for provider-owned
    fields, whose declared paths may carry a sensitive-looking leaf); the same
    name anywhere else in the document still blocks.
    """

    if not isinstance(data, bytes):
        raise FormatError(code="format")
    normalized_format = format.lower().lstrip(".") if isinstance(format, str) else ""
    if normalized_format not in {"json", "jsonc", "toml"}:
        raise FormatError(code="format")
    exempt = frozenset(sensitive_exempt_paths)
    for exempt_path in exempt:
        _validate_path(exempt_path)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FormatError(code="encoding") from None

    if normalized_format in {"json", "jsonc"}:
        parser = _JsonParser(
            data,
            allow_comments=normalized_format == "jsonc",
            public_names=public_names,
            exempt_paths=exempt,
        )
        try:
            root = parser.parse()
        except RecursionError:
            raise FormatError(code="format") from None
        if not isinstance(root.value, dict):
            raise FormatError(code="format")
        value = root.value
        try:
            _validate_tree(value, public_names, exempt)
        except RecursionError:
            raise FormatError(code="format") from None
        return Document(
            data,
            value,
            normalized_format,
            root,
            _public_names=public_names,
            _sensitive_exempt_paths=exempt,
        )

    try:
        parsed = tomllib.loads(text, parse_float=Decimal)
    except (
        tomllib.TOMLDecodeError,
        ValueError,
        OverflowError,
        InvalidOperation,
        RecursionError,
    ):
        raise FormatError(code="format") from None
    if not isinstance(parsed, dict):
        raise FormatError(code="format")
    try:
        _validate_tree(parsed, public_names, exempt)
    except RecursionError:
        raise FormatError(code="format") from None
    offsets = _char_to_byte_offsets(text)
    assignments, headers, inline_tables = _scan_toml(text, parsed, offsets)
    try:
        root = _build_toml_node(parsed)
    except RecursionError:
        raise FormatError(code="format") from None
    for assignment in assignments:
        node = _lookup_node(root, assignment.path)
        if node is not None and not isinstance(node.value, (dict, list)):
            node.start = assignment.value_start
            node.end = assignment.value_end
            node.assignment = assignment
    return Document(
        data,
        parsed,
        normalized_format,
        root,
        tuple(headers),
        inline_tables,
        public_names,
        exempt,
    )


def get_leaf(document: Document, path: LeafPath) -> object:
    """Return one scalar value, or ``MISSING`` when its literal path is absent."""

    _validate_path(path)
    node = _lookup_node(document._root, path)
    if node is None:
        return MISSING
    if isinstance(node.value, (dict, list)):
        raise FormatError(code="path")
    return node.value


def patch_document(document: Document, edits: Mapping[LeafPath, object]) -> bytes:
    """Patch selected scalar spans and necessary syntax while preserving bytes."""

    if not isinstance(edits, Mapping):
        raise FormatError(code="patch")
    normalized: dict[LeafPath, object] = {}
    for path, value in edits.items():
        _validate_path(path)
        if any(
            path[: index + 1] not in document._sensitive_exempt_paths
            and is_sensitive_name(segment, public=document._public_names)
            for index, segment in enumerate(path)
        ):
            raise FormatError(code="sensitive_input")
        if value is not MISSING:
            _validate_scalar(value, document._format)
        normalized[path] = value
    _validate_non_overlapping_paths(tuple(normalized))
    if not normalized:
        return document.data

    original_nodes: dict[LeafPath, _Node | None] = {}
    for path in normalized:
        original_nodes[path] = _lookup_node(document._root, path)

    if document._format in {"json", "jsonc"}:
        current = _patch_json_existing(document, normalized, original_nodes)
    else:
        current = _patch_toml_existing(document, normalized, original_nodes)

    public = document._public_names
    exempt = document._sensitive_exempt_paths
    current_document = parse_document(
        current,
        format=document._format,
        public_names=public,
        sensitive_exempt_paths=exempt,
    )
    missing_updates = {
        path: value
        for path, value in normalized.items()
        if value is not MISSING and original_nodes[path] is None
    }
    for path in sorted(missing_updates, reverse=True):
        if document._format in {"json", "jsonc"}:
            current_document = parse_document(
                _insert_json_path(current_document, path, missing_updates[path]),
                format=document._format,
                public_names=public,
                sensitive_exempt_paths=exempt,
            )
        else:
            current_document = parse_document(
                _insert_toml_path(current_document, path, missing_updates[path]),
                format=document._format,
                public_names=public,
                sensitive_exempt_paths=exempt,
            )

    for path, expected in normalized.items():
        actual = get_leaf(current_document, path)
        if expected is MISSING:
            if actual is not MISSING:
                raise FormatError(code="patch")
        elif actual is MISSING or not semantic_equal(actual, expected):
            raise FormatError(code="patch")
    if not unselected_equal(document, current_document, normalized.keys()):
        raise FormatError(code="patch")
    return current_document.data


def semantic_equal(left: object, right: object) -> bool:
    """Compare JSON/TOML values recursively, keeping bool distinct from numbers."""

    if left is MISSING or right is MISSING:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if _is_number(left) or _is_number(right):
        return _is_number(left) and _is_number(right) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        return left.keys() == right.keys() and all(
            semantic_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(semantic_equal(a, b) for a, b in zip(left, right, strict=True))
        )
    return False


def unselected_equal(
    left: Document, right: Document, paths: Collection[LeafPath]
) -> bool:
    """Compare full trees, masking edits and only their newly created ancestors."""

    return semantic_equal(*_unselected_projections(left, right, paths))


def unselected_scalar_changes(
    left: Document, right: Document, paths: Collection[LeafPath]
) -> dict[LeafPath, object] | None:
    """Return the scalar values that make ``right`` differ outside ``paths``.

    Each entry is a leaf of ``right`` that is new or has another scalar value
    than ``left``. The result is ``None`` when the difference is anything else:
    a removed key, a changed array, an empty added table, or a scalar that
    became a table or the reverse.
    """

    left_view, right_view = _unselected_projections(left, right, paths)
    changes: dict[LeafPath, object] = {}
    if not _collect_scalar_changes(left_view, right_view, (), changes):
        return None
    return changes


def _collect_scalar_changes(
    left: object, right: object, path: LeafPath, changes: dict[LeafPath, object]
) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        for key, right_child in right.items():
            if key in left:
                if not _collect_scalar_changes(
                    left[key], right_child, (*path, key), changes
                ):
                    return False
            elif not _collect_added_leaves(right_child, (*path, key), changes):
                return False
        return all(key in right for key in left)
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return semantic_equal(left, right)
    if not semantic_equal(left, right):
        changes[path] = right
    return True


def _collect_added_leaves(
    value: object, path: LeafPath, changes: dict[LeafPath, object]
) -> bool:
    if isinstance(value, list):
        return False
    if isinstance(value, dict):
        return bool(value) and all(
            _collect_added_leaves(child, (*path, key), changes)
            for key, child in value.items()
        )
    changes[path] = value
    return True


def _unselected_projections(
    left: Document, right: Document, paths: Collection[LeafPath]
) -> tuple[object, object]:

    trie: dict[object, object] = {}
    terminal = object()
    selected_paths: list[LeafPath] = []
    for path in paths:
        try:
            _validate_path(path)
        except FormatError:
            continue
        selected_paths.append(path)
        branch: dict[object, object] = trie
        for segment in path:
            child = branch.get(segment)
            if not isinstance(child, dict):
                child = {}
                branch[segment] = child
            branch = child
        branch[terminal] = True

    created_ancestors: set[LeafPath] = set()
    for path in selected_paths:
        # An absent selected leaf cannot justify masking an empty ancestor.
        if not _path_is_scalar(right.value, path):
            continue
        current: object = left.value
        missing_ancestor = False
        for index, segment in enumerate(path[:-1]):
            prefix = path[: index + 1]
            if missing_ancestor:
                created_ancestors.add(prefix)
                continue
            if not isinstance(current, dict):
                break
            if segment not in current:
                created_ancestors.add(prefix)
                missing_ancestor = True
            else:
                current = current[segment]

    def project(
        value: object, branch: dict[object, object], path: LeafPath = ()
    ) -> object:
        if terminal in branch:
            return MISSING
        if not isinstance(value, dict):
            return value
        projected: dict[str, object] = {}
        for key, child_value in value.items():
            child_branch = branch.get(key)
            if not isinstance(child_branch, dict):
                projected[key] = child_value
                continue
            child = project(child_value, child_branch, (*path, key))
            if child is not MISSING:
                projected[key] = child
        if not projected and path in created_ancestors:
            return MISSING
        return projected

    return project(left.value, trie), project(right.value, trie)


class _JsonParser:
    def __init__(
        self,
        data: bytes,
        *,
        allow_comments: bool,
        public_names: frozenset[str],
        exempt_paths: frozenset[LeafPath] = frozenset(),
    ) -> None:
        self.public_names = public_names
        self.exempt_paths = exempt_paths
        self.data = data
        self.length = len(data)
        self.index = 0
        self.allow_comments = allow_comments
        # Path of keys above the value being parsed; array members poison it
        # with None so an exemption can never match below an array.
        self._path: list[object] = []

    def parse(self) -> _Node:
        self._skip_trivia()
        if self.index >= self.length:
            raise FormatError(code="format")
        root = self._value()
        self._skip_trivia()
        if self.index != self.length:
            raise FormatError(code="format")
        return root

    def _value(self) -> _Node:
        if self.index >= self.length:
            raise FormatError(code="format")
        current = self.data[self.index]
        if current == ord("{"):
            return self._object()
        if current == ord("["):
            return self._array()
        if current == ord('"'):
            start, end, value = self._string()
            return _Node(value, start, end)
        for token, value in ((b"true", True), (b"false", False), (b"null", None)):
            if self.data.startswith(token, self.index):
                start = self.index
                self.index += len(token)
                return _Node(value, start, self.index)
        match = _JSON_NUMBER.match(self.data, self.index)
        if match is not None:
            start, end = match.span()
            token = self.data[start:end]
            self.index = end
            try:
                if b"." in token or b"e" in token.lower():
                    value = Decimal(token.decode("ascii"))
                    if not value.is_finite():
                        raise InvalidOperation
                else:
                    value = int(token)
            except (ValueError, InvalidOperation):
                raise FormatError(code="format") from None
            return _Node(value, start, end)
        raise FormatError(code="format")

    def _object(self) -> _Node:
        start = self.index
        self.index += 1
        open_end = self.index
        members: list[_JsonMember] = []
        children: dict[str, _Node] = {}
        self._skip_trivia()
        if self._take(ord("}")):
            return _Node(
                {}, start, self.index, children, members, open_end, self.index - 1
            )
        while True:
            if self.index >= self.length or self.data[self.index] != ord('"'):
                raise FormatError(code="format")
            key_start, key_end, key = self._string()
            if key in children:
                raise FormatError(code="format")
            if tuple(self._path + [key]) not in self.exempt_paths and (
                is_sensitive_name(key, public=self.public_names)
            ):
                raise FormatError(code="sensitive_input")
            self._skip_trivia()
            colon = self.index
            if not self._take(ord(":")):
                raise FormatError(code="format")
            self._skip_trivia()
            self._path.append(key)
            try:
                value = self._value()
            finally:
                self._path.pop()
            self._skip_trivia()
            comma: int | None = None
            if self._take(ord(",")):
                comma = self.index - 1
                self._skip_trivia()
                if self._take(ord("}")):
                    if not self.allow_comments:
                        raise FormatError(code="format")
                    members.append(
                        _JsonMember(key, key_start, key_end, colon, value, comma)
                    )
                    children[key] = value
                    break
            elif self._take(ord("}")):
                members.append(_JsonMember(key, key_start, key_end, colon, value, None))
                children[key] = value
                break
            else:
                raise FormatError(code="format")
            members.append(_JsonMember(key, key_start, key_end, colon, value, comma))
            children[key] = value
        node = _Node(
            {key: child.value for key, child in children.items()},
            start,
            self.index,
            children,
            members,
            open_end,
            self.index - 1,
        )
        return node

    def _array(self) -> _Node:
        start = self.index
        self.index += 1
        children: list[_Node] = []
        self._skip_trivia()
        if self._take(ord("]")):
            return _Node([], start, self.index, children)
        while True:
            self._path.append(None)
            try:
                children.append(self._value())
            finally:
                self._path.pop()
            self._skip_trivia()
            if self._take(ord("]")):
                break
            if not self._take(ord(",")):
                raise FormatError(code="format")
            self._skip_trivia()
            if self.index < self.length and self.data[self.index] == ord("]"):
                if not self.allow_comments:
                    raise FormatError(code="format")
                self.index += 1
                break
        return _Node([child.value for child in children], start, self.index, children)

    def _string(self) -> tuple[int, int, str]:
        start = self.index
        self.index += 1
        while self.index < self.length:
            byte = self.data[self.index]
            if byte == ord('"'):
                self.index += 1
                end = self.index
                try:
                    value = json.loads(self.data[start:end].decode("utf-8"))
                    value.encode("utf-8")
                except (UnicodeDecodeError, json.JSONDecodeError, UnicodeEncodeError):
                    raise FormatError(code="format") from None
                if not isinstance(value, str):
                    raise FormatError(code="format")
                return start, end, value
            if byte == ord("\\"):
                self.index += 2
                continue
            if byte < 0x20:
                raise FormatError(code="format")
            self.index += 1
        raise FormatError(code="format")

    def _skip_trivia(self) -> None:
        while self.index < self.length:
            byte = self.data[self.index]
            if byte in b" \t\r\n":
                self.index += 1
                continue
            if self.allow_comments and self.data.startswith(b"//", self.index):
                self.index += 2
                while self.index < self.length and self.data[self.index] not in b"\r\n":
                    self.index += 1
                continue
            if self.allow_comments and self.data.startswith(b"/*", self.index):
                end = self.data.find(b"*/", self.index + 2)
                if end < 0:
                    raise FormatError(code="format")
                self.index = end + 2
                continue
            break

    def _take(self, byte: int) -> bool:
        if self.index < self.length and self.data[self.index] == byte:
            self.index += 1
            return True
        return False


def _patch_json_existing(
    document: Document,
    edits: Mapping[LeafPath, object],
    nodes: Mapping[LeafPath, _Node | None],
) -> bytes:
    replacements: list[tuple[int, int, bytes]] = []
    deletions: dict[LeafPath, set[str]] = {}
    for path, value in edits.items():
        node = nodes[path]
        if node is None:
            continue
        if isinstance(node.value, (dict, list)):
            raise FormatError(code="path")
        if value is MISSING:
            deletions.setdefault(path[:-1], set()).add(path[-1])
            continue
        if node.start is None or node.end is None:
            raise FormatError(code="patch")
        replacements.append(
            (node.start, node.end, _encode_scalar(value, document._format))
        )

    for parent_path, keys in deletions.items():
        parent = _lookup_node(document._root, parent_path)
        if (
            parent is None
            or not isinstance(parent.value, dict)
            or parent.members is None
        ):
            raise FormatError(code="path")
        trailing_comma = bool(
            parent.members
            and parent.members[-1].comma is not None
            and document._format == "jsonc"
        )
        for index, member in enumerate(parent.members):
            if member.key in keys:
                if document._format == "jsonc":
                    if member.value.start is None or member.value.end is None:
                        raise FormatError(code="patch")
                    comments = _jsonc_trivia_comments(
                        document.data[member.key_end : member.colon]
                    ) + _jsonc_trivia_comments(
                        document.data[member.colon + 1 : member.value.start]
                    )
                    replacements.append((member.key_start, member.value.end, comments))
                else:
                    replacements.append(
                        (member.key_start, member.value.end or member.key_end, b"")
                    )
            if member.comma is None:
                continue
            later_survivor = any(
                item.key not in keys for item in parent.members[index + 1 :]
            )
            keep = member.key not in keys and (later_survivor or trailing_comma)
            if not keep:
                replacements.append((member.comma, member.comma + 1, b""))
    return _apply_byte_replacements(document.data, replacements)


def _jsonc_trivia_comments(trivia: bytes) -> bytes:
    comments: list[bytes] = []
    index = 0
    while index < len(trivia):
        if trivia[index] in b" \t\r\n":
            index += 1
            continue
        start = index
        if trivia.startswith(b"//", index):
            index += 2
            while index < len(trivia) and trivia[index] not in b"\r\n":
                index += 1
            if index < len(trivia) and trivia[index] == ord("\r"):
                index += 1
            if index < len(trivia) and trivia[index] == ord("\n"):
                index += 1
        elif trivia.startswith(b"/*", index):
            end = trivia.find(b"*/", index + 2)
            if end < 0:
                raise FormatError(code="patch")
            index = end + 2
        else:
            raise FormatError(code="patch")
        comments.append(trivia[start:index])
    return b" ".join(comments)


def _insert_json_path(document: Document, path: LeafPath, value: object) -> bytes:
    current = document._root
    for index, segment in enumerate(path):
        if not isinstance(current.value, dict) or not isinstance(
            current.children, dict
        ):
            raise FormatError(code="path")
        child = current.children.get(segment)
        if child is None:
            nested: object = value
            for parent_segment in reversed(path[index + 1 :]):
                nested = {parent_segment: nested}
            if (
                not isinstance(nested, (dict, str, bool, int, Decimal))
                and nested is not None
            ):
                raise FormatError(code="patch")
            if current.open_end is None or current.close_start is None:
                raise FormatError(code="patch")
            literal = _encode_json_value(
                nested,
                document._public_names,
                document._sensitive_exempt_paths,
                path[: index + 1],
            )
            key_literal = _encode_json_string(segment)
            if not current.members:
                insertion_at = current.open_end
                if _json_multiline(document.data, current):
                    newline = _json_newline(document.data)
                    indent = _json_closing_indent(document.data, current) + b"  "
                    inserted = newline + indent + key_literal + b": " + literal
                else:
                    inserted = key_literal + b":" + literal
            else:
                last = current.members[-1]
                after_colon = document.data[
                    last.colon + 1 : last.value.start or last.colon + 1
                ]
                colon = (
                    b":" + after_colon if not after_colon.strip(b" \t\r\n") else b": "
                )
                separator = _json_separator(document.data, current, last)
                if last.comma is not None:
                    insertion_at = last.comma + 1
                    inserted = separator + key_literal + colon + literal
                else:
                    insertion_at = last.value.end or last.key_end
                    inserted = b"," + separator + key_literal + colon + literal
            return _apply_byte_replacements(
                document.data, [(insertion_at, insertion_at, inserted)]
            )
        if index == len(path) - 1:
            raise FormatError(code="patch")
        if isinstance(child.value, list):
            raise FormatError(code="path")
        current = child
    raise FormatError(code="patch")


def _patch_toml_existing(
    document: Document,
    edits: Mapping[LeafPath, object],
    nodes: Mapping[LeafPath, _Node | None],
) -> bytes:
    replacements: list[tuple[int, int, bytes]] = []
    inline_deletions: dict[LeafPath, set[LeafPath]] = {}
    for path, value in edits.items():
        node = nodes[path]
        if node is None:
            continue
        if isinstance(node.value, (dict, list)):
            raise FormatError(code="path")
        assignment = node.assignment
        if assignment is None or node.start is None or node.end is None:
            raise FormatError(code="patch")
        if assignment.multiline:
            raise FormatError(code="patch")
        if value is MISSING:
            if assignment.inline_owner is None:
                replacements.append(
                    (assignment.line_start, assignment.statement_end, b"")
                )
            else:
                inline_deletions.setdefault(assignment.inline_owner, set()).add(path)
        else:
            replacements.append((node.start, node.end, _encode_scalar(value, "toml")))
    for table_path, deleted_paths in inline_deletions.items():
        table = document._toml_inline_tables.get(table_path)
        if table is None or table.multiline:
            raise FormatError(code="patch")
        for index, member in enumerate(table.members):
            if member.path in deleted_paths:
                replacements.append((member.key_start, member.value_end, b""))
            if member.comma is None:
                continue
            later_survivor = any(
                other.path not in deleted_paths for other in table.members[index + 1 :]
            )
            if member.path in deleted_paths or not later_survivor:
                replacements.append((member.comma, member.comma + 1, b""))
    return _apply_byte_replacements(document.data, replacements)


def _insert_toml_path(document: Document, path: LeafPath, value: object) -> bytes:
    parent_path = path[:-1]
    parent = _lookup_node(document._root, parent_path)
    if parent is not None and not isinstance(parent.value, dict):
        raise FormatError(code="path")

    inline_path = next(
        (
            path[:prefix_length]
            for prefix_length in range(len(path) - 1, 0, -1)
            if path[:prefix_length] in document._toml_inline_tables
        ),
        None,
    )
    if inline_path is not None:
        table = document._toml_inline_tables[inline_path]
        if table.multiline:
            raise FormatError(code="patch")
        relative_path = path[len(inline_path) :]
        first_child = _lookup_node(document._root, inline_path + (relative_path[0],))
        if first_child is not None and isinstance(first_child.value, dict):
            key = ".".join(_encode_toml_key(segment) for segment in relative_path)
            literal = _encode_scalar(value, "toml")
        else:
            nested: object = value
            for segment in reversed(relative_path[1:]):
                nested = {segment: nested}
            key = _encode_toml_key(relative_path[0])
            literal = _encode_toml_inline_value(
                nested,
                document._public_names,
                document._sensitive_exempt_paths,
                (*inline_path, relative_path[0]),
            )
        if table.members:
            last = table.members[-1]
            insertion_at = last.value_end
            inserted = b", " + key.encode("utf-8") + b" = " + literal
        else:
            insertion_at = table.open_end
            inserted = key.encode("utf-8") + b" = " + literal
        return _apply_byte_replacements(
            document.data, [(insertion_at, insertion_at, inserted)]
        )

    exact_header = next(
        (
            header
            for header in reversed(document._toml_headers)
            if not header.array and header.path == parent_path
        ),
        None,
    )
    ancestor_header = max(
        (
            header
            for header in document._toml_headers
            if not header.array
            and len(header.path) < len(path)
            and path[: len(header.path)] == header.path
        ),
        key=lambda header: len(header.path),
        default=None,
    )
    if exact_header is not None:
        later_headers = [
            header.line_start
            for header in document._toml_headers
            if header.line_start > exact_header.line_start
        ]
        insertion_at = min(later_headers) if later_headers else len(document.data)
        key = _encode_toml_key(path[-1])
    elif ancestor_header is not None:
        later_headers = [
            header.line_start
            for header in document._toml_headers
            if header.line_start > ancestor_header.line_start
        ]
        insertion_at = min(later_headers) if later_headers else len(document.data)
        key = ".".join(
            _encode_toml_key(segment) for segment in path[len(ancestor_header.path) :]
        )
    else:
        first_header = min(
            (header.line_start for header in document._toml_headers),
            default=len(document.data),
        )
        insertion_at = first_header
        key = ".".join(_encode_toml_key(segment) for segment in path)
    line = key.encode("utf-8") + b" = " + _encode_scalar(value, "toml")
    return _insert_toml_line(document.data, insertion_at, line)


def _insert_toml_line(data: bytes, insertion_at: int, line: bytes) -> bytes:
    newline = _toml_newline(data)
    before = data[:insertion_at]
    at_eof = insertion_at == len(data)
    prefix = b"" if not before or before.endswith((b"\n", b"\r")) else newline
    suffix = b"" if at_eof and data and not data.endswith((b"\n", b"\r")) else newline
    return data[:insertion_at] + prefix + line + suffix + data[insertion_at:]


def _scan_toml(
    text: str, parsed: dict[str, object], offsets: list[int]
) -> tuple[
    list[_TomlAssignment],
    list[_TomlHeader],
    dict[LeafPath, _TomlInlineTable],
]:
    assignments: list[_TomlAssignment] = []
    headers: list[_TomlHeader] = []
    inline_tables: dict[LeafPath, _TomlInlineTable] = {}
    index = 0
    table_path: LeafPath = ()
    in_array_table = False
    length = len(text)
    while index < length:
        line_start = text.rfind("\n", 0, index) + 1
        cursor = index
        while cursor < length and text[cursor] in " \t":
            cursor += 1
        if cursor >= length:
            break
        if text[cursor] in "\r\n":
            index = _toml_after_newline(text, cursor)
            continue
        if text[cursor] == "#":
            index = _toml_after_line(text, cursor)
            continue
        if text[cursor] == "[":
            header_path, array, header_end = _parse_toml_header(text, cursor)
            line_end = _toml_after_line(text, header_end)
            headers.append(_TomlHeader(header_path, offsets[line_start], array))
            table_path = header_path
            in_array_table = array
            index = line_end
            continue

        equals = _toml_assignment_equals(text, cursor)
        key_path = _parse_toml_key_path(text[cursor:equals])
        value_start_char = equals + 1
        while value_start_char < length and text[value_start_char] in " \t":
            value_start_char += 1
        value_end_char, statement_end_char = _scan_toml_value(text, value_start_char)
        full_path = table_path + key_path
        if not in_array_table and _path_is_scalar(parsed, full_path):
            assignments.append(
                _TomlAssignment(
                    full_path,
                    offsets[line_start],
                    offsets[statement_end_char],
                    offsets[value_start_char],
                    offsets[value_end_char],
                    _toml_value_is_multiline(text, value_start_char, value_end_char),
                )
            )
        elif (
            not in_array_table
            and value_start_char < value_end_char
            and text[value_start_char] == "{"
        ):
            _scan_toml_inline_table(
                text,
                parsed,
                offsets,
                full_path,
                value_start_char,
                value_end_char,
                offsets[line_start],
                offsets[statement_end_char],
                assignments,
                inline_tables,
            )
        index = statement_end_char
    return assignments, headers, inline_tables


def _scan_toml_inline_table(
    text: str,
    parsed: dict[str, object],
    offsets: list[int],
    table_path: LeafPath,
    start: int,
    end: int,
    line_start: int,
    statement_end: int,
    assignments: list[_TomlAssignment],
    inline_tables: dict[LeafPath, _TomlInlineTable],
) -> None:
    close = end - 1
    members: list[_TomlInlineMember] = []
    index = start + 1
    while index < close:
        while index < close and text[index] in " \t\r\n":
            index += 1
        if index >= close:
            break
        key_start = index
        equals = _toml_assignment_equals(text, index)
        key_path = _parse_toml_key_path(text[key_start:equals])
        value_start = equals + 1
        while value_start < close and text[value_start] in " \t\r\n":
            value_start += 1
        value_end, comma = _scan_toml_inline_value(text, value_start, close)
        full_path = table_path + key_path
        member = _TomlInlineMember(
            full_path,
            offsets[key_start],
            offsets[equals],
            offsets[value_start],
            offsets[value_end],
            offsets[comma] if comma is not None else None,
        )
        members.append(member)
        if value_start < value_end and text[value_start] == "{":
            _scan_toml_inline_table(
                text,
                parsed,
                offsets,
                full_path,
                value_start,
                value_end,
                line_start,
                statement_end,
                assignments,
                inline_tables,
            )
        elif _path_is_scalar(parsed, full_path):
            assignments.append(
                _TomlAssignment(
                    full_path,
                    line_start,
                    statement_end,
                    offsets[value_start],
                    offsets[value_end],
                    _toml_value_is_multiline(text, value_start, value_end),
                    table_path,
                )
            )
        if comma is None:
            break
        index = comma + 1
    inline_tables[table_path] = _TomlInlineTable(
        table_path,
        offsets[start + 1],
        offsets[close],
        tuple(members),
        "\n" in text[start:end] or "\r" in text[start:end],
    )


def _scan_toml_inline_value(
    text: str, start: int, close: int
) -> tuple[int, int | None]:
    index = start
    square_depth = 0
    curly_depth = 0
    quote: str | None = None
    multiline = False
    escaped = False
    last_end = start
    while index < close:
        char = text[index]
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
                index += 1
            elif quote == '"' and char == "\\":
                escaped = True
                index += 1
            elif (
                multiline
                and (quote_end := _toml_multiline_quote_end(text, index, quote))
                is not None
            ):
                index = quote_end
                quote = None
                multiline = False
                last_end = index
                continue
            elif not multiline and char == quote:
                index += 1
                quote = None
                last_end = index
                continue
            else:
                index += 1
            last_end = index
            continue
        if char in {'"', "'"}:
            quote = char
            multiline = text.startswith(char * 3, index)
            index += 3 if multiline else 1
            last_end = index
            continue
        if char == "[":
            square_depth += 1
        elif char == "]":
            square_depth -= 1
        elif char == "{":
            curly_depth += 1
        elif char == "}":
            if curly_depth == 0 and square_depth == 0:
                return last_end, None
            curly_depth -= 1
        elif char == "," and square_depth == 0 and curly_depth == 0:
            return last_end, index
        if char not in " \t\r\n":
            last_end = index + 1
        index += 1
    if quote is not None or square_depth or curly_depth:
        raise FormatError(code="format")
    return last_end, None


def _parse_toml_header(text: str, start: int) -> tuple[LeafPath, bool, int]:
    array = text.startswith("[[", start)
    opener = 2 if array else 1
    closer = "]]" if array else "]"
    index = start + opener
    quote: str | None = None
    escaped = False
    while index < len(text):
        char = text[index]
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if text.startswith(closer, index):
            path = _parse_toml_key_path(text[start + opener : index])
            return path, array, index + len(closer)
        if char in "\r\n":
            break
        index += 1
    raise FormatError(code="format")


def _toml_assignment_equals(text: str, start: int) -> int:
    index = start
    quote: str | None = None
    escaped = False
    while index < len(text):
        char = text[index]
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "=":
            return index
        elif char in "#\r\n":
            break
        index += 1
    raise FormatError(code="format")


def _parse_toml_key_path(raw: str) -> LeafPath:
    segments: list[str] = []
    index = 0
    while index < len(raw):
        while index < len(raw) and raw[index] in " \t":
            index += 1
        if index >= len(raw):
            break
        start = index
        if raw[index] in {'"', "'"}:
            quote = raw[index]
            index += 1
            escaped = False
            while index < len(raw):
                char = raw[index]
                if quote == '"' and escaped:
                    escaped = False
                elif quote == '"' and char == "\\":
                    escaped = True
                elif char == quote:
                    index += 1
                    break
                index += 1
            else:
                raise FormatError(code="format")
            token = raw[start:index]
            try:
                segment = tomllib.loads("key = " + token)["key"]
            except (tomllib.TOMLDecodeError, ValueError, KeyError):
                raise FormatError(code="format") from None
            if not isinstance(segment, str):
                raise FormatError(code="format")
        else:
            match = _BARE_TOML_KEY.match(raw, index)
            if match is None:
                raise FormatError(code="format")
            segment = match.group()
            index = match.end()
        segments.append(segment)
        while index < len(raw) and raw[index] in " \t":
            index += 1
        if index == len(raw):
            break
        if raw[index] != ".":
            raise FormatError(code="format")
        index += 1
    if not segments:
        raise FormatError(code="format")
    return tuple(segments)


def _scan_toml_value(text: str, start: int) -> tuple[int, int]:
    index = start
    length = len(text)
    square_depth = 0
    curly_depth = 0
    quote: str | None = None
    multiline = False
    last_end = start
    while index < length:
        char = text[index]
        if quote is not None:
            if quote == '"' and char == "\\":
                index = min(index + 2, length)
                last_end = index
                continue
            quote_end = (
                _toml_multiline_quote_end(text, index, quote) if multiline else None
            )
            if quote_end is not None:
                index = quote_end
                quote = None
                multiline = False
                last_end = index
                continue
            if not multiline and char == quote:
                index += 1
                quote = None
                last_end = index
                continue
            index += 1
            last_end = index
            continue
        if char in {'"', "'"}:
            quote = char
            multiline = text.startswith(char * 3, index)
            index += 3 if multiline else 1
            last_end = index
            continue
        if char == "#":
            while index < length and text[index] not in "\r\n":
                index += 1
            if square_depth == 0 and curly_depth == 0:
                return last_end, _toml_after_newline(text, index)
            continue
        if char == "[":
            square_depth += 1
        elif char == "]":
            square_depth -= 1
            if square_depth < 0:
                raise FormatError(code="format")
        elif char == "{":
            curly_depth += 1
        elif char == "}":
            curly_depth -= 1
            if curly_depth < 0:
                raise FormatError(code="format")
        elif char in "\r\n" and square_depth == 0 and curly_depth == 0:
            return last_end, _toml_after_newline(text, index)
        if char not in " \t\r\n":
            last_end = index + 1
        index += 1
    if quote is not None or square_depth or curly_depth:
        raise FormatError(code="format")
    return last_end, length


def _toml_after_newline(text: str, index: int) -> int:
    if index < len(text) and text[index] == "\r":
        index += 1
        if index < len(text) and text[index] == "\n":
            index += 1
        return index
    if index < len(text) and text[index] == "\n":
        return index + 1
    return index


def _toml_after_line(text: str, index: int) -> int:
    while index < len(text) and text[index] not in "\r\n":
        index += 1
    return _toml_after_newline(text, index)


def _toml_multiline_quote_end(text: str, start: int, quote: str) -> int | None:
    if not text.startswith(quote * 3, start):
        return None
    end = start + 3
    while end < len(text) and text[end] == quote:
        end += 1
    return end


def _toml_value_is_multiline(text: str, start: int, end: int) -> bool:
    return (
        text.startswith(('"""', "'''"), start)
        or "\n" in text[start:end]
        or "\r" in text[start:end]
    )


def _char_to_byte_offsets(text: str) -> list[int]:
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + len(char.encode("utf-8")))
    return offsets


def _build_toml_node(value: object) -> _Node:
    if isinstance(value, dict):
        return _Node(
            value,
            children={key: _build_toml_node(child) for key, child in value.items()},
        )
    if isinstance(value, list):
        return _Node(value, children=[_build_toml_node(child) for child in value])
    return _Node(value)


def _path_is_scalar(value: dict[str, object], path: LeafPath) -> bool:
    current: object = value
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return False
        current = current[segment]
    return not isinstance(current, (dict, list))


def _lookup_node(root: _Node, path: LeafPath) -> _Node | None:
    current = root
    for segment in path:
        if isinstance(current.value, list):
            raise FormatError(code="path")
        if not isinstance(current.value, dict) or not isinstance(
            current.children, dict
        ):
            raise FormatError(code="path")
        if segment not in current.children:
            return None
        child = current.children[segment]
        if not isinstance(child, _Node):
            raise FormatError(code="path")
        current = child
    return current


def _validate_tree(
    value: object,
    public: frozenset[str],
    exempt: frozenset[LeafPath] = frozenset(),
    prefix: LeafPath | None = (),
) -> None:
    """Reject sensitive names, except a key at one exact exempt path.

    ``prefix`` is None below an array: array members have no literal path, so
    an exemption must never reach through one.
    """

    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise FormatError(code="format")
            _ensure_utf8(key)
            full = None if prefix is None else (*prefix, key)
            if (full is None or full not in exempt) and is_sensitive_name(
                key, public=public
            ):
                raise FormatError(code="sensitive_input")
            _validate_tree(child, public, exempt, full)
        return
    if isinstance(value, list):
        for child in value:
            _validate_tree(child, public, exempt, None)
        return
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            _ensure_utf8(value)
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise FormatError(code="format")
        return
    raise FormatError(code="format")


def _validate_scalar(value: object, format: str) -> None:
    if value is None and format in {"json", "jsonc"}:
        return
    if isinstance(value, (str, bool)):
        if isinstance(value, str):
            _ensure_utf8(value)
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, Decimal) and value.is_finite():
        return
    raise FormatError(code="patch")


def _validate_path(path: LeafPath) -> None:
    if (
        not isinstance(path, tuple)
        or not path
        or len(path) > _MAX_PATH_SEGMENTS
        or any(not isinstance(item, str) for item in path)
    ):
        raise FormatError(code="path") from None
    for item in path:
        _ensure_utf8(item)


def _validate_non_overlapping_paths(paths: tuple[LeafPath, ...]) -> None:
    ordered = sorted(paths)
    for left, right in itertools.pairwise(ordered):
        if len(left) < len(right) and right[: len(left)] == left:
            raise FormatError(code="patch")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def _ensure_utf8(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise FormatError(code="format") from None


def _encode_scalar(value: object, format: str) -> bytes:
    _validate_scalar(value, format)
    if value is None:
        return b"null"
    if isinstance(value, str):
        return _encode_json_string(value)
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int):
        try:
            literal = str(value)
        except ValueError:
            raise FormatError(code="patch") from None
        if len(literal) > _MAX_NUMBER_LITERAL_LENGTH:
            raise FormatError(code="patch") from None
        return literal.encode("ascii")
    if isinstance(value, Decimal):
        try:
            literal = str(value)
        except (ValueError, OverflowError):
            raise FormatError(code="patch") from None
        if format == "toml" and "." not in literal and "e" not in literal.lower():
            literal += ".0"
        if len(literal) > _MAX_NUMBER_LITERAL_LENGTH:
            raise FormatError(code="patch") from None
        return literal.encode("ascii")
    raise FormatError(code="patch")


def _encode_json_value(
    value: object,
    public: frozenset[str],
    exempt: frozenset[LeafPath] = frozenset(),
    prefix: LeafPath = (),
) -> bytes:
    if value is None or isinstance(value, (str, bool, int, Decimal)):
        return _encode_scalar(value, "json")
    if isinstance(value, dict):
        parts = []
        for key, child in value.items():
            full = (*prefix, key)
            if not isinstance(key, str) or (
                full not in exempt and is_sensitive_name(key, public=public)
            ):
                raise FormatError(code="sensitive_input")
            parts.append(
                _encode_json_string(key)
                + b":"
                + _encode_json_value(child, public, exempt, full)
            )
        return b"{" + b",".join(parts) + b"}"
    raise FormatError(code="patch")


def _encode_json_string(value: str) -> bytes:
    _ensure_utf8(value)
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, UnicodeEncodeError):
        raise FormatError(code="patch") from None


def _encode_toml_key(value: str) -> str:
    if _BARE_TOML_KEY.fullmatch(value):
        return value
    return _encode_json_string(value).decode("utf-8")


def _encode_toml_inline_value(
    value: object,
    public: frozenset[str],
    exempt: frozenset[LeafPath] = frozenset(),
    prefix: LeafPath = (),
) -> bytes:
    if isinstance(value, dict):
        if not value:
            return b"{}"
        pairs: list[bytes] = []
        for key, child in value.items():
            full = (*prefix, key)
            if not isinstance(key, str) or (
                full not in exempt and is_sensitive_name(key, public=public)
            ):
                raise FormatError(code="sensitive_input")
            pairs.append(
                _encode_toml_key(key).encode("utf-8")
                + b" = "
                + _encode_toml_inline_value(child, public, exempt, full)
            )
        return b"{ " + b", ".join(pairs) + b" }"
    return _encode_scalar(value, "toml")


def _apply_byte_replacements(
    data: bytes, replacements: list[tuple[int, int, bytes]]
) -> bytes:
    ordered = sorted(replacements, key=lambda item: (item[0], item[1]), reverse=True)
    previous_start = len(data) + 1
    result = data
    for start, end, replacement in ordered:
        if start < 0 or end < start or end > len(data) or end > previous_start:
            raise FormatError(code="patch")
        result = result[:start] + replacement + result[end:]
        previous_start = start
    return result


def _json_multiline(data: bytes, node: _Node) -> bool:
    if node.open_end is None or node.close_start is None:
        return False
    return (
        b"\n" in data[node.open_end : node.close_start]
        or b"\r" in data[node.open_end : node.close_start]
    )


def _json_newline(data: bytes) -> bytes:
    position = data.find(b"\n")
    if position > 0 and data[position - 1 : position] == b"\r":
        return b"\r\n"
    return b"\n"


def _json_closing_indent(data: bytes, node: _Node) -> bytes:
    if node.close_start is None:
        return b""
    line_start = (
        max(
            data.rfind(b"\n", 0, node.close_start),
            data.rfind(b"\r", 0, node.close_start),
        )
        + 1
    )
    prefix = data[line_start : node.close_start]
    return prefix if not prefix.strip(b" \t") else b""


def _json_separator(data: bytes, node: _Node, last: _JsonMember) -> bytes:
    if _json_multiline(data, node):
        newline = _json_newline(data)
        line_start = (
            max(
                data.rfind(b"\n", 0, last.key_start),
                data.rfind(b"\r", 0, last.key_start),
            )
            + 1
        )
        indent = data[line_start : last.key_start]
        if indent.strip(b" \t"):
            indent = _json_closing_indent(data, node) + b"  "
        return newline + indent
    return b" "


def _toml_newline(data: bytes) -> bytes:
    position = data.find(b"\n")
    if position > 0 and data[position - 1 : position] == b"\r":
        return b"\r\n"
    return b"\n"
