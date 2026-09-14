from __future__ import annotations

import fcntl
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

from luwu import reconcile
from luwu.errors import ApplyError
from luwu.plan_record import PlanRecord, PlanRecordError
from tests.test_m3c_execution import _ExecutionProject, _snapshot


class M3cExecutionOutcomeTests(unittest.TestCase):
    def test_apply_error_execution_context_is_optional(self) -> None:
        self.assertIsNone(ApplyError("failure").execution)

        context: dict[str, object] = {
            "plan_id": None,
            "committed": False,
            "changed_targets": [],
            "resources": [],
        }
        self.assertEqual(ApplyError("failure", execution=context).execution, context)

    def test_preflight_failure_has_only_not_attempted_context(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            project.sources["beta"].write_text("beta-changed-after-plan\n")
            before = _snapshot(project)
            record_path = project.root / "journal.json"

            with self.assertRaises(ApplyError) as raised:
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            self.assertEqual(error.code, "stale_plan")
            self.assertEqual(_snapshot(project), before)
            self.assertFalse(record_path.exists())
            self._assert_context(
                error,
                plan_id=None,
                committed=False,
                changed_targets=[],
                states=["not-attempted", "not-attempted"],
            )

    def test_initial_journal_failure_before_publication_has_context(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            before = _snapshot(project)
            record_path = project.root / "journal.json"

            def fail_write(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                raise PlanRecordError(
                    "injected journal failure", code="plan_record_write"
                )

            with (
                patch.object(PlanRecord, "write", fail_write),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            self.assertEqual(error.code, "recovery_required")
            self.assertEqual(_snapshot(project), before)
            self.assertFalse(record_path.exists())
            context = error.execution
            assert isinstance(context, dict)
            plan_id = context["plan_id"]
            assert isinstance(plan_id, str)
            self._assert_context(
                error,
                plan_id=plan_id,
                committed=False,
                changed_targets=[],
                states=["not-attempted", "not-attempted"],
            )
            self.assertIsInstance(context["plan_id"], str)

    def test_published_initial_journal_failure_does_not_claim_target_commit(
        self,
    ) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def publish_then_fail(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                if self.to_dict()["state"] == "planned":
                    real_write(self, path, expected=expected)
                    raise PlanRecordError(
                        "journal publication could not be confirmed",
                        code="plan_record_durability_unknown",
                        committed=True,
                    )
                real_write(self, path, expected=expected)

            with (
                patch.object(PlanRecord, "write", publish_then_fail),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "planned")
            self.assertFalse(error.committed)
            self.assertEqual(
                cast(dict[str, object], error.execution)["plan_id"],
                journal["plan_id"],
            )
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=False,
                changed_targets=[],
                states=["not-attempted", "not-attempted"],
            )

    def test_plan_intent_journal_failure_keeps_all_targets_unattempted(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def fail_plan_intent(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                document = self.to_dict()
                if document["state"] == "commit_intent" and all(
                    resource["state"] == "preflighted"
                    for resource in cast(list[dict[str, object]], document["resources"])
                ):
                    raise PlanRecordError(
                        "plan intent failed", code="plan_record_write"
                    )
                real_write(self, path, expected=expected)

            with (
                patch.object(PlanRecord, "write", fail_plan_intent),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "preflighted")
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=False,
                changed_targets=[],
                states=["not-attempted", "not-attempted"],
            )

    def test_preflight_journal_failures_keep_targets_unattempted(self) -> None:
        cases: tuple[tuple[str, int | None, list[str]], ...] = (
            (
                "plan",
                None,
                ["planned", "planned"],
            ),
            (
                "resource-0",
                0,
                ["planned", "planned"],
            ),
            (
                "resource-1",
                1,
                ["preflighted", "planned"],
            ),
        )
        for label, target_ordinal, expected_states in cases:
            with (
                self.subTest(failure=label),
                _ExecutionProject(
                    ("alpha", "beta"),
                    targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
                ) as project,
            ):
                plan = reconcile.build_plan(project.manifest)
                record_path = project.root / "journal.json"
                real_write = PlanRecord.write

                def fail_preflight_transition(
                    self: PlanRecord,
                    path: Path,
                    *,
                    expected: PlanRecord | None = None,
                    failure_ordinal: int | None = target_ordinal,
                    write_record: Callable[..., None] = real_write,
                ) -> None:
                    document = self.to_dict()
                    resources = cast(list[dict[str, object]], document["resources"])
                    states = [str(resource["state"]) for resource in resources]
                    should_fail = document["state"] == "preflighted" and (
                        failure_ordinal is None
                        and states == ["planned", "planned"]
                        or failure_ordinal is not None
                        and states[failure_ordinal] == "preflighted"
                        and all(
                            state == "preflighted" for state in states[:failure_ordinal]
                        )
                        and all(
                            state == "planned"
                            for state in states[failure_ordinal + 1 :]
                        )
                    )
                    if should_fail:
                        raise PlanRecordError(
                            "preflight transition failed", code="plan_record_write"
                        )
                    write_record(self, path, expected=expected)

                before = _snapshot(project)
                with (
                    patch.object(PlanRecord, "write", fail_preflight_transition),
                    self.assertRaises(ApplyError) as raised,
                ):
                    reconcile.execute_execution_plan(plan, record_path, confirm=True)

                error = raised.exception
                journal = reconcile.inspect_execution_record(record_path)
                self.assertEqual(error.code, "recovery_required")
                self.assertEqual(
                    journal["state"],
                    "planned" if target_ordinal is None else "preflighted",
                )
                self.assertEqual(
                    [
                        item["state"]
                        for item in cast(list[dict[str, object]], journal["resources"])
                    ],
                    expected_states,
                )
                self.assertEqual(_snapshot(project), before)
                self._assert_context(
                    error,
                    plan_id=str(journal["plan_id"]),
                    committed=False,
                    changed_targets=[],
                    states=["not-attempted", "not-attempted"],
                )

    def test_resource_intent_failure_preserves_prior_target_fact(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def fail_beta_intent(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                resources = cast(list[dict[str, object]], self.to_dict()["resources"])
                if resources[1]["state"] == "commit_intent":
                    raise PlanRecordError(
                        "resource intent failed", code="plan_record_write"
                    )
                real_write(self, path, expected=expected)

            with (
                patch.object(PlanRecord, "write", fail_beta_intent),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-old\n")
            journal_resources = cast(list[dict[str, object]], journal["resources"])
            self.assertEqual(
                [(item["name"], item["state"]) for item in journal_resources],
                [("alpha", "committed"), ("beta", "preflighted")],
            )
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["committed", "not-attempted"],
            )

    def test_noop_journal_failure_keeps_completed_noop_in_context(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-desired\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def publish_noop_then_fail(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                document = self.to_dict()
                if any(
                    resource["state"] == "unchanged"
                    for resource in cast(list[dict[str, object]], document["resources"])
                ):
                    real_write(self, path, expected=expected)
                    raise PlanRecordError("noop log failed", code="plan_record_write")
                real_write(self, path, expected=expected)

            with (
                patch.object(PlanRecord, "write", publish_noop_then_fail),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-desired\n")
            journal_resources = cast(list[dict[str, object]], journal["resources"])
            self.assertEqual(
                [(item["name"], item["state"]) for item in journal_resources],
                [("alpha", "committed"), ("beta", "unchanged")],
            )
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["committed", "unchanged"],
            )

    def test_writer_failure_before_replacement_is_failed(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_replace = reconcile.os.replace

            def fail_target_replace(
                source: str,
                target: str,
                *,
                src_dir_fd: int | None = None,
                dst_dir_fd: int | None = None,
            ) -> None:
                if source.startswith(".alpha.conf.luwu-"):
                    raise OSError("target replacement failed")
                real_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                patch("luwu.reconcile.os.replace", side_effect=fail_target_replace),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(error.code, "execution_failed")
            self.assertFalse(error.committed)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-old\n")
            journal_resources = cast(list[dict[str, object]], journal["resources"])
            self.assertEqual(
                [(item["name"], item["state"]) for item in journal_resources],
                [("alpha", "unknown"), ("beta", "not-attempted")],
            )
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=False,
                changed_targets=[],
                states=["failed", "not-attempted"],
            )

    def test_writer_failure_after_replacement_is_unknown_and_changed(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"

            with (
                patch(
                    "luwu.reconcile.sync_directory",
                    side_effect=OSError("directory sync failed"),
                ),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(error.code, "recovery_required")
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["unknown", "not-attempted"],
            )

    def test_target_postcondition_failure_is_unknown_and_changed(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"

            with (
                patch("luwu.reconcile._condition_matches_expected", return_value=False),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(journal["state"], "recovery_required")
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["unknown", "not-attempted"],
            )

    def test_unknown_writer_failure_marking_keeps_all_changed_targets(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_sync = reconcile.sync_directory
            real_write = PlanRecord.write
            sync_calls = 0

            def fail_beta_sync(parent_descriptor: int) -> None:
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls == 2:
                    raise OSError("beta directory sync failed")
                real_sync(parent_descriptor)

            def fail_failure_marking(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                resources = cast(list[dict[str, object]], self.to_dict()["resources"])
                if any(resource["state"] == "unknown" for resource in resources):
                    raise PlanRecordError(
                        "failure marking failed", code="plan_record_write"
                    )
                real_write(self, path, expected=expected)

            with (
                patch("luwu.reconcile.sync_directory", fail_beta_sync),
                patch.object(PlanRecord, "write", fail_failure_marking),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(error.code, "recovery_required")
            self.assertTrue(error.committed)
            self.assertEqual(journal["state"], "commit_intent")
            self.assertEqual(
                [
                    item["state"]
                    for item in cast(list[dict[str, object]], journal["resources"])
                ],
                ["committed", "commit_intent"],
            )
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-desired\n")
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf", "targets/beta.conf"],
                states=["committed", "unknown"],
            )

    def test_resource_commit_log_failure_does_not_downgrade_committed_context(
        self,
    ) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def fail_committed_log(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                if any(
                    resource["state"] == "committed"
                    for resource in cast(
                        list[dict[str, object]], self.to_dict()["resources"]
                    )
                ):
                    raise PlanRecordError("resource commit log failed", committed=True)
                real_write(self, path, expected=expected)

            with (
                patch.object(PlanRecord, "write", fail_committed_log),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(journal["state"], "recovery_required")
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["committed"],
            )

    def test_failure_marking_publish_failures_preserve_prior_target_fact(self) -> None:
        expected_journal_states = {
            1: ["committed", "commit_intent", "preflighted"],
            2: ["committed", "unknown", "preflighted"],
            3: ["committed", "unknown", "not-attempted"],
        }
        for failure_step in (1, 2, 3):
            with (
                self.subTest(failure_step=failure_step),
                _ExecutionProject(
                    ("alpha", "beta", "gamma"),
                    targets={
                        "alpha": "alpha-old\n",
                        "beta": "beta-old\n",
                        "gamma": "gamma-old\n",
                    },
                ) as project,
            ):
                plan = reconcile.build_plan(project.manifest)
                record_path = project.root / "journal.json"
                real_writer = reconcile._write_observation
                real_write = PlanRecord.write
                marking_attempts = 0

                def fail_beta(
                    current_plan: reconcile.Plan,
                    observation: reconcile.ResourceObservation,
                    *,
                    execution: bool = False,
                    write_observation: Callable[..., bool] = real_writer,
                ) -> bool:
                    if observation.resource.name == "beta":
                        raise ApplyError(
                            "writer failed before replacement",
                            code="write_failed",
                            committed=False,
                        )
                    return write_observation(
                        current_plan, observation, execution=execution
                    )

                def fail_marking_step(
                    self: PlanRecord,
                    path: Path,
                    *,
                    expected: PlanRecord | None = None,
                    failure_number: int = failure_step,
                    write_record: Callable[..., None] = real_write,
                ) -> None:
                    nonlocal marking_attempts
                    document = self.to_dict()
                    resources = cast(list[dict[str, object]], document["resources"])
                    is_marking = any(
                        resource["state"] == "unknown" for resource in resources
                    ) or document["state"] in {"unknown", "recovery_required"}
                    if is_marking:
                        marking_attempts += 1
                        if marking_attempts == failure_number:
                            raise PlanRecordError(
                                "failure marking failed", code="plan_record_write"
                            )
                    write_record(self, path, expected=expected)

                with (
                    patch("luwu.reconcile._write_observation", side_effect=fail_beta),
                    patch.object(PlanRecord, "write", fail_marking_step),
                    self.assertRaises(ApplyError) as raised,
                ):
                    reconcile.execute_execution_plan(plan, record_path, confirm=True)

                error = raised.exception
                journal = reconcile.inspect_execution_record(record_path)
                journal_resources = cast(list[dict[str, object]], journal["resources"])
                self.assertEqual(
                    [item["state"] for item in journal_resources],
                    expected_journal_states[failure_step],
                )
                self.assertEqual(
                    project.targets["alpha"].read_text(), "alpha-desired\n"
                )
                self.assertEqual(project.targets["beta"].read_text(), "beta-old\n")
                self._assert_context(
                    error,
                    plan_id=str(journal["plan_id"]),
                    committed=True,
                    changed_targets=["targets/alpha.conf"],
                    states=["committed", "failed", "not-attempted"],
                )

    def test_final_plan_commit_failure_preserves_all_noop_context(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-desired\n", "beta": "beta-desired\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def fail_final_noop(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                if self.to_dict()["state"] == "committed":
                    raise PlanRecordError("final plan log failed", committed=True)
                real_write(self, path, expected=expected)

            with (
                patch.object(PlanRecord, "write", fail_final_noop),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "commit_intent")
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=False,
                changed_targets=[],
                states=["unchanged", "unchanged"],
            )

    def test_final_plan_commit_failure_preserves_prior_target_context(self) -> None:
        with _ExecutionProject(
            ("alpha", "beta"),
            targets={"alpha": "alpha-old\n", "beta": "beta-desired\n"},
        ) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write

            def fail_final_plan(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                if self.to_dict()["state"] == "committed":
                    raise PlanRecordError("final plan log failed", committed=True)
                real_write(self, path, expected=expected)

            with (
                patch.object(PlanRecord, "write", fail_final_plan),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["committed", "unchanged"],
            )

    def test_raw_journal_cleanup_error_keeps_prior_target_context(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            plan = reconcile.build_plan(project.manifest)
            record_path = project.root / "journal.json"
            real_write = PlanRecord.write
            real_flock = fcntl.flock

            def fail_unlock(
                self: PlanRecord,
                path: Path,
                *,
                expected: PlanRecord | None = None,
            ) -> None:
                if self.to_dict()["state"] == "committed":

                    def broken_flock(descriptor: int, operation: int) -> None:
                        if operation == fcntl.LOCK_UN:
                            raise OSError("journal unlock failed")
                        real_flock(descriptor, operation)

                    with patch(
                        "luwu.plan_record.fcntl.flock", side_effect=broken_flock
                    ):
                        return real_write(self, path, expected=expected)
                real_write(self, path, expected=expected)

            with (
                patch.object(PlanRecord, "write", fail_unlock),
                self.assertRaises(ApplyError) as raised,
            ):
                reconcile.execute_execution_plan(plan, record_path, confirm=True)

            error = raised.exception
            journal = reconcile.inspect_execution_record(record_path)
            self.assertNotIn("journal unlock failed", str(error))
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self._assert_context(
                error,
                plan_id=str(journal["plan_id"]),
                committed=True,
                changed_targets=["targets/alpha.conf"],
                states=["committed"],
            )

    def _assert_context(
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
        self.assertNotIn("state", context)
        self.assertEqual(context["plan_id"], plan_id)
        self.assertEqual(context["committed"], committed)
        self.assertEqual(context["changed_targets"], changed_targets)
        resources = cast(list[dict[str, object]], context["resources"])
        self.assertEqual([item["state"] for item in resources], states)
        self.assertTrue(
            all(set(item) == {"name", "target", "state"} for item in resources)
        )
        self.assertEqual(error.committed, committed)


if __name__ == "__main__":
    unittest.main()
