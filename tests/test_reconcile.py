from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Self
from unittest.mock import patch

from luwu.errors import ApplyError, ManifestError, RenderError
from luwu.manifest import load_manifest
from luwu.reconcile import Action, ApplyOutcome, Plan, Status, apply_plan, build_plan
from luwu.rendering import render_template


class ReconcileTests(unittest.TestCase):
    def test_missing_target_is_planned_without_writing(self) -> None:
        with _Project() as project:
            before = project.source.read_bytes()

            plan = build_plan(project.manifest)

            observation = plan.observations[0]
            self.assertEqual(observation.status, Status.MISSING)
            self.assertEqual(observation.action, Action.CREATE)
            self.assertFalse(project.target.exists())
            self.assertEqual(project.source.read_bytes(), before)

    def test_reconciliation_rejects_a_manually_constructed_multi_resource_plan(
        self,
    ) -> None:
        with _Project() as project:
            multi_resource_manifest = replace(
                project.manifest,
                resources=project.manifest.resources * 2,
            )

            with self.assertRaises(ManifestError) as manifest_error:
                build_plan(multi_resource_manifest)
            self.assertEqual(manifest_error.exception.code, "resource_count")

            plan = build_plan(project.manifest)
            multi_observation_plan = replace(
                plan,
                observations=plan.observations * 2,
            )
            with self.assertRaises(ApplyError) as apply_error:
                apply_plan(multi_observation_plan)
            self.assertEqual(apply_error.exception.code, "resource_count")
            self.assertFalse(project.target.exists())

            foreign_resource = replace(
                plan.observations[0].resource,
                target=project.root / "live/undeclared.conf",
                target_name="live/undeclared.conf",
            )
            foreign_observation_plan = replace(
                plan,
                observations=(
                    replace(plan.observations[0], resource=foreign_resource),
                ),
            )
            with self.assertRaises(ApplyError) as foreign_error:
                apply_plan(foreign_observation_plan)
            self.assertEqual(foreign_error.exception.code, "invalid_plan")
            self.assertFalse((project.root / "live/undeclared.conf").exists())

    def test_reconciliation_rejects_a_plan_not_issued_by_the_planner(self) -> None:
        with _Project() as project:
            plan = build_plan(project.manifest)
            copied_plan = Plan(
                manifest=project.manifest,
                observations=plan.observations,
            )

            with self.assertRaises(ApplyError) as context:
                apply_plan(copied_plan)

            self.assertEqual(context.exception.code, "invalid_plan")
            self.assertFalse(project.target.exists())

    def test_planning_rejects_a_manifest_not_issued_by_the_loader(self) -> None:
        with _Project() as project:
            copied_manifest = replace(project.manifest)

            with self.assertRaises(ManifestError) as context:
                build_plan(copied_manifest)

            self.assertEqual(context.exception.code, "invalid_manifest_provenance")

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

    def test_undefined_template_input_fails_closed(self) -> None:
        with _Project() as project:
            project.source.write_text("{{ not_declared }}\n", encoding="utf-8")

            with self.assertRaises(RenderError) as context:
                build_plan(project.manifest)

            self.assertEqual(context.exception.code, "template_invalid")

    def test_template_runtime_errors_stay_inside_the_render_error_boundary(
        self,
    ) -> None:
        with _Project() as project:
            project.source.write_text("{{ 1 / 0 }}\n", encoding="utf-8")

            with self.assertRaises(RenderError) as context:
                build_plan(project.manifest)

            self.assertEqual(context.exception.code, "template_invalid")
            self.assertIsNone(context.exception.__cause__)

    def test_rendering_rejects_unclassified_template_variables(self) -> None:
        with _Project() as project:
            resource = replace(
                project.manifest.resources[0],
                variables={"profile": "developer"},
            )

            with self.assertRaises(RenderError) as context:
                render_template(resource, root=project.root)

            self.assertEqual(context.exception.code, "variables_sensitivity")

    def test_non_regular_template_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "templates/settings.conf.j2").mkdir(parents=True)
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                """version = 1

[resources.settings]
source = "templates/settings.conf.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )
            manifest = load_manifest(manifest_path)

            with self.assertRaises(RenderError) as context:
                build_plan(manifest)

            self.assertEqual(context.exception.code, "source_not_regular")

    def test_stale_target_is_not_overwritten(self) -> None:
        with _Project() as project:
            plan = build_plan(project.manifest)
            project.target.write_text("changed after plan\n", encoding="utf-8")

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(
                project.target.read_text(encoding="utf-8"),
                "changed after plan\n",
            )

    def test_stale_noop_target_is_detected(self) -> None:
        with _Project() as project:
            project.target.write_bytes(project.desired)
            plan = build_plan(project.manifest)
            project.target.write_text("changed after plan\n", encoding="utf-8")

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(
                project.target.read_text(encoding="utf-8"),
                "changed after plan\n",
            )

    def test_stale_target_identity_is_detected_even_when_bytes_are_unchanged(
        self,
    ) -> None:
        with _Project() as project:
            project.target.write_bytes(project.desired)
            plan = build_plan(project.manifest)
            replacement = project.target.with_name("replacement.conf")
            replacement.write_bytes(project.desired)
            replacement.replace(project.target)

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(project.target.read_bytes(), project.desired)

    def test_stale_source_is_not_applied(self) -> None:
        with _Project() as project:
            plan = build_plan(project.manifest)
            project.source.write_text('profile = "new"\n', encoding="utf-8")

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertFalse(project.target.exists())

    def test_stale_template_source_link_is_not_applied(self) -> None:
        with _Project() as project:
            plan = build_plan(project.manifest)
            alternate = project.root / "templates/alternate.conf.j2"
            alternate.write_bytes(project.source.read_bytes())
            project.source.unlink()
            project.source.symlink_to(alternate)

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertFalse(project.target.exists())

    def test_stale_manifest_is_not_applied(self) -> None:
        with _Project() as project:
            plan = build_plan(project.manifest)
            project.manifest_path.write_text(
                project.manifest_path.read_text(encoding="utf-8").replace(
                    'profile = "developer"',
                    'profile = "changed"',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertFalse(project.target.exists())

    def test_existing_target_permissions_are_preserved(self) -> None:
        with _Project() as project:
            project.target.write_text('profile = "other"\n', encoding="utf-8")
            project.target.chmod(0o600)
            plan = build_plan(project.manifest)

            apply_plan(plan)

            self.assertEqual(project.target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(project.target.read_bytes(), project.desired)

    def test_default_non_j2_resource_is_applied_as_a_symbolic_link(self) -> None:
        with _SymbolicProject() as project:
            self.assertEqual(project.manifest.resources[0].kind, "symbolic")
            plan = build_plan(project.manifest)

            result = apply_plan(plan)

            self.assertEqual(result.changed_targets, ("live/settings.conf",))
            self.assertTrue(project.target.is_symlink())
            self.assertEqual(project.target.resolve(), project.source.resolve())
            self.assertEqual(build_plan(project.manifest).summary()["changes"], 0)

    def test_explicit_symbolic_j2_resource_is_applied_as_a_link(self) -> None:
        with _SymbolicProject(
            source_name="files/settings.conf.j2",
            kind="symbolic",
        ) as project:
            self.assertEqual(project.manifest.resources[0].kind, "symbolic")

            result = apply_plan(build_plan(project.manifest))

            self.assertEqual(result.changed_targets, ("live/settings.conf",))
            self.assertTrue(project.target.is_symlink())
            self.assertEqual(project.target.resolve(), project.source.resolve())

    def test_symbolic_resource_blocks_a_different_existing_symlink(self) -> None:
        with _SymbolicProject() as project:
            other = project.root / "other.conf"
            other.write_text("other\n", encoding="utf-8")
            project.target.symlink_to(other)

            plan = build_plan(project.manifest)

            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            self.assertIn("different path", plan.observations[0].reason)


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


class _SymbolicProject:
    def __init__(
        self, *, source_name: str = "files/settings.conf", kind: str | None = None
    ) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "files").mkdir()
        (self.root / "live").mkdir()
        self.source = self.root / source_name
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_text('profile = "developer"\n', encoding="utf-8")
        self.target = self.root / "live/settings.conf"
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text(
            f'''version = 1

[resources.settings]
{f'kind = "{kind}"' if kind is not None else ""}
source = "{source_name}"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
''',
            encoding="utf-8",
        )
        self.manifest = load_manifest(self.manifest_path)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()
