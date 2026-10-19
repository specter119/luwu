"""Strict, read-only loader for the independent version 8 manifest contract."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import tomllib
import unicodedata
import weakref
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import quote

from .errors import ManifestError
from .filesystem import (
    DirectoryBoundary,
    FileChangedError,
    RootChangedError,
    UnsafeEntryError,
    capture_directory_boundary,
    list_directory,
    open_boundary,
    open_child_directory,
    open_child_file,
    read_stable_file,
    verify_directory_binding,
)
from .sensitivity import is_sensitive_name

_VERSION = 8
_FORMATS = frozenset({"json", "jsonc", "toml"})
_MAX_PATH_SEGMENTS = 256
_FIELD_OWNERS = frozenset({"source", "live", "merge", "ignore"})
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class Roots:
    """The explicit, non-overlapping filesystem authorities of a manifest."""

    source: DirectoryBoundary
    inputs: DirectoryBoundary
    target: DirectoryBoundary

    def verify(self) -> None:
        """Recheck all captured root identities before a later operation."""

        self.source.verify()
        self.inputs.verify()
        self.target.verify()


_PROVENANCE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class InputLayer:
    """One public field read from a declared input document."""

    file: Path = dataclass_field(repr=False)
    path: tuple[str, ...] = dataclass_field(repr=False)
    format: str
    optional: bool = False


@dataclass(frozen=True, slots=True)
class InputBinding:
    """An ordered set of input layers and an optional reverse destination."""

    name: str
    layers: tuple[InputLayer, ...] = dataclass_field(repr=False)
    reverse_to: InputLayer | None = dataclass_field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class FieldBinding:
    """A field alias, its owner, and optional public input binding."""

    path: tuple[str, ...] = dataclass_field(repr=False)
    owner: str
    input: str | None = None


@dataclass(frozen=True, slots=True)
class Resource:
    """One selected resource; directory declarations become copy leaves."""

    name: str
    kind: str
    source: Path = dataclass_field(repr=False)
    target: Path = dataclass_field(repr=False)
    format: str | None
    fields: Mapping[str, FieldBinding] = dataclass_field(repr=False)
    baseline: Path = dataclass_field(repr=False)
    parents: str
    parent_mode: int
    mode: int | None
    source_name: str = dataclass_field(repr=False)
    target_name: str = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class _DirectorySource:
    """A declared SOURCE directory whose expanded identity stays immutable."""

    name: str
    relative: Path
    exclude: tuple[str, ...]
    recursive: bool


@dataclass(frozen=True, slots=True, weakref_slot=True)
class Manifest:
    """A validated profile selection and its private filesystem provenance."""

    path: Path = dataclass_field(repr=False)
    profile: str
    roots: Roots = dataclass_field(repr=False)
    resources: tuple[Resource, ...]
    inputs: Mapping[str, InputBinding] = dataclass_field(repr=False)
    packages: tuple[str, ...]
    excluded: tuple[str, ...]
    content_digest: str = dataclass_field(repr=False)
    expansion: tuple[tuple[object, ...], ...] = dataclass_field(repr=False)
    _provenance: object = dataclass_field(repr=False, compare=False)
    _signature: str = dataclass_field(repr=False, compare=False)
    _manifest_identity: tuple[int, int] = dataclass_field(
        default=(0, 0), repr=False, compare=False
    )
    _directory_sources: tuple[_DirectorySource, ...] = dataclass_field(
        default=(), repr=False, compare=False
    )
    _directory_freshness: tuple[tuple[object, ...], ...] = dataclass_field(
        default=(), repr=False, compare=False
    )


_LOADED_MANIFESTS: dict[
    int, tuple[weakref.ReferenceType[Manifest], tuple[object, ...], str]
] = {}


def _manifest_storage_identity(manifest: Manifest) -> tuple[object, ...]:
    resource_storage = tuple(
        (
            id(resource),
            id(resource.fields),
            tuple(
                (alias, id(binding))
                for alias, binding in sorted(resource.fields.items())
            ),
        )
        for resource in manifest.resources
    )
    input_storage = tuple(
        (
            name,
            id(binding),
            id(binding.layers),
            tuple(id(layer) for layer in binding.layers),
            None if binding.reverse_to is None else id(binding.reverse_to),
        )
        for name, binding in sorted(manifest.inputs.items())
    )
    root_storage = (
        id(manifest.roots),
        id(manifest.roots.source),
        id(manifest.roots.inputs),
        id(manifest.roots.target),
    )
    return (
        id(manifest.resources),
        resource_storage,
        id(manifest.inputs),
        input_storage,
        root_storage,
        id(manifest.packages),
        id(manifest.excluded),
        id(manifest.expansion),
        tuple(id(entry) for entry in manifest.expansion),
        id(manifest._manifest_identity),
        manifest._manifest_identity,
        id(manifest._directory_sources),
        tuple(
            (
                id(source),
                id(source.exclude),
                tuple(id(pattern) for pattern in source.exclude),
            )
            for source in manifest._directory_sources
        ),
        id(manifest._directory_freshness),
        tuple(id(entry) for entry in manifest._directory_freshness),
    )


def _register_manifest(manifest: Manifest) -> None:
    key = id(manifest)

    def discard(reference: weakref.ReferenceType[Manifest]) -> None:
        registered = _LOADED_MANIFESTS.get(key)
        if registered is not None and registered[0] is reference:
            del _LOADED_MANIFESTS[key]

    reference = weakref.ref(manifest, discard)
    _LOADED_MANIFESTS[key] = (
        reference,
        _manifest_storage_identity(manifest),
        manifest._signature,
    )


@dataclass(frozen=True, slots=True)
class _ResourceDeclaration:
    name: str
    kind: str
    source_relative: Path
    target_relative: Path
    baseline_relative: Path
    parents: str
    parent_mode: int
    mode: int | None
    format: str | None = None
    fields: Mapping[str, FieldBinding] = dataclass_field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )
    exclude: tuple[str, ...] = ()
    recursive: bool = True


@dataclass(frozen=True, slots=True)
class _Package:
    dependencies: tuple[str, ...]
    resources: tuple[str, ...]


def load_manifest(path: Path, *, profile: str) -> Manifest:
    """Load and validate one version 8 manifest profile without reading payloads."""

    manifest_path = _absolute_path(path)
    raw, manifest_identity = _read_manifest_snapshot(manifest_path)
    document = _parse_manifest(raw)
    if type(document.get("version")) is not int or document["version"] != _VERSION:
        raise ManifestError("manifest version must be 8", code="manifest_version")
    _require_exact_keys(
        document,
        required={"version", "roots", "profiles", "packages", "inputs", "resources"},
        allowed={"version", "roots", "profiles", "packages", "inputs", "resources"},
        code="manifest_schema",
    )

    roots = _parse_roots(document["roots"], manifest_root=manifest_path.parent)
    profiles = _parse_profiles(document["profiles"])
    packages = _parse_packages(document["packages"])
    declarations = _parse_resources(document["resources"])
    inputs = _parse_inputs(document["inputs"], roots=roots)
    _validate_package_references(packages, declarations, inputs)

    if not isinstance(profile, str) or not _valid_name(profile):
        raise ManifestError("profile name is invalid", code="profile")
    try:
        profile_packages, profile_excludes = profiles[profile]
    except KeyError:
        raise ManifestError(
            "selected profile is not declared", code="profile"
        ) from None

    selected_packages, excluded_packages = _select_packages(
        profile_packages, profile_excludes, packages
    )
    ordered_packages = _stable_topological_order(selected_packages, packages)
    selected_names = _selected_resources(ordered_packages, packages)
    selected_declarations = tuple(declarations[name] for name in selected_names)
    used_input_names = {
        binding.input
        for declaration in selected_declarations
        for binding in declaration.fields.values()
        if binding.input is not None
    }
    selected_inputs = MappingProxyType(
        {name: inputs[name] for name in sorted(used_input_names)}
    )

    directory_freshness: list[tuple[object, ...]] = []
    directory_sources: list[_DirectorySource] = []
    expansion: list[tuple[object, ...]] = []
    expanded_resources: list[Resource] = []
    directory_scopes: list[tuple[str, Path, Path]] = []

    for declaration in selected_declarations:
        if declaration.kind == "directory":
            source_dir = roots.source.path / declaration.source_relative
            target_dir = roots.target.path / declaration.target_relative
            directory_scopes.append((declaration.name, source_dir, target_dir))
            directory_sources.append(
                _DirectorySource(
                    name=declaration.name,
                    relative=declaration.source_relative,
                    exclude=declaration.exclude,
                    recursive=declaration.recursive,
                )
            )
            leaves, snapshots, directory_entries = _expand_directory(
                roots.source,
                declaration.source_relative,
                declaration.exclude,
                recursive=declaration.recursive,
                resource_name=declaration.name,
            )
            directory_freshness.extend(snapshots)
            expansion.extend(directory_entries)
            for relative_leaf in leaves:
                source_relative = declaration.source_relative / relative_leaf
                target_relative = declaration.target_relative / relative_leaf
                baseline_relative = _directory_baseline(
                    declaration.baseline_relative, relative_leaf
                )
                expanded_resources.append(
                    Resource(
                        name=f"{declaration.name}:{relative_leaf.as_posix()}",
                        kind="copy",
                        source=roots.source.path / source_relative,
                        target=roots.target.path / target_relative,
                        format=None,
                        fields=MappingProxyType({}),
                        baseline=roots.inputs.path / baseline_relative,
                        parents=declaration.parents,
                        parent_mode=declaration.parent_mode,
                        mode=declaration.mode,
                        source_name=source_relative.as_posix(),
                        target_name=target_relative.as_posix(),
                    )
                )
        else:
            expanded_resources.append(
                Resource(
                    name=declaration.name,
                    kind=declaration.kind,
                    source=roots.source.path / declaration.source_relative,
                    target=roots.target.path / declaration.target_relative,
                    format=declaration.format,
                    fields=declaration.fields,
                    baseline=roots.inputs.path / declaration.baseline_relative,
                    parents=declaration.parents,
                    parent_mode=declaration.parent_mode,
                    mode=declaration.mode,
                    source_name=declaration.source_relative.as_posix(),
                    target_name=declaration.target_relative.as_posix(),
                )
            )

    _validate_declared_relationships(
        expanded_resources,
        selected_declarations,
        selected_inputs,
        directory_scopes,
        roots,
        manifest_path,
    )

    # Only source files are author-controlled, so only they may abort loading.
    # Live targets, baselines, and input layers are observed per resource by
    # the engine, where an unsafe entry blocks that resource instead of the
    # whole profile.
    direct_declarations = {
        declaration.name: declaration
        for declaration in selected_declarations
        if declaration.kind != "directory"
    }
    for resource in expanded_resources:
        declaration = direct_declarations.get(resource.name)
        if declaration is not None:
            _inspect_source_file(roots.source, declaration.source_relative)

    try:
        roots.verify()
    except (OSError, ValueError, NotImplementedError):
        raise ManifestError(
            "a declared root changed during manifest loading", code="root_stale"
        ) from None
    directory_freshness_tuple = tuple(sorted(set(directory_freshness), key=repr))
    expansion_tuple = tuple(sorted(expansion, key=repr))
    resource_tuple = tuple(expanded_resources)
    content_digest = hashlib.sha256(raw).hexdigest()
    manifest_without_signature = Manifest(
        path=manifest_path,
        profile=profile,
        roots=roots,
        resources=resource_tuple,
        inputs=selected_inputs,
        packages=ordered_packages,
        excluded=excluded_packages,
        content_digest=content_digest,
        expansion=expansion_tuple,
        _provenance=_PROVENANCE_TOKEN,
        _signature="",
        _manifest_identity=manifest_identity,
        _directory_sources=tuple(directory_sources),
        _directory_freshness=directory_freshness_tuple,
    )
    signature = _manifest_signature(manifest_without_signature)
    result = Manifest(
        path=manifest_without_signature.path,
        profile=manifest_without_signature.profile,
        roots=manifest_without_signature.roots,
        resources=manifest_without_signature.resources,
        inputs=manifest_without_signature.inputs,
        packages=manifest_without_signature.packages,
        excluded=manifest_without_signature.excluded,
        content_digest=manifest_without_signature.content_digest,
        expansion=manifest_without_signature.expansion,
        _provenance=_PROVENANCE_TOKEN,
        _signature=signature,
        _manifest_identity=manifest_without_signature._manifest_identity,
        _directory_sources=manifest_without_signature._directory_sources,
        _directory_freshness=manifest_without_signature._directory_freshness,
    )
    _register_manifest(result)
    return result


def verify_manifest(manifest: Manifest) -> None:
    """Verify declaration bytes, roots, and declared SOURCE directory expansions."""

    if (
        not isinstance(manifest, Manifest)
        or manifest._provenance is not _PROVENANCE_TOKEN
    ):
        raise ManifestError("manifest provenance is not trusted", code="provenance")
    try:
        registered = _LOADED_MANIFESTS.get(id(manifest))
        if registered is None or registered[0]() is not manifest:
            raise ManifestError("manifest provenance is not trusted", code="provenance")
        registered_signature = registered[2]
        if _manifest_storage_identity(manifest) != registered[1]:
            raise ManifestError("manifest storage was changed", code="provenance")
        signature = _manifest_signature(manifest)
    except ManifestError:
        raise
    except (AttributeError, TypeError, ValueError, RecursionError):
        raise ManifestError(
            "manifest provenance is not valid", code="provenance"
        ) from None
    if (
        signature != manifest._signature
        or signature != registered_signature
        or manifest._signature != registered_signature
    ):
        raise ManifestError("manifest provenance was modified", code="provenance")
    try:
        raw, identity = _read_manifest_snapshot(manifest.path)
        if (
            identity != manifest._manifest_identity
            or hashlib.sha256(raw).hexdigest() != manifest.content_digest
        ):
            raise ManifestError(
                "manifest selection or expansion is stale", code="stale"
            )
        manifest.roots.verify()
        directory_freshness: list[tuple[object, ...]] = []
        expansion: list[tuple[object, ...]] = []
        for source in manifest._directory_sources:
            _, snapshots, entries = _expand_directory(
                manifest.roots.source,
                source.relative,
                source.exclude,
                recursive=source.recursive,
                resource_name=source.name,
            )
            directory_freshness.extend(snapshots)
            expansion.extend(entries)
    except ManifestError:
        raise ManifestError(
            "manifest selection or expansion is stale", code="stale"
        ) from None
    except (OSError, ValueError, NotImplementedError):
        raise ManifestError(
            "manifest selection or expansion is stale", code="stale"
        ) from None
    directory_freshness_tuple = tuple(sorted(set(directory_freshness), key=repr))
    expansion_tuple = tuple(sorted(expansion, key=repr))
    if (
        directory_freshness_tuple != manifest._directory_freshness
        or expansion_tuple != manifest.expansion
    ):
        raise ManifestError("manifest selection or expansion is stale", code="stale")


def _absolute_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise ManifestError("manifest path is invalid", code="manifest_path")
    try:
        absolute = Path(os.path.abspath(path.expanduser()))
        str(absolute).encode("utf-8")
        capture_directory_boundary(absolute.parent)
    except (OSError, RuntimeError, ValueError, NotImplementedError):
        raise ManifestError(
            "manifest path is not safely accessible", code="manifest_path"
        ) from None
    return absolute


def _read_manifest_snapshot(path: Path) -> tuple[bytes, tuple[int, int]]:
    """Read a manifest and retain the no-follow identity of its opened file."""

    parent_descriptor = -1
    try:
        parent = capture_directory_boundary(path.parent)
        parent_descriptor = _open_boundary(parent)
        try:
            raw, info = read_stable_file(parent_descriptor, path.name)
        except UnsafeEntryError:
            raise ManifestError(
                "manifest must be a regular single-linked file", code="manifest_path"
            ) from None
        except FileChangedError:
            raise ManifestError(
                "manifest changed while being read", code="manifest_stale"
            ) from None
        _verify_directory_binding(parent, Path(), parent_descriptor)
        return raw, (info.st_dev, info.st_ino)
    except ManifestError:
        raise
    except (OSError, ValueError, TypeError):
        raise ManifestError(
            "manifest cannot be read safely", code="manifest_unreadable"
        ) from None
    finally:
        if parent_descriptor >= 0:
            _close_descriptor(parent_descriptor)


def _parse_manifest(raw: bytes) -> dict[str, object]:
    text = ""
    decoding_failed = False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoding_failed = True
    if decoding_failed:
        raise ManifestError("manifest is not valid UTF-8", code="manifest_encoding")

    parsed: object = None
    parsing_failed = False
    try:
        parsed = tomllib.loads(text)
    except (ValueError, RecursionError):
        parsing_failed = True
    if parsing_failed:
        raise ManifestError("manifest is not valid TOML", code="manifest_toml")
    if not isinstance(parsed, dict):
        raise ManifestError("manifest root must be a table", code="manifest_schema")
    return parsed


def _parse_roots(raw: object, *, manifest_root: Path) -> Roots:
    table = _require_table(raw, "roots", code="roots")
    _require_exact_keys(
        table,
        required={"source", "inputs", "target"},
        allowed={"source", "inputs", "target"},
        code="roots",
    )
    paths: dict[str, Path] = {}
    for name in ("source", "inputs"):
        paths[name] = manifest_root / _relative_path(table[name], field=f"roots.{name}")
    target_value = table["target"]
    if not isinstance(target_value, str) or not target_value:
        raise ManifestError("roots.target must be a non-empty path", code="roots")
    if Path(target_value).is_absolute():
        if (
            "\\" in target_value
            or "\x00" in target_value
            or ".." in Path(target_value).parts
        ):
            raise ManifestError("roots.target path is unsafe", code="roots")
        paths["target"] = Path(target_value)
    else:
        paths["target"] = manifest_root / _relative_path(
            target_value, field="roots.target"
        )

    source_key = _path_key(paths["source"])
    inputs_key = _path_key(paths["inputs"])
    target_key = _path_key(paths["target"])
    if _keys_overlap(source_key, inputs_key):
        raise ManifestError(
            "source and inputs roots must not overlap", code="root_overlap"
        )
    if _keys_overlap(target_key, source_key) and len(target_key) >= len(source_key):
        raise ManifestError(
            "target root must contain source and inputs roots", code="root_overlap"
        )
    if _keys_overlap(target_key, inputs_key) and len(target_key) >= len(inputs_key):
        raise ManifestError(
            "target root must contain source and inputs roots", code="root_overlap"
        )
    try:
        boundaries = {
            name: capture_directory_boundary(path) for name, path in paths.items()
        }
    except (OSError, ValueError, NotImplementedError):
        raise ManifestError(
            "all roots must be existing safe directories", code="root_boundary"
        ) from None
    identities = {(item.device, item.inode) for item in boundaries.values()}
    if len(identities) != 3:
        raise ManifestError("root paths must not alias", code="root_overlap")
    return Roots(
        source=boundaries["source"],
        inputs=boundaries["inputs"],
        target=boundaries["target"],
    )


def _parse_profiles(
    raw: object,
) -> Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    table = _require_named_table(raw, "profiles", code="profiles")
    profiles: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for name, value in table.items():
        profile = _require_table(value, "profile", code="profiles")
        _require_exact_keys(
            profile,
            required={"packages", "exclude"},
            allowed={"packages", "exclude"},
            code="profiles",
        )
        profiles[name] = (
            _name_list(profile["packages"], code="profiles"),
            _name_list(profile["exclude"], code="profiles"),
        )
    return MappingProxyType(profiles)


def _parse_packages(raw: object) -> Mapping[str, _Package]:
    table = _require_named_table(raw, "packages", code="packages")
    packages: dict[str, _Package] = {}
    for name, value in table.items():
        package = _require_table(value, "package", code="packages")
        _require_exact_keys(
            package,
            required={"dependencies", "resources"},
            allowed={"dependencies", "resources"},
            code="packages",
        )
        packages[name] = _Package(
            dependencies=_name_list(package["dependencies"], code="packages"),
            resources=_name_list(package["resources"], code="packages"),
        )
    for package in packages.values():
        missing = set(package.dependencies) - set(packages)
        if missing:
            raise ManifestError(
                "package dependency is not declared", code="package_dependency"
            )
    _stable_topological_order(tuple(packages), packages)
    return MappingProxyType(packages)


def _parse_resources(raw: object) -> Mapping[str, _ResourceDeclaration]:
    table = _require_named_table(raw, "resources", code="resources")
    declarations: dict[str, _ResourceDeclaration] = {}
    for name, value in table.items():
        resource = _require_table(value, "resource", code="resources")
        kind = resource.get("kind")
        if not isinstance(kind, str) or kind not in {"template", "copy", "directory"}:
            raise ManifestError(
                "resource kind must be template, copy, or directory",
                code="resource_kind",
            )
        common_required = {
            "kind",
            "source",
            "target",
            "baseline",
            "content_sensitivity",
            "parents",
            "parent_mode",
        }
        common_allowed = common_required | {"mode"}
        if kind == "template":
            allowed = common_allowed | {"format", "scope", "fields"}
            required = common_required | {"format", "scope", "fields"}
        elif kind == "copy":
            allowed = common_allowed | {"scope", "owner"}
            required = common_required | {"scope", "owner"}
        else:
            allowed = common_allowed | {"scope", "owner", "exclude", "recursive"}
            required = common_required | {"scope", "owner"}
        _require_exact_keys(
            resource, required=required, allowed=allowed, code="resource_schema"
        )
        if resource["content_sensitivity"] != "public":
            raise ManifestError(
                "version 8 resources must declare public content",
                code="resource_sensitivity",
            )
        parents = resource["parents"]
        if not isinstance(parents, str) or parents not in {"require", "create"}:
            raise ManifestError(
                "resource parents must be require or create", code="resource_parents"
            )
        parent_mode = _parse_mode(resource["parent_mode"], code="resource_mode")
        mode = (
            _parse_mode(resource["mode"], code="resource_mode")
            if "mode" in resource
            else None
        )
        if kind == "template":
            if resource["scope"] != "fields":
                raise ManifestError(
                    "template resources must use field scope", code="resource_scope"
                )
            format_name = _format(resource["format"])
            fields = _parse_field_bindings(resource["fields"])
            exclude: tuple[str, ...] = ()
            recursive = True
        else:
            if resource["scope"] != "whole-file" or resource["owner"] != "source":
                raise ManifestError(
                    "copy and directory resources must own whole files",
                    code="resource_scope",
                )
            format_name = None
            fields = MappingProxyType({})
            exclude = (
                _glob_list(resource.get("exclude", [])) if kind == "directory" else ()
            )
            recursive_value = (
                resource.get("recursive", True) if kind == "directory" else True
            )
            if type(recursive_value) is not bool:
                raise ManifestError(
                    "directory recursive must be a boolean", code="directory_schema"
                )
            recursive = recursive_value
        declarations[name] = _ResourceDeclaration(
            name=name,
            kind=kind,
            source_relative=_relative_path(resource["source"], field="resource.source"),
            target_relative=_relative_path(resource["target"], field="resource.target"),
            baseline_relative=_relative_path(
                resource["baseline"], field="resource.baseline"
            ),
            parents=parents,
            parent_mode=parent_mode,
            mode=mode,
            format=format_name,
            fields=fields,
            exclude=exclude,
            recursive=recursive,
        )
    return MappingProxyType(declarations)


def _parse_field_bindings(raw: object) -> Mapping[str, FieldBinding]:
    table = _require_named_table(raw, "fields", code="fields")
    if not table:
        raise ManifestError("template fields must not be empty", code="fields")
    fields: dict[str, FieldBinding] = {}
    for alias, value in table.items():
        field_table = _require_table(value, "field binding", code="fields")
        _require_exact_keys(
            field_table,
            required={"path", "owner"},
            allowed={"path", "owner", "input"},
            code="fields",
        )
        owner = field_table["owner"]
        if not isinstance(owner, str) or owner not in _FIELD_OWNERS:
            raise ManifestError("field owner is unsupported", code="field_owner")
        path = _leaf_path(field_table["path"], code="field_path")
        input_name = field_table.get("input")
        if input_name is not None and (
            not isinstance(input_name, str) or not _valid_name(input_name)
        ):
            raise ManifestError(
                "field input binding name is invalid", code="field_input"
            )
        if owner in {"live", "merge"} and input_name is None:
            raise ManifestError(
                "live and merge fields require an input binding", code="field_input"
            )
        if owner == "ignore" and input_name is not None:
            raise ManifestError(
                "ignored fields cannot use input bindings", code="field_input"
            )
        fields[alias] = FieldBinding(path=path, owner=owner, input=input_name)
    field_values = tuple(fields.values())
    for index, current in enumerate(field_values):
        if any(
            _leaf_paths_overlap(current.path, other.path)
            for other in field_values[index + 1 :]
        ):
            raise ManifestError("field paths must not overlap", code="field_overlap")
    return MappingProxyType(fields)


def _parse_inputs(raw: object, *, roots: Roots) -> Mapping[str, InputBinding]:
    table = _require_named_table(raw, "inputs", code="inputs")
    inputs: dict[str, InputBinding] = {}
    for name, value in table.items():
        input_table = _require_table(value, "input binding", code="inputs")
        _require_exact_keys(
            input_table,
            required={"layers"},
            allowed={"layers", "reverse_to"},
            code="inputs",
        )
        raw_layers = input_table["layers"]
        if not isinstance(raw_layers, list) or not raw_layers:
            raise ManifestError(
                "input layers must be a non-empty array", code="input_layers"
            )
        layers = tuple(
            _parse_layer(layer, roots.inputs, optional_allowed=True)
            for layer in raw_layers
        )
        reverse_to = (
            _parse_layer(
                input_table["reverse_to"], roots.inputs, optional_allowed=False
            )
            if "reverse_to" in input_table
            else None
        )
        inputs[name] = InputBinding(name=name, layers=layers, reverse_to=reverse_to)
    return MappingProxyType(inputs)


def _parse_layer(
    raw: object, root: DirectoryBoundary, *, optional_allowed: bool
) -> InputLayer:
    table = _require_table(raw, "input layer", code="input_layer")
    allowed = {"file", "path", "format"} | ({"optional"} if optional_allowed else set())
    required = {"file", "path", "format"}
    _require_exact_keys(table, required=required, allowed=allowed, code="input_layer")
    file_relative = _relative_path(table["file"], field="input.file")
    if len(file_relative.parts) < 2 or file_relative.parts[0] not in {
        "global",
        "local",
    }:
        raise ManifestError(
            "input files must be declared below global or local",
            code="input_provenance",
        )
    leaf_path = _leaf_path(table["path"], code="input_path")
    optional = table.get("optional", False)
    if type(optional) is not bool:
        raise ManifestError("input optional must be a boolean", code="input_layer")
    if not optional_allowed and optional:
        raise ManifestError(
            "reverse destinations cannot be optional", code="input_layer"
        )
    return InputLayer(
        file=root.path / file_relative,
        path=leaf_path,
        format=_format(table["format"]),
        optional=optional,
    )


def _validate_package_references(
    packages: Mapping[str, _Package],
    resources: Mapping[str, _ResourceDeclaration],
    inputs: Mapping[str, InputBinding],
) -> None:
    for profile in packages.values():
        if set(profile.resources) - set(resources):
            raise ManifestError(
                "package resource is not declared", code="package_resource"
            )
    for resource in resources.values():
        for field_binding in resource.fields.values():
            if field_binding.input is not None and field_binding.input not in inputs:
                raise ManifestError(
                    "field input binding is not declared", code="field_input"
                )


def _select_packages(
    requested: tuple[str, ...],
    excluded: tuple[str, ...],
    packages: Mapping[str, _Package],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if set(requested) - set(packages) or set(excluded) - set(packages):
        raise ManifestError("profile package is not declared", code="profile_package")
    excluded_set = set(excluded)
    selected: set[str] = set()
    pending = sorted(set(requested) - excluded_set, reverse=True)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        missing_dependencies = set(packages[name].dependencies) & excluded_set
        if missing_dependencies:
            raise ManifestError(
                "selected package depends on an excluded package",
                code="package_excluded_dependency",
            )
        pending.extend(
            sorted(set(packages[name].dependencies) - selected, reverse=True)
        )
    return tuple(sorted(selected)), tuple(sorted(excluded_set))


def _stable_topological_order(
    names: tuple[str, ...], packages: Mapping[str, _Package]
) -> tuple[str, ...]:
    selected = set(names)
    remaining = {name: set(packages[name].dependencies) & selected for name in selected}
    result: list[str] = []
    while remaining:
        ready = sorted(
            name for name, dependencies in remaining.items() if not dependencies
        )
        if not ready:
            raise ManifestError(
                "package dependencies contain a cycle", code="package_cycle"
            )
        result.extend(ready)
        for name in ready:
            del remaining[name]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return tuple(result)


def _selected_resources(
    ordered_packages: tuple[str, ...], packages: Mapping[str, _Package]
) -> tuple[str, ...]:
    seen: set[str] = set()
    selected: list[str] = []
    for package_name in ordered_packages:
        for resource_name in sorted(packages[package_name].resources):
            if resource_name not in seen:
                selected.append(resource_name)
                seen.add(resource_name)
    return tuple(selected)


def _validate_declared_relationships(
    resources: list[Resource],
    declarations: tuple[_ResourceDeclaration, ...],
    inputs: Mapping[str, InputBinding],
    directory_scopes: list[tuple[str, Path, Path]],
    roots: Roots,
    manifest_path: Path,
) -> None:
    normalized_paths: dict[Path, tuple[str, ...]] = {}

    def path_key(path: Path) -> tuple[str, ...]:
        try:
            return normalized_paths[path]
        except KeyError:
            normalized = _path_key(path)
            normalized_paths[path] = normalized
            return normalized

    def paths_overlap(left: Path, right: Path) -> bool:
        return _keys_overlap(path_key(left), path_key(right))

    whole_paths: list[tuple[str, Path]] = [("manifest", manifest_path)]
    for resource in resources:
        whole_paths.extend(
            (
                ("source", resource.source),
                ("target", resource.target),
                ("baseline", resource.baseline),
            )
        )
    for index, (_, left) in enumerate(whole_paths):
        for _, right in whole_paths[index + 1 :]:
            if paths_overlap(left, right):
                raise ManifestError(
                    "manifest, source, target, and baseline paths must not overlap",
                    code="path_overlap",
                )
    for index, (left_name, left_source, left_target) in enumerate(directory_scopes):
        for right_name, right_source, right_target in directory_scopes[index + 1 :]:
            if any(
                paths_overlap(left_path, right_path)
                for left_path in (left_source, left_target)
                for right_path in (right_source, right_target)
            ):
                raise ManifestError(
                    "directory declarations must not overlap",
                    code="path_overlap",
                )
    for name, source_dir, target_dir in directory_scopes:
        own_sources = {
            resource.source
            for resource in resources
            if resource.name.startswith(f"{name}:")
        }
        own_targets = {
            resource.target
            for resource in resources
            if resource.name.startswith(f"{name}:")
        }
        for kind, path in whole_paths:
            is_own_leaf = (kind == "source" and path in own_sources) or (
                kind == "target" and path in own_targets
            )
            if is_own_leaf:
                continue
            if paths_overlap(path, source_dir) or paths_overlap(path, target_dir):
                raise ManifestError(
                    "directory resource paths must not overlap other resources",
                    code="path_overlap",
                )

    input_locations: list[tuple[tuple[str, ...], Path, tuple[str, ...], str, str]] = []
    reverse_locations: list[
        tuple[tuple[str, ...], Path, tuple[str, ...], str, str]
    ] = []
    for name, binding in inputs.items():
        for layer in binding.layers:
            file_key = path_key(layer.file)
            input_locations.append(
                (file_key, layer.file, layer.path, name, layer.format)
            )
        if binding.reverse_to is not None:
            destination = binding.reverse_to
            file_key = path_key(destination.file)
            reverse_locations.append(
                (file_key, destination.file, destination.path, name, destination.format)
            )
    for index, (
        left_key,
        left_file,
        left_path,
        left_name,
        left_format,
    ) in enumerate(input_locations):
        for (
            right_key,
            right_file,
            right_path,
            right_name,
            right_format,
        ) in input_locations[index + 1 :]:
            if not _keys_overlap(left_key, right_key):
                continue
            if left_key != right_key or left_file != right_file:
                raise ManifestError(
                    "input file paths must not alias", code="path_overlap"
                )
            if left_format != right_format:
                raise ManifestError(
                    "shared input documents must use one format",
                    code="input_format",
                )
            if _leaf_paths_overlap(left_path, right_path):
                code = (
                    "input_path_overlap"
                    if left_name != right_name
                    else "input_layer_overlap"
                )
                raise ManifestError(
                    "shared input files must bind distinct fields", code=code
                )

    for index, (
        left_key,
        left_file,
        left_path,
        left_name,
        left_format,
    ) in enumerate(reverse_locations):
        for (
            right_key,
            right_file,
            right_path,
            right_name,
            right_format,
        ) in reverse_locations[index + 1 :]:
            if not _keys_overlap(left_key, right_key):
                continue
            if left_key != right_key or left_file != right_file:
                raise ManifestError(
                    "reverse destinations must not alias", code="write_alias"
                )
            if left_format != right_format:
                raise ManifestError(
                    "shared input documents must use one format",
                    code="input_format",
                )
            if left_name != right_name and _leaf_paths_overlap(left_path, right_path):
                raise ManifestError(
                    "reverse destinations must not alias", code="write_alias"
                )

    for file_key, destination_file_path, path, name, format_name in reverse_locations:
        destination_file = inputs[name].reverse_to
        assert destination_file is not None
        for (
            input_file,
            input_path,
            input_leaf,
            input_name,
            input_format,
        ) in input_locations:
            if not _keys_overlap(input_file, file_key):
                continue
            if input_file != file_key or input_path != destination_file_path:
                raise ManifestError(
                    "reverse destination aliases an input file", code="write_alias"
                )
            if input_format != format_name:
                raise ManifestError(
                    "shared input documents must use one format",
                    code="input_format",
                )
            if input_name != name and _leaf_paths_overlap(path, input_leaf):
                raise ManifestError(
                    "reverse destination aliases another input field",
                    code="write_alias",
                )

    protected_paths = [path for _, path in whole_paths]
    for binding in inputs.values():
        for layer in binding.layers:
            if any(paths_overlap(layer.file, path) for path in protected_paths):
                raise ManifestError(
                    "input paths must not alias manifest, source, target, or baseline paths",
                    code="path_overlap",
                )
        if binding.reverse_to is not None:
            destination = binding.reverse_to.file
            for path in protected_paths:
                if paths_overlap(destination, path):
                    raise ManifestError(
                        "reverse destinations must not alias write paths",
                        code="write_alias",
                    )
    for declaration in declarations:
        source = roots.source.path / declaration.source_relative
        target = roots.target.path / declaration.target_relative
        if paths_overlap(target, roots.source.path) or paths_overlap(
            target, roots.inputs.path
        ):
            raise ManifestError(
                "target paths must remain outside source and inputs roots",
                code="root_overlap",
            )
        if declaration.kind == "directory" and paths_overlap(source, roots.inputs.path):
            raise ManifestError(
                "directory source paths escape their authority", code="root_overlap"
            )


def _inspect_source_file(
    root: DirectoryBoundary, relative: Path
) -> tuple[tuple[object, ...], ...]:
    if not relative.parts:
        raise ManifestError(
            "declared path must name an entry below its root", code="path_boundary"
        )
    snapshots: list[tuple[object, ...]] = []
    descriptor = -1
    try:
        descriptor = _open_boundary(root)
        for index, component in enumerate(relative.parts[:-1]):
            prefix = Path(*relative.parts[: index + 1])
            with _walk_errors(
                missing=("declared path parent does not exist", "path_missing"),
                unsafe=(
                    "declared path parent is not a safe directory",
                    "path_boundary",
                ),
                stale=("declared path changed during validation", "path_stale"),
            ):
                child, opened = open_child_directory(descriptor, component)
            snapshots.append(_stat_snapshot(root, prefix, opened))
            _close_descriptor(descriptor)
            descriptor = child
        with _walk_errors(
            missing=("declared file does not exist", "path_missing"),
            unsafe=(
                "declared file must be regular and single-linked",
                "path_boundary",
            ),
            stale=("declared file changed during validation", "path_stale"),
        ):
            file_descriptor, current = open_child_file(descriptor, relative.parts[-1])
        try:
            snapshots.append(_stat_snapshot(root, relative, current))
            _verify_directory_binding(root, relative.parent, descriptor)
        finally:
            _close_descriptor(file_descriptor)
        return tuple(snapshots)
    except ManifestError:
        raise
    except (OSError, ValueError, TypeError, NotImplementedError):
        raise ManifestError(
            "declared path is not safely accessible", code="path_boundary"
        ) from None
    finally:
        if descriptor >= 0:
            _close_descriptor(descriptor)


def _expand_directory(
    root: DirectoryBoundary,
    relative: Path,
    patterns: tuple[str, ...],
    *,
    recursive: bool,
    resource_name: str,
) -> tuple[list[Path], tuple[tuple[object, ...], ...], list[tuple[object, ...]]]:
    leaves: list[Path] = []
    snapshots: list[tuple[object, ...]] = []
    expansion: list[tuple[object, ...]] = []
    descriptor = -1
    try:
        descriptor = _open_boundary(root)
        for index, component in enumerate(relative.parts):
            with _walk_errors(
                unsafe=(
                    "directory source must contain only safe directories and files",
                    "directory_boundary",
                ),
                stale=("directory source changed during validation", "directory_stale"),
            ):
                child, opened = open_child_directory(descriptor, component)
            prefix = Path(*relative.parts[: index + 1])
            snapshots.append(_stat_snapshot(root, prefix, opened))
            _close_descriptor(descriptor)
            descriptor = child
        _scan_directory(
            descriptor,
            root,
            relative,
            Path(),
            patterns,
            recursive=recursive,
            resource_name=resource_name,
            leaves=leaves,
            snapshots=snapshots,
            expansion=expansion,
        )
        _verify_directory_binding(root, relative, descriptor)
        leaves.sort(key=lambda item: item.as_posix())
        return leaves, tuple(snapshots), expansion
    except ManifestError:
        raise
    except FileNotFoundError:
        raise ManifestError(
            "directory source does not exist", code="path_missing"
        ) from None
    except (OSError, ValueError, TypeError, NotImplementedError):
        raise ManifestError(
            "directory source is not safely accessible", code="directory_boundary"
        ) from None
    finally:
        if descriptor >= 0:
            _close_descriptor(descriptor)


def _scan_directory(
    descriptor: int,
    root: DirectoryBoundary,
    source_relative: Path,
    prefix: Path,
    patterns: tuple[str, ...],
    *,
    recursive: bool,
    resource_name: str,
    leaves: list[Path],
    snapshots: list[tuple[object, ...]],
    expansion: list[tuple[object, ...]],
) -> None:
    before = os.fstat(descriptor)
    try:
        names = list_directory(descriptor)
    except OSError:
        raise ManifestError(
            "directory entries cannot be read safely", code="directory_boundary"
        ) from None
    for name in names:
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise ManifestError(
                "directory contains an unsafe entry name", code="directory_entry"
            ) from None
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or _has_control_chars(name)
        ):
            raise ManifestError(
                "directory contains an unsafe entry name", code="directory_entry"
            )
        relative_entry = prefix / name
        relative_text = relative_entry.as_posix()
        if any(_glob_matches(relative_text, pattern) for pattern in patterns):
            continue
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            raise ManifestError(
                "directory changed during expansion", code="directory_stale"
            ) from None
        source_entry = source_relative / relative_entry
        if stat.S_ISDIR(info.st_mode):
            with _walk_errors(
                unsafe=(
                    "directory source must contain only safe files",
                    "directory_boundary",
                ),
                stale=("directory changed during expansion", "directory_stale"),
            ):
                child, current = open_child_directory(descriptor, name, observed=info)
            try:
                snapshots.append(_stat_snapshot(root, source_entry, current))
                if recursive:
                    _scan_directory(
                        child,
                        root,
                        source_relative,
                        relative_entry,
                        patterns,
                        recursive=recursive,
                        resource_name=resource_name,
                        leaves=leaves,
                        snapshots=snapshots,
                        expansion=expansion,
                    )
            finally:
                _close_descriptor(child)
            continue
        with _walk_errors(
            unsafe=(
                "directory source must contain only safe files",
                "directory_boundary",
            ),
            stale=("directory changed during expansion", "directory_stale"),
        ):
            file_descriptor, current = open_child_file(descriptor, name, observed=info)
        try:
            snapshot = _stat_snapshot(root, source_entry, current)
            snapshots.append(snapshot)
            leaves.append(relative_entry)
            expansion.append((resource_name, relative_text, *snapshot[2:]))
        finally:
            _close_descriptor(file_descriptor)
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ManifestError(
            "directory changed during expansion", code="directory_stale"
        )
    _verify_directory_binding(root, source_relative / prefix, descriptor)


def _directory_baseline(prefix: Path, relative_leaf: Path) -> Path:
    suffix = quote(relative_leaf.as_posix(), safe="")
    return prefix.with_name(f"{prefix.name}.leaf-{suffix}")


def _parse_mode(value: object, *, code: str) -> int:
    if isinstance(value, str) and re.fullmatch(r"0?[0-7]{3}", value):
        parsed = int(value, 8)
    elif type(value) is int and 0 <= value <= 0o777:
        parsed = value
    else:
        raise ManifestError("file modes must be valid octal permissions", code=code)
    if parsed > 0o777:
        raise ManifestError("file modes must be valid octal permissions", code=code)
    return parsed


def _format(value: object) -> str:
    if not isinstance(value, str) or value not in _FORMATS:
        raise ManifestError(
            "document format must be json, jsonc, or toml", code="format"
        )
    return value


def _leaf_path(value: object, *, code: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_PATH_SEGMENTS:
        raise ManifestError(
            "field paths must contain between 1 and 256 keys", code=code
        )
    parts: list[str] = []
    for part in value:
        if (
            not isinstance(part, str)
            or not part
            or _has_control_chars(part)
            or is_sensitive_name(part)
        ):
            raise ManifestError(
                "field paths must contain only non-empty public keys", code=code
            )
        parts.append(part)
    return tuple(parts)


def _relative_path(value: object, *, field: str) -> Path:
    del field
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ManifestError(
            "declared paths must be non-empty safe relative paths",
            code="path_boundary",
        )
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ManifestError(
            "declared paths must be non-empty safe relative paths",
            code="path_boundary",
        )
    if not pure.parts:
        raise ManifestError(
            "declared paths must name an entry below their root",
            code="path_boundary",
        )
    if any(_has_control_chars(part) for part in pure.parts):
        raise ManifestError(
            "declared paths contain an unsafe component", code="path_boundary"
        )
    return Path(*pure.parts)


def _glob_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestError(
            "directory excludes must be an array of relative globs",
            code="directory_schema",
        )
    patterns: list[str] = []
    for pattern in value:
        if (
            not isinstance(pattern, str)
            or not pattern
            or "\\" in pattern
            or "\x00" in pattern
        ):
            raise ManifestError(
                "directory excludes must be safe relative globs",
                code="directory_schema",
            )
        pure = PurePosixPath(pattern)
        pattern_parts = pattern.split("/")
        if pure.is_absolute() or any(
            part in {"", ".", ".."} or _has_control_chars(part)
            for part in pattern_parts
        ):
            raise ManifestError(
                "directory excludes must be safe relative globs",
                code="directory_schema",
            )
        patterns.append(pattern)
    if len(set(patterns)) != len(patterns):
        raise ManifestError(
            "directory exclude patterns must be unique", code="directory_schema"
        )
    return tuple(patterns)


def _glob_matches(path: str, pattern: str) -> bool:
    path_parts = path.split("/")
    pattern_parts = pattern.split("/")
    memo: dict[tuple[int, int], bool] = {}

    def match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(
                    path_parts[path_index], pattern_parts[pattern_index]
                )
                and match(path_index + 1, pattern_index + 1)
            )
        memo[key] = result
        return result

    return match(0, 0)


def _require_table(value: object, label: str, *, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ManifestError(f"{label} must be a TOML table", code=code)
    return value


def _require_named_table(value: object, label: str, *, code: str) -> dict[str, object]:
    table = _require_table(value, label, code=code)
    if any(not _valid_name(name) for name in table):
        raise ManifestError(f"{label} names must be stable identifiers", code=code)
    return table


def _require_exact_keys(
    table: Mapping[str, object],
    *,
    required: set[str],
    allowed: set[str],
    code: str,
) -> None:
    if required - set(table) or set(table) - allowed:
        raise ManifestError(
            "version 8 manifest schema has missing or unsupported fields", code=code
        )


def _name_list(value: object, *, code: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not _valid_name(item) for item in value
    ):
        raise ManifestError("package lists must contain stable identifiers", code=code)
    if len(set(value)) != len(value):
        raise ManifestError("package lists must not repeat identifiers", code=code)
    return tuple(value)


def _valid_name(value: str) -> bool:
    return bool(_NAME_RE.fullmatch(value))


def _has_control_chars(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


def _path_key(path: Path) -> tuple[str, ...]:
    absolute = Path(os.path.abspath(path))
    return tuple(
        unicodedata.normalize("NFC", part).casefold() for part in absolute.parts
    )


def _keys_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    common = min(len(left), len(right))
    return left[:common] == right[:common]


def _leaf_paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    left_key = tuple(unicodedata.normalize("NFKC", part).casefold() for part in left)
    right_key = tuple(unicodedata.normalize("NFKC", part).casefold() for part in right)
    common = min(len(left_key), len(right_key))
    return left_key[:common] == right_key[:common]


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        raise ManifestError(
            "filesystem descriptor could not be closed safely", code="filesystem"
        ) from None


def _open_boundary(root: DirectoryBoundary) -> int:
    with _walk_errors():
        return open_boundary(root)


def _verify_directory_binding(
    root: DirectoryBoundary, relative: Path, descriptor: int
) -> None:
    try:
        with _walk_errors(stale=("declared directory identity changed", "path_stale")):
            verify_directory_binding(root, relative, descriptor)
    except ManifestError:
        raise
    except (OSError, ValueError, TypeError):
        raise ManifestError(
            "declared directory identity changed", code="path_stale"
        ) from None


@contextmanager
def _walk_errors(
    *,
    missing: tuple[str, str] | None = None,
    unsafe: tuple[str, str] | None = None,
    stale: tuple[str, str] | None = None,
) -> Generator[None]:
    """Map typed filesystem failures to this call site's manifest error."""

    try:
        yield
    except RootChangedError:
        raise ManifestError(
            "declared root identity changed", code="root_stale"
        ) from None
    except FileNotFoundError:
        if missing is None:
            raise
        raise ManifestError(missing[0], code=missing[1]) from None
    except UnsafeEntryError:
        if unsafe is None:
            raise
        raise ManifestError(unsafe[0], code=unsafe[1]) from None
    except FileChangedError:
        if stale is None:
            raise
        raise ManifestError(stale[0], code=stale[1]) from None


