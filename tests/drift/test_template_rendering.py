"""Tests for template rendering."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Self

from luwu.errors import RenderError
from luwu.manifest import load_manifest
from luwu.reconcile import build_plan
from luwu.rendering import render_template


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


class TemplateRenderingTests(unittest.TestCase):
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

    def test_rendered_unicode_encoding_failure_is_a_controlled_error(self) -> None:
        with _Project() as project:
            project.source.write_text('{{ "\\ud800" }}', encoding="utf-8")
            with self.assertRaises(RenderError) as context:
                build_plan(project.manifest)
            self.assertEqual(context.exception.code, "rendered_encoding")
            self.assertNotIn("\\ud800", str(context.exception))
            self.assertFalse(project.target.exists())
