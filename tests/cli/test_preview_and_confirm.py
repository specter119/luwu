"""Tests for preview and confirm."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Self

from luwu.cli import main


class _CliProject:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        (self.root / "templates/settings.conf.j2").write_text(
            'profile = "{{ profile }}"\n',
            encoding="utf-8",
        )
        self.target = self.root / "live/settings.conf"
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text(
            """version = 1

[resources.settings]
source = "templates/settings.conf.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
variables_sensitivity = "public"

[resources.settings.variables]
profile = "developer"
""",
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()


class _ExecutionCliProject:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "sources").mkdir()
        (self.root / "targets").mkdir()
        self.source_value = "public-execution-value\n"
        self.sources: dict[str, Path] = {}
        self.targets: dict[str, Path] = {}
        resources = []
        for name in ("zeta", "alpha"):
            source = self.root / "sources" / f"{name}.conf.j2"
            target = self.root / "targets" / f"{name}.conf"
            source.write_text(self.source_value, encoding="utf-8")
            self.sources[name] = source
            self.targets[name] = target
            resources.append(
                f"""[resources.{name}]
kind = "template"
source = "sources/{name}.conf.j2"
target = "targets/{name}.conf"
owner = "source"
scope = "whole-file"
content_sensitivity = "public"
"""
            )
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text(
            "version = 5\n\n" + "\n".join(resources), encoding="utf-8"
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()


class PreviewAndConfirmTests(unittest.TestCase):
    def test_unconfirmed_apply_is_a_preview_and_does_not_write(self) -> None:
        with _CliProject() as project:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["apply", "--manifest", str(project.manifest_path)])

            self.assertEqual(exit_code, 2)
            self.assertFalse(project.target.exists())
            self.assertIn("No files changed", stderr.getvalue())
            self.assertIn("Apply preview", stdout.getvalue())

    def test_confirmed_json_apply_reports_post_apply_verification_without_values(
        self,
    ) -> None:
        with _CliProject() as project:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--yes",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["applied"])
            self.assertEqual(payload["changed_targets"], ["live/settings.conf"])
            self.assertEqual(payload["verification"]["summary"]["changes"], 0)
            self.assertNotIn("developer", stdout.getvalue())
            self.assertEqual(project.target.read_bytes(), b'profile = "developer"\n')

    def test_confirmed_human_apply_prints_plan_before_success(self) -> None:
        with _CliProject() as project:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--yes",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertLess(output.index("Apply plan"), output.index("Applied"))

    def test_plan_json_is_metadata_only_and_read_only(self) -> None:
        with _CliProject() as project:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "plan",
                        "--manifest",
                        str(project.manifest_path),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["resources"][0]["status"], "missing")
            self.assertFalse(project.target.exists())
            self.assertNotIn("developer", stdout.getvalue())

    def test_v5_apply_preview_does_not_create_a_journal(self) -> None:
        with _ExecutionCliProject() as project:
            record_path = project.root / "journal.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["state"], "preview")
            self.assertEqual(payload["outcome"], "preview")
            self.assertFalse(payload["journal"]["created"])
            self.assertEqual(
                payload["target_names"], ["targets/alpha.conf", "targets/zeta.conf"]
            )
            self.assertFalse(project.targets["alpha"].exists())
            self.assertFalse(record_path.exists())
            self.assertNotIn(project.source_value, stdout.getvalue())
