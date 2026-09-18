from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from luwu.errors import ManifestError
from luwu.manifest import (
    PROVIDER_MANIFEST_VERSION,
    is_provider_manifest,
    load_manifest,
)


def _manifest(*, extra_root: str = "", resource_extra: str = "") -> str:
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


class ManifestM4Tests(unittest.TestCase):
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
            manifest = load_manifest(self._write(Path(directory), _manifest()))
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
                body = _manifest().replace(
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
                body = _manifest().replace(
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
                body = _manifest()
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
                body = _manifest()
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
                body = _manifest().replace(
                    f"version = {PROVIDER_MANIFEST_VERSION}", f"version = {version}"
                )
                with self.assertRaises(ManifestError) as context:
                    load_manifest(self._write(Path(directory), body))
                self.assertIn(
                    context.exception.code,
                    {"manifest_unknown_field", "resource_unknown_field"},
                )


if __name__ == "__main__":
    unittest.main()
