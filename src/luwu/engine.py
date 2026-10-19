"""In-memory plan, explicit consent, and verified execution loop."""

from __future__ import annotations

import hashlib
import os
import stat
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from .baseline import (
    Baseline,
    encode_public_json,
    load_baseline,
    make_copy_baseline,
    make_field_baseline,
    write_baseline,
)
from .errors import LuwuError, MutationError
from .filesystem import (
    FileObservation,
    FilesystemError,
    capture_directory_boundary,
    create_link,
    create_parent,
    missing_parents,
    observe_file,
    observe_link,
    open_child_directory,
    open_parent_directory,
    write_file,
)
from .formats import (
    MISSING,
    Document,
    LeafPath,
    _Node,
    _TomlInlineTable,
    get_leaf,
    parse_document,
    patch_document,
    semantic_equal,
    unselected_equal,
)
from .inputs import (
    InputContext,
    InputError,
    InputProvenance,
    ResolvedInput,
    _input_category,
    absolute_input_path,
    load_inputs,
)
from .manifest import (
    FieldBinding,
    Manifest,
    Resource,
    verify_manifest,
)
from .ownership import OwnershipField, OwnershipResult, classify_fields
from .plan_evidence import (
    _capture_plan_evidence,
    _matches_plan_evidence,
    _PlanEvidence,
)
from .rendering import render_source

_SCHEMA_VERSION = 8
_PATH_TYPE = type(Path())
_PLAN_PROVENANCE = object()
_SAFE_ERROR_CODES = frozenset(
    {
        "adopt_invalid",
        "baseline_invalid",
        "baseline_stale",
        "cleanup_unknown",
        "encoding",
        "field_invalid",
        "format",
        "input_invalid",
        "input_missing",
        "invalid_content",
        "invalid_mode",
        "invalid_observation",
        "observe_failed",
        "operation_invalid",
        "parent_create_failed",
        "parent_create_unknown",
        "parent_mode_conflict",
        "parent_mode_unsafe",
        "parent_required",
        "patch",
        "path",
        "plan_invalid",
        "platform_unsupported",
        "resource_blocked",
        "reverse_delete",
        "reverse_destination",
        "reverse_consumer_change",
        "review_required",
        "sensitive_input",
        "stale_inputs",
        "stale_plan",
        "unsafe_file",
        "unsafe_path",
        "unsafe_stage",
        "write_failed",
        "write_unknown",
    }
)


class EngineError(MutationError):
    """A fixed-message, metadata-only authorization error."""

    _MESSAGES: ClassVar[dict[str, str]] = {
        "plan_invalid": "plan provenance is invalid",
        "stale_plan": "plan inputs changed; build a new plan",
        "review_required": "resource requires review",
        "field_invalid": "field selection is invalid",
        "operation_invalid": "operation is not permitted for this resource",
        "parent_required": "parent directories are missing",
        "parent_mode_conflict": "shared parent directories request conflicting modes",
        "parent_mode_unsafe": "cannot create a parent directory with group or world write permissions",
        "reverse_delete": "reverse sync cannot delete an input value",
        "reverse_destination": "reverse destination cannot accept the selected value",
        "reverse_consumer_change": "reverse sync would change another declared consumer",
        "write_failed": "write was not committed",
        "write_unknown": "write outcome is uncertain",
    }

    def __init__(
        self,
        code: str,
        *,
        operation: str | None = None,
        resource: str | None = None,
        fields: tuple[str, ...] = (),
        committed: bool = False,
        outcome: str | None = None,
    ) -> None:
        message = self._MESSAGES.get(code, "operation could not be completed safely")
        super().__init__(
            message,
            code=code if code in self._MESSAGES else "operation_invalid",
            operation=operation,
            resource=resource,
            fields=fields,
            committed=committed,
            outcome=outcome,
        )


@dataclass(frozen=True, slots=True)
class ActionResult:
    """One metadata-only action outcome, including each created directory."""

    kind: str
    resource: str
    path: str
    state: str
    fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "resource": self.resource,
            "path": self.path,
            "state": self.state,
            "fields": list(self.fields),
        }


@dataclass(frozen=True, slots=True)
class Result:
    """A fixed schema-8 operation outcome with ordered per-action states."""

    operation: str
    outcome: str
    actions: tuple[ActionResult, ...] = ()
    verification_status: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "operation": self.operation,
            "command": self.operation,
            "outcome": self.outcome,
            "actions": [action.to_dict() for action in self.actions],
            "verification_status": self.verification_status,
        }
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result


@dataclass(frozen=True, slots=True)
class _ResourcePlan:
    resource: Resource = field(repr=False)
    status: str
    action: str
    reason: str
    error_code: str | None
    desired_data: bytes | None = field(repr=False)
    desired_document: Document | None = field(repr=False)
    desired_values: Mapping[str, object] = field(repr=False)
    source_observation: FileObservation | None = field(repr=False)
    target_observation: FileObservation | None = field(repr=False)
    target_document: Document | None = field(repr=False)
    live_values: Mapping[str, object] = field(repr=False)
    baseline_observation: FileObservation | None = field(repr=False)
    baseline: Baseline | None = field(repr=False)
    inputs: InputContext | None = field(repr=False)
    ownership: OwnershipResult | None = field(repr=False)
    undeclared_changed: bool
    forward_fields: tuple[str, ...]
    reverse_fields: tuple[str, ...]
    write_data: bytes | None = field(repr=False)
    target_missing_parents: tuple[Path, ...] = field(repr=False)
    baseline_missing_parents: tuple[Path, ...] = field(repr=False)
    input_missing_parents: Mapping[Path, tuple[Path, ...]] = field(repr=False)
    parent_states: Mapping[Path, tuple[int, int] | None] = field(repr=False)
    link_value: str | None = field(default=None, repr=False)

    def to_dict(self, manifest: Manifest) -> dict[str, object]:
        ownership = None
        if self.ownership is not None:
            ownership = {
                "baseline_status": self.ownership.baseline_status,
                "undeclared_changed": self.undeclared_changed,
                "fields": [item.to_dict() for item in self.ownership.fields],
            }
        target_root = manifest.roots.target.path
        target_parents = [
            {
                "path": _relative_path(target_root, path),
                "mode": _mode_text(self.resource.parent_mode),
            }
            for path in self.target_missing_parents
        ]
        baseline_root = manifest.roots.inputs.path
        baseline_parents = [
            {
                "path": _relative_path(baseline_root, path),
                "mode": _mode_text(self.resource.parent_mode),
            }
            for path in self.baseline_missing_parents
        ]
        reverse_input_parents = [
            {
                "file": _relative_path(baseline_root, destination),
                "directories": [
                    {
                        "path": _relative_path(baseline_root, path),
                        "mode": _mode_text(self.resource.parent_mode),
                    }
                    for path in parents
                ],
            }
            for destination, parents in sorted(
                self.input_missing_parents.items(),
                key=lambda entry: entry[0].as_posix(),
            )
        ]
        input_provenance = []
        if self.inputs is not None:
            for name, resolved in sorted(self.inputs.values.items()):
                input_provenance.append(
                    {
                        "binding": name,
                        "winner": {
                            "layer": resolved.winner.layer_index,
                            "file": _relative_path(baseline_root, resolved.winner.file),
                            "path": list(resolved.winner.path),
                        },
                        "present_layers": [
                            {
                                "layer": candidate.layer_index,
                                "file": _relative_path(baseline_root, candidate.file),
                                "path": list(candidate.path),
                            }
                            for candidate in resolved.layers
                        ],
                        "layers": [
                            {
                                "layer": index,
                                "file": _relative_path(baseline_root, layer.file),
                                "path": list(layer.path),
                                "format": layer.format,
                                "optional": layer.optional,
                                "status": (
                                    "present"
                                    if index
                                    in {
                                        candidate.layer_index
                                        for candidate in resolved.layers
                                    }
                                    else (
                                        "absent_file"
                                        if self.inputs.files[layer.file].data is None
                                        else "absent_value"
                                    )
                                ),
                            }
                            for index, layer in enumerate(manifest.inputs[name].layers)
                        ],
                    }
                )
        return {
            "name": self.resource.name,
            "source": self.resource.source_name,
            "target": self.resource.target_name,
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "error_code": self.error_code,
            "forward_fields": list(self.forward_fields),
            "reverse_fields": list(self.reverse_fields),
            "ownership": ownership,
            "input_provenance": input_provenance,
            "undeclared_changed": self.undeclared_changed,
            "parents": self.resource.parents,
            "parent_mode": _mode_text(self.resource.parent_mode),
            "mode": None
            if self.resource.mode is None
            else _mode_text(self.resource.mode),
            "effective_mode": _effective_mode_text(
                self.resource, self.target_observation
            ),
            "parent_actions": {
                "target": target_parents,
                "baseline": baseline_parents,
                "reverse_inputs": reverse_input_parents,
            },
        }


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class Plan:
    """An in-process, loader-backed plan that cannot be reconstructed from JSON."""

    manifest: Manifest = field(repr=False)
    _resources: tuple[_ResourcePlan, ...] = field(repr=False)
    _provenance: object = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "command": "plan",
            "profile": self.manifest.profile,
            "packages": list(self.manifest.packages),
            "excluded": list(self.manifest.excluded),
            "resources": [item.to_dict(self.manifest) for item in self._resources],
        }

    def __repr__(self) -> str:
        return (
            f"Plan(schema_version={_SCHEMA_VERSION}, resources={len(self._resources)})"
        )


@dataclass(frozen=True, slots=True)
class _PlanRecord:
    manifest: Manifest = field(repr=False)
    resources: tuple[_ResourcePlan, ...] = field(repr=False)
    evidence: _PlanEvidence = field(repr=False)


_PLAN_REGISTRY: weakref.WeakKeyDictionary[Plan, _PlanRecord] = (
    weakref.WeakKeyDictionary()
)


@dataclass(frozen=True, slots=True)
class _WriteSpec:
    kind: str
    resource: Resource = field(repr=False)
    root: Path = field(repr=False)
    path: Path = field(repr=False)
    data: bytes = field(repr=False)
    expected: FileObservation = field(repr=False)
    mode: int | None
    fields: tuple[str, ...] = ()
    link: str | None = None


@dataclass(slots=True)
class _ReverseGroup:
    format: str
    edits: dict[LeafPath, object] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)


def build_plan(manifest: Manifest) -> Plan:
    """Observe every selected resource and return a sealed in-memory plan."""

    try:
        resources = _observe_all(manifest)
        verify_manifest(manifest)
    except LuwuError:
        raise EngineError("stale_plan") from None
    except (OSError, ValueError, NotImplementedError):
        raise EngineError("stale_plan") from None
    plan = Plan(manifest, resources)
    object.__setattr__(plan, "_provenance", _PLAN_PROVENANCE)
    try:
        evidence = _capture_plan_evidence(_plan_projection(plan))
    except Exception:  # noqa: BLE001 - keep plan graph failures metadata-only
        raise EngineError("plan_invalid") from None
    _PLAN_REGISTRY[plan] = _PlanRecord(
        manifest,
        resources,
        evidence,
    )
    return plan


