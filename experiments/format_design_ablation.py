# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Run format design probes with metadata-only JSON output.

Run from the repository root with ``uv run --script experiments/format_design_ablation.py``.
"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from luwu import m6_formats
from luwu.errors import MutationError
from luwu.m6_formats import M6FormatError, parse_m6_document, patch_m6_document
from luwu.semantic import _json_values_equal

_LEGACY_ASSIGNMENT = re.compile(
    r"^[ \t]*(?P<key>[A-Za-z0-9_-]+)[ \t]*=[ \t]*(?P<value>.*?)[ \t]*$"
)
_LEGACY_INTEGER = re.compile(r"[+-]?\d+")
_LEGACY_DECIMAL = re.compile(r"[+-]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?")
_INVALID_TOML = (
    b"model = 01\n",
    b"model = 1__0\n",
    b'model = "\\/"\n',
)
_VALID_TOML = b"model = 1\n"
_VALID_UNICODE_TOML = b'model = "\\U0001F642"\n'


def _legacy_guard_off_values(text: str) -> dict[str, object]:
    """Reproduce the former scalar conversion without standard TOML validation."""

    values: dict[str, object] = {}
    for line in text.splitlines():
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        match = _LEGACY_ASSIGNMENT.fullmatch(line)
        if match is None:
            raise M6FormatError("legacy TOML subset rejected input")
        key = match.group("key")
        raw_value = match.group("value").strip(" \t")
        if key in values:
            raise M6FormatError("legacy TOML subset rejected input")
        if raw_value.startswith('"'):
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                raise M6FormatError("legacy TOML subset rejected input") from None
        elif raw_value in {"true", "false"}:
            value = raw_value == "true"
        else:
            numeric = raw_value.replace("_", "")
            if _LEGACY_INTEGER.fullmatch(numeric):
                value = int(numeric)
            elif _LEGACY_DECIMAL.fullmatch(numeric):
                value = Decimal(numeric)
            else:
                raise M6FormatError("legacy TOML subset rejected input")
        values[key] = value
    return values


def _toml_accepts(source: bytes, *, guard_off: bool = False) -> bool:
    try:
        if guard_off:
            with patch.object(
                m6_formats, "_load_toml_values", _legacy_guard_off_values
            ):
                parse_m6_document(source, format="toml")
        else:
            parse_m6_document(source, format="toml")
    except MutationError:
        return False
    return True


def _json_serialization_probe() -> dict[str, object]:
    environment = Environment()
    typed_text = environment.from_string("{{ value | tojson }}").render(value=None)
    quoted_text = environment.from_string('"{{ value | tojson }}"').render(value=None)
    typed_value = json.loads(typed_text)
    quoted_value = json.loads(quoted_text)
    typed_matches_null = _json_values_equal(typed_value, None)
    quoted_matches_null = _json_values_equal(quoted_value, None)
    passed = typed_matches_null and not quoted_matches_null
    return {
        "probe": "typed_json_null",
        "typed_serialization_matches_null": typed_matches_null,
        "quoted_serialization_matches_null": quoted_matches_null,
        "status": "pass" if passed else "fail",
    }


def _deletion_probe() -> dict[str, object]:
    source = b'{"model":"old","keep":1}\n'
    deleted = patch_m6_document(source, format="json", deletes=("model",))
    desired = parse_m6_document(deleted, format="json")
    live_absent = parse_m6_document(b'{"keep":1}\n', format="json")
    live_null = parse_m6_document(b'{"model":null,"keep":1}\n', format="json")
    missing = object()

    naive_verification = (
        "model" in desired
        and "model" in live_absent
        and _json_values_equal(desired["model"], live_absent["model"])
    )
    presence_aware_verification = _json_values_equal(
        desired.get("model", missing), live_absent.get("model", missing)
    )
    absent_matches_null = _json_values_equal(
        desired.get("model", missing), live_null.get("model", missing)
    )
    passed = (
        "model" not in desired
        and not naive_verification
        and presence_aware_verification
        and not absent_matches_null
    )
    return {
        "probe": "presence_aware_deletion",
        "deleted_field_absent": "model" not in desired,
        "naive_verification_passes": naive_verification,
        "presence_aware_verification_passes": presence_aware_verification,
        "absent_matches_json_null": absent_matches_null,
        "status": "pass" if passed else "fail",
    }


def _toml_guard_probes() -> list[dict[str, object]]:
    legacy_positive = _toml_accepts(_VALID_TOML, guard_off=True)
    legacy_counterexamples = [
        _toml_accepts(source, guard_off=True) for source in _INVALID_TOML
    ]
    legacy_unicode = _toml_accepts(_VALID_UNICODE_TOML, guard_off=True)
    legacy_passed = (
        legacy_positive and all(legacy_counterexamples) and not legacy_unicode
    )

    guarded_positive = _toml_accepts(_VALID_TOML)
    guarded_counterexamples = [_toml_accepts(source) for source in _INVALID_TOML]
    guarded_unicode = _toml_accepts(_VALID_UNICODE_TOML)
    guarded_passed = (
        guarded_positive and not any(guarded_counterexamples) and guarded_unicode
    )

    return [
        {
            "probe": "toml_standard_syntax_guard",
            "mode": "legacy_guard_off",
            "positive_control_accepted": legacy_positive,
            "invalid_counterexamples_accepted": sum(legacy_counterexamples),
            "valid_unicode_escape_accepted": legacy_unicode,
            "status": "pass" if legacy_passed else "fail",
        },
        {
            "probe": "toml_standard_syntax_guard",
            "mode": "tomllib_guard_on",
            "positive_control_accepted": guarded_positive,
            "invalid_counterexamples_accepted": sum(guarded_counterexamples),
            "valid_unicode_escape_accepted": guarded_unicode,
            "status": "pass" if guarded_passed else "fail",
        },
    ]


def _span_preservation_probe() -> dict[str, object]:
    source = b'# keep\r\nmodel = "old"\r\nprovider = "same"\r\n'
    patched = patch_m6_document(source, format="toml", updates={"model": "new"})
    preserved = patched == source.replace(b'"old"', b'"new"', 1)
    return {
        "probe": "existing_span_patcher",
        "new_adapter": False,
        "preserves_comments_crlf_and_unselected_bytes": preserved,
        "status": "pass" if preserved else "fail",
    }


def main() -> int:
    probes = [
        _json_serialization_probe(),
        _deletion_probe(),
        *_toml_guard_probes(),
        _span_preservation_probe(),
    ]
    passed = all(probe["status"] == "pass" for probe in probes)
    report = {
        "probes": probes,
        "status": "pass" if passed else "fail",
        "decision": "retain typed serialization and bounded span patching; "
        "standard TOML validation rejects the reproduced legacy forms",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
