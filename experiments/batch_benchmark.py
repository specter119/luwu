#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Measure the batch execution scenarios on a sanitized fixture.

The harness deliberately uses the real CLI and excludes fixture construction
from timed samples.  It is a baseline/acceptance aid, not a public command and
does not inspect a user's HOME, network, provider, or configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SIZES = (1, 10, 30, 100, 176, 200)
SCENARIOS = ("plan", "fresh", "noop", "one-change", "all-changed", "recovery")
_PYTHON_PROBE = """\
import json
import sys
import sysconfig
from pathlib import Path

import luwu

print(json.dumps({
    "python_version": sys.version.split()[0],
    "python_prefix": sys.prefix,
    "python_site_packages": str(Path(sysconfig.get_path("purelib")).resolve()),
    "luwu_file": str(Path(luwu.__file__).resolve()),
    "luwu_version": luwu.__version__,
}))
"""


@dataclass
class Fixture:
    root: Path
    sources: list[Path]
    source_names: list[str]
    source_kinds: list[str]
    target_names: list[str]
    manifest: Path
    python: Path
    source_root: Path | None

    def command(self, *args: str) -> list[str]:
        return [
            str(self.python),
            "-B",
            "-m",
            "luwu",
            *args,
        ]


def _write_fixture(
    root: Path,
    size: int,
    *,
    python: Path,
    source_root: Path | None,
) -> Fixture:
    sources_root = root / "sources"
    targets_root = root / "targets"
    sources_root.mkdir()
    targets_root.mkdir()
    sources: list[Path] = []
    source_names: list[str] = []
    source_kinds: list[str] = []
    target_names: list[str] = []
    resources: list[str] = []

    for ordinal in range(size):
        name = f"resource_{ordinal:04d}"
        if ordinal % 10 == 0:
            kind = "symbolic"
            suffix = ".conf"
            source_text = f"public-symbolic-{ordinal:04d}\n"
        else:
            kind = "template"
            suffix = ".conf.j2"
            source_text = (
                f'public_value = "resource-{ordinal:04d}"\npublic_index = {ordinal}\n'
            )
        source = sources_root / f"{name}{suffix}"
        source.write_text(source_text, encoding="utf-8")
        sources.append(source)
        source_names.append(f"sources/{name}{suffix}")
        source_kinds.append(kind)
        target_names.append(f"targets/{name}.conf")
        resources.append(
            f"""[resources.{name}]
kind = "{kind}"
source = "sources/{name}{suffix}"
target = "targets/{name}.conf"
owner = "source"
scope = "whole-file"
content_sensitivity = "public"
"""
        )

    manifest = root / "luwu.toml"
    manifest.write_text("version = 5\n\n" + "\n".join(resources), encoding="utf-8")
    return Fixture(
        root=root,
        sources=sources,
        source_names=source_names,
        source_kinds=source_kinds,
        target_names=target_names,
        manifest=manifest,
        python=python,
        source_root=source_root,
    )


