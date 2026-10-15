"""Tests for manifest core."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, cast

from luwu.errors import ManifestError
from luwu.manifest import (
    EXECUTION_MANIFEST_VERSION,
    EXECUTION_RESOURCE_CAPABILITY,
    PROVIDER_MANIFEST_VERSION,
    is_execution_manifest,
    is_provider_manifest,
    load_manifest,
)


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


def _manifest(*, version: int = 3, extra: str = "") -> str:
    return f"""version = {version}

[resources.settings]
kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
{extra}
[resources.settings.fields]
"server.url" = "source"
"credentials/token" = "ignore"
"""


def _manifest_v5(*, resources: str, extra: str = "") -> str:
    return f"""version = {EXECUTION_MANIFEST_VERSION}

{resources}
{extra}"""


def _resource(
    name: str,
    *,
    kind: str = "symbolic",
    source: str | None = None,
    target: str | None = None,
    extra: str = "",
) -> str:
    source = source or f"sources/{name}.conf"
    target = target or f"targets/{name}.conf"
    return f"""[resources.{name}]
kind = "{kind}"
source = "{source}"
target = "{target}"
owner = "source"
scope = "whole-file"
content_sensitivity = "public"
{extra}
"""


def _manifest_v6(*, extra_root: str = "", resource_extra: str = "") -> str:
    return f"""version = {PROVIDER_MANIFEST_VERSION}
