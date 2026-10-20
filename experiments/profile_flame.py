"""Sample one benchmark scenario with py-spy and print where the time goes.

Builds the same fixture as ``profile_benchmark.py``, runs a single CLI command
under ``py-spy record`` (1 kHz, no root needed for a child process), and writes
a collapsed-stack file plus a flame graph SVG. The printed table lists the
inclusive share of every Luwu frame, so a change can be judged by whether the
frame it targets shrank.

    .venv/bin/python experiments/profile_flame.py onechange 200

py-spy is run with ``uvx py-spy`` unless a ``py-spy`` executable is on PATH.
"""

from __future__ import annotations

import argparse
import collections
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_benchmark as bench

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("plan", "noop", "onechange", "allchanged")


def _sample(
    scenario: str, size: int, python: Path, scratch: Path, output: Path, fmt: str
) -> None:
    source_root = ROOT / "src"
    with tempfile.TemporaryDirectory(prefix="luwu-flame-", dir=scratch) as directory:
        fixture = bench._write_fixture(Path(directory), size)
        bench._seed_baselines(fixture, python, source_root)
        if scenario != "plan":
            bench._run(
                fixture, python, source_root, *bench._command_args(fixture, "apply")
            )
        bench._set_scenario_sources(fixture, scenario)
        operation = "plan" if scenario == "plan" else "apply"
        spy = ["py-spy"] if shutil.which("py-spy") else ["uvx", "py-spy"]
        # py-spy exits 1 with "No child process" when the target ends first;
        # the samples are already written by then.
        subprocess.run(
            [
                *spy,
                "record",
                "-r",
                "1000",
                "-f",
                fmt,
                "-o",
                str(output),
                "--",
                str(python),
                "-m",
                "luwu",
                *bench._command_args(fixture, operation),
            ],
            cwd=fixture.root,
            env=bench._environment(source_root),
            capture_output=True,
            text=True,
            check=False,
        )


def _report(folded: Path, top: int) -> None:
    total = 0
    inclusive: collections.Counter[str] = collections.Counter()
    own: collections.Counter[str] = collections.Counter()
    for line in folded.read_text(encoding="utf-8").splitlines():
        stack, _, count = line.rpartition(" ")
        samples = int(count)
        total += samples
        frames = [re.sub(r":\d+\)", ")", frame) for frame in stack.split(";")]
        for frame in set(frames):
            inclusive[frame] += samples
        own[frames[-1]] += samples
    print(f"samples: {total} (1 ms each)")
    print("\ninclusive share of Luwu frames")
    shown = 0
    for frame, samples in inclusive.most_common():
        if "(luwu/" in frame and "<module>" not in frame:
            name = frame
            print(f"{samples / total * 100:5.1f}%  {name}")
            shown += 1
            if shown == top:
                break
    print("\nself time")
    for frame, samples in own.most_common(8):
        print(f"{samples / total * 100:5.1f}%  {frame}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("size", type=int)
    parser.add_argument("--python", type=Path, default=ROOT / ".venv/bin/python")
    parser.add_argument(
        "--scratch", type=Path, default=Path.home() / ".cache/luwu-bench/flame"
    )
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--no-svg", action="store_true")
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    stem = args.scratch / f"{args.scenario}-{args.size}"
    python = args.python.absolute()
    _sample(
        args.scenario,
        args.size,
        python,
        args.scratch,
        stem.with_suffix(".folded"),
        "raw",
    )
    if not args.no_svg:
        _sample(
            args.scenario,
            args.size,
            python,
            args.scratch,
            stem.with_suffix(".svg"),
            "flamegraph",
        )
        print(f"flame graph: {stem.with_suffix('.svg')}")
    _report(stem.with_suffix(".folded"), args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
