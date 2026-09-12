"""Observation, explainable planning, and explicit atomic application."""

from __future__ import annotations

import hashlib
import os
import stat
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from .errors import ApplyError, ManifestError, RenderError
from .filesystem import (
    FileChangedError,
    NotRegularFileError,
    create_temporary_file,
    create_temporary_symlink,
    lock_directory,
    open_parent_directory,
    read_regular_file_at,
    resolve_link_target,
    sync_directory,
    unlock_directory,
    verify_directory_identity,
)
from .manifest import (
    _LOADER_PROVENANCE,
    EXECUTION_MANIFEST_VERSION,
    Manifest,
    Resource,
    load_manifest,
)
from .ownership import OwnershipResult, classify_fields
from .plan_record import PlanRecord, PlanRecordError
from .rendering import read_source, render_template
from .semantic import (
    ComparisonResult,
    ComparisonStatus,
    compare,
    not_compared,
)


class Status(StrEnum):
    """The semantic state of a declared target."""

    IN_SYNC = "in_sync"
    MISSING = "missing"
    FORMATTING = "formatting"
    DRIFTED = "drifted"
    UNBASED = "unbased"
    CONFLICT = "conflict"
    BLOCKED = "blocked"


class Action(StrEnum):
    """The action a plan permits for one resource."""

    NOOP = "noop"
    CREATE = "create"
    REPLACE = "replace"
    REPORT = "report"
    BLOCK = "block"


class ApplyOutcome(StrEnum):
    """The observable result of an explicit apply attempt."""

    NO_CHANGES = "no_changes"
    COMMITTED = "committed"
    COMMITTED_BUT_VERIFICATION_FAILED = "committed_but_verification_failed"
    COMMITTED_STATE_UNKNOWN = "committed_state_unknown"
    VERIFICATION_FAILED = "verification_failed"


_PLAN_PROVENANCE = object()
_PLAN_CAPABILITY_TOKEN = object()
_EXECUTION_RECORD_CONTRACT = "public-source-whole-file-v5"


