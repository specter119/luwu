"""Tests for profile parent and mode."""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.engine import (
    accept_baseline,
    adopt_resource,
    apply_plan,
)
from luwu.filesystem import FileObservation
from tests.support import profile_case

_CheckInputs = Callable[[], None] | None


class ProfileParentAndModeTests(profile_case.ProfileEngineCase):
    def test_external_mode_change_to_created_parent_is_not_waived(self) -> None:
        self._accept_template_baseline()
        original_create = engine_module.create_parent

        def create_then_change_mode(
            root: Path,
            path: Path,
            mode: int,
            *,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_create(root, path, mode, check_inputs=check_inputs)
            if path == self.home / ".config":
                path.chmod(0o600)
            return identity

        with patch("luwu.engine.create_parent", side_effect=create_then_change_mode):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.error_code, "parent_create_unknown")
        states = {(action.kind, action.path): action.state for action in result.actions}
        self.assertEqual(states[("mkdir", ".config")], "uncertain")
        self.assertEqual(states[("mkdir", ".config/app")], "not_attempted")
        self.assertEqual(
            states[("target_write", ".config/app/settings.json")], "not_attempted"
        )
        self.assertEqual(stat.S_IMODE((self.home / ".config").stat().st_mode), 0o600)

    def test_created_parent_identity_is_not_replaced_by_a_matching_foreign_dir(
        self,
    ) -> None:
        self._accept_template_baseline()
        original_create = engine_module.create_parent
        first_parent = self.home / ".config"

        def create_then_replace(
            root: Path,
            path: Path,
            mode: int,
            *,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_create(root, path, mode, check_inputs=check_inputs)
            if path == first_parent:
                moved = self.home / ".config-original"
                path.rename(moved)
                path.mkdir(mode=mode)
                path.chmod(mode)
            return identity

        with patch("luwu.engine.create_parent", side_effect=create_then_replace):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.error_code, "parent_create_unknown")
        states = {(action.kind, action.path): action.state for action in result.actions}
        self.assertEqual(states[("mkdir", ".config")], "uncertain")
        self.assertEqual(states[("mkdir", ".config/app")], "not_attempted")
        self.assertEqual(
            states[("target_write", ".config/app/settings.json")], "not_attempted"
        )
        self.assertTrue((self.home / ".config-original").is_dir())

    def test_successful_write_does_not_adopt_a_foreign_mode_change(self) -> None:
        self._accept_template_baseline()
        plan = self._plan()
        original_write = engine_module.write_file
        target = self._target()

        def write_then_change_mode(
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
                path.chmod(0o644)
            return identity

        with patch("luwu.engine.write_file", side_effect=write_then_change_mode):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.error_code, "write_unknown")
        target_action = next(
            action for action in result.actions if action.kind == "target_write"
        )
        self.assertEqual(target_action.state, "committed")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_apply_blocks_conflicting_shared_parent_modes_before_writes(self) -> None:
        for name in ("first", "second"):
            (self.source / f"{name}.txt").write_text(
                f"{name}-source\n", encoding="utf-8"
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
target = "nested/first.txt"
baseline = "baselines/first.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
[resources.second]
kind = "copy"
source = "second.txt"
target = "nested/second.txt"
baseline = "baselines/second.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "create"
parent_mode = "0750"
""",
            encoding="utf-8",
        )
        for resource in ("first", "second"):
            self.assertEqual(
                adopt_resource(self._plan(), resource=resource, confirm=True).outcome,
                "committed",
            )
        before = self._snapshot()
        plan = self._plan()
        payload = plan.to_dict()
        planned_modes = {
            resource["parent_actions"]["target"][0]["mode"]
            for resource in payload["resources"]
        }
        self.assertEqual(planned_modes, {"0o0700", "0o0750"})
        result = apply_plan(plan, confirm=True)
        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "parent_mode_conflict")
        self.assertTrue(
            all(action.state == "not_attempted" for action in result.actions)
        )
        self.assertEqual(self._snapshot(), before)

    def test_apply_blocks_unsafe_missing_parent_mode_before_any_write(self) -> None:
        manifest = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(
            manifest.replace('parent_mode = "0700"', 'parent_mode = "0777"'),
            encoding="utf-8",
        )
        accepted = accept_baseline(
            self._plan(),
            resource="settings",
            fields=("theme", "brand"),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(accepted.outcome, "committed")

        before = self._snapshot()
        result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "parent_mode_unsafe")
        self.assertTrue(
            all(action.state == "not_attempted" for action in result.actions)
        )
        self.assertFalse((self.home / ".config").exists())
        self.assertEqual(self._snapshot(), before)

    def test_public_file_mode_0666_remains_allowed(self) -> None:
        (self.home / ".config" / "app").mkdir(parents=True)
        manifest = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(
            manifest.replace('mode = "0600"', 'mode = "0666"').replace(
                'parent_mode = "0700"', 'parent_mode = "0777"'
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            accept_baseline(
                self._plan(),
                resource="settings",
                fields=("theme", "brand"),
                value_from="desired",
                confirm=True,
            ).outcome,
            "committed",
        )

        result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(stat.S_IMODE(self._target().stat().st_mode), 0o666)
