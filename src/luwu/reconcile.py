"""Observation, explainable planning, and explicit atomic application."""

from __future__ import annotations

import hashlib
import os
import stat
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from .errors import ApplyError, ManifestError, ProviderError, RenderError
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
    PROVIDER_MANIFEST_VERSION,
    Manifest,
    Resource,
    load_manifest,
)
from .ownership import OwnershipResult, classify_fields
from .plan_record import (
    PlanRecord,
    PlanRecordError,
    SecretPlanRecord,
    record_lock_path,
)
from .providers import (
    ProviderAuthority,
    ProviderResolver,
    resolve_provider,
)
from .rendering import read_source, render_template
from .secrets import SecretRenderContext
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
_PROVIDER_EXECUTION_RECORD_CONTRACT = "provider-secret-whole-file-v6"
_PROVIDER_EXECUTION_CAPABILITY = "provider-secret-whole-file"


@dataclass(frozen=True, slots=True)
class _PlanCapability:
    """Immutable manifest identity and capability captured by the planner."""

    manifest_path: Path
    manifest_root: Path
    manifest_digest: str
    manifest_version: int
    token: object = field(repr=False, compare=False)
    authority: ProviderAuthority | None = field(default=None, repr=False, compare=False)
    resolver: ProviderResolver | Callable[..., object] | None = field(
        default=None, repr=False, compare=False
    )
    secret_contexts: tuple[SecretRenderContext | None, ...] = field(
        default=(), repr=False, compare=False
    )
    observation_fingerprints: tuple[tuple[object, ...], ...] = field(
        default=(), repr=False, compare=False
    )


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
    baseline_digest: str | None = field(default=None, repr=False, compare=False)


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
        if self.contract_version in {
            EXECUTION_MANIFEST_VERSION,
            PROVIDER_MANIFEST_VERSION,
        }:
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
    record: PlanRecord | SecretPlanRecord | None
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


def build_plan(
    manifest: Manifest,
    *,
    authority: ProviderAuthority | None = None,
    resolver: ProviderResolver | Callable[..., object] | None = None,
) -> Plan:
    """Render and inspect the declared resource without writing any path.

    Version 6 provider values are resolved once while building the plan.  The
    resulting secret render context stays private to the in-process plan
    capability and is never part of a public projection or durable record.
    """

    if manifest.version == 1:
        resource = _require_single_resource(manifest)
    if manifest._provenance is not _LOADER_PROVENANCE:
        raise ManifestError(
            "manifest must be loaded by load_manifest before planning",
            code="invalid_manifest_provenance",
        )
    secret_contexts: tuple[SecretRenderContext | None, ...] = ()
    if manifest.version == PROVIDER_MANIFEST_VERSION:
        observations, secret_contexts = _build_provider_plan(
            manifest,
            authority=authority,
            resolver=resolver,
        )
    elif manifest.version == 1:
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
            manifest_digest=(
                ""
                if manifest.version == PROVIDER_MANIFEST_VERSION
                else manifest.content_digest
            ),
            manifest_version=manifest.version,
            token=_PLAN_CAPABILITY_TOKEN,
            authority=authority,
            resolver=resolver,
            secret_contexts=secret_contexts,
            observation_fingerprints=tuple(
                _observation_fingerprint(observation) for observation in observations
            ),
        ),
    )
    return plan


def _build_provider_plan(
    manifest: Manifest,
    *,
    authority: ProviderAuthority | None,
    resolver: ProviderResolver | Callable[..., object] | None,
) -> tuple[tuple[ResourceObservation, ...], tuple[SecretRenderContext | None, ...]]:
    observations: list[ResourceObservation] = []
    contexts: list[SecretRenderContext | None] = []
    for resource in manifest.resources:
        observation, context = _plan_provider_resource(
            manifest,
            resource,
            authority=authority,
            resolver=resolver,
        )
        observations.append(observation)
        contexts.append(context)
    return tuple(observations), tuple(contexts)


