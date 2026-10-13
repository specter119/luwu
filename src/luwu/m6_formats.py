"""Bounded public input loading and field patching for the M6 pilot."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .baseline import _encode_value, parse_public_object
from .errors import MutationError
from .filesystem import (
    FileChangedError,
    NotRegularFileError,
    open_parent_directory,
    read_regular_file_at,
)
from .manifest import M6Roots, Resource
from .reverse_sync import build_source_patch


class M6FormatError(MutationError):
    """A public pilot document is outside the bounded format subset."""


@dataclass(frozen=True, slots=True)
class M6InputValue:
    """One public input value with private freshness evidence."""

    value: object = field(repr=False)
    provenance: str
    path: Path
    identity: tuple[int, int] = field(repr=False)
    digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class M6InputContext:
    """Resolved global/local values and private input freshness evidence."""

    global_values: dict[str, object] = field(repr=False)
    local_values: dict[str, object] = field(repr=False)
    values: dict[str, M6InputValue] = field(repr=False)


_SAFE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_TOML_ASSIGNMENT = re.compile(
    r"^(?P<prefix>[ \t]*)(?P<key>[A-Za-z0-9_-]+)(?P<separator>[ \t]*=[ \t]*)"
    r"(?P<value>[^\r\n]*?)(?P<newline>\r?\n|$)$"
)
_SENSITIVE_NAMES = frozenset(
    {"api_key", "apikey", "credential", "password", "private_key", "secret", "token"}
)


def load_m6_inputs(resource: Resource, roots: M6Roots) -> M6InputContext:
    """Read only declared scalar JSON/TOML input values."""

    global_values: dict[str, object] = {}
    local_values: dict[str, object] = {}
    values: dict[str, M6InputValue] = {}
    for field_name, path in resource.inputs.items():
        try:
            relative = path.relative_to(roots.inputs.path)
        except ValueError:
            raise M6FormatError(
                "M6 input escaped the declared input root", code="m6_input_boundary"
            ) from None
        if not relative.parts or relative.parts[0] not in {"global", "local"}:
            raise M6FormatError(
                "M6 input provenance must be global or local",
                code="m6_input_provenance",
            )
        raw, identity = _read_input(path)
        document = _parse_document(raw, path=path)
        if field_name not in document:
            raise M6FormatError(
                "declared M6 input field is absent", code="m6_input_missing"
            )
        value = document[field_name]
        if _is_sensitive_name(field_name) or not _is_public_scalar(value):
            raise M6FormatError(
                "M6 input is outside the public scalar boundary",
                code="m6_input_unsupported",
            )
        item = M6InputValue(
            value=value,
            provenance=relative.parts[0],
            path=path,
            identity=identity,
            digest=hashlib.sha256(raw).hexdigest(),
        )
        values[field_name] = item
        target = global_values if item.provenance == "global" else local_values
        target[field_name] = value
    return M6InputContext(global_values, local_values, values)


def patch_m6_document(
    source: bytes,
    *,
    format: str,
    updates: dict[str, object] | None = None,
    deletes: tuple[str, ...] = (),
) -> bytes:
    """Patch selected top-level public fields while preserving other bytes."""

    updates = {} if updates is None else dict(updates)
    if set(updates) & set(deletes):
        raise M6FormatError("a field cannot be updated and deleted", code="m6_patch")
    selected = tuple(dict.fromkeys((*updates, *deletes)))
    if not selected:
        raise M6FormatError(
            "at least one M6 field must be selected", code="field_required"
        )
    for name in selected:
        if not isinstance(name, str) or not _SAFE_TOML_KEY.fullmatch(name):
            raise M6FormatError("M6 field name is unsupported", code="m6_patch")
    if format == "json":
        return _patch_json(source, updates, deletes, selected)
    if format == "toml":
        return _patch_toml(source, updates, deletes, selected)
    raise M6FormatError("M6 format is unsupported", code="m6_format")


def _patch_json(
    source: bytes,
    updates: dict[str, object],
    deletes: tuple[str, ...],
    selected: tuple[str, ...],
) -> bytes:
    document = parse_public_object(source, code="m6_format")
    changed = dict(document)
    changed.update(updates)
    for name in deletes:
        changed.pop(name, None)
    live = _encode_value(changed).encode("utf-8")
    fields = {name: "live" for name in selected}
    reverse_sync = {name: name for name in selected}
    patch = build_source_patch(
        source,
        live,
        fields=fields,
        owners=fields,
        reverse_sync=reverse_sync,
        selected_fields=selected,
    )
    return patch.data


def _patch_toml(
    source: bytes,
    updates: dict[str, object],
    deletes: tuple[str, ...],
    selected: tuple[str, ...],
) -> bytes:
    text = _decode_utf8(source)
    _parse_toml(text, path=None)
    lines = text.splitlines(keepends=True)
    spans: dict[str, tuple[int, int, str]] = {}
    offset = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("["):
            raise M6FormatError(
                "TOML tables are outside the M6 bounded subset", code="m6_format"
            )
        match = _TOML_ASSIGNMENT.match(line)
        if match is not None:
            key = match.group("key")
            if key in spans:
                raise M6FormatError("duplicate TOML field", code="m6_format")
            value_text = match.group("value").rstrip(" \t")
            if "#" in value_text:
                raise M6FormatError(
                    "inline TOML comments are outside the M6 bounded subset",
                    code="m6_format",
                )
            value_start = offset + match.start("value")
            value_end = value_start + len(value_text)
            spans[key] = (
                offset,
                offset + len(line),
                line[value_start - offset : value_end - offset],
            )
        offset += len(line)

    replacements: list[tuple[int, int, str]] = []
    for name in selected:
        if name in deletes:
            if name in spans:
                replacements.append((spans[name][0], spans[name][1], ""))
            continue
        value = updates[name]
        if not _is_public_scalar(value):
            raise M6FormatError(
                "TOML value is outside the scalar subset", code="m6_format"
            )
        literal = _encode_toml_scalar(value)
        if name in spans:
            line_start, line_end, _ = spans[name]
            line = text[line_start:line_end]
            match = _TOML_ASSIGNMENT.match(line)
            assert match is not None
            start = line_start + match.start("value")
            end = start + len(match.group("value").rstrip(" \t"))
            replacements.append((start, end, literal))
        else:
            suffix = "" if text.endswith(("\n", "\r")) or not text else "\n"
            replacements.append((len(text), len(text), f"{suffix}{name} = {literal}\n"))

    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text.encode("utf-8")


def parse_m6_document(data: bytes, *, format: str) -> dict[str, object]:
    """Parse one bounded M6 JSON or TOML object without exposing content in errors."""

    if format.lower().lstrip(".") == "json":
        try:
            value = json.loads(
                _decode_utf8(data),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise M6FormatError(
                "M6 JSON input is unsupported", code="m6_format"
            ) from None
    elif format.lower().lstrip(".") == "toml":
        value = _parse_toml(_decode_utf8(data), path=None)
    else:
        raise M6FormatError("M6 input format is unsupported", code="m6_format")
    if not isinstance(value, dict):
        raise M6FormatError("M6 input must be a top-level object", code="m6_format")
    return value


def encode_m6_object(value: dict[str, object]) -> bytes:
    """Encode an internal object for ownership comparison only."""

    return _encode_value(value).encode("utf-8")


def _parse_document(data: bytes, *, path: Path) -> dict[str, object]:
    return parse_m6_document(data, format=path.suffix)


def _parse_toml(text: str, *, path: Path | None) -> dict[str, object]:
    try:
        value = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise M6FormatError("M6 TOML input is unsupported", code="m6_format") from None
    if not isinstance(value, dict) or any(
        not _is_public_scalar(item) for item in value.values()
    ):
        raise M6FormatError(
            "M6 TOML must contain only top-level scalar values", code="m6_format"
        )
    if any(line.lstrip().startswith("[") for line in text.splitlines()):
        raise M6FormatError(
            "TOML tables are outside the M6 bounded subset", code="m6_format"
        )
    return value


def _read_input(path: Path) -> tuple[bytes, tuple[int, int]]:
    try:
        parent, name = open_parent_directory(path.parent, path)
        try:
            data, info = read_regular_file_at(parent, name)
        finally:
            import os

            os.close(parent)
    except (
        FileNotFoundError,
        NotRegularFileError,
        FileChangedError,
        OSError,
        ValueError,
    ):
        raise M6FormatError(
            "M6 input cannot be read safely", code="m6_input_unreadable"
        ) from None
    return data, (info.st_dev, info.st_ino)


def _decode_utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise M6FormatError(
            "M6 document is not valid UTF-8", code="m6_encoding"
        ) from None


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _is_public_scalar(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int, Decimal)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _is_sensitive_name(name: str) -> bool:
    folded = name.casefold()
    return folded in _SENSITIVE_NAMES or any(
        token in folded for token in _SENSITIVE_NAMES
    )


def _encode_toml_scalar(value: object) -> str:
    if value is None:
        raise M6FormatError("TOML has no null value", code="m6_format")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    raise M6FormatError("TOML value is outside the scalar subset", code="m6_format")
