from __future__ import annotations

import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from luwu import mutations
from luwu.cli import main
from luwu.reconcile import build_plan, execute_execution_plan
from tests.test_m3b import _Project
from tests.test_m3c_execution import _ExecutionProject


class M3FollowupCliTests(unittest.TestCase):
    def test_lock_target_collision_reports_zero_write(self) -> None:
        with _ExecutionProject(("alpha",), targets={"alpha": None}) as project:
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
            with self.subTest(as_json=as_json), _Project() as project:
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
        with _ExecutionProject(("alpha",), targets={"alpha": None}) as project:
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
        with _Project() as project:
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
