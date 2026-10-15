"""Tests for preview and confirm."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from luwu.cli import main
from tests.support.projects import CliTemplateProject, ExecutionCliProject

ROOT = Path(__file__).resolve().parents[2]


class PreviewAndConfirmTests(unittest.TestCase):
    def test_unconfirmed_apply_is_a_preview_and_does_not_write(self) -> None:
        with CliTemplateProject() as project:
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
        with CliTemplateProject() as project:
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
        with CliTemplateProject() as project:
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
        with CliTemplateProject() as project:
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
        with ExecutionCliProject() as project:
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


class QuickstartLoopTests(unittest.TestCase):
    def test_readme_quickstart_fixture_plans_applies_and_inspects_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            demo = Path(directory)
            shutil.copytree(
                ROOT / "tests" / "fixtures" / "quickstart_v1_template",
                demo,
                dirs_exist_ok=True,
            )
            manifest = str(demo / "luwu.toml")
            results = {}
            for name, argv in (
                ("plan", ["plan", "--manifest", manifest]),
                ("apply", ["apply", "--manifest", manifest, "--yes"]),
                ("inspect", ["inspect", "--manifest", manifest]),
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    results[name] = (main(argv), stdout.getvalue())

            self.assertEqual({code for code, _ in results.values()}, {0})
            self.assertIn("action: create", results["plan"][1])
            self.assertIn("Verification: clean", results["apply"][1])
            self.assertIn("status: in_sync", results["inspect"][1])
