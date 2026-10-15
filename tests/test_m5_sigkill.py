from __future__ import annotations

import os
import signal
import unittest
from contextlib import ExitStack
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
from typing import cast
from unittest.mock import patch

from luwu import reconcile
from luwu.manifest import load_manifest
from luwu.reconcile import (
    Plan,
    ResourceObservation,
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
)
from tests.support.projects import ExecutionProject


def _child_execute(root: str, mode: str, ready: Connection) -> None:
    try:
        project_root = Path(root)
        plan = build_plan(load_manifest(project_root / "luwu.toml"))
        record_path = project_root / "journal.json"
        real_writer = reconcile._write_observation

        def block() -> None:
            ready.send("ready")
            ready.close()
            signal.pause()

        with ExitStack() as patches:
            if mode == "pre-journal":

                def before_journal(plan: Plan) -> None:
                    del plan
                    block()

                patches.enter_context(
                    patch.object(
                        reconcile,
                        "_preflight_execution_manifest",
                        before_journal,
                    )
                )
            elif mode == "before-replace":

                def before_replace(
                    plan: Plan,
                    observation: ResourceObservation,
                    *,
                    execution: bool = False,
                ) -> bool:
                    del plan, observation, execution
                    block()
                    return False

                patches.enter_context(
                    patch.object(reconcile, "_write_observation", before_replace)
                )
            elif mode == "after-replace":

                def after_replace(
                    plan: Plan,
                    observation: ResourceObservation,
                    *,
                    execution: bool = False,
                ) -> bool:
                    committed = real_writer(plan, observation, execution=execution)
                    block()
                    return committed

                patches.enter_context(
                    patch.object(reconcile, "_write_observation", after_replace)
                )
            elif mode == "partial":
                calls = 0

                def partial(
                    plan: Plan,
                    observation: ResourceObservation,
                    *,
                    execution: bool = False,
                ) -> bool:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        block()
                        return False
                    return real_writer(plan, observation, execution=execution)

                patches.enter_context(
                    patch.object(reconcile, "_write_observation", partial)
                )
            else:
                raise AssertionError(f"unknown mode: {mode}")

            execute_execution_plan(plan, record_path, confirm=True)
    except BaseException as exc:
        try:
            ready.send((type(exc).__name__, str(exc)))
        except (BrokenPipeError, OSError):
            pass
        raise


class M5SigkillTests(unittest.TestCase):
    def _kill_at_boundary(self, project: ExecutionProject, mode: str) -> None:
        context = get_context("fork")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_child_execute,
            args=(str(project.root), mode, sender),
        )
        process.start()
        sender.close()
        try:
            self.assertTrue(
                receiver.poll(10),
                f"child did not reach {mode} boundary; exitcode={process.exitcode}",
            )
            message = receiver.recv()
            self.assertEqual(message, "ready")
            pid = process.pid
            if pid is None:
                raise AssertionError("child process has no pid")
            os.kill(pid, signal.SIGKILL)
            process.join(10)
            self.assertEqual(process.exitcode, -signal.SIGKILL)
        finally:
            if process.is_alive():
                pid = process.pid
                if pid is not None:
                    os.kill(pid, signal.SIGKILL)
                process.join(10)
            receiver.close()

    def test_sigkill_before_journal_leaves_no_mutation(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            before = project.targets["alpha"].read_bytes()
            self._kill_at_boundary(project, "pre-journal")

            self.assertEqual(project.targets["alpha"].read_bytes(), before)
            self.assertFalse((project.root / "journal.json").exists())

    def test_sigkill_before_replace_keeps_journal_and_target_unmodified(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            self._kill_at_boundary(project, "before-replace")

            journal = inspect_execution_record(project.root / "journal.json")
            self.assertNotEqual(journal["state"], "committed")
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-old\n")

    def test_sigkill_after_replace_reports_uncommitted_journal_state(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            self._kill_at_boundary(project, "after-replace")

            journal = inspect_execution_record(project.root / "journal.json")
            resources = cast(list[dict[str, object]], journal["resources"])
            self.assertNotEqual(journal["state"], "committed")
            self.assertNotEqual(resources[0]["state"], "committed")
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")

    def test_sigkill_between_resources_preserves_partial_commit_boundary(self) -> None:
        with ExecutionProject(
            ("beta", "alpha"),
            targets={"alpha": "alpha-old\n", "beta": "beta-old\n"},
        ) as project:
            self._kill_at_boundary(project, "partial")

            journal = inspect_execution_record(project.root / "journal.json")
            resources = {
                str(item["name"]): item["state"]
                for item in cast(list[dict[str, object]], journal["resources"])
            }
            self.assertNotEqual(journal["state"], "committed")
            self.assertEqual(resources["alpha"], "committed")
            self.assertNotEqual(resources["beta"], "committed")
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-old\n")


if __name__ == "__main__":
    unittest.main()