def apply_plan(plan: Plan, *, confirm: bool) -> Result:
    """Apply the complete confirmed forward batch after a full live recheck."""

    record = _mutation_record(plan, operation="apply")
    if record is None:
        return Result("apply", "not_attempted", error_code="stale_plan")
    specs = _forward_specs(record.resources, record.manifest)
    target_directories = {
        item.resource.target: item.target_missing_parents
        for item in record.resources
        if item.action in {"forward", "create"}
    }
    review_required = any(
        item.undeclared_changed
        or item.status in {"blocked", "conflict", "unbased", "adopt_required"}
        for item in record.resources
    )
    if review_required and (
        not confirm or any(item.undeclared_changed for item in record.resources)
    ):
        return _result_for_specs(
            "apply",
            "not_attempted",
            specs,
            error_code="review_required",
            include_directories=target_directories,
            root=record.manifest.roots.target.path,
        )
    if _shared_directory_mode_conflict(record.resources):
        return _result_for_specs(
            "apply",
            "not_attempted",
            specs,
            error_code="parent_mode_conflict",
            include_directories=target_directories,
            root=record.manifest.roots.target.path,
        )
    target_parents = tuple(
        directory for parents in target_directories.values() for directory in parents
    )
    if _unsafe_shared_parent_mode(record.resources, target_parents):
        return _result_for_specs(
            "apply",
            "not_attempted",
            specs,
            error_code="parent_mode_unsafe",
            include_directories=target_directories,
            root=record.manifest.roots.target.path,
        )
    if not confirm:
        return _result_for_specs(
            "apply",
            "confirmation_required",
            specs,
            include_directories={
                item.resource.target: item.target_missing_parents
                for item in record.resources
                if item.action in {"forward", "create"}
            },
            root=record.manifest.roots.target.path,
        )
    if review_required:
        raise EngineError("review_required", operation="apply")
    if not specs:
        try:
            _assert_fresh(record)
        except EngineError as exc:
            return Result("apply", "not_attempted", error_code=exc.code)
        return Result("apply", "no_changes", verification_status="verified")

    actions = _initial_action_results(
        specs,
        include_directories=target_directories,
        root=record.manifest.roots.target.path,
    )
    expected_targets: dict[Path, FileObservation] = {}
    expected_baselines: dict[Path, FileObservation] = {}
    expected_inputs: dict[Path, FileObservation] = {}
    expected_directories: dict[Path, tuple[tuple[int, int], int]] = {}
    executed_specs: list[_WriteSpec] = []
    committed = False
    for spec in specs:
        item = _resource_for_path(record.resources, spec.resource.target)
        resource_names = frozenset({item.resource.name})
        target_root = record.manifest.roots.target.path
        parents = item.target_missing_parents
        if parents and item.resource.parents != "create":
            return _finish_actions(
                "apply",
                actions,
                "partial" if committed else "not_attempted",
                "parent_required",
            )
        for parent in parents:
            if parent in expected_directories:
                continue
            parent_resources = _directory_parent_resource_names(
                record.resources, parent
            )
            try:
                _assert_forward_fresh(
                    record,
                    resource_names=parent_resources,
                    expected_targets=expected_targets,
                    expected_baselines=expected_baselines,
                    expected_inputs=expected_inputs,
                    expected_directories=expected_directories,
                )
                identity = create_parent(
                    target_root,
                    parent,
                    item.resource.parent_mode,
                    check_inputs=lambda parent_resources=parent_resources: (
                        _recheck_forward_fresh(
                            record,
                            resource_names=parent_resources,
                            expected_targets=expected_targets,
                            expected_baselines=expected_baselines,
                            expected_inputs=expected_inputs,
                            expected_directories=expected_directories,
                        )
                    ),
                )
                _verify_created_parent(
                    target_root, parent, identity, item.resource.parent_mode
                )
                expected_directories[parent] = (identity, item.resource.parent_mode)
                _refresh_expected_ancestry(
                    record,
                    parent,
                    identity,
                    expected_targets=expected_targets,
                    expected_baselines=expected_baselines,
                    expected_inputs=expected_inputs,
                )
                committed = True
                _mark_action(
                    actions,
                    "mkdir",
                    item.resource.name,
                    _relative_path(target_root, parent),
                    "committed",
                )
            except EngineError as exc:
                return _finish_actions(
                    "apply",
                    actions,
                    "partial" if committed else "not_attempted",
                    exc.code,
                )
            except FilesystemError as exc:
                state = _directory_failure_state(exc)
                _mark_action(
                    actions,
                    "mkdir",
                    item.resource.name,
                    _relative_path(target_root, parent),
                    state,
                )
                if state in {"committed", "uncertain"}:
                    return _finish_actions(
                        "apply", actions, "committed_state_unknown", exc.code
                    )
                return _finish_actions(
                    "apply",
                    actions,
                    "partial" if committed else "not_attempted",
                    exc.code,
                )

        try:
            fresh_resources = _assert_forward_fresh(
                record,
                resource_names=resource_names,
                expected_targets=expected_targets,
                expected_baselines=expected_baselines,
                expected_inputs=expected_inputs,
                expected_directories=expected_directories,
            )
            fresh_item = _resource_by_name(fresh_resources, item.resource.name)
            fresh_spec = _forward_specs_for_item(fresh_item, record.manifest)
            if fresh_spec is None:
                raise EngineError("stale_plan", resource=item.resource.name)
            expected = expected_targets.get(fresh_spec.path, item.target_observation)
            if expected is None:
                raise EngineError("stale_plan", resource=item.resource.name)
            if expected != fresh_spec.expected:
                raise EngineError("stale_plan", resource=item.resource.name)
        except EngineError as exc:
            return _finish_actions(
                "apply", actions, "partial" if committed else "not_attempted", exc.code
            )

        def recheck(resource_names: frozenset[str] = resource_names) -> None:
            _recheck_forward_fresh(
                record,
                resource_names=resource_names,
                expected_targets=expected_targets,
                expected_baselines=expected_baselines,
                expected_inputs=expected_inputs,
                expected_directories=expected_directories,
            )

        try:
            if fresh_spec.link is not None:
                identity = create_link(
                    fresh_spec.root,
                    fresh_spec.path,
                    fresh_spec.link,
                    expected=expected,
                    check_inputs=recheck,
                )
            else:
                identity = write_file(
                    fresh_spec.root,
                    fresh_spec.path,
                    fresh_spec.data,
                    expected=expected,
                    mode=fresh_spec.mode,
                    check_inputs=recheck,
                )
        except FilesystemError as exc:
            return _write_failure_result(
                "apply",
                actions,
                kind=fresh_spec.kind,
                resource=item.resource,
                root=fresh_spec.root,
                path=fresh_spec.path,
                fields=fresh_spec.fields,
                error=exc,
                committed_before=committed,
            )

        if fresh_spec.link is not None:
            expected_written = replace(
                expected,
                identity=identity,
                link_target=fresh_spec.link,
                missing_chain=(),
            )
        else:
            expected_written = _expected_write_observation(
                expected, fresh_spec.data, identity, fresh_spec.mode
            )
        expected_targets[fresh_spec.path] = expected_written
        committed = True
        executed_specs.append(fresh_spec)
        _mark_action(
            actions,
            fresh_spec.kind,
            item.resource.name,
            _relative_path(fresh_spec.root, fresh_spec.path),
            "committed",
            fresh_spec.fields,
        )
        try:
            if fresh_spec.link is not None:
                written = observe_link(fresh_spec.root, fresh_spec.path)
            else:
                written = observe_file(fresh_spec.root, fresh_spec.path)
        except FilesystemError as exc:
            return _finish_actions(
                "apply",
                actions,
                "committed_but_verification_failed",
                exc.code,
            )
        if not (
            _matches_own_link(expected_written, written, identity)
            if fresh_spec.link is not None
            else _matches_own_write(
                expected_written, written, identity, fresh_spec.data, fresh_spec.mode
            )
        ):
            return _finish_actions(
                "apply", actions, "committed_state_unknown", "write_unknown"
            )
        expected_targets[fresh_spec.path] = written

    try:
        fresh = _assert_fresh(
            record,
            expected_targets=expected_targets,
            expected_baselines=expected_baselines,
            expected_inputs=expected_inputs,
            expected_directories=expected_directories,
        )
        if not _verify_forward_results(record.resources, fresh, tuple(executed_specs)):
            return _finish_actions(
                "apply", actions, "committed_but_verification_failed", "stale_plan"
            )
    except EngineError as exc:
        return _finish_actions(
            "apply", actions, "committed_but_verification_failed", exc.code
        )
    return _finish_actions(
        "apply", actions, "committed", None, verification_status="verified"
    )


def accept_baseline(
    plan: Plan,
    *,
    resource: str,
    fields: tuple[str, ...],
    value_from: str,
    confirm: bool,
) -> Result:
    """Accept selected declared template fields from desired or live state."""

    record = _mutation_record(plan, operation="accept")
    if record is None:
        return Result("accept", "not_attempted", error_code="stale_plan")
    item = _resource_by_name(record.resources, resource)
    if item.resource.kind != "template" or value_from not in {"desired", "live"}:
        raise EngineError("operation_invalid", operation="accept", resource=resource)
    _validate_selection(item, fields, operation="accept")
    if (
        value_from == "live"
        and item.target_observation is not None
        and item.target_observation.data is None
    ):
        raise EngineError(
            "operation_invalid", operation="accept", resource=resource, fields=fields
        )
    if item.status == "blocked":
        raise EngineError(
            "review_required", operation="accept", resource=resource, fields=fields
        )
    if item.undeclared_changed:
        raise EngineError(
            "review_required", operation="accept", resource=resource, fields=fields
        )
    root = record.manifest.roots.inputs.path
    path = item.resource.baseline
    if path is None:
        raise EngineError("operation_invalid", operation="accept", resource=resource)
    if not confirm:
        spec = _baseline_preview_spec(record.manifest, item, fields)
        return _result_for_specs(
            "accept",
            "confirmation_required",
            (spec,),
            include_directories={path: item.baseline_missing_parents},
            root=root,
        )

    selected_values = (
        item.desired_values if value_from == "desired" else item.live_values
    )
    if any(name not in selected_values for name in fields):
        raise EngineError("field_invalid", operation="accept", resource=resource)
    try:
        data = make_field_baseline(
            record.manifest,
            item.resource,
            current=selected_values,
            selected_fields=fields,
            existing=item.baseline,
        )
    except LuwuError:
        raise EngineError(
            "field_invalid", operation="accept", resource=resource, fields=fields
        ) from None
    parent_paths = item.baseline_missing_parents
    if parent_paths and item.resource.parents != "create":
        return Result("accept", "not_attempted", error_code="parent_required")
    actions = _mutation_actions_for_paths(
        "accept", item.resource, root, parent_paths, path, "baseline_write", fields
    )
    parent_resource_names = {
        parent: _directory_parent_resource_names(record.resources, parent)
        for parent in parent_paths
    }
    if any(
        _shared_parent_mode_conflict(record.resources, names)
        for names in parent_resource_names.values()
    ):
        return _finish_actions(
            "accept", actions, "not_attempted", "parent_mode_conflict"
        )
    if _unsafe_shared_parent_mode(record.resources, parent_paths):
        return _finish_actions("accept", actions, "not_attempted", "parent_mode_unsafe")
    expected_targets: dict[Path, FileObservation] = {}
    expected_baselines: dict[Path, FileObservation] = {}
    expected_inputs: dict[Path, FileObservation] = {}
    expected_directories: dict[Path, tuple[tuple[int, int], int]] = {}
    committed = False
    resource_names = frozenset({resource})
    for parent in parent_paths:
        if parent in expected_directories:
            continue
        shared_resource_names = parent_resource_names[parent]
        try:
            _assert_fresh(
                record,
                resource_names=shared_resource_names,
                expected_directories=expected_directories,
            )
            identity = create_parent(
                root,
                parent,
                item.resource.parent_mode,
                check_inputs=lambda shared_resource_names=shared_resource_names: (
                    _recheck_fresh(
                        record,
                        resource_names=shared_resource_names,
                        expected_directories=expected_directories,
                    )
                ),
            )
            _verify_created_parent(root, parent, identity, item.resource.parent_mode)
            expected_directories[parent] = (identity, item.resource.parent_mode)
            _refresh_expected_ancestry(
                record,
                parent,
                identity,
                expected_targets=expected_targets,
                expected_baselines=expected_baselines,
                expected_inputs=expected_inputs,
            )
            committed = True
            _mark_action(
                actions, "mkdir", resource, _relative_path(root, parent), "committed"
            )
        except EngineError as exc:
            return _finish_actions(
                "accept", actions, "partial" if committed else "not_attempted", exc.code
            )
        except FilesystemError as exc:
            state = _directory_failure_state(exc)
            _mark_action(
                actions, "mkdir", resource, _relative_path(root, parent), state
            )
            outcome = (
                "committed_state_unknown"
                if state in {"committed", "uncertain"}
                else ("partial" if committed else "not_attempted")
            )
            return _finish_actions("accept", actions, outcome, exc.code)

    try:
        _assert_fresh(
            record,
            resource_names=resource_names,
            expected_directories=expected_directories,
        )
        expected = expected_baselines.get(path, item.baseline_observation)
        if expected is None:
            raise EngineError("stale_plan", resource=resource)
    except EngineError as exc:
        return _finish_actions(
            "accept", actions, "partial" if committed else "not_attempted", exc.code
        )
    try:
        identity = write_baseline(
            record.manifest,
            item.resource,
            data=data,
            expected=expected,
            check_inputs=lambda: _recheck_fresh(
                record,
                resource_names=resource_names,
                expected_directories=expected_directories,
            ),
        )
    except FilesystemError as exc:
        return _write_failure_result(
            "accept",
            actions,
            kind="baseline_write",
            resource=item.resource,
            root=root,
            path=path,
            fields=fields,
            error=exc,
            committed_before=committed,
        )
    expected_written = _expected_write_observation(expected, data, identity, 0o600)
    expected_baselines[path] = expected_written
    committed = True
    _mark_action(
        actions,
        "baseline_write",
        resource,
        _relative_path(root, path),
        "committed",
        fields,
    )
    try:
        baseline_observation = observe_file(root, path)
    except FilesystemError as exc:
        return _finish_actions(
            "accept", actions, "committed_but_verification_failed", exc.code
        )
    if not _matches_own_write(
        expected_written, baseline_observation, identity, data, 0o600
    ):
        return _finish_actions(
            "accept", actions, "committed_state_unknown", "write_unknown"
        )
    expected_baselines[path] = baseline_observation
    try:
        fresh = _assert_fresh(
            record,
            resource_names=resource_names,
            expected_targets=expected_targets,
            expected_baselines=expected_baselines,
            expected_inputs=expected_inputs,
            expected_directories=expected_directories,
        )
        fresh_item = _resource_by_name(fresh, resource)
        verified = fresh_item.baseline is not None and _mapping_values_equal(
            fresh_item.baseline.values, selected_values, fields
        )
        if not verified:
            return _finish_actions(
                "accept", actions, "committed_but_verification_failed", "stale_plan"
            )
    except (EngineError, FilesystemError) as exc:
        return _finish_actions(
            "accept",
            actions,
            "committed_but_verification_failed",
            exc.code,
        )
    return _finish_actions(
        "accept", actions, "committed", None, verification_status="verified"
    )