def _plan_provider_resource(
    manifest: Manifest,
    resource: Resource,
    *,
    authority: ProviderAuthority | None,
    resolver: ProviderResolver | Callable[..., object] | None,
) -> tuple[ResourceObservation, SecretRenderContext | None]:
    """Resolve each declared provider exactly once and render one v6 resource."""

    parent_issue = _target_parent_issue_for_resource(manifest, resource)
    if parent_issue is not None:
        return (
            _blocked_observation(
                resource,
                reason=parent_issue,
                comparison=not_compared(
                    resource.comparison,
                    code="unsafe_target",
                    reason="secret target boundary is unsafe; comparison was not run",
                ),
            ),
            None,
        )

    values: dict[str, str] = {}
    try:
        for alias, reference in resource.providers.items():
            values[alias] = resolve_provider(
                reference,
                authority=authority,
                resolver=resolver,
            )
        context = SecretRenderContext(values)
        rendered = render_template(resource, root=manifest.root, secrets=context)
    except ProviderError:
        return (
            _blocked_observation(
                resource,
                reason="provider value is unavailable; execution is blocked",
                comparison=not_compared(
                    resource.comparison,
                    code="provider_unavailable",
                    reason="provider value was not available; comparison was not run",
                ),
            ),
            None,
        )
    except RenderError:
        return (
            _blocked_observation(
                resource,
                reason="secret-backed template could not be rendered safely",
                comparison=not_compared(
                    resource.comparison,
                    code="secret_render_failed",
                    reason="secret-backed rendering failed; comparison was not run",
                ),
            ),
            None,
        )

    live = _read_target(
        resource.target,
        root=_target_root_for_resource(manifest, resource),
        secret_target=True,
    )
    if live.issue is not None or live.link_target is not None:
        return (
            _blocked_observation(
                resource,
                desired_bytes=rendered.data,
                source_digest=rendered.source_digest,
                source_path=rendered.source_path,
                source_identity=rendered.source_identity,
                reason=live.issue or "secret target is a symlink",
                live_digest=live.digest,
                live_mode=live.mode,
                live_link_target=live.link_target,
                live_identity=live.identity,
                target_parent_identity=live.parent_identity,
                comparison=not_compared(
                    resource.comparison,
                    code="unsafe_target",
                    reason="secret target is unsafe; comparison was not run",
                ),
            ),
            context,
        )

    desired = rendered.data
    if live.data is None:
        observation = ResourceObservation(
            resource=resource,
            status=Status.MISSING,
            action=Action.CREATE,
            reason="secret target does not exist",
            desired_bytes=desired,
            desired_link=None,
            source_digest=rendered.source_digest,
            source_path=rendered.source_path,
            live_digest=None,
            live_mode=None,
            live_link_target=None,
            source_identity=rendered.source_identity,
            live_identity=None,
            target_parent_identity=live.parent_identity,
            comparison=not_compared(
                resource.comparison,
                code="target_missing",
                reason="secret target does not exist; comparison was not run",
            ),
        )
        return observation, context

    comparison = compare(desired, live.data, strategy="exact-bytes")
    if comparison.status is ComparisonStatus.EXACT:
        status, action, reason = (
            Status.IN_SYNC,
            Action.NOOP,
            "secret template matches target",
        )
    else:
        status, action, reason = (
            Status.DRIFTED,
            Action.REPLACE,
            "secret template differs from target",
        )
    return (
        ResourceObservation(
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
        ),
        context,
    )


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
        baseline_digest=None if baseline is None else _digest(baseline),
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
    plan: Plan,
    record_path: Path,
    *,
    confirm: bool,
    authority: ProviderAuthority | None = None,
    resolver: ProviderResolver | Callable[..., object] | None = None,
) -> ExecutionResult:
    """Execute a loader/planner-issued v5 or v6 plan with a durable journal.

    Preview is completely side-effect free. Confirmed execution preflights all
    resources before creating the journal and processes them in stable order.
    A failure is never rolled back; the journal records the recovery boundary.
    """

    if (
        plan.contract_version == PROVIDER_MANIFEST_VERSION
        and plan.blocked
        and authority is not None
    ):
        plan = build_plan(plan.manifest, authority=authority, resolver=resolver)
    _require_execution_plan(plan)
    preview = _execution_preview(plan)
    if not confirm:
        return ExecutionResult(preview=preview, record=None)

    execution_resources: list[dict[str, object]]
    if plan.contract_version == PROVIDER_MANIFEST_VERSION:
        execution_resources: list[dict[str, object]] = [
            {"ordinal": ordinal, "state": "not-attempted"}
            for ordinal, _observation in enumerate(plan.observations)
        ]
    else:
        execution_resources = [
            {
                "name": observation.resource.name,
                "target": observation.resource.target_name,
                "state": "not-attempted",
            }
            for observation in plan.observations
        ]
    plan_id: str | None = None
    changed_targets: list[str] = []
    try:
        _preflight_execution_manifest(plan)
        for observation in plan.observations:
            _preflight_observation(plan, observation)
        _check_execution_record_path(plan, record_path)

        record = _execution_record(plan)
        plan_id = str(record.to_dict()["plan_id"])
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

        for ordinal, observation in enumerate(plan.observations):
            if observation.action is Action.NOOP:
                previous = record
                record = record.transition_path(ordinal, "unchanged")
                execution_resources[ordinal]["state"] = "unchanged"
                _write_execution_record(record, record_path, expected=previous)
                continue

            previous = record
            record = record.transition_path(ordinal, "commit_intent")
            _write_execution_record(record, record_path, expected=previous)
            try:
                writer_committed = _write_observation(plan, observation, execution=True)
                if not writer_committed:
                    raise ApplyError(
                        f"writer for resource {observation.resource.name!r} "
                        "did not commit the target",
                        code="write_failed",
                        target_name=_execution_target_key(plan, ordinal),
                    )
            except ApplyError as exc:
                target_name = _execution_target_key(plan, ordinal)
                changed_key = _execution_target_key(plan, ordinal)
                indeterminate = exc.code == "recovery_required" and not exc.committed
                if exc.committed:
                    if changed_key not in changed_targets:
                        changed_targets.append(changed_key)
                    execution_resources[ordinal]["state"] = "unknown"
                elif indeterminate:
                    execution_resources[ordinal]["state"] = "unknown"
                else:
                    execution_resources[ordinal]["state"] = "failed"
                _mark_execution_failure(
                    record,
                    plan,
                    ordinal,
                    record_path,
                    committed=bool(changed_targets),
                    recovery_required=indeterminate,
                )
                raise ApplyError(
                    f"execution stopped at resource {observation.resource.name!r}; "
                    "rollback=never",
                    code=(
                        "recovery_required"
                        if changed_targets or indeterminate
                        else "execution_failed"
                    ),
                    committed=bool(changed_targets),
                    target_name=target_name,
                ) from exc

            changed_key = _execution_target_key(plan, ordinal)
            if changed_key not in changed_targets:
                changed_targets.append(changed_key)
            execution_resources[ordinal]["state"] = "unknown"
            try:
                if plan.contract_version == PROVIDER_MANIFEST_VERSION:
                    confirmed = _secret_target_matches(plan, observation)
                    postcondition = None
                else:
                    postcondition = _execution_condition(
                        observation.resource.target, plan.manifest.root
                    )
                    confirmed = _condition_matches_expected(
                        postcondition,
                        _execution_postcondition(observation, postcondition),
                    )
                if not confirmed:
                    raise ApplyError(
                        f"postcondition for resource {observation.resource.name!r} "
                        "could not be confirmed",
                        code="durability_unconfirmed",
                        committed=True,
                        target_name=_execution_target_key(plan, ordinal),
                    )
                committed_record = record.update_path_condition(
                    ordinal,
                    observation.resource.target_name,
                    postcondition=postcondition,
                ).transition_path(ordinal, "committed")
                execution_resources[ordinal]["state"] = "committed"
                _write_execution_record(committed_record, record_path, expected=record)
            except ApplyError as exc:
                _mark_execution_failure(
                    record,
                    plan,
                    ordinal,
                    record_path,
                    committed=bool(changed_targets),
                )
                if execution_resources[ordinal]["state"] == "committed":
                    raise
                raise ApplyError(
                    f"execution stopped at resource {observation.resource.name!r}; "
                    "rollback=never",
                    code="recovery_required",
                    committed=bool(changed_targets),
                    target_name=_execution_target_key(plan, ordinal),
                ) from exc
            record = committed_record

        previous = record
        record = record.transition("committed")
        _write_execution_record(record, record_path, expected=previous)
        return ExecutionResult(
            preview=preview,
            record=record,
            changed_targets=tuple(changed_targets),
        )
    except ApplyError as exc:
        exc.committed = bool(changed_targets)
        exc.execution = _execution_error_metadata(
            plan_id, changed_targets, execution_resources
        )
        raise


