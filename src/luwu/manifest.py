"""Loading and validating the M1 manifest contract."""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ManifestError

MANIFEST_VERSION = 1
_MANIFEST_FIELDS = {"version", "resources"}
_RESOURCE_FIELDS = {"kind", "source", "target", "owner", "scope", "variables"}


@dataclass(frozen=True, slots=True)
class Resource:
    """One fully declared template or symbolic resource."""

    name: str
    kind: str
    source: Path
    target: Path
    source_name: str
    target_name: str
    owner: str
    scope: str
    variables: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class Manifest:
    """A validated manifest and the root against which it is scoped."""

    path: Path
    root: Path
    resources: tuple[Resource, ...]
    content_digest: str


def load_manifest(path: Path) -> Manifest:
    """Read and validate a manifest without touching any declared target."""

    try:
        manifest_path = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ManifestError(
            f"cannot resolve manifest {path}",
            code="manifest_unreadable",
        ) from exc
    try:
        raw = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise ManifestError(
            f"manifest does not exist: {path}",
            code="manifest_missing",
        ) from exc
    except OSError as exc:
        raise ManifestError(
            f"cannot read manifest {path}: {exc.strerror or type(exc).__name__}",
            code="manifest_unreadable",
        ) from exc

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ManifestError(
            f"manifest is not valid UTF-8: {path}",
            code="manifest_encoding",
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(
            "manifest is not valid TOML",
            code="manifest_toml",
        ) from exc

    if not isinstance(document, dict):
        raise ManifestError("manifest root must be a table", code="manifest_shape")

    unknown_fields = set(document) - _MANIFEST_FIELDS
    if unknown_fields:
        raise ManifestError(
            f"manifest has unsupported field(s): {', '.join(sorted(unknown_fields))}",
            code="manifest_unknown_field",
        )

    version = document.get("version")
    if type(version) is not int or version != MANIFEST_VERSION:
        raise ManifestError(
            f"manifest version must be {MANIFEST_VERSION}",
            code="manifest_version",
        )

    raw_resources = document.get("resources")
    if not isinstance(raw_resources, dict) or not raw_resources:
        raise ManifestError(
            "manifest resources must be a non-empty table",
            code="manifest_resources",
        )
    if len(raw_resources) != 1:
        raise ManifestError(
            "M1 supports exactly one resource",
            code="resource_count",
        )

    try:
        root = manifest_path.parent.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ManifestError(
            f"cannot resolve manifest directory {manifest_path.parent}",
            code="manifest_unreadable",
        ) from exc
    resources: list[Resource] = []

    for name, raw_resource in raw_resources.items():
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(
                "resource names must be non-empty strings",
                code="resource_name",
            )
        if not isinstance(raw_resource, dict):
            raise ManifestError(
                f"resource {name!r} must be a table",
                code="resource_shape",
            )

        unknown_resource_fields = set(raw_resource) - _RESOURCE_FIELDS
        if unknown_resource_fields:
            raise ManifestError(
                f"resource {name!r} has unsupported field(s): "
                f"{', '.join(sorted(unknown_resource_fields))}",
                code="resource_unknown_field",
            )

        source_name = _required_string(raw_resource, "source", resource_name=name)
        source_relative = _declared_relative_path(
            source_name,
            field=f"resource {name!r} source",
        )

        raw_kind = raw_resource.get("kind")
        if raw_kind is None:
            kind = "template" if source_relative.suffix == ".j2" else "symbolic"
        else:
            kind = _required_string(raw_resource, "kind", resource_name=name)
        if kind not in {"template", "symbolic"}:
            raise ManifestError(
                f"resource {name!r} kind must be 'template' or 'symbolic'",
                code="resource_kind",
            )
        if kind == "template" and source_relative.suffix != ".j2":
            raise ManifestError(
                f"resource {name!r} template source must use the .j2 suffix",
                code="resource_source_suffix",
            )

        target_name = _required_string(raw_resource, "target", resource_name=name)
        target_relative = _declared_relative_path(
            target_name,
            field=f"resource {name!r} target",
        )

        owner = _required_string(raw_resource, "owner", resource_name=name)
        if owner != "source":
            raise ManifestError(
                f"resource {name!r} owner must be 'source'",
                code="resource_owner",
            )

        scope = _required_string(raw_resource, "scope", resource_name=name)
        if scope != "whole-file":
            raise ManifestError(
                f"resource {name!r} scope must be 'whole-file'",
                code="resource_scope",
            )

        raw_variables = raw_resource.get("variables", {})
        variables = _copy_supported_value(
            raw_variables,
            field=f"resource {name!r} variables",
        )
        if not isinstance(variables, dict):
            raise ManifestError(
                f"resource {name!r} variables must be a table",
                code="resource_variables",
            )
        if kind == "symbolic" and variables:
            raise ManifestError(
                f"resource {name!r} symbolic resources cannot have variables",
                code="resource_variables",
            )

        source = root / source_relative
        try:
            resolved_source = source.resolve(strict=False)
            resolved_source.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ManifestError(
                f"resource {name!r} source must stay inside the manifest directory",
                code="resource_source_boundary",
            ) from exc

        target = root / target_relative
        if target == manifest_path:
            raise ManifestError(
                f"resource {name!r} target must not replace the manifest",
                code="target_manifest",
            )
        if target == source:
            raise ManifestError(
                f"resource {name!r} source and target must be different paths",
                code="source_target_same",
            )
        if target == resolved_source:
            raise ManifestError(
                f"resource {name!r} source and target must resolve to different paths",
                code="source_target_conflict",
            )
        resources.append(
            Resource(
                name=name,
                kind=kind,
                source=source,
                target=target,
                source_name=source_name,
                target_name=target_name,
                owner=owner,
                scope=scope,
                variables=variables,
            )
        )

    return Manifest(
        path=manifest_path,
        root=root,
        resources=tuple(resources),
        content_digest=hashlib.sha256(raw).hexdigest(),
    )


def _required_string(
    resource: Mapping[str, Any],
    field: str,
    *,
    resource_name: str,
) -> str:
    value = resource.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(
            f"resource {resource_name!r} field {field!r} must be a non-empty string",
            code="resource_field",
        )
    return value


def _declared_relative_path(value: str, *, field: str) -> Path:
    if "\x00" in value:
        raise ManifestError(f"{field} contains a NUL byte", code="path_invalid")

    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ManifestError(
            f"{field} must be a relative path without '..'",
            code="path_boundary",
        )

    parts = tuple(part for part in candidate.parts if part not in ("", "."))
    if not parts:
        raise ManifestError(f"{field} must not be empty", code="path_invalid")
    return Path(*parts)


def _copy_supported_value(value: Any, *, field: str) -> object:
    value_type = type(value)
    if value is None or value_type in (str, int, float, bool):
        return value
    if isinstance(value, list):
        return [
            _copy_supported_value(item, field=f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ManifestError(
                    f"{field} keys must be strings",
                    code="resource_variables",
                )
            copied[key] = _copy_supported_value(item, field=f"{field}.{key}")
        return copied
    raise ManifestError(
        f"{field} contains an unsupported value type",
        code="resource_variables",
    )
