"""Observation, explainable planning, and explicit atomic application."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .errors import ApplyError, LuwuError, RenderError
from .filesystem import (
    FileChangedError,
    NotRegularFileError,
    create_temporary_file,
    create_temporary_symlink,
    open_parent_directory,
    read_regular_file_at,
    resolve_link_target,
    sync_directory,
)
from .manifest import Manifest, Resource, load_manifest
from .rendering import render_template


class Status(StrEnum):
    """The semantic state of a declared target."""

    IN_SYNC = "in_sync"
    MISSING = "missing"
    FORMATTING = "formatting"
    DRIFTED = "drifted"
    BLOCKED = "blocked"


class Action(StrEnum):
    """The action a plan permits for one resource."""

    NOOP = "noop"
    CREATE = "create"
    REPLACE = "replace"
    BLOCK = "block"


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


@dataclass(frozen=True, slots=True)
class Plan:
    """An in-memory plan. It is intentionally not persisted by M1."""

    manifest: Manifest
    observations: tuple[ResourceObservation, ...]

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
        return not self.blocked

    def summary(self) -> dict[str, int]:
        return {
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


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """The initial plan, changed target labels, and post-write recalculation."""

    initial_plan: Plan
    changed_targets: tuple[str, ...]
    verification_plan: Plan


@dataclass(frozen=True, slots=True)
class _TargetState:
    data: bytes | None
    digest: str | None
    mode: int | None
    link_target: Path | None = None
    issue: str | None = None


def build_plan(manifest: Manifest) -> Plan:
    """Render and inspect every resource without writing any path."""

    observations: list[ResourceObservation] = []
    for resource in manifest.resources:
        if resource.kind == "template":
            observations.append(_plan_template(manifest, resource))
        else:
            observations.append(_plan_symbolic(manifest, resource))

    return Plan(manifest=manifest, observations=tuple(observations))


def _plan_template(manifest: Manifest, resource: Resource) -> ResourceObservation:
    rendered = render_template(resource, root=manifest.root)
    desired = rendered.data
    parent_issue = _target_parent_issue(resource.target, root=manifest.root)
    if parent_issue is not None:
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            reason=parent_issue,
        )

    live = _read_target(resource.target, root=manifest.root)
    if live.issue is not None or live.link_target is not None:
        reason = live.issue or "target is a symlink; refusing to replace it"
        return _blocked_observation(
            resource,
            desired_bytes=desired,
            source_digest=rendered.source_digest,
            reason=reason,
            live_digest=live.digest,
            live_mode=live.mode,
            live_link_target=live.link_target,
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
        )

    if live.data == desired:
        status = Status.IN_SYNC
        reason = "rendered template matches target"
    elif _normalize_text(live.data) == _normalize_text(desired):
        status = Status.FORMATTING
        reason = "only line endings, trailing spaces, or final newline differ"
    else:
        status = Status.DRIFTED
        reason = "rendered template differs from target"

    return ResourceObservation(
        resource=resource,
        status=status,
        action=Action.NOOP if status is not Status.DRIFTED else Action.REPLACE,
        reason=reason,
        desired_bytes=desired,
        desired_link=None,
        source_digest=rendered.source_digest,
        source_path=rendered.source_path,
        live_digest=live.digest,
        live_mode=live.mode,
        live_link_target=None,
    )


def _plan_symbolic(manifest: Manifest, resource: Resource) -> ResourceObservation:
    source_path, source_issue = _resolve_symbolic_source(resource, root=manifest.root)
    desired_link = os.path.relpath(resource.source, resource.target.parent)
    if source_issue is not None:
        return _blocked_observation(
            resource,
            desired_link=desired_link,
            source_path=source_path,
            reason=source_issue,
        )

    parent_issue = _target_parent_issue(resource.target, root=manifest.root)
    if parent_issue is not None:
        return _blocked_observation(
            resource,
            desired_link=desired_link,
            source_path=source_path,
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
    )


def apply_plan(plan: Plan) -> ApplyResult:
    """Apply a previously calculated plan after a complete stale-state check."""

    if not plan.can_apply:
        raise ApplyError(
            "plan contains blocked resources; no files were changed",
            code="plan_blocked",
        )

    _preflight_manifest(plan)
    for observation in plan.observations:
        _preflight_observation(plan, observation)

    changed_targets: list[str] = []
    for observation in plan.changes:
        _write_observation(plan, observation)
        changed_targets.append(observation.resource.target_name)

    try:
        verification_manifest = _load_current_manifest(plan)
        verification_plan = build_plan(verification_manifest)
    except ApplyError:
        raise
    except LuwuError as exc:
        raise ApplyError(
            "post-apply verification could not recalculate the current plan",
            code="post_apply_verification_failed",
        ) from exc
    if verification_plan.changes or verification_plan.blocked:
        raise ApplyError(
            "post-apply verification did not reach a stable plan",
            code="post_apply_verification_failed",
        )
    return ApplyResult(
        initial_plan=plan,
        changed_targets=tuple(changed_targets),
        verification_plan=verification_plan,
    )


def plan_to_dict(plan: Plan, *, command: str) -> dict[str, object]:
    """Serialize only metadata and explanations; never rendered content."""

    return {
        "schema_version": 1,
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
            }
            for observation in plan.observations
        ],
        "summary": plan.summary(),
    }


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
    )


def _resolve_symbolic_source(
    resource: Resource,
    *,
    root: Path,
) -> tuple[Path | None, str | None]:
    try:
        source_path = resource.source.resolve(strict=True)
        source_path.relative_to(root)
    except FileNotFoundError:
        return None, "symbolic source does not exist"
    except (OSError, RuntimeError, ValueError):
        return None, "symbolic source must stay inside the manifest directory"
    try:
        info = source_path.lstat()
    except OSError:
        return None, "symbolic source cannot be inspected"
    if not stat.S_ISREG(info.st_mode):
        return None, "symbolic source is not a regular file"
    return source_path, None


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
        try:
            info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return _TargetState(data=None, digest=None, mode=None)

        if stat.S_ISLNK(info.st_mode):
            link_text = os.readlink(name, dir_fd=parent_descriptor)
            link_target = resolve_link_target(target, link_text)
            return _TargetState(
                data=None,
                digest=None,
                mode=None,
                link_target=link_target,
            )
        if not stat.S_ISREG(info.st_mode):
            return _TargetState(
                data=None,
                digest=None,
                mode=None,
                issue="target is not a regular file",
            )

        data, current = read_regular_file_at(parent_descriptor, name)
        return _TargetState(
            data=data,
            digest=_digest(data),
            mode=stat.S_IMODE(current.st_mode),
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
        except RenderError as exc:
            raise ApplyError(
                f"source for resource {resource.name!r} changed after planning; "
                "run plan again",
                code="stale_plan",
            ) from exc
        if (
            current_rendered.source_digest != observation.source_digest
            or current_rendered.data != observation.desired_bytes
            or current_rendered.source_path != observation.source_path
        ):
            raise ApplyError(
                f"source or inputs for resource {resource.name!r} changed after planning; "
                "run plan again",
                code="stale_plan",
            )
        return

    source_path, source_issue = _resolve_symbolic_source(
        resource,
        root=plan.manifest.root,
    )
    if source_issue is not None or source_path != observation.source_path:
        raise ApplyError(
            f"source for resource {resource.name!r} changed after planning; "
            "run plan again",
            code="stale_plan",
        )


def _preflight_manifest(plan: Plan) -> None:
    try:
        if plan.manifest.path.resolve(strict=True) != plan.manifest.path:
            raise OSError("manifest path is no longer stable")
        current_digest = _digest(plan.manifest.path.read_bytes())
    except (OSError, RuntimeError) as exc:
        raise ApplyError(
            "manifest changed or became unreadable after planning; run plan again",
            code="stale_plan",
        ) from exc
    if current_digest != plan.manifest.content_digest:
        raise ApplyError(
            "manifest changed after planning; run plan again",
            code="stale_plan",
        )


def _load_current_manifest(plan: Plan) -> Manifest:
    """Reload the manifest so post-apply verification uses current inputs."""

    current = load_manifest(plan.manifest.path)
    if (
        current.path != plan.manifest.path
        or current.root != plan.manifest.root
        or current.content_digest != plan.manifest.content_digest
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
        current.digest != observation.live_digest
        or current.mode != observation.live_mode
    ):
        raise ApplyError(
            f"target for resource {observation.resource.name!r} changed {phase}; "
            "run plan again",
            code="stale_plan",
        )


def _write_observation(plan: Plan, observation: ResourceObservation) -> None:
    _preflight_manifest(plan)
    _check_target_parent(plan, observation, phase="during apply")
    _preflight_source(plan, observation)
    try:
        parent_descriptor, target_name = open_parent_directory(
            plan.manifest.root,
            observation.resource.target,
        )
    except (OSError, NotImplementedError, ValueError) as exc:
        raise ApplyError(
            f"cannot safely open target parent for resource "
            f"{observation.resource.name!r}",
            code="unsafe_target",
        ) from exc

    try:
        if observation.resource.kind == "symbolic":
            _write_symbolic_observation(
                plan, observation, parent_descriptor, target_name
            )
        else:
            _write_template_observation(
                plan,
                observation,
                parent_descriptor,
                target_name,
            )
    finally:
        os.close(parent_descriptor)


def _write_template_observation(
    plan: Plan,
    observation: ResourceObservation,
    parent_descriptor: int,
    target_name: str,
) -> None:
    if observation.desired_bytes is None:
        raise ApplyError(
            f"resource {observation.resource.name!r} has no rendered content",
            code="apply_failed",
        )

    mode = observation.live_mode if observation.live_mode is not None else 0o644
    temporary_name: str | None = None
    try:
        descriptor, created_name = create_temporary_file(
            parent_descriptor,
            prefix=f".{observation.resource.target.name}.luwu-",
        )
        temporary_name = created_name
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(observation.desired_bytes)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)

        _preflight_manifest(plan)
        _preflight_source(plan, observation)
        _check_target_state(
            plan,
            observation,
            phase="during apply",
            parent_descriptor=parent_descriptor,
        )
        os.replace(
            created_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        sync_directory(parent_descriptor)
    except ApplyError:
        raise
    except (OSError, NotImplementedError, RuntimeError, TypeError) as exc:
        raise ApplyError(
            f"cannot atomically write target for resource {observation.resource.name!r}: "
            f"{getattr(exc, 'strerror', None) or type(exc).__name__}",
            code="write_failed",
        ) from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except (OSError, NotImplementedError):
                pass


def _write_symbolic_observation(
    plan: Plan,
    observation: ResourceObservation,
    parent_descriptor: int,
    target_name: str,
) -> None:
    if observation.desired_link is None:
        raise ApplyError(
            f"resource {observation.resource.name!r} has no symbolic target",
            code="apply_failed",
        )

    temporary_name: str | None = None
    try:
        created_name = create_temporary_symlink(
            parent_descriptor,
            prefix=f".{observation.resource.target.name}.luwu-",
            target=observation.desired_link,
        )
        temporary_name = created_name
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
        _preflight_manifest(plan)
        _preflight_source(plan, observation)
        _check_target_state(
            plan,
            observation,
            phase="during apply",
            parent_descriptor=parent_descriptor,
        )
        os.replace(
            created_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        sync_directory(parent_descriptor)
    except ApplyError:
        raise
    except (OSError, NotImplementedError, RuntimeError, TypeError) as exc:
        raise ApplyError(
            f"cannot atomically write target for resource {observation.resource.name!r}: "
            f"{getattr(exc, 'strerror', None) or type(exc).__name__}",
            code="write_failed",
        ) from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except (OSError, NotImplementedError):
                pass


def _normalize_text(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    if text and not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
