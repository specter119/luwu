from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from luwu import reconcile
from luwu.cli import _execution_error_metadata, main
from luwu.errors import ApplyError
from luwu.plan_record import PlanRecord, PlanRecordError
from tests.test_cli import _LegacyCliProject
from tests.test_m3c_execution import _ExecutionProject


class M3cExecutionOutcomeCliTests(unittest.TestCase):
    def test_final_plan_publish_failure_keeps_committed_targets_in_json(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def fail_final_plan_write(
                record: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                if record.to_dict()["state"] == "committed":
                    raise PlanRecordError(
                        "final execution record was not published",
                        code="plan_record_write",
                        committed=False,
                    )
                real_write(record, path, expected=expected)

            with patch.object(PlanRecord, "write", new=fail_final_plan_write):
                status, output, _ = _run_apply(project, record_path, as_json=True)

            payload = _json(output)
            self.assertEqual(status, 2)
            self.assertEqual(payload["error"]["code"], "recovery_required")
            self.assertEqual(
                payload["execution"]["changed_targets"], ["targets/alpha.conf"]
            )
            self.assertTrue(payload["execution"]["committed"])
            self.assertEqual(
                payload["execution"]["resources"],
                [
                    {
                        "name": "alpha",
                        "target": "targets/alpha.conf",
                        "state": "committed",
                    }
                ],
            )
            self.assertEqual(payload["journal"]["state"], "commit_intent")
            self.assertTrue(payload["journal"]["created"])
            self.assertNotIn(project.source_values["alpha"], output)

    def test_initial_planned_record_publish_failure_does_not_claim_commit(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": None}) as project:
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def publish_then_fail_initial_record(
                record: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                real_write(record, path, expected=expected)
                if record.to_dict()["state"] == "planned" and expected is None:
                    raise PlanRecordError(
                        "initial execution record publication failed after write",
                        code="plan_record_durability_unknown",
                        committed=True,
                    )

            with patch.object(
                PlanRecord, "write", new=publish_then_fail_initial_record
            ):
                status, output, _ = _run_apply(project, record_path, as_json=True)

            payload = _json(output)
            self.assertEqual(status, 2)
            self.assertEqual(payload["error"]["code"], "recovery_required")
            self.assertFalse(payload["execution"]["committed"])
            self.assertEqual(payload["execution"]["changed_targets"], [])
            self.assertEqual(
                payload["execution"]["resources"],
                [
                    {
                        "name": "alpha",
                        "target": "targets/alpha.conf",
                        "state": "not-attempted",
                    }
                ],
            )
            self.assertEqual(payload["journal"]["state"], "planned")
            self.assertTrue(payload["journal"]["created"])
            self.assertFalse(project.targets["alpha"].exists())
            self.assertNotIn(project.source_values["alpha"], output)

    def test_committed_target_survives_unreadable_journal_output(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write
            faulted = False

            def corrupt_committed_record(
                record: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                nonlocal faulted
                real_write(record, path, expected=expected)
                resources = cast(list[dict[str, object]], record.to_dict()["resources"])
                if not faulted and any(
                    resource["state"] == "committed" for resource in resources
                ):
                    faulted = True
                    path.write_text("{journal is unreadable", encoding="utf-8")
                    raise PlanRecordError(
                        "committed journal became unreadable",
                        code="plan_record_durability_unknown",
                        committed=True,
                    )

            with patch.object(PlanRecord, "write", new=corrupt_committed_record):
                status, output, _ = _run_apply(project, record_path, as_json=True)

            payload = _json(output)
            self.assertEqual(status, 2)
            self.assertEqual(payload["journal"]["state"], "unreadable")
            self.assertEqual(
                payload["execution"]["changed_targets"], ["targets/alpha.conf"]
            )
            self.assertTrue(payload["execution"]["committed"])
            self.assertEqual(payload["execution"]["resources"][0]["state"], "committed")
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertNotIn(project.source_values["alpha"], output)

    def test_record_exists_diagnostic_failure_keeps_execution_context(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write
            real_exists = Path.exists
            sentinel = "record-exists-diagnostic-sentinel"

            def fail_final_plan_write(
                record: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                if record.to_dict()["state"] == "committed":
                    raise PlanRecordError(
                        "final execution record was not published",
                        code="plan_record_write",
                    )
                real_write(record, path, expected=expected)

            def fail_record_exists(path: Path) -> bool:
                if path == record_path:
                    raise OSError(sentinel)
                return real_exists(path)

            with (
                patch.object(PlanRecord, "write", new=fail_final_plan_write),
                patch.object(
                    Path, "exists", autospec=True, side_effect=fail_record_exists
                ),
            ):
                status, output, _ = _run_apply(project, record_path, as_json=True)

            payload = _json(output)
            self.assertEqual(status, 2)
            self.assertEqual(payload["error"]["code"], "recovery_required")
            self.assertEqual(payload["journal"]["state"], "unreadable")
            self.assertIsNone(payload["journal"]["created"])
            self.assertEqual(
                payload["execution"]["changed_targets"], ["targets/alpha.conf"]
            )
            self.assertTrue(payload["execution"]["committed"])
            self.assertEqual(payload["execution"]["resources"][0]["state"], "committed")
            self.assertNotIn(sentinel, output)

    def test_partial_writer_failure_reports_committed_and_unknown_targets_human(
        self,
    ) -> None:
        with _ExecutionProject(
            ("alpha", "beta", "zeta"),
            targets={
                "alpha": "alpha-old\n",
                "beta": "beta-old\n",
                "zeta": "zeta-old\n",
            },
        ) as project:
            record_path = project.root / "journal.json"
            real_writer = reconcile._write_observation
            calls = 0

            def write_then_report_unknown(
                plan: reconcile.Plan,
                observation: reconcile.ResourceObservation,
                *,
                execution: bool = False,
            ) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_writer(plan, observation, execution=execution)
                real_writer(plan, observation, execution=execution)
                raise ApplyError(
                    "writer durability is unknown",
                    code="durability_unconfirmed",
                    committed=True,
                    target_name=observation.resource.target_name,
                )

            with patch(
                "luwu.reconcile._write_observation",
                side_effect=write_then_report_unknown,
            ):
                status, _, error_output = _run_apply(
                    project, record_path, as_json=False
                )

            self.assertEqual(status, 2)
            self.assertIn("Changed targets: 2", error_output)
            self.assertIn("targets/alpha.conf", error_output)
            self.assertIn("targets/beta.conf", error_output)
            self.assertIn("state=committed", error_output)
            self.assertIn("state=unknown", error_output)
            self.assertIn("state=not-attempted", error_output)
            self.assertIn("state: recovery_required", error_output)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-desired\n")
            self.assertEqual(project.targets["zeta"].read_text(), "zeta-old\n")
            for value in project.source_values.values():
                self.assertNotIn(value, error_output)

    def test_noop_final_record_failure_has_no_changed_targets(self) -> None:
        with _ExecutionProject(
            ("alpha", "zeta"),
            targets={"alpha": "alpha-desired\n", "zeta": "zeta-desired\n"},
        ) as project:
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def fail_noop_final_record(
                record: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                if record.to_dict()["state"] == "committed":
                    raise PlanRecordError(
                        "final no-op record was not published",
                        code="plan_record_write",
                        committed=False,
                    )
                real_write(record, path, expected=expected)

            with patch.object(PlanRecord, "write", new=fail_noop_final_record):
                status, output, _ = _run_apply(project, record_path, as_json=True)

            payload = _json(output)
            self.assertEqual(status, 2)
            self.assertFalse(payload["execution"]["committed"])
            self.assertEqual(payload["execution"]["changed_targets"], [])
            self.assertEqual(
                [item["state"] for item in payload["execution"]["resources"]],
                ["unchanged", "unchanged"],
            )
            self.assertEqual(payload["journal"]["state"], "commit_intent")
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["zeta"].read_text(), "zeta-desired\n")
            self.assertNotIn("execution.state", output)
            for value in project.source_values.values():
                self.assertNotIn(value, output)

    def test_execution_error_serializer_allowlists_fixed_metadata(self) -> None:
        error = ApplyError("execution failed", code="recovery_required", committed=True)
        error.execution = {
            "plan_id": "plan-123",
            "committed": True,
            "changed_targets": ["targets/alpha.conf"],
            "resources": [
                {
                    "name": "alpha",
                    "target": "targets/alpha.conf",
                    "state": "committed",
                    "rendered": "private-rendered-value",
                    "condition": {"hash": "private-hash"},
                }
            ],
            "state": "private-execution-state",
            "exception": "private-exception-payload",
        }

        serialized = _execution_error_metadata(error)

        self.assertEqual(
            serialized,
            {
                "plan_id": "plan-123",
                "committed": True,
                "changed_targets": ["targets/alpha.conf"],
                "resources": [
                    {
                        "name": "alpha",
                        "target": "targets/alpha.conf",
                        "state": "committed",
                    }
                ],
            },
        )
        assert serialized is not None
        encoded = json.dumps(serialized)
        for forbidden in (
            "private-rendered-value",
            "private-hash",
            "private-execution-state",
            "private-exception-payload",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn("state", serialized)

    def test_legacy_v4_apply_output_has_no_execution_context(self) -> None:
        with _LegacyCliProject(4) as project:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                status = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--yes",
                        "--json",
                    ]
                )

            payload = _json(stdout.getvalue())
            self.assertEqual(status, 2)
            self.assertEqual(payload["apply_block_reason"], "m3_read_only")
            self.assertNotIn("execution", payload)


def _run_apply(
    project: _ExecutionProject,
    record_path: Path,
    *,
    as_json: bool,
) -> tuple[int, str, str]:
    arguments = [
        "apply",
        "--manifest",
        str(project.manifest_path),
        "--record",
        str(record_path),
        "--yes",
    ]
    if as_json:
        arguments.append("--json")
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = main(arguments)
    return status, stdout.getvalue(), stderr.getvalue()


def _json(output: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(output))


if __name__ == "__main__":
    unittest.main()
