"""Explicit layered public inputs for the execution loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TypedDict

from .errors import LuwuError
from .filesystem import FileObservation, observe_file
from .formats import MISSING, get_leaf, parse_document
from .manifest import Manifest, Resource
from .semantic import is_public_scalar


class InputError(LuwuError):
    """A fixed-message error while resolving declared input layers."""


@dataclass(frozen=True, slots=True)
class InputProvenance:
    """One selected value candidate and the layer that supplied it."""

    layer_index: int
    file: Path = field(repr=False)
    path: tuple[str, ...]
    category: str | None
    value: object = field(repr=False)
    identity: tuple[int, int] | None = field(repr=False)
    digest: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    """The effective overlay value and all present, ordered contributors."""

    name: str
    value: object = field(repr=False)
    winner: InputProvenance = field(repr=False)
    layers: tuple[InputProvenance, ...] = field(repr=False)


class TemplateVariables(TypedDict):
    inputs: Mapping[str, object]
    global_inputs: Mapping[str, object]
    local_inputs: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class InputContext:
    """Resolved values and private file snapshots used for freshness checks."""

    values: Mapping[str, ResolvedInput] = field(repr=False)
    files: Mapping[Path, FileObservation] = field(repr=False)

    def template_variables(self) -> TemplateVariables:
        """Build the small, explicit Jinja input namespaces."""

        effective = {name: value.value for name, value in self.values.items()}
        global_values: dict[str, object] = {}
        local_values: dict[str, object] = {}
        for name, resolved in self.values.items():
            for candidate in resolved.layers:
                if candidate.category == "global":
                    global_values[name] = candidate.value
                elif candidate.category == "local":
                    local_values[name] = candidate.value
        return {
            "inputs": MappingProxyType(effective),
            "global_inputs": MappingProxyType(global_values),
            "local_inputs": MappingProxyType(local_values),
        }


def load_inputs(manifest: Manifest, resource: Resource) -> InputContext:
    """Load exactly the overlay bindings referenced by one resource.

    Layers are applied in declaration order; the last present scalar is the
    effective value. Optional layers may omit their file or selected leaf.
    Every observed layer, including an absent optional file, remains part of
    the private freshness snapshot.
    """

    input_root = manifest.roots.inputs.path
    bindings = manifest.inputs
    fields = resource.fields
    selected_names = tuple(
        sorted(
            {binding.input for binding in fields.values() if binding.input is not None}
            | set(resource.input_names)
        )
    )

    values: dict[str, ResolvedInput] = {}
    files: dict[Path, FileObservation] = {}
    for name in selected_names:
        binding = bindings.get(name)
        if binding is None:
            raise InputError(
                "declared input binding is unavailable", code="input_invalid"
            )
        contributors: list[InputProvenance] = []
        for layer_index, layer in enumerate(binding.layers):
            path = absolute_input_path(input_root, layer.file)
            observation = files.get(path)
            if observation is None:
                observation = observe_file(input_root, path)
                files[path] = observation
            if observation.data is None:
                if layer.optional:
                    continue
                raise InputError(
                    "required input layer is missing", code="input_missing"
                )
            parse_failed = False
            value: object = MISSING
            try:
                document = parse_document(observation.data, format=layer.format)
                value = get_leaf(document, layer.path)
            except (LuwuError, UnicodeError):
                parse_failed = True
            if parse_failed:
                raise InputError("input layer is invalid", code="input_invalid")
            if value is MISSING:
                if layer.optional:
                    continue
                raise InputError(
                    "required input value is missing", code="input_missing"
                )
            if not is_public_scalar(value):
                raise InputError(
                    "input value is outside the public scalar boundary",
                    code="input_invalid",
                )
            contributors.append(
                InputProvenance(
                    layer_index=layer_index,
                    file=path,
                    path=layer.path,
                    category=_input_category(path.relative_to(input_root)),
                    value=value,
                    identity=observation.identity,
                    digest=observation.digest,
                )
            )
        reverse_to = binding.reverse_to
        if reverse_to is not None:
            reverse_path = absolute_input_path(input_root, reverse_to.file)
            if reverse_path not in files:
                files[reverse_path] = observe_file(input_root, reverse_path)
        if not contributors:
            raise InputError("input binding has no present value", code="input_missing")
        winner = contributors[-1]
        values[name] = ResolvedInput(
            name=name,
            value=winner.value,
            winner=winner,
            layers=tuple(contributors),
        )
    return InputContext(
        values=MappingProxyType(values),
        files=MappingProxyType(files),
    )


def absolute_input_path(root: Path, path: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    outside_root = False
    try:
        candidate.relative_to(root)
    except ValueError:
        outside_root = True
    if outside_root:
        raise InputError("input layer is outside its root", code="unsafe_path")
    return candidate


def _input_category(root_relative_file: Path) -> str | None:
    """Classify only an input file's declared root-relative top-level area."""

    first_segment = root_relative_file.parts[0] if root_relative_file.parts else None
    return first_segment if first_segment in {"global", "local"} else None
