#!/usr/bin/env python3
"""Measure the latest M5 batch scenarios on a sanitized fixture.

The harness deliberately uses the real CLI and excludes fixture construction
from timed samples.  It is a baseline/acceptance aid, not a public command and
does not inspect a user's HOME, network, provider, or configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SIZES = (1, 10, 30, 100, 176, 200)
SCENARIOS = ("plan", "fresh", "noop", "one-change", "all-changed", "recovery")


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
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if fixture.source_root is None:
        environment.pop("PYTHONPATH", None)
    else:
        environment["PYTHONPATH"] = str(fixture.source_root)
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
    by_name = dict(actions)
    if scenario == "plan" or scenario == "fresh":
        expected = {name: "create" for name in by_name}
    elif scenario == "noop":
        expected = {name: "noop" for name in by_name}
    elif scenario == "one-change":
        expected = {
            name: ("create" if fixture.source_kinds[0] == "symbolic" else "replace")
            if index == 0
            else "noop"
            for index, name in enumerate(by_name)
        }
    elif scenario == "all-changed":
        expected = {
            name: ("create" if fixture.source_kinds[index] == "symbolic" else "replace")
            for index, name in enumerate(by_name)
        }
    elif scenario == "recovery":
        expected = {name: "noop" for name in by_name}
    else:
        raise RuntimeError(f"unknown scenario {scenario}")
    if by_name != expected:
        raise RuntimeError(
            f"scenario {scenario} action set mismatch: expected {expected}, got {by_name}"
        )
    return actions


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
        _run(fixture, *args)
        return time.perf_counter() - started, actions


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


def _environment_metadata(scratch: Path, *, python: Path) -> dict[str, object]:
    statvfs = os.statvfs(scratch)
    return {
        "python": str(python),
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "filesystem": {
            "block_size": statvfs.f_frsize,
            "durable_scratch": True,
            "tmpfs_excluded_by_operator": True,
        },
        "sandbox_mode": os.environ.get("LUWU_SANDBOX_MODE", "unspecified"),
    }


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
        help="run without the source checkout on PYTHONPATH",
    )
    parser.add_argument(
        "--size",
        dest="sizes",
        action="append",
        default=None,
        help="resource count (repeatable; defaults to the M5 size matrix)",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        action="append",
        default=None,
        help="scenario (repeatable; defaults to all M5 scenarios)",
    )
    parser.add_argument("--output", type=Path, help="optional JSON result path")
    args = parser.parse_args(argv)
    if args.repetitions < 5:
        parser.error("--repetitions must be at least five warm samples")
    if not args.scratch.exists() or not args.scratch.is_dir():
        parser.error("--scratch must be an existing directory")
    if not args.python.is_file():
        parser.error("--python must name an executable file")
    sizes = _parse_sizes(args.sizes or (str(size) for size in SIZES))
    scenarios = tuple(args.scenario or SCENARIOS)
    source_root = (
        None if args.installed else Path(__file__).resolve().parents[1] / "src"
    )
    results = [
        _measure(
            args.scratch,
            size,
            scenario,
            args.repetitions,
            python=args.python,
            source_root=source_root,
        )
        for size in sizes
        for scenario in scenarios
    ]
    _add_scaling_ratios(results)
    payload = {
        "schema_version": 1,
        "fixture": "synthetic-public-v5",
        "sizes": list(sizes),
        "scenarios": list(scenarios),
        "repetitions": args.repetitions,
        "sample_policy": "one first sample plus restored independent warm samples",
        "environment": _environment_metadata(args.scratch, python=args.python),
        "results": results,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