@dataclass(frozen=True, slots=True)
class _PlanCapability:
    """Immutable manifest identity and capability captured by the planner."""

    manifest_path: Path
    manifest_root: Path
    manifest_digest: str
    manifest_version: int
    token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    """A public explanation plus private bytes used for stale-plan checks."""

    resource: Resource
    status: Status
    action: Action
    reason: str
    desired_bytes: bytes | None = field(repr=False, compare=False)
    desired_link: str | None = field(repr=False, compare=False)
    source_digest: str | None = field(repr=False, compare=False)
    source_path: Path | None = field(repr=False, compare=False)
    live_digest: str | None = field(repr=False, compare=False)
    live_mode: int | None = field(repr=False, compare=False)
    live_link_target: Path | None = field(repr=False, compare=False)
    source_identity: tuple[int, int] | None = field(
        default=None, repr=False, compare=False
    )
    live_identity: tuple[int, int] | None = field(
        default=None, repr=False, compare=False
    )
    target_parent_identity: tuple[int, int] | None = field(
        default=None, repr=False, compare=False
    )
    comparison: ComparisonResult | None = field(default=None, repr=False, compare=False)
    ownership: OwnershipResult | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class Plan:
    """An in-memory plan. It is intentionally not persisted by M1."""

    manifest: Manifest
    observations: tuple[ResourceObservation, ...]
    manifest_version: int | None = field(default=None, init=True)
    _provenance: object | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def changes(self) -> tuple[ResourceObservation, ...]:
        return tuple(
            observation
            for observation in self.observations
            if observation.action in (Action.CREATE, Action.REPLACE)
        )

    @property
    def blocked(self) -> tuple[ResourceObservation, ...]:
        return tuple(
            observation
            for observation in self.observations
            if observation.status is Status.BLOCKED
        )

    @property
    def can_apply(self) -> bool:
        return self.apply_block_reason is None

    @property
    def contract_version(self) -> int:
        capability = _capability_for(self)
        if capability is not None:
            return capability.manifest_version
        return (
            self.manifest_version
            if self.manifest_version is not None
            else self.manifest.version
        )

    @property
    def apply_block_reason(self) -> str | None:
        if self.blocked:
            return "plan_blocked"
        if self.contract_version == EXECUTION_MANIFEST_VERSION:
            return "execution_required"
        if self.contract_version >= 3:
            return "m3_read_only"
        if self.contract_version >= 2:
            return "m2_read_only"
        return None

    def summary(self) -> dict[str, int]:
        summary = {
            "total": len(self.observations),
            "changes": len(self.changes),
            "in_sync": sum(
                observation.status is Status.IN_SYNC
                for observation in self.observations
            ),
            "formatting": sum(
                observation.status is Status.FORMATTING
                for observation in self.observations
            ),
            "blocked": len(self.blocked),
        }
        if self.contract_version >= 2:
            summary["reported"] = sum(
                observation.action is Action.REPORT for observation in self.observations
            )
        if self.contract_version >= 3:
            summary["unbased"] = sum(
                observation.status is Status.UNBASED
                for observation in self.observations
            )
            summary["conflict"] = sum(
                observation.status is Status.CONFLICT
                for observation in self.observations
            )
        return summary


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """The initial plan, changed target labels, and post-write recalculation."""

    initial_plan: Plan
    changed_targets: tuple[str, ...]
    verification_plan: Plan | None
    outcome: ApplyOutcome = ApplyOutcome.COMMITTED
    verification_error: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Metadata-only result of the v5 multi-resource execution boundary."""

    preview: dict[str, object]
    record: PlanRecord | None
    changed_targets: tuple[str, ...] = ()

    @property
    def record_state(self) -> str | None:
        return None if self.record is None else str(self.record.to_dict()["state"])


def _make_plan_capability_registry() -> tuple[
    Callable[[Plan, _PlanCapability], None],
    Callable[[Plan], _PlanCapability | None],
]:
    """Create planner-owned capability operations without a replaceable map."""

    capabilities: dict[int, tuple[weakref.ReferenceType[Plan], _PlanCapability]] = {}

    def register(plan: Plan, capability: _PlanCapability) -> None:
        plan_id = id(plan)

        def remove(reference: weakref.ReferenceType[Plan]) -> None:
            entry = capabilities.get(plan_id)
            if entry is not None and entry[0] is reference:
                del capabilities[plan_id]

        capabilities[plan_id] = (weakref.ref(plan, remove), capability)

    def lookup(plan: Plan) -> _PlanCapability | None:
        entry = capabilities.get(id(plan))
        if entry is None or entry[0]() is not plan:
            return None
        return entry[1]

    return register, lookup


_register_plan_capability, _capability_for = _make_plan_capability_registry()


@dataclass(frozen=True, slots=True)
class _TargetState:
    data: bytes | None = field(repr=False)
    digest: str | None
    mode: int | None
    link_target: Path | None = None
    issue: str | None = None
    identity: tuple[int, int] | None = None
    parent_identity: tuple[int, int] | None = None


def build_plan(manifest: Manifest) -> Plan:
    """Render and inspect the declared resource without writing any path."""

    if manifest.version == 1:
        resource = _require_single_resource(manifest)
    if manifest._provenance is not _LOADER_PROVENANCE:
        raise ManifestError(
            "manifest must be loaded by load_manifest before planning",
            code="invalid_manifest_provenance",
        )
    if manifest.version == 1:
        observations = (_plan_resource(manifest, resource),)
    else:
        observations = tuple(
            _plan_resource(manifest, resource, collect_errors=True)
            for resource in manifest.resources
        )
    plan = Plan(
        manifest=manifest,
        observations=observations,
        manifest_version=manifest.version,
    )
    object.__setattr__(plan, "_provenance", _PLAN_PROVENANCE)
    _register_plan_capability(
        plan,
        _PlanCapability(
            manifest_path=manifest.path,
            manifest_root=manifest.root,
            manifest_digest=manifest.content_digest,
            manifest_version=manifest.version,
            token=_PLAN_CAPABILITY_TOKEN,
        ),
    )
    return plan


def _plan_resource(
    manifest: Manifest,
    resource: Resource,
    *,
    collect_errors: bool = False,
) -> ResourceObservation:
    try:
        if resource.scope == "fields":
            return _plan_fields(manifest, resource)
        return (
            _plan_template(manifest, resource)
            if resource.kind == "template"
            else (
                _plan_symbolic(manifest, resource)
                if resource.kind == "symbolic"
                else _plan_copy(manifest, resource)
            )
        )
    except RenderError as exc:
        if not collect_errors:
            raise
        return _blocked_observation(
            resource,
            reason=(f"resource could not be rendered safely [{exc.code}]: {exc}"),
            comparison=(
                not_compared(
                    resource.comparison,
                    code=exc.code,
                    reason="resource could not be rendered; comparison was not run",
                )
                if manifest.version >= 2
                else None
            ),
        )


def _plan_fields(manifest: Manifest, resource: Resource) -> ResourceObservation:
    """Observe one M3a field-scoped JSON resource without creating a target."""

    rendered = render_template(resource, root=manifest.root)
    desired = rendered.data
    parent_issue = _target_parent_issue(resource.target, root=manifest.root)
    if parent_issue is not None:
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            source_path=rendered.source_path,
            source_identity=rendered.source_identity,
            reason=parent_issue,
            comparison=not_compared(
                resource.comparison,
                code="unsafe_target",
                reason="target boundary is unsafe; comparison was not run",
            ),
        )

    live = _read_target(resource.target, root=manifest.root)
    if live.issue is not None or live.link_target is not None:
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            source_path=rendered.source_path,
            source_identity=rendered.source_identity,
            reason=live.issue or "target is a symlink; refusing to inspect it",
            live_digest=live.digest,
            live_mode=live.mode,
            live_link_target=live.link_target,
            live_identity=live.identity,
            target_parent_identity=live.parent_identity,
            comparison=not_compared(
                resource.comparison,
                code="unsafe_target",
                reason="target boundary is unsafe; comparison was not run",
            ),
        )
    if live.data is None:
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            source_path=rendered.source_path,
            source_identity=rendered.source_identity,
            reason="target is required for field reconciliation",
            target_parent_identity=live.parent_identity,
            comparison=not_compared(
                resource.comparison,
                code="target_missing",
                reason="target does not exist; field comparison was not run",
            ),
        )

    baseline, baseline_issue = _read_baseline(resource, root=manifest.root)
    if baseline_issue is not None:
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            source_path=rendered.source_path,
            source_identity=rendered.source_identity,
            reason=baseline_issue,
            live_digest=live.digest,
            live_mode=live.mode,
            live_identity=live.identity,
            target_parent_identity=live.parent_identity,
            comparison=not_compared(
                resource.comparison,
                code="baseline_invalid",
                reason="baseline could not be read safely; comparison was not run",
            ),
        )

    try:
        ownership = classify_fields(
            desired,
            live.data,
            fields=resource.fields,
            baseline=baseline,
            resource_name=resource.name,
            source_name=resource.source_name,
            target_name=resource.target_name,
        )
    except Exception as exc:  # noqa: BLE001 - classifier has a stable error boundary
        code = getattr(exc, "code", "ownership_invalid_input")
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            source_path=rendered.source_path,
            source_identity=rendered.source_identity,
            reason="field comparison input is unsupported",
            live_digest=live.digest,
            live_mode=live.mode,
            live_identity=live.identity,
            target_parent_identity=live.parent_identity,
            comparison=not_compared(
                resource.comparison,
                code=code,
                reason="field comparison was not run",
            ),
        )

    comparison = compare(desired, live.data, strategy=resource.comparison)
    if comparison.status is ComparisonStatus.UNSUPPORTED:
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            source_path=rendered.source_path,
            source_identity=rendered.source_identity,
            reason="field comparison input is unsupported",
            live_digest=live.digest,
            live_mode=live.mode,
            live_identity=live.identity,
            target_parent_identity=live.parent_identity,
            comparison=comparison,
        )

    status, action, reason = _aggregate_ownership(ownership)
    return ResourceObservation(
        resource=resource,
        status=status,
        action=action,
        reason=reason,
        desired_bytes=desired,
        desired_link=None,
        source_digest=rendered.source_digest,
        source_path=rendered.source_path,
        live_digest=live.digest,
        live_mode=live.mode,
        live_link_target=None,
        source_identity=rendered.source_identity,
        live_identity=live.identity,
        target_parent_identity=live.parent_identity,
        comparison=comparison,
        ownership=ownership,
    )


def _aggregate_ownership(
    ownership: OwnershipResult,
) -> tuple[Status, Action, str]:
    if ownership.baseline_status == "absent":
        return Status.UNBASED, Action.REPORT, "baseline is absent"
    if any(
        field.status == "conflict" or field.decision == "review"
        for field in ownership.fields
    ):
        return Status.CONFLICT, Action.REPORT, "field changes require review"
    if (
        any(
            field.status in {"source_changed", "live_changed", "converged"}
            for field in ownership.fields
        )
        or ownership.undeclared_changed
    ):
        return Status.DRIFTED, Action.REPORT, "field changes are reported"
    return Status.IN_SYNC, Action.NOOP, "declared fields match the baseline"


def _read_baseline(
    resource: Resource,
    *,
    root: Path,
) -> tuple[bytes | None, str | None]:
    if resource.baseline is None:
        return None, None
    try:
        parent_descriptor, name = open_parent_directory(root, resource.baseline)
    except FileNotFoundError:
        return None, "baseline does not exist"
    except (OSError, NotImplementedError, ValueError):
        return None, "baseline cannot be read safely"
    try:
        try:
            data, _ = read_regular_file_at(parent_descriptor, name)
        except FileNotFoundError:
            return None, "baseline does not exist"
        except NotRegularFileError:
            return None, "baseline is not a regular file"
        except FileChangedError:
            return None, "baseline changed during inspection"
        except (OSError, NotImplementedError, RuntimeError):
            return None, "baseline cannot be read safely"
        return data, None
    finally:
        os.close(parent_descriptor)


def _require_single_resource(manifest: Manifest) -> Resource:
    if len(manifest.resources) != 1:
        raise ManifestError(
            "M1 supports exactly one resource",
            code="resource_count",
        )
    return manifest.resources[0]


def _plan_template(manifest: Manifest, resource: Resource) -> ResourceObservation:
    rendered = render_template(resource, root=manifest.root)
    desired = rendered.data
    parent_issue = _target_parent_issue(resource.target, root=manifest.root)
    if parent_issue is not None:
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            source_identity=rendered.source_identity,
            reason=parent_issue,
            comparison=(
                not_compared(
                    resource.comparison,
                    code="unsafe_target",
                    reason="target boundary is unsafe; comparison was not run",
                )
                if manifest.version >= 2
                else None
            ),
        )

    desired_comparison = None
    if resource.comparison == "json":
        desired_comparison = compare(
            desired,
            desired,
            strategy=resource.comparison,
        )
        if desired_comparison.status is ComparisonStatus.UNSUPPORTED:
            return _blocked_observation(
                resource,
                desired_bytes=desired,
                source_digest=rendered.source_digest,
                source_path=rendered.source_path,
                reason=desired_comparison.reason,
                source_identity=rendered.source_identity,
                comparison=desired_comparison,
            )

    live = _read_target(resource.target, root=manifest.root)
    if live.issue is not None or live.link_target is not None:
        reason = live.issue or "target is a symlink; refusing to replace it"
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            source_identity=rendered.source_identity,
            reason=reason,
            live_digest=live.digest,
            live_mode=live.mode,
            live_link_target=live.link_target,
            live_identity=live.identity,
            target_parent_identity=live.parent_identity,
            comparison=(
                not_compared(
                    resource.comparison,
                    code="unsafe_target",
                    reason="target boundary is unsafe; comparison was not run",
                )
                if manifest.version >= 2
                else None
            ),
        )

    if live.data is None:
        return ResourceObservation(
            resource=resource,
            status=Status.MISSING,
            action=Action.CREATE,
            reason="target does not exist",
            desired_bytes=desired,
            desired_link=None,
            source_digest=rendered.source_digest,
            source_path=rendered.source_path,
            live_digest=None,
            live_mode=None,
            live_link_target=None,
            source_identity=rendered.source_identity,
            target_parent_identity=live.parent_identity,
            comparison=(
                not_compared(
                    resource.comparison,
                    code="target_missing",
                    reason="target does not exist; comparison was not run",
                )
                if manifest.version >= 2
                else None
            ),
        )

    comparison = compare(
        desired,
        live.data,
        strategy=resource.comparison,
    )
    if resource.comparison == "json":
        if comparison.status is ComparisonStatus.EXACT:
            status = Status.IN_SYNC
            action = Action.NOOP
            reason = "strict JSON bytes match"
        elif comparison.status is ComparisonStatus.EQUIVALENT_BUT_REFORMATTED:
            status = Status.FORMATTING
            action = Action.NOOP
            reason = comparison.reason
        elif comparison.status is ComparisonStatus.DIFFERENT:
            status = Status.DRIFTED
            action = Action.REPORT
            reason = comparison.reason
        else:
            return _blocked_observation(
                resource,
                desired_bytes=desired,
                source_digest=rendered.source_digest,
                source_path=rendered.source_path,
                reason=comparison.reason,
                live_digest=live.digest,
                live_mode=live.mode,
                source_identity=rendered.source_identity,
                live_identity=live.identity,
                target_parent_identity=live.parent_identity,
                comparison=comparison,
            )
    elif comparison.status is ComparisonStatus.EXACT:
        status = Status.IN_SYNC
        action = Action.NOOP
        reason = "rendered template matches target"
    else:
        status = Status.DRIFTED
        action = Action.REPLACE
        reason = "rendered template differs from target"

    return ResourceObservation(
        resource=resource,
        status=status,
        action=action,
        reason=reason,
        desired_bytes=desired,
        desired_link=None,
        source_digest=rendered.source_digest,
        source_path=rendered.source_path,
        live_digest=live.digest,
        live_mode=live.mode,
        live_link_target=None,
        source_identity=rendered.source_identity,
        live_identity=live.identity,
        target_parent_identity=live.parent_identity,
        comparison=(
            comparison
            if manifest.version >= 2 or resource.comparison != "exact-bytes"
            else None
        ),
    )


def _plan_copy(manifest: Manifest, resource: Resource) -> ResourceObservation:
    source_path, desired, source_identity = read_source(resource, root=manifest.root)
    parent_issue = _target_parent_issue(resource.target, root=manifest.root)
    if parent_issue is not None:
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=_digest(desired),
            source_path=source_path,
            source_identity=source_identity,
            reason=parent_issue,
            comparison=not_compared(
                resource.comparison,
                code="unsafe_target",
                reason="target boundary is unsafe; comparison was not run",
            ),
        )

    live = _read_target(resource.target, root=manifest.root)
    if live.issue is not None or live.link_target is not None:
        reason = live.issue or "target is a symlink; refusing to replace it"
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=_digest(desired),
            source_path=source_path,
            reason=reason,
            live_digest=live.digest,
            live_mode=live.mode,
            live_link_target=live.link_target,
            live_identity=live.identity,
            target_parent_identity=live.parent_identity,
            comparison=not_compared(
                resource.comparison,
                code="unsafe_target",
                reason="target boundary is unsafe; comparison was not run",
            ),
        )

    comparison = None
    if live.data is None:
        status = Status.MISSING
        action = Action.CREATE
        reason = "target does not exist"
    else:
        comparison = compare(desired, live.data, strategy="exact-bytes")
        if comparison.status is ComparisonStatus.EXACT:
            status = Status.IN_SYNC
            action = Action.NOOP
            reason = "source file matches target"
        else:
            status = Status.DRIFTED
            action = Action.REPLACE
            reason = "source file differs from target"

    return ResourceObservation(
        resource=resource,
        status=status,
        action=action,
        reason=reason,
        desired_bytes=desired,
        desired_link=None,
        source_digest=_digest(desired),
        source_path=source_path,
        live_digest=live.digest,
        live_mode=live.mode,
        live_link_target=None,
        source_identity=source_identity,
        live_identity=live.identity,
        target_parent_identity=live.parent_identity,
        comparison=(
            not_compared(
                resource.comparison,
                code="target_missing",
                reason="target does not exist; comparison was not run",
            )
            if manifest.version >= 2 and comparison is None
            else comparison
        ),
    )


def _plan_symbolic(manifest: Manifest, resource: Resource) -> ResourceObservation:
    source_path, source_identity, source_issue = _resolve_symbolic_source(
        resource, root=manifest.root
    )
    desired_link = os.path.relpath(resource.source, resource.target.parent)
    if source_issue is not None:
        return _blocked_observation(
            resource,
            desired_link=desired_link,
            source_path=source_path,
            source_identity=source_identity,
            reason=source_issue,
        )

    parent_issue = _target_parent_issue(resource.target, root=manifest.root)
    if parent_issue is not None:
        return _blocked_observation(
            resource,
            desired_link=desired_link,
            source_path=source_path,
            source_identity=source_identity,
            reason=parent_issue,
        )

    live = _read_target(resource.target, root=manifest.root)
    if live.issue is not None:
        return _blocked_observation(
            resource,
            desired_link=desired_link,
            source_path=source_path,
            reason=live.issue,
            live_digest=live.digest,
            live_mode=live.mode,
            live_link_target=live.link_target,
            live_identity=live.identity,
            target_parent_identity=live.parent_identity,
        )
    if live.link_target is not None:
        if live.link_target == source_path:
            return ResourceObservation(
                resource=resource,
                status=Status.IN_SYNC,
                action=Action.NOOP,
                reason="target is the declared symbolic link",
                desired_bytes=None,
                desired_link=desired_link,
                source_digest=None,
                source_path=source_path,
                live_digest=None,
                live_mode=None,
                live_link_target=live.link_target,
                source_identity=source_identity,
                live_identity=live.identity,
                target_parent_identity=live.parent_identity,
            )
        reason = (
            "target symlink points outside the manifest directory"
            if not _is_within(live.link_target, manifest.root)
            else "target is a symlink to a different path; refusing to replace it"
        )
        return _blocked_observation(
            resource,
            desired_link=desired_link,
            source_path=source_path,
            reason=reason,
            live_link_target=live.link_target,
        )
    if live.data is None:
        return ResourceObservation(
            resource=resource,
            status=Status.MISSING,
            action=Action.CREATE,
            reason="target does not exist",
            desired_bytes=None,
            desired_link=desired_link,
            source_digest=None,
            source_path=source_path,
            live_digest=None,
            live_mode=None,
            live_link_target=None,
            source_identity=source_identity,
            target_parent_identity=live.parent_identity,
        )
    return ResourceObservation(
        resource=resource,
        status=Status.DRIFTED,
        action=Action.REPLACE,
        reason="target is not the declared symbolic link",
        desired_bytes=None,
        desired_link=desired_link,
        source_digest=None,
        source_path=source_path,
        live_digest=live.digest,
        live_mode=live.mode,
        live_link_target=None,
        source_identity=source_identity,
        live_identity=live.identity,
        target_parent_identity=live.parent_identity,
    )


def apply_plan(plan: Plan) -> ApplyResult:
    """Apply a previously calculated plan after a complete stale-state check."""

    _require_single_plan(plan)
    if plan.apply_block_reason == "plan_blocked":
        raise ApplyError(
            "plan contains blocked resources; no files were changed",
            code="plan_blocked",
        )

    _preflight_manifest(plan)
    for observation in plan.observations:
        _preflight_observation(plan, observation)

    changed_targets: list[str] = []
    for observation in plan.changes:
        try:
            _write_observation(plan, observation)
        except ApplyError as exc:
            if not exc.committed:
                raise
            changed_targets.append(exc.target_name or observation.resource.target_name)
            return ApplyResult(
                initial_plan=plan,
                changed_targets=tuple(changed_targets),
                verification_plan=None,
                outcome=(
                    ApplyOutcome.COMMITTED_BUT_VERIFICATION_FAILED
                    if exc.code == "post_apply_verification_failed"
                    else ApplyOutcome.COMMITTED_STATE_UNKNOWN
                ),
                verification_error=exc.code,
            )
        changed_targets.append(observation.resource.target_name)

    try:
        verification_manifest = _load_current_manifest(plan)
        verification_plan = build_plan(verification_manifest)
    except Exception:  # noqa: BLE001 - verification must report committed state
        return ApplyResult(
            initial_plan=plan,
            changed_targets=tuple(changed_targets),
            verification_plan=None,
            outcome=(
                ApplyOutcome.COMMITTED_BUT_VERIFICATION_FAILED
                if changed_targets
                else ApplyOutcome.VERIFICATION_FAILED
            ),
            verification_error="post_apply_verification_failed",
        )
    if verification_plan.changes or verification_plan.blocked:
        return ApplyResult(
            initial_plan=plan,
            changed_targets=tuple(changed_targets),
            verification_plan=verification_plan,
            outcome=(
                ApplyOutcome.COMMITTED_BUT_VERIFICATION_FAILED
                if changed_targets
                else ApplyOutcome.VERIFICATION_FAILED
            ),
            verification_error="post_apply_verification_failed",
        )
    outcome = ApplyOutcome.COMMITTED if changed_targets else ApplyOutcome.NO_CHANGES
    return ApplyResult(
        initial_plan=plan,
        changed_targets=tuple(changed_targets),
        verification_plan=verification_plan,
        outcome=outcome,
    )


def execute_execution_plan(
    plan: Plan, record_path: Path, *, confirm: bool
) -> ExecutionResult:
    """Execute a loader/planner-issued v5 plan with a durable journal.

    Preview is completely side-effect free. Confirmed execution preflights all
    resources before creating the journal and processes them in stable order.
    A failure is never rolled back; the journal records the recovery boundary.
    """

    _require_execution_plan(plan)
    preview = _execution_preview(plan)
    if not confirm:
        return ExecutionResult(preview=preview, record=None)

    _preflight_execution_manifest(plan)
    for observation in plan.observations:
        _preflight_observation(plan, observation)
    _check_execution_record_path(plan, record_path)

    record = _execution_record(plan)
    _write_execution_record(record, record_path)
    previous = record
    record = record.transition("preflighted")
    _write_execution_record(record, record_path, expected=previous)
    for ordinal in range(len(plan.observations)):
        previous = record
        record = record.transition_path(ordinal, "preflighted")
        _write_execution_record(record, record_path, expected=previous)
    previous = record
    record = record.transition("commit_intent")
    _write_execution_record(record, record_path, expected=previous)

    changed_targets: list[str] = []
    for ordinal, observation in enumerate(plan.observations):
        if observation.action is Action.NOOP:
            previous = record
            record = record.transition_path(ordinal, "unchanged")
            _write_execution_record(record, record_path, expected=previous)
            continue
        previous = record
        record = record.transition_path(ordinal, "commit_intent")
        _write_execution_record(record, record_path, expected=previous)
        try:
            _write_observation(plan, observation, execution=True)
        except ApplyError as exc:
            recovery_required = exc.committed or any(
                resource["ordinal"] < ordinal and resource["state"] == "committed"
                for resource in record.to_dict()["resources"]
            )
            _mark_execution_failure(
                record, plan, ordinal, record_path, committed=recovery_required
            )
            raise ApplyError(
                f"execution stopped at resource {observation.resource.name!r}; "
                "rollback=never",
                code=("recovery_required" if recovery_required else "execution_failed"),
                committed=recovery_required,
                target_name=observation.resource.target_name,
            ) from exc
        changed_targets.append(observation.resource.target_name)
        try:
            postcondition = _execution_condition(
                observation.resource.target, plan.manifest.root
            )
            if not _condition_matches_expected(
                postcondition,
                _execution_postcondition(
                    observation,
                    _execution_condition(
                        observation.resource.target, plan.manifest.root
                    ),
                ),
            ):
                raise ApplyError(
                    f"postcondition for resource {observation.resource.name!r} "
                    "could not be confirmed",
                    code="durability_unconfirmed",
                    committed=True,
                    target_name=observation.resource.target_name,
                )
            committed_record = record.update_path_condition(
                ordinal,
                observation.resource.target_name,
                postcondition=postcondition,
            ).transition_path(ordinal, "committed")
            _write_execution_record(committed_record, record_path, expected=record)
        except ApplyError as exc:
            recovery_required = True
            _mark_execution_failure(
                record, plan, ordinal, record_path, committed=recovery_required
            )
            raise ApplyError(
                f"execution stopped at resource {observation.resource.name!r}; "
                "rollback=never",
                code="recovery_required",
                committed=True,
                target_name=observation.resource.target_name,
            ) from exc
        record = committed_record

    previous = record
    record = record.transition("committed")
    _write_execution_record(record, record_path, expected=previous)
    return ExecutionResult(
        preview=preview, record=record, changed_targets=tuple(changed_targets)
    )


def inspect_execution_record(record_path: Path) -> dict[str, object]:
    """Read a journal for recovery decisions without replaying or writing."""

    record = PlanRecord.read(record_path).to_dict()
    manifest = record["manifest"]
    if (
        not isinstance(manifest, dict)
        or manifest["version"] != EXECUTION_MANIFEST_VERSION
        or record["execution_contract"] != _EXECUTION_RECORD_CONTRACT
    ):
        raise PlanRecordError(
            "record is not a version-5 execution journal",
            code="execution_record_contract",
        )
    _validate_execution_record(record)
    return record


def _validate_execution_record(record: dict[str, object]) -> None:
    """Validate the execution-specific meaning of the closed record schema."""

    if record["mutation_contract"] != "atomic-single-file":
        raise PlanRecordError(
            "execution journal mutation contract is unsupported",
            code="execution_record_contract",
        )
    manifest = record["manifest"]
    if not isinstance(manifest, dict):
        raise PlanRecordError(
            "execution journal manifest is invalid", code="execution_record_contract"
        )
    root = Path(str(manifest["root"]))
    manifest_path = Path(str(manifest["path"]))
    if (
        not root.is_absolute()
        or not manifest_path.is_absolute()
        or manifest_path.parent != root
    ):
        raise PlanRecordError(
            "execution journal manifest paths are invalid",
            code="execution_record_contract",
        )
    resources = record["resources"]
    if not isinstance(resources, list):
        raise PlanRecordError(
            "execution journal resources are invalid",
            code="execution_record_contract",
        )
    for resource in resources:
        if not isinstance(resource, dict) or resource["operation"] not in {
            Action.NOOP.value,
            Action.CREATE.value,
            Action.REPLACE.value,
        }:
            raise PlanRecordError(
                "execution journal resource operation is invalid",
                code="execution_record_contract",
            )
        paths = resource["paths"]
        if not isinstance(paths, list) or len(paths) != 2:
            raise PlanRecordError(
                "execution journal resource paths are invalid",
                code="execution_record_contract",
            )
        roles = {path["role"] for path in paths if isinstance(path, dict)}
        if roles != {"source", "target"}:
            raise PlanRecordError(
                "execution journal path roles are invalid",
                code="execution_record_contract",
            )
        for path in paths:
            if not isinstance(path, dict):
                raise PlanRecordError(
                    "execution journal path is invalid",
                    code="execution_record_contract",
                )
            expected_operation = (
                "observe" if path["role"] == "source" else "atomic_replace"
            )
            if path["operation"] != expected_operation:
                raise PlanRecordError(
                    "execution journal path operation is invalid",
                    code="execution_record_contract",
                )


def reobserve_execution_record(record_path: Path) -> dict[str, object]:
    """Re-observe a v5 journal without replaying inputs or writing state."""

    record = inspect_execution_record(record_path)
    manifest_info = record["manifest"]
    if not isinstance(manifest_info, dict):
        raise PlanRecordError(
            "execution journal manifest is invalid", code="execution_record_contract"
        )
    manifest_path = Path(str(manifest_info["path"]))
    try:
        manifest_path_is_stable = manifest_path.resolve(strict=True) == manifest_path
    except (OSError, RuntimeError):
        manifest_path_is_stable = False
    if not manifest_path_is_stable:
        return _reobserve_blocked(record, "manifest_unavailable")
    try:
        manifest = load_manifest(manifest_path)
        current_digest = _digest(manifest_path.read_bytes())
    except (ManifestError, OSError, RuntimeError):
        return _reobserve_blocked(record, "manifest_unavailable")
    expected_digest = str(manifest_info["digest"])[len("sha256:") :]
    if (
        manifest.version != EXECUTION_MANIFEST_VERSION
        or current_digest != expected_digest
        or str(manifest.root) != str(manifest_info["root"])
    ):
        return _reobserve_blocked(record, "manifest_changed")

    try:
        plan = build_plan(manifest)
    except (ManifestError, RenderError, OSError, RuntimeError):
        return _reobserve_blocked(record, "observation_failed")
    observations = {item.resource.name: item for item in plan.observations}
    persisted_resources = record["resources"]
    if not isinstance(persisted_resources, list) or len(persisted_resources) != len(
        plan.observations
    ):
        return _reobserve_blocked(record, "record_manifest_mismatch")
    for ordinal, observation in enumerate(plan.observations):
        persisted = persisted_resources[ordinal]
        if not isinstance(persisted, dict):
            return _reobserve_blocked(record, "record_manifest_mismatch")
        paths = {
            path["role"]: path["path"]
            for path in persisted["paths"]
            if isinstance(path, dict)
        }
        if (
            persisted["ordinal"] != ordinal
            or persisted["name"] != observation.resource.name
            or paths
            != {
                "source": observation.resource.source_name,
                "target": observation.resource.target_name,
            }
        ):
            return _reobserve_blocked(record, "record_manifest_mismatch")
    resources: list[dict[str, object]] = []
    for persisted in persisted_resources:
        if not isinstance(persisted, dict):
            continue
        observation = observations.get(str(persisted["name"]))
        path_results: list[dict[str, object]] = []
        for persisted_path in persisted["paths"]:
            if not isinstance(persisted_path, dict):
                continue
            relative = Path(str(persisted_path["path"]))
            current = _execution_condition(manifest.root / relative, manifest.root)
            expected = persisted_path["postcondition"]
            matches = _condition_matches_expected(current, expected)
            path_results.append(
                {
                    "role": persisted_path["role"],
                    "path": persisted_path["path"],
                    "matches_postcondition": matches,
                    "current_type": current["type"],
                }
            )
        resource_state = str(persisted["state"])
        all_match = all(bool(item["matches_postcondition"]) for item in path_results)
        if resource_state in {"committed", "unchanged"} and all_match:
            reobserved = "confirmed"
        elif resource_state in {"unknown", "recovery_required"} and all_match:
            reobserved = "matches_postcondition"
        elif resource_state == "not-attempted":
            reobserved = "not-attempted"
        else:
            reobserved = "changed_or_unknown"
        resources.append(
            {
                "ordinal": persisted["ordinal"],
                "name": persisted["name"],
                "record_state": resource_state,
                "reobserved_state": reobserved,
                "plan_status": (
                    observation.status.value if observation is not None else "missing"
                ),
                "paths": path_results,
            }
        )
    overall = "confirmed"
    if any(
        item["reobserved_state"] in {"changed_or_unknown", "matches_postcondition"}
        for item in resources
    ):
        overall = "recovery_required"
    return {
        "schema_version": EXECUTION_MANIFEST_VERSION,
        "command": "recover",
        "mode": "reobserve-only",
        "plan_id": record["plan_id"],
        "record_state": record["state"],
        "outcome": overall,
        "resources": resources,
    }


def _reobserve_blocked(record: dict[str, object], reason: str) -> dict[str, object]:
    return {
        "schema_version": EXECUTION_MANIFEST_VERSION,
        "command": "recover",
        "mode": "reobserve-only",
        "plan_id": record["plan_id"],
        "record_state": record["state"],
        "outcome": "recovery_required",
        "reason": reason,
        "resources": [],
    }


def _require_execution_plan(plan: Plan) -> None:
    capability = _capability_for(plan)
    if (
        capability is None
        or capability.token is not _PLAN_CAPABILITY_TOKEN
        or plan._provenance is not _PLAN_PROVENANCE
        or plan.manifest._provenance is not _LOADER_PROVENANCE
    ):
        raise ApplyError("plan lacks a valid execution capability", code="invalid_plan")
    if capability.manifest_version != 5 or plan.manifest.execution_capability != (
        "public-source-whole-file"
    ):
        raise ApplyError("plan is not a version-5 execution plan", code="invalid_plan")
    if (
        plan.manifest.path != capability.manifest_path
        or plan.manifest.root != capability.manifest_root
        or plan.manifest.content_digest != capability.manifest_digest
        or plan.manifest.version != capability.manifest_version
    ):
        raise ApplyError("plan manifest identity is invalid", code="invalid_plan")
    if (
        not plan.observations
        or tuple(observation.resource for observation in plan.observations)
        != plan.manifest.resources
    ):
        raise ApplyError(
            "plan observations must match manifest resources", code="invalid_plan"
        )
    for observation in plan.observations:
        if observation.resource.capability != "public-source-whole-file":
            raise ApplyError("resource capability is invalid", code="invalid_plan")
        if observation.status is Status.BLOCKED or observation.action is Action.BLOCK:
            raise ApplyError("plan contains blocked resources", code="plan_blocked")
        if observation.action not in (Action.NOOP, Action.CREATE, Action.REPLACE):
            raise ApplyError(
                "plan contains a non-executable action", code="invalid_plan"
            )


def _execution_preview(plan: Plan) -> dict[str, object]:
    return {
        "schema_version": 5,
        "execution_capability": "public-source-whole-file",
        "rollback": "never",
        "resources": [
            {
                "ordinal": ordinal,
                "name": observation.resource.name,
                "source": observation.resource.source_name,
                "target": observation.resource.target_name,
                "status": observation.status.value,
                "action": observation.action.value,
                "reason": observation.reason,
            }
            for ordinal, observation in enumerate(plan.observations)
        ],
    }


def _check_execution_record_path(plan: Plan, record_path: Path) -> None:
    record_candidates = {_absolute_path(record_path)}
    try:
        record_candidates.add(record_path.expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        pass
    declared = [plan.manifest.path]
    declared.extend(
        path
        for observation in plan.observations
        for path in (observation.resource.source, observation.resource.target)
    )
    for candidate in declared:
        candidate_paths = {_absolute_path(candidate)}
        try:
            candidate_paths.add(candidate.resolve(strict=False))
        except (OSError, RuntimeError):
            pass
        if any(
            _paths_overlap(record_candidate, declared_candidate)
            for record_candidate in record_candidates
            for declared_candidate in candidate_paths
        ):
            raise ApplyError(
                "execution journal path overlaps a declared path; no files were changed",
                code="record_path_conflict",
            )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _execution_condition(path: Path, root: Path) -> dict[str, int | str]:
    try:
        path.relative_to(root)
        parent, name = open_parent_directory(root, path)
    except FileNotFoundError:
        return {"type": "missing", "mode": 0, "size": 0, "mtime_ns": 0, "file_id": 0}
    except (NotImplementedError, OSError, ValueError):
        return {"type": "unsafe", "mode": 0, "size": 0, "mtime_ns": 0, "file_id": 0}
    try:
        verify_directory_identity(parent, path.parent)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return {
                "type": "missing",
                "mode": 0,
                "size": 0,
                "mtime_ns": 0,
                "file_id": 0,
            }
        verify_directory_identity(parent, path.parent)
    except (FileChangedError, NotImplementedError, OSError, ValueError):
        return {"type": "unsafe", "mode": 0, "size": 0, "mtime_ns": 0, "file_id": 0}
    finally:
        os.close(parent)
    file_type = (
        "symlink"
        if stat.S_ISLNK(info.st_mode)
        else ("regular" if stat.S_ISREG(info.st_mode) else "other")
    )
    return {
        "type": file_type,
        "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "file_id": info.st_ino,
    }


def _execution_postcondition(
    observation: ResourceObservation, current: dict[str, int | str]
) -> dict[str, int | str]:
    if observation.action is Action.NOOP:
        return current
    if observation.resource.kind == "symbolic":
        return {
            "type": "symlink",
            "mode": 0o777,
            "size": len((observation.desired_link or "").encode()),
            "mtime_ns": 0,
            "file_id": 0,
        }
    mode = int(current["mode"]) if current["type"] == "regular" else 0o644
    return {
        "type": "regular",
        "mode": mode,
        "size": len(observation.desired_bytes or b""),
        "mtime_ns": 0,
        "file_id": 0,
    }


def _condition_matches_expected(
    current: dict[str, int | str], expected: object
) -> bool:
    if not isinstance(expected, dict):
        return False
    if current.get("type") != expected.get("type"):
        return False
    if current.get("mode") != expected.get("mode"):
        return False
    if current.get("size") != expected.get("size"):
        return False
    for key in ("mtime_ns", "file_id"):
        expected_value = expected.get(key)
        if expected_value not in (0, current.get(key)):
            return False
    return True


def _execution_record(plan: Plan) -> PlanRecord:
    resources = []
    for ordinal, observation in enumerate(plan.observations):
        resource = observation.resource
        source_condition = _execution_condition(resource.source, plan.manifest.root)
        target_condition = _execution_condition(resource.target, plan.manifest.root)
        resources.append(
            {
                "ordinal": ordinal,
                "name": resource.name,
                "operation": observation.action.value,
                "paths": [
                    _execution_path(
                        "source",
                        resource.source_name,
                        source_condition,
                        source_condition,
                    ),
                    _execution_path(
                        "target",
                        resource.target_name,
                        target_condition,
                        _execution_postcondition(observation, target_condition),
                    ),
                ],
                "state": "planned",
            }
        )
    return PlanRecord.create(
        plan_id=str(uuid4()),
        execution_contract=_EXECUTION_RECORD_CONTRACT,
        mutation_contract="atomic-single-file",
        manifest={
            "path": str(plan.manifest.path),
            "root": str(plan.manifest.root),
            "version": 5,
            "digest": f"sha256:{plan.manifest.content_digest}",
        },
        resources=resources,
    )


def _execution_path(
    role: str, path: str, pre: dict[str, int | str], post: dict[str, int | str]
) -> dict[str, object]:
    return {
        "role": role,
        "path": path,
        "operation": "observe" if role == "source" else "atomic_replace",
        "precondition": pre,
        "postcondition": post,
        "state": "planned",
    }


def _write_execution_record(
    record: PlanRecord,
    record_path: Path,
    *,
    expected: PlanRecord | None = None,
) -> None:
    try:
        record.write(record_path, expected=expected)
    except PlanRecordError as exc:
        raise ApplyError(
            "execution journal durability is unknown; recovery is required",
            code="recovery_required",
            committed=exc.committed,
        ) from exc


def _mark_execution_failure(
    record: PlanRecord, plan: Plan, ordinal: int, record_path: Path, *, committed: bool
) -> None:
    try:
        failed = record.transition_path(ordinal, "unknown")
        previous = record
        _write_execution_record(failed, record_path, expected=previous)
        for later in range(ordinal + 1, len(plan.observations)):
            previous = failed
            failed = failed.transition_path(later, "not-attempted")
            _write_execution_record(failed, record_path, expected=previous)
        previous = failed
        failed = failed.transition("recovery_required" if committed else "unknown")
        _write_execution_record(failed, record_path, expected=previous)
    except (ApplyError, PlanRecordError) as exc:
        raise ApplyError(
            "execution journal durability is unknown; recovery is required",
            code="recovery_required",
            committed=committed,
        ) from exc


def _preflight_execution_manifest(plan: Plan) -> None:
    _preflight_manifest(plan, execution=True)


def _require_single_plan(plan: Plan) -> None:
    if plan.manifest.version == 1 and (
        len(plan.manifest.resources) != 1 or len(plan.observations) != 1
    ):
        raise ApplyError(
            "M1 supports exactly one resource; no files were changed",
            code="resource_count",
        )
    capability = _capability_for(plan)
    if capability is None or capability.token is not _PLAN_CAPABILITY_TOKEN:
        raise ApplyError(
            "plan lacks a valid manifest capability; no files were changed",
            code="invalid_plan",
        )
    if capability.manifest_version == 1 and (
        len(plan.manifest.resources) != 1 or len(plan.observations) != 1
    ):
        raise ApplyError(
            "M1 supports exactly one resource; no files were changed",
            code="resource_count",
        )
    if (
        plan.manifest._provenance is not _LOADER_PROVENANCE
        or plan._provenance is not _PLAN_PROVENANCE
    ):
        raise ApplyError(
            "plan must be produced by build_plan; no files were changed",
            code="invalid_plan",
        )
    if capability.manifest_version >= 2:
        if capability.manifest_version == EXECUTION_MANIFEST_VERSION:
            raise ApplyError(
                "version 5 plans require the execution API; no files were changed",
                code="execution_required",
            )
        if plan.blocked:
            raise ApplyError(
                "plan contains blocked resources; no files were changed",
                code="plan_blocked",
            )
        if capability.manifest_version >= 3:
            raise ApplyError(
                "manifest version 3 plans are read-only in M3a; no files were changed",
                code="m3_read_only",
            )
        raise ApplyError(
            "manifest version 2 plans are read-only in M2; no files were changed",
            code="m2_read_only",
        )
    if (
        plan.manifest.path != capability.manifest_path
        or plan.manifest.root != capability.manifest_root
        or plan.manifest.content_digest != capability.manifest_digest
        or plan.manifest.version != capability.manifest_version
    ):
        raise ApplyError(
            "plan manifest version changed; no files were changed",
            code="invalid_plan",
        )


def plan_to_dict(plan: Plan, *, command: str) -> dict[str, object]:
    """Serialize only metadata and explanations; never rendered content."""

    payload: dict[str, object] = {
        "schema_version": plan.contract_version,
        "command": command,
        "manifest": str(plan.manifest.path),
        "resources": [
            {
                "name": observation.resource.name,
                "kind": observation.resource.kind,
                "source": observation.resource.source_name,
                "target": observation.resource.target_name,
                "owner": observation.resource.owner,
                "scope": observation.resource.scope,
                "status": observation.status.value,
                "action": observation.action.value,
                "reason": observation.reason,
                "impact": _impact_to_dict(
                    observation,
                    read_only=2 <= plan.contract_version < EXECUTION_MANIFEST_VERSION,
                ),
                **(
                    {"comparison": observation.comparison.to_dict()}
                    if observation.comparison is not None
                    else {}
                ),
                **(
                    {"comparison_strategy": observation.resource.comparison}
                    if 2 <= plan.contract_version < EXECUTION_MANIFEST_VERSION
                    else {}
                ),
                **(
                    {"baseline": observation.resource.baseline_name}
                    if 3 <= plan.contract_version < EXECUTION_MANIFEST_VERSION
                    else {}
                ),
                **(
                    {"ownership": observation.ownership.to_dict()}
                    if observation.ownership is not None
                    else {}
                ),
            }
            for observation in plan.observations
        ],
        "summary": plan.summary(),
    }
    if plan.contract_version >= 2:
        payload.update(
            {
                "manifest_version": plan.contract_version,
                "applyable": plan.can_apply,
                "apply_block_reason": plan.apply_block_reason,
            }
        )
    return payload


def _blocked_observation(
    resource: Resource,
    *,
    desired_bytes: bytes | None = None,
    desired_link: str | None = None,
    source_digest: str | None = None,
    source_path: Path | None = None,
    reason: str,
    live_digest: str | None = None,
    live_mode: int | None = None,
    live_link_target: Path | None = None,
    source_identity: tuple[int, int] | None = None,
    live_identity: tuple[int, int] | None = None,
    target_parent_identity: tuple[int, int] | None = None,
    comparison: ComparisonResult | None = None,
    ownership: OwnershipResult | None = None,
) -> ResourceObservation:
    return ResourceObservation(
        resource=resource,
        status=Status.BLOCKED,
        action=Action.BLOCK,
        reason=reason,
        desired_bytes=desired_bytes,
        desired_link=desired_link,
        source_digest=source_digest,
        source_path=source_path,
        live_digest=live_digest,
        live_mode=live_mode,
        live_link_target=live_link_target,
        source_identity=source_identity,
        live_identity=live_identity,
        target_parent_identity=target_parent_identity,
        comparison=comparison,
        ownership=ownership,
    )


def _impact_to_dict(
    observation: ResourceObservation,
    *,
    read_only: bool = False,
) -> dict[str, object]:
    target = observation.resource.target_name
    impact: dict[str, object] = {
        "writes": [target]
        if observation.action in (Action.CREATE, Action.REPLACE) and not read_only
        else [],
        "overwrites": [target]
        if observation.action is Action.REPLACE and not read_only
        else [],
        "scope": observation.resource.scope,
        "undeclared": (
            "content outside declared fields is only reported as a change signal"
            if observation.resource.scope == "fields"
            else "content outside the declared target is not examined"
        ),
    }
    if read_only and observation.action in (
        Action.CREATE,
        Action.REPLACE,
    ):
        impact["deferred"] = [target]
    return impact


def _resolve_symbolic_source(
    resource: Resource,
    *,
    root: Path,
) -> tuple[Path | None, tuple[int, int] | None, str | None]:
    try:
        source_path = resource.source.resolve(strict=True)
        source_path.relative_to(root)
    except FileNotFoundError:
        return None, None, "symbolic source does not exist"
    except (OSError, RuntimeError, ValueError):
        return None, None, "symbolic source must stay inside the manifest directory"
    try:
        info = source_path.lstat()
    except OSError:
        return None, None, "symbolic source cannot be inspected"
    if not stat.S_ISREG(info.st_mode):
        return None, None, "symbolic source is not a regular file"
    return source_path, _identity(info), None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _target_parent_issue(target: Path, *, root: Path) -> str | None:
    try:
        parts = target.parent.relative_to(root).parts
    except ValueError:
        return "target is outside the manifest directory"

    current = root
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return "target parent does not exist"
        except OSError:
            return "target parent cannot be inspected"
        if stat.S_ISLNK(info.st_mode):
            return "target parent contains a symlink"
        if not stat.S_ISDIR(info.st_mode):
            return "target parent is not a directory"
    return None


def _read_target(target: Path, *, root: Path) -> _TargetState:
    try:
        parent_descriptor, name = open_parent_directory(root, target)
    except FileNotFoundError:
        return _TargetState(
            data=None,
            digest=None,
            mode=None,
            issue="target parent changed during inspection",
        )
    except (OSError, NotImplementedError, ValueError):
        return _TargetState(
            data=None,
            digest=None,
            mode=None,
            issue="target cannot be inspected",
        )

    try:
        return _read_target_at(
            target,
            parent_descriptor=parent_descriptor,
            name=name,
        )
    finally:
        os.close(parent_descriptor)


def _read_target_at(
    target: Path,
    *,
    parent_descriptor: int,
    name: str,
) -> _TargetState:
    try:
        parent_identity = _identity(os.fstat(parent_descriptor))
        try:
            info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return _TargetState(
                data=None,
                digest=None,
                mode=None,
                parent_identity=parent_identity,
            )

        if stat.S_ISLNK(info.st_mode):
            link_text = os.readlink(name, dir_fd=parent_descriptor)
            link_target = resolve_link_target(target, link_text)
            return _TargetState(
                data=None,
                digest=None,
                mode=None,
                link_target=link_target,
                identity=_identity(info),
                parent_identity=parent_identity,
            )
        if not stat.S_ISREG(info.st_mode):
            return _TargetState(
                data=None,
                digest=None,
                mode=None,
                issue="target is not a regular file",
                identity=_identity(info),
                parent_identity=parent_identity,
            )

        data, current = read_regular_file_at(parent_descriptor, name)
        return _TargetState(
            data=data,
            digest=_digest(data),
            mode=stat.S_IMODE(current.st_mode),
            identity=_identity(current),
            parent_identity=parent_identity,
        )
    except FileNotFoundError:
        return _TargetState(
            data=None,
            digest=None,
            mode=None,
            issue="target changed during inspection",
        )
    except (NotImplementedError, RuntimeError):
        return _TargetState(
            data=None,
            digest=None,
            mode=None,
            issue="target symlink cannot be inspected",
        )
    except NotRegularFileError:
        return _TargetState(
            data=None,
            digest=None,
            mode=None,
            issue="target is not a regular file",
        )
    except FileChangedError:
        return _TargetState(
            data=None,
            digest=None,
            mode=None,
            issue="target changed during inspection",
        )
    except OSError:
        return _TargetState(
            data=None,
            digest=None,
            mode=None,
            issue="target cannot be read",
        )


def _preflight_observation(plan: Plan, observation: ResourceObservation) -> None:
    _check_target_parent(plan, observation, phase="after planning")
    _preflight_source(plan, observation)
    _check_target_state(plan, observation, phase="after planning")


def _check_target_parent(
    plan: Plan,
    observation: ResourceObservation,
    *,
    phase: str,
) -> None:
    parent_issue = _target_parent_issue(
        observation.resource.target,
        root=plan.manifest.root,
    )
    if parent_issue is not None:
        raise ApplyError(
            f"cannot apply {observation.resource.name!r}: target parent {phase}: "
            f"{parent_issue}",
            code="unsafe_target",
        )


def _preflight_source(plan: Plan, observation: ResourceObservation) -> None:
    resource = observation.resource
    if resource.kind == "template":
        try:
            current_rendered = render_template(resource, root=plan.manifest.root)
        except RenderError:
            raise ApplyError(
                f"source for resource {resource.name!r} changed after planning; "
                "run plan again",
                code="stale_plan",
            ) from None
        if (
            current_rendered.source_digest != observation.source_digest
            or current_rendered.data != observation.desired_bytes
            or current_rendered.source_path != observation.source_path
            or current_rendered.source_identity != observation.source_identity
        ):
            raise ApplyError(
                f"source or inputs for resource {resource.name!r} changed after planning; "
                "run plan again",
                code="stale_plan",
            )
        return

    source_path, source_identity, source_issue = _resolve_symbolic_source(
        resource,
        root=plan.manifest.root,
    )
    if (
        source_issue is not None
        or source_path != observation.source_path
        or source_identity != observation.source_identity
    ):
        raise ApplyError(
            f"source for resource {resource.name!r} changed after planning; "
            "run plan again",
            code="stale_plan",
        )


def _preflight_manifest(plan: Plan, *, execution: bool = False) -> None:
    capability = _capability_for(plan)
    if capability is None or capability.token is not _PLAN_CAPABILITY_TOKEN:
        raise ApplyError(
            "plan lacks a valid manifest capability; no files were changed",
            code="invalid_plan",
        )
    try:
        if capability.manifest_path.resolve(strict=True) != capability.manifest_path:
            raise OSError("manifest path is no longer stable")
        current_digest = _digest(capability.manifest_path.read_bytes())
    except (OSError, RuntimeError):
        raise ApplyError(
            "manifest changed or became unreadable after planning; run plan again",
            code="stale_plan",
        ) from None
    if current_digest != capability.manifest_digest:
        raise ApplyError(
            "manifest changed after planning; run plan again",
            code="stale_plan",
        )
    try:
        current_manifest = load_manifest(capability.manifest_path)
    except ManifestError:
        raise ApplyError(
            "manifest changed or became unreadable after planning; run plan again",
            code="stale_plan",
        ) from None
    if execution and current_manifest.version != 5:
        raise ApplyError(
            "manifest is no longer a version-5 execution manifest; no files were changed",
            code="stale_plan",
        )
    if not execution and current_manifest.version >= 3:
        raise ApplyError(
            "manifest version 3 plans are read-only in M3a; no files were changed",
            code="m3_read_only",
        )
    if not execution and current_manifest.version >= 2:
        raise ApplyError(
            "manifest version 2 plans are read-only in M2; no files were changed",
            code="m2_read_only",
        )
    if current_manifest.version != capability.manifest_version:
        raise ApplyError(
            "manifest version changed after planning; run plan again",
            code="stale_plan",
        )
    if (
        plan.manifest.path != current_manifest.path
        or plan.manifest.root != current_manifest.root
        or plan.manifest.content_digest != current_manifest.content_digest
        or plan.manifest.version != current_manifest.version
        or plan.manifest.resources != current_manifest.resources
        or tuple(observation.resource for observation in plan.observations)
        != current_manifest.resources
    ):
        raise ApplyError(
            "plan does not match its manifest; no files were changed",
            code="invalid_plan",
        )


def _load_current_manifest(plan: Plan) -> Manifest:
    """Reload the manifest so post-apply verification uses current inputs."""

    capability = _capability_for(plan)
    if capability is None or capability.token is not _PLAN_CAPABILITY_TOKEN:
        raise ApplyError(
            "plan lacks a valid manifest capability",
            code="post_apply_verification_failed",
        )
    current = load_manifest(capability.manifest_path)
    if (
        current.path != capability.manifest_path
        or current.root != capability.manifest_root
        or current.content_digest != capability.manifest_digest
        or current.version != capability.manifest_version
    ):
        raise ApplyError(
            "manifest changed during apply; run plan again",
            code="post_apply_verification_failed",
        )
    return current


def _check_target_state(
    plan: Plan,
    observation: ResourceObservation,
    *,
    phase: str,
    parent_descriptor: int | None = None,
) -> None:
    current = (
        _read_target_at(
            observation.resource.target,
            parent_descriptor=parent_descriptor,
            name=observation.resource.target.name,
        )
        if parent_descriptor is not None
        else _read_target(observation.resource.target, root=plan.manifest.root)
    )
    if current.issue is not None:
        raise ApplyError(
            f"cannot apply {observation.resource.name!r}: {current.issue}",
            code="unsafe_target",
        )

    if observation.resource.kind == "template" and current.link_target is not None:
        raise ApplyError(
            f"cannot apply {observation.resource.name!r}: target is a symlink; "
            "refusing to replace it",
            code="unsafe_target",
        )
    if observation.resource.kind == "symbolic":
        if observation.live_link_target is not None:
            if current.link_target != observation.live_link_target:
                raise ApplyError(
                    f"target for resource {observation.resource.name!r} changed {phase}; "
                    "run plan again",
                    code="stale_plan",
                )
        elif current.link_target is not None:
            raise ApplyError(
                f"cannot apply {observation.resource.name!r}: target became a symlink; "
                "run plan again",
                code="unsafe_target",
            )

    if (
        current.parent_identity != observation.target_parent_identity
        or current.identity != observation.live_identity
        or current.digest != observation.live_digest
        or current.mode != observation.live_mode
    ):
        raise ApplyError(
            f"target for resource {observation.resource.name!r} changed {phase}; "
            "run plan again",
            code="stale_plan",
        )


def _write_observation(
    plan: Plan, observation: ResourceObservation, *, execution: bool = False
) -> bool:
    try:
        parent_descriptor, target_name = open_parent_directory(
            plan.manifest.root,
            observation.resource.target,
        )
    except (OSError, NotImplementedError, ValueError):
        raise ApplyError(
            f"cannot safely open target parent for resource "
            f"{observation.resource.name!r}",
            code="unsafe_target",
        ) from None

    locked = False
    committed = False
    write_error: ApplyError | None = None
    cleanup_error: OSError | NotImplementedError | None = None
    try:
        try:
            lock_directory(parent_descriptor)
            locked = True
        except (OSError, NotImplementedError):
            raise ApplyError(
                f"cannot safely lock target parent for resource "
                f"{observation.resource.name!r}; no files were changed",
                code="concurrent_change",
            ) from None

        # These checks deliberately run again while the target directory is
        # locked. The earlier plan-wide preflight is explanatory; this is the
        # last safe observation before creating and committing the temporary
        # entry.
        try:
            verify_directory_identity(
                parent_descriptor,
                observation.resource.target.parent,
            )
        except FileChangedError:
            raise ApplyError(
                f"target parent for resource {observation.resource.name!r} "
                "changed during apply",
                code="concurrent_change",
            ) from None
        _preflight_manifest(plan, execution=execution)
        _check_target_parent(plan, observation, phase="during apply")
        _preflight_source(plan, observation)
        _check_target_state(
            plan,
            observation,
            phase="during apply",
            parent_descriptor=parent_descriptor,
        )
        if observation.resource.kind == "symbolic":
            committed = _write_symbolic_observation(
                plan, observation, parent_descriptor, target_name, execution=execution
            )
        else:
            committed = _write_template_observation(
                plan,
                observation,
                parent_descriptor,
                target_name,
                execution=execution,
            )
    except ApplyError as exc:
        write_error = exc
        committed = exc.committed
    finally:
        if locked:
            try:
                unlock_directory(parent_descriptor)
            except (OSError, NotImplementedError) as exc:
                cleanup_error = exc
        try:
            os.close(parent_descriptor)
        except OSError as exc:
            cleanup_error = cleanup_error or exc

    if cleanup_error is not None:
        raise ApplyError(
            f"target directory cleanup for resource {observation.resource.name!r} "
            "could not be confirmed",
            code="cleanup_failed",
            committed=committed,
            target_name=observation.resource.target_name,
        ) from None
    if write_error is not None:
        raise write_error
    return committed


def _write_template_observation(
    plan: Plan,
    observation: ResourceObservation,
    parent_descriptor: int,
    target_name: str,
    *,
    execution: bool = False,
) -> bool:
    desired_bytes = observation.desired_bytes
    if desired_bytes is None:
        raise ApplyError(
            f"resource {observation.resource.name!r} has no rendered content",
            code="apply_failed",
        )

    mode = observation.live_mode if observation.live_mode is not None else 0o644

    def create_entry() -> str:
        descriptor, created_name = create_temporary_file(
            parent_descriptor,
            prefix=f".{observation.resource.target.name}.luwu-",
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(desired_bytes)
                handle.flush()
                os.fchmod(handle.fileno(), mode)
                os.fsync(handle.fileno())
        except BaseException:
            try:
                _cleanup_temporary_entry(parent_descriptor, created_name)
            except OSError:
                raise ApplyError(
                    f"temporary entry for resource {observation.resource.name!r} "
                    "could not be cleaned up",
                    code="cleanup_failed",
                ) from None
            raise
        return created_name

    return _write_temporary_entry(
        plan,
        observation,
        parent_descriptor,
        target_name,
        create_entry=create_entry,
        execution=execution,
    )


def _write_symbolic_observation(
    plan: Plan,
    observation: ResourceObservation,
    parent_descriptor: int,
    target_name: str,
    *,
    execution: bool = False,
) -> bool:
    desired_link = observation.desired_link
    if desired_link is None:
        raise ApplyError(
            f"resource {observation.resource.name!r} has no symbolic target",
            code="apply_failed",
        )

    def create_entry() -> str:
        return create_temporary_symlink(
            parent_descriptor,
            prefix=f".{observation.resource.target.name}.luwu-",
            target=desired_link,
        )

    def validate_entry(created_name: str) -> None:
        temporary_link = os.readlink(created_name, dir_fd=parent_descriptor)
        if (
            resolve_link_target(observation.resource.target, temporary_link)
            != observation.source_path
        ):
            raise ApplyError(
                f"source for resource {observation.resource.name!r} changed during apply; "
                "run plan again",
                code="stale_plan",
            )

    return _write_temporary_entry(
        plan,
        observation,
        parent_descriptor,
        target_name,
        create_entry=create_entry,
        validate_entry=validate_entry,
        execution=execution,
    )


def _write_temporary_entry(
    plan: Plan,
    observation: ResourceObservation,
    parent_descriptor: int,
    target_name: str,
    *,
    create_entry: Callable[[], str],
    validate_entry: Callable[[str], None] | None = None,
    execution: bool = False,
) -> bool:
    temporary_name: str | None = None
    committed = False
    apply_error: ApplyError | None = None
    cleanup_error: OSError | None = None
    temporary_identity: tuple[int, int] | None = None
    temporary_is_symlink = False
    try:
        created_name = create_entry()
        temporary_name = created_name
        if validate_entry is not None:
            validate_entry(created_name)
        temporary_info = os.stat(
            created_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        temporary_identity = _identity(temporary_info)
        temporary_is_symlink = stat.S_ISLNK(temporary_info.st_mode)
        _preflight_manifest(plan, execution=execution)
        _preflight_source(plan, observation)
        _check_target_state(
            plan,
            observation,
            phase="during apply",
            parent_descriptor=parent_descriptor,
        )
        verify_directory_identity(parent_descriptor, observation.resource.target.parent)
        current_temporary = os.stat(
            created_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _identity(current_temporary) != temporary_identity
            or stat.S_ISLNK(current_temporary.st_mode) != temporary_is_symlink
        ):
            raise ApplyError(
                f"temporary entry for resource {observation.resource.name!r} "
                "changed during apply",
                code="concurrent_change",
            )
        if temporary_is_symlink:
            if (
                observation.desired_link is None
                or os.readlink(created_name, dir_fd=parent_descriptor)
                != observation.desired_link
            ):
                raise ApplyError(
                    f"temporary entry for resource {observation.resource.name!r} "
                    "changed during apply",
                    code="concurrent_change",
                )
        else:
            current_data, _ = read_regular_file_at(parent_descriptor, created_name)
            if current_data != observation.desired_bytes:
                raise ApplyError(
                    f"temporary entry for resource {observation.resource.name!r} "
                    "changed during apply",
                    code="concurrent_change",
                )
        os.replace(
            created_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        committed = True
        temporary_name = None
        sync_directory(parent_descriptor)
    except ApplyError as exc:
        apply_error = exc
    except FileChangedError:
        apply_error = ApplyError(
            f"target or temporary entry for resource {observation.resource.name!r} "
            "changed during apply",
            code="concurrent_change",
        )
    except (OSError, NotImplementedError, RuntimeError, TypeError) as exc:
        apply_error = ApplyError(
            f"cannot atomically write target for resource {observation.resource.name!r}: "
            f"{getattr(exc, 'strerror', None) or type(exc).__name__}",
            code="durability_unconfirmed" if committed else "write_failed",
            committed=committed,
            target_name=observation.resource.target_name,
        )
    finally:
        if temporary_name is not None:
            try:
                _cleanup_temporary_entry(parent_descriptor, temporary_name)
            except OSError as exc:
                cleanup_error = exc

    if cleanup_error is not None:
        apply_error = ApplyError(
            f"temporary entry for resource {observation.resource.name!r} "
            "could not be cleaned up",
            code="cleanup_failed",
            committed=committed or (apply_error.committed if apply_error else False),
            target_name=observation.resource.target_name,
        )
    if apply_error is not None:
        raise apply_error
    return committed


def _cleanup_temporary_entry(parent_descriptor: int, name: str) -> None:
    os.unlink(name, dir_fd=parent_descriptor)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
