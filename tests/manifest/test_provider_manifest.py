"""Manifest rules for provider declarations and provider-owned fields."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from luwu.errors import ManifestError
from luwu.manifest import load_manifest


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
        '[providers.openai]\ntype = "rbw"\nitem = "openai"\nfield = "api_key"\n'
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
            (provider.name, provider.item, provider.field),
            ("openai", "openai", "api_key"),
        )
        binding = resource.fields["key"]
        self.assertEqual(
            (binding.owner, binding.input, binding.provider),
            ("provider", None, "openai"),
        )
        self.assertEqual(resource.provider_fields, ("key",))
        self.assertIsNone(resource.fields["theme"].provider)
        self.assertEqual(resource.kind, "template")

    def test_provider_field_is_optional(self) -> None:
        providers = '[providers.openai]\ntype = "rbw"\nitem = "openai"\n'
        manifest = self._load(providers + self._RESOURCE)
        provider = manifest.providers["openai"]
        self.assertIsNone(provider.field)

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
        self.assertEqual(
            self._code(
                '[providers.openai]\ntype = "rbw"\nitem = "openai"\nprofile = "w"\n'
            ),
            "providers",
        )

    def test_provider_type_is_rbw_only(self) -> None:
        self.assertEqual(
            self._code('[providers.openai]\ntype = "vault"\nitem = "openai"\n'),
            "providers",
        )
        self.assertEqual(
            self._code('[providers.openai]\ntype = 1\nitem = "openai"\n'),
            "providers",
        )

    def test_provider_arguments_must_be_safe_argv_text(self) -> None:
        for line in (
            'item = "-rf"\n',
            'item = ""\n',
            "item = 1\n",
            'item = "openai"\nfield = "-d"\n',
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
