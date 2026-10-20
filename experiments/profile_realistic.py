#!/usr/bin/env python3
"""Measure plan and apply on a dotfiles-shaped version-8 workload.

The public benchmark fixture is flat. This one mixes the shapes that dominate
a real manifest: symlinked files, symlinked directories with nested files,
whole-file templates that share input layer files, structured templates, and
plain copies. Besides wall time it reports operation counts from one
in-process run, so growth in repeated manifest verification, directory
expansion, input loading, or rendering is visible without a profiler.

Fixture setup and baseline seeding are not timed. Run on a durable local
filesystem (set TMPDIR); the numbers are run output, not repository content.

    PYTHONPATH=src python experiments/profile_realistic.py --sizes 100 200
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
from pathlib import Path

PROFILE = "realistic"
PACKAGE = "workload"
INPUT_KEYS = 40
SCENARIOS = ("plan", "noop", "fresh")
_SEED = """\
import json, sys
from pathlib import Path
from luwu.baseline import make_copy_baseline, make_field_baseline
from luwu.manifest import load_manifest

manifest = load_manifest(Path(sys.argv[1]), profile="realistic")
resources = {resource.name: resource for resource in manifest.resources}
for item in json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")):
    resource = resources[item["name"]]
    if item["seed"] == "copy":
        data = make_copy_baseline(
            manifest,
            resource,
            accepted_data=bytes.fromhex(item["data_hex"]),
            accepted_mode=item["mode"],
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
_COUNT = """\
import contextlib, io, json, sys, time
import luwu.engine as engine
import luwu.manifest as manifest
from luwu.cli import main

counts = {}
def wrap(module, name, key):
    original = getattr(module, name)
    def inner(*args, **kwargs):
        entry = counts.setdefault(key, [0, 0.0])
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            entry[0] += 1
            entry[1] += time.perf_counter() - started
    setattr(module, name, inner)

wrap(engine, "verify_manifest", "verify_manifest")
wrap(manifest, "_expand_directory", "expand_directory")
wrap(engine, "load_inputs", "load_inputs")
wrap(engine, "render_source", "render")
sys.argv = ["luwu", *json.loads(sys.argv[1])]
with contextlib.redirect_stdout(io.StringIO()):
    try:
        main()
    except SystemExit:
        pass
print(json.dumps({key: value[0] for key, value in counts.items()}))
"""


def _environment() -> dict[str, str]:
    environment = os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}
    paths = [
        str(Path(entry).resolve())
        for entry in environment.get("PYTHONPATH", "").split(os.pathsep)
        if entry
    ]
    if paths:
        environment["PYTHONPATH"] = os.pathsep.join(paths)
    return environment


def _toml(value: str) -> str:
    return json.dumps(value)


