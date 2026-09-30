"""M5 interruption and fault honesty table for public v5 execution.

Each row injects one fault at a precise boundary of
``luwu.reconcile.execute_execution_plan`` through existing patch seams (the
target writer, the post-replacement durability sync, and ``PlanRecord.write``).
No sleeps, subprocess kills, or timing are involved. Every row asserts that
the actual target bytes, the durable journal, the raised error, and the
read-only recovery views tell the same story:

- writer fails before the first target writer: both targets stay unchanged,
  the journal stays ``unknown``/``not-attempted``, and the error is
  ``execution_failed`` with ``committed=False``.
- writer fails after one committed target and before the next: the first
  target really changed, the journal records ``committed``/``unknown``/
  ``not-attempted``, and the error is ``recovery_required`` with
  ``committed=True``.
- durability fails after the replacement and before the committed record is
  published: the target really changed, the journal never claims a clean
  ``committed`` state for it, and recovery is required.
- journal publication fails before any target write: no target changes, no
  journal survives, and the error never claims a commit.
- journal CAS conflicts with a competing journal before any target write: the
  competing journal survives byte-for-byte and no false commit is reported.
- an unprocessed batched no-op stays ``not-attempted`` when an earlier changed
  writer fails; it is never reported as confirmed or unchanged.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from luwu import reconcile
from luwu.errors import ApplyError
from luwu.plan_record import PlanRecord, PlanRecordError
from luwu.reconcile import (
    Plan,
    ResourceObservation,
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
    reobserve_execution_record,
)
from tests.test_m3c_execution import _ExecutionProject, _snapshot


class M5InterruptionHonestyTests(unittest.TestCase):
    maxDiff = None

    def test_writer_failure_before_first_target_writer(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            before = _snapshot(project)
            failure = ApplyError(
                "writer failed before any replacement",
                code="write_failed",
                committed=False,
            )

            with (
                patch("luwu.reconcile._write_observation", side_effect=failure),
                self.assertRaises(ApplyError) as raised,
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            self.assertEqual(error.code, "execution_failed")
            self.assertFalse(error.committed)
            self.assertEqual(_snapshot(project), before)
            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "unknown")
            self.assertEqual(_resource_states(journal), ["unknown", "not-attempted"])
            self._assert_error_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=False,
                changed_targets=[],
                states=["failed", "not-attempted"],
            )
            self._assert_recovery_views(
                record_path,
                record_state="unknown",
                reobserved=["changed_or_unknown", "not-attempted"],
            )

    def test_writer_failure_after_one_committed_target(self) -> None:
        with _ExecutionProject(
            ("gamma", "beta", "alpha"),  # execution order: alpha, beta, gamma
            targets={
                "alpha": "alpha-old\n",
                "beta": "beta-old\n",
                "gamma": "gamma-old\n",
            },
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            calls = 0
            real_writer = reconcile._write_observation

            def fail_second_writer(
                current_plan: Plan,
                observation: ResourceObservation,
                *,
                execution: bool = False,
            ) -> bool:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ApplyError(
                        "writer failed before replacement",
                        code="write_failed",
                        committed=False,
                    )
                return real_writer(current_plan, observation, execution=execution)

            with (
                patch(
                    "luwu.reconcile._write_observation",
                    side_effect=fail_second_writer,
                ),
                self.assertRaises(ApplyError) as raised,
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            self.assertEqual(error.code, "recovery_required")
            self.assertTrue(error.committed)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-old\n")
            self.assertEqual(project.targets["gamma"].read_text(), "gamma-old\n")
            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "recovery_required")
            self.assertEqual(
                _resource_states(journal), ["committed", "unknown", "not-attempted"]
            )
            self._assert_error_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["committed", "failed", "not-attempted"],
            )
            self._assert_recovery_views(
                record_path,
                record_state="recovery_required",
                reobserved=["confirmed", "changed_or_unknown", "not-attempted"],
            )

    def test_failure_after_replacement_before_journal_publication(self) -> None:
        with _ExecutionProject(
            ("beta", "alpha"),  # execution order: alpha, beta
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"

            with (
                patch(
                    "luwu.reconcile.sync_directory",
                    side_effect=OSError("directory fsync failed"),
                ),
                self.assertRaises(ApplyError) as raised,
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            self.assertEqual(error.code, "recovery_required")
            self.assertTrue(error.committed)
            # The target really changed; nothing may claim a clean commit.
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-old\n")
            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "recovery_required")
            self.assertEqual(_resource_states(journal), ["unknown", "not-attempted"])
            journal_resources = cast(list[dict[str, object]], journal["resources"])
            self.assertNotEqual(journal_resources[0]["state"], "committed")
            self._assert_error_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["unknown", "not-attempted"],
            )
            self._assert_recovery_views(
                record_path,
                record_state="recovery_required",
                reobserved=["matches_postcondition", "not-attempted"],
            )

    def test_journal_publication_failure_before_any_target_write(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            before = _snapshot(project)

            def fail_publication(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                raise PlanRecordError(
                    "injected journal failure", code="plan_record_write"
                )

            with (
                patch.object(PlanRecord, "write", fail_publication),
                self.assertRaises(ApplyError) as raised,
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            self.assertEqual(error.code, "recovery_required")
            self.assertFalse(error.committed)
            self.assertEqual(_snapshot(project), before)
            self.assertFalse(record_path.exists())
            context = error.execution
            assert isinstance(context, dict)
            failed_plan_id = cast(str, context["plan_id"])
            self.assertIsInstance(failed_plan_id, str)
            self._assert_error_context(
                error,
                plan_id=failed_plan_id,
                committed=False,
                changed_targets=[],
                states=["not-attempted", "not-attempted"],
            )

    def test_journal_cas_conflict_before_any_target_write(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            competing = reconcile._execution_record(build_plan(project.manifest))
            competing.write(record_path)
            competing_bytes = record_path.read_bytes()
            competing_plan_id = str(competing.to_dict()["plan_id"])
            before = _snapshot(project)

            with self.assertRaises(ApplyError) as raised:
                execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            self.assertEqual(error.code, "recovery_required")
            self.assertFalse(error.committed)
            self.assertEqual(_snapshot(project), before)
            # The CAS guard refused the create; the competing journal survives.
            self.assertEqual(record_path.read_bytes(), competing_bytes)
            context = error.execution
            assert isinstance(context, dict)
            failed_plan_id = cast(str, context["plan_id"])
            self.assertIsInstance(failed_plan_id, str)
            self.assertNotEqual(failed_plan_id, competing_plan_id)
            self._assert_error_context(
                error,
                plan_id=failed_plan_id,
                committed=False,
                changed_targets=[],
                states=["not-attempted"],
            )
            self._assert_recovery_views(
                record_path,
                record_state="planned",
                reobserved=["changed_or_unknown"],
            )

    def test_unprocessed_noop_stays_not_attempted_after_earlier_writer_failure(
        self,
    ) -> None:
        with _ExecutionProject(
            ("beta", "alpha"),  # execution order: alpha (replace), beta (noop)
            targets={"alpha": "alpha-old\n", "beta": "beta-desired\n"},
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            before = _snapshot(project)
            failure = ApplyError(
                "writer failed before replacement",
                code="write_failed",
                committed=False,
            )

            with (
                patch("luwu.reconcile._write_observation", side_effect=failure),
                self.assertRaises(ApplyError),
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            self.assertEqual(_snapshot(project), before)
            journal = inspect_execution_record(record_path)
            self.assertEqual(_resource_states(journal), ["unknown", "not-attempted"])
            self._assert_recovery_views(
                record_path,
                record_state="unknown",
                reobserved=["changed_or_unknown", "not-attempted"],
            )

    def test_parent_rebinding_is_reported_as_concurrent_change(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            before = _snapshot(project)

            with (
                patch(
                    "luwu.reconcile.verify_directory_identity",
                    side_effect=reconcile.FileChangedError("parent rebound"),
                ),
                self.assertRaises(ApplyError) as raised,
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            self.assertEqual(error.code, "execution_failed")
            self.assertFalse(error.committed)
            cause = error.__cause__
            self.assertIsInstance(cause, ApplyError)
            assert isinstance(cause, ApplyError)
            self.assertEqual(cause.code, "concurrent_change")
            self.assertEqual(_snapshot(project), before)
            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "unknown")
            self.assertEqual(_resource_states(journal), ["unknown"])
            context = cast(dict[str, object], error.execution)
            self.assertEqual(context["changed_targets"], [])
            self.assertEqual(
                cast(list[dict[str, object]], context["resources"])[0]["state"],
                "failed",
            )

    def _assert_error_context(
        self,
        error: ApplyError,
        *,
        plan_id: str | None,
        committed: bool,
        changed_targets: list[str],
        states: list[str],
    ) -> None:
        context = error.execution
        self.assertIsInstance(context, dict)
        assert isinstance(context, dict)
        self.assertEqual(
            set(context), {"plan_id", "committed", "changed_targets", "resources"}
        )
        self.assertEqual(context["plan_id"], plan_id)
        self.assertEqual(context["committed"], committed)
        self.assertEqual(context["changed_targets"], changed_targets)
        resources = cast(list[dict[str, object]], context["resources"])
        self.assertEqual([item["state"] for item in resources], states)
        self.assertEqual(error.committed, committed)

    def _assert_recovery_views(
        self,
        record_path: Path,
        *,
        record_state: str,
        reobserved: list[str],
    ) -> None:
        """Inspection and reobserve are read-only and never upgrade an outcome."""
        before = _tree_snapshot(record_path.parent)
        journal = inspect_execution_record(record_path)
        self.assertEqual(_tree_snapshot(record_path.parent), before)
        self.assertEqual(journal["state"], record_state)
        result = reobserve_execution_record(record_path)
        self.assertEqual(_tree_snapshot(record_path.parent), before)
        self.assertEqual(result["record_state"], record_state)
        self.assertEqual(result["outcome"], "recovery_required")
        states = [
            str(item["reobserved_state"])
            for item in cast(list[dict[str, object]], result["resources"])
        ]
        self.assertEqual(states, reobserved)


def _resource_states(journal: dict[str, object]) -> list[str]:
    return [
        str(item["state"])
        for item in cast(list[dict[str, object]], journal["resources"])
    ]


def _tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory",)
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        else:
            snapshot[relative] = ("other",)
    return snapshot


if __name__ == "__main__":
    unittest.main()
