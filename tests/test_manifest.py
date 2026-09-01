from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from luwu.errors import ManifestError
from luwu.manifest import load_manifest


class ManifestTests(unittest.TestCase):
    def test_loads_explicit_template_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "templates").mkdir()
            (root / "templates/settings.conf.j2").write_text(
                "profile = '{{ profile }}'\n",
                encoding="utf-8",
            )
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(_manifest_text(), encoding="utf-8")

            manifest = load_manifest(manifest_path)

            self.assertEqual(manifest.root, root)
            self.assertEqual(len(manifest.resources), 1)
            self.assertEqual(manifest.resources[0].kind, "template")
            self.assertEqual(manifest.resources[0].target_name, "live/settings.conf")
            self.assertEqual(manifest.resources[0].variables["profile"], "developer")
            with self.assertRaises(TypeError):
                manifest.resources[0].variables["profile"] = "changed"

    def test_explicit_symbolic_kind_overrides_j2_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "templates/settings.conf.j2"
            source.parent.mkdir()
            source.write_text("literal", encoding="utf-8")
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                """version = 1

[resources.settings]
kind = "symbolic"
source = "templates/settings.conf.j2"
target = "settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            manifest = load_manifest(manifest_path)

            self.assertEqual(manifest.resources[0].kind, "symbolic")

    def test_rejects_explicit_template_kind_for_non_j2_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 1

[resources.settings]
kind = "template"
source = "settings.conf"
target = "settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_source_suffix")

    def test_rejects_unknown_resource_field_instead_of_ignoring_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                _manifest_text().replace('scope = "whole-file"', 'secret = "no"'),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_unknown_field")

    def test_rejects_sensitive_variable_names_in_the_public_m1_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                _manifest_text().replace(
                    'profile = "developer"',
                    'token = "not accepted"',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_secret_field")

    def test_requires_explicit_public_sensitivity_for_variables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                _manifest_text().replace(
                    'variables_sensitivity = "public"\n',
                    "",
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_sensitivity")

    def test_rejects_paths_that_leave_manifest_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                _manifest_text().replace(
                    'target = "live/settings.conf"',
                    'target = "../settings.conf"',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "path_boundary")

    def test_rejects_a_source_symlink_that_leaves_manifest_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(temporary_directory)
            outside = Path(outside_directory) / "outside-source.j2"
            outside.write_text("outside", encoding="utf-8")
            (root / "templates").mkdir()
            (root / "templates/settings.conf.j2").symlink_to(outside)
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(_manifest_text(), encoding="utf-8")

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_source_boundary")

    def test_rejects_multiple_resources_in_m1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                _manifest_text()
                + "\n[resources.other]\n"
                + 'kind = "template"\n'
                + 'source = "templates/settings.conf.j2"\n'
                + 'target = "live/settings.conf"\n'
                + 'owner = "source"\n'
                + 'scope = "whole-file"\n',
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_count")

    def test_rejects_a_resource_source_and_target_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                """version = 1

[resources.settings]
source = "templates/settings.conf.j2"
target = "templates/settings.conf.j2"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "source_target_same")

    def test_rejects_a_source_symlink_resolving_to_a_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "templates").mkdir()
            (root / "live").mkdir()
            (root / "live/settings.conf").write_text("current\n", encoding="utf-8")
            (root / "templates/alias.j2").symlink_to(root / "live/settings.conf")
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                """version = 1

[resources.settings]
source = "templates/alias.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "source_target_conflict")


def _manifest_text() -> str:
    return """version = 1

[resources.settings]
source = "templates/settings.conf.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
variables_sensitivity = "public"

[resources.settings.variables]
profile = "developer"
"""
