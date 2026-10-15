"""Tests for plan capability."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Self

from luwu.errors import ApplyError, ManifestError
from luwu.manifest import load_manifest
from luwu.reconcile import Plan, apply_plan, build_plan


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


class PlanCapabilityV1Tests(unittest.TestCase):
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
