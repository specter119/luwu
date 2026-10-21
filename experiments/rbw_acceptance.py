#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Manual acceptance walk for the rbw facts a fake cannot prove.

Never run by CI or by the test suite. The operator locks, unlocks, stops, and
edits the real vault at the prompts while the script drives the ``luwu`` CLI
and one direct ``rbw unlocked`` probe; the script never starts an rbw agent
itself. Use a dedicated temporary vault item that holds a throwaway value. The
script builds one synthetic profile under ``--workdir`` (created 0700) and
records only redacted metadata: every recorded string passes through
:func:`redact`, which refuses text longer than 64 characters or carrying the
provider value. The provider value is read only from the committed target
file, is never printed, and the report is 0600. The script prints the facts it
asks the operator to confirm and, separately, the behaviors this walk does not
confirm.

    uv run --no-sync --script experiments/rbw_acceptance.py \
        --python .venv/bin/python \
        --rbw /usr/bin/rbw --workdir /tmp/luwu-rbw-acceptance \
        --item dotfiles-api-key [--field apiKey]

The default ``--luwu`` is ``<python> -m luwu``; ``--python`` selects that
interpreter and re-executes the script under it, so a ``uv run --script``
wrapper can hand over to the checkout environment. Pass
``--luwu /path/to/venv/bin/luwu`` to use an installed entry point instead.
A relative ``--rbw`` path, a missing rbw, and a non-empty workdir are refused.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
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

# Facts this walk assumes about a real rbw and asks the operator to confirm.
OPEN_FACTS = (
    "rbw get --raw -- ITEM must accept the `--` end-of-options marker",
    "raw JSON keys: data.password, data.username, notes, fields[].name/value",
    "stderr phrase: `agent is locked`",
    (
        "socket: <XDG_RUNTIME_DIR>/rbw/socket, else $TMPDIR (default /tmp)"
        "/rbw-<uid>/socket; observe whether `rbw stop-agent` leaves the socket "
        "file behind, in which case the blocked code is provider_command_failed"
    ),
    "whether RBW_AGENT=/bin/false actually prevents rbw from spawning an agent",
)

# A stopped agent can surface either way: with the socket removed luwu's lstat
# precheck fails as provider_agent_unavailable, and with a stale socket left
# behind the following `rbw unlocked` fails as provider_command_failed.
AGENT_STOPPED_CODES = ("provider_agent_unavailable", "provider_command_failed")

# Behaviors this walk cannot reach, or only observes, so it must not present
# them as confirmed.
NOT_CONFIRMED_FACTS = (
    (
        "whether `rbw get` on a locked agent unlocks or invokes pinentry by "
        "itself; luwu always probes `rbw unlocked` first, so this walk never "
        "runs `get` against a locked agent"
    ),
    (
        "whether `RLIMIT_CORE=0` with a cleared dumpable flag keeps a core dump "
        "from being written when `core_pattern` is a pipe; the walk only "
        "records the kernel settings above"
    ),
)

# The names luwu.providers passes to its rbw child: the vault location comes
# from HOME and the XDG and TMPDIR variables, so nothing else is needed.
_CHILD_ENVIRON_NAMES = (
    "HOME",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "TMPDIR",
)

# Recorded as information only: a nonzero dumpable flag or a pipe core_pattern
# decides where a crash writes, and neither is a pass condition of this walk.
KERNEL_SETTINGS = (
    "/proc/sys/kernel/core_pattern",
    "/proc/sys/fs/suid_dumpable",
)


