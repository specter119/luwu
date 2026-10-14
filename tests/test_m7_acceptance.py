"""Independent M7 v8 CLI acceptance over synthetic, isolated fixtures."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m7"
OUTPUT_SENTINEL = "synthetic-output-secret-sentinel-v1"
UNKNOWN_SENTINEL = "synthetic-unknown-container-secret-v1"
EXPECTED_RESOURCES = tuple(
    line
    for line in (FIXTURE / "expected" / "profile-resources.txt")
    .read_text(encoding="utf-8")
    .splitlines()
    if line
)

_COMMENT_RACE_HOOK = """
import sys
from pathlib import Path
import luwu.m7_engine as engine
from luwu.cli import main
input_path = Path(sys.argv.pop(1))
ABLATEME
original_write_file = engine.write_file
triggered = False
def race_write_file(root, path, data, *, expected, mode=None, check_inputs=None):
    global triggered
    if not triggered and Path(path).name == "settings.jsonc":
        contents = input_path.read_bytes()
        marker = b"Global layer"
        if contents.count(marker) != 1:
            raise AssertionError("comment race marker must occur once")
        input_path.write_bytes(contents.replace(marker, b"Global later", 1))
        triggered = True
    return original_write_file(
        root, path, data, expected=expected, mode=mode, check_inputs=check_inputs
    )
engine.write_file = race_write_file
raise SystemExit(main())
"""

_FAIL_PARENT_HOOK = """
import sys
from pathlib import Path
import luwu.m7_engine as engine
from luwu.m7_filesystem import M7FilesystemError
from luwu.cli import main
failed_name = sys.argv.pop(1)
original_create_parent = engine.create_parent
def fail_one_parent(root, path, mode, *, check_inputs=None):
    if Path(path).name == failed_name:
        raise M7FilesystemError("synthetic parent failure", code="m7_parent_create_failed")
    return original_create_parent(root, path, mode, check_inputs=check_inputs)
engine.create_parent = fail_one_parent
raise SystemExit(main())
"""

_FAIL_WRITE_HOOK = """
import sys
from pathlib import Path
import luwu.m7_engine as engine
from luwu.m7_filesystem import M7FilesystemError
from luwu.cli import main
failed_name = sys.argv.pop(1)
original_write_file = engine.write_file
def fail_one_write(root, path, data, *, expected, mode=None, check_inputs=None):
    if Path(path).name == failed_name:
        raise M7FilesystemError("synthetic target failure", code="m7_write_failed")
    return original_write_file(
        root, path, data, expected=expected, mode=mode, check_inputs=check_inputs
    )
engine.write_file = fail_one_write
raise SystemExit(main())
"""

_EXCLUDED_ACCESS_HOOK = """
import builtins
import io
import json
import os
import sys
from pathlib import Path
from luwu.cli import main

report = Path(sys.argv.pop(1))
arguments = sys.argv[1:]
manifest = Path(arguments[arguments.index("--manifest") + 1])
root = manifest.parent
excluded_paths = (
    root / "repo/templates/mihomo.jsonc.j2",
    root / "repo/templates/excluded-source-must-not-be-read",
    root / "home/.config/mihomo",
    root / "inputs/local/mihomo-exclusive",
    root / "inputs/local/mihomo-exclusive/excluded-input-must-not-be-read",
    root / "inputs/baselines/mihomo.json",
)
enumerated_parents = (root / "repo", root / "repo/templates", root / "inputs")
events = []

def candidate_path(path, dir_fd):
    if isinstance(path, int):
        try:
            return os.path.normpath(os.readlink(f"/proc/self/fd/{path}"))
        except OSError:
            return None
    try:
        value = os.fsdecode(os.fspath(path))
    except TypeError:
        return None
    if not os.path.isabs(value) and dir_fd is not None:
        try:
            parent = os.readlink(f"/proc/self/fd/{dir_fd}")
        except OSError:
            return None
        value = os.path.join(parent, value)
    if not os.path.isabs(value):
        return None
    return os.path.normpath(value)

def is_excluded(candidate):
    return any(
        candidate == os.fspath(path)
        or candidate.startswith(os.fspath(path) + os.sep)
        for path in excluded_paths
    )

def tracked(name, original):
    def call(*args, **kwargs):
        paths = [kwargs[key] for key in ("path", "file", "src", "dst") if key in kwargs]
        path_args = args[:2] if name in ("rename", "replace", "link", "symlink") else args[:1]
        paths.extend(path_args)
        for index, path in enumerate(paths):
            dir_fd = kwargs.get("dir_fd")
            if name in ("rename", "replace", "link", "symlink"):
                key = "src_dir_fd" if index == 0 else "dst_dir_fd"
                dir_fd = kwargs.get(key)
            candidate = candidate_path(path, dir_fd)
            if candidate is not None and (
                is_excluded(candidate)
                or (
                    name in ("listdir", "scandir")
                    and candidate
                    in {os.fspath(parent) for parent in enumerated_parents}
                )
            ):
                events.append(name)
        return original(*args, **kwargs)
    return call

operations = (
    (os, "stat", "stat"),
    (os, "lstat", "lstat"),
    (os, "open", "os.open"),
    (builtins, "open", "builtins.open"),
    (io, "open", "io.open"),
    (os, "listdir", "listdir"),
    (os, "scandir", "scandir"),
    (os, "readlink", "readlink"),
    (os, "access", "access"),
    (os, "mkdir", "mkdir"),
    (os, "rmdir", "rmdir"),
    (os, "unlink", "unlink"),
    (os, "remove", "remove"),
    (os, "rename", "rename"),
    (os, "replace", "replace"),
    (os, "chmod", "chmod"),
    (os, "chown", "chown"),
    (os, "link", "link"),
    (os, "symlink", "symlink"),
    (os, "fchmod", "fchmod"),
    (os, "fchown", "fchown"),
)
for module, attribute, label in operations:
    setattr(module, attribute, tracked(label, getattr(module, attribute)))

exit_code = main()
counts = {label: events.count(label) for _, _, label in operations}
report.write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
raise SystemExit(86 if events else exit_code)
"""

_MUTATION_COUNT_HOOK = """
import json
import sys
from pathlib import Path
import luwu.m7_engine as engine
from luwu.cli import main

report = Path(sys.argv.pop(1))
counts = {"create_parent": 0, "write_file": 0}
for name in tuple(counts):
    original = getattr(engine, name)
    def make_counter(label, operation):
        def count(*args, **kwargs):
            counts[label] += 1
            return operation(*args, **kwargs)
        return count
    setattr(engine, name, make_counter(name, original))

exit_code = main()
report.write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
raise SystemExit(exit_code)
"""

_FUTURE_SOURCE_CHANGE_HOOK = """
import json
import os
import sys
from pathlib import Path
import luwu.m7_engine as engine
from luwu.cli import main

control = json.loads(Path(sys.argv.pop(1)).read_text(encoding="utf-8"))
trigger_targets = {os.path.normpath(path) for path in control["trigger_targets"]}
mutation_path = Path(control["mutation_path"])
replacement = bytes.fromhex(control["replacement_hex"])
report = Path(control["report"])
completed_targets = set()
state = {"mutated": False}
original_write_file = engine.write_file

def write_then_change_future_source(root, path, data, *, expected, mode=None, check_inputs=None):
    identity = original_write_file(
        root, path, data, expected=expected, mode=mode, check_inputs=check_inputs
    )
    candidate = os.path.normpath(os.fspath(path))
    if candidate in trigger_targets:
        completed_targets.add(candidate)
    if not state["mutated"] and trigger_targets <= completed_targets:
        mutation_path.write_bytes(replacement)
        state["mutated"] = True
    return identity

engine.write_file = write_then_change_future_source
exit_code = main()
report.write_text(
    json.dumps({"mutated": state["mutated"], "completed": sorted(completed_targets)}),
    encoding="utf-8",
)
raise SystemExit(exit_code)
"""

_NOOP_FINAL_CHANGE_HOOK = """
import json
import os
import sys
from pathlib import Path
import luwu.m7_engine as engine
from luwu.cli import main

control = json.loads(Path(sys.argv.pop(1)).read_text(encoding="utf-8"))
trigger_target = os.path.normpath(control["trigger_target"])
noop_target = Path(control["noop_target"])
replacement = bytes.fromhex(control["replacement_hex"])
report = Path(control["report"])
state = {"mutated": False}
original_write_file = engine.write_file

