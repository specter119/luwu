"""Safe public baseline parsing and atomic persistence for M3b."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import MutationError
from .filesystem import (
    FileChangedError,
    NotRegularFileError,
    create_temporary_file,
    lock_directory,
    open_parent_directory,
    read_regular_file_at,
    sync_directory,
    unlock_directory,
)
from .manifest import Resource
from .semantic import _parse_strict_json

_MISSING = object()
_BASELINE_KEYS = frozenset(
    {"schema_version", "resource", "source", "target", "owners", "values"}
)


def parse_public_object(
    data: bytes, *, code: str = "mutation_input_invalid"
) -> dict[str, Any]:
    """Parse one strict public JSON object without exposing invalid content."""

    value, error = _parse_strict_json(data)
    if error is not None or not isinstance(value, dict):
        raise MutationError("JSON input must be a strict object", code=code)
    return value


def encode_public_json(value: Any) -> bytes:
    """Encode the supported JSON model without turning Decimal into a string."""

    return (_encode_value(value) + "\n").encode("utf-8")


def read_baseline(root: Path, resource: Resource) -> bytes | None:
    """Read a declared baseline through no-follow descriptor operations."""

    if resource.baseline is None:
        return None
    try:
        parent, name = open_parent_directory(root, resource.baseline)
    except FileNotFoundError:
        return None
    except (OSError, NotImplementedError, ValueError):
        raise MutationError(
            "baseline cannot be read safely", code="baseline_invalid"
        ) from None
    try:
        try:
            data, _ = read_regular_file_at(parent, name)
        except FileNotFoundError:
            return None
        except (NotRegularFileError, FileChangedError, OSError, RuntimeError):
            raise MutationError(
                "baseline cannot be read safely", code="baseline_invalid"
            ) from None
        return data
    finally:
        os.close(parent)


def baseline_values(
    data: bytes,
    *,
    resource: Resource,
) -> dict[str, Any]:
    """Validate and return only the public values in a baseline envelope."""

    envelope = parse_public_object(data, code="baseline_invalid")
    if set(envelope) != _BASELINE_KEYS:
        raise MutationError("baseline envelope is invalid", code="baseline_invalid")
    version = envelope["schema_version"]
    if isinstance(version, bool) or version != Decimal(1):
        raise MutationError("baseline envelope is invalid", code="baseline_invalid")
    if (envelope["resource"], envelope["source"], envelope["target"]) != (
        resource.name,
        resource.source_name,
        resource.target_name,
    ):
        raise MutationError(
            "baseline identity does not match the manifest", code="baseline_stale"
        )
    owners = envelope["owners"]
    values = envelope["values"]
    if owners != dict(resource.fields) or not isinstance(values, dict):
        raise MutationError("baseline envelope is invalid", code="baseline_invalid")
    allowed = {name for name, owner in resource.fields.items() if owner != "ignore"}
    if set(values) - allowed:
        raise MutationError("baseline envelope is invalid", code="baseline_invalid")
    return dict(values)


def make_baseline(
    *,
    resource: Resource,
    current: Mapping[str, Any],
    selected_fields: tuple[str, ...],
    existing: bytes | None,
) -> bytes:
    """Create a baseline envelope by explicitly accepting selected fields."""

    if resource.baseline is None:
        raise MutationError(
            "resource has no declared baseline", code="baseline_required"
        )
    values = (
        baseline_values(existing, resource=resource) if existing is not None else {}
    )
    for name in selected_fields:
        owner = resource.fields.get(name)
        if owner is None:
            raise MutationError("field is not declared", code="field_not_declared")
        if owner == "ignore":
            raise MutationError(
                "ignored fields cannot be accepted", code="field_not_selectable"
            )
        value = current.get(name, _MISSING)
        if value is _MISSING:
            values.pop(name, None)
        else:
            values[name] = value
    envelope = {
        "schema_version": 1,
        "resource": resource.name,
        "source": resource.source_name,
        "target": resource.target_name,
        "owners": dict(resource.fields),
        "values": values,
    }
    return encode_public_json(envelope)


def write_baseline(root: Path, resource: Resource, data: bytes) -> None:
    """Atomically replace a baseline while preserving an existing mode."""

    if resource.baseline is None:
        raise MutationError(
            "resource has no declared baseline", code="baseline_required"
        )
    try:
        parent, name = open_parent_directory(root, resource.baseline)
    except (OSError, NotImplementedError, ValueError):
        raise MutationError(
            "baseline parent cannot be opened safely", code="baseline_write_failed"
        ) from None
    temporary: str | None = None
    locked = False
    try:
        lock_directory(parent)
        locked = True
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise MutationError(
                    "baseline is not a regular file", code="baseline_invalid"
                )
            mode = info.st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        descriptor, temporary = create_temporary_file(parent, prefix=f".{name}.luwu-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), mode)
                os.fsync(handle.fileno())
        except BaseException:
            if temporary is not None:
                _remove_temporary(parent, temporary)
            raise
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        sync_directory(parent)
    except MutationError:
        raise
    except (OSError, NotImplementedError) as exc:
        raise MutationError(
            "baseline write could not be confirmed", code="baseline_write_failed"
        ) from exc
    finally:
        if temporary is not None:
            _remove_temporary(parent, temporary)
        if locked:
            unlock_directory(parent)
        os.close(parent)


def _remove_temporary(parent: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _encode_value(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float)):
        return json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_encode_value(item) for item in value) + "]"
    if isinstance(value, dict):
        members = []
        for key, item in value.items():
            members.append(
                json.dumps(str(key), ensure_ascii=False) + ":" + _encode_value(item)
            )
        return "{" + ",".join(members) + "}"
    raise MutationError(
        "JSON input contains an unsupported value", code="mutation_input_invalid"
    )