class AcceptanceError(RuntimeError):
    """A failed check or local precondition; the message is operator-facing."""

    def __init__(self, message: str, checks: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.checks = checks or {}


def require(
    checks: dict[str, object], observed: dict[str, object] | None = None
) -> dict[str, object]:
    """Return the recorded checks when every one holds, and fail otherwise.

    ``observed`` carries fields that are recorded but not asserted, so a
    failure still reports the whole step record.
    """

    merged = {**(observed or {}), **checks}
    for name, value in checks.items():
        if not value:
            raise AcceptanceError(f"check failed: {name}", merged)
    return merged


def redact(value: object, secret: str | None = None) -> str:
    """Return recordable text, refusing long strings and the provider value."""

    if not isinstance(value, str):
        return REDACTION_MARKER
    if len(value) > MAX_RECORDED_TEXT or (secret is not None and secret in value):
        return REDACTION_MARKER
    return value


def child_environ() -> dict[str, str]:
    """The minimal environment shared by the luwu and rbw child processes."""

    environ = {
        name: os.environ[name] for name in _CHILD_ENVIRON_NAMES if name in os.environ
    }
    environ["PATH"] = os.defpath
    return environ


def kernel_setting(path: str) -> str:
    """Read one /proc setting for the record; a missing file is not a failure."""

    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return "unavailable"
    return redact(text) if text else "empty"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="interpreter for the default luwu command",
    )
    parser.add_argument("--rbw", required=True, type=Path, help="absolute rbw path")
    parser.add_argument("--workdir", required=True, type=Path, help="0700 workdir")
    parser.add_argument("--item", required=True, help="rbw item name to read")
    parser.add_argument("--field", default=None, help="rbw field inside the item")
    parser.add_argument("--luwu", default=None, help="luwu command to run")
    return parser


def luwu_command(namespace: argparse.Namespace, python: Path) -> list[str]:
    if namespace.luwu is None:
        return [str(python), "-m", "luwu"]
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


def write_profile(workdir: Path, *, item: str, field_name: str | None) -> Path:
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
    environ: dict[str, str] = field(default_factory=child_environ)


def _scan_output(context: Context, output: str, command: str) -> None:
    if context.secret is not None and context.secret in output:
        raise AcceptanceError(f"{command} printed the provider value")


