"""Tests for resource kind inference, deploy defaults, and directory expansion."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from luwu.errors import ManifestError
from luwu.manifest import load_manifest

_HEAD = """
version = 8

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[profiles.dev]
packages = ["all"]
exclude = []

[packages.all]
dependencies = []
resources = [{resources}]

[inputs.shell_name]
layers = [{{ file = "global/shell.json", path = ["name"], format = "json" }}]
{defaults}
"""


class ResourceKindTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "repo"
        for directory in (
            self.source / "tree/sub",
            self.root / "inputs/global",
            self.root / "inputs/baselines",
            self.root / "home",
        ):
            directory.mkdir(parents=True)
        (self.root / "inputs/global/shell.json").write_text(
            '{"name": "zsh"}\n', encoding="utf-8"
        )
        for name, body in {
            "plain.conf": "plain\n",
            "greeting.sh.j2": "echo {{ shell_name }}\n",
            "settings.json.j2": "{}\n",
            "notes.txt.j2": "x\n",
            "tree/a.conf": "a\n",
            "tree/sub/b.sh.j2": "echo b\n",
        }.items():
            (self.source / name).write_text(body, encoding="utf-8")

    def _load(self, resources: str, *, defaults: str = ""):
        names = ", ".join(
            f'"{line[len("[resources.") : -1]}"'
            for line in resources.splitlines()
            if line.startswith("[resources.") and line.count(".") == 1
        )
        body = _HEAD.format(resources=names, defaults=defaults) + resources
        path = self.root / "luwu.toml"
        path.write_text(body, encoding="utf-8")
        return load_manifest(path, profile="dev")

    def _code(self, resources: str, *, defaults: str = "") -> str:
        with self.assertRaises(ManifestError) as raised:
            self._load(resources, defaults=defaults)
        return raised.exception.code

    def test_plain_file_defaults_to_symlink_without_baseline(self) -> None:
        manifest = self._load(
            '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
        )
        (resource,) = manifest.resources
        self.assertEqual(
            (resource.kind, resource.baseline, resource.templated),
            ("symlink", None, False),
        )

    def test_deploy_copy_needs_a_baseline_and_a_symlink_refuses_one(self) -> None:
        copy = (
            '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
            'deploy = "copy"\n'
        )
        self.assertEqual(self._code(copy), "resource_schema")
        manifest = self._load(copy + 'baseline = "baselines/plain.json"\n')
        self.assertEqual(manifest.resources[0].kind, "copy")
        self.assertIsNotNone(manifest.resources[0].baseline)
        self.assertEqual(
            self._code(
                '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
                'baseline = "baselines/plain.json"\n'
            ),
            "resource_schema",
        )
        self.assertEqual(
            self._code(
                '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
                'mode = "0644"\n'
            ),
            "resource_schema",
        )

    def test_global_default_and_per_resource_override(self) -> None:
        resources = (
            '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
            'baseline = "baselines/plain.json"\n'
            '[resources.linked]\nsource = "tree/a.conf"\ntarget = "linked.conf"\n'
            'deploy = "symlink"\n'
        )
        manifest = self._load(resources, defaults='[defaults]\ndeploy = "copy"\n')
        kinds = {item.name: item.kind for item in manifest.resources}
        self.assertEqual(kinds, {"plain": "copy", "linked": "symlink"})

    def test_defaults_supply_parent_policy(self) -> None:
        plain = '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
        manifest = self._load(
            plain,
            defaults='[defaults]\nparents = "create"\nparent_mode = "0750"\n',
        )
        self.assertEqual(
            (manifest.resources[0].parents, manifest.resources[0].parent_mode),
            ("create", 0o750),
        )
        self.assertEqual(
            self._code(plain, defaults='[defaults]\ndeploy = "hardlink"\n'),
            "resource_deploy",
        )
        self.assertEqual(
            self._code(plain, defaults='[defaults]\nowner = "x"\n'), "defaults"
        )

    def test_j2_suffix_infers_a_whole_file_template(self) -> None:
        resources = (
            '[resources.greeting]\nsource = "greeting.sh.j2"\ntarget = "greeting.sh"\n'
            'baseline = "baselines/greeting.json"\ninputs = ["shell_name"]\n'
        )
        manifest = self._load(resources)
        (resource,) = manifest.resources
        self.assertEqual(
            (resource.kind, resource.templated, resource.input_names, resource.fields),
            ("copy", True, ("shell_name",), {}),
        )
        self.assertIn("shell_name", manifest.inputs)
        self.assertEqual(
            self._code(resources.replace('baseline = "baselines/greeting.json"\n', "")),
            "resource_schema",
        )
        self.assertEqual(
            self._code(resources + 'deploy = "symlink"\n'), "resource_deploy"
        )

    def test_kind_overrides_the_suffix_inference(self) -> None:
        raw = (
            '[resources.greeting]\nsource = "greeting.sh.j2"\ntarget = "greeting.sh"\n'
            'kind = "file"\n'
        )
        self.assertEqual(self._load(raw).resources[0].kind, "symlink")
        forced = (
            '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
            'kind = "template"\nbaseline = "baselines/plain.json"\n'
        )
        self.assertEqual(
            (
                self._load(forced).resources[0].kind,
                self._load(forced).resources[0].templated,
            ),
            ("copy", True),
        )
        self.assertEqual(
            self._code(raw.replace('"file"', '"directory"')), "resource_kind"
        )

    def test_fields_make_a_structured_template_with_inferred_format(self) -> None:
        fields = (
            'baseline = "baselines/settings.json"\n'
            "[resources.settings.fields.model]\n"
            'path = ["model"]\nowner = "source"\n'
        )
        head = '[resources.settings]\nsource = "settings.json.j2"\ntarget = "s.json"\n'
        resource = self._load(head + fields).resources[0]
        self.assertEqual((resource.kind, resource.format), ("template", "json"))
        self.assertEqual(
            self._code(head.replace("settings.json.j2", "notes.txt.j2") + fields),
            "format",
        )
        self.assertEqual(
            self._code(head + 'deploy = "copy"\n' + fields), "resource_deploy"
        )
        self.assertEqual(self._code(head + 'kind = "file"\n' + fields), "resource_kind")
        self.assertEqual(
            self._code(head + 'inputs = ["shell_name"]\n' + fields), "resource_schema"
        )

    def test_retired_resource_keys_are_rejected(self) -> None:
        base = '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
        for extra in ('scope = "whole-file"\n', 'owner = "source"\n'):
            with self.subTest(extra=extra):
                self.assertEqual(self._code(base + extra), "resource_schema")
        for kind in ("copy", "directory"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    self._code(base + f'kind = "{kind}"\n'), "resource_kind"
                )

    def test_directory_expands_into_per_file_leaves(self) -> None:
        resources = (
            '[resources.tree]\nsource = "tree"\ntarget = ".config/tree"\n'
            'baseline = "baselines/tree"\nparents = "create"\n'
        )
        manifest = self._load(resources)
        leaves = {
            item.name: (item.kind, item.target_name, item.templated)
            for item in manifest.resources
        }
        self.assertEqual(
            leaves,
            {
                "tree:a.conf": ("symlink", ".config/tree/a.conf", False),
                "tree:sub/b.sh.j2": ("copy", ".config/tree/sub/b.sh", True),
            },
        )
        by_name = {item.name: item for item in manifest.resources}
        self.assertIsNone(by_name["tree:a.conf"].baseline)
        self.assertIsNotNone(by_name["tree:sub/b.sh.j2"].baseline)
        self.assertEqual(
            self._code(resources.replace('baseline = "baselines/tree"\n', "")),
            "resource_schema",
        )

    def test_exclude_and_recursive_apply_only_to_directories(self) -> None:
        base = '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
        self.assertEqual(self._code(base + 'exclude = ["*.tmp"]\n'), "directory_schema")
        self.assertEqual(self._code(base + "recursive = false\n"), "directory_schema")

    def test_undeclared_and_public_names_apply_to_structured_templates(self) -> None:
        fields = (
            'baseline = "baselines/settings.json"\n'
            "[resources.settings.fields.model]\n"
            'path = ["model"]\nowner = "source"\n'
        )
        head = '[resources.settings]\nsource = "settings.json.j2"\ntarget = "s.json"\n'
        resource = self._load(
            head + 'undeclared = "preserve"\npublic_names = ["env_key"]\n' + fields
        ).resources[0]
        self.assertEqual(
            (resource.undeclared, resource.public_names),
            ("preserve", frozenset({"env_key"})),
        )
        self.assertEqual(self._load(head + fields).resources[0].undeclared, "block")
        self.assertEqual(
            self._code(head + 'undeclared = "ignore"\n' + fields), "resource_undeclared"
        )
        plain = '[resources.plain]\nsource = "plain.conf"\ntarget = "plain.conf"\n'
        self.assertEqual(
            self._code(plain + 'undeclared = "preserve"\n'), "resource_undeclared"
        )
        self.assertEqual(
            self._code(plain + 'public_names = ["env_key"]\n'), "resource_schema"
        )

    def test_input_public_names_admit_a_sensitive_layer_path(self) -> None:
        plain = (
            '[resources.plain]\nsource = "greeting.sh.j2"\ntarget = "greeting.sh"\n'
            'baseline = "baselines/greeting.json"\ninputs = ["shell_name"]\n'
        )

        def load(extra: str):
            body = _HEAD.format(resources='"plain"', defaults=extra)
            path = self.root / "luwu.toml"
            path.write_text(
                body.replace('path = ["name"]', 'path = ["env_key"]') + plain,
                encoding="utf-8",
            )
            return load_manifest(path, profile="dev")

        with self.assertRaises(ManifestError):
            load("")
        manifest = load('public_names = ["env_key"]\n')
        self.assertEqual(
            manifest.inputs["shell_name"].public_names, frozenset({"env_key"})
        )


class ProviderFieldManifestTests(unittest.TestCase):
    """Provider declarations and provider-owned fields on structured templates."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "repo"
        for directory in (
            self.source / "tree",
            self.root / "inputs/global",
            self.root / "inputs/baselines",
            self.root / "home",
        ):
            directory.mkdir(parents=True)
        (self.source / "opencode.json.j2").write_text("{}\n", encoding="utf-8")
        (self.source / "notes.txt").write_text("notes\n", encoding="utf-8")
        (self.source / "tree" / "leaf.conf").write_text("leaf\n", encoding="utf-8")

    _PROVIDERS = (
        "[providers.openai]\n"
        'type = "rbw"\n'
        'item = "openai"\n'
        'field = "api_key"\n'
        'profile = "work"\n'
    )
    _RESOURCE = (
        "[resources.opencode]\n"
        'source = "opencode.json.j2"\n'
        'target = ".config/opencode/opencode.json"\n'
        'format = "json"\n'
        'baseline = "baselines/opencode.json"\n'
        "mode = 0o600\n"
        "[resources.opencode.fields.theme]\n"
        'path = ["theme"]\n'
        'owner = "source"\n'
        "[resources.opencode.fields.key]\n"
        'path = ["provider", "openai", "options", "apiKey"]\n'
        'owner = "provider"\n'
        'provider = "openai"\n'
    )

    def _load(self, body: str):
        if "[resources." not in body:
            body = body + self._RESOURCE
        path = self.root / "luwu.toml"
        path.write_text(
            """
version = 8

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[profiles.dev]
packages = ["all"]
exclude = []

[packages.all]
dependencies = []
resources = ["opencode"]
"""
            + body,
            encoding="utf-8",
        )
        return load_manifest(path, profile="dev")

    def _code(self, body: str) -> str:
        with self.assertRaises(ManifestError) as raised:
            self._load(body)
        return raised.exception.code

    def test_provider_declaration_and_field_round_trip(self) -> None:
        manifest = self._load(self._PROVIDERS + self._RESOURCE)
        (resource,) = manifest.resources
        provider = manifest.providers["openai"]
        self.assertEqual(
            (provider.name, provider.item, provider.field, provider.profile),
            ("openai", "openai", "api_key", "work"),
        )
        binding = resource.fields["key"]
        self.assertEqual(
            (binding.owner, binding.input, binding.provider),
            ("provider", None, "openai"),
        )
        self.assertEqual(resource.provider_fields, ("key",))
        self.assertIsNone(resource.fields["theme"].provider)
        self.assertEqual(resource.kind, "template")

    def test_provider_field_and_profile_are_optional(self) -> None:
        providers = '[providers.openai]\ntype = "rbw"\nitem = "openai"\n'
        manifest = self._load(providers + self._RESOURCE)
        provider = manifest.providers["openai"]
        self.assertIsNone(provider.field)
        self.assertIsNone(provider.profile)

    def test_provider_table_shape_and_keys_are_closed(self) -> None:
        path = self.root / "luwu.toml"
        path.write_text(
            """
version = 8
providers = "openai"

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[profiles.dev]
packages = ["all"]
exclude = []

[packages.all]
dependencies = []
resources = []

[resources.placeholder]
source = "notes.txt"
target = "notes.txt"
""",
            encoding="utf-8",
        )
        with self.assertRaises(ManifestError) as raised:
            load_manifest(path, profile="dev")
        self.assertEqual(raised.exception.code, "providers")
        self.assertEqual(self._code('[providers]\nopenai = "x"\n'), "providers")
        self.assertEqual(
            self._code(
                '[providers.openai]\ntype = "rbw"\nitem = "openai"\nextra = 1\n'
            ),
            "providers",
        )
        self.assertEqual(
            self._code('[providers.openai]\nitem = "openai"\n'), "providers"
        )
        self.assertEqual(self._code('[providers.openai]\ntype = "rbw"\n'), "providers")
        self.assertEqual(
            self._code('[providers.1bad]\ntype = "rbw"\nitem = "openai"\n'),
            "providers",
        )

    def test_provider_type_is_rbw_only(self) -> None:
        self.assertEqual(
            self._code('[providers.openai]\ntype = "vault"\nitem = "openai"\n'),
            "provider_type",
        )
        self.assertEqual(
            self._code('[providers.openai]\ntype = 1\nitem = "openai"\n'),
            "provider_type",
        )

    def test_provider_arguments_must_be_safe_argv_text(self) -> None:
        for line in (
            'item = "-rf"\n',
            'item = ""\n',
            "item = 1\n",
            'item = "openai"\nfield = "-d"\n',
            'item = "openai"\nprofile = "bad\\tname"\n',
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    self._code(f'[providers.openai]\ntype = "rbw"\n{line}'),
                    "provider_value",
                )

    def test_duplicate_provider_names_are_rejected(self) -> None:
        body = (
            '[providers.openai]\ntype = "rbw"\nitem = "one"\n'
            '[providers.openai]\ntype = "rbw"\nitem = "two"\n'
        )
        self.assertEqual(self._code(body), "manifest_toml")

    def test_field_provider_reference_rules(self) -> None:
        resource = self._RESOURCE.replace('provider = "openai"', 'provider = "other"')
        self.assertEqual(self._code(self._PROVIDERS + resource), "field_provider")
        self.assertEqual(
            self._code(
                self._PROVIDERS + self._RESOURCE.replace('provider = "openai"\n', "")
            ),
            "field_provider",
        )
        self.assertEqual(
            self._code(
                self._PROVIDERS
                + self._RESOURCE.replace('provider = "openai"', 'provider = "1bad"')
            ),
            "field_provider",
        )
        non_provider = self._RESOURCE.replace(
            'path = ["theme"]\nowner = "source"',
            'path = ["theme"]\nowner = "source"\nprovider = "openai"',
        )
        self.assertEqual(self._code(self._PROVIDERS + non_provider), "field_provider")

    def test_provider_fields_cannot_use_input_bindings(self) -> None:
        resource = self._RESOURCE.replace(
            'provider = "openai"', 'provider = "openai"\ninput = "shell_name"'
        )
        self.assertEqual(self._code(self._PROVIDERS + resource), "field_input")

    def test_provider_field_requires_a_private_mode(self) -> None:
        self.assertEqual(
            self._code(self._PROVIDERS + self._RESOURCE.replace("mode = 0o600\n", "")),
            "resource_mode",
        )
        for mode in ("0o644", "0o660", "0o607"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    self._code(
                        self._PROVIDERS
                        + self._RESOURCE.replace("mode = 0o600", f"mode = {mode}")
                    ),
                    "resource_mode",
                )
        manifest = self._load(
            self._PROVIDERS + self._RESOURCE.replace("mode = 0o600", "mode = 0o400")
        )
        self.assertEqual(manifest.resources[0].mode, 0o400)

    def test_provider_paths_share_the_existing_overlap_rule(self) -> None:
        prefix = self._RESOURCE.replace(
            'path = ["provider", "openai", "options", "apiKey"]',
            'path = ["theme", "accent"]',
        )
        self.assertEqual(self._code(self._PROVIDERS + prefix), "field_overlap")
        identical = self._RESOURCE.replace(
            'path = ["provider", "openai", "options", "apiKey"]',
            'path = ["theme"]',
        )
        self.assertEqual(self._code(self._PROVIDERS + identical), "field_overlap")

    def test_provider_paths_may_name_secret_locations(self) -> None:
        manifest = self._load(self._PROVIDERS + self._RESOURCE)
        self.assertEqual(
            manifest.resources[0].fields["key"].path,
            ("provider", "openai", "options", "apiKey"),
        )
        public_owner = self._RESOURCE.replace('owner = "provider"', 'owner = "source"')
        self.assertEqual(self._code(self._PROVIDERS + public_owner), "field_path")

    def test_only_structured_templates_have_provider_fields(self) -> None:
        plain = (
            '[resources.opencode]\nsource = "notes.txt"\ntarget = "notes.txt"\n'
            'baseline = "baselines/notes.json"\ndeploy = "copy"\n'
        )
        manifest = self._load(plain)
        self.assertEqual(manifest.resources[0].fields, {})
        self.assertEqual(manifest.resources[0].provider_fields, ())
        whole_file_template = plain.replace(
            'source = "notes.txt"', 'source = "opencode.json.j2"'
        ).replace('deploy = "copy"\n', "")
        templated = self._load(whole_file_template).resources[0]
        self.assertEqual((templated.kind, templated.templated), ("copy", True))
        self.assertEqual(templated.provider_fields, ())
        directory = (
            plain.replace('source = "notes.txt"', 'source = "tree"')
            .replace('target = "notes.txt"', 'target = ".config/tree"')
            .replace('baseline = "baselines/notes.json"', 'baseline = "baselines/tree"')
        )
        leaves = self._load(directory).resources
        self.assertEqual([leaf.name for leaf in leaves], ["opencode:leaf.conf"])
        self.assertEqual(leaves[0].provider_fields, ())
        structured_directory = self._RESOURCE.replace(
            'source = "opencode.json.j2"', 'source = "tree"'
        ).replace(
            'target = ".config/opencode/opencode.json"', 'target = ".config/tree"'
        )
        self.assertEqual(
            self._code(self._PROVIDERS + structured_directory), "resource_kind"
        )

    def test_provider_declaration_validation_covers_unselected_resources(self) -> None:
        body = self._PROVIDERS + self._RESOURCE.replace(
            "[resources.opencode]", "[resources.other]"
        ).replace("[resources.opencode.fields.", "[resources.other.fields.")

        def load(body: str):
            path = self.root / "luwu.toml"
            path.write_text(
                """
version = 8

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[profiles.dev]
packages = ["all"]
exclude = []

[packages.all]
dependencies = []
resources = []

[resources.placeholder]
source = "notes.txt"
target = "notes.txt"
"""
                + body,
                encoding="utf-8",
            )
            return load_manifest(path, profile="dev")

        manifest = load(body)
        self.assertEqual(manifest.resources, ())
        self.assertIn("openai", manifest.providers)
        with self.assertRaises(ManifestError) as raised:
            load(body.replace('provider = "openai"', 'provider = "ghost"'))
        self.assertEqual(raised.exception.code, "field_provider")


if __name__ == "__main__":
    unittest.main()
