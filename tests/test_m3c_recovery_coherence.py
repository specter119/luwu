from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from luwu import reconcile
from luwu.errors import ApplyError
from luwu.reconcile import (
    Plan,
    ResourceObservation,
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
    reobserve_execution_record,
)
from tests.test_m3c_execution import _ExecutionProject


class M3cRecoveryCoherenceTests(unittest.TestCase):
    def test_content_drift_with_matching_metadata_blocks_committed_confirmation(
        self,
    ) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = self._execute(project)
            self._assert_confirmed(project, record_path)

            self._mutate_target_preserving_postcondition(project, record_path)
            before = _snapshot(project.root)
            result = reobserve_execution_record(record_path)

            self._assert_changed_result(result, "committed")
            self.assertEqual(_snapshot(project.root), before)

    def test_content_drift_with_matching_metadata_blocks_unchanged_confirmation(
        self,
    ) -> None:
        with _ExecutionProject(
            ("alpha",), targets={"alpha": "alpha-desired\n"}
        ) as project:
            record_path = self._execute(project)
            self._assert_confirmed(project, record_path)

            self._mutate_target_preserving_postcondition(project, record_path)
            before = _snapshot(project.root)
            result = reobserve_execution_record(record_path)

            self._assert_changed_result(result, "unchanged")
            self.assertEqual(_snapshot(project.root), before)

    def test_content_drift_with_matching_metadata_blocks_unknown_match(self) -> None:
        with _ExecutionProject(
            ("alpha",), targets={"alpha": "alpha-current\n"}
        ) as project:
            record_path = project.root / "journal.json"
            failure = ApplyError(
                "writer failed before replacement",
                code="apply_failed",
                committed=False,
                target_name="targets/alpha.conf",
            )
            with (
                patch("luwu.reconcile._write_observation", side_effect=failure),
                self.assertRaises(ApplyError),
            ):
                execute_execution_plan(
                    build_plan(project.manifest), record_path, confirm=True
                )

            journal = inspect_execution_record(record_path)
            resources = cast(list[dict[str, object]], journal["resources"])
            self.assertEqual(resources[0]["state"], "unknown")
            self._mutate_target_preserving_postcondition(project, record_path)
            before = _snapshot(project.root)
            result = reobserve_execution_record(record_path)

            self._assert_changed_result(result, "unknown")
            self.assertEqual(_snapshot(project.root), before)

    def test_unknown_with_matching_metadata_requires_recovery(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            failure = ApplyError(
                "writer result could not be confirmed",
                code="apply_failed",
                committed=True,
                target_name="targets/alpha.conf",
            )
            real_writer = reconcile._write_observation

            def write_then_raise(
                current_plan: Plan,
                observation: ResourceObservation,
                *,
                execution: bool = False,
            ) -> bool:
                real_writer(current_plan, observation, execution=execution)
                raise failure

            with (
                patch(
                    "luwu.reconcile._write_observation",
                    side_effect=write_then_raise,
                ),
                self.assertRaises(ApplyError) as context,
            ):
                execute_execution_plan(
                    build_plan(project.manifest), record_path, confirm=True
                )

            self.assertEqual(context.exception.code, "recovery_required")
            self.assertTrue(context.exception.committed)
            journal = inspect_execution_record(record_path)
            resources = cast(list[dict[str, object]], journal["resources"])
            self.assertEqual(journal["state"], "recovery_required")
            self.assertEqual(resources[0]["state"], "unknown")

            before = _snapshot(project.root)
            result = reobserve_execution_record(record_path)

            self.assertEqual(_snapshot(project.root), before)
            self.assertEqual(result["outcome"], "recovery_required")
            resource = cast(list[dict[str, object]], result["resources"])[0]
            self.assertEqual(resource["reobserved_state"], "matches_postcondition")
            self.assertEqual(resource["plan_status"], "in_sync")
            paths = cast(list[dict[str, object]], resource["paths"])
            self.assertTrue(all(path["matches_postcondition"] for path in paths))

    def test_in_sync_confirms_and_not_attempted_does_not_upgrade(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta", "zeta"),
            targets={
                "alpha": "alpha-old\n",
                "beta": "beta-old\n",
                "zeta": "zeta-desired\n",
            },
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            failure = ApplyError(
                "writer failed before replacement",
                code="apply_failed",
                committed=False,
                target_name="targets/beta.conf",
            )
            calls = 0
            real_writer = reconcile._write_observation

            def write_with_failure(
                current_plan: Plan,
                observation: ResourceObservation,
                *,
                execution: bool = False,
            ) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_writer(current_plan, observation, execution=execution)
                raise failure

            with (
                patch(
                    "luwu.reconcile._write_observation",
                    side_effect=write_with_failure,
                ),
                self.assertRaises(ApplyError),
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            before = _snapshot(project.root)
            result = reobserve_execution_record(record_path)
            self.assertEqual(_snapshot(project.root), before)
            resources = cast(list[dict[str, object]], result["resources"])
            by_name = {str(item["name"]): item for item in resources}
            self.assertEqual(by_name["alpha"]["reobserved_state"], "confirmed")
            self.assertEqual(by_name["alpha"]["plan_status"], "in_sync")
            self.assertEqual(by_name["zeta"]["reobserved_state"], "not-attempted")
            self.assertEqual(by_name["zeta"]["plan_status"], "in_sync")
            self.assertEqual(result["outcome"], "recovery_required")

    def _execute(self, project: _ExecutionProject) -> Path:
        record_path = project.root / "journal.json"
        execute_execution_plan(build_plan(project.manifest), record_path, confirm=True)
        return record_path

    def _assert_confirmed(self, project: _ExecutionProject, record_path: Path) -> None:
        before = _snapshot(project.root)
        result = reobserve_execution_record(record_path)
        self.assertEqual(_snapshot(project.root), before)
        self.assertEqual(result["outcome"], "confirmed")
        resource = cast(list[dict[str, object]], result["resources"])[0]
        self.assertEqual(resource["reobserved_state"], "confirmed")
        self.assertEqual(resource["plan_status"], "in_sync")

    def _assert_changed_result(
        self, result: dict[str, object], record_state: str
    ) -> None:
        self.assertEqual(result["outcome"], "recovery_required")
        resource = cast(list[dict[str, object]], result["resources"])[0]
        self.assertEqual(resource["record_state"], record_state)
        self.assertEqual(resource["reobserved_state"], "changed_or_unknown")
        self.assertEqual(resource["plan_status"], "drifted")
        paths = cast(list[dict[str, object]], resource["paths"])
        self.assertTrue(all(path["matches_postcondition"] for path in paths))

    def _mutate_target_preserving_postcondition(
        self, project: _ExecutionProject, record_path: Path
    ) -> None:
        journal = inspect_execution_record(record_path)
        resources = cast(list[dict[str, object]], journal["resources"])
        paths = cast(list[dict[str, object]], resources[0]["paths"])
        target_path = project.targets["alpha"]
        expected = cast(
            dict[str, int | str],
            next(path["postcondition"] for path in paths if path["role"] == "target"),
        )
        original = target_path.read_bytes()
        self.assertGreater(len(original), 0)
        mutated = bytes((original[0] ^ 1,)) + original[1:]
        target_stat = target_path.stat()
        target_path.write_bytes(mutated)
        os.utime(
            target_path,
            ns=(
                target_stat.st_atime_ns,
                int(expected["mtime_ns"])
                if int(expected["mtime_ns"])
                else target_stat.st_mtime_ns,
            ),
        )
        self.assertNotEqual(target_path.read_bytes(), original)


def _snapshot(root: Path) -> dict[str, tuple[object, ...]]:
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