def run_cli(
    context: Context, arguments: list[str], *, json_output: bool = True
) -> dict[str, Any]:
    command = [
        *context.luwu,
        *arguments,
        "--manifest",
        str(context.manifest),
        "--profile",
        PROFILE,
    ]
    if json_output:
        command.append("--json")
    completed = subprocess.run(
        command,
        cwd=context.workdir,
        env=context.environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    _scan_output(context, completed.stdout + completed.stderr, "a luwu command")
    try:
        payload: object = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    return {
        "exit_code": completed.returncode,
        "payload": payload,
        "stderr": completed.stderr,
    }


def run_rbw(
    context: Context,
    arguments: list[str],
    *,
    extra_environ: dict[str, str] | None = None,
) -> int:
    """Run rbw directly with the child whitelist and the given overrides."""

    environ = dict(context.environ)
    environ.update(extra_environ or {})
    completed = subprocess.run(
        [str(context.rbw), *arguments],
        cwd=context.workdir,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    _scan_output(context, completed.stdout + completed.stderr, "the rbw probe")
    return completed.returncode


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
    """Assert that a command reported one resource blocked with a fixed code.

    ``plan`` exits 0 even when a resource is blocked, so the exit code is
    recorded but not asserted.
    """

    entry = resource_entry(run)
    observed: dict[str, object] = {
        "exit_code": run["exit_code"],
        "status": redact(entry.get("status")),
        "error_code": redact(entry.get("error_code")),
        "provider_state": redact(provider_state(run)),
    }
    observed.update(checks or {})
    if entry.get("status") != "blocked" or entry.get("error_code") != expected_code:
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


def agent_count_checks(
    name: str, before: int | None, after: int | None
) -> dict[str, object]:
    """Record the rbw-agent count around one call and fail on a new agent.

    A missing pgrep is recorded as unverified: it is neither a pass nor a
    failure, and only a countable increase fails the step.
    """

    countable = before is not None and after is not None
    checks: dict[str, object] = {
        "agent_processes_before": before,
        "agent_processes_after": after,
        "agent_count_checked": countable,
        "agent_count_unavailable": not countable,
        "no_rbw_agent_started": (after == before) if countable else None,
    }
    if countable and after != before:
        raise AcceptanceError(f"{name} started an rbw agent", checks)
    return checks


def runtime_socket(context: Context) -> Path:
    """The agent socket path luwu.providers checks without an rbw profile."""

    runtime = context.environ.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isabs(runtime):
        return Path(runtime) / "rbw" / "socket"
    return (
        Path(context.environ.get("TMPDIR") or "/tmp") / f"rbw-{os.getuid()}" / "socket"
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
    socket_path = runtime_socket(context)
    try:
        stale_socket = stat.S_ISSOCK(os.lstat(socket_path).st_mode)
    except OSError:
        stale_socket = False
    # The count spans the plan and the direct probe, so neither may start an
    # agent.
    before = rbw_agent_count()
    plan = run_cli(context, ["plan", *authority(context)])
    exit_code = run_rbw(
        context, ["unlocked"], extra_environ={"RBW_AGENT": "/bin/false"}
    )
    after = rbw_agent_count()
    entry = resource_entry(plan)
    code = entry.get("error_code")
    observed: dict[str, object] = {
        "stale_socket_left_behind": stale_socket,
        "observed_error_code": redact(code),
        "probe_exit_code": exit_code,
    }
    observed.update(agent_count_checks("agent_stopped", before, after))
    return "agent_stopped", require(
        {
            "plan_blocked": entry.get("status") == "blocked",
            "code_is_agent_stopped": code in AGENT_STOPPED_CODES,
            "provider_state_matches_code": provider_state(plan) == code,
            "probe_nonzero": exit_code != 0,
        },
        observed,
    )


def step_unlocked_apply_verify(context: Context) -> tuple[str, dict[str, object]]:
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
    target = target_path(context)
    secret = provider_value(target)
    if secret is not None:
        context.secret = secret
    # The value is known from here on, so these calls exercise run_cli's scan
    # of the later output; it raises instead of returning a verdict.
    for later_arguments, as_json in (
        (["plan", *authority(context)], True),
        (["inspect"], True),
        (["inspect"], False),
    ):
        run_cli(context, later_arguments, json_output=as_json)
    checks = {
        "accept_committed": payload_of(accepted).get("outcome") == "committed",
        "plan_provider_ok": provider_state(plan) == "ok",
        "apply_committed": payload_of(applied).get("outcome") == "committed",
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
        "later_output_without_the_value": secret is not None,
    }
    return "unlocked_apply_verify", require(checks)


STEPS = (
    step_locked_vault,
    step_agent_stopped,
    step_unlocked_apply_verify,
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
    parser = build_parser()
    namespace = parser.parse_args(argv)
    python = namespace.python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error("--python must name an executable file")
    script_args = sys.argv[1:] if argv is None else argv
    if python != Path(sys.executable).absolute():
        os.execv(
            str(python),
            [str(python), str(Path(__file__).resolve()), *script_args],
        )
    if python != Path(sys.executable).absolute():
        parser.error("execution did not enter the requested Python interpreter")
    try:
        rbw = validate_rbw(namespace.rbw.expanduser())
        workdir = prepare_workdir(namespace.workdir)
        luwu = luwu_command(namespace, python)
    except AcceptanceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    manifest = write_profile(workdir, item=namespace.item, field_name=namespace.field)
    context = Context(workdir=workdir, manifest=manifest, rbw=rbw, luwu=luwu)
    version = rbw_version(rbw, context.environ)
    kernel = {path: kernel_setting(path) for path in KERNEL_SETTINGS}
    print("Luwu rbw acceptance walk; use a dedicated temporary vault item.")
    print(f"workdir: {workdir}\nrbw version: {version}")
    for path, value in kernel.items():
        print(f"{path}: {value} (informational only)")
    print("Facts this walk asks the operator to confirm:")
    for fact in OPEN_FACTS:
        print(f"  - {fact}")
    print("Not confirmed by this walk:")
    for fact in NOT_CONFIRMED_FACTS:
        print(f"  - {fact}")

    steps: list[dict[str, object]] = []
    blocked = False
    for function in STEPS:
        step: dict[str, object]
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
        "kernel_settings": kernel,
        "open_facts": list(OPEN_FACTS),
        "not_confirmed_by_this_walk": list(NOT_CONFIRMED_FACTS),
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
    print(
        f"delete the workdir when done: {workdir} (its target file holds the real value)"
    )
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