def adopt_resource(plan: Plan, *, resource: str, confirm: bool) -> Result:
    """Accept one copy resource's current whole-file state as its baseline."""

    record = _mutation_record(plan, operation="adopt")
    if record is None:
        return Result("adopt", "not_attempted", error_code="stale_plan")
    item = _resource_by_name(record.resources, resource)
    if item.resource.kind != "copy" or item.status == "blocked":
        raise EngineError("operation_invalid", operation="adopt", resource=resource)
    if not confirm:
        spec = _baseline_preview_spec(record.manifest, item, ())
        return _result_for_specs(
            "adopt",
            "confirmation_required",
            (spec,),
            include_directories={
                item.resource.baseline_path(): item.baseline_missing_parents
            },
            root=record.manifest.roots.inputs.path,
        )
    source_data = item.desired_data
    live_data = (
        None if item.target_observation is None else item.target_observation.data
    )
    accepted_data = live_data if live_data is not None else source_data
    if accepted_data is None:
        raise EngineError("review_required", operation="adopt", resource=resource)
    accepted_observation = (
        item.target_observation if live_data is not None else item.source_observation
    )
    accepted_mode = None if accepted_observation is None else accepted_observation.mode
    if accepted_mode is None:
        raise EngineError("review_required", operation="adopt", resource=resource)
    try:
        data = make_copy_baseline(
            record.manifest,
            item.resource,
            accepted_data=accepted_data,
            accepted_mode=accepted_mode,
        )
    except LuwuError:
        raise EngineError(
            "operation_invalid", operation="adopt", resource=resource
        ) from None
    root = record.manifest.roots.inputs.path
    path = item.resource.baseline
    if path is None:
        raise EngineError("operation_invalid", operation="adopt", resource=resource)
    parent_paths = item.baseline_missing_parents
    if parent_paths and item.resource.parents != "create":
        return Result("adopt", "not_attempted", error_code="parent_required")
    actions = _mutation_actions_for_paths(
        "adopt", item.resource, root, parent_paths, path, "baseline_write", ()
    )
    parent_resource_names = {
        parent: _directory_parent_resource_names(record.resources, parent)
        for parent in parent_paths
    }
    if any(
        _shared_parent_mode_conflict(record.resources, names)
        for names in parent_resource_names.values()
    ):
        return _finish_actions(
            "adopt", actions, "not_attempted", "parent_mode_conflict"
        )
    if _unsafe_shared_parent_mode(record.resources, parent_paths):
        return _finish_actions("adopt", actions, "not_attempted", "parent_mode_unsafe")
    expected_baselines: dict[Path, FileObservation] = {}
    expected_directories: dict[Path, tuple[tuple[int, int], int]] = {}
    committed = False
    resource_names = frozenset({resource})
    for parent in parent_paths:
        if parent in expected_directories:
            continue
        shared_resource_names = parent_resource_names[parent]
        try:
            _assert_fresh(
                record,
                resource_names=shared_resource_names,
                expected_directories=expected_directories,
            )
            identity = create_parent(
                root,
                parent,
                item.resource.parent_mode,
                check_inputs=lambda shared_resource_names=shared_resource_names: (
                    _recheck_fresh(
                        record,
                        resource_names=shared_resource_names,
                        expected_directories=expected_directories,
                    )
                ),
            )
            _verify_created_parent(root, parent, identity, item.resource.parent_mode)
            expected_directories[parent] = (identity, item.resource.parent_mode)
            _refresh_expected_ancestry(
                record,
                parent,
                identity,
                expected_baselines=expected_baselines,
            )
            committed = True
            _mark_action(
                actions, "mkdir", resource, _relative_path(root, parent), "committed"
            )
        except EngineError as exc:
            return _finish_actions(
                "adopt", actions, "partial" if committed else "not_attempted", exc.code
            )
        except FilesystemError as exc:
            state = _directory_failure_state(exc)
            _mark_action(
                actions, "mkdir", resource, _relative_path(root, parent), state
            )
            outcome = (
                "committed_state_unknown"
                if state in {"committed", "uncertain"}
                else ("partial" if committed else "not_attempted")
            )
            return _finish_actions("adopt", actions, outcome, exc.code)

    try:
        _assert_fresh(
            record,
            resource_names=resource_names,
            expected_directories=expected_directories,
        )
        expected = expected_baselines.get(path, item.baseline_observation)
        if expected is None:
            raise EngineError("stale_plan", resource=resource)
    except EngineError as exc:
        return _finish_actions(
            "adopt", actions, "partial" if committed else "not_attempted", exc.code
        )
    try:
        identity = write_baseline(
            record.manifest,
            item.resource,
            data=data,
            expected=expected,
            check_inputs=lambda: _recheck_fresh(
                record,
                resource_names=resource_names,
                expected_directories=expected_directories,
            ),
        )
    except FilesystemError as exc:
        return _write_failure_result(
            "adopt",
            actions,
            kind="baseline_write",
            resource=item.resource,
            root=root,
            path=path,
            fields=(),
            error=exc,
            committed_before=committed,
        )
    expected_written = _expected_write_observation(expected, data, identity, 0o600)
    expected_baselines[path] = expected_written
    committed = True
    _mark_action(
        actions, "baseline_write", resource, _relative_path(root, path), "committed"
    )
    try:
        observation = observe_file(root, path)
    except FilesystemError as exc:
        return _finish_actions(
            "adopt", actions, "committed_but_verification_failed", exc.code
        )
    if not _matches_own_write(expected_written, observation, identity, data, 0o600):
        return _finish_actions(
            "adopt", actions, "committed_state_unknown", "write_unknown"
        )
    expected_baselines[path] = observation
    try:
        fresh = _assert_fresh(
            record,
            resource_names=resource_names,
            expected_baselines=expected_baselines,
            expected_directories=expected_directories,
        )
        fresh_item = _resource_by_name(fresh, resource)
        verified = (
            fresh_item.baseline is not None
            and fresh_item.baseline.whole_file_digest
            == hashlib.sha256(accepted_data).hexdigest()
            and fresh_item.baseline.whole_file_mode == accepted_mode
        )
        if not verified:
            return _finish_actions(
                "adopt", actions, "committed_but_verification_failed", "stale_plan"
            )
    except (EngineError, FilesystemError) as exc:
        return _finish_actions(
            "adopt",
            actions,
            "committed_but_verification_failed",
            exc.code,
        )
    return _finish_actions(
        "adopt", actions, "committed", None, verification_status="verified"
    )


