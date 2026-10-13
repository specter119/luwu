"""The bounded, field-aware M6 bidirectional pilot loop."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .baseline import make_baseline, read_baseline, write_baseline
from .errors import LuwuError, MutationError
from .filesystem import (
    FileChangedError,
    create_temporary_file,
    lock_directory,
    open_parent_directory,
    read_regular_file_at,
    sync_directory,
    unlock_directory,
    verify_directory_identity,
)
from .m6_formats import (
    M6FormatError,
    M6InputContext,
    encode_m6_object,
    load_m6_inputs,
    parse_m6_document,
    patch_m6_document,
)
from .manifest import M6_MANIFEST_VERSION, Manifest, Resource, load_manifest
from .ownership import OwnershipResult, classify_fields
from .reconcile import _read_target
from .rendering import RenderedTemplate, render_template


class M6PilotError(LuwuError):
    """A stable, metadata-only M6 pilot boundary error."""


@dataclass(frozen=True, slots=True)
class M6ResourcePlan:
    resource: Resource
    status: str
    action: str
    reason: str
    ownership: OwnershipResult | None = field(repr=False)
    forward_fields: tuple[str, ...]
    reverse_fields: tuple[str, ...]
    desired: dict[str, object] = field(repr=False)
    rendered_data: bytes = field(repr=False)
    live: dict[str, object] | None = field(repr=False)
    input_context: M6InputContext = field(repr=False)
    source_identity: tuple[int, int]
    source_digest: str
    target_identity: tuple[int, int] | None
    target_digest: str | None
    target_parent_identity: tuple[int, int] | None
    baseline_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.resource.name,
            "source": self.resource.source_name,
            "target": self.resource.target_name,
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "forward_fields": list(self.forward_fields),
            "reverse_fields": list(self.reverse_fields),
            "ownership": None if self.ownership is None else self.ownership.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class M6Plan:
    manifest: Manifest
    resources: tuple[M6ResourcePlan, ...]

    @property
    def resource(self) -> M6ResourcePlan:
        if len(self.resources) != 1:
            raise M6PilotError(
                "M6 pilot requires one resource", code="m6_resource_count"
            )
        return self.resources[0]

    def to_dict(self, *, command: str = "plan") -> dict[str, object]:
        return {
            "schema_version": M6_MANIFEST_VERSION,
            "command": command,
            "manifest": str(self.manifest.path),
            "manifest_version": self.manifest.version,
            "resources": [item.to_dict() for item in self.resources],
        }


@dataclass(frozen=True, slots=True)
class M6MutationResult:
    operation: str
    outcome: str
    resource: str
    fields: tuple[str, ...]
    verification: dict[str, object] | None = None
    committed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": M6_MANIFEST_VERSION,
            "command": self.operation,
            "resource": self.resource,
            "fields": list(self.fields),
            "outcome": self.outcome,
            "committed": list(self.committed),
            "verification": self.verification,
        }


def build_m6_plan(manifest: Manifest) -> M6Plan:
    """Observe one version-7 resource without writing any pilot state."""

    _require_m6_manifest(manifest)
    assert manifest.roots is not None
    try:
        manifest.roots.verify()
    except (OSError, NotImplementedError, ValueError, FileChangedError):
        raise M6PilotError(
            "M6 root identity could not be verified", code="m6_root_stale"
        ) from None
    plans = tuple(_plan_resource(manifest, resource) for resource in manifest.resources)
    return M6Plan(manifest, plans)


def accept_m6_baseline(
    manifest: Manifest,
    *,
    resource_name: str,
    fields: tuple[str, ...],
    value_from: str,
    confirm: bool,
) -> M6MutationResult:
    """Explicitly establish or advance selected public baseline fields."""

    plan = build_m6_plan(manifest)
    item = _resource_plan(plan, resource_name)
    if value_from not in {"desired", "live"} or (
        value_from == "live" and item.live is None
    ):
        raise M6PilotError(
            "baseline source or live target is invalid", code="m6_baseline"
        )
    _validate_fields(item.resource, fields)
    current = item.desired if value_from == "desired" else item.live
    existing = read_baseline(manifest.roots.inputs.path, item.resource)
    data = make_baseline(
        resource=item.resource,
        current=current,
        selected_fields=fields,
        existing=existing,
    )
    if not confirm:
        return M6MutationResult(
            "m6-accept", "confirmation_required", resource_name, fields
        )
    _recheck_plan(plan)
    write_baseline(
        manifest.roots.inputs.path,
        item.resource,
        data,
        expected_data=existing,
    )
    verification = build_m6_plan(load_manifest(manifest.path)).to_dict(
        command="verification"
    )
    return M6MutationResult(
        "m6-accept", "committed", resource_name, fields, verification
    )


def apply_m6_forward(plan: M6Plan, *, confirm: bool) -> M6MutationResult:
    """Apply only source-owned field changes to the allowlisted target."""

    item = plan.resource
    if not confirm:
        return M6MutationResult(
            "m6-apply", "confirmation_required", item.resource.name, item.forward_fields
        )
    _recheck_plan(plan)
    current = build_m6_plan(load_manifest(plan.manifest.path))
    fresh = current.resource
    if fresh.forward_fields != item.forward_fields or fresh.action != item.action:
        raise M6PilotError("M6 forward plan is stale", code="m6_stale_plan")
    if fresh.action == "noop":
        return M6MutationResult("m6-apply", "no_changes", fresh.resource.name, ())
    if fresh.action not in {"create", "forward"}:
        raise M6PilotError(
            "M6 plan has no safe forward candidate", code="m6_review_required"
        )
    assert plan.manifest.roots is not None
    if fresh.action == "create":
        data = fresh.rendered_data
        expected_identity = None
        expected_digest = None
    else:
        assert fresh.live is not None
        updates, deletes = _selected_values(fresh, fresh.forward_fields)
        data = patch_m6_document(
            _target_bytes(plan.manifest, fresh),
            format=_format_for(fresh.resource),
            updates=updates,
            deletes=deletes,
        )
        expected_identity = fresh.target_identity
        expected_digest = fresh.target_digest
    _write_atomic(
        plan.manifest.roots.target.path,
        fresh.resource.target,
        data,
        expected_identity=expected_identity,
        expected_digest=expected_digest,
    )
    verification = build_m6_plan(load_manifest(plan.manifest.path))
    outcome = "committed"
    if verification.resource.action in {"forward", "create"}:
        outcome = "committed_but_verification_failed"
    return M6MutationResult(
        "m6-apply",
        outcome,
        fresh.resource.name,
        fresh.forward_fields,
        verification.to_dict(command="verification"),
        (fresh.resource.target_name,),
    )


def reverse_m6_inputs(
    plan: M6Plan,
    *,
    fields: tuple[str, ...],
    confirm: bool,
) -> M6MutationResult:
    """Preview or explicitly patch selected live-owned input fields."""

    item = plan.resource
    _validate_fields(item.resource, fields)
    if any(name not in item.reverse_fields for name in fields):
        raise M6PilotError(
            "selected field is not a reverse candidate", code="m6_review_required"
        )
    if not confirm:
        return M6MutationResult(
            "m6-reverse-sync", "confirmation_required", item.resource.name, fields
        )
    _recheck_plan(plan)
    fresh = build_m6_plan(load_manifest(plan.manifest.path)).resource
    if fresh.action == "review":
        raise M6PilotError(
            "M6 resource requires review before reverse sync",
            code="m6_review_required",
        )
    if tuple(name for name in fields if name not in fresh.reverse_fields):
        raise M6PilotError("M6 reverse plan is stale", code="m6_stale_plan")
    assert plan.manifest.roots is not None
    committed: list[str] = []
    grouped: dict[Path, list[str]] = {}
    for name in fields:
        path = fresh.resource.inputs.get(name)
        if path is None:
            raise M6PilotError(
                "reverse field has no declared input", code="m6_input_mapping"
            )
        grouped.setdefault(path, []).append(name)
    try:
        for path, names in grouped.items():
            raw, identity = _read_file(path)
            live = fresh.live or {}
            updates, deletes = _selected_values_from_object(live, names)
            data = patch_m6_document(
                raw,
                format=_format_for_path(path),
                updates=updates,
                deletes=deletes,
            )
            _write_atomic(
                plan.manifest.roots.inputs.path,
                path,
                data,
                expected_identity=identity,
                expected_digest=_digest(raw),
            )
            committed.extend(names)
    except (M6PilotError, MutationError, LuwuError):
        outcome = "committed_state_unknown" if committed else "not_committed"
        return M6MutationResult(
            "m6-reverse-sync",
            outcome,
            fresh.resource.name,
            fields,
            committed=tuple(committed),
        )
    verification = build_m6_plan(load_manifest(plan.manifest.path))
    outcome = (
        "committed"
        if not any(name in verification.resource.reverse_fields for name in fields)
        else "committed_but_verification_failed"
    )
    return M6MutationResult(
        "m6-reverse-sync",
        outcome,
        fresh.resource.name,
        fields,
        verification.to_dict(command="verification"),
        tuple(committed),
    )


def _plan_resource(manifest: Manifest, resource: Resource) -> M6ResourcePlan:
    assert manifest.roots is not None
    try:
        inputs = load_m6_inputs(resource, manifest.roots)
        rendered = render_template(
            resource,
            root=manifest.roots.source.path,
            m6_inputs=inputs,
        )
        desired = parse_m6_document(rendered.data, format=_format_for(resource))
        baseline = read_baseline(manifest.roots.inputs.path, resource)
    except (M6FormatError, MutationError, LuwuError) as exc:
        return _blocked_plan(
            resource, str(exc), code=getattr(exc, "code", "m6_blocked")
        )
    except Exception as exc:  # noqa: BLE001 - public observation boundary
        return _blocked_plan(resource, type(exc).__name__, code="m6_blocked")

    target = _read_target(resource.target, root=manifest.roots.target.path)
    if target.issue is not None or target.link_target is not None:
        return _blocked_plan(
            resource,
            target.issue or "target is a symlink",
            code="m6_unsafe_target",
            rendered=rendered,
            desired=desired,
            inputs=inputs,
            target=target,
        )
    if target.data is None:
        return M6ResourcePlan(
            resource,
            "missing",
            "create",
            "target is absent and must be created explicitly",
            None,
            tuple(sorted(resource.fields)),
            (),
            desired,
            rendered.data,
            None,
            inputs,
            rendered.source_identity,
            rendered.source_digest,
            None,
            None,
            target.parent_identity,
            _digest(baseline),
        )
    try:
        live = parse_m6_document(target.data, format=_format_for(resource))
        ownership = classify_fields(
            encode_m6_object(desired),
            encode_m6_object(live),
            fields=resource.fields,
            baseline=baseline,
            resource_name=resource.name,
            source_name=resource.source_name,
            target_name=resource.target_name,
        )
    except (M6FormatError, MutationError, LuwuError) as exc:
        return _blocked_plan(
            resource,
            str(exc),
            code=getattr(exc, "code", "m6_blocked"),
            rendered=rendered,
            desired=desired,
            inputs=inputs,
            target=target,
        )
    forward = tuple(
        field.name
        for field in ownership.fields
        if field.decision == "forward_candidate"
    )
    reverse = tuple(
        field.name
        for field in ownership.fields
        if field.decision == "reverse_candidate"
    )
    if ownership.baseline_status == "absent":
        return M6ResourcePlan(
            resource,
            "unbased",
            "review",
            "baseline is absent; accept selected fields explicitly",
            ownership,
            (),
            (),
            desired,
            rendered.data,
            live,
            inputs,
            rendered.source_identity,
            rendered.source_digest,
            target.identity,
            target.digest,
            target.parent_identity,
            _digest(baseline),
        )
    if ownership.undeclared_changed:
        action, reason = "review", "undeclared live content changed"
    elif any(field.decision == "review" for field in ownership.fields):
        action, reason = "review", "field ownership requires review"
    elif forward:
        action, reason = "forward", "declared source-owned fields changed"
    elif reverse:
        action, reason = "reverse_candidate", "declared live-owned fields changed"
    else:
        action, reason = "noop", "declared fields are converged"
    return M6ResourcePlan(
        resource,
        "drifted" if action not in {"noop", "review"} else action,
        action,
        reason,
        ownership,
        forward,
        reverse,
        desired,
        rendered.data,
        live,
        inputs,
        rendered.source_identity,
        rendered.source_digest,
        target.identity,
        target.digest,
        target.parent_identity,
        _digest(baseline),
    )


def _blocked_plan(
    resource: Resource,
    reason: str,
    *,
    code: str,
    rendered: RenderedTemplate | None = None,
    desired: dict[str, object] | None = None,
    inputs: M6InputContext | None = None,
    target: Any | None = None,
) -> M6ResourcePlan:
    return M6ResourcePlan(
        resource,
        "blocked",
        "review",
        f"{reason} [{code}]",
        None,
        (),
        (),
        {} if desired is None else desired,
        b"" if rendered is None else rendered.data,
        None,
        inputs or M6InputContext({}, {}, {}),
        (0, 0) if rendered is None else rendered.source_identity,
        "" if rendered is None else rendered.source_digest,
        None if target is None else target.identity,
        None if target is None else target.digest,
        None if target is None else target.parent_identity,
    )


def _recheck_plan(plan: M6Plan) -> None:
    current = load_manifest(plan.manifest.path)
    if current.content_digest != plan.manifest.content_digest:
        raise M6PilotError("M6 manifest changed; run a new plan", code="m6_stale_plan")
    fresh = build_m6_plan(current).resource
    expected = plan.resource
    if current.roots != plan.manifest.roots:
        raise M6PilotError("M6 roots changed; run a new plan", code="m6_stale_plan")
    if (
        fresh.source_identity != expected.source_identity
        or fresh.source_digest != expected.source_digest
        or fresh.target_identity != expected.target_identity
        or fresh.target_digest != expected.target_digest
        or fresh.target_parent_identity != expected.target_parent_identity
        or fresh.baseline_digest != expected.baseline_digest
        or _input_fingerprint(fresh.input_context)
        != _input_fingerprint(expected.input_context)
    ):
        raise M6PilotError(
            "M6 inputs or target changed; run a new plan", code="m6_stale_plan"
        )


def _write_atomic(
    root: Path,
    path: Path | None,
    data: bytes,
    *,
    expected_identity: tuple[int, int] | None,
    expected_digest: str | None,
) -> None:
    if path is None:
        raise M6PilotError("M6 write path is not declared", code="m6_write_path")
    try:
        parent, name = open_parent_directory(root, path)
    except (OSError, NotImplementedError, ValueError):
        raise M6PilotError("M6 write parent is unsafe", code="m6_unsafe_path") from None
    temp: str | None = None
    locked = False
    replaced = False
    try:
        lock_directory(parent)
        locked = True
        verify_directory_identity(parent, path.parent)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise M6PilotError(
                    "M6 target is not a regular file", code="m6_unsafe_target"
                )
            identity = (info.st_dev, info.st_ino)
            if expected_identity != identity:
                raise M6PilotError("M6 write inputs are stale", code="m6_stale_plan")
            if expected_digest is not None:
                current, _ = read_regular_file_at(parent, name)
                if _digest(current) != expected_digest:
                    raise M6PilotError(
                        "M6 write inputs are stale", code="m6_stale_plan"
                    )
            mode = stat.S_IMODE(info.st_mode)
        except FileNotFoundError:
            if expected_identity is not None or expected_digest is not None:
                raise M6PilotError(
                    "M6 write inputs are stale", code="m6_stale_plan"
                ) from None
            mode = 0o644
        descriptor, temp = create_temporary_file(parent, ".luwu-m6-")
        try:
            os.fchmod(descriptor, mode)
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        temp = None
        replaced = True
        sync_directory(parent)
    except M6PilotError:
        raise
    except (OSError, NotImplementedError, FileChangedError):
        raise M6PilotError(
            "M6 write outcome is unknown" if replaced else "M6 write failed",
            code="m6_write_unknown" if replaced else "m6_write_failed",
        ) from None
    finally:
        if temp is not None:
            try:
                os.unlink(temp, dir_fd=parent)
            except OSError:
                pass
        if locked:
            try:
                unlock_directory(parent)
            except OSError:
                pass
        os.close(parent)


def _read_file(path: Path) -> tuple[bytes, tuple[int, int]]:
    try:
        parent, name = open_parent_directory(path.parent, path)
    except (FileNotFoundError, OSError, NotImplementedError, ValueError):
        raise M6PilotError(
            "M6 file cannot be read safely", code="m6_read_failed"
        ) from None
    try:
        try:
            data, info = read_regular_file_at(parent, name)
        except (FileNotFoundError, OSError, RuntimeError):
            raise M6PilotError(
                "M6 file cannot be read safely", code="m6_read_failed"
            ) from None
    finally:
        os.close(parent)
    return data, (info.st_dev, info.st_ino)


def _target_bytes(manifest: Manifest, item: M6ResourcePlan) -> bytes:
    assert manifest.roots is not None
    data, _ = _read_file(item.resource.target)
    return data


def _selected_values(
    item: M6ResourcePlan, fields: tuple[str, ...]
) -> tuple[dict[str, object], tuple[str, ...]]:
    assert item.live is not None
    updates: dict[str, object] = {}
    deletes: list[str] = []
    for name in fields:
        if name not in item.desired:
            deletes.append(name)
        else:
            updates[name] = item.desired[name]
    return updates, tuple(deletes)


def _selected_values_from_object(
    values: Mapping[str, object], fields: list[str]
) -> tuple[dict[str, object], tuple[str, ...]]:
    updates: dict[str, object] = {}
    deletes: list[str] = []
    for name in fields:
        if name not in values:
            deletes.append(name)
        else:
            updates[name] = values[name]
    return updates, tuple(deletes)


def _format_for(resource: Resource) -> str:
    return resource.comparison


def _format_for_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in {"json", "toml"}:
        raise M6PilotError("M6 format is unsupported", code="m6_format")
    return suffix


def _validate_fields(resource: Resource, fields: tuple[str, ...]) -> None:
    if not fields or len(set(fields)) != len(fields):
        raise M6PilotError("M6 fields must be selected once", code="m6_fields")
    if any(name not in resource.fields for name in fields):
        raise M6PilotError("M6 field is not declared", code="m6_fields")


def _resource_plan(plan: M6Plan, name: str) -> M6ResourcePlan:
    for item in plan.resources:
        if item.resource.name == name:
            return item
    raise M6PilotError("M6 resource is not declared", code="m6_resource")


def _require_m6_manifest(manifest: Manifest) -> None:
    if manifest.version != M6_MANIFEST_VERSION or manifest.roots is None:
        raise M6PilotError("M6 requires manifest version 7", code="m6_manifest_version")
    if len(manifest.resources) != 1:
        raise M6PilotError("M6 pilot requires one resource", code="m6_resource_count")


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _input_fingerprint(context: M6InputContext) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                name,
                item.path,
                item.identity,
                item.digest,
            )
            for name, item in context.values.items()
        )
    )
