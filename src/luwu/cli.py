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
from .reconcile import ApplyResult, Plan, apply_plan, build_plan, plan_to_dict


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
        return 0
    except LuwuError as exc:
        _emit_error(exc, as_json=getattr(args, "json", False))
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
        payload.update({"applied": False, "reason": "confirmation_required"})
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
                "applied": True,
                "changed_targets": list(result.changed_targets),
                "verification": plan_to_dict(
                    result.verification_plan,
                    command="verification",
                ),
            }
        )
        _print_json(payload)
        return

    print("Applied")
    print(f"Changed targets: {len(result.changed_targets)}")
    print("Verification: clean")


def _print_human_plan(plan: Plan, *, heading: str) -> None:
    print(heading)
    print(f"Manifest: {plan.manifest.path}")
    for observation in plan.observations:
        resource = observation.resource
        print(f"- {resource.name}")
        print(f"  source: {resource.source_name}")
        print(f"  target: {resource.target_name}")
        print(f"  owner: {resource.owner}")
        print(f"  status: {observation.status.value}")
        print(f"  action: {observation.action.value}")
        print(f"  reason: {observation.reason}")
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
