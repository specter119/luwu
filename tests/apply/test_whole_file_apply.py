"""Tests for whole file apply."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Self
from unittest.mock import patch

from luwu.errors import ApplyError
from luwu.manifest import load_manifest
from luwu.reconcile import Action, ApplyOutcome, Status, apply_plan, build_plan


class _Project:
    def __init__(self, *, target: str = "live/settings.conf") -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "templates/settings.conf.j2"
        self.target = self.root / target
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_text('profile = "{{ profile }}"\n', encoding="utf-8")
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text(
            f'''version = 1

[resources.settings]
source = "templates/settings.conf.j2"
target = "{target}"
owner = "source"
scope = "whole-file"
variables_sensitivity = "public"

[resources.settings.variables]
profile = "developer"
''',
            encoding="utf-8",
        )
        self.manifest = load_manifest(self.manifest_path)
        self.desired = b'profile = "developer"\n'

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()


class WholeFileApplyV1Tests(unittest.TestCase):
    def test_missing_target_is_planned_without_writing(self) -> None:
        with _Project() as project:
            before = project.source.read_bytes()

            plan = build_plan(project.manifest)

            observation = plan.observations[0]
            self.assertEqual(observation.status, Status.MISSING)
            self.assertEqual(observation.action, Action.CREATE)
            self.assertFalse(project.target.exists())
            self.assertEqual(project.source.read_bytes(), before)

    def test_plan_compares_rendered_template_and_recognizes_exact_sync(self) -> None:
        with _Project() as project:
            project.target.write_bytes(project.desired)

            plan = build_plan(project.manifest)

            self.assertEqual(plan.observations[0].status, Status.IN_SYNC)
            self.assertEqual(plan.observations[0].action, Action.NOOP)

    def test_apply_reports_a_clean_noop_without_claiming_a_write(self) -> None:
        with _Project() as project:
            project.target.write_bytes(project.desired)

            result = apply_plan(build_plan(project.manifest))

            self.assertEqual(result.outcome, ApplyOutcome.NO_CHANGES)
            self.assertEqual(result.changed_targets, ())

    def test_whitespace_difference_is_replaceable_without_format_evidence(self) -> None:
        with _Project() as project:
            project.target.write_bytes(b'profile = "developer"  \r\n')

            plan = build_plan(project.manifest)
            result = apply_plan(plan)

            self.assertEqual(plan.observations[0].status, Status.DRIFTED)
            self.assertEqual(plan.observations[0].action, Action.REPLACE)
            self.assertEqual(result.outcome, ApplyOutcome.COMMITTED)
            self.assertEqual(result.changed_targets, ("live/settings.conf",))
            self.assertEqual(project.target.read_bytes(), project.desired)

    def test_committed_write_with_failed_verification_reports_changed_target(
        self,
    ) -> None:
        with _Project() as project:
            plan = build_plan(project.manifest)

            with patch(
                "luwu.reconcile._load_current_manifest",
                side_effect=ApplyError(
                    "verification unavailable",
                    code="post_apply_verification_failed",
                ),
            ):
                result = apply_plan(plan)

            self.assertEqual(
                result.outcome,
                ApplyOutcome.COMMITTED_BUT_VERIFICATION_FAILED,
            )
            self.assertEqual(result.changed_targets, ("live/settings.conf",))
            self.assertIsNone(result.verification_plan)
            self.assertEqual(
                result.verification_error, "post_apply_verification_failed"
            )
            self.assertEqual(project.target.read_bytes(), project.desired)

    def test_committed_write_with_unconfirmed_durability_reports_unknown_state(
        self,
    ) -> None:
        with _Project() as project:
            plan = build_plan(project.manifest)

            with patch(
                "luwu.reconcile.sync_directory",
                side_effect=OSError("directory fsync unavailable"),
            ):
                result = apply_plan(plan)

            self.assertEqual(result.outcome, ApplyOutcome.COMMITTED_STATE_UNKNOWN)
            self.assertEqual(result.changed_targets, ("live/settings.conf",))
            self.assertEqual(result.verification_error, "durability_unconfirmed")
            self.assertEqual(project.target.read_bytes(), project.desired)

    def test_committed_write_with_failed_directory_cleanup_reports_unknown_state(
        self,
    ) -> None:
        with _Project() as project:
            plan = build_plan(project.manifest)

            with patch(
                "luwu.reconcile.unlock_directory",
                side_effect=OSError("unlock unavailable"),
            ):
                result = apply_plan(plan)

            self.assertEqual(result.outcome, ApplyOutcome.COMMITTED_STATE_UNKNOWN)
            self.assertEqual(result.changed_targets, ("live/settings.conf",))
            self.assertEqual(result.verification_error, "cleanup_failed")
            self.assertEqual(project.target.read_bytes(), project.desired)

    def test_semantic_drift_is_replaceable(self) -> None:
        with _Project() as project:
            project.target.write_text('profile = "other"\n', encoding="utf-8")

            plan = build_plan(project.manifest)

            self.assertEqual(plan.observations[0].status, Status.DRIFTED)
            self.assertEqual(plan.observations[0].action, Action.REPLACE)

    def test_target_symlink_is_blocked_without_following_it(self) -> None:
        with _Project() as project:
            outside = project.root.parent / "outside-settings.conf"
            outside.write_text("keep me\n", encoding="utf-8")
            project.target.symlink_to(outside)

            plan = build_plan(project.manifest)

            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            self.assertEqual(plan.observations[0].action, Action.BLOCK)
            self.assertIn("symlink", plan.observations[0].reason)
            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)
            self.assertEqual(context.exception.code, "plan_blocked")
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep me\n")

    def test_target_symlink_loop_is_reported_as_blocked(self) -> None:
        with _Project() as project:
            project.target.symlink_to(project.target)

            plan = build_plan(project.manifest)

            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            self.assertEqual(plan.observations[0].action, Action.BLOCK)

    def test_missing_target_parent_is_blocked(self) -> None:
        with _Project(target="missing/settings.conf") as project:
            project.target.parent.rmdir()

            plan = build_plan(project.manifest)

            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            self.assertIn("parent does not exist", plan.observations[0].reason)

    def test_target_parent_symlink_is_blocked(self) -> None:
        with _Project() as project:
            outside = project.root.parent / f"{project.root.name}-outside-target"
            outside.mkdir()
            project.target.parent.rmdir()
            project.target.parent.symlink_to(outside, target_is_directory=True)

            plan = build_plan(project.manifest)

            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            self.assertIn("parent contains a symlink", plan.observations[0].reason)
            self.assertFalse((outside / "settings.conf").exists())
            outside.rmdir()

    def test_existing_target_permissions_are_preserved(self) -> None:
        with _Project() as project:
            project.target.write_text('profile = "other"\n', encoding="utf-8")
            project.target.chmod(0o600)
            plan = build_plan(project.manifest)

            apply_plan(plan)

            self.assertEqual(project.target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(project.target.read_bytes(), project.desired)

    def test_file_permissions_are_in_place_when_contents_are_synced(self) -> None:
        with _Project() as project:
            project.target.write_bytes(b"old target\n")
            project.target.chmod(0o640)
            plan = build_plan(project.manifest)
            synced_modes = []
            original_fsync = os.fsync

            def record_sync(descriptor: int) -> None:
                info = os.fstat(descriptor)
                if stat.S_ISREG(info.st_mode):
                    synced_modes.append(stat.S_IMODE(info.st_mode))
                original_fsync(descriptor)

            with patch("luwu.reconcile.os.fsync", side_effect=record_sync):
                result = apply_plan(plan)

            self.assertEqual(result.outcome, ApplyOutcome.COMMITTED)
            self.assertEqual(synced_modes, [0o640])

    def test_precommit_permission_or_sync_failure_preserves_old_target(self) -> None:
        for operation in ("fchmod", "fsync"):
            with self.subTest(operation=operation), _Project() as project:
                project.target.write_bytes(b"old target\n")
                project.target.chmod(0o640)
                before = project.target.stat()
                plan = build_plan(project.manifest)

                with (
                    patch(
                        f"luwu.reconcile.os.{operation}", side_effect=OSError("failure")
                    ),
                    self.assertRaises(ApplyError) as context,
                ):
                    apply_plan(plan)

                self.assertFalse(context.exception.committed)
                self.assertEqual(project.target.read_bytes(), b"old target\n")
                self.assertEqual(project.target.stat().st_ino, before.st_ino)
                self.assertEqual(stat.S_IMODE(project.target.stat().st_mode), 0o640)
                self.assertEqual(list(project.target.parent.glob("*.luwu-*")), [])
