"""Explicit, single-resource M3b mutations."""

from __future__ import annotations

import hashlib
import os
import stat
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
    create_temporary_file,
    lock_directory,
    open_parent_directory,
    read_regular_file_at,
    sync_directory,
    unlock_directory,
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
    _check_manifest_fresh(manifest)
    write_baseline(manifest.root, resource, data)
    verification = _verify(manifest)
    return MutationResult(
        operation="accept",
        resource=resource.name,
        fields=fields,
        write_path=resource.baseline_name or "",
        applied=True,
        outcome="committed",
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
    plan = _checked_plan(manifest, resource)
    observation = plan.observations[0]
    if observation.ownership is None:
        raise MutationError("field ownership was not observed", code="review_required")
    if observation.ownership.undeclared_changed:
        raise MutationError("undeclared content changed", code="undeclared_changed")
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
    _write_source(
        manifest.root,
        resource,
        patch,
        expected_identity=source_identity,
        expected_digest=source_digest,
    )
    verification = _verify(manifest)
    return MutationResult(
        operation="reverse-sync",
        resource=resource.name,
        fields=fields,
        write_path=resource.source_name,
        applied=True,
        outcome="committed",
        patch=patch.to_dict(),
        verification=verification,
    )


def _resource(manifest: Manifest, name: str) -> Resource:
    if manifest.version != 4:
        raise MutationError(
            "M3b mutations require manifest version 4", code="mutation_version"
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
) -> None:
    try:
        parent, name = open_parent_directory(root, resource.source)
    except (OSError, NotImplementedError, ValueError):
        raise MutationError(
            "source parent cannot be opened safely", code="unsafe_source"
        ) from None
    temporary: str | None = None
    locked = False
    try:
        lock_directory(parent)
        locked = True
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise MutationError(
                    "source is not a regular file", code="unsafe_source"
                )
            mode = stat.S_IMODE(info.st_mode)
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
        descriptor, temporary = create_temporary_file(parent, prefix=f".{name}.luwu-")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(patch.data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        sync_directory(parent)
    except MutationError:
        raise
    except (OSError, NotImplementedError) as exc:
        raise MutationError(
            "source write could not be confirmed", code="source_write_failed"
        ) from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        if locked:
            unlock_directory(parent)
        os.close(parent)
