# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Profile sanitized M5 planning and execution paths.

The report contains timing, allocation, call-count and function-name metadata
only. Fixture contents are synthetic and are removed after each row.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from experiments.m5_benchmark import _write_fixture
from luwu import reconcile
from luwu.manifest import load_manifest
from luwu.reconcile import build_plan, execute_execution_plan


def _profile_action(action: object, stats_path: Path) -> dict[str, object]:
    counters = {"directory_sync": 0, "journal_publication": 0}
    real_sync = reconcile.sync_directory
    real_publish = reconcile._write_execution_record

    def counted_sync(*args: object, **kwargs: object) -> None:
        counters["directory_sync"] += 1
        real_sync(*args, **kwargs)

    def counted_publish(*args: object, **kwargs: object) -> None:
        counters["journal_publication"] += 1
        real_publish(*args, **kwargs)

    profiler = cProfile.Profile()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        with (
            patch.object(reconcile, "sync_directory", counted_sync),
            patch.object(reconcile, "_write_execution_record", counted_publish),
        ):
            profiler.enable()
            action()
            profiler.disable()
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    elapsed = time.perf_counter() - started
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    profiler.dump_stats(str(stats_path))
    stats = pstats.Stats(profiler)
    top = []
    for (filename, line, function), values in sorted(
        stats.stats.items(), key=lambda item: item[1][3], reverse=True
    )[:12]:
        primitive_calls, calls, total, cumulative, _callers = values
        top.append(
            {
                "function": f"{Path(filename).name}:{line}:{function}",
                "primitive_calls": primitive_calls,
                "calls": calls,
                "total_seconds": total,
                "cumulative_seconds": cumulative,
            }
        )
    return {
        "seconds": elapsed,
        "peak_traced_bytes": peak,
        "directory_sync_calls": counters["directory_sync"],
        "journal_publication_calls": counters["journal_publication"],
        "profile": str(stats_path),
        "top_functions": top,
    }


def _profile_row(
    scratch: Path,
    output_dir: Path,
    size: int,
    *,
    operation: str,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix=f"luwu-m5-profile-{size}-", dir=scratch
    ) as directory:
        fixture = _write_fixture(
            Path(directory),
            size,
            python=Path(sys.executable),
            source_root=ROOT / "src",
        )
        manifest = load_manifest(fixture.manifest)
        record = fixture.root / "profile.journal.json"
        if operation == "plan":
            action = lambda: build_plan(manifest)
        elif operation == "apply":
            action = lambda: execute_execution_plan(
                build_plan(manifest), record, confirm=True
            )
        else:
            raise ValueError(f"unknown operation: {operation}")
        stats_path = output_dir / f"{operation}-{size}.pstats"
        profile = _profile_action(action, stats_path)
        return {
            "size": size,
            "operation": operation,
            "profile": profile,
        }


def _sizes(values: list[str] | None) -> tuple[int, ...]:
    parsed = tuple(int(value) for value in (values or ["30", "100", "176", "200"]))
    if not parsed or any(value <= 0 for value in parsed):
        raise ValueError("sizes must be positive")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--size", dest="sizes", action="append")
    args = parser.parse_args(argv)
    if not args.scratch.is_dir():
        parser.error("--scratch must be an existing directory")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        _profile_row(args.scratch, args.output_dir, size, operation=operation)
        for size in _sizes(args.sizes)
        for operation in ("plan", "apply")
    ]
    payload = {
        "schema_version": 1,
        "fixture": "synthetic-public-v5",
        "operations": ["plan", "apply"],
        "environment": {
            "python": str(sys.executable),
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "scratch": str(args.scratch),
            "filesystem_block_size": os.statvfs(args.scratch).f_frsize,
            "tracemalloc": True,
            "cprofile": True,
        },
        "rows": rows,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    (args.output_dir / "m5-profile.json").write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
