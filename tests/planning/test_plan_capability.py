"""Tests for plan capability."""

from __future__ import annotations

import unittest
from dataclasses import replace

from luwu.errors import ApplyError, ManifestError
from luwu.reconcile import Plan, apply_plan, build_plan
from tests.support.projects import TemplateProject


class PlanCapabilityV1Tests(unittest.TestCase):
    def test_reconciliation_rejects_a_manually_constructed_multi_resource_plan(
        self,
    ) -> None:
        with TemplateProject() as project:
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
        with TemplateProject() as project:
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
        with TemplateProject() as project:
            copied_manifest = replace(project.manifest)

            with self.assertRaises(ManifestError) as context:
                build_plan(copied_manifest)

            self.assertEqual(context.exception.code, "invalid_manifest_provenance")