def reverse_inputs(
    plan: Plan,
    *,
    resource: str,
    fields: tuple[str, ...],
    confirm: bool,
) -> Result:
    """Patch explicit live-owned fields to grouped declared input documents."""

    record = _mutation_record(plan, operation="reverse-sync")
    if record is None:
        return Result("reverse-sync", "not_attempted", error_code="stale_plan")
    item = _resource_by_name(record.resources, resource)
    _validate_selection(item, fields, operation="reverse-sync")
    if item.status in {"blocked", "conflict", "unbased", "adopt_required"}:
        raise EngineError(
            "review_required",
            operation="reverse-sync",
            resource=resource,
            fields=fields,
        )
    if item.undeclared_changed:
        raise EngineError(
            "review_required",
            operation="reverse-sync",
            resource=resource,
            fields=fields,
        )
    if item.ownership is None or item.target_document is None or item.inputs is None:
        raise EngineError(
            "review_required",
            operation="reverse-sync",
            resource=resource,
            fields=fields,
        )
    if any(field.decision == "review" for field in item.ownership.fields):
        raise EngineError(
            "review_required",
            operation="reverse-sync",
            resource=resource,
            fields=fields,
        )
    for name in fields:
        binding = item.resource.fields[name]
        if binding.owner not in {"live", "merge"} or name not in item.reverse_fields:
            raise EngineError(
                "field_invalid",
                operation="reverse-sync",
                resource=resource,
                fields=(name,),
            )
        if binding.input is None:
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=resource,
                fields=(name,),
            )
        value = item.live_values.get(name, MISSING)
        if value is MISSING:
            raise EngineError(
                "reverse_delete",
                operation="reverse-sync",
                resource=resource,
                fields=(name,),
            )
        resolved = item.inputs.values.get(binding.input)
        input_binding = record.manifest.inputs[binding.input]
        destination = input_binding.reverse_to
        # A destination layer that is absent or below the current winner still
        # takes effect after the write, as long as no higher layer overrides it.
        destination_layer = None
        if destination is not None:
            destination_layer = next(
                (
                    index
                    for index, layer in enumerate(input_binding.layers)
                    if layer.file == destination.file and layer.path == destination.path
                ),
                None,
            )
        if (
            resolved is None
            or destination_layer is None
            or destination_layer < resolved.winner.layer_index
        ):
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=resource,
                fields=(name,),
            )

    location_updates = _reverse_location_updates(record.manifest, item, fields)
    _preflight_reverse_consumers(record, item, fields, location_updates)
    resource_names = _reverse_consumer_resource_names(record, location_updates)
    groups = _prepare_reverse_groups(record, item, fields)
    specs = tuple(
        _WriteSpec(
            "input_write",
            item.resource,
            record.manifest.roots.inputs.path,
            path,
            data,
            expected,
            None,
            tuple(names),
        )
        for path, (data, expected, names) in sorted(
            groups.items(), key=lambda part: part[0].as_posix()
        )
    )
    missing_by_path: dict[Path, tuple[Path, ...]] = {}
    for path in groups:
        missing = item.input_missing_parents.get(path, ())
        if missing and item.resource.parents != "create":
            return _result_for_specs(
                "reverse-sync", "not_attempted", specs, error_code="parent_required"
            )
        missing_by_path[path] = missing
    action_specs = _initial_action_results(
        specs,
        include_directories=missing_by_path,
        root=record.manifest.roots.inputs.path,
    )
    parent_resource_names = {
        parent: _directory_parent_resource_names(record.resources, parent)
        for parents in missing_by_path.values()
        for parent in parents
    }
    if any(
        _shared_parent_mode_conflict(record.resources, names)
        for names in parent_resource_names.values()
    ):
        return _finish_actions(
            "reverse-sync", action_specs, "not_attempted", "parent_mode_conflict"
        )
    missing_parents = tuple(
        directory for parents in missing_by_path.values() for directory in parents
    )
    if _unsafe_shared_parent_mode(record.resources, missing_parents):
        return _finish_actions(
            "reverse-sync", action_specs, "not_attempted", "parent_mode_unsafe"
        )
    if not confirm:
        return Result("reverse-sync", "confirmation_required", tuple(action_specs))
    expected_inputs: dict[Path, FileObservation] = {}
    expected_directories: dict[Path, tuple[tuple[int, int], int]] = {}
    committed = False
    for spec in specs:
        for parent in missing_by_path[spec.path]:
            if parent in expected_directories:
                continue
            shared_resource_names = parent_resource_names[parent]
            try:
                _assert_fresh(
                    record,
                    resource_names=shared_resource_names,
                    expected_inputs=expected_inputs,
                    expected_directories=expected_directories,
                )
                identity = create_parent(
                    spec.root,
                    parent,
                    item.resource.parent_mode,
                    check_inputs=lambda shared_resource_names=shared_resource_names: (
                        _recheck_fresh(
                            record,
                            resource_names=shared_resource_names,
                            expected_inputs=expected_inputs,
                            expected_directories=expected_directories,
                        )
                    ),
                )
                _verify_created_parent(
                    spec.root, parent, identity, item.resource.parent_mode
                )
                expected_directories[parent] = (identity, item.resource.parent_mode)
                _refresh_expected_ancestry(
                    record,
                    parent,
                    identity,
                    expected_inputs=expected_inputs,
                )
                committed = True
                _mark_action(
                    action_specs,
                    "mkdir",
                    resource,
                    _relative_path(spec.root, parent),
                    "committed",
                )
            except EngineError as exc:
                return _finish_actions(
                    "reverse-sync",
                    action_specs,
                    "partial" if committed else "not_attempted",
                    exc.code,
                )
            except FilesystemError as exc:
                state = _directory_failure_state(exc)
                _mark_action(
                    action_specs,
                    "mkdir",
                    resource,
                    _relative_path(spec.root, parent),
                    state,
                )
                outcome = (
                    "committed_state_unknown"
                    if state in {"committed", "uncertain"}
                    else ("partial" if committed else "not_attempted")
                )
                return _finish_actions("reverse-sync", action_specs, outcome, exc.code)
        try:
            _assert_fresh(
                record,
                resource_names=resource_names,
                expected_inputs=expected_inputs,
                expected_directories=expected_directories,
            )
            expected = expected_inputs.get(spec.path, spec.expected)
        except EngineError as exc:
            return _finish_actions(
                "reverse-sync",
                action_specs,
                "partial" if committed else "not_attempted",
                exc.code,
            )
        try:
            identity = write_file(
                spec.root,
                spec.path,
                spec.data,
                expected=expected,
                mode=spec.mode,
                check_inputs=lambda resource_names=resource_names: _recheck_fresh(
                    record,
                    resource_names=resource_names,
                    expected_inputs=expected_inputs,
                    expected_directories=expected_directories,
                ),
            )
        except FilesystemError as exc:
            return _write_failure_result(
                "reverse-sync",
                action_specs,
                kind=spec.kind,
                resource=item.resource,
                root=spec.root,
                path=spec.path,
                fields=spec.fields,
                error=exc,
                committed_before=committed,
            )
        expected_written = _expected_write_observation(
            expected, spec.data, identity, spec.mode
        )
        expected_inputs[spec.path] = expected_written
        committed = True
        _mark_action(
            action_specs,
            spec.kind,
            resource,
            _relative_path(spec.root, spec.path),
            "committed",
            spec.fields,
        )
        try:
            observation = observe_file(spec.root, spec.path)
        except FilesystemError as exc:
            return _finish_actions(
                "reverse-sync",
                action_specs,
                "committed_but_verification_failed",
                exc.code,
            )
        if not _matches_own_write(
            expected_written, observation, identity, spec.data, spec.mode
        ):
            return _finish_actions(
                "reverse-sync",
                action_specs,
                "committed_state_unknown",
                "write_unknown",
            )
        expected_inputs[spec.path] = observation

    try:
        fresh = _assert_fresh(
            record,
            expected_inputs=expected_inputs,
            expected_directories=expected_directories,
        )
        if not _verify_reverse_results(
            record, fresh, resource, fields, location_updates
        ):
            return _finish_actions(
                "reverse-sync",
                action_specs,
                "committed_but_verification_failed",
                "stale_plan",
            )
    except EngineError as exc:
        return _finish_actions(
            "reverse-sync", action_specs, "committed_but_verification_failed", exc.code
        )
    return _finish_actions(
        "reverse-sync", action_specs, "committed", None, verification_status="verified"
    )


def _observe_all(manifest: Manifest) -> tuple[_ResourcePlan, ...]:
    return tuple(
        _observe_resource(manifest, resource) for resource in manifest.resources
    )


def _observe_resource(manifest: Manifest, resource: Resource) -> _ResourcePlan:
    empty = _ResourcePlan(
        resource=resource,
        status="blocked",
        action="blocked",
        reason="resource could not be observed",
        error_code="resource_blocked",
        desired_data=None,
        desired_document=None,
        desired_values=MappingProxyType({}),
        source_observation=None,
        target_observation=None,
        target_document=None,
        live_values=MappingProxyType({}),
        baseline_observation=None,
        baseline=None,
        inputs=None,
        ownership=None,
        undeclared_changed=False,
        forward_fields=(),
        reverse_fields=(),
        write_data=None,
        target_missing_parents=(),
        baseline_missing_parents=(),
        input_missing_parents=MappingProxyType({}),
        parent_states=MappingProxyType({}),
    )
    try:
        if resource.kind == "symlink":
            return _observe_link_resource(manifest, resource)
        roots = manifest.roots
        source = observe_file(roots.source.path, resource.source)
        if source.data is None:
            raise EngineError("review_required")
        target = observe_file(roots.target.path, resource.target)
        baseline_observation, baseline = load_baseline(
            manifest, resource, allow_stale=True
        )
        baseline_replaced = baseline is None and baseline_observation.data is not None
        inputs = (
            load_inputs(manifest, resource)
            if resource.kind == "template" or resource.templated
            else None
        )
        desired_document: Document | None = None
        desired_values: Mapping[str, object] = MappingProxyType({})
        if resource.templated:
            assert inputs is not None
            try:
                template_text = source.data.decode("utf-8")
            except UnicodeDecodeError:
                raise EngineError("review_required") from None
            desired_data = render_source(
                template_text,
                dict(inputs.template_variables()),
                resource_name=resource.name,
            ).encode("utf-8")
        elif resource.kind == "copy":
            desired_data = source.data
        else:
            raise EngineError("operation_invalid")
        if resource.kind == "template":
            assert resource.format is not None
            desired_document = parse_document(desired_data, format=resource.format)
            desired_values = MappingProxyType(
                _project(desired_document, resource.fields)
            )

        target_document: Document | None = None
        live_values: Mapping[str, object] = MappingProxyType({})
        ownership: OwnershipResult | None = None
        undeclared_changed = False
        forward_fields: tuple[str, ...] = ()
        reverse_fields: tuple[str, ...] = ()
        write_data: bytes | None = None
        if target.data is None:
            complete = _baseline_complete(resource, baseline)
            if resource.kind == "copy":
                if baseline is None or baseline.whole_file_digest is None:
                    status, action, reason = (
                        "adopt_required",
                        "adopt",
                        "copy requires explicit adoption",
                    )
                else:
                    status, action, reason = (
                        "missing",
                        "create",
                        "target is absent and requires explicit apply",
                    )
                    write_data = desired_data
                    forward_fields = ("whole_file",)
            elif baseline is None or not complete:
                status, action, reason = (
                    "unbased",
                    "accept_baseline",
                    "baseline does not cover declared fields",
                )
            else:
                status, action, reason = (
                    "missing",
                    "create",
                    "target is absent and requires explicit apply",
                )
                write_data = desired_data
                forward_fields = tuple(sorted(resource.fields))
        elif resource.kind == "copy":
            baseline_digest = None if baseline is None else baseline.whole_file_digest
            source_digest = hashlib.sha256(desired_data).hexdigest()
            live_digest = target.digest
            baseline_mode = None if baseline is None else baseline.whole_file_mode
            live_mode_changed = (
                resource.mode is None
                and baseline_mode is not None
                and target.mode != baseline_mode
            )
            if baseline_digest is None:
                status, action, reason = (
                    "adopt_required",
                    "adopt",
                    "copy requires explicit adoption",
                )
            elif source_digest == live_digest:
                if resource.mode is not None and target.mode != resource.mode:
                    status, action, reason = (
                        "drifted",
                        "forward",
                        "live copy mode differs from the declared mode",
                    )
                    write_data = desired_data
                    forward_fields = ("mode",)
                elif live_mode_changed:
                    status, action, reason = (
                        "conflict",
                        "review",
                        "live copy mode changed from baseline",
                    )
                else:
                    status, action, reason = (
                        "in_sync",
                        "noop",
                        "source and live whole files match",
                    )
            elif source_digest == baseline_digest:
                status, action, reason = (
                    "conflict",
                    "review",
                    "live copy changed from baseline",
                )
            elif live_digest == baseline_digest:
                if live_mode_changed and resource.mode is None:
                    status, action, reason = (
                        "conflict",
                        "review",
                        "live copy mode changed from baseline",
                    )
                else:
                    status, action, reason = (
                        "drifted",
                        "forward",
                        "source copy changed from baseline",
                    )
                    write_data = desired_data
                    forward_fields = ("whole_file",)
            else:
                status, action, reason = (
                    "conflict",
                    "review",
                    "source and live copy changed from baseline",
                )
        else:
            assert desired_document is not None
            target_document = parse_document(target.data, format=resource.format or "")
            live_values = MappingProxyType(_project(target_document, resource.fields))
            synthetic_baseline = _ownership_baseline(resource, baseline)
            ownership = classify_fields(
                encode_public_json(dict(desired_values)),
                encode_public_json(dict(live_values)),
                fields={
                    name: binding.owner for name, binding in resource.fields.items()
                },
                baseline=synthetic_baseline,
                resource_name=resource.name,
                source_name=resource.source_name,
                target_name=resource.target_name,
            )
            if baseline is not None:
                accepted = set(baseline.values)
                classified = tuple(
                    field_item
                    if field_item.owner == "ignore" or field_item.name in accepted
                    else OwnershipField(
                        field_item.name,
                        field_item.owner,
                        "unbased",
                        "none",
                        "baseline value is absent",
                    )
                    for field_item in ownership.fields
                )
                ownership = OwnershipResult(
                    fields=classified,
                    baseline_status=(
                        "complete"
                        if _baseline_complete(resource, baseline)
                        else "partial"
                    ),
                    undeclared_changed=False,
                )
            selected_paths = tuple(binding.path for binding in resource.fields.values())
            undeclared_changed = not unselected_equal(
                desired_document, target_document, selected_paths
            )
            forward_fields = tuple(
                field_item.name
                for field_item in ownership.fields
                if field_item.decision == "forward_candidate"
            )
            reverse_fields = tuple(
                field_item.name
                for field_item in ownership.fields
                if field_item.decision == "reverse_candidate"
            )
            conflicts = any(
                field_item.status == "conflict" for field_item in ownership.fields
            )
            if conflicts:
                status, action, reason = (
                    "conflict",
                    "review",
                    "declared fields changed on both sides",
                )
                forward_fields = ()
                reverse_fields = ()
            elif forward_fields:
                status, action, reason = (
                    "drifted",
                    "forward",
                    "declared source fields changed",
                )
                updates = {
                    resource.fields[name].path: desired_values.get(name, MISSING)
                    for name in forward_fields
                }
                write_data = patch_document(target_document, updates)
            elif reverse_fields:
                status, action, reason = (
                    "reverse_candidate",
                    "reverse",
                    "declared live fields changed",
                )
            elif any(
                field_item.decision == "review" for field_item in ownership.fields
            ):
                status, action, reason = (
                    "conflict",
                    "review",
                    "a declared field is outside its owner direction",
                )
            elif all(
                field_item.status in {"unchanged", "converged", "ignored"}
                for field_item in ownership.fields
            ):
                status = (
                    "converged"
                    if any(
                        field_item.status == "converged"
                        for field_item in ownership.fields
                    )
                    else "in_sync"
                )
                action, reason = "noop", "declared fields match"
            else:
                status, action, reason = (
                    "unbased",
                    "accept_baseline",
                    "declared fields are not based",
                )

        parent_states: dict[Path, tuple[int, int] | None] = {}
        target_missing = missing_parents(roots.target.path, resource.target)
        baseline_missing = missing_parents(roots.inputs.path, resource.baseline_path())
        input_missing: dict[Path, tuple[Path, ...]] = {}
        _extend_parent_states(parent_states, roots.target.path, resource.target)
        _extend_parent_states(parent_states, roots.source.path, resource.source)
        _extend_parent_states(
            parent_states, roots.inputs.path, resource.baseline_path()
        )
        if inputs is not None:
            for path in inputs.files:
                _extend_parent_states(parent_states, roots.inputs.path, path)
            for binding_name in inputs.values:
                destination = manifest.inputs[binding_name].reverse_to
                if destination is not None:
                    input_missing[destination.file] = missing_parents(
                        roots.inputs.path,
                        destination.file,
                    )
        if target_missing and resource.parents == "require" and action == "create":
            status, action, reason = "blocked", "blocked", "target parents are missing"
        error_code = None
        if baseline_replaced and status in {"unbased", "adopt_required"}:
            reason = (
                "baseline was recorded for a different declaration; "
                "accepting it again replaces the old baseline"
            )
            error_code = "baseline_stale"
        if undeclared_changed:
            status, action, reason = (
                "review",
                "blocked",
                "undeclared content changed; inspect declared scope or correct it manually, then re-plan",
            )
            error_code = "review_required"
        return _ResourcePlan(
            resource=resource,
            status=status,
            action=action,
            reason=reason,
            error_code=error_code,
            desired_data=desired_data,
            desired_document=desired_document,
            desired_values=desired_values,
            source_observation=source,
            target_observation=target,
            target_document=target_document,
            live_values=live_values,
            baseline_observation=baseline_observation,
            baseline=baseline,
            inputs=inputs,
            ownership=ownership,
            undeclared_changed=undeclared_changed,
            forward_fields=forward_fields,
            reverse_fields=reverse_fields,
            write_data=write_data,
            target_missing_parents=target_missing,
            baseline_missing_parents=baseline_missing,
            input_missing_parents=MappingProxyType(input_missing),
            parent_states=MappingProxyType(parent_states),
        )
    except EngineError:
        return _replace_blocked(empty, code="resource_blocked")
    except LuwuError as exc:
        return _replace_blocked(empty, code=_safe_code(exc.code))
    except (OSError, ValueError, UnicodeError, NotImplementedError):
        return _replace_blocked(empty, code="resource_blocked")
    except Exception:  # noqa: BLE001 - observation errors must never expose payloads
        return _replace_blocked(empty, code="resource_blocked")