def inspect_execution_record(record_path: Path) -> dict[str, object]:
    """Read a journal for recovery decisions without replaying or writing."""

    try:
        record = PlanRecord.read(record_path).to_dict()
    except PlanRecordError:
        try:
            record = SecretPlanRecord.read(record_path).to_dict()
        except PlanRecordError:
            raise PlanRecordError(
                "record is not a supported execution journal",
                code="execution_record_contract",
            ) from None
    manifest = record["manifest"]
    if (
        isinstance(manifest, dict)
        and manifest.get("version") == PROVIDER_MANIFEST_VERSION
    ):
        _validate_provider_execution_record(record)
        return record
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


def _validate_provider_execution_record(record: dict[str, object]) -> None:
    """Validate the v6 journal meaning without inspecting provider inputs."""

    if record.get("execution_contract") != _PROVIDER_EXECUTION_RECORD_CONTRACT:
        raise PlanRecordError(
            "record is not a version-6 provider execution journal",
            code="execution_record_contract",
        )
    if record.get("mutation_contract") != "atomic-single-secret-target":
        raise PlanRecordError(
            "provider execution journal mutation contract is unsupported",
            code="execution_record_contract",
        )
    manifest = record.get("manifest")
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != PROVIDER_MANIFEST_VERSION
    ):
        raise PlanRecordError(
            "provider execution journal manifest is invalid",
            code="execution_record_contract",
        )
    resources = record.get("resources")
    if not isinstance(resources, list) or not resources:
        raise PlanRecordError(
            "provider execution journal resources are invalid",
            code="execution_record_contract",
        )
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("operation") not in {
            Action.NOOP.value,
            Action.CREATE.value,
            Action.REPLACE.value,
        }:
            raise PlanRecordError(
                "provider execution journal resource operation is invalid",
                code="execution_record_contract",
            )


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


