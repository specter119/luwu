"""Schema-2 public baselines bound to complete declarations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .errors import LuwuError, MutationError
from .filesystem import DirectoryBoundary, FileObservation, observe_file, write_file
from .formats import MISSING, semantic_equal
from .manifest import InputBinding, InputLayer, Manifest, Resource
from .semantic import (
    _contains_surrogate,
    _exceeds_nesting_limit,
    _object_pairs_without_duplicates,
    _reject_non_finite_number,
)


def encode_public_json(value: Any) -> bytes:
    """Encode the supported JSON model without turning Decimal into a string."""

    return (_encode_value(value) + "\n").encode("utf-8")


def _encode_value(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MutationError(
                "JSON input contains a non-finite number",
                code="mutation_input_invalid",
            )
        return str(value)
    if isinstance(value, (int, float)):
        try:
            return json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MutationError(
                "JSON input contains an invalid number", code="mutation_input_invalid"
            ) from exc
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_encode_value(item) for item in value) + "]"
    if isinstance(value, dict):
        members = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise MutationError(
                    "JSON object keys must be strings", code="mutation_input_invalid"
                )
            members.append(
                json.dumps(key, ensure_ascii=False) + ":" + _encode_value(item)
            )
        return "{" + ",".join(members) + "}"
    raise MutationError(
        "JSON input contains an unsupported value", code="mutation_input_invalid"
    )


class BaselineError(LuwuError):
    """A fixed-message error for invalid or stale schema-2 baselines."""


@dataclass(frozen=True, slots=True)
class Baseline:
    """Validated baseline values and their private on-disk observation."""

    values: Mapping[str, object] = field(repr=False)
    whole_file_digest: str | None = field(repr=False)
    whole_file_mode: int | None = field(repr=False)
    observation: FileObservation = field(repr=False)
    data: bytes = field(repr=False)


def read_baseline(manifest: Manifest, resource: Resource) -> Baseline | None:
    """Read and validate a resource baseline without exposing its contents."""

    return load_baseline(manifest, resource)[1]


def load_baseline(
    manifest: Manifest, resource: Resource, *, allow_stale: bool = False
) -> tuple[FileObservation, Baseline | None]:
    """Return the baseline leaf observation and its validated schema-2 value.

    With ``allow_stale``, a baseline recorded for a different declaration is
    reported as absent so the caller can offer an explicit re-accept. The
    observation still describes the stale file, so the replacement is guarded
    by its identity.
    """

    root = manifest.roots.inputs.path
    path = _absolute(root, resource.baseline)
    observation = observe_file(root, path)
    if observation.data is None:
        return observation, None
    try:
        validated_values, whole_file_digest, whole_file_mode = _validate_baseline_data(
            observation.data, manifest, resource
        )
    except BaselineError as exc:
        if allow_stale and exc.code == "baseline_stale":
            return observation, None
        raise
    return observation, Baseline(
        values=MappingProxyType(validated_values),
        whole_file_digest=whole_file_digest,
        whole_file_mode=whole_file_mode,
        observation=observation,
        data=observation.data,
    )


def make_field_baseline(
    manifest: Manifest,
    resource: Resource,
    *,
    current: Mapping[str, object],
    selected_fields: tuple[str, ...],
    existing: Baseline | None,
) -> bytes:
    """Accept selected declared fields while preserving other accepted values."""

    fields = resource.fields
    if not selected_fields or len(set(selected_fields)) != len(selected_fields):
        raise BaselineError("baseline field selection is invalid", code="field_invalid")
    values = {} if existing is None else dict(existing.values)
    for name in selected_fields:
        binding = fields.get(name)
        if binding is None or binding.owner == "ignore":
            raise BaselineError(
                "baseline field is not selectable", code="field_invalid"
            )
        value = current.get(name, MISSING)
        if value is MISSING:
            values.pop(name, None)
        elif not _is_public_scalar(value):
            raise BaselineError(
                "baseline value is outside the public scalar boundary",
                code="baseline_invalid",
            )
        else:
            values[name] = value
    return _encode_envelope(manifest, resource, values)


def make_copy_baseline(
    manifest: Manifest,
    resource: Resource,
    *,
    accepted_data: bytes,
    accepted_mode: int,
) -> bytes:
    """Accept only a whole-file digest for a declared copy resource."""

    if (
        type(accepted_data) is not bytes
        or type(accepted_mode) is not int
        or not 0 <= accepted_mode <= 0o777
        or resource.kind != "copy"
    ):
        raise BaselineError("whole-file adoption is invalid", code="adopt_invalid")
    return _encode_envelope(
        manifest,
        resource,
        {
            "whole_file": {
                "algorithm": "sha256",
                "digest": hashlib.sha256(accepted_data).hexdigest(),
            },
            "mode": accepted_mode,
        },
    )


def write_baseline(
    manifest: Manifest,
    resource: Resource,
    *,
    data: bytes,
    expected: FileObservation,
    check_inputs: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Atomically replace a baseline under the declared input root."""

    root = manifest.roots.inputs.path
    path = _absolute(root, resource.baseline)
    return write_file(
        root, path, data, expected=expected, mode=0o600, check_inputs=check_inputs
    )


