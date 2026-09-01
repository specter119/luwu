"""The user-facing CLI, kept separate from manifest and filesystem logic."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .errors import LuwuError
from .manifest import load_manifest
from .reconcile import (
    ApplyOutcome,
    ApplyResult,
    Plan,
    ResourceObservation,
    apply_plan,
    build_plan,
    plan_to_dict,
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        plan = build_plan(manifest)
        if args.command in {"inspect", "plan"}:
            _emit_plan(plan, command=args.command, as_json=args.json)
            return 0

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
                print("No files changed because the plan is blocked.", file=sys.stderr)
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
                "reason": ("plan_blocked" if plan.blocked else "confirmation_required"),
            }
        )
        _print_json(payload)
        return
    _print_human_plan(plan, heading="Apply preview")


def _emit_apply_blocked(plan: Plan, *, as_json: bool) -> None:
    if as_json:
        payload = plan_to_dict(plan, command="apply")
        payload.update({"applied": False, "reason": "plan_blocked"})
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
        print("  transition: source -> live target")
        print(f"  status: {observation.status.value}")
        print(f"  action: {observation.action.value}")
        print(f"  reason: {_display(observation.reason)}")
        impact = _impact_text(observation)
        print(f"  impact: {impact}")
    summary = plan.summary()
    print(
        "Summary: "
        f"{summary['total']} resource(s), "
        f"{summary['changes']} change(s), "
        f"{summary['blocked']} blocked"
    )


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _emit_error(error: LuwuError, *, as_json: bool) -> None:
    if as_json:
        _print_json({"error": {"code": error.code, "message": str(error)}})
    else:
        print(f"error[{error.code}]: {error}", file=sys.stderr)


def _display(value: object) -> str:
    """Keep control characters in user-controlled labels from becoming output."""

    text = str(value)
    return "".join(
        character if 0x20 <= ord(character) != 0x7F else f"\\x{ord(character):02x}"
        for character in text
    )


def _impact_text(observation: ResourceObservation) -> str:
    action = observation.action.value
    target = _display(observation.resource.target_name)
    if action == "replace":
        return f"overwrites {target}; outside declared target is not examined"
    if action == "create":
        return f"writes {target}; outside declared target is not examined"
    return "writes nothing; outside declared target is not examined"