def write_then_change_noop_target(root, path, data, *, expected, mode=None, check_inputs=None):
    identity = original_write_file(
        root, path, data, expected=expected, mode=mode, check_inputs=check_inputs
    )
    if not state["mutated"] and os.path.normpath(os.fspath(path)) == trigger_target:
        noop_target.write_bytes(replacement)
        state["mutated"] = True
    return identity

engine.write_file = write_then_change_noop_target
exit_code = main()
report.write_text(json.dumps(state), encoding="utf-8")
raise SystemExit(exit_code)
"""

_NOOP_PREFLIGHT_CHANGE_HOOK = """
import json
import sys
from pathlib import Path
import luwu.m7_engine as engine
from luwu.cli import main

control = json.loads(Path(sys.argv.pop(1)).read_text(encoding="utf-8"))
noop_target = Path(control["noop_target"])
replacement = bytes.fromhex(control["replacement_hex"])
report = Path(control["report"])
counts = {"create_parent": 0, "write_file": 0}
state = {"mutated": False}
original_build_plan = engine.build_plan

def build_plan_then_change_noop(manifest):
    plan = original_build_plan(manifest)
    if not state["mutated"]:
        noop_target.write_bytes(replacement)
        state["mutated"] = True
    return plan

engine.build_plan = build_plan_then_change_noop
for name in tuple(counts):
    original = getattr(engine, name)
    def make_counter(label, operation):
        def count(*args, **kwargs):
            counts[label] += 1
            return operation(*args, **kwargs)
        return count
    setattr(engine, name, make_counter(name, original))

