"""Loading and validating versioned manifest contracts."""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .errors import ManifestError

MANIFEST_VERSION = 1
_MANIFEST_VERSION_V2 = 2
_MANIFEST_VERSION_V3 = 3
_SUPPORTED_MANIFEST_VERSIONS = frozenset(
    {MANIFEST_VERSION, _MANIFEST_VERSION_V2, _MANIFEST_VERSION_V3}
)
_MANIFEST_FIELDS = {"version", "resources"}
_RESOURCE_FIELDS = {
    "comparison",
    "kind",
    "source",
    "target",
    "owner",
    "scope",
    "variables",
    "variables_sensitivity",
    "fields",
    "baseline",
    "content_sensitivity",
}
_V3_RESOURCE_FIELDS = {
    "comparison",
    "kind",
    "source",
    "target",
    "owner",
    "scope",
    "fields",
    "baseline",
    "content_sensitivity",
}
_LOADER_PROVENANCE = object()
_PUBLIC_VARIABLES_TOKEN = object()
_SENSITIVE_VARIABLE_KEYS = {
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "secrets",
    "token",
}


class _PublicVariables(Mapping[str, object]):
    """Manifest literals explicitly admitted to the non-secret M1 boundary."""

    def __init__(self, values: Mapping[str, object], *, token: object) -> None:
        if token is not _PUBLIC_VARIABLES_TOKEN:
            raise TypeError(
                "public variables can only be issued by the manifest loader"
            )
        self._values = MappingProxyType(
            {key: _freeze_public_value(value) for key, value in values.items()}
        )

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True, slots=True)
class Resource:
    """One fully declared template, symbolic, or M2 copy resource."""

    name: str
    kind: str
    source: Path
    target: Path
    source_name: str
    target_name: str
    owner: str
    scope: str
    variables: Mapping[str, object] = field(repr=False)
    variables_sensitivity: str | None = None
    comparison: str = "exact-bytes"
    fields: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )
    baseline: Path | None = None
    baseline_name: str | None = None
    content_sensitivity: str | None = None


@dataclass(frozen=True, slots=True)
class Manifest:
    """A validated manifest and the root against which it is scoped."""

    version: int
    path: Path
    root: Path
    resources: tuple[Resource, ...]
    content_digest: str
    _provenance: object | None = field(
        default=None, init=False, repr=False, compare=False
    )


