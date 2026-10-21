#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Manual acceptance walk for provider-owned fields against a real rbw.

Never run by CI or by the test suite, and it never starts a real rbw itself:
the operator locks, unlocks, stops, and edits the real vault at the prompts
while the script drives the installed ``luwu`` CLI. It builds one synthetic
profile under ``--workdir`` (created 0700) and records only redacted metadata:
every recorded string passes through :func:`redact`, which refuses text longer
than 64 characters or carrying the provider value. The provider value is read
only from the committed target file, is never printed, and the report is 0600.

    .venv/bin/python experiments/rbw_acceptance.py \
        --rbw /usr/bin/rbw --workdir /tmp/luwu-rbw-acceptance \
        --item dotfiles-api-key [--field apiKey] [--profile work]

The default ``--luwu`` is ``sys.executable -m luwu``; pass ``--luwu
/path/to/venv/bin/luwu`` to use an installed entry point. A relative ``--rbw``
path, a missing rbw, and a non-empty workdir are refused.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROVIDER_FIELD = "apiKey"
PUBLIC_FIELD = "user"
PROFILE = "acceptance"
TARGET_RELATIVE = Path("target") / ".config" / "app.json"
PLACEHOLDER = "acceptance-placeholder-9c14"
MAX_RECORDED_TEXT = 64
COMMAND_TIMEOUT_SECONDS = 60.0
REDACTION_MARKER = "<redaction-refused>"

# Facts this walk assumes about a real rbw and cannot prove by itself.
OPEN_FACTS = (
    "rbw get --raw -- ITEM must accept the `--` end-of-options marker",
    "raw JSON keys: data.password, data.username, notes, fields[].name/value",
    "stderr phrase: `agent is locked`",
    "socket: <XDG_RUNTIME_DIR>/rbw[-profile]/socket, else /tmp/rbw-<uid>/socket",
    "whether RBW_AGENT=/bin/false actually prevents rbw from spawning an agent",
    "whether RLIMIT_CORE=0 with PR_SET_DUMPABLE stops core dumps under a pipe core_pattern",
)


class AcceptanceError(RuntimeError):
    """A failed check or local precondition; the message is operator-facing."""

    def __init__(self, message: str, checks: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.checks = checks or {}


def require(checks: dict[str, object]) -> dict[str, object]:
    """Return the recorded checks when every one holds, and fail otherwise."""

    for name, value in checks.items():
        if not value:
            raise AcceptanceError(f"check failed: {name}", checks)
    return checks


def redact(value: object, secret: str | None = None) -> str:
    """Return recordable text, refusing long strings and the provider value."""

    if not isinstance(value, str):
        return REDACTION_MARKER
    if len(value) > MAX_RECORDED_TEXT or (secret is not None and secret in value):
        return REDACTION_MARKER
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rbw", required=True, type=Path, help="absolute rbw path")
    parser.add_argument("--workdir", required=True, type=Path, help="0700 workdir")
    parser.add_argument("--item", required=True, help="rbw item name to read")
    parser.add_argument("--field", default=None, help="rbw field inside the item")
    parser.add_argument("--profile", default=None, help="rbw profile name")
    parser.add_argument("--luwu", default=None, help="luwu command to run")
    return parser


def luwu_command(namespace: argparse.Namespace) -> list[str]:
    if namespace.luwu is None:
        return [sys.executable, "-m", "luwu"]
    words = shlex.split(namespace.luwu)
    if not words:
        raise AcceptanceError("--luwu must name a command")
    return words


def validate_rbw(path: Path) -> Path:
    if not path.is_absolute():
        raise AcceptanceError("--rbw must be an absolute path")
    if not path.is_file():
        raise AcceptanceError("--rbw must name an existing file")
    if not os.access(path, os.X_OK):
        raise AcceptanceError("--rbw must be executable")
    return path


def prepare_workdir(path: Path) -> Path:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise AcceptanceError("--workdir must be an empty directory or absent")
    else:
        resolved.mkdir(parents=True)
    resolved.chmod(0o700)
    return resolved


def write_profile(
    workdir: Path, *, item: str, field_name: str | None, profile: str | None
) -> Path:
    """Build the synthetic profile: one public field and one provider field."""

    source = workdir / "source" / "templates"
    local = workdir / "inputs" / "local"
    for directory in (
        source,
        local,
        workdir / "inputs" / "baselines",
        workdir / "target",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (source / "app.json.j2").write_text(
        '{"service":{"apiKey":"'
        + PLACEHOLDER
        + '","user":{{ inputs.user | tojson }}}}\n',
        encoding="utf-8",
    )
    (local / "user.json").write_text('{"user":"initial-user"}\n', encoding="utf-8")
    provider = ['type = "rbw"', f"item = {json.dumps(item)}"]
    if field_name is not None:
        provider.append(f"field = {json.dumps(field_name)}")
    if profile is not None:
        provider.append(f"profile = {json.dumps(profile)}")
    manifest = workdir / "luwu.toml"
    manifest.write_text(
        "version = 8\n"
        '\n[roots]\nsource = "source"\ninputs = "inputs"\ntarget = "target"\n'
        f"\n[profiles.{PROFILE}]\n"
        'packages = ["app"]\nexclude = []\n'
        '\n[packages.app]\ndependencies = []\nresources = ["app"]\n'
        "\n[inputs.user]\n"
        'layers = [{ file = "local/user.json", path = ["user"], format = "json" }]\n'
        'reverse_to = { file = "local/user.json", path = ["user"], format = "json" }\n'
        "\n[providers.vault]\n"
        f"{chr(10).join(provider)}\n"
        "\n[resources.app]\n"
        'source = "templates/app.json.j2"\ntarget = ".config/app.json"\n'
        'format = "json"\nbaseline = "baselines/app.json"\n'
        'parents = "create"\nparent_mode = "0700"\nmode = "0600"\n'
        f"\n[resources.app.fields.{PROVIDER_FIELD}]\n"
        f'path = ["service", {json.dumps(PROVIDER_FIELD)}]\n'
        'owner = "provider"\nprovider = "vault"\n'
        f"\n[resources.app.fields.{PUBLIC_FIELD}]\n"
        f'path = ["service", {json.dumps(PUBLIC_FIELD)}]\n'
        'owner = "merge"\ninput = "user"\n',
        encoding="utf-8",
    )
    return manifest


@dataclass
class Context:
    workdir: Path
    manifest: Path
    rbw: Path
    luwu: list[str]
    secret: str | None = None
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))


