"""Tests for pilot inputs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from luwu.m6_formats import load_m6_inputs
from luwu.manifest import load_manifest
from luwu.rendering import render_template


class PilotInputsV7Tests(unittest.TestCase):
    def test_input_provenance_and_renderer_context_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("repo", "inputs/global", "inputs/local", "home"):
                (root / name).mkdir(parents=True)
            (root / "repo/pi.json.j2").write_text(
                '{"model":"{{ global_inputs.model }}","provider":"{{ local_inputs.provider }}"}\n',
                encoding="utf-8",
            )
            (root / "inputs/global/pi.json").write_text(
                '{"model":"pi-4"}\n', encoding="utf-8"
            )
            (root / "inputs/local/pi.json").write_text(
                '{"provider":"local-provider"}\n', encoding="utf-8"
            )
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(self._manifest(), encoding="utf-8")

            manifest = load_manifest(manifest_path)
            roots = manifest.roots
            assert roots is not None
            context = load_m6_inputs(manifest.resources[0], roots)
            rendered = render_template(
                manifest.resources[0],
                root=roots.source.path,
                m6_inputs=context,
            )

            self.assertEqual(context.values["model"].provenance, "global")
            self.assertEqual(context.values["provider"].provenance, "local")
            self.assertEqual(
                rendered.data, b'{"model":"pi-4","provider":"local-provider"}\n'
            )
            self.assertNotIn("pi-4", repr(context.values["model"]))

    def _manifest(self) -> str:
        return """version = 7

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[resources.pi]
kind = "template"
source = "pi.json.j2"
target = "pi.json"
owner = "fields"
scope = "fields"
comparison = "json"
content_sensitivity = "public"
inputs = { model = "global/pi.json", provider = "local/pi.json" }

[resources.pi.fields]
model = "source"
provider = "live"
"""