def _observe_link_resource(manifest: Manifest, resource: Resource) -> _ResourcePlan:
    roots = manifest.roots
    source = observe_file(roots.source.path, resource.source)
    if source.data is None:
        raise EngineError("review_required")
    link_value = str(resource.source)
    target = observe_link(roots.target.path, resource.target)
    target_missing = missing_parents(roots.target.path, resource.target)
    parent_states: dict[Path, tuple[int, int] | None] = {}
    _extend_parent_states(parent_states, roots.target.path, resource.target)
    _extend_parent_states(parent_states, roots.source.path, resource.source)
    forward_fields: tuple[str, ...] = ()
    if target.link_target == link_value:
        status, action, reason = "in_sync", "noop", "link points at the source"
    elif target.identity is not None:
        status, action, reason = (
            "blocked",
            "blocked",
            "target is a symlink to a different path"
            if target.link_target is not None
            else "target exists and is not a symlink",
        )
    elif target_missing and resource.parents == "require":
        status, action, reason = "blocked", "blocked", "target parents are missing"
    else:
        status, action, reason = (
            "missing",
            "create",
            "target is absent and requires explicit apply",
        )
        forward_fields = ("link",)
    return _ResourcePlan(
        resource=resource,
        status=status,
        action=action,
        reason=reason,
        error_code=None,
        desired_data=None,
        desired_document=None,
        desired_values=MappingProxyType({}),
        source_observation=source,
        target_observation=target,
        target_document=None,
        live_values=MappingProxyType({}),
        baseline_observation=None,
        baseline=None,
        inputs=None,
        ownership=None,
        undeclared_changed=False,
        forward_fields=forward_fields,
        reverse_fields=(),
        write_data=None,
        target_missing_parents=target_missing,
        baseline_missing_parents=(),
        input_missing_parents=MappingProxyType({}),
        parent_states=MappingProxyType(parent_states),
        link_value=link_value,
    )


def _replace_blocked(item: _ResourcePlan, *, code: str) -> _ResourcePlan:
    return _ResourcePlan(
        resource=item.resource,
        status="blocked",
        action="blocked",
        reason="resource could not be observed safely",
        error_code=code,
        desired_data=item.desired_data,
        desired_document=item.desired_document,
        desired_values=item.desired_values,
        source_observation=item.source_observation,
        target_observation=item.target_observation,
        target_document=item.target_document,
        live_values=item.live_values,
        baseline_observation=item.baseline_observation,
        baseline=item.baseline,
        inputs=item.inputs,
        ownership=item.ownership,
        undeclared_changed=item.undeclared_changed,
        forward_fields=(),
        reverse_fields=(),
        write_data=None,
        target_missing_parents=item.target_missing_parents,
        baseline_missing_parents=item.baseline_missing_parents,
        input_missing_parents=item.input_missing_parents,
        parent_states=item.parent_states,
        link_value=item.link_value,
    )


def _project(
    document: Document, fields: Mapping[str, FieldBinding]
) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, binding in fields.items():
        value = get_leaf(document, binding.path)
        if value is not MISSING:
            values[name] = value
    return values


def _ownership_baseline(resource: Resource, baseline: Baseline | None) -> bytes | None:
    if baseline is None:
        return None
    return encode_public_json(
        {
            "schema_version": 1,
            "resource": resource.name,
            "source": resource.source_name,
            "target": resource.target_name,
            "owners": {
                name: binding.owner for name, binding in resource.fields.items()
            },
            "values": dict(baseline.values),
        }
    )


def _baseline_complete(resource: Resource, baseline: Baseline | None) -> bool:
    if baseline is None:
        return False
    required = {
        name for name, binding in resource.fields.items() if binding.owner != "ignore"
    }
    return required.issubset(baseline.values)


def _prepare_reverse_groups(
    record: _PlanRecord,
    item: _ResourcePlan,
    fields: tuple[str, ...],
) -> dict[Path, tuple[bytes, FileObservation, tuple[str, ...]]]:
    assert item.inputs is not None
    groups: dict[Path, _ReverseGroup] = {}
    for name in fields:
        binding = item.resource.fields[name]
        input_name = binding.input
        if input_name is None:
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=item.resource.name,
                fields=(name,),
            )
        input_binding = record.manifest.inputs.get(input_name)
        if input_binding is None:
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=item.resource.name,
                fields=(name,),
            )
        destination = input_binding.reverse_to
        if destination is None:
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=item.resource.name,
                fields=(name,),
            )
        path = destination.file
        group = groups.get(path)
        if group is None:
            group = _ReverseGroup(destination.format)
            groups[path] = group
        elif group.format != destination.format:
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=item.resource.name,
                fields=(name,),
            )
        value = item.live_values[name]
        previous = group.edits.get(destination.path, MISSING)
        if previous is not MISSING and not semantic_equal(previous, value):
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=item.resource.name,
                fields=(name,),
            )
        group.edits[destination.path] = value
        group.fields.append(name)

    prepared: dict[Path, tuple[bytes, FileObservation, tuple[str, ...]]] = {}
    for path, group in groups.items():
        expected = item.inputs.files.get(path)
        if expected is None:
            raise EngineError(
                "stale_plan", operation="reverse-sync", resource=item.resource.name
            )
        format_name = group.format
        if expected.data is None:
            empty = b"" if format_name == "toml" else b"{}"
            document = parse_document(empty, format=format_name)
        else:
            document = parse_document(expected.data, format=format_name)
        data = patch_document(document, group.edits)
        prepared[path] = (data, expected, tuple(sorted(group.fields)))
    return prepared


def _preflight_reverse_consumers(
    record: _PlanRecord,
    item: _ResourcePlan,
    fields: tuple[str, ...],
    updates: Mapping[tuple[Path, tuple[str, ...]], object],
) -> None:
    for consumer in record.resources:
        if consumer.inputs is None:
            continue
        if consumer.resource.kind == "copy" and any(
            _input_binding_uses_locations(record.manifest, name, updates)
            for name in consumer.resource.input_names
        ):
            raise EngineError(
                "reverse_consumer_change",
                operation="reverse-sync",
                resource=consumer.resource.name,
            )
        affected = tuple(
            sorted(
                name
                for name, binding in consumer.resource.fields.items()
                if binding.input is not None
                and _input_binding_uses_locations(
                    record.manifest, binding.input, updates
                )
            )
        )
        if not affected:
            continue
        if consumer.undeclared_changed or consumer.desired_document is None:
            raise EngineError(
                "review_required",
                operation="reverse-sync",
                resource=consumer.resource.name,
                fields=affected,
            )
        simulated_inputs = _with_input_location_updates(
            record.manifest, consumer.inputs, updates
        )
        try:
            source_data = (
                None
                if consumer.source_observation is None
                else consumer.source_observation.data
            )
            if source_data is None or consumer.resource.format is None:
                raise ValueError
            template_text = source_data.decode("utf-8")
            rendered = render_source(
                template_text,
                dict(simulated_inputs.template_variables()),
                resource_name=consumer.resource.name,
            )
            simulated_document = parse_document(
                rendered.encode("utf-8"), format=consumer.resource.format
            )
            simulated_values = _project(simulated_document, consumer.resource.fields)
        except (LuwuError, UnicodeError, ValueError, TypeError):
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=consumer.resource.name,
                fields=affected,
            ) from None
        allowed_paths = (
            tuple(item.resource.fields[name].path for name in fields)
            if consumer.resource.name == item.resource.name
            else ()
        )
        if not unselected_equal(
            consumer.desired_document, simulated_document, allowed_paths
        ):
            unselected_fields = tuple(
                name
                for name in affected
                if consumer.resource.name != item.resource.name or name not in fields
            )
            raise EngineError(
                "reverse_consumer_change"
                if unselected_fields
                else "reverse_destination",
                operation="reverse-sync",
                resource=consumer.resource.name,
                fields=unselected_fields or affected,
            )
        for name in affected:
            input_name = consumer.resource.fields[name].input
            assert input_name is not None
            resolved = simulated_inputs.values.get(input_name)
            desired_value = simulated_values.get(name, MISSING)
            live_value = consumer.live_values.get(name, MISSING)
            if (
                resolved is None
                or desired_value is MISSING
                or live_value is MISSING
                or not semantic_equal(live_value, desired_value)
                or not semantic_equal(desired_value, resolved.value)
            ):
                unselected = (
                    consumer.resource.name != item.resource.name or name not in fields
                )
                raise EngineError(
                    "reverse_consumer_change" if unselected else "reverse_destination",
                    operation="reverse-sync",
                    resource=consumer.resource.name,
                    fields=(name,),
                )