def run_cli(context: Context, arguments: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [
            *context.luwu,
            *arguments,
            "--manifest",
            str(context.manifest),
            "--profile",
            PROFILE,
            "--json",
        ],
        cwd=context.workdir,
        env=context.environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    try:
        payload: object = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    return {"exit_code": completed.returncode, "payload": payload}


def payload_of(run: dict[str, Any]) -> dict[str, Any]:
    payload = run["payload"]
    return payload if isinstance(payload, dict) else {}


def resource_entry(run: dict[str, Any], name: str = "app") -> dict[str, Any]:
    resources = payload_of(run).get("resources")
    if not isinstance(resources, list):
        return {}
    for item in resources:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return {}


def provider_state(run: dict[str, Any]) -> object:
    providers = resource_entry(run).get("providers")
    if isinstance(providers, list) and providers and isinstance(providers[0], dict):
        return providers[0].get("state")
    return None


def record(
    function: Any,
    status: str,
    reason: str | None = None,
    checks: dict[str, object] | None = None,
) -> dict[str, object]:
    """One recorded step result; skipped steps carry the earlier failure."""

    return {
        "step": function.__name__,
        "status": status,
        "reason": reason,
        "checks": checks or {},
    }


def blocked_step(
    name: str,
    run: dict[str, Any],
    expected_code: str,
    checks: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """Assert that a command reported one resource blocked with a fixed code."""

    entry = resource_entry(run)
    observed: dict[str, object] = {
        "exit_code": run["exit_code"],
        "status": redact(entry.get("status")),
        "error_code": redact(entry.get("error_code")),
        "provider_state": redact(provider_state(run)),
    }
    observed.update(checks or {})
    if (
        run["exit_code"] != 0
        or entry.get("status") != "blocked"
        or entry.get("error_code") != expected_code
    ):
        raise AcceptanceError(f"{name} did not report {expected_code}", observed)
    return name, observed


def prompt(message: str) -> None:
    print(f"  -> {message}")
    try:
        input("     press Enter when done: ")
    except EOFError:
        raise AcceptanceError("operator input ended before the step finished") from None


def authority(context: Context, executable: Path | None = None) -> list[str]:
    target = context.rbw if executable is None else executable
    return ["--provider-executable", f"rbw={target}"]


def target_path(context: Context) -> Path:
    return context.workdir / TARGET_RELATIVE


def provider_value(target: Path) -> str | None:
    try:
        value = json.loads(target.read_text(encoding="utf-8"))["service"][
            PROVIDER_FIELD
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) and value else None


def value_holders(workdir: Path, value: str) -> list[Path]:
    needle = value.encode("utf-8")
    return [
        path
        for path in sorted(workdir.rglob("*"))
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes()
    ]


def rbw_agent_count() -> int | None:
    """How many `rbw-agent` processes exist, or None when pgrep is absent."""

    try:
        completed = subprocess.run(
            ["pgrep", "-x", "rbw-agent"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode not in (0, 1):
        return None
    return len([line for line in completed.stdout.splitlines() if line.strip()])


def step_plan_without_authority(context: Context) -> tuple[str, dict[str, object]]:
    return blocked_step(
        "plan_without_authority",
        run_cli(context, ["plan"]),
        "provider_authority_required",
    )


def step_locked_vault(context: Context) -> tuple[str, dict[str, object]]:
    prompt("Lock the vault now (for example: rbw lock), then")
    return blocked_step(
        "locked_vault",
        run_cli(context, ["plan", *authority(context)]),
        "provider_locked",
    )


def step_agent_stopped(context: Context) -> tuple[str, dict[str, object]]:
    prompt("Stop the rbw agent now (for example: rbw stop-agent), then")
    before = rbw_agent_count()
    run = run_cli(context, ["plan", *authority(context)])
    after = rbw_agent_count()
    return blocked_step(
        "agent_stopped",
        run,
        "provider_agent_unavailable",
        {
            "agent_processes_before": before,
            "agent_processes_after": after,
            "no_rbw_agent_started": before is not None and after == before,
        },
    )


def step_unlocked_apply(context: Context) -> tuple[str, dict[str, object]]:
    prompt("Unlock the vault now (for example: rbw unlock), then")
    accepted = run_cli(
        context,
        [
            "accept",
            "--resource",
            "app",
            "--from",
            "desired",
            "--field",
            "user",
            "--yes",
        ],
    )
    plan = run_cli(context, ["plan", *authority(context)])
    applied = run_cli(context, ["apply", *authority(context), "--yes"])
    checks = {
        "accept_committed": payload_of(accepted).get("outcome") == "committed",
        "plan_provider_ok": provider_state(plan) == "ok",
        "apply_committed": payload_of(applied).get("outcome") == "committed",
        "target_written": target_path(context).is_file(),
    }
    return "unlocked_apply", require(checks)


def step_verify_target(context: Context) -> tuple[str, dict[str, object]]:
    target = target_path(context)
    secret = provider_value(target)
    if secret is not None:
        context.secret = secret
    checks = {
        "target_exists": target.is_file(),
        "owner_only_mode": target.is_file()
        and stat.S_IMODE(target.stat().st_mode) == 0o600,
        "provider_field_is_text": secret is not None,
        "placeholder_replaced": target.is_file()
        and PLACEHOLDER.encode() not in target.read_bytes(),
        "only_target_holds_the_value": secret is not None
        and value_holders(context.workdir, secret) == [target],
        "no_staging_entry": not any(
            ".luwu-tmp-" in path.name for path in context.workdir.rglob("*")
        ),
    }
    return "verify_target", require(checks)


def step_executable_replacement(context: Context) -> tuple[str, dict[str, object]]:
    """A replaced executable must fail with a fixed provider code.

    Deliberately not a race: the copy is replaced while no command runs, so the
    next plan inspects different bytes at the same path. The in-flight identity
    check is owned by tests/providers/test_executable.py.
    """

    private = context.workdir / "executable-copy"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    copied = private / "rbw"
    shutil.copy2(context.rbw, copied)
    copied.chmod(0o700)
    run_ok = run_cli(context, ["plan", *authority(context, copied)])
    replacement = copied.with_name("rbw.next")
    shutil.copy2(shutil.which("true") or "/usr/bin/true", replacement)
    replacement.chmod(0o700)
    os.replace(replacement, copied)
    changed = run_cli(context, ["plan", *authority(context, copied)])
    entry = resource_entry(changed)
    code = entry.get("error_code")
    checks = {
        "private_copy_provider_ok": provider_state(run_ok) == "ok",
        "changed_copy_status": redact(entry.get("status")),
        "changed_copy_error_code": redact(code),
        "changed_copy_never_fetched": provider_state(changed) != "ok",
        "changed_copy_blocked": entry.get("status") == "blocked",
        "changed_copy_fixed_code": isinstance(code, str)
        and code.startswith("provider_"),
    }
    return "executable_replacement", require(checks)


def step_public_choice(context: Context) -> tuple[str, dict[str, object]]:
    target = target_path(context)
    choice = f"operator-choice-{uuid.uuid4().hex[:8]}"
    document = json.loads(target.read_text(encoding="utf-8"))
    document["service"][PUBLIC_FIELD] = choice
    target.write_text(json.dumps(document) + "\n", encoding="utf-8")
    reversed_ = run_cli(
        context,
        ["reverse-sync", "--resource", "app", "--field", PUBLIC_FIELD, "--yes"],
    )
    applied = run_cli(context, ["apply", *authority(context), "--yes"])
    try:
        written = json.loads(
            (context.workdir / "inputs" / "local" / "user.json").read_text(
                encoding="utf-8"
            )
        )[PUBLIC_FIELD]
        live = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        written, live = None, {}
    checks = {
        "reverse_committed": payload_of(reversed_).get("outcome") == "committed",
        "reverse_wrote_the_choice": written == choice,
        "apply_ok": applied["exit_code"] == 0,
        "public_choice_retained": live.get("service", {}).get(PUBLIC_FIELD) == choice,
        "provider_value_current": live.get("service", {}).get(PROVIDER_FIELD)
        == context.secret,
        "target_exists": target.is_file(),
    }
    return "public_choice", require(checks)


STEPS = (
    step_plan_without_authority,
    step_locked_vault,
    step_agent_stopped,
    step_unlocked_apply,
    step_verify_target,
    step_executable_replacement,
    step_public_choice,
)


def rbw_version(rbw: Path, environ: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            [str(rbw), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
            env=environ,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    lines = (completed.stdout or completed.stderr).strip().splitlines()
    return "unknown" if not lines else redact(lines[0].strip())


def main(argv: list[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    try:
        rbw = validate_rbw(namespace.rbw.expanduser())
        workdir = prepare_workdir(namespace.workdir)
        luwu = luwu_command(namespace)
    except AcceptanceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    manifest = write_profile(
        workdir,
        item=namespace.item,
        field_name=namespace.field,
        profile=namespace.profile,
    )
    context = Context(workdir=workdir, manifest=manifest, rbw=rbw, luwu=luwu)
    version = rbw_version(rbw, context.environ)
    print(
        "Luwu rbw acceptance walk; never run in CI and never against a vault you own."
    )
    print(f"workdir: {workdir}\nrbw version: {version}")
    print("Open facts to confirm by hand:")
    for fact in OPEN_FACTS:
        print(f"  - {fact}")

    steps: list[dict[str, object]] = []
    blocked = False
    for function in STEPS:
        if blocked:
            step = record(function, "SKIPPED", "an earlier step failed")
        else:
            try:
                name, checks = function(context)
                step = {
                    "step": name,
                    "status": "PASS",
                    "reason": None,
                    "checks": checks,
                }
            except AcceptanceError as error:
                step = record(
                    function, "FAIL", redact(str(error), context.secret), error.checks
                )
                blocked = True
            except Exception as error:  # noqa: BLE001 - one step must not hide the rest
                step = record(function, "FAIL", type(error).__name__)
                blocked = True
        steps.append(step)
        print(f"[{step['status']}] {step['step']}")

    confined = context.secret is not None and value_holders(
        workdir, context.secret
    ) == [target_path(context)]
    status = "fail" if any(step["status"] == "FAIL" for step in steps) else "pass"
    report: dict[str, object] = {
        "experiment": "rbw_acceptance",
        "never_run_in_ci": True,
        "rbw_version": version,
        "workdir_created_0700": True,
        "steps": steps,
        "final_secret_confined_to_target": confined,
        "status": status,
    }
    report_path = workdir / "acceptance-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report_path.chmod(0o600)
    print(f"final_secret_confined_to_target: {confined}\nreport: {report_path}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
