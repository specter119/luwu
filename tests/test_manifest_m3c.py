from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from luwu.errors import ManifestError
from luwu.manifest import (
    EXECUTION_MANIFEST_VERSION,
    EXECUTION_RESOURCE_CAPABILITY,
    is_execution_manifest,
    load_manifest,
)


def _manifest(*, resources: str, extra: str = "") -> str:
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


class ManifestM3cTests(unittest.TestCase):
    def test_v5_exposes_a_stable_execution_capability_and_sorts_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "luwu.toml"
            manifest_path.write_text(
                _manifest(resources=_resource("zeta") + _resource("alpha")),
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
                _manifest(
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
                    _manifest(resources=_resource("settings", extra=f"\n{field}\n")),
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
                    _manifest(resources=resource), encoding="utf-8"
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
                    _manifest(resources=resource), encoding="utf-8"
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
                    _manifest(resources=resources), encoding="utf-8"
                )

                with self.assertRaises(ManifestError) as context:
                    load_manifest(manifest_path)

                self.assertEqual(context.exception.code, code)


if __name__ == "__main__":
    unittest.main()
