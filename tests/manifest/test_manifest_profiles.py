"""Tests for manifest profiles."""

from __future__ import annotations

import dataclasses
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luwu import manifest as manifest_module
from luwu.errors import ManifestError
from luwu.filesystem import FileChangedError, RootChangedError
from luwu.manifest import (
    _manifest_signature,
    _parse_manifest,
    _path_key,
    load_manifest,
    verify_manifest,
)


class ManifestV8Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source_root = self.root / "repo"
        self.inputs_root = self.root / "inputs"
        self.target_root = self.root / "home"
        for directory in (
            self.source_root,
            self.inputs_root / "global",
            self.inputs_root / "local",
            self.inputs_root / "baselines",
            self.target_root / ".config",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (self.source_root / "settings.json.j2").write_text("{}\n", encoding="utf-8")
        (self.inputs_root / "global/model.json").write_text(
            '{"model":"local-test"}\n', encoding="utf-8"
        )
        self.manifest_path = self.root / "luwu.toml"

    def _manifest(self, body: str | None = None) -> str:
        if body is not None:
            return body
        return """
version = 8

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[profiles.dev]
packages = ["settings"]
exclude = []

[packages.settings]
dependencies = []
resources = ["settings"]

[inputs.model]
layers = [
  { file = "global/model.json", path = ["model"], format = "json" },
  { file = "local/model.json", path = ["model"], format = "json", optional = true },
]
reverse_to = { file = "local/model.json", path = ["model"], format = "json" }

[resources.settings]
kind = "template"
source = "settings.json.j2"
target = ".config/settings.json"
format = "json"
baseline = "baselines/settings.json"
content_sensitivity = "public"
scope = "fields"
parents = "require"
parent_mode = "0700"

[resources.settings.fields.model]
path = ["model"]
owner = "merge"
input = "model"
"""

    def _load(self, body: str | None = None, *, profile: str = "dev"):
        self.manifest_path.write_text(self._manifest(body), encoding="utf-8")
        return load_manifest(self.manifest_path, profile=profile)

    def _directory_manifest(
        self,
        *,
        source: str = "assets",
        recursive: bool = True,
        exclude: str | None = None,
    ) -> str:
        prefix = self._manifest().split("[resources.settings]", maxsplit=1)[0]
        prefix = prefix.replace('resources = ["settings"]', 'resources = ["assets"]')
        exclude_line = f"exclude = [{exclude}]\n" if exclude is not None else ""
        return (
            prefix
            + f'''[resources.assets]
kind = "directory"
source = "{source}"
target = ".config/assets"
baseline = "baselines/assets"
content_sensitivity = "public"
scope = "whole-file"
owner = "source"
parents = "create"
parent_mode = "0700"
{exclude_line}recursive = {str(recursive).lower()}
            '''
        )

    def _shared_input_manifest(
        self,
        *,
        second_read: str = "second",
        second_reverse: str = "second",
        second_format: str = "json",
    ) -> str:
        old_input = (
            "[inputs.model]\nlayers = [\n"
            '  { file = "global/model.json", path = ["model"], format = "json" },\n'
            '  { file = "local/model.json", path = ["model"], format = "json", optional = true },\n'
            ']\nreverse_to = { file = "local/model.json", path = ["model"], format = "json" }'
        )
        new_input = f'''[inputs.first]
layers = [{{ file = "global/shared.json", path = ["first"], format = "json" }}]
reverse_to = {{ file = "global/shared.json", path = ["first"], format = "json" }}

[inputs.second]
layers = [{{ file = "global/shared.json", path = ["{second_read}"], format = "{second_format}" }}]
reverse_to = {{ file = "global/shared.json", path = ["{second_reverse}"], format = "{second_format}" }}'''
        body = self._manifest().replace(old_input, new_input)
        return body.replace(
            '[resources.settings.fields.model]\npath = ["model"]\nowner = "merge"\ninput = "model"',
            '[resources.settings.fields.first]\npath = ["first"]\nowner = "merge"\ninput = "first"\n\n'
            '[resources.settings.fields.second]\npath = ["second"]\nowner = "merge"\ninput = "second"',
        )

    def _copy_manifest(self) -> str:
        prefix = self._manifest().split("[resources.settings]", maxsplit=1)[0]
        prefix = prefix.replace('resources = ["settings"]', 'resources = ["plain"]')
        return (
            prefix
            + """[resources.plain]
kind = "copy"
source = "plain.conf"
target = ".config/plain.conf"
baseline = "baselines/plain.json"
content_sensitivity = "public"
scope = "whole-file"
owner = "source"
parents = "require"
parent_mode = "0700"
"""
        )

    def test_loads_v8_field_bindings_and_private_provenance(self) -> None:
        manifest = self._load()

        self.assertEqual(manifest.profile, "dev")
        self.assertEqual(manifest.packages, ("settings",))
        self.assertEqual(manifest.resources[0].kind, "template")
        self.assertEqual(manifest.resources[0].fields["model"].path, ("model",))
        binding = manifest.inputs["model"]
        self.assertEqual(len(binding.layers), 2)
        self.assertTrue(binding.layers[1].optional)
        self.assertEqual(binding.reverse_to.path, ("model",))
        self.assertNotIn("local-test", repr(manifest))
        with self.assertRaises(TypeError):
            manifest.inputs["other"] = binding
        with self.assertRaises(TypeError):
            manifest.resources[0].fields["other"] = manifest.resources[0].fields[
                "model"
            ]
        verify_manifest(manifest)

    def test_copy_resource_is_whole_file_and_has_no_format_or_fields(self) -> None:
        (self.source_root / "plain.conf").write_text("plain\n", encoding="utf-8")

        manifest = self._load(self._copy_manifest())

        resource = manifest.resources[0]
        self.assertEqual(resource.kind, "copy")
        self.assertIsNone(resource.format)
        self.assertEqual(dict(resource.fields), {})

    def test_rejects_legacy_and_future_versions_and_unknown_schema_fields(self) -> None:
        for version in (7, 9, True):
            with self.subTest(version=version):
                body = self._manifest().replace(
                    "version = 8", f"version = {str(version).lower()}"
                )
                with self.assertRaises(ManifestError):
                    self._load(body)

        body = self._manifest().replace("version = 8", "version = 8\nproviders = []")
        with self.assertRaises(ManifestError):
            self._load(body)

    def test_profiles_and_packages_require_all_version_8_fields(self) -> None:
        missing_excludes = self._manifest().replace("exclude = []\n", "")
        with self.assertRaises(ManifestError):
            self._load(missing_excludes)

        missing_dependencies = self._manifest().replace("dependencies = []\n", "")
        with self.assertRaises(ManifestError):
            self._load(missing_dependencies)

    def test_manifest_parser_does_not_retain_source_in_exception_chain(self) -> None:
        marker = "SYNTHETIC_MARKER"
        cases = (
            (b"version=8\n# SYNTHETIC_MARKER\xff", "manifest_encoding"),
            (b'version=8\ninvalid = "SYNTHETIC_MARKER', "manifest_toml"),
        )
        for raw, expected_code in cases:
            with self.subTest(code=expected_code):
                with self.assertRaises(ManifestError) as raised:
                    _parse_manifest(raw)

                error = raised.exception
                self.assertEqual(error.code, expected_code)
                self.assertIsNone(error.__context__)
                self.assertIsNone(error.__cause__)

                pending: list[BaseException] = [error]
                visited: set[int] = set()
                while pending:
                    current = pending.pop()
                    if id(current) in visited:
                        continue
                    visited.add(id(current))
                    self.assertNotIn(marker, str(current))
                    self.assertNotIn(marker, repr(current))
                    if current.__context__ is not None:
                        pending.append(current.__context__)
                    if current.__cause__ is not None:
                        pending.append(current.__cause__)

    def test_rejects_unknown_nested_resource_fields(self) -> None:
        body = self._manifest().replace(
            'content_sensitivity = "public"',
            'content_sensitivity = "public"\nexecution = true',
        )
        with self.assertRaises(ManifestError):
            self._load(body)

    def test_requires_public_content_and_field_input_for_live_merge(self) -> None:
        with self.assertRaises(ManifestError):
            self._load(
                self._manifest().replace(
                    'content_sensitivity = "public"', 'content_sensitivity = "secret"'
                )
            )

        body = self._manifest().replace('input = "model"\n', "")
        with self.assertRaises(ManifestError):
            self._load(body)

    def test_rejects_secret_shaped_decoded_field_paths(self) -> None:
        body = self._manifest().replace(
            'path = ["model"]\nowner = "merge"',
            'path = ["client_secret"]\nowner = "merge"',
        )
        with self.assertRaises(ManifestError):
            self._load(body)

    def test_field_paths_accept_literal_separator_and_dotted_keys(self) -> None:
        body = (
            self._manifest()
            + r"""

[resources.settings.fields.windows_key]
path = ["windows\\profile"]
owner = "source"

[resources.settings.fields.dotted_literal]
path = ["config", "a.b"]
owner = "source"

[resources.settings.fields.nested]
path = ["config", "a", "b"]
owner = "source"

[resources.settings.fields.dot_segment]
path = ["."]
owner = "source"

[resources.settings.fields.dotdot_segment]
path = [".."]
owner = "source"
"""
        )
        manifest = self._load(body)
        fields = manifest.resources[0].fields

        self.assertEqual(fields["windows_key"].path, ("windows\\profile",))
        self.assertEqual(fields["dotted_literal"].path, ("config", "a.b"))
        self.assertEqual(fields["nested"].path, ("config", "a", "b"))
        self.assertEqual(fields["dot_segment"].path, (".",))
        self.assertEqual(fields["dotdot_segment"].path, ("..",))

    def test_official_shape_fixture_loads_absolute_project_and_hook_keys(self) -> None:
        fixture_manifest = (
            Path(__file__).resolve().parents[1] / "fixtures/dotfiles_profile/luwu.toml"
        )
        manifest = load_manifest(fixture_manifest, profile="official_shapes")
        codex = next(
            resource for resource in manifest.resources if resource.name == "codex"
        )

        self.assertEqual(
            codex.fields["project_trust"].path,
            ("projects", "/synthetic/luwu-demo/project.a", "trust_level"),
        )
        self.assertEqual(
            codex.fields["hook_enabled"].path,
            (
                "hooks",
                "state",
                "/synthetic/luwu-demo/hooks.json:pre_tool_use:0:0",
                "enabled",
            ),
        )

    def test_field_path_segments_keep_control_and_length_limits(self) -> None:
        control_path = self._manifest().replace(
            '[resources.settings.fields.model]\npath = ["model"]',
            '[resources.settings.fields.model]\npath = ["bad\\nkey"]',
        )
        with self.assertRaises(ManifestError) as raised:
            self._load(control_path)
        self.assertEqual(raised.exception.code, "field_path")

        def with_segment_count(count: int) -> str:
            segments = ", ".join(f'"segment_{index}"' for index in range(count))
            return self._manifest() + (
                "\n[resources.settings.fields.deep]\n"
                f'path = [{segments}]\nowner = "source"\n'
            )

        manifest = self._load(with_segment_count(256))
        self.assertEqual(len(manifest.resources[0].fields["deep"].path), 256)

        with self.assertRaises(ManifestError) as raised:
            self._load(with_segment_count(257))
        self.assertEqual(raised.exception.code, "field_path")

    def test_stable_dependency_order_and_exclusion_precede_directory_walk(self) -> None:
        excluded = self.source_root / "excluded"
        excluded.mkdir()
        (excluded / "unsafe-link").symlink_to(self.root / "outside")
        body = (
            self._manifest()
            .replace(
                '[profiles.dev]\npackages = ["settings"]\nexclude = []',
                '[profiles.dev]\npackages = ["app", "alpha", "ignored"]\nexclude = ["ignored"]',
            )
            .replace(
                '[packages.settings]\ndependencies = []\nresources = ["settings"]',
                "[packages.alpha]\ndependencies = []\nresources = []\n\n"
                '[packages.base]\ndependencies = []\nresources = ["settings"]\n\n'
                '[packages.app]\ndependencies = ["base"]\nresources = []\n\n'
                '[packages.ignored]\ndependencies = []\nresources = ["unused"]',
            )
            .replace(
                "[resources.settings]",
                '[resources.unused]\nkind = "directory"\nsource = "excluded"\ntarget = ".config/ignored"\n'
                'baseline = "baselines/unused"\ncontent_sensitivity = "public"\n'
                'scope = "whole-file"\nowner = "source"\nparents = "create"\nparent_mode = "0700"\n\n'
                "[resources.settings]",
            )
        )

        manifest = self._load(body)

        self.assertEqual(manifest.packages, ("alpha", "base", "app"))
        self.assertEqual(manifest.excluded, ("ignored",))
        self.assertEqual(
            tuple(resource.name for resource in manifest.resources), ("settings",)
        )

    def test_package_cycles_and_excluded_dependencies_fail_closed(self) -> None:
        cycle = self._manifest().replace(
            '[packages.settings]\ndependencies = []\nresources = ["settings"]',
            '[packages.settings]\ndependencies = ["other"]\nresources = ["settings"]\n\n'
            '[packages.other]\ndependencies = ["settings"]\nresources = []',
        )
        with self.assertRaises(ManifestError):
            self._load(cycle)

        excluded_dependency = (
            self._manifest()
            .replace(
                '[profiles.dev]\npackages = ["settings"]\nexclude = []',
                '[profiles.dev]\npackages = ["settings"]\nexclude = ["base"]',
            )
            .replace(
                '[packages.settings]\ndependencies = []\nresources = ["settings"]',
                '[packages.settings]\ndependencies = ["base"]\nresources = ["settings"]\n\n'
                "[packages.base]\ndependencies = []\nresources = []",
            )
        )
        with self.assertRaises(ManifestError):
            self._load(excluded_dependency)

    def test_excluded_resources_and_their_inputs_are_never_inspected(self) -> None:
        excluded = self.source_root / "excluded"
        excluded.mkdir()
        (excluded / "unsafe-link").symlink_to(self.root / "outside")
        body = (
            self._manifest()
            .replace(
                '[profiles.dev]\npackages = ["settings"]\nexclude = []',
                '[profiles.dev]\npackages = ["settings", "ignored"]\nexclude = ["ignored"]',
            )
            .replace(
                '[packages.settings]\ndependencies = []\nresources = ["settings"]',
                '[packages.settings]\ndependencies = []\nresources = ["settings"]\n\n'
                '[packages.ignored]\ndependencies = []\nresources = ["ignored_dir", "ignored_template"]',
            )
            .replace(
                "[resources.settings]",
                '[resources.ignored_dir]\nkind = "directory"\nsource = "excluded"\n'
                'target = ".config/ignored-dir"\nbaseline = "baselines/ignored-dir"\n'
                'content_sensitivity = "public"\nscope = "whole-file"\nowner = "source"\n'
                'parents = "create"\nparent_mode = "0700"\n\n[resources.settings]',
            )
        )
        body += """
[inputs.ignored]
layers = [{ file = "global/missing.json", path = ["value"], format = "json" }]

[resources.ignored_template]
kind = "template"
source = "missing-template.j2"
target = ".config/ignored-template.json"
format = "json"
baseline = "baselines/ignored-template.json"
content_sensitivity = "public"
scope = "fields"
parents = "create"
parent_mode = "0700"

[resources.ignored_template.fields.value]
path = ["value"]
owner = "merge"
input = "ignored"
"""

        manifest = self._load(body)

        self.assertEqual(
            tuple(resource.name for resource in manifest.resources), ("settings",)
        )
        self.assertNotIn("ignored", manifest.inputs)

    def test_shared_input_document_supports_distinct_field_paths_and_writers(
        self,
    ) -> None:
        shared = self.inputs_root / "global/shared.json"
        shared.write_text('{"first":"a","second":"b"}\n', encoding="utf-8")
        body = self._shared_input_manifest()

        manifest = self._load(body)

        self.assertEqual(set(manifest.inputs), {"first", "second"})
        self.assertEqual(manifest.inputs["first"].layers[0].file, shared)
        self.assertEqual(manifest.inputs["second"].reverse_to.path, ("second",))

    def test_shared_input_document_rejects_overlapping_field_and_write_paths(
        self,
    ) -> None:
        overlapping_input = self._shared_input_manifest(second_read="first")
        with self.assertRaises(ManifestError) as raised:
            self._load(overlapping_input)
        self.assertEqual(raised.exception.code, "input_path_overlap")

        overlapping_writes = self._shared_input_manifest(second_reverse="first")
        with self.assertRaises(ManifestError) as raised:
            self._load(overlapping_writes)
        self.assertEqual(raised.exception.code, "write_alias")

    def test_shared_input_documents_must_use_the_same_format(self) -> None:
        body = self._shared_input_manifest(second_format="toml")

        with self.assertRaises(ManifestError) as raised:
            self._load(body)

        self.assertEqual(raised.exception.code, "input_format")

    def test_input_files_cannot_alias_baseline_paths(self) -> None:
        body = self._manifest().replace(
            'baseline = "baselines/settings.json"', 'baseline = "global/model.json"'
        )
        with self.assertRaises(ManifestError) as raised:
            self._load(body)
        self.assertEqual(raised.exception.code, "path_overlap")

    def test_target_cannot_write_inside_source_or_input_roots(self) -> None:
        body = (
            self._manifest()
            .replace('target = "home"', f'target = "{self.root}"')
            .replace(
                'target = ".config/settings.json"', 'target = "repo/settings.json.j2"'
            )
        )
        with self.assertRaises(ManifestError):
            self._load(body)

    def test_roots_must_be_existing_distinct_safe_directories(self) -> None:
        body = self._manifest().replace('inputs = "inputs"', 'inputs = "repo"')
        with self.assertRaises(ManifestError):
            self._load(body)

        link = self.root / "home-link"
        link.symlink_to(self.target_root, target_is_directory=True)
        body = self._manifest().replace('target = "home"', 'target = "home-link"')
        with self.assertRaises(ManifestError):
            self._load(body)

    def test_directory_expansion_is_sorted_excludes_before_stat_and_has_unique_baselines(
        self,
    ) -> None:
        asset_root = self.source_root / "assets"
        (asset_root / "nested").mkdir(parents=True)
        (asset_root / "z.conf").write_text("z", encoding="utf-8")
        (asset_root / "nested/a.conf").write_text("a", encoding="utf-8")
        (asset_root / "skip.tmp").symlink_to(self.root / "missing-target")
        body = self._directory_manifest(exclude='"*.tmp"')

        manifest = self._load(body)

        self.assertEqual(
            [resource.source_name for resource in manifest.resources],
            ["assets/nested/a.conf", "assets/z.conf"],
        )
        self.assertEqual(
            [resource.name for resource in manifest.resources],
            ["assets:nested/a.conf", "assets:z.conf"],
        )
        self.assertTrue(all(resource.kind == "copy" for resource in manifest.resources))
        baselines = [resource.baseline for resource in manifest.resources]
        self.assertEqual(len(set(baselines)), 2)
        self.assertTrue(all(path.name.startswith("assets.leaf-") for path in baselines))
        verify_manifest(manifest)

    def test_directory_expansion_respects_nonrecursive_and_empty_directories(
        self,
    ) -> None:
        asset_root = self.source_root / "assets"
        (asset_root / "nested").mkdir(parents=True)
        (asset_root / "top.conf").write_text("top", encoding="utf-8")
        (asset_root / "nested/deep.conf").write_text("deep", encoding="utf-8")
        body = self._directory_manifest(recursive=False)
        manifest = self._load(body)
        self.assertEqual(
            [resource.source_name for resource in manifest.resources],
            ["assets/top.conf"],
        )
        self.assertFalse((self.target_root / ".config/assets").exists())

        empty = self.source_root / "empty"
        empty.mkdir()
        body = body.replace('source = "assets"', 'source = "empty"')
        manifest = self._load(body)
        self.assertEqual(manifest.resources, ())

    def test_empty_directory_declarations_still_reject_overlapping_scopes(self) -> None:
        (self.source_root / "assets").mkdir()
        (self.source_root / "other").mkdir()
        body = (
            self._directory_manifest().replace(
                'resources = ["assets"]', 'resources = ["assets", "other"]'
            )
            + """
[resources.other]
kind = "directory"
source = "other"
target = ".config/assets/nested"
baseline = "baselines/other"
content_sensitivity = "public"
scope = "whole-file"
owner = "source"
parents = "create"
parent_mode = "0700"
"""
        )
        with self.assertRaises(ManifestError) as raised:
            self._load(body)
        self.assertEqual(raised.exception.code, "path_overlap")

    def test_included_symlinked_directory_entry_is_rejected(self) -> None:
        asset_root = self.source_root / "assets"
        asset_root.mkdir()
        (asset_root / "bad-link").symlink_to(self.root / "missing-target")
        body = self._directory_manifest()
        with self.assertRaises(ManifestError):
            self._load(body)

    def test_target_parent_policy_and_input_optional_layer(self) -> None:
        body = self._manifest().replace(
            'target = ".config/settings.json"', 'target = ".new/settings.json"'
        )
        manifest = self._load(body)
        self.assertEqual(manifest.resources[0].parents, "require")
        self.assertFalse((self.target_root / ".new").exists())

        body = body.replace('parents = "require"', 'parents = "create"')
        manifest = self._load(body)
        self.assertEqual(manifest.resources[0].parents, "create")
        self.assertFalse((self.target_root / ".new").exists())

    def test_unsafe_live_paths_do_not_abort_manifest_loading(self) -> None:
        (self.inputs_root / "local/model.json").hardlink_to(
            self.inputs_root / "global/model.json"
        )
        outside = self.root / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        self.target_root.joinpath(".config").mkdir(parents=True, exist_ok=True)
        self.target_root.joinpath(".config/settings.json").symlink_to(outside)

        manifest = self._load()

        self.assertEqual(len(manifest.resources), 1)

    def test_rejects_traversal_invalid_globs_and_aliasing_field_paths(self) -> None:
        traversal = self._manifest().replace(
            'source = "settings.json.j2"', 'source = "../settings.json.j2"'
        )
        with self.assertRaises(ManifestError):
            self._load(traversal)

        overlapping_fields = self._manifest() + (
            '\n[resources.settings.fields.child]\npath = ["model", "nested"]\nowner = "source"\n'
        )
        with self.assertRaises(ManifestError) as raised:
            self._load(overlapping_fields)
        self.assertEqual(raised.exception.code, "field_overlap")

        invalid_glob = self._directory_manifest(exclude='"../secret"')
        with self.assertRaises(ManifestError):
            self._load(invalid_glob)

    def test_relationship_validation_normalizes_each_path_once(self) -> None:
        target_path = self.target_root / ".config/settings.json"
        with patch("luwu.manifest._path_key", wraps=_path_key) as normalize:
            self._load()

        target_key_calls = sum(
            call.args[0] == target_path for call in normalize.call_args_list
        )
        self.assertEqual(target_key_calls, 1)

    def test_freshness_detects_manifest_and_directory_changes_and_forgery(self) -> None:
        manifest = self._load()
        (self.inputs_root / "global/model.json").write_text(
            '{"model":"changed"}\n', encoding="utf-8"
        )
        (self.target_root / ".config/settings.json").write_text(
            '{"model":"target"}\n', encoding="utf-8"
        )
        (self.inputs_root / "baselines/settings.json").write_text(
            '{"model":"baseline"}\n', encoding="utf-8"
        )
        verify_manifest(manifest)

        (self.source_root / "settings.json.j2").write_text(
            '{"model":"source changed"}\n', encoding="utf-8"
        )
        # Direct source observations belong to the engine's per-resource
        # snapshots; verify_manifest only owns directory expansion freshness.
        verify_manifest(manifest)

        manifest = self._load()
        self.manifest_path.write_text(
            self._manifest().replace('target = "home"', 'target = "home"\n'),
            encoding="utf-8",
        )
        with self.assertRaises(ManifestError):
            verify_manifest(manifest)

        asset_root = self.source_root / "assets"
        asset_root.mkdir()
        (asset_root / "one.conf").write_text("one", encoding="utf-8")
        body = self._directory_manifest()
        manifest = self._load(body)
        (asset_root / "two.conf").write_text("two", encoding="utf-8")
        with self.assertRaises(ManifestError):
            verify_manifest(manifest)

    def test_verify_detects_directory_add_rename_and_remove(self) -> None:
        asset_root = self.source_root / "assets"
        asset_root.mkdir()
        (asset_root / "one.conf").write_text("one", encoding="utf-8")
        body = self._directory_manifest()

        manifest = self._load(body)
        added = asset_root / "two.conf"
        added.write_text("two", encoding="utf-8")
        with self.assertRaises(ManifestError):
            verify_manifest(manifest)

        manifest = self._load(body)
        renamed = asset_root / "renamed.conf"
        added.rename(renamed)
        with self.assertRaises(ManifestError):
            verify_manifest(manifest)

        manifest = self._load(body)
        renamed.unlink()
        with self.assertRaises(ManifestError):
            verify_manifest(manifest)

    def test_verify_detects_directory_leaf_identity_change(self) -> None:
        asset_root = self.source_root / "assets"
        asset_root.mkdir()
        leaf = asset_root / "one.conf"
        leaf.write_text("same bytes", encoding="utf-8")
        manifest = self._load(self._directory_manifest())

        replacement = asset_root / "replacement.conf"
        replacement.write_text("same bytes", encoding="utf-8")
        replacement.replace(leaf)

        with self.assertRaises(ManifestError):
            verify_manifest(manifest)

    def test_verify_detects_manifest_identity_change_with_same_bytes(self) -> None:
        manifest = self._load()
        replacement = self.root / "replacement.toml"
        replacement.write_bytes(self.manifest_path.read_bytes())
        self.assertNotEqual(
            (replacement.stat().st_dev, replacement.stat().st_ino),
            (self.manifest_path.stat().st_dev, self.manifest_path.stat().st_ino),
        )
        replacement.replace(self.manifest_path)

        with self.assertRaises(ManifestError):
            verify_manifest(manifest)

    def test_verify_does_not_reparse_or_reload_selected_resources(self) -> None:
        manifest = self._load()
        with (
            patch(
                "luwu.manifest._parse_manifest",
                side_effect=AssertionError("verify reparsed the manifest"),
            ),
            patch(
                "luwu.manifest.load_manifest",
                side_effect=AssertionError("verify reloaded all resources"),
            ),
        ):
            verify_manifest(manifest)

        loaded = self._load()
        mutable_copy = dataclasses.replace(loaded, inputs=dict(loaded.inputs))
        with self.assertRaises(ManifestError) as raised:
            verify_manifest(mutable_copy)
        self.assertEqual(raised.exception.code, "provenance")

        nested_mutation = self._load()
        replacement = dataclasses.replace(
            nested_mutation.resources[0],
            fields=dict(nested_mutation.resources[0].fields),
        )
        object.__setattr__(nested_mutation, "resources", (replacement,))
        with self.assertRaises(ManifestError) as raised:
            verify_manifest(nested_mutation)
        self.assertEqual(raised.exception.code, "provenance")

        altered = dataclasses.replace(loaded, profile="elsewhere")
        with self.assertRaises(ManifestError):
            verify_manifest(altered)

        malformed = dataclasses.replace(loaded, resources=("invalid",))
        with self.assertRaises(ManifestError):
            verify_manifest(malformed)

    def test_provenance_rejects_resigned_in_place_resource_mutations(self) -> None:
        target_mutation = self._load()
        resource = target_mutation.resources[0]
        object.__setattr__(resource, "target", resource.source)
        object.__setattr__(
            target_mutation, "_signature", _manifest_signature(target_mutation)
        )
        with self.assertRaises(ManifestError) as raised:
            verify_manifest(target_mutation)
        self.assertEqual(raised.exception.code, "provenance")

        owner_mutation = self._load()
        binding = owner_mutation.resources[0].fields["model"]
        object.__setattr__(binding, "owner", "source")
        object.__setattr__(
            owner_mutation, "_signature", _manifest_signature(owner_mutation)
        )
        with self.assertRaises(ManifestError) as raised:
            verify_manifest(owner_mutation)
        self.assertEqual(raised.exception.code, "provenance")

    def test_verify_manifest_does_not_reinspect_mutable_paths(self) -> None:
        manifest = self._load()
        outside = self.root / "outside.json"
        outside.write_text('{"safe":true}\n', encoding="utf-8")
        mutable_paths = (
            self.inputs_root / "global/model.json",
            self.target_root / ".config/settings.json",
            self.inputs_root / "baselines/settings.json",
        )
        for path in mutable_paths:
            path.unlink(missing_ok=True)
            path.symlink_to(outside)

        verify_manifest(manifest)

    def _nested_source_manifest(self) -> str:
        return self._manifest().replace(
            'source = "settings.json.j2"', 'source = "sub/settings.json.j2"'
        )

    def _assert_load_code(self, body: str, code: str) -> None:
        with self.assertRaises(ManifestError) as raised:
            self._load(body)
        self.assertEqual(raised.exception.code, code)

    def test_source_file_walk_reports_missing_unsafe_and_stale_by_code(self) -> None:
        body = self._nested_source_manifest()
        self._assert_load_code(body, "path_missing")

        sub = self.source_root / "sub"
        sub.mkdir()
        self._assert_load_code(body, "path_missing")

        (sub / "settings.json.j2").write_text("{}\n", encoding="utf-8")
        self._load(body)

        (sub / "twin").hardlink_to(sub / "settings.json.j2")
        self._assert_load_code(body, "path_boundary")
        (sub / "twin").unlink()

        (sub / "settings.json.j2").rename(self.root / "moved.j2")
        (self.root / "real-sub").mkdir()
        (self.root / "moved.j2").rename(self.root / "real-sub/settings.json.j2")
        sub.rmdir()
        sub.symlink_to(self.root / "real-sub")
        self._assert_load_code(body, "path_boundary")

    def test_source_file_walk_maps_a_race_to_a_stale_code(self) -> None:
        (self.source_root / "sub").mkdir()
        (self.source_root / "sub/settings.json.j2").write_text("{}\n")
        body = self._nested_source_manifest()
        for target, code in (
            ("open_child_directory", "path_stale"),
            ("open_child_file", "path_stale"),
        ):
            with (
                self.subTest(target=target),
                patch(f"luwu.manifest.{target}", side_effect=FileChangedError("race")),
            ):
                self._assert_load_code(body, code)

    def test_unreadable_entries_are_unsafe_not_stale(self) -> None:
        (self.source_root / "sub").mkdir()
        (self.source_root / "sub/settings.json.j2").write_text("{}\n")
        (self.source_root / "assets").mkdir()
        (self.source_root / "assets/a.conf").write_text("a")
        real_open = os.open

        def deny(name: str):
            def open_denied(path: str, flags: int, *, dir_fd: int | None = None) -> int:
                if path == name and dir_fd is not None:
                    raise PermissionError(errno.EACCES, "denied")
                return real_open(path, flags, dir_fd=dir_fd)

            return open_denied

        cases = (
            (self._nested_source_manifest(), "settings.json.j2", "path_boundary"),
            (self._directory_manifest(), "a.conf", "directory_boundary"),
        )
        for body, name, code in cases:
            with (
                self.subTest(code=code),
                patch("luwu.filesystem.os.open", deny(name)),
            ):
                self._assert_load_code(body, code)

        self.manifest_path.write_text(self._manifest(), encoding="utf-8")
        with (
            patch("luwu.filesystem.os.open", deny("luwu.toml")),
            self.assertRaises(ManifestError) as raised,
        ):
            load_manifest(self.manifest_path, profile="dev")
        self.assertEqual(raised.exception.code, "manifest_unreadable")

    def test_root_replacement_during_a_walk_is_a_root_stale_error(self) -> None:
        with patch("luwu.manifest.open_boundary", side_effect=RootChangedError("x")):
            self._assert_load_code(self._manifest(), "root_stale")

    def test_directory_walk_reports_missing_unsafe_and_stale_by_code(self) -> None:
        body = self._directory_manifest()
        self._assert_load_code(body, "path_missing")

        (self.root / "elsewhere").mkdir()
        (self.source_root / "assets").symlink_to(self.root / "elsewhere")
        self._assert_load_code(body, "directory_boundary")
        (self.source_root / "assets").unlink()

        assets = self.source_root / "assets"
        (assets / "nested").mkdir(parents=True)
        (assets / "nested/a.conf").write_text("a", encoding="utf-8")
        (assets / "twin.conf").write_text("t", encoding="utf-8")
        (assets / "nested/twin.conf").hardlink_to(assets / "twin.conf")
        self._assert_load_code(body, "directory_boundary")
        (assets / "nested/twin.conf").unlink()

        for target in ("open_child_directory", "open_child_file"):
            with (
                self.subTest(target=target),
                patch(f"luwu.manifest.{target}", side_effect=FileChangedError("race")),
            ):
                self._assert_load_code(body, "directory_stale")

    def test_manifest_file_must_be_a_single_linked_regular_file(self) -> None:
        real = self.root / "real.toml"
        real.write_text(self._manifest(), encoding="utf-8")
        for make in (
            lambda: self.manifest_path.symlink_to(real),
            lambda: self.manifest_path.hardlink_to(real),
        ):
            self.manifest_path.unlink(missing_ok=True)
            make()
            with self.assertRaises(ManifestError) as raised:
                load_manifest(self.manifest_path, profile="dev")
            self.assertEqual(raised.exception.code, "manifest_path")

        self.manifest_path.unlink()
        with self.assertRaises(ManifestError) as raised:
            load_manifest(self.manifest_path, profile="dev")
        self.assertEqual(raised.exception.code, "manifest_unreadable")

        self.manifest_path.write_text(self._manifest(), encoding="utf-8")
        with (
            patch(
                "luwu.manifest.read_stable_file", side_effect=FileChangedError("race")
            ),
            self.assertRaises(ManifestError) as raised,
        ):
            load_manifest(self.manifest_path, profile="dev")
        self.assertEqual(raised.exception.code, "manifest_stale")

    def test_source_file_parent_swapped_after_open_is_stale(self) -> None:
        sub = self.source_root / "sub"
        sub.mkdir()
        (sub / "settings.json.j2").write_text("{}\n", encoding="utf-8")
        real = manifest_module.open_child_file

        def open_then_swap_parent(parent: int, name: str, **kwargs):
            opened = real(parent, name, **kwargs)
            if name == "settings.json.j2":
                sub.rename(self.root / "kept-sub")
                sub.mkdir()
                (sub / "settings.json.j2").write_text("{}\n", encoding="utf-8")
            return opened

        with patch("luwu.manifest.open_child_file", open_then_swap_parent):
            self._assert_load_code(self._nested_source_manifest(), "path_stale")

    def test_directory_changed_while_it_is_listed_is_stale(self) -> None:
        assets = self.source_root / "assets"
        assets.mkdir()
        (assets / "a.conf").write_text("a", encoding="utf-8")
        real = manifest_module.open_child_file

        def open_then_add_entry(parent: int, name: str, **kwargs):
            opened = real(parent, name, **kwargs)
            (assets / "late.conf").write_text("late", encoding="utf-8")
            return opened

        with patch("luwu.manifest.open_child_file", open_then_add_entry):
            self._assert_load_code(self._directory_manifest(), "directory_stale")
