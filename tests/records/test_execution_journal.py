"""Tests for execution journal."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from luwu import reconcile
from luwu.errors import ApplyError
from luwu.plan_record import PlanRecord
from luwu.reconcile import (
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
    reobserve_execution_record,
)
from tests.support.projects import ExecutionProject
from tests.support.snapshots import project_bytes


def _json_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key).casefold() for key in value} | {
            nested for child in value.values() for nested in _json_keys(child)
        }
    if isinstance(value, list):
        return {nested for child in value for nested in _json_keys(child)}
    return set()


class ExecutionJournalV5Tests(unittest.TestCase):
    def test_record_path_cannot_overlap_a_declared_target(self) -> None:
        with ExecutionProject(
            ("alpha", "zeta"),
            targets={"alpha": "alpha-old\n", "zeta": "zeta-old\n"},
        ) as project:
            before = project_bytes(project)
            with self.assertRaises(ApplyError) as context:
                execute_execution_plan(
                    build_plan(project.manifest), project.root / "targets", confirm=True
                )

            self.assertEqual(context.exception.code, "record_path_conflict")
            self.assertEqual(project_bytes(project), before)

    def test_committed_journal_contains_actual_target_postcondition(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            execute_execution_plan(
                build_plan(project.manifest), record_path, confirm=True
            )

            journal = inspect_execution_record(record_path)
            resource = cast(list[dict[str, object]], journal["resources"])[0]
            target = next(
                path
                for path in cast(list[dict[str, object]], resource["paths"])
                if path["role"] == "target"
            )
            postcondition = cast(dict[str, int], target["postcondition"])
            self.assertGreater(postcondition["mtime_ns"], 0)
            self.assertGreater(postcondition["file_id"], 0)

    def test_journal_commit_failure_leaves_recovery_boundary(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = reconcile._write_execution_record

            def fail_committed_record(
                record: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                resources = cast(dict[str, object], record.to_dict())["resources"]
                if any(
                    item["state"] == "committed"
                    for item in cast(list[dict[str, object]], resources)
                ):
                    raise ApplyError(
                        "journal commit could not be persisted",
                        code="recovery_required",
                        committed=True,
                    )
                real_write(record, path, expected=expected)

            with (
                patch(
                    "luwu.reconcile._write_execution_record",
                    side_effect=fail_committed_record,
                ),
                self.assertRaises(ApplyError) as context,
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            self.assertEqual(context.exception.code, "recovery_required")
            self.assertTrue(context.exception.committed)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(
                inspect_execution_record(record_path)["state"], "recovery_required"
            )

    def test_reobserve_rejects_a_rebound_parent_without_following_it(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            execute_execution_plan(
                build_plan(project.manifest), record_path, confirm=True
            )
            old_targets = project.root / "targets-old"
            project.targets["alpha"].parent.rename(old_targets)
            outside = project.root / "outside"
            outside.mkdir()
            (outside / "alpha.conf").write_text("alpha-desired\n")
            (project.root / "targets").symlink_to(outside, target_is_directory=True)

            result = reobserve_execution_record(record_path)

            self.assertEqual(result["outcome"], "recovery_required")
            alpha = cast(list[dict[str, object]], result["resources"])[0]
            target = next(
                path
                for path in cast(list[dict[str, object]], alpha["paths"])
                if path["role"] == "target"
            )
            self.assertEqual(target["current_type"], "unsafe")

    def test_journal_is_metadata_only_and_inspection_is_read_only(self) -> None:
        with ExecutionProject(
            ("alpha", "zeta"),
            targets={"alpha": None, "zeta": "zeta-desired\n"},
        ) as project:
            record_path = project.root / "journal.json"
            execute_execution_plan(
                build_plan(project.manifest), record_path, confirm=True
            )
            before = record_path.read_bytes()

            inspected = inspect_execution_record(record_path)
            after = record_path.read_bytes()
            raw = after.decode("utf-8").casefold()

            self.assertEqual(after, before)
            self.assertEqual(inspected, json.loads(raw))
            forbidden = {
                "values",
                "bytes",
                "rendered",
                "diff",
                "patch",
                "provider",
                "secret",
            }
            self.assertTrue(forbidden.isdisjoint(_json_keys(inspected)))
            for token in forbidden:
                self.assertNotIn(token, raw)
            for value in project.source_values.values():
                self.assertNotIn(value.casefold(), raw)

    def test_reobserve_rejects_journal_paths_not_bound_to_manifest(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            execute_execution_plan(
                build_plan(project.manifest), record_path, confirm=True
            )
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            for resource in payload["resources"]:
                for path in resource["paths"]:
                    if path["role"] == "target":
                        path["path"] = "unrelated.txt"
            for event in payload["events"]:
                if event["scope"] == "path" and event["path"] == "targets/alpha.conf":
                    event["path"] = "unrelated.txt"
            record_path.write_text(json.dumps(payload), encoding="utf-8")

            result = reobserve_execution_record(record_path)

            self.assertEqual(result["outcome"], "recovery_required")
            self.assertEqual(result["reason"], "record_manifest_mismatch")
            self.assertEqual(result["resources"], [])
