from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Self
from unittest.mock import patch

from luwu.cli import main
from luwu.errors import MutationError


class CliTests(unittest.TestCase):
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

    def test_v1_to_v4_apply_boundary_remains_unchanged(self) -> None:
        for version in range(1, 5):
            with self.subTest(version=version), _LegacyCliProject(version) as project:
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
                if version == 1:
                    self.assertEqual(exit_code, 0)
                    self.assertTrue(payload["applied"])
                    self.assertEqual(project.target.read_text(), project.source_value)
                else:
                    self.assertEqual(exit_code, 2)
                    self.assertEqual(
                        payload["apply_block_reason"],
                        "m2_read_only" if version == 2 else "m3_read_only",
                    )
                    self.assertEqual(project.target.read_text(), project.target_value)

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


class _LegacyCliProject:
    def __init__(self, version: int) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.source_value = '{"value": 2}\n'
        self.target_value = '{"value": 1}\n'
        self.source = self.root / "templates/settings.json.j2"
        self.target = self.root / "live/settings.json"
        self.manifest_path = self.root / "luwu.toml"
        self.source.write_text(self.source_value, encoding="utf-8")
        self.target.write_text(self.target_value, encoding="utf-8")
        if version == 1:
            resource = """source = "templates/settings.json.j2"
target = "live/settings.json"
owner = "source"
scope = "whole-file"
"""
        elif version == 2:
            resource = """kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "source"
scope = "whole-file"
"""
        else:
            field_owner = "source" if version == 3 else "live"
            reverse_sync = (
                "\n[resources.settings.reverse_sync]\n"
                'format = "literal-json"\n\n'
                "[resources.settings.reverse_sync.fields]\n"
                'value = "value"\n'
                if version == 4
                else ""
            )
            resource = f'''kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
baseline = "baseline.json"

[resources.settings.fields]
value = "{field_owner}"
{reverse_sync}'''
            (self.root / "baseline.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "resource": "settings",
                        "source": "templates/settings.json.j2",
                        "target": "live/settings.json",
                        "owners": {"value": field_owner},
                        "values": {"value": 1},
                    }
                ),
                encoding="utf-8",
            )
        self.manifest_path.write_text(
            f"version = {version}\n\n[resources.settings]\n{resource}",
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()