def reobserve_execution_record(
    record_path: Path,
    *,
    authority: ProviderAuthority | None = None,
    resolver: ProviderResolver | Callable[..., object] | None = None,
) -> dict[str, object]:
    """Re-observe a v5 or v6 journal without replaying inputs or writing state."""

    record = inspect_execution_record(record_path)
    if record.get("execution_contract") == _PROVIDER_EXECUTION_RECORD_CONTRACT:
        return _reobserve_provider_record(
            record,
            authority=authority,
            resolver=resolver,
        )
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
        current_digest = manifest.content_digest
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
        fresh_in_sync = observation is not None and observation.status is Status.IN_SYNC
        if resource_state in {"committed", "unchanged"} and all_match and fresh_in_sync:
            reobserved = "confirmed"
        elif (
            resource_state in {"unknown", "recovery_required"}
            and all_match
            and fresh_in_sync
        ):
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
    overall = (
        "confirmed"
        if all(item["reobserved_state"] == "confirmed" for item in resources)
        else "recovery_required"
    )
    return {
        "schema_version": EXECUTION_MANIFEST_VERSION,
        "command": "recover",
        "mode": "reobserve-only",
        "plan_id": record["plan_id"],
        "record_state": record["state"],
        "outcome": overall,
        "resources": resources,
    }


def _reobserve_provider_record(
    record: dict[str, object],
    *,
    authority: ProviderAuthority | None,
    resolver: ProviderResolver | Callable[..., object] | None,
) -> dict[str, object]:
    """Observe current v6 state; never claim historical secret-content proof."""

    manifest_info = record.get("manifest")
    if not isinstance(manifest_info, dict):
        return _reobserve_provider_blocked(record, "manifest_unavailable")
    try:
        manifest_path = Path(str(manifest_info["path"]))
        expected_root = Path(str(manifest_info["root"]))
        expected_version = int(manifest_info["version"])
    except (KeyError, TypeError, ValueError):
        return _reobserve_provider_blocked(record, "manifest_unavailable")
    try:
        if (
            not manifest_path.is_absolute()
            or manifest_path.resolve(strict=True) != manifest_path
        ):
            return _reobserve_provider_blocked(record, "manifest_unavailable")
    except (OSError, RuntimeError):
        return _reobserve_provider_blocked(record, "manifest_unavailable")
    try:
        manifest = load_manifest(manifest_path)
    except (ManifestError, OSError, RuntimeError):
        return _reobserve_provider_blocked(record, "manifest_unavailable")
    if (
        expected_version != PROVIDER_MANIFEST_VERSION
        or manifest.version != PROVIDER_MANIFEST_VERSION
        or manifest.root != expected_root
    ):
        return _reobserve_provider_blocked(record, "manifest_changed")

    persisted_resources = record.get("resources")
    if not isinstance(persisted_resources, list) or len(persisted_resources) != len(
        manifest.resources
    ):
        return _reobserve_provider_blocked(record, "record_manifest_mismatch")
    for ordinal, persisted in enumerate(persisted_resources):
        if not isinstance(persisted, dict) or (
            persisted.get("ordinal") != ordinal
            or persisted.get("label") != f"resource-{ordinal}"
        ):
            return _reobserve_provider_blocked(record, "record_manifest_mismatch")

    if authority is None:
        return _reobserve_provider_blocked(record, "capability_required")

    try:
        plan = build_plan(manifest, authority=authority, resolver=resolver)
    except (ManifestError, RenderError, ProviderError, OSError, RuntimeError):
        return _reobserve_provider_blocked(record, "observation_failed")
    if plan.blocked:
        return _reobserve_provider_blocked(record, "provider_unavailable")

    resources: list[dict[str, object]] = []
    for ordinal, persisted in enumerate(persisted_resources):
        assert isinstance(persisted, dict)
        observation = plan.observations[ordinal]
        record_state = str(persisted["state"])
        fresh_in_sync = observation.status is Status.IN_SYNC
        if record_state == "not-attempted":
            reobserved = "not-attempted"
        elif fresh_in_sync:
            reobserved = "currently_converged"
        else:
            reobserved = "changed_or_unknown"
        resources.append(
            {
                "ordinal": ordinal,
                "label": f"resource-{ordinal}",
                "record_state": record_state,
                "reobserved_state": reobserved,
                "plan_status": observation.status.value,
            }
        )
    outcome = (
        "currently_converged"
        if resources
        and all(
            item["reobserved_state"] in {"currently_converged", "not-attempted"}
            for item in resources
        )
        and any(item["reobserved_state"] == "currently_converged" for item in resources)
        else "recovery_required"
    )
    return {
        "schema_version": PROVIDER_MANIFEST_VERSION,
        "command": "recover",
        "mode": "reobserve-only",
        "plan_id": record["plan_id"],
        "record_state": record["state"],
        "outcome": outcome,
        "resources": resources,
    }


