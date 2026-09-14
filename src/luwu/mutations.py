"""Explicit, single-resource M3b mutations."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .baseline import (
    make_baseline,
    parse_public_object,
    read_baseline,
    write_baseline,
)
from .errors import MutationError
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
from .manifest import Manifest, Resource, load_manifest
from .reconcile import (
    Status,
    _read_target,
    build_plan,
    plan_to_dict,
)
from .rendering import read_source, render_template
from .reverse_sync import SourcePatch, build_source_patch


@dataclass(frozen=True, slots=True)
class MutationResult:
    operation: str
    resource: str
    fields: tuple[str, ...]
    write_path: str
    applied: bool
    outcome: str
    patch: dict[str, Any]
    verification: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
            "command": self.operation,
            "resource": self.resource,
            "fields": list(self.fields),
            "write": self.write_path,
            "applied": self.applied,
            "outcome": self.outcome,
            "patch": self.patch,
            "verification": self.verification,
        }


def accept_baseline(
    manifest: Manifest,
    *,
    resource_name: str,
    value_from: str,
    fields: tuple[str, ...],
    confirm: bool,
) -> MutationResult:
    """Preview or explicitly write selected public baseline fields."""

    resource = _resource(manifest, resource_name)
    if value_from not in {"desired", "live"}:
        raise MutationError(
            "baseline source must be desired or live", code="baseline_source"
        )
    if not fields:
        raise MutationError(
            "at least one field must be selected", code="field_required"
        )
    _validate_selected_fields(resource, fields)
    plan = _checked_plan(manifest, resource, allow_missing_baseline=True)
    observation = plan.observations[0]
    rendered = render_template(resource, root=manifest.root)
    _check_observation_fresh(
        observation, rendered.source_identity, rendered.source_digest
    )
    desired = parse_public_object(rendered.data)
    live_state = _read_target(resource.target, root=manifest.root)
    if (
        live_state.data is None
        or live_state.issue is not None
        or live_state.link_target is not None
    ):
        raise MutationError(
            "live target cannot be accepted safely", code="unsafe_target"
        )
    _check_live_fresh(observation, live_state)
    live = parse_public_object(live_state.data)
    current = desired if value_from == "desired" else live
    existing = read_baseline(manifest.root, resource)
    data = make_baseline(
        resource=resource,
        current=current,
        selected_fields=fields,
        existing=existing,
    )
    if not confirm:
        return MutationResult(
            operation="accept",
            resource=resource.name,
            fields=fields,
            write_path=resource.baseline_name or "",
            applied=False,
            outcome="confirmation_required",
            patch={
                "operation": "write_baseline",
                "from": value_from,
                "fields": list(fields),
            },
        )
    latest_rendered = render_template(resource, root=manifest.root)
    _check_observation_fresh(
        observation, latest_rendered.source_identity, latest_rendered.source_digest
    )
    latest_live_state = _read_target(resource.target, root=manifest.root)
    if (
        latest_live_state.data is None
        or latest_live_state.issue is not None
        or latest_live_state.link_target is not None
    ):
        raise MutationError(
            "live target cannot be accepted safely", code="unsafe_target"
        )
    _check_live_fresh(observation, latest_live_state)
    _check_manifest_fresh(manifest)
    write_baseline(
        manifest.root,
        resource,
        data,
        expected_data=existing,
        check_inputs=lambda: _check_accept_inputs(
            manifest,
            resource,
            observation,
            expected_live_identity=latest_live_state.identity,
            expected_live_digest=latest_live_state.digest,
            expected_live_parent_identity=latest_live_state.parent_identity,
        ),
    )
    verification, outcome = _verify_after_commit(
        manifest,
        check_inputs=lambda: _check_accept_inputs(
            manifest,
            resource,
            observation,
            expected_live_identity=latest_live_state.identity,
            expected_live_digest=latest_live_state.digest,
            expected_live_parent_identity=latest_live_state.parent_identity,
        ),
    )
    return MutationResult(
        operation="accept",
        resource=resource.name,
        fields=fields,
        write_path=resource.baseline_name or "",
        applied=True,
        outcome=outcome,
        patch={
            "operation": "write_baseline",
            "from": value_from,
            "fields": list(fields),
        },
        verification=verification,
    )


def reverse_sync(
    manifest: Manifest,
    *,
    resource_name: str,
    fields: tuple[str, ...],
    confirm: bool,
) -> MutationResult:
    """Preview or explicitly write selected live-owned fields to source JSON."""

    resource = _resource(manifest, resource_name)
    if not fields:
        raise MutationError(
            "at least one field must be selected", code="field_required"
        )
    _validate_selected_fields(resource, fields)
    plan = _checked_plan(manifest, resource)
    observation = plan.observations[0]
    if observation.status is Status.CONFLICT:
        raise MutationError(
            "resource changes require review before reverse sync",
            code="review_required",
        )
    if observation.ownership is None:
        raise MutationError("field ownership was not observed", code="review_required")
    if observation.ownership.undeclared_changed:
        raise MutationError("undeclared content changed", code="undeclared_changed")
    baseline_digest = observation.baseline_digest
    if baseline_digest is None:
        raise MutationError(
            "baseline is required for reverse sync", code="review_required"
        )
    ownership = {field.name: field for field in observation.ownership.fields}
    for name in fields:
        item = ownership.get(name)
        if (
            item is None
            or item.status != "live_changed"
            or item.decision != "reverse_candidate"
        ):
            raise MutationError("field changes require review", code="review_required")
    _, source, source_identity = read_source(resource, root=manifest.root)
    source_digest = hashlib.sha256(source).hexdigest()
    _check_observation_fresh(observation, source_identity, source_digest)
    live_state = _read_target(resource.target, root=manifest.root)
    if live_state.data is None:
        raise MutationError("live target cannot be read safely", code="unsafe_target")
    if live_state.identity is None:
        raise MutationError("live target identity is unavailable", code="unsafe_target")
    live_identity = live_state.identity
    _check_live_fresh(observation, live_state)
    patch = build_source_patch(
        source,
        live_state.data,
        fields=resource.fields,
        owners=resource.fields,
        reverse_sync=resource.reverse_sync,
        selected_fields=fields,
    )
    if not confirm:
        return MutationResult(
            operation="reverse-sync",
            resource=resource.name,
            fields=fields,
            write_path=resource.source_name,
            applied=False,
            outcome="confirmation_required",
            patch=patch.to_dict(),
        )
    _check_manifest_fresh(manifest)
    check_inputs = lambda: _check_reverse_sync_inputs(
        manifest,
        resource,
        expected_baseline_digest=baseline_digest,
        expected_live_identity=live_identity,
        expected_live_digest=live_state.digest,
        expected_live_parent_identity=live_state.parent_identity,
    )
    _write_source(
        manifest.root,
        resource,
        patch,
        expected_identity=source_identity,
        expected_digest=source_digest,
        check_inputs=check_inputs,
    )
    verification, outcome = _verify_after_commit(
        manifest,
        check_inputs=check_inputs,
    )
    return MutationResult(
        operation="reverse-sync",
        resource=resource.name,
        fields=fields,
        write_path=resource.source_name,
        applied=True,
        outcome=outcome,
        patch=patch.to_dict(),
        verification=verification,
    )


def _resource(manifest: Manifest, name: str) -> Resource:
    if manifest.version != 4:
        raise MutationError(
            "M3b mutations require manifest version 4", code="mutation_version"
        )
    if len(manifest.resources) != 1:
        raise MutationError(
            "M3b mutations require exactly one declared resource",
            code="resource_count",
        )
    matches = [resource for resource in manifest.resources if resource.name == name]
    if len(matches) != 1:
        raise MutationError(
            "resource is required and must be declared", code="resource_required"
        )
    return matches[0]


def _checked_plan(
    manifest: Manifest,
    resource: Resource,
    *,
    allow_missing_baseline: bool = False,
):
    plan = build_plan(manifest)
    observation = next(
        item for item in plan.observations if item.resource.name == resource.name
    )
    if observation.status is Status.BLOCKED and not (
        allow_missing_baseline and observation.reason == "baseline does not exist"
    ):
        raise MutationError("resource is blocked", code="plan_blocked")
    return plan


def _verify(manifest: Manifest) -> dict[str, Any]:
    current = load_manifest(manifest.path)
    return plan_to_dict(build_plan(current), command="verification")


def _verify_after_commit(
    manifest: Manifest,
    *,
    check_inputs: Callable[[], None] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Keep a known write distinct from an unavailable post-write check."""

    try:
        if check_inputs is not None:
            check_inputs()
        return _verify(manifest), "committed"
    except Exception:  # noqa: BLE001 - the write already happened
        return {"error": "post_write_verification_failed"}, (
            "committed_but_verification_failed"
        )


