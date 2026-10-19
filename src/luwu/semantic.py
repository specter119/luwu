"""Strict public JSON parsing and value equality shared by baselines and ownership."""

from __future__ import annotations

import json
from decimal import Decimal, DecimalException
from typing import Any


class _DuplicateObjectKey(ValueError):
    """Internal signal for a duplicate object member."""


class _NonFiniteNumber(ValueError):
    """Internal signal for JSON extensions such as NaN and Infinity."""


_MAX_JSON_NESTING = 128


def _parse_strict_json(
    data: bytes,
) -> tuple[Any | None, str | None]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, "invalid_utf8"
    if _exceeds_nesting_limit(text):
        return None, "json_nesting_limit"

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=_reject_non_finite_number,
            parse_float=Decimal,
            parse_int=Decimal,
            strict=True,
        )
    except _DuplicateObjectKey:
        return None, "duplicate_object_key"
    except _NonFiniteNumber:
        return None, "non_finite_number"
    except DecimalException:
        return None, "json_number_out_of_range"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except RecursionError:
        return None, "json_nesting_limit"
    if _contains_surrogate(value):
        return None, "json_surrogate"
    return value, None


def _exceeds_nesting_limit(text: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_NESTING:
                return True
        elif character in "]}":
            depth = max(0, depth - 1)
    return False


def _contains_surrogate(value: Any) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                return True
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _object_pairs_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise _DuplicateObjectKey
        values[key] = value
    return values


def _reject_non_finite_number(value: str) -> None:
    raise _NonFiniteNumber(value)


def _json_values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        if left.keys() != right.keys():
            return False
        return all(_json_values_equal(left[key], right[key]) for key in left)

    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        return (
            isinstance(left, Decimal) and isinstance(right, Decimal) and left == right
        )
    return type(left) is type(right) and left == right
