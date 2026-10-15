"""Tests for symbolic links."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Self

from luwu.manifest import load_manifest
from luwu.reconcile import Status, apply_plan, build_plan


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


class SymbolicResourceV1Tests(unittest.TestCase):
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