def _run_capture(fixture: Fixture, *args: str) -> subprocess.CompletedProcess[str]:
    environment = _subprocess_environment(fixture.source_root)
    completed = subprocess.run(
        fixture.command(*args),
        cwd=fixture.root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"scenario command failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed


def _run(fixture: Fixture, *args: str) -> None:
    _run_capture(fixture, *args)


def _subprocess_environment(source_root: Path | None) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if source_root is None:
        environment.pop("PYTHONPATH", None)
    else:
        environment["PYTHONPATH"] = str(source_root)
    return environment


def _probe_python(
    python: Path,
    *,
    scratch: Path,
    source_root: Path | None,
    installed: bool,
) -> dict[str, str]:
    try:
        completed = subprocess.run(
            [str(python), "-B", "-c", _PYTHON_PROBE],
            cwd=scratch,
            env=_subprocess_environment(source_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as error:
        raise RuntimeError(f"cannot run target Python {python}: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"target Python could not import luwu ({completed.returncode}): {detail}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("target Python probe returned invalid JSON") from error
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), str)
        for key in (
            "python_version",
            "python_prefix",
            "python_site_packages",
            "luwu_file",
            "luwu_version",
        )
    ):
        raise RuntimeError("target Python probe returned incomplete package metadata")

    package_file = Path(value["luwu_file"]).resolve()
    checkout_source = Path(__file__).resolve().parents[1] / "src"
    if installed:
        try:
            package_file.relative_to(checkout_source.resolve())
        except ValueError:
            pass
        else:
            raise RuntimeError(
                "--installed imported luwu from the checkout source tree: "
                f"{package_file}"
            )
        site_packages = Path(value["python_site_packages"]).resolve()
        try:
            package_file.relative_to(site_packages)
        except ValueError as error:
            raise RuntimeError(
                "--installed imported luwu outside the target Python site-packages: "
                f"{package_file} (site-packages: {site_packages})"
            ) from error
    value["luwu_file"] = str(package_file)
    return value


def _apply(fixture: Fixture, record: Path) -> None:
    _run(
        fixture,
        "apply",
        "--manifest",
        str(fixture.manifest),
        "--yes",
        "--record",
        str(record),
        "--json",
    )


def _plan_json(fixture: Fixture) -> dict[str, object]:
    completed = _run_capture(
        fixture,
        "plan",
        "--manifest",
        str(fixture.manifest),
        "--json",
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise TypeError("plan output is not a JSON object")
    return value


def _plan_actions(fixture: Fixture) -> tuple[tuple[str, str], ...]:
    payload = _plan_json(fixture)
    return _actions_from_payload(payload)


def _actions_from_payload(payload: dict[str, object]) -> tuple[tuple[str, str], ...]:
    resources = payload.get("resources")
    if not isinstance(resources, list):
        raise TypeError("plan output has no resources")
    result: list[tuple[str, str]] = []
    for resource in resources:
        if not isinstance(resource, dict):
            raise TypeError("plan resource metadata is invalid")
        name = resource.get("name")
        action = resource.get("action")
        if not isinstance(name, str) or not isinstance(action, str):
            raise TypeError("plan resource action metadata is invalid")
        result.append((name, action))
    return tuple(result)


def _change_source(fixture: Fixture, ordinal: int) -> None:
    name = f"resource_{ordinal:04d}"
    if fixture.source_kinds[ordinal] == "template":
        source = fixture.sources[ordinal]
        source.write_text(
            f'public_value = "changed-{ordinal:04d}"\npublic_index = {ordinal}\n',
            encoding="utf-8",
        )
        return
    changed_name = f"sources/{name}-changed.conf"
    changed_path = fixture.root / changed_name
    changed_path.write_text(
        f"public-symbolic-changed-{ordinal:04d}\n", encoding="utf-8"
    )
    target = fixture.root / fixture.target_names[ordinal]
    if target.is_symlink() or target.exists():
        target.unlink()
    manifest_text = fixture.manifest.read_text(encoding="utf-8")
    manifest_text = manifest_text.replace(
        f'source = "{fixture.source_names[ordinal]}"',
        f'source = "{changed_name}"',
        1,
    )
    fixture.manifest.write_text(manifest_text, encoding="utf-8")
    fixture.sources[ordinal] = changed_path
    fixture.source_names[ordinal] = changed_name


def _prepare_scenario(fixture: Fixture, scenario: str) -> Path:
    record = fixture.root / f"{scenario}.journal.json"
    if scenario == "plan":
        return record
    if scenario in {"noop", "one-change", "all-changed", "recovery"}:
        _apply(fixture, fixture.root / "setup.journal.json")
    if scenario == "one-change":
        _change_source(fixture, 0)
    elif scenario == "all-changed":
        for ordinal in range(len(fixture.sources)):
            _change_source(fixture, ordinal)
    return record


def _assert_prepared_scenario(
    fixture: Fixture, scenario: str
) -> tuple[tuple[str, str], ...]:
    actions = _plan_actions(fixture)
    expected_names = _expected_resource_names(fixture)
    if scenario == "plan" or scenario == "fresh":
        expected = {name: "create" for name in expected_names}
    elif scenario == "noop":
        expected = {name: "noop" for name in expected_names}
    elif scenario == "one-change":
        expected = {
            name: ("create" if fixture.source_kinds[0] == "symbolic" else "replace")
            if index == 0
            else "noop"
            for index, name in enumerate(expected_names)
        }
    elif scenario == "all-changed":
        expected = {
            name: ("create" if fixture.source_kinds[index] == "symbolic" else "replace")
            for index, name in enumerate(expected_names)
        }
    elif scenario == "recovery":
        expected = {name: "noop" for name in expected_names}
    else:
        raise RuntimeError(f"unknown scenario {scenario}")

    _assert_action_set(fixture, scenario, actions, expected)
    return actions


def _assert_action_set(
    fixture: Fixture,
    scenario: str,
    actions: tuple[tuple[str, str], ...],
    expected: dict[str, str],
) -> None:
    expected_names = _expected_resource_names(fixture)

    actual_names = [name for name, _action in actions]
    duplicates = sorted(
        name for name, count in Counter(actual_names).items() if count > 1
    )
    expected_name_set = set(expected_names)
    actual_name_set = set(actual_names)
    missing = sorted(expected_name_set - actual_name_set)
    extra = sorted(actual_name_set - expected_name_set)
    if duplicates or missing or extra or len(actual_names) != len(expected_names):
        raise RuntimeError(
            f"scenario {scenario} resource set mismatch: "
            f"missing={missing}, duplicate={duplicates}, extra={extra}"
        )

    by_name = dict(actions)
    if by_name != expected:
        raise RuntimeError(
            f"scenario {scenario} action set mismatch: "
            f"expected {expected}, got {by_name}"
        )


def _json_object(stdout: str, *, description: str) -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{description} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise TypeError(f"{description} returned a non-object JSON value")
    return payload


def _assert_exact_names(
    actual: object, expected: tuple[str, ...], *, description: str
) -> None:
    if not isinstance(actual, list) or any(
        not isinstance(name, str) for name in actual
    ):
        raise RuntimeError(f"{description} has invalid resource names")
    duplicates = sorted(name for name, count in Counter(actual).items() if count > 1)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if duplicates or missing or extra or len(actual) != len(expected):
        raise RuntimeError(
            f"{description} resource set mismatch: "
            f"missing={missing}, duplicate={duplicates}, extra={extra}"
        )


def _expected_resource_names(fixture: Fixture) -> tuple[str, ...]:
    names = tuple(Path(name).stem for name in fixture.target_names)
    if len(names) != len(set(names)):
        raise RuntimeError("fixture has duplicate resource names")
    if not (
        len(names)
        == len(fixture.sources)
        == len(fixture.source_names)
        == len(fixture.source_kinds)
    ):
        raise RuntimeError("fixture resource metadata is incomplete")
    return names


def _expected_states(actions: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {
        name: ("unchanged" if action == "noop" else "committed")
        for name, action in actions
    }


def _assert_targets(fixture: Fixture) -> None:
    target_root = fixture.root / "targets"
    actual_names = sorted(path.name for path in target_root.iterdir())
    expected_names = sorted(Path(name).name for name in fixture.target_names)
    if actual_names != expected_names:
        raise RuntimeError(
            f"target set mismatch: expected={expected_names}, got={actual_names}"
        )
    for source, target_name, kind in zip(
        fixture.sources, fixture.target_names, fixture.source_kinds
    ):
        target = fixture.root / target_name
        if kind == "symbolic":
            matches = target.is_symlink() and target.resolve(
                strict=True
            ) == source.resolve(strict=True)
        else:
            matches = (
                not target.is_symlink()
                and target.is_file()
                and target.read_bytes() == source.read_bytes()
            )
        if not matches:
            raise RuntimeError(
                f"target does not match its fixture source: {target_name}"
            )


def _assert_recovery_result(
    payload: dict[str, object],
    fixture: Fixture,
    record_path: Path,
    actions: tuple[tuple[str, str], ...],
) -> None:
    if (
        payload.get("outcome") != "confirmed"
        or payload.get("record_state") != "committed"
        or payload.get("journal") != {"path": str(record_path)}
    ):
        raise RuntimeError("recovery did not confirm the committed journal")
    resources = payload.get("resources")
    expected_names = _expected_resource_names(fixture)
    if not isinstance(resources, list):
        raise TypeError("recovery result has no resource list")
    actual_names = [
        resource.get("name") if isinstance(resource, dict) else None
        for resource in resources
    ]
    _assert_exact_names(actual_names, expected_names, description="recovery result")
    states = _expected_states(actions)
    for ordinal, (resource, (name, _action)) in enumerate(zip(resources, actions)):
        if (
            not isinstance(resource, dict)
            or resource.get("ordinal") != ordinal
            or resource.get("record_state") != states[name]
            or resource.get("reobserved_state") != "confirmed"
            or resource.get("plan_status") != "in_sync"
        ):
            raise RuntimeError(f"recovery did not confirm resource {name}")
        paths = resource.get("paths")
        if not isinstance(paths, list):
            raise TypeError(f"recovery paths are incomplete for {name}")
        path_by_role = {
            path.get("role"): path for path in paths if isinstance(path, dict)
        }
        if (
            len(paths) != 2
            or set(path_by_role) != {"source", "target"}
            or path_by_role["source"].get("path") != fixture.source_names[ordinal]
            or path_by_role["target"].get("path") != fixture.target_names[ordinal]
            or any(
                path.get("matches_postcondition") is not True
                for path in path_by_role.values()
            )
        ):
            raise RuntimeError(f"recovery paths are incomplete for {name}")


def _assert_apply_result(
    payload: dict[str, object],
    fixture: Fixture,
    record_path: Path,
    actions: tuple[tuple[str, str], ...],
) -> None:
    if payload.get("state") != "committed" or payload.get("outcome") != "committed":
        raise RuntimeError("apply command did not report a committed outcome")
    if not record_path.is_file():
        raise RuntimeError(f"execution journal was not created: {record_path}")
    preview = payload.get("preview")
    if not isinstance(preview, dict):
        raise TypeError("apply result has no execution preview")
    _assert_action_set(
        fixture,
        "timed apply",
        _actions_from_payload(preview),
        dict(actions),
    )
    target_names = tuple(fixture.target_names)
    if payload.get("target_names") != list(target_names):
        raise RuntimeError("apply result target list is incomplete")
    changed_targets = [
        fixture.target_names[ordinal]
        for ordinal, (_name, action) in enumerate(actions)
        if action != "noop"
    ]
    if payload.get("changed_targets") != changed_targets:
        raise RuntimeError("apply result changed-target list is incomplete")
    journal = payload.get("journal")
    if (
        not isinstance(journal, dict)
        or journal.get("path") != str(record_path)
        or journal.get("created") is not True
        or journal.get("state") != "committed"
        or journal.get("manifest_version") != 5
    ):
        raise RuntimeError("apply result journal metadata is incomplete")


def _verify_sample(
    fixture: Fixture,
    scenario: str,
    record: Path,
    actions: tuple[tuple[str, str], ...],
    completed: subprocess.CompletedProcess[str],
) -> None:
    payload = _json_object(completed.stdout, description=f"{scenario} sample")
    if scenario == "plan":
        actual = _actions_from_payload(payload)
        _assert_action_set(fixture, "timed plan", actual, dict(actions))
        target_root = fixture.root / "targets"
        if any(target_root.iterdir()):
            raise RuntimeError("plan sample unexpectedly created targets")
        if record.exists():
            raise RuntimeError("plan sample unexpectedly created an execution journal")
        return

    if scenario == "recovery":
        setup_record = fixture.root / "setup.journal.json"
        setup_actions = tuple(
            (name, "create") for name in _expected_resource_names(fixture)
        )
        _assert_recovery_result(payload, fixture, setup_record, setup_actions)
        _assert_targets(fixture)
        return

    _assert_apply_result(payload, fixture, record, actions)
    _assert_targets(fixture)
    recovery = _run_capture(fixture, "recover", "--record", str(record), "--json")
    recovery_payload = _json_object(recovery.stdout, description="post-sample recovery")
    _assert_recovery_result(recovery_payload, fixture, record, actions)


def _timed_sample(
    base: Path,
    size: int,
    scenario: str,
    *,
    python: Path,
    source_root: Path | None,
) -> tuple[float, tuple[tuple[str, str], ...]]:
    with tempfile.TemporaryDirectory(dir=base) as directory:
        fixture = _write_fixture(
            Path(directory),
            size,
            python=python,
            source_root=source_root,
        )
        record = _prepare_scenario(fixture, scenario)
        actions = _assert_prepared_scenario(fixture, scenario)
        args: tuple[str, ...]
        if scenario == "plan":
            args = (
                "plan",
                "--manifest",
                str(fixture.manifest),
                "--json",
            )
        elif scenario == "recovery":
            args = (
                "recover",
                "--record",
                str(fixture.root / "setup.journal.json"),
                "--json",
            )
        else:
            args = (
                "apply",
                "--manifest",
                str(fixture.manifest),
                "--yes",
                "--record",
                str(record),
                "--json",
            )
        started = time.perf_counter()
        completed = _run_capture(fixture, *args)
        elapsed = time.perf_counter() - started
        _verify_sample(fixture, scenario, record, actions, completed)
        return elapsed, actions


def _measure(
    base: Path,
    size: int,
    scenario: str,
    repetitions: int,
    *,
    python: Path,
    source_root: Path | None,
) -> dict[str, object]:
    first, first_actions = _timed_sample(
        base,
        size,
        scenario,
        python=python,
        source_root=source_root,
    )
    warm_results = [
        _timed_sample(
            base,
            size,
            scenario,
            python=python,
            source_root=source_root,
        )
        for _ in range(repetitions)
    ]
    warm_samples = [sample for sample, _actions in warm_results]
    actions = [actions for _sample, actions in warm_results]
    if any(item != first_actions for item in actions):
        raise RuntimeError("scenario action set changed between restored samples")
    return {
        "size": size,
        "scenario": scenario,
        "first": first,
        "samples": warm_samples,
        "warm_samples": warm_samples,
        "median": statistics.median(warm_samples),
        "maximum": max(warm_samples),
        "actions": [{"name": name, "action": action} for name, action in first_actions],
    }


def _parse_sizes(values: Iterable[str]) -> tuple[int, ...]:
    sizes = tuple(int(value) for value in values)
    if not sizes or any(size <= 0 for size in sizes):
        raise argparse.ArgumentTypeError("sizes must be positive integers")
    return sizes


def _findmnt_metadata(scratch: Path) -> dict[str, object]:
    findmnt = shutil.which("findmnt")
    if findmnt is None:
        return {"available": False, "filesystem": None, "target": None}
    try:
        completed = subprocess.run(
            [
                findmnt,
                "--json",
                "--target",
                str(scratch.resolve()),
                "--output",
                "FSTYPE,TARGET",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return {"available": False, "filesystem": None, "target": None}
    if completed.returncode != 0:
        return {"available": True, "filesystem": None, "target": None}
    try:
        filesystems_value = json.loads(completed.stdout).get("filesystems", [])
    except (AttributeError, json.JSONDecodeError):
        filesystems_value = []
    filesystems = filesystems_value if isinstance(filesystems_value, list) else []
    mount = filesystems[0] if filesystems and isinstance(filesystems[0], dict) else {}
    return {
        "available": True,
        "filesystem": mount.get("fstype"),
        "target": mount.get("target"),
    }


def _environment_metadata(
    scratch: Path,
    *,
    python: Path,
    runtime: dict[str, str],
) -> dict[str, object]:
    statvfs = os.statvfs(scratch)
    return {
        "python": str(python),
        "python_version": runtime["python_version"],
        "python_prefix": runtime["python_prefix"],
        "python_site_packages": runtime["python_site_packages"],
        "luwu": {
            "file": runtime["luwu_file"],
            "version": runtime["luwu_version"],
        },
        "platform": sys.platform,
        "filesystem": {
            "block_size": statvfs.f_frsize,
            "findmnt": _findmnt_metadata(scratch),
        },
        "sandbox_mode": os.environ.get("LUWU_SANDBOX_MODE", "unspecified"),
    }


def _write_checkpoint(path: Path, payload: dict[str, object]) -> None:
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


def _add_scaling_ratios(results: list[dict[str, object]]) -> None:
    medians = {
        (int(row["size"]), str(row["scenario"])): float(row["median"])
        for row in results
    }
    for row in results:
        if int(row["size"]) != 200:
            continue
        baseline = medians.get((100, str(row["scenario"])))
        if baseline is not None and baseline > 0:
            row["ratio_100_to_200"] = float(row["median"]) / baseline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch",
        type=Path,
        default=Path(tempfile.gettempdir()),
        help="durable scratch parent; fixture directories are removed after each sample",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python executable used for timed CLI subprocesses",
    )
    parser.add_argument(
        "--installed",
        action="store_true",
        help="run without the source checkout on PYTHONPATH and verify the imported package",
    )
    parser.add_argument(
        "--size",
        dest="sizes",
        action="append",
        default=None,
        help="resource count (repeatable; defaults to the standard size matrix)",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        action="append",
        default=None,
        help="scenario (repeatable; defaults to all batch scenarios)",
    )
    parser.add_argument("--output", type=Path, help="optional JSON result path")
    args = parser.parse_args(argv)
    if args.repetitions < 5:
        parser.error("--repetitions must be at least five warm samples")
    if not args.scratch.exists() or not args.scratch.is_dir():
        parser.error("--scratch must be an existing directory")
    python = args.python.expanduser().absolute()
    if not python.is_file():
        parser.error("--python must name an executable file")
    sizes = _parse_sizes(args.sizes or (str(size) for size in SIZES))
    scenarios = tuple(args.scenario or SCENARIOS)
    source_root = (
        None if args.installed else Path(__file__).resolve().parents[1] / "src"
    )
    try:
        runtime = _probe_python(
            python,
            scratch=args.scratch,
            source_root=source_root,
            installed=args.installed,
        )
    except RuntimeError as error:
        parser.error(str(error))

    results: list[dict[str, object]] = []
    payload = {
        "schema_version": 1,
        "complete": False,
        "fixture": "synthetic-public-v5",
        "sizes": list(sizes),
        "scenarios": list(scenarios),
        "repetitions": args.repetitions,
        "sample_policy": "one first sample plus restored independent warm samples",
        "environment": _environment_metadata(
            args.scratch,
            python=python,
            runtime=runtime,
        ),
        "results": results,
    }
    if args.output is not None:
        _write_checkpoint(args.output, payload)
    for size in sizes:
        for scenario in scenarios:
            results.append(
                _measure(
                    args.scratch,
                    size,
                    scenario,
                    args.repetitions,
                    python=python,
                    source_root=source_root,
                )
            )
            _add_scaling_ratios(results)
            if args.output is not None:
                _write_checkpoint(args.output, payload)

    payload["complete"] = True
    _add_scaling_ratios(results)
    if args.output is not None:
        _write_checkpoint(args.output, payload)
    else:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
