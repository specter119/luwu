# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Measure synthetic provider resolution latency separately from M5 I/O."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from luwu.manifest import load_manifest
from luwu.providers import ProviderAuthority
from luwu.reconcile import build_plan


class SyntheticResolver:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.calls = 0

    def resolve(self, reference: object, *, authority: ProviderAuthority) -> str:
        del reference, authority
        self.calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return "synthetic-provider-value"


def _write_manifest(root: Path, target_root: Path, size: int) -> Path:
    root.mkdir(parents=True)
    (root / "templates").mkdir()
    target_root.mkdir()
    resources = []
    for ordinal in range(size):
        name = f"resource_{ordinal:04d}"
        (root / "templates" / f"{name}.conf.j2").write_text(
            "value={{ secrets.value }}\n", encoding="utf-8"
        )
        resources.append(
            f'''[resources.{name}]
kind = "template"
source = "templates/{name}.conf.j2"
target = "{target_root / f"{name}.conf"}"
owner = "source"
scope = "whole-file"
content_sensitivity = "secret"

[resources.{name}.providers.value]
type = "rbw"
item = "synthetic-item"
field = "value"
'''
        )
    manifest = root / "luwu.toml"
    manifest.write_text(
        'version = 6\ncapabilities = ["subprocess"]\n\n' + "\n".join(resources),
        encoding="utf-8",
    )
    return manifest


def _latencies(values: list[str] | None) -> tuple[float, ...]:
    parsed = tuple(float(value) for value in (values or ["0", "1", "5", "20"]))
    if not parsed or any(value < 0 for value in parsed):
        raise ValueError("latencies must be non-negative milliseconds")
    return parsed


def _sizes(values: list[str] | None) -> tuple[int, ...]:
    parsed = tuple(int(value) for value in (values or ["1", "10", "30", "100"]))
    if not parsed or any(value <= 0 for value in parsed):
        raise ValueError("sizes must be positive")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--latency-ms", action="append", dest="latencies")
    parser.add_argument("--size", action="append", dest="sizes")
    args = parser.parse_args(argv)
    if args.repetitions < 5:
        parser.error("--repetitions must be at least five")
    rows: list[dict[str, object]] = []
    for size in _sizes(args.sizes):
        for latency_ms in _latencies(args.latencies):
            with tempfile.TemporaryDirectory(prefix="luwu-m5-provider-") as directory:
                workspace = Path(directory)
                manifest = load_manifest(
                    _write_manifest(workspace / "manifest", workspace / "targets", size)
                )
                samples: list[float] = []
                calls: list[int] = []
                for sample_number in range(args.repetitions + 1):
                    resolver = SyntheticResolver(latency_ms / 1000.0)
                    started = time.perf_counter()
                    build_plan(
                        manifest,
                        authority=ProviderAuthority(capabilities={"subprocess"}),
                        resolver=resolver,
                    )
                    elapsed = time.perf_counter() - started
                    if sample_number == 0:
                        first = elapsed
                    else:
                        samples.append(elapsed)
                    calls.append(resolver.calls)
            rows.append(
                {
                    "size": size,
                    "latency_ms": latency_ms,
                    "first": first,
                    "samples": samples,
                    "median": statistics.median(samples),
                    "maximum": max(samples),
                    "provider_calls": calls,
                }
            )
    payload = {
        "schema_version": 1,
        "fixture": "synthetic-secret-provider-boundary",
        "provider": "injected-resolver",
        "provider_access": "none",
        "network_access": "none",
        "repetitions": args.repetitions,
        "sample_policy": "one first sample plus independent warm samples",
        "rows": rows,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
