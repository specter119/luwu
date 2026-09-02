from __future__ import annotations

import tempfile
import unittest
from collections.abc import MutableMapping
from pathlib import Path
from typing import cast

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

            self.assertEqual(manifest.version, 1)
            self.assertEqual(manifest.root, root)
            self.assertEqual(len(manifest.resources), 1)
            self.assertEqual(manifest.resources[0].kind, "template")
            self.assertEqual(manifest.resources[0].target_name, "live/settings.conf")
            self.assertEqual(manifest.resources[0].variables["profile"], "developer")
            with self.assertRaises(TypeError):
                variables = cast(
                    MutableMapping[str, object], manifest.resources[0].variables
                )
                variables["profile"] = "changed"

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

    def test_loads_multiple_resources_in_v2_and_orders_them_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.zeta]
source = "templates/zeta.j2"
target = "live/zeta.conf"
owner = "source"
scope = "whole-file"

[resources.alpha]
source = "templates/alpha.j2"
target = "live/alpha.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            manifest = load_manifest(manifest_path)

            self.assertEqual(manifest.version, 2)
            self.assertEqual(
                tuple(resource.name for resource in manifest.resources),
                ("alpha", "zeta"),
            )

    def test_v2_allows_multiple_resources_with_a_shared_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.first]
source = "templates/settings.j2"
target = "live/first.conf"
owner = "source"
scope = "whole-file"

[resources.second]
source = "templates/settings.j2"
target = "live/second.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            manifest = load_manifest(manifest_path)

            self.assertEqual(len(manifest.resources), 2)

    def test_rejects_v2_resources_with_the_same_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.first]
source = "templates/first.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"

[resources.second]
source = "templates/second.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_target_conflict")

    def test_rejects_v2_target_source_exact_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.first]
source = "templates/first.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"

[resources.second]
source = "live/settings.conf"
target = "live/second.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_path_conflict")

    def test_rejects_v2_source_symlink_resolving_to_another_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "templates").mkdir()
            (root / "live").mkdir()
            (root / "live/settings.conf").write_text("current\n", encoding="utf-8")
            (root / "templates/alias.j2").symlink_to(root / "live/settings.conf")
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.alias]
source = "templates/alias.j2"
target = "live/alias.conf"
owner = "source"
scope = "whole-file"

[resources.settings]
source = "templates/settings.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_path_conflict")

    def test_rejects_v2_resolved_source_ancestor_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "templates").mkdir()
            (root / "live/dir").mkdir(parents=True)
            (root / "templates/alias.j2").symlink_to(root / "live/dir")
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.alias]
kind = "symbolic"
source = "templates/alias.j2"
target = "live/alias"
owner = "source"
scope = "whole-file"

[resources.child]
source = "templates/child.j2"
target = "live/dir/child.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_path_overlap")

    def test_v2_relationship_validation_checks_non_adjacent_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.first]
source = "templates/first.j2"
target = "live/shared.conf"
owner = "source"
scope = "whole-file"

[resources.middle]
source = "templates/middle.j2"
target = "live/middle.conf"
owner = "source"
scope = "whole-file"

[resources.third]
source = "templates/third.j2"
target = "live/shared.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_target_conflict")

    def test_rejects_v2_ancestor_path_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.first]
source = "templates/first.j2"
target = "live"
owner = "source"
scope = "whole-file"

[resources.second]
source = "templates/second.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_path_overlap")

    def test_v2_relationship_errors_are_deterministic_by_resource_name(self) -> None:
        manifest_bodies = (
            """version = 2

[resources.first]
source = "templates/first.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"

[resources.second]
source = "templates/second.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
            """version = 2

[resources.second]
source = "templates/second.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"

[resources.first]
source = "templates/first.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
        )

        messages = []
        for body in manifest_bodies:
            with (
                self.subTest(body=body),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                manifest_path = Path(temporary_directory) / "luwu.toml"
                manifest_path.write_text(body, encoding="utf-8")

                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)

                self.assertEqual(
                    context.exception.code,
                    "resource_target_conflict",
                )
                messages.append(str(context.exception))

        self.assertEqual(messages[0], messages[1])

    def test_v2_rejects_unknown_format_field(self) -> None:
        for field_name in ("format",):
            with (
                self.subTest(field_name=field_name),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                manifest_path = Path(temporary_directory) / "luwu.toml"
                manifest_path.write_text(
                    f"""version = 2

[resources.settings]
source = "templates/settings.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
{field_name} = "unsupported"
""",
                    encoding="utf-8",
                )

                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)

                self.assertEqual(
                    context.exception.code,
                    "resource_unknown_field",
                )

    def test_v2_accepts_explicit_json_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.settings]
source = "templates/settings.j2"
target = "live/settings.json"
owner = "source"
scope = "whole-file"
comparison = "json"
""",
                encoding="utf-8",
            )

            manifest = load_manifest(manifest_path)

            self.assertEqual(manifest.resources[0].comparison, "json")

    def test_v1_rejects_explicit_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                _manifest_text().replace(
                    'scope = "whole-file"',
                    'scope = "whole-file"\ncomparison = "json"',
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_comparison_version")

    def test_v2_rejects_json_comparison_for_symbolic_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.settings]
kind = "symbolic"
source = "files/settings.conf"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
comparison = "json"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_comparison_kind")

    def test_v2_accepts_copy_resource_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 2

[resources.settings]
kind = "copy"
source = "files/settings.conf"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            manifest = load_manifest(manifest_path)

            self.assertEqual(manifest.resources[0].kind, "copy")

    def test_v1_rejects_copy_resource_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "luwu.toml"
            manifest_path.write_text(
                """version = 1

[resources.settings]
kind = "copy"
source = "files/settings.conf"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(manifest_path)

            self.assertEqual(context.exception.code, "resource_kind")

    def test_v2_keeps_ownership_and_scope_explicit(self) -> None:
        for field_name, value, expected_code in (
            ("owner", "live", "resource_owner"),
            ("scope", "fields", "resource_scope"),
        ):
            with (
                self.subTest(field_name=field_name),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                manifest_path = Path(temporary_directory) / "luwu.toml"
                owner = value if field_name == "owner" else "source"
                scope = value if field_name == "scope" else "whole-file"
                manifest_path.write_text(
                    f"""version = 2

[resources.settings]
source = "templates/settings.j2"
target = "live/settings.json"
owner = "{owner}"
scope = "{scope}"
""",
                    encoding="utf-8",
                )

                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)

                self.assertEqual(context.exception.code, expected_code)

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
