#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Measure reproducible version-8 plan and apply workloads on public fixtures.

Each sample gets new source, input, and target roots. Fixture setup, accepted
baseline seeding, convergence setup, and all independent checks are outside
the timed CLI call. This experiment deliberately has no journal or recovery
case and makes no version-5 durability claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

RESOURCES = (20, 200)
SCENARIOS = ("plan", "noop", "onechange", "allchanged")
BUDGETS = {
    "plan": 1.0,
    "noop": 2.0,
    "onechange": 3.0,
    "allchanged": 10.0,
}
PROFILE = "benchmark"
PACKAGE = "workload"
EXPECTED_MISSING_TARGET_EFFECTIVE_MODE = "0o0644"
MODULES = (
    "luwu",
    "luwu.cli",
    "luwu.manifest",
    "luwu.baseline",
    "luwu.formats",
    "luwu.filesystem",
    "luwu.inputs",
    "luwu.engine",
    "luwu.plan_evidence",
)
_PLAN_FIELDS = frozenset(
    {"schema_version", "command", "profile", "packages", "excluded", "resources"}
)
_PLAN_RESOURCE_FIELDS = frozenset(
    {
        "name",
        "source",
        "target",
        "status",
        "action",
        "reason",
        "error_code",
        "forward_fields",
        "reverse_fields",
        "ownership",
        "input_provenance",
        "undeclared_changed",
        "parents",
        "parent_mode",
        "mode",
        "effective_mode",
        "parent_actions",
    }
)
_APPLY_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "command",
        "outcome",
        "actions",
        "verification_status",
    }
)
_ACTION_FIELDS = frozenset({"kind", "resource", "path", "state", "fields"})
_PROBE = """\
import hashlib
import importlib
import json
import sys
import sysconfig
from pathlib import Path

names = __MODULES__
modules = {}
for name in names:
    module = importlib.import_module(name)
    path = Path(module.__file__).resolve()
    modules[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
import luwu
print(json.dumps({
    "python_version": sys.version.split()[0],
    "python_prefix": sys.prefix,
    "python_site_packages": str(Path(sysconfig.get_path("purelib")).resolve()),
    "luwu_version": luwu.__version__,
    "modules": modules,
}))
""".replace("__MODULES__", repr(MODULES))
_SEED = """\
import json
import sys
from pathlib import Path

from luwu.baseline import make_copy_baseline, make_field_baseline
from luwu.manifest import load_manifest

manifest_path = Path(sys.argv[1])
descriptor_path = Path(sys.argv[2])
manifest = load_manifest(manifest_path, profile="benchmark")
descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
resources = {resource.name: resource for resource in manifest.resources}
if set(resources) != {item["name"] for item in descriptor}:
    raise RuntimeError("fixture profile selection differs from the independent resource set")
for item in descriptor:
    resource = resources[item["name"]]
    if item["kind"] == "copy":
        expected_mode = item["expected_target_mode"]
        if type(expected_mode) is not int:
            raise RuntimeError("copy fixture has no independent target mode")
        accepted_data = bytes.fromhex(item["initial_bytes_hex"])
        if resource.source.read_bytes() != accepted_data:
            raise RuntimeError("copy source differs from its independent fixture")
        data = make_copy_baseline(
            manifest,
            resource,
            accepted_data=accepted_data,
            accepted_mode=expected_mode,
        )
    else:
        data = make_field_baseline(
            manifest,
            resource,
            current=item["fields"],
            selected_fields=("index", "value"),
            existing=None,
        )
    resource.baseline.parent.mkdir(parents=True, exist_ok=True)
    resource.baseline.write_bytes(data)
    resource.baseline.chmod(0o600)
"""


@dataclass(frozen=True)
class ResourceFixture:
    name: str
    kind: str
    source: Path
    source_name: str
    target: Path
    target_name: str
    expected_target_mode: int | None
    baseline: Path
    baseline_name: str
    initial_bytes: bytes
    changed_bytes: bytes
    initial_fields: dict[str, object]
    changed_fields: dict[str, object]


