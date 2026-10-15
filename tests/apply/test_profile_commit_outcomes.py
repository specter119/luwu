"""Tests for profile commit outcomes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from luwu import m7_engine
from luwu.m7_engine import (
    adopt_resource,
    apply_plan,
)
from luwu.m7_filesystem import FileObservation
from tests.support import profile_case

_CheckInputs = Callable[[], None] | None


class ProfileCommitOutcomeTests(profile_case.ProfileEngineCase):
    def test_observation_failure_after_successful_write_preserves_commit(self) -> None:
        self._accept_template_baseline()
        plan = self._plan()
        original_write = m7_engine.write_file
        original_observe = m7_engine.observe_file
        target = self._target()
        write_returned = False

        def write_then_arm(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            nonlocal write_returned
            identity = original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )
            if path == target:
                write_returned = True
            return identity

        def observe_after_write(root: Path, path: Path):
            if write_returned and path == target:
                raise m7_engine.M7FilesystemError(
                    "private injected failure", code="m7_observe_failed"
                )
            return original_observe(root, path)

        with (
            patch("luwu.m7_engine.write_file", side_effect=write_then_arm),
            patch("luwu.m7_engine.observe_file", side_effect=observe_after_write),
        ):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed_but_verification_failed")
        self.assertEqual(result.error_code, "m7_observe_failed")
        target_action = next(
            action for action in result.actions if action.kind == "target_write"
        )
        self.assertEqual(target_action.state, "committed")
        self.assertIn('"theme":"overlay-theme-v1"', target.read_text(encoding="utf-8"))

    def test_writer_errors_distinguish_not_replaced_and_committed(self) -> None:
        self._accept_template_baseline()
        target = self._target()
        target.parent.mkdir(parents=True, exist_ok=True)
        with patch(
            "luwu.m7_engine.write_file",
            side_effect=m7_engine.M7FilesystemError(
                "private injected failure",
                code="m7_write_failed",
                state="not_replaced",
                committed=False,
            ),
        ):
            not_replaced = apply_plan(self._plan(), confirm=True)
        not_replaced_action = next(
            action for action in not_replaced.actions if action.kind == "target_write"
        )
        self.assertEqual(not_replaced.outcome, "not_replaced")
        self.assertEqual(not_replaced_action.state, "not_replaced")
        self.assertFalse(target.exists())

        original_write = m7_engine.write_file

        def write_then_raise(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )
            if path == target:
                raise m7_engine.M7FilesystemError(
                    "private injected failure",
                    code="m7_write_unknown",
                    state="uncertain",
                    committed=True,
                )
            return identity

        with patch("luwu.m7_engine.write_file", side_effect=write_then_raise):
            committed = apply_plan(self._plan(), confirm=True)
        committed_action = next(
            action for action in committed.actions if action.kind == "target_write"
        )
        self.assertEqual(committed.outcome, "committed_state_unknown")
        self.assertEqual(committed_action.state, "committed")
        self.assertIn('"theme":"overlay-theme-v1"', target.read_text(encoding="utf-8"))

    def test_middle_writer_not_replaced_preserves_partial_batch_state(self) -> None:
        for name in ("first", "second"):
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v1\n", encoding="utf-8"
            )
            (self.home / f"{name}.txt").write_text(
                f"live-{name}-v1\n", encoding="utf-8"
            )
        self.manifest_path.write_text(
            """
version = 8
[roots]
source = "source"
inputs = "inputs"
target = "home"
[profiles.work]
packages = ["notes"]
exclude = []
[packages.notes]
dependencies = []
resources = ["first", "second"]
[inputs]
[resources.first]
kind = "copy"
source = "first.txt"
target = "first.txt"
baseline = "baselines/first.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
[resources.second]
kind = "copy"
source = "second.txt"
target = "second.txt"
baseline = "baselines/second.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
""",
            encoding="utf-8",
        )
        for name in ("first", "second"):
            self.assertEqual(
                adopt_resource(self._plan(), resource=name, confirm=True).outcome,
                "committed",
            )
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v2\n", encoding="utf-8"
            )

        original_write = m7_engine.write_file
        second_target = self.home / "second.txt"

        def fail_second_write(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            if path == second_target:
                raise m7_engine.M7FilesystemError(
                    "private injected failure",
                    code="m7_write_failed",
                    state="not_replaced",
                    committed=False,
                )
            return original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )

        with patch("luwu.m7_engine.write_file", side_effect=fail_second_write):
            result = apply_plan(self._plan(), confirm=True)

        states = {
            (action.resource, action.kind): action.state for action in result.actions
        }
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(states[("first", "target_write")], "committed")
        self.assertEqual(states[("second", "target_write")], "not_replaced")
        self.assertEqual((self.home / "first.txt").read_text(), "source-first-v2\n")
        self.assertEqual(second_target.read_text(), "live-second-v1\n")