def baseline_identity(manifest: Manifest, resource: Resource) -> dict[str, object]:
    """Return this resource's stable declaration identity for its baseline."""

    root_identity = {
        "source": _root_identity(manifest.roots.source),
        "inputs": _root_identity(manifest.roots.inputs),
        "target": _root_identity(manifest.roots.target),
    }
    bindings = resource.fields
    input_names = sorted(
        {binding.input for binding in bindings.values() if binding.input is not None}
    )
    input_identity = {
        name: _binding_identity(manifest.inputs[name], manifest.roots.inputs.path)
        for name in input_names
    }
    mapping = {
        name: {
            "path": list(binding.path),
            "owner": binding.owner,
            "input": binding.input,
        }
        for name, binding in sorted(bindings.items())
    }
    return {
        "resource": resource.name,
        "kind": resource.kind,
        "source": resource.source_name,
        "target": resource.target_name,
        "baseline": _relative_name(manifest.roots.inputs.path, resource.baseline),
        "format": resource.format,
        "profile": manifest.profile,
        "roots": root_identity,
        "mapping": mapping,
        "inputs": input_identity,
        "parents": resource.parents,
        "parent_mode": resource.parent_mode,
        "mode": resource.mode,
    }


def _encode_envelope(
    manifest: Manifest,
    resource: Resource,
    values: Mapping[str, object],
) -> bytes:
    envelope = {
        "schema_version": 2,
        "identity": baseline_identity(manifest, resource),
        "values": dict(values),
    }
    data: bytes | None = None
    try:
        data = encode_public_json(envelope)
    except UnicodeEncodeError:
        pass
    if data is None:
        raise BaselineError(
            "baseline envelope is invalid", code="baseline_invalid"
        ) from None
    _validate_baseline_data(data, manifest, resource)
    return data


def _validate_baseline_data(
    data: bytes, manifest: Manifest, resource: Resource
) -> tuple[dict[str, object], str | None, int | None]:
    envelope = _parse_schema2_json(data)
    if not isinstance(envelope, dict) or set(envelope) != {
        "schema_version",
        "identity",
        "values",
    }:
        raise BaselineError("baseline envelope is invalid", code="baseline_invalid")
    if (
        type(envelope.get("schema_version")) is not int
        or envelope["schema_version"] != 2
    ):
        raise BaselineError("baseline envelope is invalid", code="baseline_invalid")
    expected_identity = baseline_identity(manifest, resource)
    if not semantic_equal(envelope["identity"], expected_identity):
        raise BaselineError(
            "baseline identity does not match the manifest", code="baseline_stale"
        )
    values = envelope["values"]
    if not isinstance(values, dict):
        raise BaselineError("baseline values are invalid", code="baseline_invalid")
    if resource.kind == "copy":
        if set(values) != {"whole_file", "mode"}:
            raise BaselineError("baseline values are invalid", code="baseline_invalid")
        whole_file = values["whole_file"]
        whole_file_mode = values["mode"]
        algorithm: object = (
            whole_file.get("algorithm") if isinstance(whole_file, dict) else None
        )
        digest: object = (
            whole_file.get("digest") if isinstance(whole_file, dict) else None
        )
        if (
            not isinstance(whole_file, dict)
            or set(whole_file) != {"algorithm", "digest"}
            or algorithm != "sha256"
            or not isinstance(digest, str)
            or not _valid_digest(digest)
            or type(whole_file_mode) is not int
            or not 0 <= whole_file_mode <= 0o777
        ):
            raise BaselineError("baseline values are invalid", code="baseline_invalid")
        return {}, digest, whole_file_mode

    allowed = {
        name for name, binding in resource.fields.items() if binding.owner != "ignore"
    }
    if set(values) - allowed or any(
        not _is_public_scalar(value) for value in values.values()
    ):
        raise BaselineError("baseline values are invalid", code="baseline_invalid")
    return dict(values), None, None


def _parse_schema2_json(data: bytes) -> object:
    if type(data) is not bytes:
        raise BaselineError("baseline envelope is invalid", code="baseline_invalid")

    parse_failed = False
    value: object = None
    try:
        text = data.decode("utf-8", errors="strict")
        if _exceeds_nesting_limit(text):
            parse_failed = True
        else:
            value = json.loads(
                text,
                object_pairs_hook=_object_pairs_without_duplicates,
                parse_constant=_reject_non_finite_number,
                parse_float=Decimal,
                parse_int=int,
                strict=True,
            )
            if _contains_surrogate(value):
                parse_failed = True
    except (UnicodeDecodeError, ValueError, DecimalException, RecursionError):
        parse_failed = True

    if parse_failed:
        raise BaselineError("baseline envelope is invalid", code="baseline_invalid")
    return value


def _root_identity(root: DirectoryBoundary) -> dict[str, object]:
    path = root.path
    return {"path": str(path)}


def _binding_identity(binding: InputBinding, root: Path) -> dict[str, object]:
    def layer_identity(layer: InputLayer) -> dict[str, object]:
        path = _relative_name(root, layer.file)
        return {
            "file": path,
            "path": list(layer.path),
            "format": layer.format,
            "optional": layer.optional,
        }

    reverse_to = binding.reverse_to
    return {
        "layers": [layer_identity(layer) for layer in binding.layers],
        "reverse_to": None if reverse_to is None else layer_identity(reverse_to),
    }


def _relative_name(root: Path, path: Path) -> str:
    candidate = path if path.is_absolute() else root / path
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        raise BaselineError(
            "input mapping is outside its root", code="baseline_invalid"
        ) from None


def _absolute(root: Path, path: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    try:
        candidate.relative_to(root)
    except ValueError:
        raise BaselineError(
            "baseline is outside its root", code="unsafe_path"
        ) from None
    return candidate


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_public_scalar(value: object) -> bool:
    return (
        value is None
        or type(value) in {str, bool, int}
        or (isinstance(value, Decimal) and value.is_finite())
    )
