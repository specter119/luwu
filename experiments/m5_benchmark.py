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
        manifest=manifest,
        python=python,
        source_root=source_root,
    )


def _run(fixture: Fixture, *args: str) -> None:
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


def _prepare_scenario(fixture: Fixture, scenario: str) -> Path:
    record = fixture.root / f"{scenario}.journal.json"
    if scenario == "plan":
        return record
    if scenario in {"noop", "one-change", "all-changed", "recovery"}:
        _apply(fixture, fixture.root / "setup.journal.json")
    if scenario == "one-change":
        fixture.sources[0].write_text(
            'public_value = "changed"\npublic_index = 0\n',
            encoding="utf-8",
        )
    elif scenario == "all-changed":
        for ordinal, source in enumerate(fixture.sources):
            if source.suffix == ".j2":
                source.write_text(
                    f'public_value = "changed-{ordinal:04d}"\n'
                    f"public_index = {ordinal}\n",
                    encoding="utf-8",
                )
            else:
                source.write_text(
                    f"public-symbolic-changed-{ordinal:04d}\n",
                    encoding="utf-8",
                )
    return record


def _timed_sample(
    base: Path,
    size: int,
    scenario: str,
    *,
    python: Path,
    source_root: Path | None,
) -> float:
    with tempfile.TemporaryDirectory(dir=base) as directory:
        fixture = _write_fixture(
            Path(directory),
            size,
            python=python,
            source_root=source_root,
        )
        record = _prepare_scenario(fixture, scenario)
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
        return time.perf_counter() - started


def _measure(
    base: Path,
    size: int,
    scenario: str,
    repetitions: int,
    *,
    python: Path,
    source_root: Path | None,
) -> dict[str, object]:
    samples = [
        _timed_sample(
            base,
            size,
            scenario,
            python=python,
            source_root=source_root,
        )
        for _ in range(repetitions)
    ]
    return {
        "size": size,
        "scenario": scenario,
        "first": samples[0],
        "samples": samples,
        "median": statistics.median(samples),
        "maximum": max(samples),
    }


def _parse_sizes(values: Iterable[str]) -> tuple[int, ...]:
    sizes = tuple(int(value) for value in values)
    if not sizes or any(size <= 0 for size in sizes):
        raise argparse.ArgumentTypeError("sizes must be positive integers")
    return sizes


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
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
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
    payload = {
        "schema_version": 1,
        "fixture": "synthetic-public-v5",
        "sizes": list(sizes),
        "scenarios": list(scenarios),
        "repetitions": args.repetitions,
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