def _stat_snapshot(
    root: DirectoryBoundary, relative: Path, info: os.stat_result
) -> tuple[object, ...]:
    return (
        str(root.path),
        relative.as_posix(),
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _manifest_signature(manifest: Manifest) -> str:
    """Sign declared selection and resolved paths, not mutable filesystem state."""

    roots = tuple(
        (
            str(boundary.path),
            boundary.device,
            boundary.inode,
        )
        for boundary in (
            manifest.roots.source,
            manifest.roots.inputs,
            manifest.roots.target,
        )
    )
    resources = tuple(
        (
            resource.name,
            resource.kind,
            str(resource.source),
            str(resource.target),
            resource.format,
            tuple(
                (alias, binding.path, binding.owner, binding.input)
                for alias, binding in sorted(resource.fields.items())
            ),
            str(resource.baseline),
            resource.parents,
            resource.parent_mode,
            resource.mode,
            resource.source_name,
            resource.target_name,
        )
        for resource in manifest.resources
    )
    inputs = tuple(
        (
            name,
            tuple(
                (str(layer.file), layer.path, layer.format, layer.optional)
                for layer in binding.layers
            ),
            None
            if binding.reverse_to is None
            else (
                str(binding.reverse_to.file),
                binding.reverse_to.path,
                binding.reverse_to.format,
                binding.reverse_to.optional,
            ),
        )
        for name, binding in sorted(manifest.inputs.items())
    )
    directory_sources = tuple(
        (source.name, source.relative.as_posix(), source.exclude, source.recursive)
        for source in manifest._directory_sources
    )
    payload = (
        str(manifest.path),
        manifest.profile,
        roots,
        resources,
        inputs,
        directory_sources,
        manifest.packages,
        manifest.excluded,
        manifest.content_digest,
    )
    return hashlib.sha256(
        repr(payload).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
