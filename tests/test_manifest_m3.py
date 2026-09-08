from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from luwu.errors import ManifestError
from luwu.manifest import load_manifest


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


class ManifestM3Tests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
