"""Pure, narrow reverse-sync patching for literal JSON sources."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from .baseline import _MISSING, _encode_value, parse_public_object
from .errors import MutationError


@dataclass(frozen=True, slots=True)
class SourcePatch:
    """A metadata description plus the new source bytes for one write."""

    data: bytes = field(repr=False)
    fields: tuple[str, ...]
    source_keys: tuple[str, ...]
    changes: tuple[tuple[str, str, bool], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": "write_source_input",
            "fields": list(self.fields),
            "source_keys": list(self.source_keys),
            "changes": [
                {
                    "key": key,
                    "operation": operation,
                    "separator_adjusted": separator_adjusted,
                }
                for key, operation, separator_adjusted in self.changes
            ],
        }


def build_source_patch(
    source: bytes,
    live: bytes,
    *,
    fields: Mapping[str, str],
    owners: Mapping[str, str],
    reverse_sync: Mapping[str, str],
    selected_fields: tuple[str, ...],
) -> SourcePatch:
    """Patch only explicitly mapped top-level JSON source keys."""

    if any(
        token in source.decode("utf-8", errors="ignore") for token in ("{{", "{%", "{#")
    ):
        raise MutationError(
            "reverse-sync does not rewrite dynamic templates",
            code="reverse_sync_unsupported",
        )
    source_value = parse_public_object(source, code="reverse_sync_unsupported")
    live_value = parse_public_object(live, code="reverse_sync_unsupported")
    if not selected_fields:
        raise MutationError(
            "at least one field must be selected", code="field_required"
        )
    if len(set(selected_fields)) != len(selected_fields):
        raise MutationError(
            "fields must be selected at most once", code="field_duplicate"
        )

    source_keys: list[str] = []
    seen_source_keys: set[str] = set()
    for name in selected_fields:
        if name not in fields:
            raise MutationError("field is not declared", code="field_not_declared")
        if owners.get(name) not in {"live", "merge"}:
            raise MutationError("field is not live-owned", code="field_not_selectable")
        source_key = reverse_sync.get(name)
        if source_key is None:
            raise MutationError(
                "field has no reverse-sync mapping", code="reverse_sync_unmapped"
            )
        if source_key != name or owners.get(source_key) not in {"live", "merge"}:
            raise MutationError(
                "literal reverse-sync requires an identity mapping to a live-owned field",
                code="reverse_sync_mapping",
            )
        if source_key in seen_source_keys:
            raise MutationError(
                "reverse-sync source keys must be unique",
                code="reverse_sync_duplicate_source",
            )
        seen_source_keys.add(source_key)
        source_keys.append(source_key)

    try:
        _, closing_start, members = _scan_literal_object(source)
    except _LiteralJsonUnsupported:
        raise MutationError(
            "literal JSON member boundaries cannot be determined safely",
            code="reverse_sync_unsupported",
        ) from None

    scanned_keys = tuple(member[0] for member in members)
    if scanned_keys != tuple(source_value):
        raise MutationError(
            "literal JSON members do not match strict parsed keys",
            code="reverse_sync_unsupported",
        )

    source_members = {member[0]: member for member in members}
    replacements: dict[str, bytes] = {}
    deleted_keys: set[str] = set()
    added_keys: list[str] = []
    operations: dict[str, str] = {}
    for name in selected_fields:
        source_key = reverse_sync[name]
        value = live_value.get(name, _MISSING)
        if source_key in source_members:
            if value is _MISSING:
                deleted_keys.add(source_key)
                operations[source_key] = "delete"
            else:
                replacements[source_key] = _encode_json_value(value)
                operations[source_key] = "replace"
        elif value is not _MISSING:
            added_keys.append(source_key)
            operations[source_key] = "add"

    has_retained_source = any(member[0] not in deleted_keys for member in members)
    changes = tuple(
        (
            source_key,
            operations[source_key],
            (operations[source_key] == "delete" and len(members) > 1)
            or (operations[source_key] == "add" and has_retained_source),
        )
        for source_key in source_keys
        if source_key in operations
    )

    data = _patch_literal_object(
        source,
        closing_start=closing_start,
        members=members,
        replacements=replacements,
        deleted_keys=deleted_keys,
        added_keys=added_keys,
        live_value=live_value,
    )
    return SourcePatch(
        data=data,
        fields=tuple(selected_fields),
        source_keys=tuple(source_keys),
        changes=changes,
    )


class _LiteralJsonUnsupported(ValueError):
    """The narrow literal-JSON scanner cannot prove a safe boundary."""


def _scan_literal_object(
    data: bytes,
) -> tuple[int, int, list[tuple[str, int, int, int]]]:
    """Return root-object boundaries and decoded top-level member spans.

    The scanner only discovers lexical boundaries.  Strict value validation is
    deliberately delegated to :func:`parse_public_object`.
    """

    opening_start = _skip_json_whitespace(data, 0)
    if opening_start >= len(data) or data[opening_start] != ord("{"):
        raise _LiteralJsonUnsupported
    opening_end = opening_start + 1
    cursor = _skip_json_whitespace(data, opening_end)
    members: list[tuple[str, int, int, int]] = []
    seen_keys: set[str] = set()

    if cursor < len(data) and data[cursor] == ord("}"):
        closing_start = cursor
    else:
        while True:
            key_start = cursor
            key_end = _scan_json_string_end(data, key_start)
            key = _decode_json_key(data[key_start:key_end])
            if key in seen_keys:
                raise _LiteralJsonUnsupported
            seen_keys.add(key)

            cursor = _skip_json_whitespace(data, key_end)
            if cursor >= len(data) or data[cursor] != ord(":"):
                raise _LiteralJsonUnsupported
            value_start = _skip_json_whitespace(data, cursor + 1)
            value_end = _scan_json_value_end(data, value_start)
            members.append((key, key_start, value_start, value_end))

            cursor = _skip_json_whitespace(data, value_end)
            if cursor >= len(data):
                raise _LiteralJsonUnsupported
            if data[cursor] == ord("}"):
                closing_start = cursor
                break
            if data[cursor] != ord(","):
                raise _LiteralJsonUnsupported
            cursor = _skip_json_whitespace(data, cursor + 1)
            if cursor >= len(data) or data[cursor] in b"}":
                raise _LiteralJsonUnsupported

    if _skip_json_whitespace(data, closing_start + 1) != len(data):
        raise _LiteralJsonUnsupported
    return opening_end, closing_start, members


def _scan_json_value_end(data: bytes, start: int) -> int:
    if start >= len(data):
        raise _LiteralJsonUnsupported
    marker = data[start]
    if marker == ord('"'):
        return _scan_json_string_end(data, start)
    if marker == ord("{"):
        return _scan_json_container_end(data, start, close=ord("}"), object_value=True)
    if marker == ord("["):
        return _scan_json_container_end(data, start, close=ord("]"), object_value=False)

    cursor = start
    while cursor < len(data) and data[cursor] not in b" \t\r\n,]}:":
        cursor += 1
    if cursor == start:
        raise _LiteralJsonUnsupported
    return cursor


def _scan_json_container_end(
    data: bytes,
    start: int,
    *,
    close: int,
    object_value: bool,
) -> int:
    cursor = _skip_json_whitespace(data, start + 1)
    if cursor < len(data) and data[cursor] == close:
        return cursor + 1

    while True:
        if object_value:
            key_end = _scan_json_string_end(data, cursor)
            cursor = _skip_json_whitespace(data, key_end)
            if cursor >= len(data) or data[cursor] != ord(":"):
                raise _LiteralJsonUnsupported
            cursor = _skip_json_whitespace(data, cursor + 1)
        value_end = _scan_json_value_end(data, cursor)
        cursor = _skip_json_whitespace(data, value_end)
        if cursor >= len(data):
            raise _LiteralJsonUnsupported
        if data[cursor] == close:
            return cursor + 1
        if data[cursor] != ord(","):
            raise _LiteralJsonUnsupported
        cursor = _skip_json_whitespace(data, cursor + 1)
        if cursor >= len(data) or data[cursor] == close:
            raise _LiteralJsonUnsupported


def _scan_json_string_end(data: bytes, start: int) -> int:
    if start >= len(data) or data[start] != ord('"'):
        raise _LiteralJsonUnsupported
    cursor = start + 1
    while cursor < len(data):
        marker = data[cursor]
        if marker == ord('"'):
            return cursor + 1
        if marker == ord("\\"):
            cursor += 2
            continue
        if marker < 0x20:
            raise _LiteralJsonUnsupported
        cursor += 1
    raise _LiteralJsonUnsupported


def _decode_json_key(token: bytes) -> str:
    try:
        value = json.loads(token.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise _LiteralJsonUnsupported from None
    if not isinstance(value, str):
        raise _LiteralJsonUnsupported
    return value


def _skip_json_whitespace(data: bytes, start: int) -> int:
    cursor = start
    while cursor < len(data) and data[cursor] in b" \t\r\n":
        cursor += 1
    return cursor


def _encode_json_value(value: Any) -> bytes:
    try:
        return _encode_value(value).encode("utf-8")
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise MutationError(
            "live JSON value cannot be encoded safely",
            code="reverse_sync_unsupported",
        ) from exc


def _encode_json_member(key: str, value: Any) -> bytes:
    try:
        encoded_key = json.dumps(key, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise MutationError(
            "live JSON key cannot be encoded safely",
            code="reverse_sync_unsupported",
        ) from exc
    return encoded_key + b":" + _encode_json_value(value)


def _patch_literal_object(
    source: bytes,
    *,
    closing_start: int,
    members: list[tuple[str, int, int, int]],
    replacements: Mapping[str, bytes],
    deleted_keys: set[str],
    added_keys: list[str],
    live_value: Mapping[str, Any],
) -> bytes:
    """Apply member/value edits while keeping all non-member bytes intact."""

    if not members:
        prefix = source[:closing_start]
        suffix = source[closing_start:]
        added = _join_added_members(added_keys, live_value)
        return prefix + added + suffix

    retained: list[tuple[int, bytes]] = []
    for index, (key, key_start, value_start, value_end) in enumerate(members):
        if key in deleted_keys:
            continue
        member = source[key_start:value_end]
        replacement = replacements.get(key)
        if replacement is not None:
            member = source[key_start:value_start] + replacement
        retained.append((index, member))

    added_positions = _added_positions(
        members,
        retained,
        added_keys,
        live_value,
    )
    entries: list[tuple[int | None, bytes]] = []
    retained_by_index = dict(retained)
    for index in range(len(members) + 1):
        for key in added_positions.get(index, ()):
            entries.append((None, _encode_json_member(key, live_value[key])))
        if index < len(members) and index in retained_by_index:
            entries.append((index, retained_by_index[index]))

    prefix = source[: members[0][1]]
    suffix = source[members[-1][3] : closing_start] + source[closing_start:]
    if not entries:
        return prefix + suffix

    separators = [
        source[members[index][3] : members[index + 1][1]]
        for index in range(len(members) - 1)
    ]
    separator_default = separators[0] if separators else b", "
    parts = [prefix, entries[0][1]]
    for previous, current in pairwise(entries):
        separator = _separator_between(
            previous[0], current[0], separators, separator_default
        )
        parts.extend((separator, current[1]))
    parts.append(suffix)
    return b"".join(parts)


def _join_added_members(added_keys: list[str], live_value: Mapping[str, Any]) -> bytes:
    return b", ".join(_encode_json_member(key, live_value[key]) for key in added_keys)


def _added_positions(
    members: list[tuple[str, int, int, int]],
    retained: list[tuple[int, bytes]],
    added_keys: list[str],
    live_value: Mapping[str, Any],
) -> dict[int, list[str]]:
    """Place additions at their live order without moving source members."""

    if not added_keys:
        return {}
    live_order = {key: index for index, key in enumerate(live_value)}
    retained_indices = {index for index, _ in retained}
    positions: dict[int, list[str]] = {}
    for added_key in added_keys:
        added_order = live_order[added_key]
        position = len(members)
        for index, (key, _, _, _) in enumerate(members):
            if index not in retained_indices:
                continue
            member_order = live_order.get(key)
            if member_order is not None and member_order > added_order:
                position = index
                break
        positions.setdefault(position, []).append(added_key)
    return positions


def _separator_between(
    previous_index: int | None,
    current_index: int | None,
    separators: list[bytes],
    default: bytes,
) -> bytes:
    if (
        previous_index is not None
        and current_index is not None
        and current_index == previous_index + 1
        and previous_index < len(separators)
    ):
        return separators[previous_index]
    if previous_index is not None and previous_index < len(separators):
        return separators[previous_index]
    if current_index is not None and current_index > 0:
        return separators[current_index - 1]
    return default
