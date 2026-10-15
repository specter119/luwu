"""Tests for json and error output."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Self
from unittest.mock import patch

from luwu import mutations
from luwu.cli import main
from luwu.errors import MutationError
from luwu.reconcile import build_plan, execute_execution_plan
from tests.support.projects import CliTemplateProject, ExecutionProject, FieldProject


class _MutationCliProject:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.secret_value = "live-value"
        self.secret_hash = hashlib.sha256(self.secret_value.encode()).hexdigest()
        self.target = self.root / "live/settings.json"
        (self.root / "templates/settings.json.j2").write_text(
            '{"setting": 1, "runtime": "live-value"}\n',
            encoding="utf-8",
        )
        self.target.write_text(
            '{"setting": 1, "runtime": "live-value"}\n',
            encoding="utf-8",
        )
        self.baseline_path = self.root / "baseline.json"
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text(
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

[resources.settings.reverse_sync]
format = "literal-json"

[resources.settings.reverse_sync.fields]
runtime = "runtime"
""",
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()


class ErrorOutputTests(unittest.TestCase):
    def test_human_manifest_errors_escape_control_characters(self) -> None:
        with CliTemplateProject() as project:
            missing = project.root / "missing\n\x1b[31mmanifest"
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                exit_code = main(["inspect", "--manifest", str(missing)])

            output = stderr.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertNotIn("\x1b", output)
            self.assertIn("\\x0a", output)
            self.assertIn("\\x1b", output)
            self.assertEqual(output.count("\n"), 1)

    def test_confirmed_accept_verification_failure_is_cli_failure_without_values(
        self,
    ) -> None:
        with _MutationCliProject() as project:
            stdout = io.StringIO()
            with (
                patch(
                    "luwu.mutations._verify",
                    side_effect=RuntimeError("verification unavailable"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "accept",
                        "--manifest",
                        str(project.manifest_path),
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

            output = stdout.getvalue()
            payload = json.loads(output)
            self.assertEqual(exit_code, 2)
            self.assertTrue(payload["applied"])
            self.assertEqual(payload["outcome"], "committed_but_verification_failed")
            self.assertEqual(payload["write"], "baseline.json")
            self.assertNotIn(project.secret_value, output)
            self.assertNotIn(project.secret_hash, output)

    def test_confirmed_reverse_sync_verification_failure_is_cli_failure(self) -> None:
        with _MutationCliProject() as project:
            changed_value = "changed-live-value"
            project.target.write_text(
                f'{{"setting": 1, "runtime": "{changed_value}"}}\n',
                encoding="utf-8",
            )
            project.baseline_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "resource": "settings",
                        "source": "templates/settings.json.j2",
                        "target": "live/settings.json",
                        "owners": {"setting": "source", "runtime": "live"},
                        "values": {"setting": 1, "runtime": "live-value"},
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with (
                patch(
                    "luwu.mutations._verify",
                    side_effect=RuntimeError("verification unavailable"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "reverse-sync",
                        "--manifest",
                        str(project.manifest_path),
                        "--resource",
                        "settings",
                        "--field",
                        "runtime",
                        "--yes",
                        "--json",
                    ]
                )

            output = stdout.getvalue()
            payload = json.loads(output)
            self.assertEqual(exit_code, 2)
            self.assertTrue(payload["applied"])
            self.assertEqual(payload["outcome"], "committed_but_verification_failed")
            self.assertEqual(payload["write"], "templates/settings.json.j2")
            for value in ("live-value", changed_value):
                self.assertNotIn(value, output)
                self.assertNotIn(
                    hashlib.sha256(value.encode()).hexdigest(),
                    output,
                )

    def test_confirmed_unknown_mutation_error_preserves_metadata_in_json_and_human(
        self,
    ) -> None:
        with _MutationCliProject() as project:
            error = MutationError(
                "baseline write state could not be confirmed",
                code="baseline_state_unknown",
                committed=True,
                outcome="committed_state_unknown",
            )
            json_stdout = io.StringIO()
            with (
                patch("luwu.mutations.write_baseline", side_effect=error),
                redirect_stdout(json_stdout),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "accept",
                        "--manifest",
                        str(project.manifest_path),
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

            payload = json.loads(json_stdout.getvalue())
            details = payload["error"]
            self.assertEqual(exit_code, 2)
            self.assertEqual(details["code"], "baseline_state_unknown")
            self.assertTrue(details["committed"])
            self.assertEqual(details["outcome"], "committed_state_unknown")
            self.assertEqual(
                {
                    "operation": details["operation"],
                    "resource": details["resource"],
                    "fields": details["fields"],
                    "write": details["write"],
                },
                {
                    "operation": "accept",
                    "resource": "settings",
                    "fields": ["setting"],
                    "write": "baseline.json",
                },
            )
            self.assertNotIn(project.secret_value, json_stdout.getvalue())
            self.assertNotIn(project.secret_hash, json_stdout.getvalue())

            human_stdout = io.StringIO()
            human_stderr = io.StringIO()
            with (
                patch("luwu.mutations.write_baseline", side_effect=error),
                redirect_stdout(human_stdout),
                redirect_stderr(human_stderr),
            ):
                self.assertEqual(
                    main(
                        [
                            "accept",
                            "--manifest",
                            str(project.manifest_path),
                            "--resource",
                            "settings",
                            "--from",
                            "desired",
                            "--field",
                            "setting",
                            "--yes",
                        ]
                    ),
                    2,
                )
            human = human_stderr.getvalue()
            self.assertIn("committed=true", human)
            self.assertIn("outcome=committed_state_unknown", human)
            self.assertIn("operation=accept", human)
            self.assertIn("resource=settings", human)
            self.assertIn("fields=setting", human)
            self.assertIn("write=baseline.json", human)
            self.assertNotIn(project.secret_value, human)
            self.assertNotIn(project.secret_hash, human)


class RecoveryRegressionCliTests(unittest.TestCase):
    def test_lock_target_collision_reports_zero_write(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": None}) as project:
            project.manifest_path.write_text(
                project.manifest_path.read_text().replace(
                    "targets/alpha.conf", ".journal.json.luwu-lock"
                )
            )
            journal = project.root / "journal.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--record",
                        str(journal),
                        "--yes",
                        "--json",
                    ]
                )
            self.assertEqual(status, 2)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["error"]["code"], "record_path_conflict")
            self.assertFalse(payload["journal"]["created"])
            self.assertFalse(journal.exists())
            self.assertFalse((project.root / ".journal.json.luwu-lock").exists())

    def test_stale_baseline_is_redacted_in_json_and_human_errors(self) -> None:
        real_write = mutations._write_source
        for as_json in (False, True):
            with self.subTest(as_json=as_json), FieldProject() as project:
                project.write_baseline({"setting": 1, "runtime": 1})
                original_source = project.source.read_bytes()
                baseline_digest = hashlib.sha256(
                    project.baseline.read_bytes()
                ).hexdigest()

                def write(*args, **kwargs):
                    project.write_baseline({"setting": 1, "runtime": "private-marker"})
                    return real_write(*args, **kwargs)

                stdout, stderr = io.StringIO(), io.StringIO()
                arguments = [
                    "reverse-sync",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--field",
                    "runtime",
                    "--yes",
                ]
                if as_json:
                    arguments.append("--json")
                with (
                    patch("luwu.mutations._write_source", side_effect=write),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    status = main(arguments)
                output = stdout.getvalue() + stderr.getvalue()
                self.assertEqual(status, 2)
                self.assertEqual(project.source.read_bytes(), original_source)
                self.assertIn("stale_plan", output)
                self.assertNotIn("private-marker", output)
                self.assertNotIn(baseline_digest, output)
                if as_json:
                    self.assertFalse(
                        json.loads(stdout.getvalue())["error"]["committed"]
                    )

    def test_recovery_drift_returns_failure_without_writes(self) -> None:
        with ExecutionProject(("alpha",), targets={"alpha": None}) as project:
            journal = project.root / "journal.json"
            execute_execution_plan(build_plan(project.manifest), journal, confirm=True)
            target = project.targets["alpha"]
            info = target.stat()
            target.write_bytes(b"x" * info.st_size)
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            before = (target.read_bytes(), journal.read_bytes())
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = main(["recover", "--record", str(journal), "--json"])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 2)
            self.assertEqual(payload["outcome"], "recovery_required")
            self.assertEqual(payload["resources"][0]["plan_status"], "drifted")
            self.assertEqual((target.read_bytes(), journal.read_bytes()), before)
            self.assertNotIn("x" * info.st_size, stdout.getvalue())

    def test_post_commit_baseline_change_reports_known_write_in_json(self) -> None:
        with FieldProject() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            real_replace = os.replace

            def replace(*args, **kwargs):
                real_replace(*args, **kwargs)
                project.write_baseline({"setting": 1, "runtime": "private-marker"})

            stdout = io.StringIO()
            with (
                patch("luwu.mutations.os.replace", side_effect=replace),
                redirect_stdout(stdout),
            ):
                status = main(
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
            error = json.loads(stdout.getvalue())["error"]
            self.assertEqual(status, 2)
            self.assertTrue(error["committed"])
            self.assertEqual(error["outcome"], "committed_state_unknown")
            self.assertEqual(error["write"], "templates/settings.json.j2")
            self.assertEqual(json.loads(project.source.read_text())["runtime"], 2)
            self.assertNotIn("private-marker", stdout.getvalue())