capabilities = ["subprocess"]
{extra_root}
[resources.database]
kind = "template"
source = "templates/database.conf.j2"
target = "/tmp/luwu-m4-database.conf"
owner = "source"
scope = "whole-file"
content_sensitivity = "secret"
{resource_extra}
[resources.database.providers.db_password]
type = "rbw"
item = "database-prod"
field = "password"
"""


class ManifestV1V2Tests(unittest.TestCase):
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


class ManifestV3Tests(unittest.TestCase):
    def test_v3_loads_frozen_fields_and_optional_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                _manifest(extra='baseline = "baseline/settings.json"\n'),
                encoding="utf-8",
            )

            resource = load_manifest(manifest_path).resources[0]

            self.assertEqual(resource.fields["server.url"], "source")
            self.assertEqual(resource.fields["credentials/token"], "ignore")
            self.assertEqual(resource.baseline_name, "baseline/settings.json")
            self.assertEqual(resource.baseline, root / "baseline/settings.json")
            with self.assertRaises(TypeError):
                cast(Any, resource.fields)["new"] = "live"

    def test_v3_rejects_non_public_or_unknown_resource_fields(self) -> None:
        for replacement, code in (
            ('content_sensitivity = "secret"', "resource_content_sensitivity"),
            ('comparison = "exact-bytes"', "resource_comparison_version"),
            ("variables = {}", "resource_unknown_field"),
        ):
            with (
                self.subTest(replacement=replacement),
                tempfile.TemporaryDirectory() as directory,
            ):
                manifest_path = Path(directory) / "luwu.toml"
                body = _manifest()
                if replacement.startswith("comparison"):
                    body = body.replace('comparison = "json"', replacement)
                elif replacement.startswith("variables"):
                    body = body.replace('content_sensitivity = "public"', replacement)
                else:
                    body = body.replace('content_sensitivity = "public"', replacement)
                manifest_path.write_text(body, encoding="utf-8")
                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)
                self.assertEqual(context.exception.code, code)

    def test_v1_and_v2_reject_v3_fields(self) -> None:
        for version in (1, 2):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
            ):
                manifest_path = Path(directory) / "luwu.toml"
                manifest_path.write_text(_manifest(version=version), encoding="utf-8")
                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)
                self.assertEqual(context.exception.code, "resource_unknown_field")

    def test_v3_rejects_baseline_equal_to_or_ancestor_of_declared_paths(self) -> None:
        for baseline in ("luwu.toml", "templates", "live/settings.json"):
            with (
                self.subTest(baseline=baseline),
                tempfile.TemporaryDirectory() as directory,
            ):
                manifest_path = Path(directory) / "luwu.toml"
                manifest_path.write_text(
                    _manifest(extra=f'baseline = "{baseline}"\n'), encoding="utf-8"
                )
                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)
                self.assertEqual(context.exception.code, "baseline_path_conflict")

    def test_v3_rejects_baseline_escaping_root_and_empty_fields(self) -> None:
        for extra, expected in (
            ('baseline = "../baseline.json"\n', "path_boundary"),
            ("", "resource_fields"),
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                manifest_path = Path(directory) / "luwu.toml"
                body = _manifest(extra=extra)
                if expected == "resource_fields":
                    body = body.split("[resources.settings.fields]")[0]
                manifest_path.write_text(body, encoding="utf-8")
                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)
                self.assertEqual(context.exception.code, expected)


class ManifestV5Tests(unittest.TestCase):
    def test_v5_exposes_a_stable_execution_capability_and_sorts_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "luwu.toml"
            manifest_path.write_text(
                _manifest_v5(resources=_resource("zeta") + _resource("alpha")),
                encoding="utf-8",
            )

            manifest = load_manifest(manifest_path)

            self.assertEqual(manifest.version, EXECUTION_MANIFEST_VERSION)
            self.assertTrue(is_execution_manifest(manifest))
            self.assertEqual(
                manifest.execution_capability, EXECUTION_RESOURCE_CAPABILITY
            )
            self.assertEqual(
                [resource.name for resource in manifest.resources], ["alpha", "zeta"]
            )
            self.assertEqual(
                [resource.capability for resource in manifest.resources],
                [EXECUTION_RESOURCE_CAPABILITY, EXECUTION_RESOURCE_CAPABILITY],
            )

    def test_v5_accepts_only_explicit_template_and_symbolic_public_resources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "luwu.toml"
            manifest_path.write_text(
                _manifest_v5(
                    resources=(
                        _resource(
                            "template",
                            kind="template",
                            source="templates/settings.j2",
                            extra=(
                                '\nvariables_sensitivity = "public"\n'
                                "[resources.template.variables]\n"
                                'profile = "public"\n'
                            ),
                        )
                        + _resource("symbolic")
                    )
                ),
                encoding="utf-8",
            )

            resources = load_manifest(manifest_path).resources

            self.assertEqual(
                [resource.kind for resource in resources], ["symbolic", "template"]
            )
            self.assertEqual(resources[1].variables["profile"], "public")

    def test_v5_rejects_legacy_or_external_capability_fields(self) -> None:
        for field in (
            'comparison = "json"',
            '[resources.settings.fields]\nkey = "source"',
            'baseline = "baseline/settings.json"',
            '[resources.settings.reverse_sync]\nformat = "literal-json"',
            'provider = "rbw"',
            'secret = "do-not-accept"',
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                manifest_path = Path(directory) / "luwu.toml"
                manifest_path.write_text(
                    _manifest_v5(resources=_resource("settings", extra=f"\n{field}\n")),
                    encoding="utf-8",
                )

                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)

                self.assertEqual(context.exception.code, "resource_unknown_field")

    def test_v5_rejects_unsupported_kind_owner_scope_and_publicness(self) -> None:
        cases = (
            ("kind =", _resource("settings", kind="copy"), "resource_kind"),
            (
                "owner =",
                _resource("settings").replace('owner = "source"', 'owner = "live"'),
                "resource_owner",
            ),
            (
                "scope =",
                _resource("settings").replace(
                    'scope = "whole-file"', 'scope = "fields"'
                ),
                "resource_scope",
            ),
            (
                "sensitivity",
                _resource("settings").replace(
                    'content_sensitivity = "public"',
                    'content_sensitivity = "secret"',
                ),
                "resource_content_sensitivity",
            ),
            (
                "missing kind",
                _resource("settings").replace('kind = "symbolic"\n', ""),
                "resource_kind",
            ),
        )
        for label, resource, code in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                manifest_path = Path(directory) / "luwu.toml"
                manifest_path.write_text(
                    _manifest_v5(resources=resource), encoding="utf-8"
                )

                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)

                self.assertEqual(context.exception.code, code)

    def test_v5_rejects_sensitive_variable_values_without_echoing_them(self) -> None:
        for key in (
            "password",
            "client_secret",
            "access_token",
            "clientSecret",
            "accessToken",
        ):
            secret_value = f"not-a-real-{key}-value"
            resource = _resource(
                "settings",
                kind="template",
                source="templates/settings.j2",
                extra=(
                    f'\n[resources.settings.variables.client]\n{key} = "{secret_value}"\n'
                ),
            )
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                manifest_path = Path(directory) / "luwu.toml"
                manifest_path.write_text(
                    _manifest_v5(resources=resource), encoding="utf-8"
                )

                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)

                self.assertEqual(context.exception.code, "resource_secret_field")
                self.assertNotIn(secret_value, str(context.exception))

    def test_v5_rejects_cross_resource_target_and_ancestor_conflicts(self) -> None:
        cases = (
            (
                "target",
                _resource("alpha", target="shared.conf")
                + _resource("beta", target="shared.conf"),
                "resource_target_conflict",
            ),
            (
                "source target",
                _resource(
                    "alpha", source="sources/alpha.conf", target="targets/beta.conf"
                )
                + _resource(
                    "beta", source="sources/beta.conf", target="sources/alpha.conf"
                ),
                "resource_path_conflict",
            ),
            (
                "ancestor",
                _resource("alpha", target="targets")
                + _resource("beta", target="targets/beta.conf"),
                "resource_path_overlap",
            ),
        )
        for label, resources, code in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                manifest_path = Path(directory) / "luwu.toml"
                manifest_path.write_text(
                    _manifest_v5(resources=resources), encoding="utf-8"
                )

                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)

                self.assertEqual(context.exception.code, code)


class ManifestV6Tests(unittest.TestCase):
    def _write(self, root: Path, body: str) -> Path:
        (root / "templates").mkdir()
        (root / "templates/database.conf.j2").write_text(
            "password={{ secrets.db_password }}\n",
            encoding="utf-8",
        )
        manifest_path = root / "luwu.toml"
        manifest_path.write_text(body, encoding="utf-8")
        return manifest_path

    def test_v6_is_closed_and_exposes_only_safe_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = load_manifest(self._write(Path(directory), _manifest_v6()))
            resource = manifest.resources[0]

            self.assertTrue(is_provider_manifest(manifest))
            self.assertEqual(manifest.capabilities, ("subprocess",))
            self.assertEqual(resource.kind, "template")
            self.assertEqual(resource.owner, "source")
            self.assertEqual(resource.scope, "whole-file")
            self.assertEqual(resource.content_sensitivity, "secret")
            self.assertEqual(resource.target, Path("/tmp/luwu-m4-database.conf"))
            self.assertEqual(resource.providers["db_password"].type, "rbw")
            self.assertNotIn("database-prod", repr(manifest))
            with self.assertRaises(TypeError):
                cast(Any, resource.providers)["another"] = object()

    def test_v6_requires_the_exact_root_capability_list(self) -> None:
        for capabilities in (
            "capabilities = []",
            'capabilities = ["network"]',
            'capabilities = ["subprocess", "network"]',
        ):
            with (
                self.subTest(capabilities=capabilities),
                tempfile.TemporaryDirectory() as directory,
            ):
                body = _manifest_v6().replace(
                    'capabilities = ["subprocess"]', capabilities
                )
                with self.assertRaises(ManifestError) as context:
                    load_manifest(self._write(Path(directory), body))
                self.assertEqual(context.exception.code, "manifest_capabilities")

    def test_v6_requires_explicit_secret_template_contract(self) -> None:
        cases = (
            ("kind", 'kind = "symbolic"', "resource_kind"),
            (
                "sensitivity",
                'content_sensitivity = "public"',
                "resource_content_sensitivity",
            ),
            ("target", 'target = "relative.conf"', "target_boundary"),
        )
        for label, replacement, code in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                body = _manifest_v6().replace(
                    {
                        "kind": 'kind = "template"',
                        "sensitivity": 'content_sensitivity = "secret"',
                        "target": 'target = "/tmp/luwu-m4-database.conf"',
                    }[label],
                    replacement,
                )
                with self.assertRaises(ManifestError) as context:
                    load_manifest(self._write(Path(directory), body))
                self.assertEqual(context.exception.code, code)

    def test_v6_rejects_targets_inside_root_and_missing_providers(self) -> None:
        for replacement, code in (
            ('target = "/tmp/inside/luwu.toml"', "target_boundary"),
            ("", "resource_providers"),
        ):
            with (
                self.subTest(replacement=replacement),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                body = _manifest_v6()
                if replacement:
                    body = body.replace(
                        'target = "/tmp/luwu-m4-database.conf"',
                        f'target = "{root / "inside/luwu.toml"}"',
                    )
                else:
                    body = body.split("[resources.database.providers.db_password]")[0]
                with self.assertRaises(ManifestError) as context:
                    load_manifest(self._write(root, body))
                self.assertEqual(context.exception.code, code)

    def test_v6_validates_alias_and_opaque_argv_values(self) -> None:
        cases = (
            ("alias", "bad-alias", "provider_alias"),
            ("reserved", "secrets", "provider_alias"),
            ("item", "-option", "provider_field"),
            ("field", r"bad\nfield", "provider_field"),
        )
        for label, value, code in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                body = _manifest_v6()
                if label in {"alias", "reserved"}:
                    body = body.replace(
                        "[resources.database.providers.db_password]",
                        f"[resources.database.providers.{value}]",
                    )
                else:
                    old_value = "database-prod" if label == "item" else "password"
                    body = body.replace(
                        f'{label} = "{old_value}"',
                        f'{label} = "{value}"',
                    )
                with self.assertRaises(ManifestError) as context:
                    load_manifest(self._write(Path(directory), body))
                self.assertEqual(context.exception.code, code)

    def test_v1_to_v5_reject_v6_root_and_resource_fields(self) -> None:
        for version in range(1, 6):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
            ):
                body = _manifest_v6().replace(
                    f"version = {PROVIDER_MANIFEST_VERSION}", f"version = {version}"
                )
                with self.assertRaises(ManifestError) as context:
                    load_manifest(self._write(Path(directory), body))
                self.assertIn(
                    context.exception.code,
                    {"manifest_unknown_field", "resource_unknown_field"},
                )
