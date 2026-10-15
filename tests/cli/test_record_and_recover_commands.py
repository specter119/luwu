"""Tests for record and recover commands."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Self
from unittest.mock import patch

from luwu.cli import main


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


class RecordAndRecoverCommandTests(unittest.TestCase):
    def test_v5_plan_identifies_execution_capability_and_write_impact(self) -> None:
        with _ExecutionCliProject() as project:
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
            self.assertEqual(payload["apply_block_reason"], "execution_required")
            self.assertFalse(payload["applyable"])
            self.assertEqual(
                payload["resources"][0]["impact"]["writes"],
                ["targets/alpha.conf"],
            )
            self.assertNotIn("deferred", payload["resources"][0]["impact"])
            self.assertFalse(project.targets["alpha"].exists())

    def test_v5_confirmed_apply_requires_record_without_writing(self) -> None:
        with _ExecutionCliProject() as project:
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
            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["error"]["code"], "record_required")
            self.assertEqual(
                payload["error"]["message"],
                "version 5 apply with --yes requires explicit --record PATH",
            )
            self.assertFalse(project.targets["alpha"].exists())
            self.assertNotIn(project.source_value, stdout.getvalue())

    def test_v5_confirmed_apply_reports_metadata_and_writes_targets(self) -> None:
        with _ExecutionCliProject() as project:
            record_path = project.root / "journal.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--record",
                        str(record_path),
                        "--yes",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["journal"]["created"])
            self.assertEqual(payload["state"], "committed")
            self.assertEqual(payload["outcome"], "committed")
            self.assertEqual(payload["journal"]["state"], "committed")
            self.assertEqual(
                payload["changed_targets"],
                ["targets/alpha.conf", "targets/zeta.conf"],
            )
            self.assertEqual(project.targets["alpha"].read_text(), project.source_value)
            self.assertEqual(project.targets["zeta"].read_text(), project.source_value)
            self.assertTrue(record_path.exists())
            self.assertNotIn(project.source_value, stdout.getvalue())

    def test_v5_failed_apply_reports_recovery_journal_metadata(self) -> None:
        with _ExecutionCliProject() as project:
            record_path = project.root / "journal.json"
            stdout = io.StringIO()
            with (
                patch(
                    "luwu.reconcile.sync_directory",
                    side_effect=[None, OSError("directory fsync unavailable")],
                ),
                redirect_stdout(stdout),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--record",
                        str(record_path),
                        "--yes",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["error"]["code"], "recovery_required")
            self.assertTrue(payload["journal"]["created"])
            self.assertEqual(payload["journal"]["state"], "recovery_required")
            self.assertEqual(
                payload["journal"]["resources"],
                [
                    {"name": "alpha", "state": "committed"},
                    {"name": "zeta", "state": "unknown"},
                ],
            )
            self.assertNotIn(project.source_value, stdout.getvalue())

    def test_record_inspect_is_read_only_and_metadata_only(self) -> None:
        with _ExecutionCliProject() as project:
            record_path = project.root / "journal.json"
            main(
                [
                    "apply",
                    "--manifest",
                    str(project.manifest_path),
                    "--record",
                    str(record_path),
                    "--yes",
                ]
            )
            record_before = record_path.read_bytes()
            target_before = project.targets["alpha"].read_bytes()
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    ["record-inspect", "--record", str(record_path), "--json"]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["state"], "committed")
            self.assertEqual(payload["outcome"], "committed")
            self.assertEqual(
                payload["target_names"], ["targets/alpha.conf", "targets/zeta.conf"]
            )
            self.assertEqual(record_path.read_bytes(), record_before)
            self.assertEqual(project.targets["alpha"].read_bytes(), target_before)
            self.assertNotIn(project.source_value, stdout.getvalue())

            human = io.StringIO()
            with redirect_stdout(human), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["record-inspect", "--record", str(record_path)]), 0
                )
            self.assertIn("State: committed", human.getvalue())

    def test_recover_reobserves_committed_record_without_writing(self) -> None:
        with _ExecutionCliProject() as project:
            record_path = project.root / "journal.json"
            self.assertEqual(
                main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--record",
                        str(record_path),
                        "--yes",
                    ]
                ),
                0,
            )
            record_before = record_path.read_bytes()
            manifest_before = project.manifest_path.read_bytes()
            source_before = {
                name: path.read_bytes() for name, path in project.sources.items()
            }
            targets_before = {
                name: path.read_bytes() for name, path in project.targets.items()
            }

            stdout = io.StringIO()
            with (
                patch("luwu.cli.load_manifest", side_effect=AssertionError),
                redirect_stdout(stdout),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(["recover", "--record", str(record_path), "--json"])

            output = stdout.getvalue()
            payload = json.loads(output)
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["journal"]["path"], str(record_path))
            self.assertTrue(payload["plan_id"])
            self.assertEqual(payload["record_state"], "committed")
            self.assertEqual(payload["outcome"], "confirmed")
            self.assertEqual(
                [item["reobserved_state"] for item in payload["resources"]],
                ["confirmed", "confirmed"],
            )
            self.assertEqual(record_path.read_bytes(), record_before)
            self.assertEqual(project.manifest_path.read_bytes(), manifest_before)
            self.assertEqual(
                {name: path.read_bytes() for name, path in project.sources.items()},
                source_before,
            )
            self.assertEqual(
                {name: path.read_bytes() for name, path in project.targets.items()},
                targets_before,
            )
            self.assertNotIn(project.source_value, output)

    def test_recover_reports_changed_target_and_never_writes_it(self) -> None:
        with _ExecutionCliProject() as project:
            record_path = project.root / "journal.json"
            self.assertEqual(
                main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--record",
                        str(record_path),
                        "--yes",
                    ]
                ),
                0,
            )
            sentinel = "private-target-value\n"
            project.targets["alpha"].write_text(sentinel, encoding="utf-8")
            target_before = project.targets["alpha"].read_bytes()

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(["recover", "--record", str(record_path), "--json"])

            output = stdout.getvalue()
            payload = json.loads(output)
            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["outcome"], "recovery_required")
            alpha = next(
                item for item in payload["resources"] if item["name"] == "alpha"
            )
            self.assertEqual(alpha["reobserved_state"], "changed_or_unknown")
            target_path = next(
                path for path in alpha["paths"] if path["role"] == "target"
            )
            self.assertFalse(target_path["matches_postcondition"])
            self.assertEqual(project.targets["alpha"].read_bytes(), target_before)
            self.assertNotIn(sentinel, output)

    def test_recover_reports_manifest_stale_and_supports_human_alias(self) -> None:
        with _ExecutionCliProject() as project:
            record_path = project.root / "journal.json"
            self.assertEqual(
                main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--record",
                        str(record_path),
                        "--yes",
                    ]
                ),
                0,
            )
            project.manifest_path.write_bytes(
                project.manifest_path.read_bytes() + b"\n"
            )
            record_before = record_path.read_bytes()
            target_before = {
                name: path.read_bytes() for name, path in project.targets.items()
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(["record-reobserve", "--record", str(record_path)])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 2)
            self.assertIn("Outcome: recovery_required", output)
            self.assertIn("Reason: manifest_changed", output)
            self.assertIn("Recovery re-observation", output)
            self.assertNotIn(project.source_value, output)
            self.assertEqual(record_path.read_bytes(), record_before)
            self.assertEqual(
                {name: path.read_bytes() for name, path in project.targets.items()},
                target_before,
            )
