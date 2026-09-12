from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Self

from luwu.cli import main
from luwu.manifest import load_manifest


class M3bMutationTests(unittest.TestCase):
    def test_v4_manifest_is_explicit_and_reverse_mapping_is_frozen(self) -> None:
        with _Project() as project:
            resource = load_manifest(project.manifest).resources[0]
            self.assertEqual(resource.reverse_sync["runtime"], "runtime")
            with self.assertRaises(TypeError):
                resource.reverse_sync["new"] = "new"  # type: ignore[index]

    def test_accept_preview_does_not_create_baseline_and_confirmed_accept_writes_it(
        self,
    ) -> None:
        with _Project() as project:
            before = project.baseline.exists()
            preview = _invoke(
                [
                    "accept",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--from",
                    "desired",
                    "--field",
                    "setting",
                    "--json",
                ]
            )
            self.assertEqual(preview[0], 2)
            self.assertFalse(preview[1]["applied"])
            self.assertFalse(before or project.baseline.exists())

            applied, payload = _invoke(
                [
                    "accept",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--from",
                    "desired",
                    "--field",
                    "setting",
                    "--yes",
                    "--json",
                ]
            )
            self.assertEqual(applied, 0)
            self.assertTrue(payload["applied"])
            baseline = json.loads(project.baseline.read_text(encoding="utf-8"))
            self.assertEqual(baseline["values"], {"setting": 1})
            self.assertNotIn("runtime-value", json.dumps(payload))

    def test_reverse_sync_changes_only_selected_source_key_and_recalculates(
        self,
    ) -> None:
        with _Project() as project:
            project.write_baseline(
                {
                    "setting": 1,
                    "runtime": 1,
                }
            )
            before = project.source.read_bytes()
            preview, payload = _invoke(
                [
                    "reverse-sync",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--field",
                    "runtime",
                    "--json",
                ]
            )
            self.assertEqual(preview, 2)
            self.assertFalse(payload["applied"])
            self.assertEqual(project.source.read_bytes(), before)

            applied, payload = _invoke(
                [
                    "reverse-sync",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--field",
                    "runtime",
                    "--yes",
                    "--json",
                ]
            )
            self.assertEqual(applied, 0)
            self.assertEqual(
                json.loads(project.source.read_text(encoding="utf-8")),
                {"setting": 1, "runtime": 2, "undeclared": "source"},
            )
            self.assertEqual(
                payload["verification"]["resources"][0]["status"], "drifted"
            )

    def test_v4_apply_remains_read_only(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            code, payload = _invoke(
                [
                    "apply",
                    "--manifest",
                    str(project.manifest),
                    "--yes",
                    "--json",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["reason"], "m3_read_only")


def _invoke(arguments: list[str]) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
        code = main(arguments)
    return code, json.loads(stdout.getvalue())


class _Project:
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.source = self.root / "templates/settings.json.j2"
        self.target = self.root / "live/settings.json"
        self.baseline = self.root / "baseline.json"
        self.manifest = self.root / "luwu.toml"
        self.source.write_text(
            '{"setting": 1, "runtime": 1, "undeclared": "source"}',
            encoding="utf-8",
        )
        self.target.write_text(
            '{"setting": 1, "runtime": 2, "undeclared": "live"}',
            encoding="utf-8",
        )
        self.manifest.write_text(
            """version = 4

[resources.settings]
kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
baseline = "baseline.json"

[resources.settings.fields]
setting = "source"
runtime = "live"
undeclared = "ignore"

[resources.settings.reverse_sync]
format = "literal-json"

[resources.settings.reverse_sync.fields]
runtime = "runtime"
""",
            encoding="utf-8",
        )

    def write_baseline(self, values: dict[str, object]) -> None:
        self.baseline.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "resource": "settings",
                    "source": "templates/settings.json.j2",
                    "target": "live/settings.json",
                    "owners": {
                        "setting": "source",
                        "runtime": "live",
                        "undeclared": "ignore",
                    },
                    "values": values,
                }
            ),
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.directory.cleanup()


if __name__ == "__main__":
    unittest.main()