exit_code = main()
report.write_text(
    json.dumps({"mutated": state["mutated"], "counts": counts}, sort_keys=True),
    encoding="utf-8",
)
raise SystemExit(exit_code)
"""


class _M7Case:
    """One disposable manifest tree and a separate process HOME."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="luwu-m7-acceptance-")
        self.root = Path(self.temporary.name)
        shutil.copytree(FIXTURE, self.root, dirs_exist_ok=True, symlinks=True)
        for relative, target in (
            (
                "home/.config/mihomo/config.jsonc",
                "excluded-target-must-not-be-followed",
            ),
            (
                "repo/templates/mihomo.jsonc.j2",
                "excluded-source-must-not-be-read",
            ),
            (
                "inputs/local/mihomo-exclusive/state.jsonc",
                "excluded-input-must-not-be-read",
            ),
        ):
            path = self.root / relative
            path.unlink()
            path.symlink_to(target)
        self.manifest = self.root / "luwu.toml"
        self.source_root = self.root / "repo"
        self.input_root = self.root / "inputs"
        self.target_root = self.root / "home"
        self.process_home = self.root / "process-home"
        self.process_home.mkdir()
        self.temporary_dir = self.root / "process-tmp"
        self.temporary_dir.mkdir()

    def close(self) -> None:
        self.temporary.cleanup()

    def cli(
        self,
        *arguments: str,
        expected_code: int | tuple[int, ...],
        profile: str = "synthetic",
    ) -> dict[str, object]:
        command = [
            sys.executable,
            "-B",
            "-m",
            "luwu",
            *arguments,
            "--manifest",
            str(self.manifest),
            "--profile",
            profile,
            "--json",
        ]
        return self._run(command, expected_code, arguments[0])

    def cli_with_engine_hook(
        self,
        program: str,
        hook_argument: Path,
        *arguments: str,
        expected_code: int | tuple[int, ...],
        profile: str = "synthetic",
    ) -> dict[str, object]:
        command = [
            sys.executable,
            "-B",
            "-c",
            program,
            str(hook_argument),
            *arguments,
            "--manifest",
            str(self.manifest),
            "--profile",
            profile,
            "--json",
        ]
        return self._run(command, expected_code, arguments[0])

    def _run(
        self,
        command: list[str],
        expected_code: int | tuple[int, ...],
        command_label: str,
    ) -> dict[str, object]:
        environment = {
            "HOME": str(self.process_home),
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TMPDIR": str(self.temporary_dir),
            "XDG_CACHE_HOME": str(self.process_home / ".cache"),
            "XDG_CONFIG_HOME": str(self.process_home / ".config"),
            "XDG_DATA_HOME": str(self.process_home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.process_home / ".local" / "state"),
        }
        source_package = ROOT / "src"
        if source_package.is_dir():
            environment["PYTHONPATH"] = str(source_package)

        completed = subprocess.run(
            command,
            cwd=self.process_home,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        accepted_codes = (
            (expected_code,) if isinstance(expected_code, int) else expected_code
        )
        if completed.returncode not in accepted_codes:
            error_code: str | None = None
            try:
                failure_payload = json.loads(completed.stdout)
                failure_error = (
                    failure_payload.get("error")
                    if isinstance(failure_payload, dict)
                    else None
                )
                candidate = (
                    failure_error.get("code")
                    if isinstance(failure_error, dict)
                    else None
                )
                error_code = candidate if isinstance(candidate, str) else None
            except json.JSONDecodeError:
                pass
            safe_code = f", error_code={error_code}" if error_code is not None else ""
            raise AssertionError(
                f"CLI {command_label} returned {completed.returncode}; "
                f"expected one of {accepted_codes}{safe_code}"
            )
        if completed.stderr:
            raise AssertionError("JSON CLI unexpectedly wrote to stderr")
        if any(
            sentinel in completed.stdout or sentinel in completed.stderr
            for sentinel in (OUTPUT_SENTINEL, UNKNOWN_SENTINEL)
        ):
            raise AssertionError("CLI output exposed the synthetic output sentinel")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise AssertionError("CLI did not emit one JSON document") from None
        if not isinstance(payload, dict):
            raise TypeError("CLI JSON result must be an object")
        return cast(dict[str, object], payload)

    def expected(self, name: str) -> bytes:
        return (self.root / "expected" / name).read_bytes()

    def snapshot(self) -> dict[str, tuple[object, ...]]:
        """Capture fixture inputs and target entries without following symlinks."""

        state: dict[str, tuple[object, ...]] = {}
        for base_name, base in (
            ("repo", self.source_root),
            ("inputs", self.input_root),
            ("home", self.target_root),
        ):
            for path in sorted(base.rglob("*")):
                relative = f"{base_name}/{path.relative_to(base).as_posix()}"
                info = path.lstat()
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISLNK(info.st_mode):
                    state[relative] = ("symlink", os.readlink(path), mode)
                elif stat.S_ISDIR(info.st_mode):
                    state[relative] = ("directory", mode)
                elif stat.S_ISREG(info.st_mode):
                    state[relative] = ("file", path.read_bytes(), mode)
                else:
                    state[relative] = ("other", mode)
        return state

    def replace_manifest(self, old: str, new: str) -> None:
        body = self.manifest.read_text(encoding="utf-8")
        if body.count(old) != 1:
            raise AssertionError("manifest mutation marker must occur once")
        self.manifest.write_text(body.replace(old, new, 1), encoding="utf-8")


class M7AcceptanceTests(unittest.TestCase):
    def _case(self) -> _M7Case:
        case = _M7Case()
        self.addCleanup(case.close)
        return case

    def _resources(self, payload: dict[str, object]) -> list[dict[str, object]]:
        raw = payload.get("resources")
        if not isinstance(raw, list):
            raise TypeError("M7 plan must report its selected resources")
        resources: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("M7 resource entries must be JSON objects")
            resources.append(cast(dict[str, object], item))
        return resources

    def _actions(self, payload: dict[str, object]) -> list[dict[str, object]]:
        raw = payload.get("actions")
        if not isinstance(raw, list):
            raise TypeError("M7 mutation result must report its actions")
        actions: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("M7 action entries must be JSON objects")
            actions.append(cast(dict[str, object], item))
        return actions

    def _assert_outcome(self, payload: dict[str, object], expected: str) -> None:
        actual = payload.get("outcome")
        if actual != expected:
            error = payload.get("error")
            error_code = error.get("code") if isinstance(error, dict) else None
            self.fail(
                f"expected outcome {expected!r}, got {actual!r}; "
                f"error_code={error_code!r}"
            )

    def _error_code(self, payload: dict[str, object]) -> str:
        error = payload.get("error")
        if not isinstance(error, dict):
            raise TypeError("CLI error payload is missing")
        code = error.get("code")
        if not isinstance(code, str):
            raise TypeError("CLI error code is missing")
        return code

    def _assert_bytes_equal(self, actual: bytes, expected: bytes, label: str) -> None:
        if actual != expected:
            self.fail(f"{label} bytes differ")

    def _assert_snapshot_equal(
        self,
        actual: dict[str, tuple[object, ...]],
        expected: dict[str, tuple[object, ...]],
        label: str,
    ) -> None:
        if actual != expected:
            self.fail(f"{label} changed fixture state")

    def _assert_resource_set(self, payload: dict[str, object], case: _M7Case) -> None:
        resources = self._resources(payload)
        names = tuple(item.get("name") for item in resources)
        self.assertEqual(names, EXPECTED_RESOURCES)
        expected_targets = {
            line
            for line in (case.root / "expected" / "profile-targets.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        }
        actual_targets = {item.get("target") for item in resources}
        self.assertEqual(actual_targets, expected_targets)

    def _accept(
        self,
        case: _M7Case,
        resource: str,
        fields: tuple[str, ...],
        *,
        profile: str = "synthetic",
    ) -> dict[str, object]:
        before = case.snapshot()
        preview = case.cli(
            "accept",
            "--resource",
            resource,
            "--from",
            "desired",
            *self._field_arguments(fields),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(preview, "confirmation_required")
        self._assert_snapshot_equal(case.snapshot(), before, "accept preview")
        result = case.cli(
            "accept",
            "--resource",
            resource,
            "--from",
            "desired",
            *self._field_arguments(fields),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(result, "committed")
        return result

    def _adopt(
        self, case: _M7Case, resource: str, *, profile: str = "synthetic"
    ) -> None:
        before = case.snapshot()
        preview = case.cli(
            "adopt", "--resource", resource, expected_code=2, profile=profile
        )
        self._assert_outcome(preview, "confirmation_required")
        self._assert_snapshot_equal(case.snapshot(), before, "adopt preview")
        result = case.cli(
            "adopt", "--resource", resource, "--yes", expected_code=0, profile=profile
        )
        self._assert_outcome(result, "committed")
        after = case.snapshot()
        before_targets = {
            path: value for path, value in before.items() if path.startswith("home/")
        }
        after_targets = {
            path: value for path, value in after.items() if path.startswith("home/")
        }
        self._assert_snapshot_equal(after_targets, before_targets, "adopt")

    def _field_arguments(self, fields: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(argument for field in fields for argument in ("--field", field))

    def _accept_all(self, case: _M7Case, *, profile: str = "synthetic") -> None:
        self._adopt(case, "copy_profile", profile=profile)
        self._adopt(case, "assets:defaults.conf", profile=profile)
        self._adopt(case, "assets:nested/keymap.jsonc", profile=profile)
        self._accept(case, "settings", ("theme", "brand"), profile=profile)
        self._accept(case, "nested_toml", ("theme", "worker_name"), profile=profile)

    def _finite_batch_targets(self, case: _M7Case) -> dict[str, Path]:
        resources = tuple(
            line
            for line in case.expected("finite-batch-resources.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        targets = tuple(
            line
            for line in case.expected("finite-batch-targets.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        self.assertEqual(len(resources), len(targets))
        return {
            name: case.target_root / target
            for name, target in zip(resources, targets, strict=True)
        }

    def _assert_finite_batch_resource_set(
        self, payload: dict[str, object], case: _M7Case
    ) -> None:
        resources = self._resources(payload)
        expected = self._finite_batch_targets(case)
        names = tuple(
            line
            for line in case.expected("finite-batch-resources.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        self.assertEqual(tuple(item.get("name") for item in resources), names)
        actual_targets = {
            str(item.get("name")): item.get("target") for item in resources
        }
        self.assertEqual(
            actual_targets,
            {
                name: target.relative_to(case.target_root).as_posix()
                for name, target in expected.items()
            },
        )

    def _accept_finite_batch(self, case: _M7Case) -> None:
        for resource in ("batch_a", "batch_b", "batch_c"):
            self._accept(case, resource, ("value",), profile="finite_batch")

    def _assert_deployed_fixture_bytes(self, case: _M7Case) -> None:
        expected = {
            ".config/m7/settings.jsonc": "settings.initial.jsonc",
            ".config/m7/deep/profile/runtime.toml": "nested.initial.toml",
            ".config/m7/profile.ini": "copy-profile.ini",
            ".config/m7/assets/defaults.conf": "assets-defaults.conf",
            ".config/m7/assets/nested/keymap.jsonc": "assets-nested-keymap.jsonc",
        }
        for target_name, expected_name in expected.items():
            self._assert_bytes_equal(
                (case.target_root / target_name).read_bytes(),
                case.expected(expected_name),
                target_name,
            )

    def test_profiled_multi_resource_reverse_and_redeploy_loop(self) -> None:
        case = self._case()
        initial = case.cli("plan", expected_code=0)
        self.assertEqual(initial.get("schema_version"), 8)
        self.assertEqual(initial.get("packages"), ["foundation", "formats", "payloads"])
        self.assertEqual(initial.get("excluded"), [])
        self._assert_resource_set(initial, case)
        self.assertEqual(
            tuple(item.get("name") for item in self._resources(initial)),
            EXPECTED_RESOURCES,
        )
        self._accept_all(case)

        before_apply_preview = case.snapshot()
        preview = case.cli("apply", expected_code=2)
        self._assert_outcome(preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_apply_preview, "apply preview"
        )
        applied = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(applied, "committed")
        self._assert_deployed_fixture_bytes(case)
        self.assertEqual(
            stat.S_IMODE((case.target_root / ".config/m7/deep").stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE((case.target_root / ".config/m7/deep/profile").stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(
                (case.target_root / ".config/m7/settings.jsonc").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(
                (case.target_root / ".config/m7/deep/profile/runtime.toml")
                .stat()
                .st_mode
            ),
            0o600,
        )

        local_settings_theme = case.input_root / "local" / "settings-theme.jsonc"
        self._assert_bytes_equal(
            local_settings_theme.read_bytes(),
            case.expected("local-settings-theme.initial.jsonc"),
            "independent settings theme input before reverse",
        )
        shared_theme_input = case.input_root / "local" / "settings.jsonc"
        shared_theme_before_reverse = shared_theme_input.read_bytes()
        settings_target = case.target_root / ".config/m7/settings.jsonc"
        live_before = settings_target.read_bytes()
        old_theme = b'"synthetic-theme-overlay-v1"'
        new_theme = b'"synthetic-theme-app-v2"'
        self.assertEqual(live_before.count(old_theme), 1)
        live_after_app_edit = live_before.replace(old_theme, new_theme, 1)
        settings_target.write_bytes(live_after_app_edit)
        local_before_reverse = local_settings_theme.read_bytes()

        reverse_preview = case.cli(
            "reverse-sync",
            "--resource",
            "settings",
            "--field",
            "theme",
            expected_code=2,
        )
        self._assert_outcome(reverse_preview, "confirmation_required")
        self._assert_bytes_equal(
            local_settings_theme.read_bytes(),
            local_before_reverse,
            "reverse preview input",
        )
        self._assert_bytes_equal(
            shared_theme_input.read_bytes(),
            shared_theme_before_reverse,
            "unselected shared input during reverse preview",
        )
        self._assert_bytes_equal(
            settings_target.read_bytes(), live_after_app_edit, "reverse preview target"
        )

        reverse = case.cli(
            "reverse-sync",
            "--resource",
            "settings",
            "--field",
            "theme",
            "--yes",
            expected_code=0,
        )
        self._assert_outcome(reverse, "committed")
        self._assert_bytes_equal(
            local_settings_theme.read_bytes(),
            case.expected("local-settings-theme.after-reverse.jsonc"),
            "reverse-updated JSONC input",
        )
        self._assert_bytes_equal(
            shared_theme_input.read_bytes(),
            shared_theme_before_reverse,
            "unselected shared input after reverse",
        )
        self._assert_bytes_equal(
            settings_target.read_bytes(), live_after_app_edit, "reverse target"
        )

        recalculated = case.cli("plan", expected_code=0)
        recalculated_resources = {
            item.get("name"): item for item in self._resources(recalculated)
        }
        self.assertEqual(recalculated_resources["settings"].get("status"), "converged")
        self.assertEqual(recalculated_resources["nested_toml"].get("status"), "in_sync")
        before_confirmed_noop = case.snapshot()
        redeployed = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(redeployed, "no_changes")
        self._assert_snapshot_equal(
            case.snapshot(), before_confirmed_noop, "post-reverse confirmed no-op"
        )
        self._assert_bytes_equal(
            settings_target.read_bytes(),
            case.expected("settings.after-app.jsonc"),
            "application-edited JSONC target",
        )
        nested_target = case.target_root / ".config/m7/deep/profile/runtime.toml"
        self._assert_bytes_equal(
            nested_target.read_bytes(),
            case.expected("nested.after-reverse.toml"),
            "independent TOML input consumer",
        )
        no_op = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(no_op, "no_changes")

        nested_target = case.target_root / ".config/m7/deep/profile/runtime.toml"
        nested_before_app = nested_target.read_bytes()
        old_worker = b'"synthetic-worker-overlay-v1"'
        new_worker = b'"synthetic-worker-app-v2"'
        self.assertEqual(nested_before_app.count(old_worker), 1)
        nested_after_app = nested_before_app.replace(old_worker, new_worker, 1)
        nested_target.write_bytes(nested_after_app)
        local_runtime = case.input_root / "local" / "runtime.toml"
        local_runtime_before = local_runtime.read_bytes()

        nested_preview = case.cli(
            "reverse-sync",
            "--resource",
            "nested_toml",
            "--field",
            "worker_name",
            expected_code=2,
        )
        self._assert_outcome(nested_preview, "confirmation_required")
        self._assert_bytes_equal(
            local_runtime.read_bytes(),
            local_runtime_before,
            "nested reverse preview input",
        )
        self._assert_bytes_equal(
            nested_target.read_bytes(),
            nested_after_app,
            "nested reverse preview target",
        )
        nested_reverse = case.cli(
            "reverse-sync",
            "--resource",
            "nested_toml",
            "--field",
            "worker_name",
            "--yes",
            expected_code=0,
        )
        self._assert_outcome(nested_reverse, "committed")
        self._assert_bytes_equal(
            local_runtime.read_bytes(),
            case.expected("local-runtime.after-reverse.toml"),
            "reverse-updated nested TOML input",
        )
        self._assert_bytes_equal(
            nested_target.read_bytes(), nested_after_app, "nested reverse target"
        )
        self._assert_bytes_equal(
            nested_target.read_bytes(),
            case.expected("nested.after-worker-app.toml"),
            "nested application-edited target",
        )
        self.assertEqual(
            case.cli("apply", "--yes", expected_code=0).get("outcome"),
            "no_changes",
        )

    def test_copy_and_directory_source_updates_are_independent(self) -> None:
        case = self._case()
        self._accept_all(case)
        self._assert_outcome(case.cli("apply", "--yes", expected_code=0), "committed")

        (case.source_root / "copies" / "profile.ini").write_bytes(
            case.expected("copy-profile.after-source-update.ini")
        )
        before_copy_preview = case.snapshot()
        copy_preview = case.cli("apply", expected_code=2)
        self._assert_outcome(copy_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_copy_preview, "copy source preview"
        )
        copy_apply = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(copy_apply, "committed")
        self._assert_bytes_equal(
            (case.target_root / ".config/m7/profile.ini").read_bytes(),
            case.expected("copy-profile.after-source-update.ini"),
            "whole-file copy update",
        )

        defaults_source = case.source_root / "assets" / "defaults.conf"
        defaults_source.write_bytes(
            case.expected("assets-defaults.after-source-update.conf")
        )
        plan = case.cli("plan", expected_code=0)
        resources = {item.get("name"): item for item in self._resources(plan)}
        self.assertEqual(resources["assets:defaults.conf"].get("status"), "drifted")
        self.assertEqual(
            resources["assets:nested/keymap.jsonc"].get("status"), "in_sync"
        )
        before_directory_preview = case.snapshot()
        directory_preview = case.cli("apply", expected_code=2)
        self._assert_outcome(directory_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_directory_preview, "directory source preview"
        )
        directory_apply = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(directory_apply, "committed")
        self._assert_bytes_equal(
            (case.target_root / ".config/m7/assets/defaults.conf").read_bytes(),
            case.expected("assets-defaults.after-source-update.conf"),
            "directory leaf update",
        )
        self._assert_bytes_equal(
            (case.target_root / ".config/m7/assets/nested/keymap.jsonc").read_bytes(),
            case.expected("assets-nested-keymap.jsonc"),
            "unchanged directory leaf",
        )

    def test_source_owned_jsonc_input_update_requires_confirmation(self) -> None:
        case = self._case()
        self._accept_all(case)
        self._assert_outcome(case.cli("apply", "--yes", expected_code=0), "committed")
        global_settings = case.input_root / "global" / "settings.jsonc"
        global_settings.write_bytes(
            case.expected("global-settings.after-brand-update.jsonc")
        )
        before_preview = case.snapshot()
        preview = case.cli("apply", expected_code=2)
        self._assert_outcome(preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_preview, "input update preview"
        )
        applied = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(applied, "committed")
        self._assert_bytes_equal(
            (case.target_root / ".config/m7/settings.jsonc").read_bytes(),
            case.expected("settings.after-brand-update.jsonc"),
            "source-owned JSONC update",
        )

    def test_wsl_profile_excludes_inaccessible_payload_paths(self) -> None:
        case = self._case()
        before = case.snapshot()
        manifest_before = case.manifest.read_bytes()
        wsl_plan = case.cli("plan", profile="wsl", expected_code=0)
        self.assertEqual(wsl_plan.get("schema_version"), 8)
        self.assertEqual(
            wsl_plan.get("packages"), ["foundation", "formats", "payloads"]
        )
        self._assert_resource_set(wsl_plan, case)
        self.assertEqual(wsl_plan.get("excluded"), ["mihomo"])
        self._assert_snapshot_equal(case.snapshot(), before, "WSL profile plan")
        self.assertEqual(case.manifest.read_bytes(), manifest_before)
        self.assertTrue(
            (case.source_root / "templates" / "mihomo.jsonc.j2").is_symlink()
        )
        self.assertEqual(
            os.readlink(case.source_root / "templates" / "mihomo.jsonc.j2"),
            "excluded-source-must-not-be-read",
        )
        self.assertTrue(
            (
                case.input_root / "local" / "mihomo-exclusive" / "state.jsonc"
            ).is_symlink()
        )
        self.assertEqual(
            os.readlink(case.input_root / "local" / "mihomo-exclusive" / "state.jsonc"),
            "excluded-input-must-not-be-read",
        )
        excluded_target = case.target_root / ".config/mihomo/config.jsonc"
        self.assertTrue(excluded_target.is_symlink())
        self.assertEqual(
            os.readlink(excluded_target), "excluded-target-must-not-be-followed"
        )

    def test_dependency_cycles_unknown_packages_and_excluded_dependency_block(
        self,
    ) -> None:
        cycle = self._case()
        before_cycle = cycle.snapshot()
        cycle.replace_manifest(
            "[packages.foundation]\ndependencies = []",
            '[packages.foundation]\ndependencies = ["formats"]',
        )
        cycle.cli("plan", expected_code=2)
        self._assert_snapshot_equal(cycle.snapshot(), before_cycle, "cycle block")

        unknown = self._case()
        before_unknown = unknown.snapshot()
        unknown.replace_manifest(
            'packages = ["payloads", "formats"]',
            'packages = ["payloads", "missing_package"]',
        )
        unknown.cli("plan", expected_code=2)
        self._assert_snapshot_equal(
            unknown.snapshot(), before_unknown, "unknown package block"
        )

        excluded_dependency = self._case()
        before_excluded_dependency = excluded_dependency.snapshot()
        excluded_dependency.replace_manifest(
            'dependencies = ["foundation"]\nresources = ["settings", "nested_toml"]',
            'dependencies = ["foundation", "mihomo"]\nresources = ["settings", "nested_toml"]',
        )
        excluded_dependency.cli("plan", profile="wsl", expected_code=2)
        self._assert_snapshot_equal(
            excluded_dependency.snapshot(),
            before_excluded_dependency,
            "excluded dependency block",
        )

    def test_excluded_resource_and_exclusive_input_have_zero_path_access(self) -> None:
        case = self._case()
        report = case.process_home / "excluded-accesses.json"
        before = case.snapshot()
        result = case.cli_with_engine_hook(
            _EXCLUDED_ACCESS_HOOK,
            report,
            "plan",
            profile="wsl",
            expected_code=(0, 86),
        )
        self.assertEqual(result.get("excluded"), ["mihomo"])
        counts = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(
            counts,
            dict.fromkeys(
                (
                    "access",
                    "builtins.open",
                    "chmod",
                    "chown",
                    "fchmod",
                    "fchown",
                    "io.open",
                    "link",
                    "listdir",
                    "lstat",
                    "mkdir",
                    "os.open",
                    "readlink",
                    "remove",
                    "rename",
                    "replace",
                    "rmdir",
                    "scandir",
                    "stat",
                    "symlink",
                    "unlink",
                ),
                0,
            ),
        )
        self._assert_snapshot_equal(case.snapshot(), before, "excluded path access")

    def test_wsl_apply_does_not_access_or_mutate_excluded_paths(self) -> None:
        case = self._case()
        self._accept_all(case, profile="wsl")
        before = case.snapshot()
        excluded_before = {
            path: value
            for path, value in before.items()
            if path.startswith(
                ("inputs/local/mihomo-exclusive/", "home/.config/mihomo/")
            )
        }
        report = case.process_home / "wsl-apply-excluded-accesses.json"
        result = case.cli_with_engine_hook(
            _EXCLUDED_ACCESS_HOOK,
            report,
            "apply",
            "--yes",
            profile="wsl",
            expected_code=0,
        )
        self._assert_outcome(result, "committed")
        counts = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(set(counts.values()), {0})
        after = case.snapshot()
        excluded_after = {
            path: value
            for path, value in after.items()
            if path.startswith(
                ("inputs/local/mihomo-exclusive/", "home/.config/mihomo/")
            )
        }
        self._assert_snapshot_equal(
            excluded_after, excluded_before, "WSL excluded paths"
        )

    def test_shared_consumer_divergence_blocks_reverse_before_any_write(self) -> None:
        case = self._case()
        self._adopt(case, "copy_profile", profile="shared_consumer")
        self._accept(
            case,
            "settings_shared",
            ("theme", "brand"),
            profile="shared_consumer",
        )
        self._accept(
            case,
            "nested_toml_consumer",
            ("theme",),
            profile="shared_consumer",
        )
        self._assert_outcome(
            case.cli("apply", "--yes", profile="shared_consumer", expected_code=0),
            "committed",
        )
        plan = case.cli("plan", profile="shared_consumer", expected_code=0)
        resources = self._resources(plan)
        expected_names = tuple(
            line
            for line in case.expected("shared-consumer-resources.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        expected_targets = {
            line
            for line in case.expected("shared-consumer-targets.txt")
            .decode("utf-8")
            .splitlines()
            if line
        }
        self.assertEqual(
            tuple(sorted(str(item.get("name")) for item in resources)),
            expected_names,
        )
        self.assertEqual(
            {str(item.get("target")) for item in resources}, expected_targets
        )
        shared_target = case.target_root / ".config/m7/shared/runtime.toml"
        self._assert_bytes_equal(
            shared_target.read_bytes(),
            case.expected("nested-toml-consumer.initial.toml"),
            "unselected nested TOML consumer before reverse",
        )
        shared_settings_target = case.target_root / ".config/m7/shared/settings.jsonc"
        shared_settings_target.write_bytes(case.expected("settings.after-app.jsonc"))
        before = case.snapshot()

        preview = case.cli(
            "reverse-sync",
            "--resource",
            "settings_shared",
            "--field",
            "theme",
            expected_code=2,
            profile="shared_consumer",
        )
        self.assertEqual(self._error_code(preview), "m7_reverse_consumer_change")
        self._assert_snapshot_equal(
            case.snapshot(), before, "shared-consumer reverse preview"
        )

        report = case.process_home / "shared-consumer-write-counts.json"
        confirmed = case.cli_with_engine_hook(
            _MUTATION_COUNT_HOOK,
            report,
            "reverse-sync",
            "--resource",
            "settings_shared",
            "--field",
            "theme",
            "--yes",
            expected_code=2,
            profile="shared_consumer",
        )
        self.assertEqual(self._error_code(confirmed), "m7_reverse_consumer_change")
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8")),
            {"create_parent": 0, "write_file": 0},
        )
        self._assert_snapshot_equal(
            case.snapshot(), before, "shared-consumer reverse confirmation"
        )

    def test_unknown_empty_containers_remain_observable_and_cannot_be_accepted(
        self,
    ) -> None:
        case = self._case()
        self._accept_all(case)
        self._assert_outcome(case.cli("apply", "--yes", expected_code=0), "committed")
        settings_target = case.target_root / ".config/m7/settings.jsonc"
        settings_target.write_bytes(case.expected("settings.unknown-container.jsonc"))
        before = case.snapshot()

        plan = case.cli("plan", expected_code=0)
        self._assert_snapshot_equal(case.snapshot(), before, "unknown-container plan")
        settings = next(
            item for item in self._resources(plan) if item.get("name") == "settings"
        )
        self.assertTrue(settings.get("undeclared_changed"))
        self.assertEqual(settings.get("status"), "review")
        self.assertEqual(settings.get("action"), "blocked")
        self.assertEqual(settings.get("error_code"), "m7_review_required")
        self.assertNotIn(UNKNOWN_SENTINEL, json.dumps(plan, sort_keys=True))

        accepted = case.cli(
            "accept",
            "--resource",
            "settings",
            "--from",
            "desired",
            "--field",
            "theme",
            "--yes",
            expected_code=2,
        )
        self.assertEqual(self._error_code(accepted), "m7_review_required")
        self._assert_snapshot_equal(case.snapshot(), before, "unknown-container accept")

        blocked = case.cli("apply", "--yes", expected_code=2)
        self.assertEqual(blocked.get("outcome"), "not_attempted")
        self.assertEqual(blocked.get("error_code"), "m7_review_required")
        self._assert_snapshot_equal(case.snapshot(), before, "unknown-container apply")

    def test_tampered_plan_bytes_are_rejected_before_any_writer(self) -> None:
        case = self._case()
        source_package = ROOT / "src"
        if source_package.is_dir() and str(source_package) not in sys.path:
            sys.path.insert(0, str(source_package))
        engine = importlib.import_module("luwu.m7_engine")
        manifest_module = importlib.import_module("luwu.m7_manifest")

        def build_current_plan():
            manifest = manifest_module.load_manifest(case.manifest, profile="synthetic")
            return engine.build_plan(manifest)

        for resource in (
            "copy_profile",
            "assets:defaults.conf",
            "assets:nested/keymap.jsonc",
        ):
            result = engine.adopt_resource(
                build_current_plan(), resource=resource, confirm=True
            )
            self.assertEqual(result.outcome, "committed")
        for resource, fields in (
            ("settings", ("theme", "brand")),
            ("nested_toml", ("theme", "worker_name")),
        ):
            result = engine.accept_baseline(
                build_current_plan(),
                resource=resource,
                fields=fields,
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(result.outcome, "committed")

        plan = build_current_plan()
        settings = next(
            item for item in plan._resources if item.resource.name == "settings"
        )
        self.assertIsNotNone(settings.write_data)
        object.__setattr__(settings, "write_data", b"tampered-plan-output-v1\n")
        before = case.snapshot()
        with (
            patch.object(engine, "write_file", wraps=engine.write_file) as write_file,
            patch.object(
                engine, "create_parent", wraps=engine.create_parent
            ) as create_parent,
            self.assertRaises(engine.M7EngineError) as raised,
        ):
            engine.apply_plan(plan, confirm=True)
        self.assertEqual(raised.exception.code, "m7_plan_invalid")
        self.assertEqual(write_file.call_count, 0)
        self.assertEqual(create_parent.call_count, 0)
        self._assert_snapshot_equal(case.snapshot(), before, "tampered plan apply")

    def test_duplicate_leaf_conflict_unknown_input_and_selected_alias_block(
        self,
    ) -> None:
        same_leaf = self._case()
        before_same_leaf = same_leaf.snapshot()
        same_leaf.replace_manifest(
            '[resources.settings.fields.brand]\npath = ["ui", "brand"]',
            '[resources.settings.fields.brand]\npath = ["ui", "theme"]',
        )
        same_leaf.cli("plan", expected_code=2)
        self._assert_snapshot_equal(
            same_leaf.snapshot(), before_same_leaf, "same-leaf block"
        )

        conflict = self._case()
        self._accept_all(conflict)
        self.assertEqual(
            conflict.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        settings_target = conflict.target_root / ".config/m7/settings.jsonc"
        live_bytes = settings_target.read_bytes()
        old_value = b'"synthetic-theme-overlay-v1"'
        self.assertEqual(live_bytes.count(old_value), 1)
        settings_target.write_bytes(
            live_bytes.replace(old_value, b'"synthetic-theme-live-v2"', 1)
        )
        local_settings = conflict.input_root / "local" / "settings-theme.jsonc"
        local_bytes = local_settings.read_bytes()
        self.assertEqual(local_bytes.count(old_value), 1)
        local_settings.write_bytes(
            local_bytes.replace(old_value, b'"synthetic-theme-source-v2"', 1)
        )
        before_conflict_mutations = conflict.snapshot()
        conflict.cli(
            "reverse-sync",
            "--resource",
            "settings",
            "--field",
            "theme",
            "--yes",
            expected_code=2,
        )
        conflict.cli("apply", "--yes", expected_code=2)
        self._assert_snapshot_equal(
            conflict.snapshot(), before_conflict_mutations, "conflict block"
        )

        unknown = self._case()
        self._accept_all(unknown)
        self.assertEqual(
            unknown.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        bad_input = unknown.input_root / "local" / "settings-theme.jsonc"
        bad_input.write_bytes(b"\xff")
        before_unknown_apply = unknown.snapshot()
        unknown.cli("apply", "--yes", expected_code=2)
        self._assert_snapshot_equal(
            unknown.snapshot(), before_unknown_apply, "unknown input block"
        )

        alias = self._case()
        before_alias = alias.snapshot()
        alias.cli("plan", profile="alias", expected_code=2)
        self._assert_snapshot_equal(
            alias.snapshot(), before_alias, "selected alias block"
        )

    def test_comment_only_stale_input_blocks_confirm_and_ablation_is_caught(
        self,
    ) -> None:
        case = self._case()
        self._accept_all(case)
        self.assertEqual(
            case.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        settings_target = case.target_root / ".config/m7/settings.jsonc"
        target_before = settings_target.read_bytes()
        global_settings = case.input_root / "global" / "settings.jsonc"
        global_settings.write_bytes(
            case.expected("global-settings.after-brand-update.jsonc")
        )
        before_preview = case.snapshot()
        case.cli("apply", expected_code=2)
        self._assert_snapshot_equal(
            case.snapshot(), before_preview, "comment-race preview"
        )

        blocked = case.cli_with_engine_hook(
            _COMMENT_RACE_HOOK.replace("ABLATEME", ""),
            global_settings,
            "apply",
            "--yes",
            expected_code=2,
        )
        self.assertNotEqual(blocked.get("outcome"), "committed")
        self._assert_bytes_equal(
            settings_target.read_bytes(), target_before, "stale comment race target"
        )
        self._assert_bytes_equal(
            global_settings.read_bytes(),
            case.expected("global-settings.comment-race.jsonc"),
            "raced comment-only input",
        )

        ablated = self._case()
        self._accept_all(ablated)
        self.assertEqual(
            ablated.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        ablated_target = ablated.target_root / ".config/m7/settings.jsonc"
        ablated_global = ablated.input_root / "global" / "settings.jsonc"
        ablated_global.write_bytes(
            ablated.expected("global-settings.after-brand-update.jsonc")
        )
        ablated_program = _COMMENT_RACE_HOOK.replace(
            "ABLATEME",
            "engine._assert_fresh = lambda record, **_kwargs: record.resources",
        )
        ablated.cli_with_engine_hook(
            ablated_program,
            ablated_global,
            "apply",
            "--yes",
            expected_code=2,
        )
        # This separate control proves the fixture byte oracle catches a disabled
        # freshness guard: the raced command changes the target before failing.
        self._assert_bytes_equal(
            ablated_target.read_bytes(),
            ablated.expected("settings.after-brand-update.jsonc"),
            "freshness ablation control target",
        )

    def test_future_resource_change_keeps_prior_independent_writes_partial(
        self,
    ) -> None:
        case = self._case()
        self._accept_finite_batch(case)
        self._assert_outcome(
            case.cli("apply", "--yes", profile="finite_batch", expected_code=0),
            "committed",
        )
        targets = self._finite_batch_targets(case)
        for resource, value in (
            ("batch_a", "finite-batch-a.initial.json"),
            ("batch_b", "finite-batch-b.initial.json"),
            ("batch_c", "finite-batch-c.initial.json"),
        ):
            self._assert_bytes_equal(
                targets[resource].read_bytes(), case.expected(value), resource
            )

        for resource in ("batch_a", "batch_b", "batch_c"):
            source = (
                case.input_root
                / "local"
                / f"finite-{resource.replace('batch_', 'batch-')}.json"
            )
            expected_name = (
                f"finite-{resource.replace('_', '-')}.after-source-update.json"
            )
            source.write_bytes(case.expected(expected_name))

        plan = case.cli("plan", profile="finite_batch", expected_code=0)
        self._assert_finite_batch_resource_set(plan, case)
        planned = {item.get("name"): item for item in self._resources(plan)}
        self.assertEqual(
            {name for name, item in planned.items() if item.get("status") == "drifted"},
            {"batch_a", "batch_b", "batch_c"},
        )
        before_preview = case.snapshot()
        preview = case.cli("apply", profile="finite_batch", expected_code=2)
        self._assert_outcome(preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_preview, "finite-batch preview"
        )

        control = case.process_home / "future-source-control.json"
        report = case.process_home / "future-source-report.json"
        future_input = case.input_root / "local/finite-batch-c.json"
        control.write_text(
            json.dumps(
                {
                    "trigger_targets": [str(targets["batch_a"])],
                    "mutation_path": str(future_input),
                    "replacement_hex": case.expected(
                        "finite-batch-c.after-preflight-race.json"
                    ).hex(),
                    "report": str(report),
                }
            ),
            encoding="utf-8",
        )

        # C changes after A returns; B is independent and remains eligible to write.
        result = case.cli_with_engine_hook(
            _FUTURE_SOURCE_CHANGE_HOOK,
            control,
            "apply",
            "--yes",
            expected_code=2,
            profile="finite_batch",
        )
        self._assert_outcome(result, "partial")
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8")),
            {
                "mutated": True,
                "completed": [str(targets["batch_a"])],
            },
        )
        target_states = {
            action.get("resource"): action.get("state")
            for action in self._actions(result)
            if action.get("kind") == "target_write"
        }
        self.assertEqual(
            target_states,
            {
                "batch_a": "committed",
                "batch_b": "committed",
                "batch_c": "not_attempted",
            },
        )
        self._assert_bytes_equal(
            targets["batch_a"].read_bytes(),
            case.expected("finite-batch-a.after-source-update.json"),
            "batch A committed before future C changed",
        )
        self._assert_bytes_equal(
            targets["batch_b"].read_bytes(),
            case.expected("finite-batch-b.after-source-update.json"),
            "independent batch B remains eligible after C changed",
        )
        self._assert_bytes_equal(
            future_input.read_bytes(),
            case.expected("finite-batch-c.after-preflight-race.json"),
            "future C input changed after preflight",
        )
        self._assert_bytes_equal(
            targets["batch_c"].read_bytes(),
            case.expected("finite-batch-c.initial.json"),
            "C target remains untouched at its stale write boundary",
        )

    def test_preflight_checks_noop_target_before_first_write(self) -> None:
        case = self._case()
        self._accept_finite_batch(case)
        self._assert_outcome(
            case.cli("apply", "--yes", profile="finite_batch", expected_code=0),
            "committed",
        )
        targets = self._finite_batch_targets(case)
        (case.input_root / "local/finite-batch-a.json").write_bytes(
            case.expected("finite-batch-a.after-source-update.json")
        )

        plan = case.cli("plan", profile="finite_batch", expected_code=0)
        self._assert_finite_batch_resource_set(plan, case)
        planned = {item.get("name"): item for item in self._resources(plan)}
        self.assertEqual(planned["batch_a"].get("status"), "drifted")
        self.assertIn(planned["batch_c"].get("status"), {"in_sync", "converged"})

        control = case.process_home / "noop-preflight-control.json"
        report = case.process_home / "noop-preflight-report.json"
        control.write_text(
            json.dumps(
                {
                    "noop_target": str(targets["batch_c"]),
                    "replacement_hex": case.expected(
                        "finite-batch-c.noop-after-preflight.json"
                    ).hex(),
                    "report": str(report),
                }
            ),
            encoding="utf-8",
        )

        result = case.cli_with_engine_hook(
            _NOOP_PREFLIGHT_CHANGE_HOOK,
            control,
            "apply",
            "--yes",
            expected_code=2,
            profile="finite_batch",
        )
        self.assertEqual(result.get("outcome"), "not_attempted")
        self.assertEqual(result.get("error_code"), "m7_stale_plan")
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8")),
            {"mutated": True, "counts": {"create_parent": 0, "write_file": 0}},
        )
        self._assert_bytes_equal(
            targets["batch_a"].read_bytes(),
            case.expected("finite-batch-a.initial.json"),
            "preflight stale noop prevents first write",
        )
        self._assert_bytes_equal(
            targets["batch_c"].read_bytes(),
            case.expected("finite-batch-c.noop-after-preflight.json"),
            "externally changed noop remains visible",
        )

    def test_noop_target_change_fails_final_verification(self) -> None:
        case = self._case()
        self._accept_finite_batch(case)
        self._assert_outcome(
            case.cli("apply", "--yes", profile="finite_batch", expected_code=0),
            "committed",
        )
        targets = self._finite_batch_targets(case)
        (case.input_root / "local/finite-batch-a.json").write_bytes(
            case.expected("finite-batch-a.after-source-update.json")
        )

        plan = case.cli("plan", profile="finite_batch", expected_code=0)
        self._assert_finite_batch_resource_set(plan, case)
        planned = {item.get("name"): item for item in self._resources(plan)}
        self.assertEqual(planned["batch_a"].get("status"), "drifted")
        self.assertIn(planned["batch_c"].get("status"), {"in_sync", "converged"})

        control = case.process_home / "noop-final-control.json"
        report = case.process_home / "noop-final-report.json"
        control.write_text(
            json.dumps(
                {
                    "trigger_target": str(targets["batch_a"]),
                    "noop_target": str(targets["batch_c"]),
                    "replacement_hex": case.expected(
                        "finite-batch-c.noop-after-preflight.json"
                    ).hex(),
                    "report": str(report),
                }
            ),
            encoding="utf-8",
        )

        result = case.cli_with_engine_hook(
            _NOOP_FINAL_CHANGE_HOOK,
            control,
            "apply",
            "--yes",
            expected_code=2,
            profile="finite_batch",
        )
        self._assert_outcome(result, "committed_but_verification_failed")
        self.assertNotEqual(result.get("verification_status"), "verified")
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8")), {"mutated": True}
        )
        target_states = {
            action.get("resource"): action.get("state")
            for action in self._actions(result)
            if action.get("kind") == "target_write"
        }
        self.assertEqual(target_states, {"batch_a": "committed"})
        self._assert_bytes_equal(
            targets["batch_a"].read_bytes(),
            case.expected("finite-batch-a.after-source-update.json"),
            "declared write before final verification",
        )
        self._assert_bytes_equal(
            targets["batch_c"].read_bytes(),
            case.expected("finite-batch-c.noop-after-preflight.json"),
            "externally changed noop target",
        )

    def test_partial_parent_creation_and_partial_target_write_are_reported(
        self,
    ) -> None:
        parent_case = self._case()
        self._accept_all(parent_case)
        self.assertEqual(
            parent_case.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        nested_target = parent_case.target_root / ".config/m7/deep/profile/runtime.toml"
        nested_target.unlink()
        (parent_case.target_root / ".config/m7/deep/profile").rmdir()
        (parent_case.target_root / ".config/m7/deep").rmdir()
        partial_parent = parent_case.cli_with_engine_hook(
            _FAIL_PARENT_HOOK,
            Path("profile"),
            "apply",
            "--yes",
            expected_code=2,
        )
        self._assert_outcome(partial_parent, "partial")
        self.assertTrue((parent_case.target_root / ".config/m7/deep").is_dir())
        self.assertFalse((parent_case.target_root / ".config/m7/deep/profile").exists())
        self.assertFalse(nested_target.exists())
        mkdir_states = {
            action.get("path"): action.get("state")
            for action in self._actions(partial_parent)
            if action.get("kind") == "mkdir"
        }
        self.assertEqual(
            mkdir_states,
            {
                ".config/m7/deep": "committed",
                ".config/m7/deep/profile": "not_attempted",
            },
        )

        write_case = self._case()
        self._accept_all(write_case)
        self.assertEqual(
            write_case.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        (write_case.input_root / "global" / "settings.jsonc").write_bytes(
            write_case.expected("global-settings.after-brand-update.jsonc")
        )
        (write_case.input_root / "local" / "runtime.toml").write_bytes(
            write_case.expected("local-runtime.after-source-update.toml")
        )
        old_settings = (
            write_case.target_root / ".config/m7/settings.jsonc"
        ).read_bytes()
        partial_write = write_case.cli_with_engine_hook(
            _FAIL_WRITE_HOOK,
            Path("settings.jsonc"),
            "apply",
            "--yes",
            expected_code=2,
        )
        self._assert_outcome(partial_write, "partial")
        self._assert_bytes_equal(
            (
                write_case.target_root / ".config/m7/deep/profile/runtime.toml"
            ).read_bytes(),
            write_case.expected("nested.after-worker-source-update.toml"),
            "committed first target",
        )
        self._assert_bytes_equal(
            (write_case.target_root / ".config/m7/settings.jsonc").read_bytes(),
            old_settings,
            "failed second target remains unchanged",
        )
        write_states = {
            action.get("resource"): action.get("state")
            for action in self._actions(partial_write)
            if action.get("kind") == "target_write"
        }
        self.assertEqual(
            write_states,
            {"nested_toml": "committed", "settings": "not_replaced"},
        )

    def test_official_application_shapes_reverse_and_redeploy_from_fixtures(
        self,
    ) -> None:
        case = self._case()
        profile = "official_shapes"
        codex_hash_initial = (
            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        )
        codex_hash_after_app = (
            "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        )
        planned = case.cli("plan", expected_code=0, profile=profile)
        self.assertNotIn(codex_hash_initial, json.dumps(planned))
        expected_resources = tuple(
            line
            for line in case.expected("official-shapes-resources.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        expected_targets = {
            line
            for line in case.expected("official-shapes-targets.txt")
            .decode("utf-8")
            .splitlines()
            if line
        }
        selected = self._resources(planned)
        selected_names = tuple(item.get("name") for item in selected)
        self.assertEqual(selected_names, expected_resources)
        self.assertEqual(len(selected_names), len(set(selected_names)))
        selected_targets = tuple(item.get("target") for item in selected)
        self.assertEqual(set(selected_targets), expected_targets)
        self.assertEqual(len(selected_targets), len(expected_targets))
        self.assertEqual(planned.get("packages"), ["official_apps"])
        self.assertEqual(planned.get("excluded"), [])

        codex_accept = self._accept(
            case,
            "codex",
            ("project_trust", "hook_enabled", "hook_trusted_hash"),
            profile=profile,
        )
        self.assertNotIn(codex_hash_initial, json.dumps(codex_accept))
        self._accept(
            case,
            "opencode",
            ("schema", "model", "autoupdate", "port", "hostname"),
            profile=profile,
        )
        self._accept(
            case,
            "droid",
            ("interaction_mode", "autonomy_level"),
            profile=profile,
        )

        before_apply = case.snapshot()
        apply_preview = case.cli("apply", expected_code=2, profile=profile)
        self._assert_outcome(apply_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_apply, "official apply preview"
        )
        applied = case.cli("apply", "--yes", expected_code=0, profile=profile)
        self._assert_outcome(applied, "committed")
        self.assertNotIn(codex_hash_initial, json.dumps(applied))

        codex_target = case.target_root / ".codex/config.toml"
        opencode_target = case.target_root / ".config/opencode/opencode.jsonc"
        self._assert_bytes_equal(
            codex_target.read_bytes(), case.expected("codex.initial.toml"), "Codex TOML"
        )
        self._assert_bytes_equal(
            opencode_target.read_bytes(),
            case.expected("opencode.initial.jsonc"),
            "OpenCode JSONC",
        )
        droid_target = case.target_root / ".factory/settings.json"
        self._assert_bytes_equal(
            droid_target.read_bytes(),
            case.expected("droid-settings.initial.json"),
            "Droid settings JSON",
        )
        hook_id = "/synthetic/luwu-m7/hooks.json:pre_tool_use:0:0"
        initial_codex = codex_target.read_text(encoding="utf-8")
        self.assertEqual(initial_codex.count(f'[hooks.state."{hook_id}"]'), 1)
        self.assertEqual(initial_codex.count(codex_hash_initial), 1)
        self.assertIn('[projects."/synthetic/luwu-m7/project.a"]', initial_codex)
        self.assertIn('trust_level = "untrusted"', initial_codex)
        self.assertIn("enabled = false", initial_codex)

        # These fixture byte replacements model an application editing its own files.
        codex_target.write_bytes(case.expected("codex.after-app.toml"))
        opencode_target.write_bytes(case.expected("opencode.after-app.jsonc"))
        droid_target.write_bytes(case.expected("droid-settings.after-app.json"))
        codex_local = case.input_root / "local/codex-runtime.toml"
        opencode_local = case.input_root / "local/opencode.jsonc"
        droid_local = case.input_root / "local/droid-settings.json"
        opencode_local_before = opencode_local.read_bytes()
        droid_local_before = droid_local.read_bytes()
        codex_live_after_app = codex_target.read_bytes()
        opencode_live_after_app = opencode_target.read_bytes()
        droid_live_after_app = droid_target.read_bytes()
        before_reverse = case.snapshot()

        codex_preview = case.cli(
            "reverse-sync",
            "--resource",
            "codex",
            *self._field_arguments(
                ("project_trust", "hook_enabled", "hook_trusted_hash")
            ),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(codex_preview, "confirmation_required")
        self.assertNotIn(codex_hash_after_app, json.dumps(codex_preview))
        self._assert_snapshot_equal(
            case.snapshot(), before_reverse, "Codex reverse preview"
        )

        codex_reverse = case.cli(
            "reverse-sync",
            "--resource",
            "codex",
            *self._field_arguments(
                ("project_trust", "hook_enabled", "hook_trusted_hash")
            ),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(codex_reverse, "committed")
        self.assertNotIn(codex_hash_after_app, json.dumps(codex_reverse))
        self._assert_bytes_equal(
            codex_local.read_bytes(),
            case.expected("codex-local.after-reverse.toml"),
            "Codex reverse input",
        )
        self._assert_bytes_equal(
            codex_target.read_bytes(), codex_live_after_app, "Codex reverse target"
        )
        self.assertEqual(
            codex_local.read_text(encoding="utf-8").count(codex_hash_after_app), 1
        )

        before_opencode_reverse = case.snapshot()
        opencode_preview = case.cli(
            "reverse-sync",
            "--resource",
            "opencode",
            *self._field_arguments(("model", "autoupdate", "port", "hostname")),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(opencode_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_opencode_reverse, "OpenCode reverse preview"
        )
        self._assert_bytes_equal(
            opencode_local.read_bytes(), opencode_local_before, "OpenCode preview input"
        )
        self._assert_bytes_equal(
            opencode_target.read_bytes(),
            opencode_live_after_app,
            "OpenCode preview target",
        )

        opencode_reverse = case.cli(
            "reverse-sync",
            "--resource",
            "opencode",
            *self._field_arguments(("model", "autoupdate", "port", "hostname")),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(opencode_reverse, "committed")
        self._assert_bytes_equal(
            opencode_local.read_bytes(),
            case.expected("opencode-local.after-reverse.jsonc"),
            "OpenCode reverse input",
        )
        self._assert_bytes_equal(
            opencode_target.read_bytes(),
            opencode_live_after_app,
            "OpenCode reverse target",
        )

        before_droid_reverse = case.snapshot()
        droid_preview = case.cli(
            "reverse-sync",
            "--resource",
            "droid",
            *self._field_arguments(("interaction_mode", "autonomy_level")),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(droid_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_droid_reverse, "Droid reverse preview"
        )
        self._assert_bytes_equal(
            droid_local.read_bytes(), droid_local_before, "Droid preview input"
        )
        self._assert_bytes_equal(
            droid_target.read_bytes(), droid_live_after_app, "Droid preview target"
        )

        droid_reverse = case.cli(
            "reverse-sync",
            "--resource",
            "droid",
            *self._field_arguments(("interaction_mode", "autonomy_level")),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(droid_reverse, "committed")
        self._assert_bytes_equal(
            droid_local.read_bytes(),
            case.expected("droid-settings.local.after-reverse.json"),
            "Droid reverse input",
        )
        self._assert_bytes_equal(
            droid_target.read_bytes(), droid_live_after_app, "Droid reverse target"
        )

        recalculated = case.cli("plan", expected_code=0, profile=profile)
        self.assertNotIn(codex_hash_after_app, json.dumps(recalculated))
        recalculated_by_name = {
            item.get("name"): item for item in self._resources(recalculated)
        }
        self.assertEqual(set(recalculated_by_name), set(expected_resources))
        self.assertEqual(recalculated_by_name["codex"].get("status"), "converged")
        self.assertEqual(recalculated_by_name["opencode"].get("status"), "converged")
        self.assertEqual(recalculated_by_name["droid"].get("status"), "converged")
        before_confirmed_noop = case.snapshot()
        redeployed = case.cli("apply", "--yes", expected_code=0, profile=profile)
        self._assert_outcome(redeployed, "no_changes")
        self.assertNotIn(codex_hash_after_app, json.dumps(redeployed))
        self._assert_snapshot_equal(
            case.snapshot(), before_confirmed_noop, "official-shape confirmed no-op"
        )
        self._assert_bytes_equal(
            codex_target.read_bytes(),
            case.expected("codex.after-app.toml"),
            "Codex app edit",
        )
        self._assert_bytes_equal(
            opencode_target.read_bytes(),
            case.expected("opencode.after-app.jsonc"),
            "OpenCode app edit",
        )
        self._assert_bytes_equal(
            droid_target.read_bytes(),
            case.expected("droid-settings.after-app.json"),
            "Droid settings app edit",
        )

    def test_droid_settings_bidirectional_fixture_excludes_workspace_trust(
        self,
    ) -> None:
        case = self._case()
        profile = "droid_settings"
        planned = case.cli("plan", expected_code=0, profile=profile)
        expected_resources = tuple(
            line
            for line in case.expected("droid-settings-resources.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        expected_targets = {
            line
            for line in case.expected("droid-settings-targets.txt")
            .decode("utf-8")
            .splitlines()
            if line
        }
        resources = self._resources(planned)
        self.assertEqual(
            tuple(item.get("name") for item in resources), expected_resources
        )
        self.assertEqual({item.get("target") for item in resources}, expected_targets)
        self.assertEqual(planned.get("packages"), ["official_droid"])
        self.assertEqual(planned.get("excluded"), [])
        self.assertEqual(expected_targets, {".factory/settings.json"})

        fields = ("interaction_mode", "autonomy_level")
        self._accept(case, "droid", fields, profile=profile)
        before_apply = case.snapshot()
        apply_preview = case.cli("apply", expected_code=2, profile=profile)
        self._assert_outcome(apply_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_apply, "Droid settings apply preview"
        )
        applied = case.cli("apply", "--yes", expected_code=0, profile=profile)
        self._assert_outcome(applied, "committed")

        target = case.target_root / ".factory/settings.json"
        local = case.input_root / "local/droid-settings.json"
        self._assert_bytes_equal(
            target.read_bytes(),
            case.expected("droid-settings.initial.json"),
            "Droid settings initial target",
        )
        local_before = local.read_bytes()
        target.write_bytes(case.expected("droid-settings.after-app.json"))
        app_edited_target = target.read_bytes()
        before_reverse = case.snapshot()

        reverse_preview = case.cli(
            "reverse-sync",
            "--resource",
            "droid",
            *self._field_arguments(fields),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(reverse_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_reverse, "Droid settings reverse preview"
        )
        self._assert_bytes_equal(
            local.read_bytes(), local_before, "Droid settings preview input"
        )

        reverse = case.cli(
            "reverse-sync",
            "--resource",
            "droid",
            *self._field_arguments(fields),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(reverse, "committed")
        self._assert_bytes_equal(
            local.read_bytes(),
            case.expected("droid-settings.local.after-reverse.json"),
            "Droid settings reverse input",
        )
        self._assert_bytes_equal(
            target.read_bytes(), app_edited_target, "Droid settings reverse target"
        )

        recalculated = case.cli("plan", expected_code=0, profile=profile)
        selected_after_reverse = self._resources(recalculated)
        self.assertEqual(len(selected_after_reverse), 1)
        self.assertEqual(selected_after_reverse[0].get("name"), "droid")
        self.assertEqual(selected_after_reverse[0].get("status"), "converged")
        before_noop = case.snapshot()
        redeployed = case.cli("apply", "--yes", expected_code=0, profile=profile)
        self._assert_outcome(redeployed, "no_changes")
        self._assert_snapshot_equal(
            case.snapshot(), before_noop, "Droid settings confirmed no-op"
        )
        self._assert_bytes_equal(
            target.read_bytes(),
            case.expected("droid-settings.after-app.json"),
            "Droid settings after redeploy",
        )


if __name__ == "__main__":
    unittest.main()