def _reverse_location_updates(
    manifest: Manifest,
    item: _ResourcePlan,
    fields: tuple[str, ...],
) -> dict[tuple[Path, tuple[str, ...]], object]:
    updates: dict[tuple[Path, tuple[str, ...]], object] = {}
    owners: dict[tuple[Path, tuple[str, ...]], str] = {}
    for name in fields:
        binding = item.resource.fields[name]
        input_binding = manifest.inputs.get(binding.input)
        destination = None if input_binding is None else input_binding.reverse_to
        value = item.live_values.get(name, MISSING)
        if destination is None or value is MISSING:
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=item.resource.name,
                fields=(name,),
            )
        path = _absolute_input_path(manifest.roots.inputs.path, destination.file)
        location = (path, destination.path)
        previous = updates.get(location, MISSING)
        if previous is not MISSING and not semantic_equal(previous, value):
            raise EngineError(
                "reverse_destination",
                operation="reverse-sync",
                resource=item.resource.name,
                fields=(owners[location], name),
            )
        updates[location] = value
        owners[location] = name

    locations = tuple(updates)
    for index, (left_file, left_path) in enumerate(locations):
        for right_file, right_path in locations[index + 1 :]:
            if left_file != right_file:
                continue
            if left_path == right_path:
                continue
            if (
                left_path == right_path[: len(left_path)]
                or right_path == left_path[: len(right_path)]
            ):
                raise EngineError(
                    "reverse_destination",
                    operation="reverse-sync",
                    resource=item.resource.name,
                    fields=fields,
                )
    return updates


def _input_binding_uses_locations(
    manifest: Manifest,
    input_name: str,
    updates: Mapping[tuple[Path, tuple[str, ...]], object],
) -> bool:
    binding = manifest.inputs.get(input_name)
    if binding is None:
        return False
    root = manifest.roots.inputs.path
    return any(
        (_absolute_input_path(root, layer.file), layer.path) in updates
        for layer in binding.layers
    )


def _reverse_consumer_resource_names(
    record: _PlanRecord,
    updates: Mapping[tuple[Path, tuple[str, ...]], object],
) -> frozenset[str]:
    return frozenset(
        item.resource.name
        for item in record.resources
        if item.inputs is not None
        and (
            any(
                binding.input is not None
                and _input_binding_uses_locations(
                    record.manifest, binding.input, updates
                )
                for binding in item.resource.fields.values()
            )
            or any(
                _input_binding_uses_locations(record.manifest, name, updates)
                for name in item.resource.input_names
            )
        )
    )


def _absolute_input_path(root: Path, path: Path) -> Path:
    try:
        return absolute_input_path(root, path)
    except InputError:
        raise EngineError("reverse_destination") from None


def _with_input_location_updates(
    manifest: Manifest,
    context: InputContext,
    updates: Mapping[tuple[Path, tuple[str, ...]], object],
) -> InputContext:
    root = manifest.roots.inputs.path
    values: dict[str, ResolvedInput] = {}
    for name in context.values:
        binding = manifest.inputs[name]
        contributors: list[InputProvenance] = []
        for layer_index, layer in enumerate(binding.layers):
            path = _absolute_input_path(root, layer.file)
            observation = context.files.get(path)
            if observation is None:
                raise EngineError("reverse_destination")
            if observation.data is None:
                if layer.optional:
                    value: object = MISSING
                else:
                    raise EngineError("reverse_destination")
            else:
                try:
                    document = parse_document(observation.data, format=layer.format)
                    value = get_leaf(document, layer.path)
                except LuwuError:
                    raise EngineError("reverse_destination") from None
            value = updates.get((path, layer.path), value)
            if value is MISSING:
                if layer.optional:
                    continue
                raise EngineError("reverse_destination")
            relative = path.relative_to(root)
            category = _input_category(relative)
            contributors.append(
                InputProvenance(
                    layer_index=layer_index,
                    file=path,
                    path=layer.path,
                    category=category,
                    value=value,
                    identity=observation.identity,
                    digest=observation.digest,
                )
            )
        if not contributors:
            raise EngineError("reverse_destination")
        winner = contributors[-1]
        values[name] = ResolvedInput(
            name=name,
            value=winner.value,
            winner=winner,
            layers=tuple(contributors),
        )
    return InputContext(MappingProxyType(values), context.files)


def _verify_reverse_results(
    record: _PlanRecord,
    fresh: tuple[_ResourcePlan, ...],
    resource_name: str,
    fields: tuple[str, ...],
    updates: Mapping[tuple[Path, tuple[str, ...]], object],
) -> bool:
    originals = {item.resource.name: item for item in record.resources}
    for candidate in fresh:
        original = originals.get(candidate.resource.name)
        if original is None:
            return False
        selected_paths = (
            tuple(original.resource.fields[name].path for name in fields)
            if candidate.resource.name == resource_name
            else ()
        )
        if original.desired_document is not None and (
            candidate.desired_document is None
            or not unselected_equal(
                original.desired_document,
                candidate.desired_document,
                selected_paths,
            )
        ):
            return False
        if candidate.inputs is None:
            continue
        affected = tuple(
            name
            for name, binding in candidate.resource.fields.items()
            if binding.input is not None
            and _input_binding_uses_locations(record.manifest, binding.input, updates)
        )
        if not affected:
            continue
        if candidate.status == "blocked" or candidate.undeclared_changed:
            return False
        for name in affected:
            binding = candidate.resource.fields[name]
            if binding.input is None:
                return False
            input_value = candidate.inputs.values.get(binding.input)
            desired = candidate.desired_values.get(name, MISSING)
            live = candidate.live_values.get(name, MISSING)
            if input_value is None or desired is MISSING or live is MISSING:
                return False
            if not (
                semantic_equal(live, desired)
                and semantic_equal(desired, input_value.value)
            ):
                return False
    return True


def _verify_forward_results(
    original: tuple[_ResourcePlan, ...],
    fresh: tuple[_ResourcePlan, ...],
    specs: tuple[_WriteSpec, ...],
) -> bool:
    written = {spec.resource.name: spec for spec in specs}
    for item in original:
        spec = written.get(item.resource.name)
        if spec is None:
            continue
        current = _resource_by_name(fresh, item.resource.name)
        if current.status == "blocked" or current.target_observation is None:
            return False
        if item.resource.kind == "symlink":
            if current.status != "in_sync":
                return False
        elif item.resource.kind == "copy":
            if (
                current.desired_data is None
                or current.target_observation.data != current.desired_data
            ):
                return False
        elif any(
            not _field_equal(current.desired_values, current.live_values, name)
            for name in spec.fields
        ):
            return False
    return True


def _assert_fresh(
    record: _PlanRecord,
    *,
    resource_names: frozenset[str] | None = None,
    expected_targets: Mapping[Path, FileObservation] | None = None,
    expected_baselines: Mapping[Path, FileObservation] | None = None,
    expected_inputs: Mapping[Path, FileObservation] | None = None,
    expected_directories: Mapping[Path, tuple[tuple[int, int], int]] | None = None,
) -> tuple[_ResourcePlan, ...]:
    expected_targets = {} if expected_targets is None else expected_targets
    expected_baselines = {} if expected_baselines is None else expected_baselines
    expected_inputs = {} if expected_inputs is None else expected_inputs
    expected_directories = {} if expected_directories is None else expected_directories
    try:
        if resource_names is None:
            fresh = _observe_all(record.manifest)
        else:
            selected_resources = tuple(
                resource
                for resource in record.manifest.resources
                if resource.name in resource_names
            )
            if len(selected_resources) != len(resource_names):
                raise EngineError("stale_plan")
            fresh = tuple(
                _observe_resource(record.manifest, resource)
                for resource in selected_resources
            )
        verify_manifest(record.manifest)
    except EngineError:
        raise
    except LuwuError:
        raise EngineError("stale_plan") from None
    except (OSError, ValueError, NotImplementedError):
        raise EngineError("stale_plan") from None
    if resource_names is None and len(fresh) != len(record.resources):
        raise EngineError("stale_plan")
    for current in fresh:
        old = _resource_by_name(record.resources, current.resource.name)
        if old.source_observation != current.source_observation:
            raise EngineError("stale_plan", resource=old.resource.name)
        target_path = old.resource.target
        target_expected = expected_targets.get(target_path, old.target_observation)
        if target_expected != current.target_observation:
            raise EngineError("stale_plan", resource=old.resource.name)
        baseline_path = old.resource.baseline
        baseline_expected = expected_baselines.get(
            baseline_path, old.baseline_observation
        )
        if baseline_expected != current.baseline_observation:
            raise EngineError("stale_plan", resource=old.resource.name)
        old_files = {} if old.inputs is None else old.inputs.files
        new_files = {} if current.inputs is None else current.inputs.files
        if set(old_files) != set(new_files):
            raise EngineError("stale_plan", resource=old.resource.name)
        for path, previous in old_files.items():
            expected = expected_inputs.get(path, previous)
            if expected != new_files[path]:
                raise EngineError("stale_plan", resource=old.resource.name)
        if old.desired_data != current.desired_data:
            changed_inputs = set(old_files) & set(expected_inputs)
            if not changed_inputs:
                raise EngineError("stale_plan", resource=old.resource.name)
        paths = set(old.parent_states) | set(current.parent_states)
        for path in paths:
            expected_created = expected_directories.get(path)
            expected_identity = (
                expected_created[0]
                if expected_created is not None
                else old.parent_states.get(path)
            )
            if expected_identity != current.parent_states.get(path):
                raise EngineError("stale_plan", resource=old.resource.name)
    for path, (identity, mode) in expected_directories.items():
        root = _directory_root(record.manifest, path)
        current_identity, current_mode = _observe_directory_mode(root, path)
        if current_identity != identity or current_mode != mode:
            raise EngineError("stale_plan")
    return fresh


def _assert_forward_fresh(
    record: _PlanRecord,
    *,
    resource_names: frozenset[str] | None = None,
    expected_targets: Mapping[Path, FileObservation] | None = None,
    expected_baselines: Mapping[Path, FileObservation] | None = None,
    expected_inputs: Mapping[Path, FileObservation] | None = None,
    expected_directories: Mapping[Path, tuple[tuple[int, int], int]] | None = None,
) -> tuple[_ResourcePlan, ...]:
    """Recheck selected dependencies and their current eligibility."""

    fresh = _assert_fresh(
        record,
        resource_names=resource_names,
        expected_targets=expected_targets,
        expected_baselines=expected_baselines,
        expected_inputs=expected_inputs,
        expected_directories=expected_directories,
    )
    if any(
        item.undeclared_changed
        or item.status in {"blocked", "conflict", "unbased", "adopt_required"}
        for item in fresh
    ):
        raise EngineError("review_required", operation="apply")
    return fresh


def _recheck_fresh(
    record: _PlanRecord,
    *,
    resource_names: frozenset[str] | None = None,
    expected_targets: Mapping[Path, FileObservation] | None = None,
    expected_baselines: Mapping[Path, FileObservation] | None = None,
    expected_inputs: Mapping[Path, FileObservation] | None = None,
    expected_directories: Mapping[Path, tuple[tuple[int, int], int]] | None = None,
) -> None:
    _assert_fresh(
        record,
        resource_names=resource_names,
        expected_targets=expected_targets,
        expected_baselines=expected_baselines,
        expected_inputs=expected_inputs,
        expected_directories=expected_directories,
    )


