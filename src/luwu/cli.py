"""The user-facing CLI, kept separate from manifest and filesystem logic."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from . import __version__, provider_cache
from .errors import LuwuError, MutationError
from .manifest import is_execution_manifest, is_provider_manifest, load_manifest
from .mutations import accept_baseline, reverse_sync
from .platform_support import probe_platform
from .providers import (
    ProviderAuthority,
    inspect_executable_identity,
)
from .reconcile import (
    ApplyOutcome,
    ApplyResult,
    ExecutionResult,
    Plan,
    ResourceObservation,
    apply_plan,
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
    plan_to_dict,
    reobserve_execution_record,
)

_EXECUTION_RESOURCE_STATES = frozenset(
    {"unchanged", "committed", "failed", "unknown", "not-attempted"}
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="luwu",
        description="Explain and explicitly apply declared configuration resources.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("inspect", "observe declared resources without writing"),
        ("plan", "show the actions an explicit apply could take"),
    ):
        command = commands.add_parser(name, help=help_text)
        _add_common_arguments(command)

    apply = commands.add_parser(
        "apply", help="apply a plan after explicit confirmation"
    )
    _add_common_arguments(apply)
    apply.add_argument(
        "--yes",
        action="store_true",
        help="confirm the calculated plan and permit atomic target writes",
    )
    apply.add_argument(
        "--record",
        type=Path,
        help="version-5/version-6 execution journal path (required with --yes)",
    )

    record_inspect = commands.add_parser(
        "record-inspect",
        aliases=("inspect-record",),
        help="inspect a version-5/version-6 execution journal without writing",
    )
    record_inspect.add_argument(
        "--record", type=Path, required=True, help="execution journal path"
    )
    record_inspect.add_argument(
        "--json",
        action="store_true",
        help="emit metadata-only JSON for agents and scripts",
    )

    recover = commands.add_parser(
        "recover",
        aliases=("record-reobserve",),
        help="re-observe a version-5/version-6 execution journal without writing",
    )
    recover.add_argument(
        "--record", type=Path, required=True, help="execution journal path"
    )
    recover.add_argument(
        "--json",
        action="store_true",
        help="emit metadata-only JSON for agents and scripts",
    )
    _add_authority_arguments(recover)

    cache_inspect = commands.add_parser(
        "cache-inspect",
        help="inspect the explicit provider metadata cache without writing",
    )
    cache_inspect.add_argument("--cache", type=Path, required=True)
    cache_inspect.add_argument("--rbw-executable", type=Path)
    cache_inspect.add_argument("--json", action="store_true")

    cache_refresh = commands.add_parser(
        "cache-refresh",
        help="explicitly write the provider metadata cache",
    )
    cache_refresh.add_argument("--cache", type=Path, required=True)
    cache_refresh.add_argument("--rbw-executable", type=Path, required=True)
    cache_refresh.add_argument("--status", default="ok")
    cache_refresh.add_argument("--ttl", type=float, default=300.0)
    cache_refresh.add_argument("--json", action="store_true")

    platform_check = commands.add_parser(
        "platform-check", help="diagnose the supported provider/write platform"
    )
    platform_check.add_argument("--json", action="store_true")

    accept = commands.add_parser(
        "accept", help="explicitly accept selected public baseline fields"
    )
    _add_common_arguments(accept)
    accept.add_argument("--resource", required=True)
    accept.add_argument(
        "--from", dest="value_from", choices=("desired", "live"), required=True
    )
    accept.add_argument("--field", action="append", required=True)
    accept.add_argument("--yes", action="store_true")

    reverse = commands.add_parser(
        "reverse-sync", help="explicitly write selected live-owned fields to source"
    )
    _add_common_arguments(reverse)
    reverse.add_argument("--resource", required=True)
    reverse.add_argument("--field", action="append", required=True)
    reverse.add_argument("--yes", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "platform-check":
            return _run_platform_check(as_json=args.json)

        if args.command == "cache-inspect":
            return _run_cache_inspect(args)

        if args.command == "cache-refresh":
            return _run_cache_refresh(args)

        if args.command in {"record-inspect", "inspect-record"}:
            record = inspect_execution_record(args.record)
            _emit_record_inspection(record, record_path=args.record, as_json=args.json)
            return 0

        if args.command in {"recover", "record-reobserve"}:
            authority = _authority_from_args(args)
            result = reobserve_execution_record(args.record, authority=authority)
            _emit_reobserve_result(result, record_path=args.record, as_json=args.json)
            return (
                0
                if result.get("outcome") in {"confirmed", "currently_converged"}
                else 2
            )

        manifest = load_manifest(args.manifest)
        authority = _authority_from_args(args)
        if args.command in {"accept", "reverse-sync"}:
            if is_provider_manifest(manifest):
                raise LuwuError(
                    "version 6 provider resources do not support public mutations",
                    code="mutation_unsupported",
                )
            try:
                if args.command == "accept":
                    result = accept_baseline(
                        manifest,
                        resource_name=args.resource,
                        value_from=args.value_from,
                        fields=tuple(args.field),
                        confirm=args.yes,
                    )
                else:
                    result = reverse_sync(
                        manifest,
                        resource_name=args.resource,
                        fields=tuple(args.field),
                        confirm=args.yes,
                    )
            except MutationError as exc:
                exc.attach_context(
                    operation=args.command,
                    resource=args.resource,
                    fields=tuple(args.field),
                    write_path=_mutation_write_path(
                        manifest,
                        operation=args.command,
                        resource_name=args.resource,
                    ),
                )
                raise
            if args.json:
                _print_json(result.to_dict())
            else:
                print(f"{result.operation}: {result.outcome}")
                print(f"Resource: {_display(result.resource)}")
                print(f"Fields: {len(result.fields)}")
                print(f"Write: {_display(result.write_path)}")
            return 0 if args.yes and result.outcome == "committed" else 2
        if (
            args.command == "apply"
            and (is_execution_manifest(manifest) or is_provider_manifest(manifest))
            and args.yes
            and getattr(args, "record", None) is None
        ):
            raise LuwuError(
                f"version {manifest.version} apply with --yes requires explicit --record PATH",
                code="record_required",
            )

        plan = build_plan(manifest, authority=authority)
        if args.command in {"inspect", "plan"}:
            _emit_plan(plan, command=args.command, as_json=args.json)
            return 0

        if is_execution_manifest(manifest) or is_provider_manifest(manifest):
            record_path = (
                getattr(args, "record", None) or manifest.root / ".luwu-preview.journal"
            )
            if is_provider_manifest(manifest) and plan.blocked:
                if args.yes:
                    _emit_apply_blocked(plan, as_json=args.json)
                else:
                    _emit_apply_preview(plan, as_json=args.json)
                return 2
            try:
                result = execute_execution_plan(
                    plan,
                    record_path,
                    confirm=args.yes,
                    authority=authority,
                )
            except LuwuError as exc:
                _emit_execution_error(exc, record_path=record_path, as_json=args.json)
                return 2
            _emit_execution_result(
                result,
                record_path=record_path,
                as_json=args.json,
            )
            return 0 if args.yes else 2

        if not args.yes:
            _emit_apply_preview(plan, as_json=args.json)
            if not args.json:
                print(
                    "No files changed. Re-run with --yes after reviewing this plan.",
                    file=sys.stderr,
                )
            return 2

        if not plan.can_apply:
            _emit_apply_blocked(plan, as_json=args.json)
            if not args.json:
                reason = plan.apply_block_reason
                if reason == "m2_read_only":
                    print(
                        "No files changed because manifest version 2 is read-only in M2.",
                        file=sys.stderr,
                    )
                elif reason == "m3_read_only":
                    version = plan.contract_version
                    milestone = "M3a" if version == 3 else "M3b"
                    print(
                        f"No files changed because manifest version {version} "
                        f"is read-only in {milestone}.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "No files changed because the plan is blocked.",
                        file=sys.stderr,
                    )
            return 2

        if not args.json:
            _print_human_plan(plan, heading="Apply plan")
        result = apply_plan(plan)
        _emit_apply_result(result, as_json=args.json)
        return (
            0
            if result.outcome in {ApplyOutcome.COMMITTED, ApplyOutcome.NO_CHANGES}
            else 2
        )
    except LuwuError as exc:
        _emit_error(exc, as_json=getattr(args, "json", False))
        return 2
    except Exception:  # noqa: BLE001 - preserve the CLI error boundary
        _emit_error(
            LuwuError(
                "operation failed; inspect the current state before retrying",
                code="internal_error",
            ),
            as_json=getattr(args, "json", False),
        )
        return 2


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("luwu.toml"),
        help="manifest path (default: luwu.toml)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit metadata-only JSON for agents and scripts",
    )
    _add_authority_arguments(parser)


def _add_authority_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-subprocess",
        action="store_true",
        help="grant the version-6 provider subprocess capability for this call",
    )
    parser.add_argument(
        "--rbw-executable",
        type=Path,
        help="absolute rbw executable used when subprocess capability is granted",
    )


def _authority_from_args(args: argparse.Namespace) -> ProviderAuthority | None:
    executable = getattr(args, "rbw_executable", None)
    allow_subprocess = bool(getattr(args, "allow_subprocess", False))
    if executable is not None and not allow_subprocess:
        raise LuwuError(
            "--rbw-executable requires --allow-subprocess",
            code="capability_required",
        )
    if not allow_subprocess:
        return None
    if executable is None or not executable.is_absolute():
        raise LuwuError(
            "--allow-subprocess requires an absolute --rbw-executable PATH",
            code="provider_executable_required",
        )
    return ProviderAuthority(executable, {"subprocess"})


def _run_platform_check(*, as_json: bool) -> int:
    status = probe_platform()
    if as_json:
        _print_json(status.to_dict())
    else:
        print(f"Supported: {str(status.supported).lower()}")
        print(f"System: {_display(status.system)}")
        print(f"Machine: {_display(status.machine)}")
        print(f"Python: {_display(status.to_dict()['python'])}")
        if status.missing:
            print(f"Missing: {', '.join(_display(item) for item in status.missing)}")
    return 0 if status.supported else 2


def _cache_identity(path: Path) -> provider_cache.ExecutableIdentity:
    identity = inspect_executable_identity(path)
    return provider_cache.ExecutableIdentity(
        device=identity.device,
        inode=identity.inode,
        mode=identity.mode,
        size=identity.size,
        mtime_ns=identity.mtime_ns,
    )


def _run_cache_inspect(args: argparse.Namespace) -> int:
    executable = getattr(args, "rbw_executable", None)
    identity = _cache_identity(executable) if executable is not None else None
    result = provider_cache.inspect_cache(
        args.cache,
        executable_identity=identity,
    )
    if args.json:
        _print_json(result.to_dict())
    else:
        print(f"Cache: {_display(args.cache)}")
        print(f"Status: {_display(result.status)}")
        if result.entry is not None:
            print(f"Provider: {_display(result.entry.provider_type)}")
            print(f"Observed: {_display(result.entry.observed_at)}")
            print(f"Expires: {_display(result.entry.expires_at)}")
    return 0 if result.status == "fresh" else 2


def _run_cache_refresh(args: argparse.Namespace) -> int:
    identity = _cache_identity(args.rbw_executable)
    result = provider_cache.refresh_cache(
        args.cache,
        provider_type=provider_cache.CACHE_PROVIDER_TYPE,
        capabilities=provider_cache.CACHE_CAPABILITIES,
        executable_identity=identity,
        status=args.status,
        ttl_seconds=args.ttl,
    )
    if args.json:
        _print_json(result.to_dict())
    else:
        print(f"Cache refreshed: {_display(args.cache)}")
        print(f"Status: {_display(result.entry.status)}")
        print(f"Durability: {str(result.durability_confirmed).lower()}")
    return 0 if result.durability_confirmed else 2


def _emit_plan(plan: Plan, *, command: str, as_json: bool) -> None:
    if as_json:
        _print_json(plan_to_dict(plan, command=command))
        return
    _print_human_plan(plan, heading="Inspection" if command == "inspect" else "Plan")


def _emit_apply_preview(plan: Plan, *, as_json: bool) -> None:
    if as_json:
        payload = plan_to_dict(plan, command="apply")
        payload.update(
            {
                "applied": False,
                "reason": (
                    plan.apply_block_reason
                    if plan.apply_block_reason is not None
                    else "confirmation_required"
                ),
            }
        )
        _print_json(payload)
        return
    _print_human_plan(plan, heading="Apply preview")


def _emit_apply_blocked(plan: Plan, *, as_json: bool) -> None:
    if as_json:
        payload = plan_to_dict(plan, command="apply")
        payload.update(
            {
                "applied": False,
                "reason": plan.apply_block_reason or "plan_blocked",
            }
        )
        _print_json(payload)
        return
    _print_human_plan(plan, heading="Apply blocked")


def _emit_apply_result(result: ApplyResult, *, as_json: bool) -> None:
    if as_json:
        payload = plan_to_dict(result.initial_plan, command="apply")
        payload.update(
            {
                "applied": result.outcome is not ApplyOutcome.VERIFICATION_FAILED,
                "mutated": bool(result.changed_targets),
                "outcome": result.outcome.value,
                "changed_targets": list(result.changed_targets),
                "verification": (
                    plan_to_dict(result.verification_plan, command="verification")
                    if result.verification_plan is not None
                    else None
                ),
                "verification_error": result.verification_error,
            }
        )
        _print_json(payload)
        return

    if result.outcome is ApplyOutcome.NO_CHANGES:
        print("No files changed")
    elif result.outcome is ApplyOutcome.COMMITTED:
        print("Applied")
    elif result.outcome is ApplyOutcome.COMMITTED_BUT_VERIFICATION_FAILED:
        print("Applied, but verification failed")
    elif result.outcome is ApplyOutcome.VERIFICATION_FAILED:
        print("No files changed, but verification failed")
    else:
        print("Applied, but the final state is not fully confirmed")
    print(f"Changed targets: {len(result.changed_targets)}")
    if result.outcome in {ApplyOutcome.COMMITTED, ApplyOutcome.NO_CHANGES}:
        print("Verification: clean")
    elif result.outcome in {
        ApplyOutcome.COMMITTED_BUT_VERIFICATION_FAILED,
        ApplyOutcome.VERIFICATION_FAILED,
    }:
        print("Verification: unavailable or not clean; inspect before retrying")
    else:
        print(
            "Verification: durability or cleanup is unconfirmed; inspect before retrying"
        )


def _emit_execution_result(
    result: ExecutionResult, *, record_path: Path, as_json: bool
) -> None:
    target_names = _execution_target_names(result.preview)
    state = result.record_state or "preview"
    outcome = "preview" if result.record is None else state
    if as_json:
        journal = (
            _execution_journal_metadata(
                result.record.to_dict(), record_path=record_path
            )
            if result.record is not None
            else {"path": str(record_path), "created": False}
        )
        if result.record is not None:
            journal["created"] = True
        _print_json(
            {
                "preview": result.preview,
                "journal": journal,
                "target_names": target_names,
                "state": state,
                "outcome": outcome,
                "changed_targets": list(result.changed_targets),
            }
        )
        return

    print("Execution" if result.record is not None else "Execution preview")
    print(f"Targets: {len(target_names)}")
    for target_name in target_names:
        print(f"- {_display(target_name)}")
    print(f"State: {_display(state)}")
    print(f"Outcome: {_display(outcome)}")
    if result.record is not None:
        print(f"Journal: {_display(record_path)}")


def _emit_record_inspection(
    record: dict[str, object], *, record_path: Path, as_json: bool
) -> None:
    target_names = _record_target_names(record)
    state = str(record["state"])
    journal = _execution_journal_metadata(record, record_path=record_path)
    if as_json:
        _print_json(
            {
                "journal": journal,
                "target_names": target_names,
                "state": state,
                "outcome": state,
            }
        )
        return

    print(f"Journal: {_display(record_path)}")
    print(f"State: {_display(state)}")
    print(f"Outcome: {_display(state)}")
    print(f"Targets: {len(target_names)}")


def _emit_reobserve_result(
    result: dict[str, object], *, record_path: Path, as_json: bool
) -> None:
    """Emit only the metadata returned by the read-only re-observation API."""

    outcome = str(result.get("outcome", "recovery_required"))
    payload = {
        "journal": {"path": str(record_path)},
        "plan_id": result.get("plan_id"),
        "record_state": result.get("record_state"),
        "outcome": outcome,
        "resources": result.get("resources", []),
    }
    for key in ("schema_version", "command", "mode", "reason"):
        if key in result:
            payload[key] = result[key]

    if as_json:
        _print_json(payload)
        return

    print("Recovery re-observation")
    print(f"Journal: {_display(record_path)}")
    print(f"Plan ID: {_display(result.get('plan_id'))}")
    print(f"State: {_display(result.get('record_state'))}")
    print(f"Outcome: {_display(outcome)}")
    if "reason" in result:
        print(f"Reason: {_display(result['reason'])}")
    resources = result.get("resources", [])
    if isinstance(resources, list):
        print(f"Resources: {len(resources)}")
        for ordinal, resource in enumerate(resources):
            if not isinstance(resource, dict):
                continue
            label = resource.get("name", resource.get("label", f"resource-{ordinal}"))
            print(
                f"- {_display(label)}: "
                f"record={_display(resource.get('record_state'))}; "
                f"reobserved={_display(resource.get('reobserved_state'))}"
            )


def _emit_execution_error(
    error: LuwuError, *, record_path: Path, as_json: bool
) -> None:
    journal: dict[str, object] = {
        "path": str(record_path),
        "created": None,
    }
    try:
        record_exists = record_path.exists()
        journal["created"] = record_exists
        if record_exists:
            record = inspect_execution_record(record_path)
            journal.update(_execution_journal_metadata(record, record_path=record_path))
    except Exception:  # noqa: BLE001 - diagnostics must not mask execution facts
        journal["state"] = "unreadable"
    execution = _execution_error_metadata(error)
    if as_json:
        payload: dict[str, object] = {
            "error": {"code": error.code, "message": str(error)},
            "journal": journal,
        }
        if execution is not None:
            payload["execution"] = execution
        _print_json(payload)
        return
    print(f"error[{error.code}]: {_display(error)}", file=sys.stderr)
    if journal["created"] or journal.get("state") == "unreadable":
        print(
            f"Journal: {_display(record_path)}; "
            f"state: {_display(journal.get('state', 'unreadable'))}",
            file=sys.stderr,
        )
    if execution is not None:
        _emit_human_execution_error(execution)


def _execution_error_metadata(error: LuwuError) -> dict[str, object] | None:
    """Serialize a complete fixed execution context without guessing values."""

    raw_execution = getattr(error, "execution", None)
    if not isinstance(raw_execution, dict):
        return None

    plan_id = raw_execution.get("plan_id")
    changed_targets = raw_execution.get("changed_targets")
    raw_resources = raw_execution.get("resources")
    committed = raw_execution.get("committed")
    v6_resources = isinstance(raw_resources, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("ordinal"), int)
        and not isinstance(item.get("ordinal"), bool)
        and isinstance(item.get("state"), str)
        and item.get("state") in _EXECUTION_RESOURCE_STATES
        for item in raw_resources
    )
    v5_resources = isinstance(raw_resources, list) and all(
        _is_execution_resource(item) for item in raw_resources
    )
    if not (
        (isinstance(plan_id, str) or plan_id is None)
        and type(committed) is bool
        and isinstance(changed_targets, list)
        and all(isinstance(target, str) for target in changed_targets)
        and isinstance(raw_resources, list)
        and (v5_resources or v6_resources)
        and committed == bool(changed_targets)
        and getattr(error, "committed", None) == committed
    ):
        return None

    normalized_targets = cast(list[str], changed_targets)
    if v6_resources:
        normalized_resources = [
            {
                "ordinal": cast(int, resource["ordinal"]),
                "state": cast(str, resource["state"]),
            }
            for resource in cast(list[dict[str, object]], raw_resources)
        ]
    else:
        normalized_resources = [
            {
                "name": cast(str, resource["name"]),
                "target": cast(str, resource["target"]),
                "state": cast(str, resource["state"]),
            }
            for resource in cast(list[dict[str, object]], raw_resources)
        ]
    return {
        "plan_id": plan_id if isinstance(plan_id, str) else None,
        "committed": committed,
        "changed_targets": normalized_targets,
        "resources": normalized_resources,
    }


def _is_execution_resource(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    name = value.get("name")
    target = value.get("target")
    state = value.get("state")
    return (
        isinstance(name, str)
        and isinstance(target, str)
        and isinstance(state, str)
        and state in _EXECUTION_RESOURCE_STATES
    )


def _emit_human_execution_error(execution: dict[str, object]) -> None:
    """Make cumulative target facts and per-resource states visible to a user."""

    print(
        f"Execution: plan_id={_display(execution['plan_id'])}; "
        f"committed={str(execution['committed']).lower()}",
        file=sys.stderr,
    )
    changed_targets = cast(list[str], execution["changed_targets"])
    print(f"Changed targets: {len(changed_targets)}", file=sys.stderr)
    for target in changed_targets:
        print(f"- {_display(target)}", file=sys.stderr)
    resources = cast(list[dict[str, str]], execution["resources"])
    print(f"Resource states: {len(resources)}", file=sys.stderr)
    for resource in resources:
        if "ordinal" in resource:
            print(
                f"- resource-{_display(resource['ordinal'])}: "
                f"state={_display(resource['state'])}",
                file=sys.stderr,
            )
        else:
            print(
                f"- {_display(resource['name'])}: "
                f"target={_display(resource['target'])}; "
                f"state={_display(resource['state'])}",
                file=sys.stderr,
            )


def _execution_target_names(preview: dict[str, object]) -> list[str]:
    resources = cast(list[object], preview.get("resources", []))
    return [
        str(resource.get("target", resource.get("label", "")))
        for resource in resources
        if isinstance(resource, dict) and ("target" in resource or "label" in resource)
    ]


def _record_target_names(record: dict[str, object]) -> list[str]:
    target_names: list[str] = []
    resources = record.get("resources", [])
    if not isinstance(resources, list):
        return target_names
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if "label" in resource:
            target_names.append(str(resource["label"]))
            continue
        paths = resource.get("paths", [])
        if not isinstance(paths, list):
            continue
        for path in paths:
            if isinstance(path, dict) and path.get("role") == "target":
                target_names.append(str(path["path"]))
    return target_names


def _execution_journal_metadata(
    record: dict[str, object], *, record_path: Path
) -> dict[str, object]:
    manifest = cast(dict[str, object], record["manifest"])
    policy = cast(dict[str, object], record["policy"])
    resources = cast(list[dict[str, object]], record["resources"])
    if manifest["version"] == 6:
        resource_metadata = [
            {"label": resource["label"], "state": resource["state"]}
            for resource in resources
        ]
    else:
        resource_metadata = [
            {"name": resource["name"], "state": resource["state"]}
            for resource in resources
        ]
    return {
        "path": str(record_path),
        "record_schema_version": record["record_schema_version"],
        "plan_id": record["plan_id"],
        "execution_contract": record["execution_contract"],
        "mutation_contract": record["mutation_contract"],
        "manifest_version": manifest["version"],
        "on_failure": policy["on_failure"],
        "rollback": policy["rollback"],
        "state": record["state"],
        "resources": resource_metadata,
    }


def _print_human_plan(plan: Plan, *, heading: str) -> None:
    print(heading)
    print(f"Manifest: {_display(plan.manifest.path)}")
    if plan.contract_version == 6:
        for ordinal, observation in enumerate(plan.observations):
            label = f"resource-{ordinal}"
            print(f"- {_display(label)}")
            print(f"  kind: {_display(observation.resource.kind)}")
            print(f"  owner: {_display(observation.resource.owner)}")
            print(f"  scope: {_display(observation.resource.scope)}")
            print(f"  status: {_display(observation.status.value)}")
            print(f"  action: {_display(observation.action.value)}")
            print(f"  reason: {_display(observation.reason)}")
            print(
                f"  impact: {_impact_text(observation, read_only=False, label=label)}"
            )
        summary = plan.summary()
        print(
            "Summary: "
            f"{summary['total']} resource(s), "
            f"{summary['changes']} change(s), "
            f"{summary['blocked']} blocked"
        )
        print("Capability: explicit secret-provider execution (M4)")
        print("Apply: use --allow-subprocess --rbw-executable PATH --yes --record PATH")
        return
    for observation in plan.observations:
        resource = observation.resource
        print(f"- {_display(resource.name)}")
        print(f"  source: {_display(resource.source_name)}")
        print(f"  target: {_display(resource.target_name)}")
        print(f"  owner: {_display(resource.owner)}")
        print(f"  scope: {_display(resource.scope)}")
        if resource.comparison != "exact-bytes":
            print(f"  comparison: {_display(resource.comparison)}")
        transition = (
            "source fields -> live fields"
            if 3 <= plan.contract_version < 5
            else "source -> live target"
        )
        print(f"  transition: {transition}")
        print(f"  status: {observation.status.value}")
        print(f"  action: {observation.action.value}")
        print(f"  reason: {_display(observation.reason)}")
        impact = _impact_text(
            observation,
            read_only=2 <= plan.contract_version < 5,
        )
        print(f"  impact: {impact}")
    summary = plan.summary()
    change_label = (
        "candidate change(s)" if 2 <= plan.contract_version < 5 else "change(s)"
    )
    print(
        "Summary: "
        f"{summary['total']} resource(s), "
        f"{summary['changes']} {change_label}, "
        f"{summary.get('reported', 0)} report(s), "
        f"{summary['blocked']} blocked"
    )
    if plan.contract_version == 3:
        print("Capability: read-only (M3a)")
        print(f"Apply: blocked ({plan.apply_block_reason})")
    elif plan.contract_version == 4:
        print("Capability: read-only (M3b)")
        print(f"Apply: blocked ({plan.apply_block_reason})")
    elif 2 <= plan.contract_version < 5:
        print("Capability: read-only (M2)")
        print(f"Apply: blocked ({plan.apply_block_reason})")
    elif plan.contract_version == 5:
        print("Capability: explicit execution (M3c)")
        print("Apply: use --yes with --record PATH")


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _emit_error(error: LuwuError, *, as_json: bool) -> None:
    error_payload: dict[str, object] = {
        "code": error.code,
        "message": str(error),
    }
    if isinstance(error, MutationError):
        error_payload.update(error.metadata())
    if as_json:
        _print_json({"error": error_payload})
    else:
        details = ""
        if isinstance(error, MutationError):
            fields = ",".join(_display(field) for field in error.fields) or "-"
            details = (
                f"; operation={_display(error.operation)}"
                f"; resource={_display(error.resource)}"
                f"; fields={fields}"
                f"; write={_display(error.write_path)}"
                f"; committed={str(error.committed).lower()}"
                f"; outcome={_display(error.outcome)}"
            )
        print(f"error[{error.code}]: {_display(error)}{details}", file=sys.stderr)


def _mutation_write_path(
    manifest: object,
    *,
    operation: str,
    resource_name: str,
) -> str | None:
    """Resolve a mutation's declared write label without reading its contents."""

    resources = getattr(manifest, "resources", ())
    matches = [resource for resource in resources if resource.name == resource_name]
    if len(matches) != 1:
        return None
    resource = matches[0]
    return resource.baseline_name if operation == "accept" else resource.source_name


def _display(value: object) -> str:
    """Keep control characters in user-controlled labels from becoming output."""

    text = str(value)
    return "".join(
        character if 0x20 <= ord(character) != 0x7F else f"\\x{ord(character):02x}"
        for character in text
    )


def _impact_text(
    observation: ResourceObservation,
    *,
    read_only: bool = False,
    label: str | None = None,
) -> str:
    action = observation.action.value
    target = _display(label if label is not None else observation.resource.target_name)
    if read_only and action in {"create", "replace"}:
        return (
            f"would write {target} in a future write-capable contract; "
            "M2 writes nothing"
        )
    if action == "replace":
        return f"overwrites {target}; outside declared target is not examined"
    if action == "create":
        return f"writes {target}; outside declared target is not examined"
    return "writes nothing; outside declared target is not examined"