def load_manifest(path: Path) -> Manifest:
    """Read and validate a manifest without touching any declared target."""

    try:
        manifest_path = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        raise ManifestError(
            f"cannot resolve manifest {path}",
            code="manifest_unreadable",
        ) from None
    try:
        raw = manifest_path.read_bytes()
    except FileNotFoundError:
        raise ManifestError(
            f"manifest does not exist: {path}",
            code="manifest_missing",
        ) from None
    except OSError as exc:
        raise ManifestError(
            f"cannot read manifest {path}: {exc.strerror or type(exc).__name__}",
            code="manifest_unreadable",
        ) from None

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ManifestError(
            f"manifest is not valid UTF-8: {path}",
            code="manifest_encoding",
        ) from None
    except tomllib.TOMLDecodeError:
        raise ManifestError(
            "manifest is not valid TOML",
            code="manifest_toml",
        ) from None

    if not isinstance(document, dict):
        raise ManifestError("manifest root must be a table", code="manifest_shape")

    unknown_fields = set(document) - _MANIFEST_FIELDS
    if unknown_fields:
        raise ManifestError(
            f"manifest has unsupported field(s): {', '.join(sorted(unknown_fields))}",
            code="manifest_unknown_field",
        )

    version = document.get("version")
    if type(version) is not int or version not in _SUPPORTED_MANIFEST_VERSIONS:
        raise ManifestError(
            "manifest version must be 1, 2, or 3",
            code="manifest_version",
        )

    raw_resources = document.get("resources")
    if not isinstance(raw_resources, dict) or not raw_resources:
        raise ManifestError(
            "manifest resources must be a non-empty table",
            code="manifest_resources",
        )
    if version == MANIFEST_VERSION and len(raw_resources) != 1:
        raise ManifestError(
            "M1 supports exactly one resource",
            code="resource_count",
        )

    try:
        root = manifest_path.parent.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ManifestError(
            f"cannot resolve manifest directory {manifest_path.parent}",
            code="manifest_unreadable",
        ) from None
    resources: list[Resource] = []
    resolved_sources: list[Path] = []

    for name, raw_resource in sorted(raw_resources.items()):
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

        allowed_resource_fields = _RESOURCE_FIELDS
        if version == _MANIFEST_VERSION_V3:
            allowed_resource_fields = _V3_RESOURCE_FIELDS
        else:
            allowed_resource_fields = _RESOURCE_FIELDS - {
                "fields",
                "baseline",
                "content_sensitivity",
            }
        unknown_resource_fields = set(raw_resource) - allowed_resource_fields
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
        if version == _MANIFEST_VERSION_V3 and raw_kind is None:
            raise ManifestError(
                f"resource {name!r} kind must be explicitly 'template' in manifest version 3",
                code="resource_kind",
            )
        if raw_kind is None:
            kind = "template" if source_relative.suffix == ".j2" else "symbolic"
        else:
            kind = _required_string(raw_resource, "kind", resource_name=name)
        supported_kinds = {"template", "symbolic"}
        if version == _MANIFEST_VERSION_V2:
            supported_kinds.add("copy")
        if version == _MANIFEST_VERSION_V3:
            supported_kinds = {"template"}
        if kind not in supported_kinds:
            raise ManifestError(
                f"resource {name!r} kind is not supported by manifest version {version}",
                code="resource_kind",
            )
        if kind == "template" and source_relative.suffix != ".j2":
            raise ManifestError(
                f"resource {name!r} template source must use the .j2 suffix",
                code="resource_source_suffix",
            )

        raw_comparison = raw_resource.get("comparison")
        if version == MANIFEST_VERSION and raw_comparison is not None:
            raise ManifestError(
                f"resource {name!r} comparison is only supported by manifest version 2",
                code="resource_comparison_version",
            )
        comparison = (
            "exact-bytes"
            if raw_comparison is None
            else _required_string(raw_resource, "comparison", resource_name=name)
        )
        if version == _MANIFEST_VERSION_V3 and comparison != "json":
            raise ManifestError(
                f"resource {name!r} comparison must be 'json' in manifest version 3",
                code="resource_comparison_version",
            )
        if comparison not in {"exact-bytes", "json"}:
            raise ManifestError(
                f"resource {name!r} comparison must be 'exact-bytes' or 'json'",
                code="resource_comparison",
            )
        if comparison == "json" and kind != "template":
            raise ManifestError(
                f"resource {name!r} json comparison requires a template resource",
                code="resource_comparison_kind",
            )

        target_name = _required_string(raw_resource, "target", resource_name=name)
        target_relative = _declared_relative_path(
            target_name,
            field=f"resource {name!r} target",
        )

        owner = _required_string(raw_resource, "owner", resource_name=name)
        if version == _MANIFEST_VERSION_V3:
            if owner != "fields":
                raise ManifestError(
                    f"resource {name!r} owner must be 'fields' in manifest version 3",
                    code="resource_owner",
                )
        elif owner != "source":
            raise ManifestError(
                f"resource {name!r} owner must be 'source'",
                code="resource_owner",
            )

        scope = _required_string(raw_resource, "scope", resource_name=name)
        if version == _MANIFEST_VERSION_V3:
            if scope != "fields":
                raise ManifestError(
                    f"resource {name!r} scope must be 'fields' in manifest version 3",
                    code="resource_scope",
                )
        elif scope != "whole-file":
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
        if kind in {"symbolic", "copy"} and variables:
            raise ManifestError(
                f"resource {name!r} {kind} resources cannot have variables",
                code="resource_variables",
            )
        raw_sensitivity = raw_resource.get("variables_sensitivity")
        if raw_sensitivity is not None and raw_sensitivity != "public":
            raise ManifestError(
                f"resource {name!r} variables_sensitivity must be 'public'",
                code="resource_sensitivity",
            )
        if variables and raw_sensitivity != "public":
            raise ManifestError(
                f"resource {name!r} variables require explicit public sensitivity",
                code="resource_sensitivity",
            )

        raw_fields = raw_resource.get("fields")
        fields = (
            _parse_fields(raw_fields, resource_name=name)
            if version == _MANIFEST_VERSION_V3
            else MappingProxyType({})
        )
        raw_content_sensitivity = raw_resource.get("content_sensitivity")
        if version == _MANIFEST_VERSION_V3 and raw_content_sensitivity != "public":
            raise ManifestError(
                f"resource {name!r} content_sensitivity must be 'public'",
                code="resource_content_sensitivity",
            )
        content_sensitivity = (
            raw_content_sensitivity if version == _MANIFEST_VERSION_V3 else None
        )
        baseline_name: str | None = None
        baseline: Path | None = None
        if version == _MANIFEST_VERSION_V3 and "baseline" in raw_resource:
            baseline_name = _required_string(
                raw_resource, "baseline", resource_name=name
            )
            baseline_relative = _declared_relative_path(
                baseline_name,
                field=f"resource {name!r} baseline",
            )
            baseline = root / baseline_relative

        source = root / source_relative
        try:
            resolved_source = source.resolve(strict=False)
            resolved_source.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise ManifestError(
                f"resource {name!r} source must stay inside the manifest directory",
                code="resource_source_boundary",
            ) from None

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
                variables=_PublicVariables(variables, token=_PUBLIC_VARIABLES_TOKEN),
                variables_sensitivity=raw_sensitivity,
                comparison=comparison,
                fields=fields,
                baseline=baseline,
                baseline_name=baseline_name,
                content_sensitivity=content_sensitivity,
            )
        )
        resolved_sources.append(resolved_source)

    _validate_resource_relationships(
        resources,
        resolved_sources,
        manifest_path=manifest_path,
    )
    manifest = Manifest(
        version=version,
        path=manifest_path,
        root=root,
        resources=tuple(resources),
        content_digest=hashlib.sha256(raw).hexdigest(),
    )
    object.__setattr__(manifest, "_provenance", _LOADER_PROVENANCE)
    return manifest


