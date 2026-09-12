"""Pure field ownership classification for the M3a JSON contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .errors import LuwuError
from .semantic import _json_values_equal, _parse_strict_json

_OWNERS = frozenset({"source", "live", "merge", "ignore"})
_MISSING = object()
_INVALID_INPUT = "ownership input is invalid"


@dataclass(frozen=True, slots=True)
class OwnershipField:
    """Safe metadata for one declared field."""

    name: str
    owner: str
    status: str
    decision: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "owner": self.owner,
            "status": self.status,
            "decision": self.decision,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OwnershipResult:
    """Metadata-only result of classifying desired, live, and baseline JSON."""

    fields: tuple[OwnershipField, ...]
    baseline_status: str
    undeclared_changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": [field.to_dict() for field in self.fields],
            "baseline_status": self.baseline_status,
            "undeclared_changed": self.undeclared_changed,
        }


def classify_fields(
    desired: bytes,
    live: bytes,
    *,
    fields: Mapping[str, str],
    baseline: bytes | None,
    resource_name: str,
    source_name: str,
    target_name: str,
) -> OwnershipResult:
    """Classify declared JSON fields without returning configuration values."""

    _require_bytes(desired)
    _require_bytes(live)
    if baseline is not None:
        _require_bytes(baseline)
    _validate_declaration(fields, resource_name, source_name, target_name)

    desired_value = _parse_object(desired)
    live_value = _parse_object(live)
    baseline_value = _parse_baseline(
        baseline, fields, resource_name, source_name, target_name
    )

    baseline_values = None if baseline_value is None else baseline_value["values"]
    result_fields = tuple(
        _classify_field(
            name,
            owner,
            desired_value,
            live_value,
            baseline_values,
            baseline is not None,
        )
        for name, owner in sorted(fields.items())
    )
    declared = set(fields)
    undeclared_keys = (set(desired_value) | set(live_value)) - declared
    undeclared_changed = any(
        not _values_equal(
            desired_value.get(key, _MISSING), live_value.get(key, _MISSING)
        )
        for key in undeclared_keys
    )
    return OwnershipResult(
        fields=result_fields,
        baseline_status="provided" if baseline is not None else "absent",
        undeclared_changed=undeclared_changed,
    )


def _classify_field(
    name: str,
    owner: str,
    desired: dict[str, Any],
    live: dict[str, Any],
    baseline: dict[str, Any] | None,
    based: bool,
) -> OwnershipField:
    if owner == "ignore":
        return OwnershipField(name, owner, "ignored", "none", "field is ignored")
    if not based:
        return OwnershipField(name, owner, "unbased", "none", "baseline is absent")
    if baseline is None:
        raise _invalid()

    desired_value = desired.get(name, _MISSING)
    live_value = live.get(name, _MISSING)
    baseline_value = baseline.get(name, _MISSING)
    desired_changed = not _values_equal(desired_value, baseline_value)
    live_changed = not _values_equal(live_value, baseline_value)

    if not desired_changed and not live_changed:
        status, decision, reason = (
            "unchanged",
            "none",
            "desired and live match baseline",
        )
    elif _values_equal(desired_value, live_value):
        status, decision, reason = "converged", "none", "desired and live converged"
    elif desired_changed and not live_changed:
        status, decision, reason = (
            "source_changed",
            _forward_decision(owner),
            "desired changed from baseline",
        )
    elif live_changed and not desired_changed:
        status, decision, reason = (
            "live_changed",
            _reverse_decision(owner),
            "live changed from baseline",
        )
    else:
        status, decision, reason = (
            "conflict",
            "review",
            "desired and live changed from baseline",
        )
    return OwnershipField(name, owner, status, decision, reason)


def _values_equal(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return _json_values_equal(left, right)


def _forward_decision(owner: str) -> str:
    return "forward_candidate" if owner in {"source", "merge"} else "review"


def _reverse_decision(owner: str) -> str:
    return "reverse_candidate" if owner in {"live", "merge"} else "review"


def _parse_object(data: bytes) -> dict[str, Any]:
    value, error = _parse_strict_json(data)
    if error is not None or not isinstance(value, dict):
        raise _invalid()
    return value


def _parse_baseline(
    data: bytes | None,
    fields: Mapping[str, str],
    resource_name: str,
    source_name: str,
    target_name: str,
) -> dict[str, Any] | None:
    if data is None:
        return None
    value, error = _parse_strict_json(data)
    if error is not None or not isinstance(value, dict):
        raise _invalid()
    if set(value) != {
        "schema_version",
        "resource",
        "source",
        "target",
        "owners",
        "values",
    }:
        raise _invalid()
    if isinstance(value["schema_version"], bool) or value["schema_version"] != Decimal(
        1
    ):
        raise _invalid()
    if (value["resource"], value["source"], value["target"]) != (
        resource_name,
        source_name,
        target_name,
    ):
        raise _invalid()
    owners = value["owners"]
    values = value["values"]
    if not isinstance(owners, dict) or owners != dict(fields):
        raise _invalid()
    if not isinstance(values, dict) or set(values) - {
        name for name, owner in fields.items() if owner != "ignore"
    }:
        raise _invalid()
    return value


def _validate_declaration(
    fields: Mapping[str, str], resource_name: str, source_name: str, target_name: str
) -> None:
    if not isinstance(fields, Mapping) or not fields:
        raise _invalid()
    if not all(
        isinstance(name, str)
        and bool(name)
        and isinstance(owner, str)
        and owner in _OWNERS
        for name, owner in fields.items()
    ):
        raise _invalid()
    if not all(
        isinstance(name, str) for name in (resource_name, source_name, target_name)
    ):
        raise _invalid()


def _require_bytes(value: object) -> None:
    if not isinstance(value, bytes):
        raise _invalid()


def _invalid() -> LuwuError:
    return LuwuError(_INVALID_INPUT, code="ownership_invalid_input")