def _recheck_forward_fresh(
    record: _PlanRecord,
    *,
    resource_names: frozenset[str] | None = None,
    expected_targets: Mapping[Path, FileObservation],
    expected_baselines: Mapping[Path, FileObservation],
    expected_inputs: Mapping[Path, FileObservation],
    expected_directories: Mapping[Path, tuple[tuple[int, int], int]],
) -> None:
    _assert_forward_fresh(
        record,
        resource_names=resource_names,
        expected_targets=expected_targets,
        expected_baselines=expected_baselines,
        expected_inputs=expected_inputs,
        expected_directories=expected_directories,
    )


def _matches_own_link(
    expected: FileObservation, observed: FileObservation, identity: tuple[int, int]
) -> bool:
    return (
        observed.identity == identity
        and observed.link_target == expected.link_target
        and not observed.foreign
        and observed.existing_ancestors == expected.existing_ancestors
        and not observed.missing_chain
    )


def _matches_own_write(
    expected: FileObservation,
    observed: FileObservation,
    identity: tuple[int, int],
    data: bytes,
    requested_mode: int | None,
) -> bool:
    """Bind a successful write only to its returned inode and planned state."""

    expected_mode = requested_mode
    if expected_mode is None:
        expected_mode = expected.mode if expected.identity is not None else 0o644
    return (
        not expected.missing_chain
        and observed.identity == identity
        and observed.data == data
        and observed.digest == hashlib.sha256(data).hexdigest()
        and observed.mode == expected_mode
        and observed.existing_ancestors == expected.existing_ancestors
        and not observed.missing_chain
    )


def _expected_write_observation(
    previous: FileObservation,
    data: bytes,
    identity: tuple[int, int],
    requested_mode: int | None,
) -> FileObservation:
    """Advance only the leaf snapshot from a successful writer return."""

    mode = requested_mode
    if mode is None:
        mode = previous.mode if previous.identity is not None else 0o644
    return replace(
        previous,
        data=data,
        identity=identity,
        digest=hashlib.sha256(data).hexdigest(),
        mode=mode,
        missing_chain=(),
    )


def _filesystem_failure_state(error: FilesystemError) -> str:
    if error.committed:
        return "committed"
    if error.state == "uncertain":
        return "uncertain"
    return "not_replaced"


def _directory_failure_state(error: FilesystemError) -> str:
    if error.committed:
        return "committed"
    if error.state == "uncertain":
        return "uncertain"
    return "not_attempted"


def _verify_created_parent(
    root: Path,
    path: Path,
    created_identity: tuple[int, int],
    requested_mode: int,
) -> None:
    """Verify a returned mkdir identity before treating it as expected state."""

    observation_failed = False
    current_identity: tuple[int, int] | None = None
    current_mode: int | None = None
    try:
        current_identity, current_mode = _observe_directory_mode(root, path)
    except EngineError:
        observation_failed = True
    if (
        observation_failed
        or current_identity != created_identity
        or current_mode != requested_mode
    ):
        raise FilesystemError(
            "created parent identity could not be verified",
            code="parent_create_unknown",
            state="uncertain",
            created_identity=created_identity,
        )


def _write_failure_result(
    operation: str,
    actions: list[ActionResult],
    *,
    kind: str,
    resource: Resource,
    root: Path,
    path: Path,
    fields: tuple[str, ...],
    error: FilesystemError,
    committed_before: bool,
) -> Result:
    """Preserve the writer's outcome independently from later verification."""

    state = _filesystem_failure_state(error)
    _mark_action(
        actions,
        kind,
        resource.name,
        _relative_path(root, path),
        state,
        fields,
    )
    if state in {"committed", "uncertain"}:
        outcome = "committed_state_unknown"
    else:
        outcome = "partial" if committed_before else "not_replaced"
    return _finish_actions(operation, actions, outcome, error.code)


def _refresh_expected_ancestry(
    record: _PlanRecord,
    directory: Path,
    identity: tuple[int, int],
    *,
    expected_targets: dict[Path, FileObservation] | None = None,
    expected_baselines: dict[Path, FileObservation] | None = None,
    expected_inputs: dict[Path, FileObservation] | None = None,
) -> None:
    """Account only for the next missing ancestor created by this operation."""

    def update(observation: FileObservation) -> FileObservation:
        if not observation.missing_chain or observation.missing_chain[0] != directory:
            return observation
        return replace(
            observation,
            existing_ancestors=(*observation.existing_ancestors, (directory, identity)),
            missing_chain=observation.missing_chain[1:],
        )

    for item in record.resources:
        target = item.resource.target
        if expected_targets is not None:
            previous = expected_targets.get(target, item.target_observation)
            if previous is not None:
                expected_targets[target] = update(previous)

        baseline_path = item.resource.baseline
        if expected_baselines is not None and baseline_path is not None:
            previous = expected_baselines.get(baseline_path, item.baseline_observation)
            if previous is not None:
                expected_baselines[baseline_path] = update(previous)

        if expected_inputs is not None and item.inputs is not None:
            for path, original in item.inputs.files.items():
                previous = expected_inputs.get(path, original)
                expected_inputs[path] = update(previous)


def _directory_root(manifest: Manifest, path: Path) -> Path:
    for boundary in (manifest.roots.target, manifest.roots.inputs):
        try:
            path.relative_to(boundary.path)
        except ValueError:
            continue
        return boundary.path
    raise EngineError("stale_plan")