def _reobserve_provider_blocked(
    record: dict[str, object], reason: str
) -> dict[str, object]:
    resources: list[dict[str, object]] = []
    raw_resources = record.get("resources")
    if isinstance(raw_resources, list):
        for resource in raw_resources:
            if isinstance(resource, dict):
                resources.append(
                    {
                        "ordinal": resource.get("ordinal"),
                        "label": resource.get("label"),
                        "record_state": resource.get("state"),
                        "reobserved_state": "unavailable",
                    }
                )
    return {
        "schema_version": PROVIDER_MANIFEST_VERSION,
        "command": "recover",
        "mode": "reobserve-only",
        "plan_id": record.get("plan_id"),
        "record_state": record.get("state"),
        "outcome": "recovery_required",
        "reason": reason,
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
    if plan.blocked:
        raise ApplyError("plan contains blocked resources", code="plan_blocked")
    if capability.manifest_version == EXECUTION_MANIFEST_VERSION:
        valid_contract = plan.manifest.execution_capability == (
            "public-source-whole-file"
        )
    elif capability.manifest_version == PROVIDER_MANIFEST_VERSION:
        valid_contract = (
            plan.manifest.version == PROVIDER_MANIFEST_VERSION
            and plan.manifest.capabilities == ("subprocess",)
            and capability.authority is not None
            and capability.authority.allows("subprocess")
            and len(capability.secret_contexts) == len(plan.observations)
        )
    else:
        valid_contract = False
    if not valid_contract:
        raise ApplyError("plan is not a supported execution plan", code="invalid_plan")
    if (
        plan.manifest.path != capability.manifest_path
        or plan.manifest.root != capability.manifest_root
        or plan.manifest.version != capability.manifest_version
        or (
            capability.manifest_version != PROVIDER_MANIFEST_VERSION
            and plan.manifest.content_digest != capability.manifest_digest
        )
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
    if (
        tuple(
            _observation_fingerprint(observation) for observation in plan.observations
        )
        != capability.observation_fingerprints
    ):
        raise ApplyError(
            "plan observations were changed after planning", code="invalid_plan"
        )
    for observation in plan.observations:
        if capability.manifest_version == EXECUTION_MANIFEST_VERSION and (
            observation.resource.capability != "public-source-whole-file"
        ):
            raise ApplyError("resource capability is invalid", code="invalid_plan")
        if capability.manifest_version == PROVIDER_MANIFEST_VERSION and (
            observation.resource.content_sensitivity != "secret"
            or not observation.resource.providers
            or observation.resource.kind != "template"
        ):
            raise ApplyError(
                "provider resource contract is invalid", code="invalid_plan"
            )
        if observation.status is Status.BLOCKED or observation.action is Action.BLOCK:
            raise ApplyError("plan contains blocked resources", code="plan_blocked")
        if observation.action not in (Action.NOOP, Action.CREATE, Action.REPLACE):
            raise ApplyError(
                "plan contains a non-executable action", code="invalid_plan"
            )


def _execution_preview(plan: Plan) -> dict[str, object]:
    if plan.contract_version == PROVIDER_MANIFEST_VERSION:
        return {
            "schema_version": PROVIDER_MANIFEST_VERSION,
            "execution_capability": _PROVIDER_EXECUTION_CAPABILITY,
            "rollback": "never",
            "resources": [
                {
                    "ordinal": ordinal,
                    "label": f"resource-{ordinal}",
                    "kind": observation.resource.kind,
                    "status": observation.status.value,
                    "action": observation.action.value,
                    "reason": observation.reason,
                }
                for ordinal, observation in enumerate(plan.observations)
            ],
        }
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


def _execution_error_metadata(
    plan_id: str | None,
    changed_targets: list[str],
    resources: list[dict[str, object]],
) -> dict[str, object]:
    """Build the fixed, metadata-only context attached to execution failures."""

    if resources and "ordinal" in resources[0]:
        public_resources = [
            {
                "ordinal": resource.get("ordinal"),
                "state": resource.get("state"),
            }
            for resource in resources
        ]
    else:
        public_resources = [
            {
                "name": resource["name"],
                "target": resource["target"],
                "state": resource["state"],
            }
            for resource in resources
        ]
    return {
        "plan_id": plan_id,
        "committed": bool(changed_targets),
        "changed_targets": list(changed_targets),
        "resources": public_resources,
    }


def _execution_target_key(plan: Plan, ordinal: int) -> str:
    if plan.contract_version == PROVIDER_MANIFEST_VERSION:
        return f"resource-{ordinal}"
    return plan.observations[ordinal].resource.target_name


def _secret_target_matches(plan: Plan, observation: ResourceObservation) -> bool:
    current = _read_target(
        observation.resource.target,
        root=_target_root_for_resource(plan.manifest, observation.resource),
        secret_target=True,
    )
    return (
        current.issue is None
        and current.link_target is None
        and current.data is not None
        and current.data == observation.desired_bytes
        and current.mode is not None
        and not (current.mode & 0o077)
    )


def _check_execution_record_path(plan: Plan, record_path: Path) -> None:
    record_paths = (record_path, record_lock_path(record_path))
    record_candidates = {
        candidate for path in record_paths for candidate in _path_variants(path)
    }
    declared = [plan.manifest.path]
    declared.extend(
        path
        for observation in plan.observations
        for path in (observation.resource.source, observation.resource.target)
    )
    for candidate in declared:
        candidate_paths = _path_variants(candidate)
        if any(
            _paths_overlap(record_candidate, declared_candidate)
            for record_candidate in record_candidates
            for declared_candidate in candidate_paths
        ):
            raise ApplyError(
                "execution journal or lock path overlaps a declared path; "
                "no files were changed",
                code="record_path_conflict",
            )
    record_identities = {
        identity
        for path in record_candidates
        if (identity := _existing_path_identity(path)) is not None
    }
    declared_identities = {
        identity
        for candidate in declared
        for path in _path_variants(candidate)
        if (identity := _existing_path_identity(path)) is not None
    }
    if record_identities & declared_identities:
        raise ApplyError(
            "execution journal or lock path aliases a declared inode; "
            "no files were changed",
            code="record_path_conflict",
        )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_variants(path: Path) -> set[Path]:
    variants = {_absolute_path(path)}
    try:
        variants.add(path.expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        pass
    return variants


def _existing_path_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except (OSError, RuntimeError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _observation_fingerprint(observation: ResourceObservation) -> tuple[object, ...]:
    resource = observation.resource
    providers = tuple(
        sorted(
            (
                alias,
                reference.type,
                reference.item,
                reference.field,
                reference.alias,
            )
            for alias, reference in resource.providers.items()
        )
    )
    return (
        (
            resource.name,
            resource.kind,
            resource.source,
            resource.target,
            resource.source_name,
            resource.target_name,
            resource.owner,
            resource.scope,
            _fingerprint_value(resource.variables),
            resource.variables_sensitivity,
            resource.comparison,
            _fingerprint_value(resource.fields),
            resource.baseline,
            resource.baseline_name,
            resource.content_sensitivity,
            _fingerprint_value(resource.reverse_sync),
            resource.capability,
            providers,
        ),
        observation.status.value,
        observation.action.value,
        observation.reason,
        observation.desired_bytes,
        observation.desired_link,
        observation.source_digest,
        observation.source_path,
        observation.live_digest,
        observation.live_mode,
        observation.live_link_target,
        observation.source_identity,
        observation.live_identity,
        observation.target_parent_identity,
        observation.comparison.to_dict() if observation.comparison else None,
        observation.ownership.to_dict() if observation.ownership else None,
        observation.baseline_digest,
    )


def _fingerprint_value(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(
            sorted((key, _fingerprint_value(item)) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_fingerprint_value(item) for item in value)
    return value


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


def _execution_record(plan: Plan) -> PlanRecord | SecretPlanRecord:
    if plan.contract_version == PROVIDER_MANIFEST_VERSION:
        return SecretPlanRecord.create(
            plan_id=str(uuid4()),
            execution_contract=_PROVIDER_EXECUTION_RECORD_CONTRACT,
            mutation_contract="atomic-single-secret-target",
            manifest={
                "path": str(plan.manifest.path),
                "root": str(plan.manifest.root),
                "version": PROVIDER_MANIFEST_VERSION,
            },
            resources=[
                {
                    "ordinal": ordinal,
                    "label": f"resource-{ordinal}",
                    "operation": observation.action.value,
                    "target_state": (
                        "missing" if observation.live_digest is None else "regular"
                    ),
                    "state": "planned",
                }
                for ordinal, observation in enumerate(plan.observations)
            ],
        )
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
    record: PlanRecord | SecretPlanRecord,
    record_path: Path,
    *,
    expected: PlanRecord | SecretPlanRecord | None = None,
) -> None:
    try:
        if isinstance(record, SecretPlanRecord):
            if expected is not None and not isinstance(expected, SecretPlanRecord):
                raise PlanRecordError(
                    "secret execution record expected value is invalid",
                    code="plan_record_cas",
                )
            record.write(record_path, expected=expected)
        else:
            if expected is not None and not isinstance(expected, PlanRecord):
                raise PlanRecordError(
                    "execution record expected value is invalid",
                    code="plan_record_cas",
                )
            record.write(record_path, expected=expected)
    except (PlanRecordError, OSError) as exc:
        raise ApplyError(
            "execution journal durability is unknown; recovery is required",
            code="recovery_required",
        ) from exc


def _mark_execution_failure(
    record: PlanRecord | SecretPlanRecord,
    plan: Plan,
    ordinal: int,
    record_path: Path,
    *,
    committed: bool,
    recovery_required: bool = False,
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
        failed = failed.transition(
            "recovery_required" if committed or recovery_required else "unknown"
        )
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
            milestone = "M3a" if capability.manifest_version == 3 else "M3b"
            raise ApplyError(
                f"manifest version {capability.manifest_version} plans are read-only "
                f"in {milestone}; no files were changed",
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

    if plan.contract_version == PROVIDER_MANIFEST_VERSION:
        resources = []
        for ordinal, observation in enumerate(plan.observations):
            label = f"resource-{ordinal}"
            resources.append(
                {
                    "ordinal": ordinal,
                    "label": label,
                    "kind": observation.resource.kind,
                    "owner": observation.resource.owner,
                    "scope": observation.resource.scope,
                    "status": observation.status.value,
                    "action": observation.action.value,
                    "reason": observation.reason,
                    "impact": {
                        "writes": [label]
                        if observation.action in (Action.CREATE, Action.REPLACE)
                        else [],
                        "overwrites": [label]
                        if observation.action is Action.REPLACE
                        else [],
                        "scope": observation.resource.scope,
                    },
                }
            )
        return {
            "schema_version": PROVIDER_MANIFEST_VERSION,
            "command": command,
            "manifest": str(plan.manifest.path),
            "manifest_version": PROVIDER_MANIFEST_VERSION,
            "applyable": plan.can_apply,
            "apply_block_reason": plan.apply_block_reason,
            "execution_capability": _PROVIDER_EXECUTION_CAPABILITY,
            "resources": resources,
            "summary": plan.summary(),
        }

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


def _target_root_for_resource(manifest: Manifest, resource: Resource) -> Path:
    """Return the descriptor root used for a resource target."""

    if manifest.version == PROVIDER_MANIFEST_VERSION:
        return Path(resource.target.anchor)
    return manifest.root


def _target_parent_issue_for_resource(
    manifest: Manifest, resource: Resource
) -> str | None:
    if manifest.version != PROVIDER_MANIFEST_VERSION:
        return _target_parent_issue(resource.target, root=manifest.root)
    if not resource.target.is_absolute() or resource.target.parent == resource.target:
        return "secret target must name a file below an existing parent"
    issue = _target_parent_issue(
        resource.target,
        root=_target_root_for_resource(manifest, resource),
    )
    if issue is not None:
        return f"secret target parent is unsafe: {issue}"
    try:
        info = resource.target.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return "secret target cannot be inspected safely"
    if stat.S_ISLNK(info.st_mode):
        return "secret target is a symlink"
    if stat.S_ISDIR(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "secret target is not a regular file"
    return _secret_target_issue_from_stat(info)


def _secret_target_issue_from_stat(info: os.stat_result) -> str | None:
    try:
        current_uid = os.getuid()
    except AttributeError:
        return "secret target owner cannot be verified"
    if info.st_uid != current_uid:
        return "secret target owner cannot be verified"
    if stat.S_IMODE(info.st_mode) & 0o077 or info.st_mode & 0o7000:
        return "secret target permissions are not owner-only"
    if info.st_nlink != 1:
        return "secret target has multiple hard links"
    return None


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


def _read_target(
    target: Path,
    *,
    root: Path,
    secret_target: bool = False,
) -> _TargetState:
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
            secret_target=secret_target,
        )
    finally:
        os.close(parent_descriptor)


def _read_target_at(
    target: Path,
    *,
    parent_descriptor: int,
    name: str,
    secret_target: bool = False,
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
        if secret_target:
            issue = _secret_target_issue_from_stat(info)
            if issue is not None:
                return _TargetState(
                    data=None,
                    digest=None,
                    mode=stat.S_IMODE(info.st_mode),
                    issue=issue,
                    identity=_identity(info),
                    parent_identity=parent_identity,
                )

        data, current = read_regular_file_at(parent_descriptor, name)
        if secret_target:
            issue = _secret_target_issue_from_stat(current)
            if issue is not None:
                return _TargetState(
                    data=None,
                    digest=None,
                    mode=stat.S_IMODE(current.st_mode),
                    issue=issue,
                    identity=_identity(current),
                    parent_identity=parent_identity,
                )
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
    parent_issue = _target_parent_issue_for_resource(
        plan.manifest, observation.resource
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
        secrets = _secret_context_for(plan, observation)
        try:
            current_rendered = render_template(
                resource,
                root=plan.manifest.root,
                secrets=secrets,
            )
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


def _secret_context_for(
    plan: Plan, observation: ResourceObservation
) -> SecretRenderContext | None:
    if plan.contract_version != PROVIDER_MANIFEST_VERSION:
        return None
    capability = _capability_for(plan)
    if capability is None:
        raise ApplyError(
            "plan lacks a valid provider capability; no files were changed",
            code="invalid_plan",
        )
    try:
        ordinal = plan.observations.index(observation)
        return capability.secret_contexts[ordinal]
    except (ValueError, IndexError):
        raise ApplyError(
            "plan provider context is invalid; no files were changed",
            code="invalid_plan",
        ) from None


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
    except (OSError, RuntimeError):
        raise ApplyError(
            "manifest changed or became unreadable after planning; run plan again",
            code="stale_plan",
        ) from None
    if capability.manifest_version != PROVIDER_MANIFEST_VERSION:
        try:
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
    if execution and current_manifest.version not in {
        EXECUTION_MANIFEST_VERSION,
        PROVIDER_MANIFEST_VERSION,
    }:
        raise ApplyError(
            "manifest is no longer an execution manifest; no files were changed",
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
        capability.manifest_version == PROVIDER_MANIFEST_VERSION
        and not _provider_manifest_matches(plan.manifest, current_manifest)
    ):
        raise ApplyError(
            "manifest changed after planning; run plan again",
            code="stale_plan",
        )
    if (
        plan.manifest.path != current_manifest.path
        or plan.manifest.root != current_manifest.root
        or (
            capability.manifest_version != PROVIDER_MANIFEST_VERSION
            and plan.manifest.content_digest != current_manifest.content_digest
        )
        or plan.manifest.version != current_manifest.version
        or plan.manifest.resources != current_manifest.resources
        or tuple(observation.resource for observation in plan.observations)
        != current_manifest.resources
    ):
        raise ApplyError(
            "plan does not match its manifest; no files were changed",
            code="invalid_plan",
        )


def _provider_manifest_matches(left: Manifest, right: Manifest) -> bool:
    """Compare v6 declarations without relying on a persisted provider digest."""

    if (
        left.version != PROVIDER_MANIFEST_VERSION
        or right.version != PROVIDER_MANIFEST_VERSION
        or left.path != right.path
        or left.root != right.root
        or left.capabilities != right.capabilities
        or len(left.resources) != len(right.resources)
    ):
        return False
    for left_resource, right_resource in zip(left.resources, right.resources):
        if (
            left_resource.name != right_resource.name
            or left_resource.kind != right_resource.kind
            or left_resource.source != right_resource.source
            or left_resource.target != right_resource.target
            or left_resource.source_name != right_resource.source_name
            or left_resource.target_name != right_resource.target_name
            or left_resource.owner != right_resource.owner
            or left_resource.scope != right_resource.scope
            or left_resource.content_sensitivity != right_resource.content_sensitivity
            or dict(left_resource.providers) != dict(right_resource.providers)
        ):
            return False
    return True


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
        or current.version != capability.manifest_version
        or (
            capability.manifest_version != PROVIDER_MANIFEST_VERSION
            and current.content_digest != capability.manifest_digest
        )
        or (
            capability.manifest_version == PROVIDER_MANIFEST_VERSION
            and not _provider_manifest_matches(plan.manifest, current)
        )
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
            secret_target=plan.contract_version == PROVIDER_MANIFEST_VERSION,
        )
        if parent_descriptor is not None
        else _read_target(
            observation.resource.target,
            root=_target_root_for_resource(plan.manifest, observation.resource),
            secret_target=plan.contract_version == PROVIDER_MANIFEST_VERSION,
        )
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
            _target_root_for_resource(plan.manifest, observation.resource),
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
        if write_error is not None and (
            write_error.committed or write_error.code == "recovery_required"
        ):
            raise write_error
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

    mode = (
        observation.live_mode
        if observation.live_mode is not None
        else (0o600 if plan.contract_version == PROVIDER_MANIFEST_VERSION else 0o644)
    )

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
    temporary_identity: tuple[int, int, int] | None = None
    temporary_is_symlink = False
    old_target_identity: tuple[int, int, int] | None = None
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
        temporary_identity = _entry_identity(temporary_info)
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
        old_target_identity = _entry_identity_at(parent_descriptor, target_name)
        current_temporary = os.stat(
            created_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _entry_identity(current_temporary) != temporary_identity:
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
        try:
            os.replace(
                created_name,
                target_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except (OSError, NotImplementedError) as exc:
            state, temporary_present = _classify_replace_failure(
                parent_descriptor,
                observation.resource.target.parent,
                target_name,
                created_name,
                old_target_identity,
                temporary_identity,
            )
            if temporary_present is False:
                temporary_name = None
            if state == "replaced":
                committed = True
                raise ApplyError(
                    f"target replacement for resource {observation.resource.name!r} "
                    "occurred but its outcome is not fully confirmed",
                    code="durability_unconfirmed",
                    committed=True,
                    target_name=observation.resource.target_name,
                ) from exc
            if state == "indeterminate":
                raise ApplyError(
                    f"target replacement state for resource "
                    f"{observation.resource.name!r} could not be determined",
                    code="recovery_required",
                    committed=False,
                    target_name=observation.resource.target_name,
                ) from exc
            raise ApplyError(
                f"target replacement for resource {observation.resource.name!r} "
                "did not occur",
                code="write_failed",
                committed=False,
                target_name=observation.resource.target_name,
            ) from exc
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
        if apply_error is not None and (
            apply_error.committed or apply_error.code == "recovery_required"
        ):
            raise apply_error
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


def _entry_identity(info: os.stat_result) -> tuple[int, int, int]:
    """Identify a no-follow directory entry, including its file type."""

    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _entry_identity_at(parent: int, name: str) -> tuple[int, int, int] | None:
    try:
        return _entry_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _classify_replace_failure(
    parent: int,
    parent_path: Path,
    target_name: str,
    temporary_name: str,
    old_target_identity: tuple[int, int, int] | None,
    staged_identity: tuple[int, int, int],
) -> tuple[str, bool | None]:
    """Classify an os.replace exception without inferring from content."""

    try:
        verify_directory_identity(parent, parent_path)
        target_identity = _entry_identity_at(parent, target_name)
        temporary_identity = _entry_identity_at(parent, temporary_name)
    except (FileChangedError, OSError, NotImplementedError, RuntimeError):
        return "indeterminate", None
    if target_identity == staged_identity and temporary_identity is None:
        return "replaced", False
    if target_identity == old_target_identity and temporary_identity == staged_identity:
        return "not_replaced", True
    return "indeterminate", temporary_identity is not None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
