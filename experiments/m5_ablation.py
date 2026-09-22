# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Ablate the M5 batch-interruption guards against real temporary projects.

Each probe runs the real planner, executor, journal, and read-only recovery
view on a temporary project, then contrasts the production guard with an
intentionally unsafe off model. The off models deliberately bypass one guard
to reproduce the dishonest outcome that the guard prevents. No secrets are
used and nothing durable is created outside ``TemporaryDirectory``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from luwu import reconcile
from luwu.errors import ApplyError
from luwu.plan_record import PlanRecordError
from luwu.reconcile import Plan, ResourceObservation
from tests.test_m3c_execution import _ExecutionProject


def manifest_guard_probe(*, guard: bool) -> bool:
    """A stale plan must not mutate a target after the manifest changed."""
    with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
        plan = reconcile.build_plan(project.manifest)
        # The manifest now declares a different target; the stale plan still
        # holds the old one.
        project.manifest_path.write_text(
            project.manifest_path.read_text().replace(
                'target = "targets/alpha.conf"',
                'target = "targets/alpha-retired.conf"',
            )
        )
        record_path = project.root / "journal.json"
        retired = project.root / "targets/alpha-retired.conf"

        if guard:
            try:
                reconcile.execute_execution_plan(plan, record_path, confirm=True)
            except ApplyError as exc:
                refused = exc.code == "stale_plan"
            else:
                refused = False
            return (
                refused
                and project.targets["alpha"].read_text() == "alpha-old\n"
                and not retired.exists()
                and not record_path.exists()
            )

        with patch(
            "luwu.reconcile._preflight_manifest",
            lambda *args, **kwargs: None,
        ):
            reconcile.execute_execution_plan(plan, record_path, confirm=True)
        # The disabled guard let the stale plan mutate the target that the
        # current manifest no longer declares.
        stale_mutated = (
            project.targets["alpha"].read_text() == "alpha-desired\n"
            and not retired.exists()
        )
        return not stale_mutated


def journal_cas_probe(*, guard: bool) -> bool:
    """A competing journal update must never be silently accepted."""
    with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
        plan = reconcile.build_plan(project.manifest)
        record_path = project.root / "journal.json"
        base = reconcile._execution_record(plan)
        base.write(record_path)
        advanced = base.transition("preflighted")
        advanced.write(record_path, expected=base)
        # A competing writer wins the next compare-and-swap.
        competing = advanced.transition_path(0, "preflighted")
        competing.write(record_path, expected=advanced)
        competing_bytes = record_path.read_bytes()

        if guard:
            # A slower writer still holds the older snapshot and tries to
            # republish it; the CAS guard must raise a conflict.
            try:
                advanced.write(record_path, expected=base)
            except PlanRecordError as exc:
                conflicted = exc.code == "plan_record_cas"
            else:
                conflicted = False
            return conflicted and record_path.read_bytes() == competing_bytes

        # Intentionally unsafe model: last-writer-wins overwrite accepts the
        # stale snapshot and erases the competing update.
        record_path.write_text(
            json.dumps(
                advanced.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        journal = reconcile.inspect_execution_record(record_path)
        resources = [item["state"] for item in journal["resources"]]
        accepted = record_path.read_bytes() != competing_bytes and resources == [
            "planned"
        ]
        return not accepted


def noop_ordering_probe(*, guard: bool) -> bool:
    """An unprocessed no-op stays not-attempted when an earlier writer fails."""
    with _ExecutionProject(
        ("beta", "alpha"),  # execution order: alpha (replace), beta (noop)
        targets={"alpha": "alpha-old\n", "beta": "beta-desired\n"},
    ) as project:
        plan = reconcile.build_plan(project.manifest)
        record_path = project.root / "journal.json"

        if guard:
            real_writer = reconcile._write_observation

            def fail_alpha_writer(
                current_plan: Plan,
                observation: ResourceObservation,
                *,
                execution: bool = False,
            ) -> bool:
                if observation.resource.name == "alpha":
                    raise ApplyError(
                        "writer failed before replacement",
                        code="write_failed",
                        committed=False,
                    )
                return real_writer(current_plan, observation, execution=execution)

            with patch(
                "luwu.reconcile._write_observation",
                side_effect=fail_alpha_writer,
            ):
                try:
                    reconcile.execute_execution_plan(plan, record_path, confirm=True)
                except ApplyError:
                    pass
                else:
                    raise AssertionError("writer fault was not exercised")

            journal = reconcile.inspect_execution_record(record_path)
            states = [item["state"] for item in journal["resources"]]
            assert states == ["unknown", "not-attempted"], states
            result = reconcile.reobserve_execution_record(record_path)
            reobserved = [item["reobserved_state"] for item in result["resources"]]
            # The unprocessed no-op is never reported as confirmed/unchanged.
            return reobserved == ["changed_or_unknown", "not-attempted"]

        # Off model: eagerly pre-mark every no-op as unchanged before the
        # changed writer is processed, then record the writer failure. The
        # journal is representable, but it misreports an unprocessed no-op.
        record = (
            reconcile._execution_record(plan)
            .transition("preflighted")
            .transition_path(0, "preflighted")
            .transition_path(1, "preflighted")
            .transition("commit_intent")
            .transition_path(0, "commit_intent")
            .transition_path(1, "unchanged")
            .transition_path(0, "unknown")
            .transition("recovery_required")
        )
        record.write(record_path)
        result = reconcile.reobserve_execution_record(record_path)
        reobserved = [item["reobserved_state"] for item in result["resources"]]
        # The pre-marked journal misreports the unprocessed no-op as confirmed.
        misreported = reobserved == ["changed_or_unknown", "confirmed"]
        return not misreported


def main() -> None:
    probes = (
        (
            "manifest stale-plan guard",
            manifest_guard_probe,
            "stale apply refused; targets and journal untouched",
            "the stale plan mutated a no-longer-declared target",
        ),
        (
            "journal CAS guard",
            journal_cas_probe,
            "conflict raised; the competing update survived byte-for-byte",
            "the unsafe overwrite silently erased the competing update",
        ),
        (
            "batched no-op ordering guard",
            noop_ordering_probe,
            "the unprocessed no-op stayed not-attempted",
            "the pre-marked no-op was misreported as confirmed",
        ),
    )
    for name, probe, on_result, off_result in probes:
        assert probe(guard=True), name
        print(f"{name}: {on_result}")
        assert not probe(guard=False), name
        print(f"{name} off: {off_result}")
    print(
        "Interruption honesty needs the existing guards only; "
        "no rollback engine or second ledger is required"
    )


if __name__ == "__main__":
    main()