def _validate_selected_fields(resource: Resource, fields: tuple[str, ...]) -> None:
    if len(set(fields)) != len(fields):
        raise MutationError(
            "fields must be selected at most once", code="field_duplicate"
        )
    for name in fields:
        if name not in resource.fields:
            raise MutationError("field is not declared", code="field_not_declared")


def _check_manifest_fresh(manifest: Manifest) -> None:
    current = load_manifest(manifest.path)
    if current.content_digest != manifest.content_digest:
        raise MutationError(
            "manifest changed; run the mutation again", code="stale_plan"
        )


def _check_observation_fresh(
    observation: Any,
    source_identity: tuple[int, int],
    source_digest: str,
) -> None:
    if (
        observation.source_identity != source_identity
        or observation.source_digest != source_digest
    ):
        raise MutationError("source changed; run the mutation again", code="stale_plan")


def _check_live_fresh(observation: Any, live_state: Any) -> None:
    if (
        observation.live_identity != live_state.identity
        or observation.live_digest != live_state.digest
        or (
            observation.target_parent_identity is not None
            and observation.target_parent_identity != live_state.parent_identity
        )
    ):
        raise MutationError(
            "live target changed; run the mutation again", code="stale_plan"
        )


def _write_source(
    root: Path,
    resource: Resource,
    patch: SourcePatch,
    *,
    expected_identity: tuple[int, int],
    expected_digest: str,
    check_inputs: Callable[[], None],
) -> None:
    try:
        parent, name = open_parent_directory(root, resource.source)
    except (OSError, NotImplementedError, ValueError):
        raise MutationError(
            "source parent cannot be opened safely", code="unsafe_source"
        ) from None
    temporary: str | None = None
    locked = False
    committed = False
    replace_attempted = False
    cleanup_error: MutationError | None = None
    pending_error: BaseException | None = None
    try:
        lock_directory(parent)
        locked = True
        _verify_source_parent(parent, resource.source.parent)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise MutationError(
                    "source is not a regular file", code="unsafe_source"
                )
            mode = stat.S_IMODE(info.st_mode)
            old_identity = _entry_identity(info)
        except FileNotFoundError:
            raise MutationError("source does not exist", code="unsafe_source") from None
        if (info.st_dev, info.st_ino) != expected_identity:
            raise MutationError(
                "source changed; run the mutation again", code="stale_plan"
            )
        current, _ = read_regular_file_at(parent, name)
        if hashlib.sha256(current).hexdigest() != expected_digest:
            raise MutationError(
                "source changed; run the mutation again", code="stale_plan"
            )
        check_inputs()
        descriptor, temporary = create_temporary_file(parent, prefix=f".{name}.luwu-")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(patch.data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        temporary_name = temporary
        if temporary_name is None:
            raise MutationError(
                "source temporary entry was not created", code="source_write_failed"
            )
        staged_identity = _entry_identity_at(parent, temporary_name)
        if staged_identity is None:
            raise MutationError(
                "source temporary entry disappeared", code="source_write_failed"
            )
        _check_source_current(parent, name, expected_identity, expected_digest)
        _verify_source_parent(parent, resource.source.parent)
        check_inputs()
        replace_attempted = True
        try:
            os.replace(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
        except (OSError, NotImplementedError) as exc:
            state, temporary_present = _classify_replace_failure(
                parent,
                resource.source.parent,
                name,
                temporary_name,
                old_identity,
                staged_identity,
            )
            if temporary_present is False:
                temporary = None
            if state == "replaced":
                committed = True
                raise MutationError(
                    "source replacement occurred but its outcome is not fully confirmed",
                    code="source_state_unknown",
                    committed=True,
                    outcome="committed_state_unknown",
                ) from exc
            if state == "indeterminate":
                raise MutationError(
                    "source replacement state could not be determined",
                    code="source_state_unknown",
                    committed=False,
                    outcome="indeterminate",
                ) from exc
            raise MutationError(
                "source replacement did not occur",
                code="source_write_failed",
                committed=False,
                outcome="not_committed",
            ) from exc
        committed = True
        temporary = None
        try:
            _verify_source_parent(parent, resource.source.parent)
            check_inputs()
        except Exception as exc:
            # The replacement succeeded; any input recheck failure is state unknown.
            raise MutationError(
                "source commit state could not be confirmed",
                code="source_state_unknown",
                committed=True,
                outcome="committed_state_unknown",
            ) from exc
        sync_directory(parent)
    except MutationError as exc:
        pending_error = exc
    except (OSError, NotImplementedError) as exc:
        pending_error = MutationError(
            "source write could not be confirmed",
            code=(
                "source_state_unknown"
                if replace_attempted and committed
                else "source_write_failed"
            ),
            committed=committed,
            outcome=(
                "committed_state_unknown"
                if replace_attempted and committed
                else "not_committed"
            ),
        )
        pending_error.__cause__ = exc
    except Exception as exc:  # noqa: BLE001 - retain the original write failure
        pending_error = exc
    finally:
        if temporary is not None:
            try:
                _remove_source_temporary(parent, temporary)
            except OSError as exc:
                cleanup_error = MutationError(
                    "source temporary entry could not be cleaned up",
                    code="cleanup_failed",
                    committed=committed,
                    outcome=(
                        "committed_state_unknown" if committed else "not_committed"
                    ),
                )
                cleanup_error.__cause__ = exc
        if locked:
            try:
                unlock_directory(parent)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = MutationError(
                        "source directory unlock could not be confirmed",
                        code="cleanup_failed",
                        committed=committed,
                        outcome=(
                            "committed_state_unknown" if committed else "not_committed"
                        ),
                    )
                    cleanup_error.__cause__ = exc
        try:
            os.close(parent)
        except OSError as exc:
            if cleanup_error is None:
                cleanup_error = MutationError(
                    "source parent close could not be confirmed",
                    code="cleanup_failed",
                    committed=committed,
                    outcome=(
                        "committed_state_unknown" if committed else "not_committed"
                    ),
                )
                cleanup_error.__cause__ = exc

    if cleanup_error is not None:
        if replace_attempted:
            pending_error = MutationError(
                "source write state could not be fully confirmed",
                code="source_state_unknown",
                committed=committed,
                outcome=("committed_state_unknown" if committed else "indeterminate"),
            )
            pending_error.__cause__ = cleanup_error
        else:
            pending_error = cleanup_error
    if pending_error is not None:
        raise pending_error


def _check_accept_inputs(
    manifest: Manifest,
    resource: Resource,
    observation: Any,
    *,
    expected_live_identity: tuple[int, int] | None,
    expected_live_digest: str | None,
    expected_live_parent_identity: tuple[int, int] | None,
) -> None:
    rendered = render_template(resource, root=manifest.root)
    _check_observation_fresh(
        observation, rendered.source_identity, rendered.source_digest
    )
    live_state = _read_target(resource.target, root=manifest.root)
    if (
        live_state.data is None
        or live_state.issue is not None
        or live_state.link_target is not None
    ):
        raise MutationError("live target cannot be accepted safely", code="stale_plan")
    if (
        live_state.identity != expected_live_identity
        or live_state.digest != expected_live_digest
        or live_state.parent_identity != expected_live_parent_identity
    ):
        raise MutationError(
            "live target changed; run the mutation again", code="stale_plan"
        )
    _check_manifest_fresh(manifest)


def _check_source_current(
    parent: int,
    name: str,
    expected_identity: tuple[int, int],
    expected_digest: str,
) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        current, _ = read_regular_file_at(parent, name)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise MutationError(
            "source changed; run the mutation again", code="stale_plan"
        ) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_dev, info.st_ino) != expected_identity
        or hashlib.sha256(current).hexdigest() != expected_digest
    ):
        raise MutationError("source changed; run the mutation again", code="stale_plan")


