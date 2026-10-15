# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Ablate M3 closure guards against real operations in temporary projects.

Small wrappers exercise the proposed contract before implementation. Removing
each guard also bypasses its later production equivalent. This experiment is
design evidence; the production failure matrix remains the regression suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from luwu import mutations, reconcile
from luwu.errors import ApplyError, MutationError
from luwu.manifest import load_manifest
from luwu.plan_record import PlanRecord, PlanRecordError
from tests.test_m3b import _Project
from tests.test_m3c_execution import _ExecutionProject


def publication_probe(*, guard: bool, phase: str) -> bool:
    with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
        original_write = PlanRecord.write
        original_writer = reconcile._write_observation
        completed_targets: list[str] = []
        journal_published = phase == "initial"

        def write_record(self, path, *, expected=None):
            state = self.to_dict()["state"]
            if phase == "initial" and state == "planned":
                original_write(self, path, expected=expected)
                raise PlanRecordError(
                    "injected journal failure",
                    code="plan_record_durability_unknown",
                    committed=True,
                )
            if phase == "final" and state == "committed":
                raise PlanRecordError(
                    "injected journal failure",
                    code="plan_record_write",
                    committed=False,
                )
            return original_write(self, path, expected=expected)

        def write_target(plan, observation, *, execution=False):
            result = original_writer(plan, observation, execution=execution)
            completed_targets.append(observation.resource.target_name)
            return result

        with (
            patch.object(PlanRecord, "write", write_record),
            patch("luwu.reconcile._write_observation", side_effect=write_target),
        ):
            try:
                reconcile.execute_execution_plan(
                    reconcile.build_plan(project.manifest),
                    project.root / "journal.json",
                    confirm=True,
                )
            except ApplyError:
                # Minimal in-memory accumulation needs no second durable log.
                reported_commit = (
                    bool(completed_targets) if guard else journal_published
                )
            else:
                raise AssertionError("journal fault was not exercised")
        actually_changed = project.targets["alpha"].read_text() == "alpha-desired\n"
        return reported_commit == actually_changed


def recovery_probe(*, guard: bool) -> bool:
    with _ExecutionProject(("alpha",), targets={"alpha": "alpha-desired\n"}) as project:
        record = reconcile._execution_record(reconcile.build_plan(project.manifest))
        record = (
            record.transition("preflighted")
            .transition_path(0, "not-attempted")
            .transition("unknown")
        )
        path = project.root / "journal.json"
        record.write(path)
        before = path.read_bytes()
        result = reconcile.reobserve_execution_record(path)
        states = [item["reobserved_state"] for item in result["resources"]]
        confirmed = (
            all(state == "confirmed" for state in states)
            if guard
            else not any(
                state in {"changed_or_unknown", "matches_postcondition"}
                for state in states
            )
        )
        assert states == ["not-attempted"]
        assert path.read_bytes() == before
        return not confirmed


def conflict_probe(*, guard: bool) -> bool:
    with _Project() as project:
        project.write_baseline({"setting": 0, "runtime": 1})
        project.target.write_text('{"setting": 3, "runtime": 2, "undeclared": "live"}')
        manifest = load_manifest(project.manifest)
        before = project.source.read_bytes()
        real_aggregate = reconcile._aggregate_ownership

        def aggregate(ownership):
            status, action, reason = real_aggregate(ownership)
            # Bypass the production guard once it exists, leaving the actual
            # field decisions intact for the selected-field check and writer.
            if not guard and status is reconcile.Status.CONFLICT:
                status = reconcile.Status.DRIFTED
            return status, action, reason

        with patch("luwu.reconcile._aggregate_ownership", side_effect=aggregate):
            try:
                observation = reconcile.build_plan(manifest).observations[0]
                if guard and observation.status is reconcile.Status.CONFLICT:
                    raise MutationError(
                        "field changes require review", code="review_required"
                    )
                mutations.reverse_sync(
                    manifest,
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )
            except MutationError:
                pass
        return project.source.read_bytes() == before


if __name__ == "__main__":
    for phase in ("initial", "final"):
        assert publication_probe(guard=True, phase=phase), phase
        assert not publication_probe(guard=False, phase=phase), phase
        print(f"{phase} journal failure: target facts survive; publication-only fails")
    for name, probe in (
        ("unattempted recovery", recovery_probe),
        ("unselected conflict", conflict_probe),
    ):
        assert probe(guard=True), name
        assert not probe(guard=False), name
        print(f"{name}: minimal guard passes; removal reproduces the failure")
    print("No extra durable ledger, recovery engine, or policy registry required")
