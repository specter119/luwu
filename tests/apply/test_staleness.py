"""Tests for staleness."""

from __future__ import annotations

import unittest

from luwu.errors import ApplyError
from luwu.reconcile import apply_plan, build_plan
from tests.support.projects import TemplateProject


class StalenessV1Tests(unittest.TestCase):
    def test_stale_target_is_not_overwritten(self) -> None:
        with TemplateProject() as project:
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
        with TemplateProject() as project:
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
        with TemplateProject() as project:
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
        with TemplateProject() as project:
            plan = build_plan(project.manifest)
            project.source.write_text('profile = "new"\n', encoding="utf-8")

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertFalse(project.target.exists())

    def test_stale_template_source_link_is_not_applied(self) -> None:
        with TemplateProject() as project:
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
        with TemplateProject() as project:
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