def _observe_directory_mode(root: Path, path: Path) -> tuple[tuple[int, int], int]:
    parent_descriptor: int | None = None
    child_descriptor: int | None = None
    try:
        parent_descriptor, name = open_parent_directory(root, path)
        child_descriptor, opened = open_child_directory(parent_descriptor, name)
        return (opened.st_dev, opened.st_ino), stat.S_IMODE(opened.st_mode)
    except (OSError, ValueError, NotImplementedError):
        raise EngineError("stale_plan") from None
    finally:
        for descriptor in (child_descriptor, parent_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _plan_projection(plan: Plan) -> list[object]:
    """Project every protected plan value into one complete graph."""

    if type(plan) is not Plan or type(plan._resources) is not tuple:
        raise EngineError("plan_invalid")
    if type(plan.manifest) is not Manifest or type(plan.manifest._signature) is not str:
        raise EngineError("plan_invalid")

    values: list[object] = [
        "luwu-tmp-plan-v1",
        plan.manifest._signature,
        len(plan._resources),
    ]
    for item in plan._resources:
        if type(item) is not _ResourcePlan or type(item.resource) is not Resource:
            raise EngineError("plan_invalid")
        resource = item.resource
        if type(resource.fields) is not MappingProxyType:
            raise EngineError("plan_invalid")
        field_names = tuple(resource.fields)
        if any(type(name) is not str for name in field_names):
            raise EngineError("plan_invalid")
        fields: dict[str, object] = {}
        for name in sorted(field_names):
            binding = resource.fields[name]
            if type(binding) is not FieldBinding or type(binding.path) is not tuple:
                raise EngineError("plan_invalid")
            fields[name] = {
                "path": list(binding.path),
                "owner": binding.owner,
                "input": binding.input,
            }
        resource_value: dict[str, object] = {
            "resource": {
                "name": resource.name,
                "kind": resource.kind,
                "source": _projection_path(resource.source),
                "target": _projection_path(resource.target),
                "format": resource.format,
                "baseline": None
                if resource.baseline is None
                else _projection_path(resource.baseline),
                "parents": resource.parents,
                "parent_mode": resource.parent_mode,
                "mode": resource.mode,
                "source_name": resource.source_name,
                "target_name": resource.target_name,
                "fields": fields,
            },
            "status": item.status,
            "action": item.action,
            "reason": item.reason,
            "error_code": item.error_code,
            "undeclared_changed": item.undeclared_changed,
            "forward": list(item.forward_fields),
            "reverse": list(item.reverse_fields),
            "desired_values": dict(item.desired_values),
            "live_values": dict(item.live_values),
            "source_observation": _observation_value(item.source_observation)
            if item.source_observation is not None
            else None,
            "target_observation": _observation_value(item.target_observation)
            if item.target_observation is not None
            else None,
            "baseline_observation": _observation_value(item.baseline_observation)
            if item.baseline_observation is not None
            else None,
            "desired_data": item.desired_data,
            "link_value": item.link_value,
            "desired_document": _document_value(item.desired_document),
            "target_document": _document_value(item.target_document),
            "write_data": item.write_data,
            "target_missing_parents": [
                _projection_path(path) for path in item.target_missing_parents
            ],
            "baseline_missing_parents": [
                _projection_path(path) for path in item.baseline_missing_parents
            ],
        }
        if type(item.parent_states) is not MappingProxyType:
            raise EngineError("plan_invalid")
        resource_value["parent_states"] = {
            _projection_path(path): None if identity is None else list(identity)
            for path, identity in item.parent_states.items()
        }
        if type(item.input_missing_parents) is not MappingProxyType:
            raise EngineError("plan_invalid")
        resource_value["input_missing_parents"] = {
            _projection_path(path): [_projection_path(parent) for parent in missing]
            for path, missing in item.input_missing_parents.items()
        }

        if item.inputs is None:
            resource_value["inputs"] = None
        else:
            if (
                type(item.inputs) is not InputContext
                or type(item.inputs.files) is not MappingProxyType
                or type(item.inputs.values) is not MappingProxyType
            ):
                raise EngineError("plan_invalid")
            file_entries: list[object] = []
            for path, observation in sorted(
                item.inputs.files.items(),
                key=lambda part: _projection_path(part[0]),
            ):
                file_entries.append(
                    [
                        _projection_path(path),
                        _observation_value(observation),
                    ]
                )
            value_entries: list[object] = []
            for name, resolved in sorted(item.inputs.values.items()):
                if type(resolved) is not ResolvedInput:
                    raise EngineError("plan_invalid")
                layers = tuple(
                    _input_provenance_value(candidate) for candidate in resolved.layers
                )
                value_entries.append(
                    {
                        "name": name,
                        "value": resolved.value,
                        "winner": _input_provenance_value(resolved.winner),
                        "layers": layers,
                    }
                )
            resource_value["inputs"] = {"files": file_entries, "values": value_entries}

        if item.baseline is None:
            resource_value["baseline"] = None
        else:
            if (
                type(item.baseline) is not Baseline
                or type(item.baseline.values) is not MappingProxyType
            ):
                raise EngineError("plan_invalid")
            resource_value["baseline"] = {
                "values": dict(item.baseline.values),
                "whole_file_digest": item.baseline.whole_file_digest,
                "whole_file_mode": item.baseline.whole_file_mode,
                "data": item.baseline.data,
            }
        if item.ownership is None:
            resource_value["ownership"] = None
        else:
            if type(item.ownership) is not OwnershipResult:
                raise EngineError("plan_invalid")
            fields_value: list[object] = []
            for ownership_field in item.ownership.fields:
                if type(ownership_field) is not OwnershipField:
                    raise EngineError("plan_invalid")
                fields_value.append(ownership_field.to_dict())
            resource_value["ownership"] = {
                "baseline_status": item.ownership.baseline_status,
                "undeclared_changed": item.ownership.undeclared_changed,
                "fields": fields_value,
            }
        values.append(resource_value)
    return values


def _projection_path(path: Path) -> str:
    if type(path) is not _PATH_TYPE:
        raise EngineError("plan_invalid")
    return str(path)


def _input_provenance_value(candidate: InputProvenance) -> dict[str, object]:
    if type(candidate) is not InputProvenance or type(candidate.path) is not tuple:
        raise EngineError("plan_invalid")
    return {
        "layer": candidate.layer_index,
        "file": _projection_path(candidate.file),
        "path": list(candidate.path),
        "category": candidate.category,
        "value": candidate.value,
        "identity": None if candidate.identity is None else list(candidate.identity),
        "digest": candidate.digest,
    }


def _observation_value(observation: FileObservation) -> dict[str, object]:
    if type(observation) is not FileObservation:
        raise EngineError("plan_invalid")
    return {
        "data": observation.data,
        "identity": None
        if observation.identity is None
        else list(observation.identity),
        "digest": observation.digest,
        "mode": observation.mode,
        "existing_ancestors": [
            [_projection_path(path), list(identity)]
            for path, identity in observation.existing_ancestors
        ],
        "missing_chain": [_projection_path(path) for path in observation.missing_chain],
        "link_target": observation.link_target,
        "foreign": observation.foreign,
    }


def _document_value(document: Document | None) -> object:
    if document is None:
        return None
    if (
        type(document) is not Document
        or type(document.data) is not bytes
        or type(document.value) is not dict
        or type(document._format) is not str
        or type(document._root) is not _Node
        or type(document._toml_headers) is not tuple
        or type(document._toml_inline_tables) is not dict
    ):
        raise EngineError("plan_invalid")
    inline_items = tuple(document._toml_inline_tables.items())
    if any(
        type(path) is not tuple
        or any(type(segment) is not str for segment in path)
        or type(table) is not _TomlInlineTable
        for path, table in inline_items
    ):
        raise EngineError("plan_invalid")
    inline_tables = tuple(sorted(inline_items, key=lambda entry: entry[0]))
    return {
        "data": document.data,
        "value": document.value,
        "format": document._format,
        "root": document._root,
        "toml_headers": document._toml_headers,
        "toml_inline_tables": inline_tables,
    }


def _require_plan(plan: Plan, *, operation: str) -> _PlanRecord:
    if not isinstance(plan, Plan) or plan._provenance is not _PLAN_PROVENANCE:
        raise EngineError("plan_invalid", operation=operation)
    record = _PLAN_REGISTRY.get(plan)
    if (
        record is None
        or plan.manifest is not record.manifest
        or plan._resources is not record.resources
    ):
        raise EngineError("plan_invalid", operation=operation)
    try:
        matches = _matches_plan_evidence(_plan_projection(plan), record.evidence)
    except Exception:  # noqa: BLE001 - caller-mutated plans have no trusted details
        raise EngineError("plan_invalid", operation=operation) from None
    if not matches:
        raise EngineError("plan_invalid", operation=operation)
    try:
        verify_manifest(record.manifest)
        manifest = record.manifest
        resources = _observe_all(manifest)
        verify_manifest(manifest)
        authorized_plan = Plan(manifest, resources, _PLAN_PROVENANCE)
        authorized_matches = _matches_plan_evidence(
            _plan_projection(authorized_plan), record.evidence
        )
    except LuwuError as exc:
        code = "plan_invalid" if exc.code == "provenance" else "stale_plan"
        raise EngineError(code, operation=operation) from None
    except (OSError, ValueError, NotImplementedError):
        raise EngineError("stale_plan", operation=operation) from None
    except Exception:  # noqa: BLE001 - loader details are never exposed
        raise EngineError("stale_plan", operation=operation) from None
    if not authorized_matches:
        raise EngineError("stale_plan", operation=operation)
    return _PlanRecord(manifest, resources, record.evidence)


def _mutation_record(plan: Plan, *, operation: str) -> _PlanRecord | None:
    try:
        return _require_plan(plan, operation=operation)
    except EngineError as exc:
        if exc.code == "stale_plan":
            return None
        raise


def _validate_selection(
    item: _ResourcePlan, fields: tuple[str, ...], *, operation: str
) -> None:
    if (
        not isinstance(fields, tuple)
        or not fields
        or any(not isinstance(name, str) for name in fields)
        or len(set(fields)) != len(fields)
    ):
        raise EngineError(
            "field_invalid",
            operation=operation,
            resource=item.resource.name,
        )
    if any(
        not isinstance(name, str) or name not in item.resource.fields for name in fields
    ):
        raise EngineError(
            "field_invalid", operation=operation, resource=item.resource.name
        )
    if any(item.resource.fields[name].owner == "ignore" for name in fields):
        raise EngineError(
            "field_invalid",
            operation=operation,
            resource=item.resource.name,
            fields=fields,
        )


def _resource_by_name(resources: tuple[_ResourcePlan, ...], name: str) -> _ResourcePlan:
    for item in resources:
        if item.resource.name == name:
            return item
    raise EngineError("operation_invalid")


def _resource_for_path(
    resources: tuple[_ResourcePlan, ...], path: Path
) -> _ResourcePlan:
    for item in resources:
        if item.resource.target == path:
            return item
    raise EngineError("plan_invalid")


def _field_equal(
    left: Mapping[str, object], right: Mapping[str, object], name: str
) -> bool:
    left_value = left.get(name, MISSING)
    right_value = right.get(name, MISSING)
    if left_value is MISSING or right_value is MISSING:
        return left_value is right_value
    return semantic_equal(left_value, right_value)


def _mapping_values_equal(
    left: Mapping[str, object], right: Mapping[str, object], names: tuple[str, ...]
) -> bool:
    return all(_field_equal(left, right, name) for name in names)


def _extend_parent_states(
    states: dict[Path, tuple[int, int] | None],
    root: Path,
    path: Path,
) -> None:
    relative = path.relative_to(root)
    current = root
    boundary = capture_directory_boundary(root)
    states[current] = (boundary.device, boundary.inode)
    missing_seen = False
    for segment in relative.parts[:-1]:
        current = current / segment
        if missing_seen:
            states[current] = None
            continue
        try:
            boundary = capture_directory_boundary(current)
        except FileNotFoundError:
            states[current] = None
            missing_seen = True
        except (OSError, ValueError, NotImplementedError):
            raise EngineError("stale_plan") from None
        else:
            states[current] = (boundary.device, boundary.inode)


def _forward_specs_for_item(
    item: _ResourcePlan, manifest: Manifest
) -> _WriteSpec | None:
    if item.action not in {"forward", "create"} or item.target_observation is None:
        return None
    if item.link_value is not None:
        return _WriteSpec(
            "link_create",
            item.resource,
            manifest.roots.target.path,
            item.resource.target,
            b"",
            item.target_observation,
            None,
            item.forward_fields,
            link=item.link_value,
        )
    if item.write_data is None:
        return None
    return _WriteSpec(
        "target_write",
        item.resource,
        manifest.roots.target.path,
        item.resource.target,
        item.write_data,
        item.target_observation,
        item.resource.mode,
        item.forward_fields,
    )


def _forward_specs(
    resources: tuple[_ResourcePlan, ...], manifest: Manifest
) -> tuple[_WriteSpec, ...]:
    return tuple(
        spec
        for item in resources
        if (spec := _forward_specs_for_item(item, manifest)) is not None
    )


def _baseline_preview_spec(
    manifest: Manifest,
    item: _ResourcePlan,
    fields: tuple[str, ...],
) -> _WriteSpec:
    path = item.resource.baseline
    if path is None or item.baseline_observation is None:
        raise EngineError("operation_invalid", resource=item.resource.name)
    return _WriteSpec(
        "baseline_write",
        item.resource,
        manifest.roots.inputs.path,
        path,
        b"",
        item.baseline_observation,
        0o600,
        fields,
    )


def _initial_action_results(
    specs: tuple[_WriteSpec, ...],
    *,
    include_directories: Mapping[Path, tuple[Path, ...]] | None = None,
    root: Path | None = None,
) -> list[ActionResult]:
    actions: list[ActionResult] = []
    include_directories = {} if include_directories is None else include_directories
    seen_directories: set[Path] = set()
    for spec in specs:
        if root is not None:
            for directory in include_directories.get(spec.path, ()):
                if directory not in seen_directories:
                    seen_directories.add(directory)
                    actions.append(
                        ActionResult(
                            "mkdir",
                            spec.resource.name,
                            _relative_path(root, directory),
                            "not_attempted",
                        )
                    )
        actions.append(
            ActionResult(
                spec.kind,
                spec.resource.name,
                _relative_path(spec.root, spec.path),
                "not_attempted",
                spec.fields,
            )
        )
    return actions


def _shared_directory_mode_conflict(resources: tuple[_ResourcePlan, ...]) -> bool:
    directories = {
        directory
        for item in resources
        if item.action in {"forward", "create"} and item.resource.parents == "create"
        for directory in item.target_missing_parents
    }
    for directory in directories:
        if _shared_parent_mode_conflict(
            resources, _directory_parent_resource_names(resources, directory)
        ):
            return True
    return False


def _directory_parent_resource_names(
    resources: tuple[_ResourcePlan, ...], directory: Path
) -> frozenset[str]:
    return frozenset(
        item.resource.name
        for item in resources
        if item.resource.parents == "create"
        and (
            directory in item.target_missing_parents
            or directory in item.baseline_missing_parents
            or any(
                directory in missing for missing in item.input_missing_parents.values()
            )
        )
    )


def _shared_parent_mode_conflict(
    resources: tuple[_ResourcePlan, ...], resource_names: frozenset[str]
) -> bool:
    return (
        len(
            {
                item.resource.parent_mode
                for item in resources
                if item.resource.name in resource_names
            }
        )
        > 1
    )


def _unsafe_shared_parent_mode(
    resources: tuple[_ResourcePlan, ...], directories: tuple[Path, ...]
) -> bool:
    for directory in directories:
        resource_names = _directory_parent_resource_names(resources, directory)
        if any(
            item.resource.name in resource_names and item.resource.parent_mode & 0o022
            for item in resources
        ):
            return True
    return False


def _mutation_actions_for_paths(
    operation: str,
    resource: Resource,
    root: Path,
    parents: tuple[Path, ...],
    path: Path,
    kind: str,
    fields: tuple[str, ...],
) -> list[ActionResult]:
    del operation
    actions = [
        ActionResult(
            "mkdir", resource.name, _relative_path(root, parent), "not_attempted"
        )
        for parent in parents
    ]
    actions.append(
        ActionResult(
            kind, resource.name, _relative_path(root, path), "not_attempted", fields
        )
    )
    return actions


def _result_for_specs(
    operation: str,
    outcome: str,
    specs: tuple[_WriteSpec, ...],
    *,
    error_code: str | None = None,
    include_directories: Mapping[Path, tuple[Path, ...]] | None = None,
    root: Path | None = None,
) -> Result:
    return Result(
        operation,
        outcome,
        tuple(
            _initial_action_results(
                specs,
                include_directories=include_directories,
                root=root,
            )
        ),
        error_code=error_code,
    )


def _mark_action(
    actions: list[ActionResult],
    kind: str,
    resource: str,
    path: str,
    state: str,
    fields: tuple[str, ...] = (),
) -> None:
    for index, action in enumerate(actions):
        if (action.kind, action.resource, action.path) == (kind, resource, path):
            actions[index] = ActionResult(
                kind, resource, path, state, fields or action.fields
            )
            return


def _finish_actions(
    operation: str,
    actions: list[ActionResult],
    outcome: str,
    error_code: str | None,
    *,
    verification_status: str | None = None,
) -> Result:
    return Result(
        operation,
        outcome,
        tuple(actions),
        verification_status=verification_status,
        error_code=None if error_code is None else _safe_code(error_code),
    )


def _safe_code(code: object) -> str:
    if type(code) is str and code in _SAFE_ERROR_CODES:
        return code
    return "resource_blocked"


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return "<outside-root>"


def _mode_text(mode: int) -> str:
    return f"0o{mode:04o}"


def _effective_mode_text(
    resource: Resource, target: FileObservation | None
) -> str | None:
    if resource.kind == "symlink":
        return None
    if resource.mode is not None:
        return _mode_text(resource.mode)
    if target is None:
        return None
    if target.data is None:
        return _mode_text(0o644)
    return None if target.mode is None else _mode_text(target.mode)