def _entry_identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _remove_source_temporary(parent: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


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
    old_identity: tuple[int, int, int],
    staged_identity: tuple[int, int, int],
) -> tuple[str, bool | None]:
    """Classify a failed replace without inferring from bytes alone."""

    try:
        _verify_source_parent(parent, parent_path)
        target_identity = _entry_identity_at(parent, target_name)
        temporary_identity = _entry_identity_at(parent, temporary_name)
    except (MutationError, OSError, NotImplementedError):
        return "indeterminate", None
    if target_identity == staged_identity and temporary_identity is None:
        return "replaced", False
    if target_identity == old_identity and temporary_identity == staged_identity:
        return "not_replaced", True
    return "indeterminate", temporary_identity is not None


def _check_live_snapshot(
    resource: Resource,
    *,
    root: Path,
    expected_identity: tuple[int, int],
    expected_digest: str | None,
    expected_parent_identity: tuple[int, int] | None,
) -> None:
    live_state = _read_target(resource.target, root=root)
    if (
        live_state.data is None
        or live_state.issue is not None
        or live_state.link_target is not None
        or live_state.identity != expected_identity
        or live_state.digest != expected_digest
        or live_state.parent_identity != expected_parent_identity
    ):
        raise MutationError(
            "live target changed; run the mutation again", code="stale_plan"
        )


