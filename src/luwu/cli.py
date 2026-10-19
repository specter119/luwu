"""The user-facing CLI, kept separate from manifest and filesystem logic."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from . import __version__, engine
from .errors import LuwuError, MutationError
from .manifest import load_manifest


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
        _add_common_arguments(commands.add_parser(name, help=help_text))

    apply = commands.add_parser(
        "apply", help="apply a plan after explicit confirmation"
    )
    _add_common_arguments(apply)
    _add_confirm_argument(apply)

    accept = commands.add_parser(
        "accept", help="explicitly accept selected public baseline fields"
    )
    _add_common_arguments(accept)
    _add_resource_argument(accept)
    accept.add_argument(
        "--from",
        dest="value_from",
        choices=("desired", "live"),
        required=True,
        help="which observed value becomes the new baseline",
    )
    _add_field_argument(accept)
    _add_confirm_argument(accept)

    adopt = commands.add_parser(
        "adopt", help="explicitly accept a declared copy resource"
    )
    _add_common_arguments(adopt)
    _add_resource_argument(adopt)
    _add_confirm_argument(adopt)

    reverse = commands.add_parser(
        "reverse-sync",
        help="explicitly write selected live-owned fields to inputs, "
        "or undeclared live changes to a literal template source",
    )
    _add_common_arguments(reverse)
    _add_resource_argument(reverse)
    reverse_scope = reverse.add_mutually_exclusive_group(required=True)
    reverse_scope.add_argument(
        "--field",
        action="append",
        help="declared field name; repeat to select several fields",
    )
    reverse_scope.add_argument(
        "--undeclared",
        action="store_true",
        help="write live changes outside the declared fields into the template "
        "source when they can be proven to render back unchanged",
    )
    _add_confirm_argument(reverse)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest, profile=args.profile)
        return _run_command(args, manifest)
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
    parser.add_argument(
        "--profile",
        required=True,
        help="manifest profile to select",
    )


def _add_resource_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--resource", required=True, help="declared resource name")


def _add_field_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--field",
        action="append",
        required=True,
        help="declared field name; repeat to select several fields",
    )


def _add_confirm_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the calculated action and permit the write; "
        "without it the command only previews",
    )


def _run_command(args: argparse.Namespace, manifest: object) -> int:
    """Translate CLI input into the process-local engine API."""

    plan = engine.build_plan(cast(Any, manifest))
    if args.command in {"inspect", "plan"}:
        _emit_metadata(plan, heading=args.command, command=args.command, args=args)
        return 0

    if args.command == "apply":
        result = engine.apply_plan(plan, confirm=args.yes)
    elif args.command == "accept":
        result = engine.accept_baseline(
            plan,
            resource=args.resource,
            fields=tuple(args.field),
            value_from=args.value_from,
            confirm=args.yes,
        )
    elif args.command == "adopt":
        result = engine.adopt_resource(plan, resource=args.resource, confirm=args.yes)
    elif args.command == "reverse-sync" and args.undeclared:
        result = engine.reverse_undeclared(
            plan, resource=args.resource, confirm=args.yes
        )
    elif args.command == "reverse-sync":
        result = engine.reverse_inputs(
            plan,
            resource=args.resource,
            fields=tuple(args.field),
            confirm=args.yes,
        )
    else:
        raise LuwuError("command is not supported", code="command_unsupported")

    heading = result.to_dict().get("operation")
    _emit_metadata(
        result,
        heading=heading if isinstance(heading, str) else "result",
        command=None,
        args=args,
    )
    return 0 if result.outcome in {"committed", "no_changes"} else 2


def _emit_metadata(
    value: object, *, heading: str, command: str | None, args: argparse.Namespace
) -> None:
    payload = cast(Any, value).to_dict()
    if not isinstance(payload, dict):
        raise TypeError("engine metadata must be an object")
    if command is not None:
        payload["command"] = command
    if args.json:
        _print_json(payload)
        return
    print(_display(heading))
    for key, item in payload.items():
        rendered = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        print(f"{_display(key)}: {rendered}")


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


def _display(value: object) -> str:
    """Keep control characters in user-controlled labels from becoming output."""

    text = str(value)
    return "".join(
        character if 0x20 <= ord(character) != 0x7F else f"\\x{ord(character):02x}"
        for character in text
    )