def write_fixture(root: Path, size: int) -> list[dict[str, object]]:
    source = root / "source"
    inputs = root / "inputs"
    for path in (source, inputs / "global", inputs / "baselines", root / "targets"):
        path.mkdir(parents=True, exist_ok=True)
    (inputs / "global" / "defaults.toml").write_text(
        "[k]\n" + "".join(f'k{i:02d} = "value-{i:02d}"\n' for i in range(INPUT_KEYS)),
        encoding="utf-8",
    )
    declarations = [
        f'[inputs.k{i:02d}]\nlayers = [{{ file = "global/defaults.toml", '
        f'path = ["k", "k{i:02d}"], format = "toml" }}]\n'
        for i in range(INPUT_KEYS)
    ]
    names: list[str] = []
    seeds: list[dict[str, object]] = []
    for ordinal in range(size):
        name = f"resource_{ordinal:04d}"
        names.append(name)
        slot = ordinal % 10
        if slot < 5:
            (source / "links").mkdir(exist_ok=True)
            (source / "links" / f"{name}.conf").write_text(f"link {ordinal}\n")
            declarations.append(
                f'[resources.{name}]\nsource = "links/{name}.conf"\n'
                f'target = ".config/links/{name}.conf"\n'
            )
        elif slot == 5:
            directory = source / "dirs" / name
            for leaf in range(8):
                nested = directory / f"sub{leaf % 2}"
                nested.mkdir(parents=True, exist_ok=True)
                (nested / f"file{leaf}.conf").write_text(f"dir {ordinal} {leaf}\n")
            declarations.append(
                f'[resources.{name}]\nsource = "dirs/{name}"\n'
                f'target = ".config/dirs/{name}"\n'
            )
        elif slot in (6, 7):
            first, second = ordinal % INPUT_KEYS, (ordinal * 7) % INPUT_KEYS
            template = (
                f"a={{{{ inputs.k{first:02d} }}}}\nb={{{{ inputs.k{second:02d} }}}}\n"
            )
            rendered = f"a=value-{first:02d}\nb=value-{second:02d}\n"
            (source / "tpl").mkdir(exist_ok=True)
            (source / "tpl" / f"{name}.conf.j2").write_text(template)
            declarations.append(
                f'[resources.{name}]\nsource = "tpl/{name}.conf.j2"\n'
                f'target = ".config/tpl/{name}.conf"\n'
                f'inputs = ["k{first:02d}", "k{second:02d}"]\n'
                f'baseline = "baselines/{name}.json"\nmode = "0600"\n'
            )
            seeds.append(
                {
                    "name": name,
                    "seed": "copy",
                    "data_hex": rendered.encode().hex(),
                    "mode": 0o600,
                }
            )
        elif slot == 8:
            fields = {"value": f"public-{ordinal:04d}", "index": ordinal}
            (source / "json").mkdir(exist_ok=True)
            (source / "json" / f"{name}.json.j2").write_text(
                json.dumps(fields, indent=2) + "\n"
            )
            declarations.append(
                f'[resources.{name}]\nsource = "json/{name}.json.j2"\n'
                f'target = ".config/json/{name}.json"\n'
                f'baseline = "baselines/{name}.json"\n'
                'format = "json"\ncontent_sensitivity = "public"\n'
                f'[resources.{name}.fields.value]\npath = ["value"]\nowner = "source"\n'
                f'[resources.{name}.fields.index]\npath = ["index"]\nowner = "source"\n'
            )
            seeds.append({"name": name, "seed": "fields", "fields": fields})
        else:
            data = f"copy {ordinal}\n"
            (source / "copies").mkdir(exist_ok=True)
            (source / "copies" / f"{name}.conf").write_text(data)
            declarations.append(
                f'[resources.{name}]\ndeploy = "copy"\n'
                f'source = "copies/{name}.conf"\n'
                f'target = ".config/copies/{name}.conf"\n'
                f'baseline = "baselines/{name}.json"\ncontent_sensitivity = "public"\n'
            )
            seeds.append(
                {
                    "name": name,
                    "seed": "copy",
                    "data_hex": data.encode().hex(),
                    "mode": 0o644,
                }
            )
    manifest = (
        "version = 8\n\n[roots]\n"
        'source = "source"\ninputs = "inputs"\ntarget = "targets"\n\n'
        '[defaults]\ndeploy = "symlink"\nparents = "create"\nparent_mode = "0700"\n\n'
        f"[profiles.{PROFILE}]\npackages = [{_toml(PACKAGE)}]\nexclude = []\n\n"
        f"[packages.{PACKAGE}]\ndependencies = []\nresources = ["
        + ", ".join(_toml(name) for name in names)
        + "]\n\n"
        + "\n".join(declarations)
    )
    (root / "luwu.toml").write_text(manifest, encoding="utf-8")
    return seeds


def luwu(root: Path, *args: str, check: bool = True) -> tuple[float, dict]:
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "luwu", *args],
        cwd=root,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    if check and completed.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {completed.stderr or completed.stdout}")
    return elapsed, json.loads(completed.stdout)


def command(scenario: str) -> tuple[str, ...]:
    common = ("--manifest", "luwu.toml", "--profile", PROFILE, "--json")
    if scenario == "plan":
        return ("plan", *common)
    return ("apply", *common, "--yes")


def prepare(root: Path, size: int, scenario: str) -> None:
    seeds = write_fixture(root, size)
    descriptor = root / "seed.json"
    descriptor.write_text(json.dumps(seeds), encoding="utf-8")
    subprocess.run(
        [sys.executable, "-B", "-c", _SEED, str(root / "luwu.toml"), str(descriptor)],
        cwd=root,
        env=_environment(),
        check=True,
    )
    descriptor.unlink()
    if scenario == "noop":
        luwu(root, *command("fresh"))


def measure(size: int, scenario: str, reps: int, workdir: Path) -> dict[str, object]:
    samples: list[float] = []
    for _ in range(reps):
        with tempfile.TemporaryDirectory(dir=workdir) as temporary:
            root = Path(temporary)
            prepare(root, size, scenario)
            elapsed, payload = luwu(root, *command(scenario))
            expected = "no_changes" if scenario == "noop" else "committed"
            if scenario != "plan" and payload.get("outcome") != expected:
                raise RuntimeError(f"unexpected outcome {payload.get('outcome')}")
            samples.append(elapsed)
    with tempfile.TemporaryDirectory(dir=workdir) as temporary:
        root = Path(temporary)
        prepare(root, size, scenario)
        counted = subprocess.run(
            [sys.executable, "-B", "-c", _COUNT, json.dumps(list(command(scenario)))],
            cwd=root,
            env=_environment(),
            capture_output=True,
            text=True,
            check=True,
        )
    return {
        "size": size,
        "scenario": scenario,
        "median": round(statistics.median(samples), 3),
        "max": round(max(samples), 3),
        "samples": [round(value, 3) for value in samples],
        "counts": json.loads(counted.stdout),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 200])
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS))
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.home() / ".cache" / "luwu-bench",
        help="durable local directory for fixtures; do not use tmpfs",
    )
    args = parser.parse_args()
    workdir = args.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    for scenario in args.scenarios:
        for size in args.sizes:
            print(json.dumps(measure(size, scenario, args.reps, workdir)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
