"""Shared CLI fixture and fault-injection hooks for profile scenarios."""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[2]


FIXTURE = ROOT / "tests" / "fixtures" / "dotfiles_profile"


OUTPUT_SENTINEL = "synthetic-output-secret-sentinel-v1"


UNKNOWN_SENTINEL = "synthetic-unknown-container-secret-v1"


EXPECTED_RESOURCES = tuple(
    line
    for line in (FIXTURE / "expected" / "profile-resources.txt")
    .read_text(encoding="utf-8")
    .splitlines()
    if line
)


COMMENT_RACE_HOOK = """
import sys
from pathlib import Path
import luwu.engine as engine
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


FAIL_PARENT_HOOK = """
import sys
from pathlib import Path
import luwu.engine as engine
from luwu.filesystem import FilesystemError
from luwu.cli import main
failed_name = sys.argv.pop(1)
original_create_parent = engine.create_parent
def fail_one_parent(root, path, mode, *, check_inputs=None):
    if Path(path).name == failed_name:
        raise FilesystemError("synthetic parent failure", code="parent_create_failed")
    return original_create_parent(root, path, mode, check_inputs=check_inputs)
engine.create_parent = fail_one_parent
raise SystemExit(main())
"""


FAIL_WRITE_HOOK = """
import sys
from pathlib import Path
import luwu.engine as engine
from luwu.filesystem import FilesystemError
from luwu.cli import main
failed_name = sys.argv.pop(1)
original_write_file = engine.write_file
def fail_one_write(root, path, data, *, expected, mode=None, check_inputs=None):
    if Path(path).name == failed_name:
        raise FilesystemError("synthetic target failure", code="write_failed")
    return original_write_file(
        root, path, data, expected=expected, mode=mode, check_inputs=check_inputs
    )
engine.write_file = fail_one_write
raise SystemExit(main())
"""


EXCLUDED_ACCESS_HOOK = """
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


MUTATION_COUNT_HOOK = """
import json
import sys
from pathlib import Path
import luwu.engine as engine
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


FUTURE_SOURCE_CHANGE_HOOK = """
import json
import os
import sys
from pathlib import Path
import luwu.engine as engine
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


NOOP_FINAL_CHANGE_HOOK = """
import json
import os
import sys
from pathlib import Path
import luwu.engine as engine
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


NOOP_PREFLIGHT_CHANGE_HOOK = """
import json
import sys
from pathlib import Path
import luwu.engine as engine
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


class ProfileCliCase:
    """One disposable manifest tree and a separate process HOME."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="luwu-test-acceptance-")
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


class ProfileScenarioCase(unittest.TestCase):
    def _case(self) -> ProfileCliCase:
        case = ProfileCliCase()
        self.addCleanup(case.close)
        return case

    def _resources(self, payload: dict[str, object]) -> list[dict[str, object]]:
        raw = payload.get("resources")
        if not isinstance(raw, list):
            raise TypeError("plan must report its selected resources")
        resources: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("resource entries must be JSON objects")
            resources.append(cast(dict[str, object], item))
        return resources

    def _actions(self, payload: dict[str, object]) -> list[dict[str, object]]:
        raw = payload.get("actions")
        if not isinstance(raw, list):
            raise TypeError("mutation result must report its actions")
        actions: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("action entries must be JSON objects")
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

    def _assert_resource_set(
        self, payload: dict[str, object], case: ProfileCliCase
    ) -> None:
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
        case: ProfileCliCase,
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
        self, case: ProfileCliCase, resource: str, *, profile: str = "synthetic"
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

    def _accept_all(self, case: ProfileCliCase, *, profile: str = "synthetic") -> None:
        self._adopt(case, "copy_profile", profile=profile)
        self._adopt(case, "assets:defaults.conf", profile=profile)
        self._adopt(case, "assets:nested/keymap.jsonc", profile=profile)
        self._accept(case, "settings", ("theme", "brand"), profile=profile)
        self._accept(case, "nested_toml", ("theme", "worker_name"), profile=profile)

    def _finite_batch_targets(self, case: ProfileCliCase) -> dict[str, Path]:
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
        self, payload: dict[str, object], case: ProfileCliCase
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

    def _accept_finite_batch(self, case: ProfileCliCase) -> None:
        for resource in ("batch_a", "batch_b", "batch_c"):
            self._accept(case, resource, ("value",), profile="finite_batch")

    def _assert_deployed_fixture_bytes(self, case: ProfileCliCase) -> None:
        expected = {
            ".config/demo/settings.jsonc": "settings.initial.jsonc",
            ".config/demo/deep/profile/runtime.toml": "nested.initial.toml",
            ".config/demo/profile.ini": "copy-profile.ini",
            ".config/demo/assets/defaults.conf": "assets-defaults.conf",
            ".config/demo/assets/nested/keymap.jsonc": "assets-nested-keymap.jsonc",
        }
        for target_name, expected_name in expected.items():
            self._assert_bytes_equal(
                (case.target_root / target_name).read_bytes(),
                case.expected(expected_name),
                target_name,
            )