@dataclass(frozen=True)
class Fixture:
    root: Path
    manifest: Path
    resources: tuple[ResourceFixture, ...]
    digest: str

    @property
    def expected_names(self) -> tuple[str, ...]:
        return tuple(resource.name for resource in self.resources)

    @property
    def copy_count(self) -> int:
        return sum(resource.kind == "copy" for resource in self.resources)

    @property
    def fields_count(self) -> int:
        return sum(resource.kind == "template" for resource in self.resources)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _json_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _resource_data(ordinal: int, changed: bool) -> tuple[str, bytes, dict[str, object]]:
    revision = "changed" if changed else "initial"
    value = f"public-{revision}-{ordinal:04d}"
    return (
        value,
        _json_bytes({"value": value, "index": ordinal}),
        {
            "value": value,
            "index": ordinal,
        },
    )


def _write_fixture(root: Path, size: int) -> Fixture:
    if type(size) is not int or size < 1:
        raise ValueError("resource count must be a positive integer")
    source_root = root / "source"
    inputs_root = root / "inputs"
    target_root = root / "targets"
    baseline_root = inputs_root / "baselines"
    for path in (source_root, inputs_root, target_root, baseline_root):
        path.mkdir(parents=True, exist_ok=True)

    resources: list[ResourceFixture] = []
    declarations: list[str] = []
    names: list[str] = []
    for ordinal in range(size):
        kind = "copy" if ordinal % 3 == 0 else "template"
        name = f"resource_{ordinal:04d}"
        names.append(name)
        _initial_value, initial_bytes, initial_fields = _resource_data(ordinal, False)
        _changed_value, changed_bytes, changed_fields = _resource_data(ordinal, True)
        if kind == "copy":
            initial_bytes = f"public-copy-initial-{ordinal:04d}\n".encode()
            changed_bytes = f"public-copy-changed-{ordinal:04d}\n".encode()
            source_name = f"{name}.txt"
            target_name = f"{name}.txt"
            expected_target_mode = 0o644
        else:
            source_name = f"{name}.json.j2"
            target_name = f"{name}.json"
            expected_target_mode = None
        source = source_root / source_name
        source.write_bytes(initial_bytes)
        if expected_target_mode is not None:
            source.chmod(expected_target_mode)
        target = target_root / target_name
        baseline_name = f"baselines/{name}.json"
        baseline = inputs_root / baseline_name
        resources.append(
            ResourceFixture(
                name=name,
                kind=kind,
                source=source,
                source_name=source_name,
                target=target,
                target_name=target_name,
                expected_target_mode=expected_target_mode,
                baseline=baseline,
                baseline_name=baseline_name,
                initial_bytes=initial_bytes,
                changed_bytes=changed_bytes,
                initial_fields=initial_fields,
                changed_fields=changed_fields,
            )
        )
        common = (
            f"kind = {json.dumps(kind)}\n"
            f"source = {_toml_string(source_name)}\n"
            f"target = {_toml_string(target_name)}\n"
            f"baseline = {_toml_string(baseline_name)}\n"
            'content_sensitivity = "public"\n'
            'parents = "require"\n'
            'parent_mode = "0700"\n'
        )
        if kind == "copy":
            declarations.append(
                f'[resources.{name}]\n{common}scope = "whole-file"\nowner = "source"\n'
            )
        else:
            declarations.append(
                f"[resources.{name}]\n{common}"
                'format = "json"\n'
                'scope = "fields"\n'
                f"[resources.{name}.fields.value]\n"
                'path = ["value"]\n'
                'owner = "source"\n'
                f"[resources.{name}.fields.index]\n"
                'path = ["index"]\n'
                'owner = "source"\n'
            )

    manifest_text = (
        "version = 8\n"
        '\n[roots]\nsource = "source"\ninputs = "inputs"\ntarget = "targets"\n'
        f"\n[profiles.{PROFILE}]\npackages = [{_toml_string(PACKAGE)}]\n"
        "exclude = []\n"
        f"\n[packages.{PACKAGE}]\ndependencies = []\nresources = ["
        + ", ".join(_toml_string(name) for name in names)
        + "]\n\n[inputs]\n\n"
        + "\n".join(declarations)
    )
    manifest = root / "luwu.toml"
    manifest.write_text(manifest_text, encoding="utf-8")
    digest = hashlib.sha256()
    digest.update(manifest.read_bytes())
    for resource in resources:
        digest.update(resource.name.encode())
        digest.update(hashlib.sha256(resource.initial_bytes).digest())
    return Fixture(
        root=root,
        manifest=manifest,
        resources=tuple(resources),
        digest=digest.hexdigest(),
    )