def _validate_resource_relationships(
    resources: list[Resource],
    resolved_sources: list[Path],
    *,
    manifest_path: Path,
) -> None:
    """Reject ambiguous path relationships between declared resources."""

    for left_index, left in enumerate(resources):
        for right_index in range(left_index + 1, len(resources)):
            right = resources[right_index]
            if left.target == right.target:
                raise ManifestError(
                    f"resources {left.name!r} and {right.name!r} declare the "
                    f"same target {left.target_name!r}",
                    code="resource_target_conflict",
                )
            if left.target in {right.source, resolved_sources[right_index]}:
                raise ManifestError(
                    f"resource {left.name!r} target {left.target_name!r} "
                    f"conflicts with resource {right.name!r} source "
                    f"{right.source_name!r}",
                    code="resource_path_conflict",
                )
            if right.target in {left.source, resolved_sources[left_index]}:
                raise ManifestError(
                    f"resource {right.name!r} target {right.target_name!r} "
                    f"conflicts with resource {left.name!r} source "
                    f"{left.source_name!r}",
                    code="resource_path_conflict",
                )
            if _resources_have_ancestor_overlap(
                left,
                right,
                left_resolved_source=resolved_sources[left_index],
                right_resolved_source=resolved_sources[right_index],
            ):
                raise ManifestError(
                    f"resources {left.name!r} and {right.name!r} have "
                    "overlapping ancestor paths",
                    code="resource_path_overlap",
                )

    baselines = [(resource, resource.baseline) for resource in resources]
    for resource, baseline in baselines:
        if baseline is None:
            continue
        declared_paths = [manifest_path]
        for other, resolved_source in zip(resources, resolved_sources):
            declared_paths.extend((other.source, other.target, resolved_source))
        for path in declared_paths:
            if (
                baseline == path
                or _is_path_ancestor(baseline, path)
                or _is_path_ancestor(path, baseline)
            ):
                raise ManifestError(
                    f"resource {resource.name!r} baseline has a conflicting path",
                    code="baseline_path_conflict",
                )
        for other, other_baseline in baselines:
            if other_baseline is None or other is resource:
                continue
            if (
                baseline == other_baseline
                or _is_path_ancestor(baseline, other_baseline)
                or _is_path_ancestor(other_baseline, baseline)
            ):
                raise ManifestError(
                    "resources declare conflicting baseline paths",
                    code="baseline_path_conflict",
                )


def _resources_have_ancestor_overlap(
    left: Resource,
    right: Resource,
    *,
    left_resolved_source: Path,
    right_resolved_source: Path,
) -> bool:
    """Return whether any non-identical declared paths have an ancestor relation."""

    left_paths = (left.source, left.target, left_resolved_source)
    right_paths = (right.source, right.target, right_resolved_source)
    return any(
        _is_path_ancestor(left_path, right_path)
        or _is_path_ancestor(right_path, left_path)
        for left_path in left_paths
        for right_path in right_paths
    )


def _is_path_ancestor(parent: Path, child: Path) -> bool:
    return parent != child and parent in child.parents


def _parse_fields(raw_fields: object, *, resource_name: str) -> Mapping[str, str]:
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ManifestError(
            f"resource {resource_name!r} fields must be a non-empty table",
            code="resource_fields",
        )
    parsed: dict[str, str] = {}
    for key, value in raw_fields.items():
        if not isinstance(key, str) or not key:
            raise ManifestError(
                f"resource {resource_name!r} fields keys must be non-empty strings",
                code="resource_fields",
            )
        if not isinstance(value, str) or value not in {
            "source",
            "live",
            "merge",
            "ignore",
        }:
            raise ManifestError(
                f"resource {resource_name!r} fields values must be source, live, merge, or ignore",
                code="resource_fields",
            )
        parsed[key] = value
    return MappingProxyType(parsed)


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
            if key.casefold().replace("-", "_") in _SENSITIVE_VARIABLE_KEYS:
                raise ManifestError(
                    f"{field}.{key} is not accepted in the public M1 input boundary",
                    code="resource_secret_field",
                )
            copied[key] = _copy_supported_value(item, field=f"{field}.{key}")
        return copied
    raise ManifestError(
        f"{field} contains an unsupported value type",
        code="resource_variables",
    )


def _freeze_public_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_public_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_public_value(item) for item in value)
    return value
