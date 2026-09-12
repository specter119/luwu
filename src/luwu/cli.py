"""The user-facing CLI, kept separate from manifest and filesystem logic."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from . import __version__
from .errors import LuwuError, MutationError
from .manifest import is_execution_manifest, load_manifest
from .mutations import accept_baseline, reverse_sync
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
        help="version-5 execution journal path (required with --yes)",
    )

    record_inspect = commands.add_parser(
        "record-inspect",
        aliases=("inspect-record",),
        help="inspect a version-5 execution journal without writing",
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
        help="re-observe a version-5 execution journal without writing",
    )
    recover.add_argument(
        "--record", type=Path, required=True, help="execution journal path"
    )
    recover.add_argument(
        "--json",
        action="store_true",
        help="emit metadata-only JSON for agents and scripts",
    )

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
        if args.command in {"record-inspect", "inspect-record"}:
            record = inspect_execution_record(args.record)
            _emit_record_inspection(record, record_path=args.record, as_json=args.json)
            return 0

        if args.command in {"recover", "record-reobserve"}:
            result = reobserve_execution_record(args.record)
            _emit_reobserve_result(result, record_path=args.record, as_json=args.json)
            return 0 if result.get("outcome") == "confirmed" else 2

        manifest = load_manifest(args.manifest)
        if args.command in {"accept", "reverse-sync"}:
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
            and is_execution_manifest(manifest)
            and args.yes
            and args.record is None
        ):
            raise LuwuError(
                "version 5 apply with --yes requires explicit --record PATH",
                code="record_required",
            )

        plan = build_plan(manifest)
        if args.command in {"inspect", "plan"}:
            _emit_plan(plan, command=args.command, as_json=args.json)
            return 0

        if is_execution_manifest(manifest):
            record_path = args.record or manifest.root / ".luwu-preview.journal"
            try:
                result = execute_execution_plan(plan, record_path, confirm=args.yes)
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
                    print(
                        "No files changed because manifest version 3 is read-only in M3a.",
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
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            print(
                f"- {_display(resource.get('name'))}: "
                f"record={_display(resource.get('record_state'))}; "
                f"reobserved={_display(resource.get('reobserved_state'))}"
            )


def _emit_execution_error(
    error: LuwuError, *, record_path: Path, as_json: bool
) -> None:
    journal: dict[str, object] = {
        "path": str(record_path),
        "created": False,
    }
    if record_path.exists():
        journal["created"] = True
        try:
            record = inspect_execution_record(record_path)
        except LuwuError:
            journal["state"] = "unreadable"
        else:
            journal.update(_execution_journal_metadata(record, record_path=record_path))
    if as_json:
        _print_json(
            {
                "error": {"code": error.code, "message": str(error)},
                "journal": journal,
            }
        )
        return
    print(f"error[{error.code}]: {error}", file=sys.stderr)
    if journal["created"]:
        print(
            f"Journal: {_display(record_path)}; "
            f"state: {_display(journal.get('state', 'unreadable'))}",
            file=sys.stderr,
        )


def _execution_target_names(preview: dict[str, object]) -> list[str]:
    resources = cast(list[object], preview.get("resources", []))
    return [
        str(resource["target"])
        for resource in resources
        if isinstance(resource, dict) and "target" in resource
    ]


def _record_target_names(record: dict[str, object]) -> list[str]:
    target_names: list[str] = []
    resources = record.get("resources", [])
    if not isinstance(resources, list):
        return target_names
    for resource in resources:
        if not isinstance(resource, dict):
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
        "resources": [
            {"name": resource["name"], "state": resource["state"]}
            for resource in resources
        ],
    }


def _print_human_plan(plan: Plan, *, heading: str) -> None:
    print(heading)
    print(f"Manifest: {_display(plan.manifest.path)}")
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
    if 3 <= plan.contract_version < 5:
        print("Capability: read-only (M3a)")
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
        print(f"error[{error.code}]: {error}{details}", file=sys.stderr)


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
) -> str:
    action = observation.action.value
    target = _display(observation.resource.target_name)
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