def _environment(source_root: Path | None) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if source_root is None:
        environment.pop("PYTHONPATH", None)
    else:
        environment["PYTHONPATH"] = str(source_root)
    return environment


def _run(
    fixture: Fixture, python: Path, source_root: Path | None, *args: str
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(python), "-B", "-m", "luwu", *args],
        cwd=fixture.root,
        env=_environment(source_root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"CLI command failed ({completed.returncode}): {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("CLI command returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise TypeError("CLI command returned a non-object JSON value")
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        json.dumps(payload),
        completed.stderr,
    )


def _command_args(fixture: Fixture, operation: str) -> tuple[str, ...]:
    common = ("--manifest", str(fixture.manifest), "--profile", PROFILE, "--json")
    if operation == "apply":
        return ("apply", *common, "--yes")
    return (operation, *common)


def _seed_baselines(
    fixture: Fixture,
    python: Path,
    source_root: Path | None,
) -> None:
    descriptor_path = fixture.root / "baseline-seed.json"
    descriptor = [
        {
            "name": item.name,
            "kind": item.kind,
            "fields": item.initial_fields,
            "expected_target_mode": item.expected_target_mode,
            "initial_bytes_hex": item.initial_bytes.hex(),
        }
        for item in fixture.resources
    ]
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    try:
        completed = subprocess.run(
            [
                str(python),
                "-B",
                "-c",
                _SEED,
                str(fixture.manifest),
                str(descriptor_path),
            ],
            cwd=fixture.root,
            env=_environment(source_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"baseline fixture seeding failed: {detail}")
    finally:
        descriptor_path.unlink(missing_ok=True)


def _scenario_changes(fixture: Fixture, scenario: str) -> tuple[str, ...]:
    if scenario == "onechange":
        return (fixture.resources[0].name,)
    if scenario == "allchanged":
        return fixture.expected_names
    return ()


def _set_scenario_sources(fixture: Fixture, scenario: str) -> None:
    changed = set(_scenario_changes(fixture, scenario))
    for resource in fixture.resources:
        if resource.name in changed:
            resource.source.write_bytes(resource.changed_bytes)


def _index_rows(
    rows_value: object,
    *,
    name_field: str,
    expected_names: tuple[str, ...],
    label: str,
) -> dict[str, dict[str, object]]:
    if not isinstance(rows_value, list):
        raise TypeError(f"schema-8 {label} must be a list")
    expected = set(expected_names)
    rows: dict[str, dict[str, object]] = {}
    duplicates: set[str] = set()
    for row in rows_value:
        if not isinstance(row, dict):
            raise TypeError(f"schema-8 {label} entries must be objects")
        name = row.get(name_field)
        if not isinstance(name, str):
            raise TypeError(f"schema-8 {label} is missing {name_field}")
        if name in rows:
            duplicates.add(name)
        rows[name] = row
    missing = sorted(expected - rows.keys())
    unknown = sorted(rows.keys() - expected)
    if duplicates or missing or unknown or len(rows_value) != len(expected):
        raise RuntimeError(
            f"schema-8 {label} resource set mismatch: "
            f"missing={missing}, duplicate={sorted(duplicates)}, unknown={unknown}"
        )
    return rows


def _expected_write_fields(
    resource: ResourceFixture, *, target_missing: bool
) -> list[str]:
    if resource.kind == "copy":
        return ["whole_file"]
    if target_missing:
        return sorted(resource.initial_fields)
    return sorted(
        name
        for name, initial in resource.initial_fields.items()
        if resource.changed_fields[name] != initial
    )


def _assert_plan_resources(payload: dict[str, object], fixture: Fixture) -> None:
    if set(payload) != _PLAN_FIELDS:
        raise RuntimeError("plan JSON does not match the schema-8 Plan.to_dict shape")
    if (
        payload.get("schema_version") != 8
        or payload.get("command") != "plan"
        or payload.get("profile") != PROFILE
        or payload.get("packages") != [PACKAGE]
        or payload.get("excluded") != []
    ):
        raise RuntimeError("schema-8 plan metadata does not match the fixture")
    rows = _index_rows(
        payload.get("resources"),
        name_field="name",
        expected_names=fixture.expected_names,
        label="Plan.to_dict resources",
    )
    resource_by_name = {item.name: item for item in fixture.resources}
    for name, row in rows.items():
        resource = resource_by_name[name]
        if set(row) != _PLAN_RESOURCE_FIELDS:
            raise RuntimeError(f"schema-8 plan resource fields mismatch for {name}")
        if (
            row["source"] != resource.source_name
            or row["target"] != resource.target_name
            or row["status"] != "missing"
            or row["action"] != "create"
            or row["error_code"] is not None
            or row["forward_fields"]
            != _expected_write_fields(resource, target_missing=True)
            or row["reverse_fields"] != []
            or row["undeclared_changed"] is not False
            or row["parents"] != "require"
            or row["parent_mode"] != "0o0700"
            or row["mode"] is not None
            or row["effective_mode"] != EXPECTED_MISSING_TARGET_EFFECTIVE_MODE
            or row["parent_actions"]
            != {"target": [], "baseline": [], "reverse_inputs": []}
        ):
            raise RuntimeError(f"schema-8 plan resource oracle mismatch for {name}")


def _assert_apply_envelope(payload: dict[str, object]) -> None:
    if set(payload) != _APPLY_FIELDS:
        raise RuntimeError(
            "apply JSON does not match the schema-8 Result.to_dict shape"
        )
    if (
        payload.get("schema_version") != 8
        or payload.get("operation") != "apply"
        or payload.get("command") != "apply"
        or payload.get("verification_status") != "verified"
    ):
        raise RuntimeError("schema-8 apply metadata is not verified")


def _assert_apply_actions(
    payload: dict[str, object],
    fixture: Fixture,
    expected_names: tuple[str, ...],
    *,
    target_missing: bool,
) -> None:
    rows = _index_rows(
        payload.get("actions"),
        name_field="resource",
        expected_names=expected_names,
        label="Result.to_dict actions",
    )
    resource_by_name = {item.name: item for item in fixture.resources}
    for name, row in rows.items():
        resource = resource_by_name[name]
        if set(row) != _ACTION_FIELDS:
            raise RuntimeError(f"schema-8 action fields mismatch for {name}")
        if (
            row["kind"] != "target_write"
            or row["path"] != resource.target_name
            or row["state"] != "committed"
            or row["fields"]
            != _expected_write_fields(resource, target_missing=target_missing)
        ):
            raise RuntimeError(f"schema-8 apply action oracle mismatch for {name}")


def _assert_result(
    payload: dict[str, object],
    fixture: Fixture,
    scenario: str,
    *,
    setup: bool = False,
) -> None:
    changes = set(
        fixture.expected_names if setup else _scenario_changes(fixture, scenario)
    )
    if scenario == "plan" and not setup:
        _assert_plan_resources(payload, fixture)
        return
    _assert_apply_envelope(payload)
    outcome = payload["outcome"]
    if not isinstance(outcome, str):
        raise TypeError("schema-8 apply result has no string outcome")
    if not setup and not changes:
        if outcome != "no_changes":
            raise RuntimeError(f"no-op apply outcome mismatch: got {outcome}")
        _assert_apply_actions(payload, fixture, (), target_missing=False)
        return
    expected_names = (
        fixture.expected_names
        if setup
        else tuple(name for name in fixture.expected_names if name in changes)
    )
    if outcome != "committed":
        raise RuntimeError(f"apply outcome mismatch: expected committed, got {outcome}")
    _assert_apply_actions(
        payload,
        fixture,
        expected_names,
        target_missing=setup,
    )


def _expected_target_bytes(resource: ResourceFixture, changed: bool) -> bytes:
    return resource.changed_bytes if changed else resource.initial_bytes


def _expected_baseline_values(resource: ResourceFixture) -> dict[str, object]:
    if resource.kind == "copy":
        if resource.expected_target_mode is None:
            raise RuntimeError("copy fixture has no independent target mode")
        return {
            "whole_file": {
                "algorithm": "sha256",
                "digest": hashlib.sha256(resource.initial_bytes).hexdigest(),
            },
            "mode": resource.expected_target_mode,
        }
    return resource.initial_fields


def _assert_targets_and_baselines(
    fixture: Fixture,
    *,
    changed_names: set[str],
    targets_present: bool,
) -> None:
    target_root = fixture.root / "targets"
    expected_target_names = (
        {resource.target_name for resource in fixture.resources}
        if targets_present
        else set()
    )
    actual_target_names = {path.name for path in target_root.iterdir()}
    if actual_target_names != expected_target_names:
        raise RuntimeError(
            f"target set mismatch: expected={sorted(expected_target_names)}, got={sorted(actual_target_names)}"
        )
    if targets_present:
        for resource in fixture.resources:
            if resource.kind == "copy":
                if (
                    resource.target.is_symlink()
                    or resource.target.read_bytes()
                    != _expected_target_bytes(resource, resource.name in changed_names)
                ):
                    raise RuntimeError(f"copy target mismatch for {resource.name}")
                actual_mode = stat.S_IMODE(resource.target.stat().st_mode)
                if actual_mode != resource.expected_target_mode:
                    raise RuntimeError(
                        f"copy target mode mismatch for {resource.name}: "
                        f"expected={resource.expected_target_mode}, got={actual_mode}"
                    )
            else:
                try:
                    actual = json.loads(resource.target.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise RuntimeError(
                        f"field target is invalid for {resource.name}"
                    ) from error
                expected = (
                    resource.changed_fields
                    if resource.name in changed_names
                    else resource.initial_fields
                )
                if actual != expected:
                    raise RuntimeError(f"field target mismatch for {resource.name}")

    for resource in fixture.resources:
        try:
            envelope = json.loads(resource.baseline.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"baseline is invalid for {resource.name}") from error
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema_version") != 2
            or not isinstance(envelope.get("identity"), dict)
            or not isinstance(envelope.get("values"), dict)
            or envelope["identity"].get("resource") != resource.name
            or envelope["identity"].get("profile") != PROFILE
            or envelope["values"] != _expected_baseline_values(resource)
        ):
            raise RuntimeError(f"baseline oracle mismatch for {resource.name}")


def _assert_no_journal(fixture: Fixture) -> None:
    if any(
        path.name.endswith((".journal", ".journal.json", ".record"))
        for path in fixture.root.rglob("*")
    ):
        raise RuntimeError(
            "v8 benchmark unexpectedly created a journal or recovery record"
        )


def _timed_sample(
    scratch: Path,
    size: int,
    scenario: str,
    *,
    python: Path,
    source_root: Path | None,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix="luwu-test-bench-", dir=scratch
    ) as directory:
        fixture = _write_fixture(Path(directory), size)
        _seed_baselines(fixture, python, source_root)
        _assert_targets_and_baselines(
            fixture, changed_names=set(), targets_present=False
        )
        if scenario != "plan":
            setup = _run(fixture, python, source_root, *_command_args(fixture, "apply"))
            _assert_result(json.loads(setup.stdout), fixture, scenario, setup=True)
            _assert_targets_and_baselines(
                fixture, changed_names=set(), targets_present=True
            )
        _set_scenario_sources(fixture, scenario)
        changed_names = set(_scenario_changes(fixture, scenario))
        operation = "plan" if scenario == "plan" else "apply"
        started = time.perf_counter()
        completed = _run(
            fixture, python, source_root, *_command_args(fixture, operation)
        )
        elapsed = time.perf_counter() - started
        payload = json.loads(completed.stdout)
        _assert_result(payload, fixture, scenario)
        _assert_targets_and_baselines(
            fixture,
            changed_names=changed_names,
            targets_present=scenario != "plan",
        )
        _assert_no_journal(fixture)
        return {
            "seconds": elapsed,
            "fixture_sha256": fixture.digest,
            "resource_count": size,
            "copy_resources": fixture.copy_count,
            "field_resources": fixture.fields_count,
            "expected_changed_resources": sorted(changed_names),
        }


def _filesystem_metadata(path: Path) -> dict[str, object]:
    findmnt = shutil.which("findmnt")
    mount: dict[str, object] = {"available": False, "filesystem": None, "target": None}
    if findmnt is not None:
        try:
            completed = subprocess.run(
                [
                    findmnt,
                    "--json",
                    "--target",
                    str(path.resolve()),
                    "--output",
                    "FSTYPE,TARGET",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                text=True,
            )
            mount["available"] = True
            if completed.returncode == 0:
                filesystems = json.loads(completed.stdout).get("filesystems", [])
                if filesystems and isinstance(filesystems[0], dict):
                    mount["filesystem"] = filesystems[0].get("fstype")
                    mount["target"] = filesystems[0].get("target")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    filesystem = mount.get("filesystem")
    return {
        **mount,
        "block_size": os.statvfs(path).f_frsize,
        "tmpfs_detected": filesystem in {"tmpfs", "ramfs"},
    }


def _probe_runtime(
    python: Path, scratch: Path, source_root: Path | None, installed: bool
) -> dict[str, object]:
    completed = subprocess.run(
        [str(python), "-B", "-c", _PROBE],
        cwd=scratch,
        env=_environment(source_root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"target Python cannot import the profile runtime: {detail}")
    try:
        runtime = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("target Python probe returned invalid JSON") from error
    if not isinstance(runtime, dict) or not isinstance(runtime.get("modules"), dict):
        raise TypeError("target Python probe returned incomplete module metadata")
    source_package = (Path(__file__).resolve().parents[1] / "src").resolve()
    site_packages = Path(str(runtime["python_site_packages"])).resolve()
    for name, metadata in runtime["modules"].items():
        if not isinstance(metadata, dict) or not isinstance(metadata.get("path"), str):
            raise TypeError(f"target Python returned no import path for {name}")
        module_path = Path(metadata["path"]).resolve()
        expected_root = site_packages if installed else source_package
        try:
            module_path.relative_to(expected_root)
        except ValueError as error:
            mode = "site-packages" if installed else "checkout source"
            raise RuntimeError(
                f"--installed/source mode imported {name} outside {mode}: {module_path}"
            ) from error
    if installed:
        try:
            Path(str(runtime["modules"]["luwu"]["path"])).resolve().relative_to(
                source_package
            )
        except ValueError:
            pass
        else:
            raise RuntimeError("--installed imported luwu from the checkout")
    return runtime


def _script_hashes() -> dict[str, str]:
    paths = (
        Path(__file__).resolve(),
        Path(__file__).with_name("profile_ablation.py").resolve(),
    )
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }


def _checkpoint(path: Path, payload: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _record_group(
    size: int,
    scenario: str,
    samples: list[dict[str, object]],
    *,
    repeats: int,
    budget_eligible: bool,
) -> dict[str, object]:
    if len(samples) != repeats + 1:
        raise RuntimeError("sample group is incomplete")
    first = float(samples[0]["seconds"])
    warm = [float(sample["seconds"]) for sample in samples[1:]]
    median = statistics.median(warm)
    maximum = max(warm)
    budget = BUDGETS[scenario] if size == 200 else None
    median_met = median <= budget if budget is not None else None
    maximum_guard_met = maximum <= 2 * budget if budget is not None else None
    return {
        "status": "complete",
        "resources": size,
        "scenario": scenario,
        "first_sample_seconds": first,
        "warm_samples_seconds": warm,
        "all_samples": samples,
        "warm_sample_count": len(warm),
        "warm_median_seconds": median,
        "warm_maximum_seconds": maximum,
        "budget_seconds_at_200": budget,
        "budget_median_budget_met": median_met,
        "budget_warm_maximum_guard_seconds": 2 * budget if budget is not None else None,
        "budget_warm_maximum_guard_met": maximum_guard_met,
        "budget_time_budget_met": (median_met and maximum_guard_met)
        if budget is not None
        else None,
        "acceptance_eligible": budget_eligible if budget is not None else None,
        "budget_comparison_scope": "absolute 200-resource plan/apply time only; no recovery or 100-to-200 scaling claim",
    }


def _parse_resources(values: list[str]) -> tuple[int, ...]:
    try:
        resources = tuple(int(value) for value in values)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "resource counts must be positive integers"
        ) from error
    if not resources or any(value < 1 for value in resources):
        raise argparse.ArgumentTypeError("resource counts must be positive integers")
    if len(resources) != len(set(resources)):
        raise argparse.ArgumentTypeError("resource counts must be unique")
    return resources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resources", nargs="+", default=[str(value) for value in RESOURCES]
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=SCENARIOS,
        default=SCENARIOS,
        help="scenario subset; defaults to all scenarios",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        help="independent warm samples per group; minimum 3",
    )
    parser.add_argument(
        "--output", type=Path, help="optional JSON report path, atomically checkpointed"
    )
    parser.add_argument(
        "--installed",
        action="store_true",
        help="require all profile imports from target site-packages",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="target Python executable",
    )
    parser.add_argument(
        "--scratch",
        type=Path,
        default=Path(tempfile.gettempdir()),
        help="existing fixture parent on the filesystem being measured",
    )
    args = parser.parse_args(argv)
    try:
        resource_counts = _parse_resources(args.resources)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    if args.repeats < 3:
        parser.error("--repeats must be at least three independent warm samples")
    scratch = args.scratch.expanduser().absolute()
    if not scratch.is_dir():
        parser.error("--scratch must be an existing directory")
    if (
        args.output is not None
        and not args.output.expanduser().absolute().parent.is_dir()
    ):
        parser.error("--output parent must already exist")
    python = args.python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error("--python must name an executable file")
    source_root = (
        None if args.installed else Path(__file__).resolve().parents[1] / "src"
    )
    try:
        runtime = _probe_runtime(python, scratch, source_root, args.installed)
    except RuntimeError as error:
        parser.error(
            f"profile production runtime is incomplete; focused run is pending: {error}"
        )
    filesystem = _filesystem_metadata(scratch)
    budget_eligible = not bool(filesystem["tmpfs_detected"])
    results: list[dict[str, object]] = []
    payload: dict[str, object] = {
        "schema_version": 1,
        "complete": False,
        "benchmark": "profile_benchmark",
        "workload": "public-v8-copy-and-fields",
        "resource_counts": list(resource_counts),
        "scenarios": list(args.scenarios),
        "repeats": args.repeats,
        "sample_policy": "each first and warm sample uses independently recreated roots; setup and oracle checks are outside timing",
        "excluded": ["journal", "recovery", "v5 durable-recovery claim"],
        "harness_sha256": _script_hashes(),
        "python": {
            "executable": str(python),
            "version": runtime.get("python_version"),
            "prefix": runtime.get("python_prefix"),
            "site_packages": runtime.get("python_site_packages"),
            "luwu_version": runtime.get("luwu_version"),
            "imports": runtime.get("modules"),
        },
        "environment": {
            "platform": sys.platform,
            "platform_release": platform.platform(),
            "filesystem": filesystem,
            "budget_absolute_budget_environment_eligible": budget_eligible,
            "budget_eligibility_note": (
                "tmpfs is excluded by the performance acceptance rules"
                if not budget_eligible
                else "filesystem is not detected as tmpfs; locality and durability remain environment facts"
            ),
            "sandbox_mode": os.environ.get("LUWU_SANDBOX_MODE", "unspecified"),
        },
        "results": results,
    }
    if args.output is not None:
        _checkpoint(args.output.expanduser().absolute(), payload)
    for size in resource_counts:
        for scenario in args.scenarios:
            samples: list[dict[str, object]] = []
            for sample_index in range(args.repeats + 1):
                try:
                    sample = _timed_sample(
                        scratch,
                        size,
                        scenario,
                        python=python,
                        source_root=source_root,
                    )
                except Exception as error:  # noqa: BLE001 - retain failed acceptance evidence
                    failed_row = {
                        "status": "failed",
                        "resources": size,
                        "scenario": scenario,
                        "attempted_sample_index": sample_index,
                        "first_sample_seconds": samples[0]["seconds"]
                        if samples
                        else None,
                        "successful_samples": samples,
                        "failed_sample": {
                            "status": "failed_before_timed_result",
                            "error_type": type(error).__name__,
                            "error": str(error),
                        },
                        "warm_median_seconds": None,
                        "warm_maximum_seconds": None,
                        "budget_seconds_at_200": BUDGETS[scenario]
                        if size == 200
                        else None,
                        "budget_time_budget_met": False if size == 200 else None,
                        "acceptance_eligible": budget_eligible if size == 200 else None,
                    }
                    results.append(failed_row)
                    payload["failure"] = {
                        "resources": size,
                        "scenario": scenario,
                        "sample_index": sample_index,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                    if args.output is not None:
                        _checkpoint(args.output.expanduser().absolute(), payload)
                    else:
                        sys.stdout.write(
                            json.dumps(payload, indent=2, sort_keys=True) + "\n"
                        )
                    return 1
                samples.append(sample)
            row = _record_group(
                size,
                scenario,
                samples,
                repeats=args.repeats,
                budget_eligible=budget_eligible,
            )
            results.append(row)
            if args.output is not None:
                _checkpoint(args.output.expanduser().absolute(), payload)
    payload["complete"] = True
    if args.output is not None:
        _checkpoint(args.output.expanduser().absolute(), payload)
    else:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
