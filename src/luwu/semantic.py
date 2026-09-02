"""Small, explicit semantic comparison adapters for future reconciliation.

The adapter compares an already-rendered desired byte sequence with live bytes.
It does not read files, render templates, format text, or return configuration
content.  A JSON comparison is deliberately limited to strict UTF-8 JSON and
uses the JSON data model without dropping fields or changing array order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from enum import StrEnum
from typing import Any


class ComparisonStrategy(StrEnum):
    """The explicitly selected equivalence relation."""

    EXACT_BYTES = "exact-bytes"
    JSON = "json"


class ComparisonStatus(StrEnum):
    """The comparison result used by a future observation or plan."""

    EXACT = "exact"
    EQUIVALENT_BUT_REFORMATTED = "equivalent-but-reformatted"
    DIFFERENT = "different"
    UNSUPPORTED = "unsupported"
    NOT_COMPARED = "not-compared"


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Metadata-only result of comparing desired bytes and live bytes."""

    strategy: ComparisonStrategy
    status: ComparisonStatus
    code: str
    reason: str

    @property
    def equivalent(self) -> bool:
        """Whether the selected strategy considers both values equivalent."""

        return self.status in {
            ComparisonStatus.EXACT,
            ComparisonStatus.EQUIVALENT_BUT_REFORMATTED,
        }

    def to_dict(self) -> dict[str, str | bool]:
        """Return a safe machine-readable representation without config data."""

        return {
            "strategy": self.strategy.value,
            "status": self.status.value,
            "code": self.code,
            "reason": self.reason,
            "equivalent": self.equivalent,
        }


@dataclass(frozen=True, slots=True)
class SemanticComparisonAdapter:
    """Compare byte sequences using one fixed, explicit strategy.

    The desired/live byte boundary matches the output of rendering and the
    result is independent of filesystem access.  Reconcile can therefore own
    observation and action while injecting this adapter for comparison.
    """

    strategy: ComparisonStrategy | str = ComparisonStrategy.EXACT_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy", ComparisonStrategy(self.strategy))

    def compare(self, desired: bytes, live: bytes) -> ComparisonResult:
        """Compare desired bytes with live bytes without exposing their content."""

        _require_bytes(desired, name="desired")
        _require_bytes(live, name="live")
        if self.strategy is ComparisonStrategy.EXACT_BYTES:
            return _compare_exact_bytes(desired, live)
        return _compare_json(desired, live)


def compare(
    desired: bytes,
    live: bytes,
    *,
    strategy: ComparisonStrategy | str = ComparisonStrategy.EXACT_BYTES,
) -> ComparisonResult:
    """Compare desired/live bytes with an explicit or exact-byte strategy."""

    return SemanticComparisonAdapter(strategy).compare(desired, live)


def not_compared(
    strategy: ComparisonStrategy | str,
    *,
    code: str,
    reason: str,
) -> ComparisonResult:
    """Describe an observation where the selected comparator did not run."""

    return _result(
        ComparisonStrategy(strategy),
        ComparisonStatus.NOT_COMPARED,
        code=code,
        reason=reason,
    )


def _require_bytes(value: object, *, name: str) -> None:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")


def _compare_exact_bytes(desired: bytes, live: bytes) -> ComparisonResult:
    if desired == live:
        return _result(
            ComparisonStrategy.EXACT_BYTES,
            ComparisonStatus.EXACT,
            code="bytes_equal",
            reason="desired and live byte sequences are identical",
        )
    return _result(
        ComparisonStrategy.EXACT_BYTES,
        ComparisonStatus.DIFFERENT,
        code="bytes_differ",
        reason="desired and live byte sequences differ",
    )


def _compare_json(desired: bytes, live: bytes) -> ComparisonResult:
    desired_value, desired_error = _parse_strict_json(desired)
    live_value, live_error = _parse_strict_json(live)
    if desired_error is not None or live_error is not None:
        error_code = _first_error_code(desired_error, live_error)
        return _result(
            ComparisonStrategy.JSON,
            ComparisonStatus.UNSUPPORTED,
            code=error_code,
            reason=_UNSUPPORTED_REASONS[error_code],
        )

    if desired == live:
        return _result(
            ComparisonStrategy.JSON,
            ComparisonStatus.EXACT,
            code="json_bytes_equal",
            reason="strict JSON byte sequences are identical",
        )
    try:
        equivalent = _json_values_equal(desired_value, live_value)
    except DecimalException:
        return _result(
            ComparisonStrategy.JSON,
            ComparisonStatus.UNSUPPORTED,
            code="json_number_out_of_range",
            reason=_UNSUPPORTED_REASONS["json_number_out_of_range"],
        )
    except RecursionError:
        return _result(
            ComparisonStrategy.JSON,
            ComparisonStatus.UNSUPPORTED,
            code="json_nesting_limit",
            reason=_UNSUPPORTED_REASONS["json_nesting_limit"],
        )
    if equivalent:
        return _result(
            ComparisonStrategy.JSON,
            ComparisonStatus.EQUIVALENT_BUT_REFORMATTED,
            code="json_values_equivalent",
            reason="strict JSON values are equivalent but represented differently",
        )
    return _result(
        ComparisonStrategy.JSON,
        ComparisonStatus.DIFFERENT,
        code="json_values_differ",
        reason="strict JSON values differ",
    )


def _result(
    strategy: ComparisonStrategy,
    status: ComparisonStatus,
    *,
    code: str,
    reason: str,
) -> ComparisonResult:
    return ComparisonResult(strategy, status, code, reason)


class _DuplicateObjectKey(ValueError):
    """Internal signal for a duplicate object member."""


class _NonFiniteNumber(ValueError):
    """Internal signal for JSON extensions such as NaN and Infinity."""


_UNSUPPORTED_REASONS = {
    "invalid_utf8": "one or both inputs are not valid UTF-8",
    "duplicate_object_key": "a JSON object contains a duplicate key",
    "non_finite_number": "JSON numbers must be finite",
    "invalid_json": "one or both inputs are not strict JSON",
    "json_number_out_of_range": "a JSON number is outside the supported numeric range",
    "json_nesting_limit": "JSON nesting exceeds the supported depth",
    "json_surrogate": "JSON strings must contain Unicode scalar values",
}

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


def _first_error_code(*error_codes: str | None) -> str:
    for error_code in error_codes:
        if error_code is not None:
            return error_code
    raise AssertionError("at least one error code is required")


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