def _check_reverse_sync_inputs(
    manifest: Manifest,
    resource: Resource,
    *,
    expected_baseline_digest: str,
    expected_live_identity: tuple[int, int],
    expected_live_digest: str | None,
    expected_live_parent_identity: tuple[int, int] | None,
) -> None:
    try:
        current_baseline = read_baseline(manifest.root, resource)
    except MutationError as exc:
        raise MutationError(
            "baseline changed; run the mutation again", code="stale_plan"
        ) from exc
    current_baseline_digest = (
        None
        if current_baseline is None
        else hashlib.sha256(current_baseline).hexdigest()
    )
    if current_baseline_digest != expected_baseline_digest:
        raise MutationError(
            "baseline changed; run the mutation again", code="stale_plan"
        )
    _check_manifest_fresh(manifest)
    _check_live_snapshot(
        resource,
        root=manifest.root,
        expected_identity=expected_live_identity,
        expected_digest=expected_live_digest,
        expected_parent_identity=expected_live_parent_identity,
    )


def _verify_source_parent(parent: int, path: Path) -> None:
    try:
        verify_directory_identity(parent, path)
    except (FileChangedError, OSError, NotImplementedError) as exc:
        raise MutationError(
            "source parent changed; run the mutation again", code="stale_plan"
        ) from exc
