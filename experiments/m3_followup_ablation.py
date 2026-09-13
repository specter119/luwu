# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Temporary-directory probes for the M3 follow-up, never user configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from luwu import mutations, reconcile
from luwu.errors import ApplyError, MutationError
from luwu.manifest import load_manifest
from tests.test_m3b import _Project
from tests.test_m3c_execution import _ExecutionProject


def lock_probe(*, guard: bool) -> bool:
    with _ExecutionProject(("alpha",), targets={"alpha": None}) as project:
        text = project.manifest_path.read_text().replace(
            "targets/alpha.conf", ".journal.json.luwu-lock"
        )
        project.manifest_path.write_text(text)
        plan = reconcile.build_plan(load_manifest(project.manifest_path))
        journal = project.root / "journal.json"
        target = project.root / ".journal.json.luwu-lock"

        def check(candidate_plan: reconcile.Plan, path: Path) -> None:
            # Deliberately tiny candidate; no registry or storage abstraction.
            if guard and path.with_name(f".{path.name}.luwu-lock") == target:
                raise ApplyError("record path conflict", code="record_path_conflict")

        with patch("luwu.reconcile._check_execution_record_path", side_effect=check):
            try:
                reconcile.execute_execution_plan(plan, journal, confirm=True)
            except ApplyError:
                pass
        return not target.exists() and not journal.exists()


def baseline_probe(*, guard: bool) -> bool:
    with _Project() as project:
        project.write_baseline({"setting": 1, "runtime": 1})
        baseline = project.baseline.read_bytes()
        source = project.source.read_bytes()
        real_write = mutations._write_source

        def write(*args, **kwargs):
            project.write_baseline({"setting": 1, "runtime": 3})
            if guard and project.baseline.read_bytes() != baseline:
                raise MutationError("baseline changed", code="stale_plan")
            # Ablate the later production guard too, when present.
            kwargs["check_inputs"] = lambda: None
            return real_write(*args, **kwargs)

        with patch("luwu.mutations._write_source", side_effect=write):
            try:
                mutations.reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )
            except MutationError:
                pass
        return project.source.read_bytes() == source


def recovery_probe(*, guard: bool) -> bool:
    with _ExecutionProject(("alpha",), targets={"alpha": None}) as project:
        journal = project.root / "journal.json"
        reconcile.execute_execution_plan(
            reconcile.build_plan(project.manifest), journal, confirm=True
        )
        target = project.targets["alpha"]
        before = target.stat()
        target.write_bytes(b"x" * before.st_size)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        # Ablate the production comparison once implemented: path metadata stays
        # identical, so pretending the fresh observation is in_sync reproduces it.
        real_plan = reconcile.build_plan

        def plan(manifest):
            result = real_plan(manifest)
            if not guard:
                from dataclasses import replace

                result = replace(
                    result,
                    observations=tuple(
                        replace(item, status=reconcile.Status.IN_SYNC)
                        for item in result.observations
                    ),
                )
            return result

        with patch("luwu.reconcile.build_plan", side_effect=plan):
            result = reconcile.reobserve_execution_record(journal)
        if guard and any(
            item["plan_status"] != "in_sync" for item in result["resources"]
        ):
            result["outcome"] = "recovery_required"
        return result["outcome"] == "recovery_required"


if __name__ == "__main__":
    for name, probe in (
        ("lock footprint", lock_probe),
        ("baseline binding", baseline_probe),
        ("recovery observation", recovery_probe),
    ):
        assert probe(guard=True), name
        assert not probe(guard=False), name
        print(f"{name}: minimal guard passes; removal reproduces contract violation")
    print("No registry, transaction, persisted snapshot, or snapshot class required")
