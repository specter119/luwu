"""Tests for version routing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import cast

from luwu.cli import main

ROOT = Path(__file__).resolve().parents[2]


V8_MANIFEST = """\
version = 8

[roots]
source = "source"
inputs = "inputs"
target = "targets"

[profiles.work]
packages = ["editor"]
exclude = []

[packages.editor]
dependencies = []
resources = ["settings", "notes"]

[inputs.theme]
layers = [
  { file = "global/theme.json", path = ["theme"], format = "json" },
  { file = "local/theme.json", path = ["theme"], format = "json" },
]
reverse_to = { file = "local/theme.json", path = ["theme"], format = "json" }

[resources.settings]
source = "templates/settings.json.j2"
target = "settings.json"
format = "json"
baseline = "settings.baseline.json"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"

[resources.settings.fields.theme]
path = ["theme"]
owner = "live"
input = "theme"

[resources.notes]
deploy = "copy"
source = "notes.txt"
target = "notes.txt"
baseline = "notes.baseline.json"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
"""


class _CliCase:
    def __init__(self, root: Path, *, manifest: Path | None = None) -> None:
        self.root = root
        self.manifest = manifest or root / "luwu.toml"
        self.process_home = root / "isolated-process-home"
        self.process_home.mkdir()
        self.temporary = root / "tmp"
        self.temporary.mkdir()

    def cli(
        self,
        *arguments: str,
        expected_code: int,
        profile: str | None = "work",
    ) -> dict[str, object]:
        command = [
            sys.executable,
            "-B",
            "-m",
            "luwu",
            *arguments,
            "--manifest",
            str(self.manifest),
        ]
        if profile is not None:
            command.extend(("--profile", profile))
        command.append("--json")
        environment = {
            "HOME": str(self.process_home),
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(self.temporary),
            "XDG_CACHE_HOME": str(self.process_home / ".cache"),
            "XDG_CONFIG_HOME": str(self.process_home / ".config"),
            "XDG_DATA_HOME": str(self.process_home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.process_home / ".local" / "state"),
            "PYTHONPATH": str(ROOT / "src"),
        }
        completed = subprocess.run(
            command,
            cwd=self.process_home,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != expected_code:
            raise AssertionError(
                f"CLI {arguments[0]} returned {completed.returncode}, "
                f"expected {expected_code}; stdout={completed.stdout!r}; "
                f"stderr={completed.stderr!r}"
            )
        if completed.stderr:
            raise AssertionError(f"CLI unexpectedly wrote stderr: {completed.stderr!r}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise AssertionError("CLI did not emit one JSON object") from None
        if not isinstance(payload, dict):
            raise TypeError("CLI JSON result must be an object")
        return cast(dict[str, object], payload)


class ProfileCliV8Tests(unittest.TestCase):
    def _v8_case(self) -> _CliCase:
        temporary = tempfile.TemporaryDirectory(prefix="luwu-test-cli-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        case = _CliCase(root)
        for relative in (
            "source/templates",
            "inputs/global",
            "inputs/local",
            "targets",
        ):
            (root / relative).mkdir(parents=True)
        (root / "luwu.toml").write_text(V8_MANIFEST, encoding="utf-8")
        (root / "source/templates/settings.json.j2").write_text(
            '{"theme": {{ inputs.theme | tojson }}}\n', encoding="utf-8"
        )
        (root / "source/notes.txt").write_text("approved-note\n", encoding="utf-8")
        (root / "inputs/global/theme.json").write_text(
            '{"theme":"calm-1"}\n', encoding="utf-8"
        )
        (root / "inputs/local/theme.json").write_text(
            '{"theme":"calm-1"}\n', encoding="utf-8"
        )
        return case

    def test_profiled_copy_and_template_cli_loop(self) -> None:
        case = self._v8_case()
        target_root = case.root / "targets"
        settings_target = target_root / "settings.json"
        notes_target = target_root / "notes.txt"

        plan = case.cli("plan", expected_code=0)
        self.assertEqual(plan["schema_version"], 8)
        resources = cast(list[dict[str, object]], plan["resources"])
        self.assertEqual(
            {resource["name"] for resource in resources}, {"settings", "notes"}
        )
        inspection = case.cli("inspect", expected_code=0)
        self.assertEqual(inspection["schema_version"], 8)
        self.assertFalse(settings_target.exists())
        self.assertFalse(notes_target.exists())

        adopted = case.cli("adopt", "--resource", "notes", "--yes", expected_code=0)
        self.assertEqual(adopted["outcome"], "committed")
        self.assertFalse(notes_target.exists())

        accepted = case.cli(
            "accept",
            "--resource",
            "settings",
            "--from",
            "desired",
            "--field",
            "theme",
            "--yes",
            expected_code=0,
        )
        self.assertEqual(accepted["outcome"], "committed")

        preview = case.cli("apply", expected_code=2)
        self.assertEqual(preview["outcome"], "confirmation_required")
        self.assertFalse(settings_target.exists())
        self.assertFalse(notes_target.exists())

        applied = case.cli("apply", "--yes", expected_code=0)
        self.assertEqual(applied["outcome"], "committed")
        self.assertEqual(json.loads(settings_target.read_text()), {"theme": "calm-1"})
        self.assertEqual(notes_target.read_text(), "approved-note\n")

        settings_target.write_text('{"theme":"calm-2"}\n', encoding="utf-8")
        reversed_inputs = case.cli(
            "reverse-sync",
            "--resource",
            "settings",
            "--field",
            "theme",
            "--yes",
            expected_code=0,
        )
        self.assertEqual(reversed_inputs["outcome"], "committed")
        self.assertEqual(
            json.loads((case.root / "inputs/local/theme.json").read_text()),
            {"theme": "calm-2"},
        )
        self.assertNotIn("calm-2", json.dumps(reversed_inputs))
        converged = case.cli("apply", "--yes", expected_code=0)
        self.assertEqual(converged["outcome"], "committed")
        self.assertEqual(
            [
                action["kind"]
                for action in cast(list[dict[str, object]], converged["actions"])
            ],
            ["baseline_write"],
        )
        settled = case.cli("apply", "--yes", expected_code=0)
        self.assertEqual(settled["outcome"], "no_changes")

    def test_accept_preview_is_metadata_only_and_read_only(self) -> None:
        case = self._v8_case()

        preview = case.cli(
            "accept",
            "--resource",
            "settings",
            "--from",
            "desired",
            "--field",
            "theme",
            expected_code=2,
        )

        self.assertEqual(preview["outcome"], "confirmation_required")
        self.assertFalse((case.root / "inputs/settings.baseline.json").exists())
        self.assertNotIn("calm-1", json.dumps(preview))

    def test_text_plan_reports_metadata_without_values(self) -> None:
        case = self._v8_case()
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                [
                    "plan",
                    "--manifest",
                    str(case.manifest),
                    "--profile",
                    "work",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        output = stdout.getvalue()
        self.assertIn("plan", output)
        self.assertIn('profile: "work"', output)
        self.assertNotIn("calm-1", output)

    def test_profile_selection_errors_are_explicit(self) -> None:
        case = self._v8_case()
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["inspect", "--manifest", str(case.manifest)])
        self.assertEqual(raised.exception.code, 2)
        selection_error = case.cli("inspect", expected_code=2, profile="undeclared")
        self.assertEqual(
            cast(dict[str, object], selection_error["error"])["code"],
            "profile",
        )

    def test_v8_rejects_provider_manifest_fields_and_hides_parse_details(self) -> None:
        case = self._v8_case()
        case.manifest.write_text(
            V8_MANIFEST.replace("version = 8", "version = 8\nproviders = {}"),
            encoding="utf-8",
        )
        accepted = case.cli("inspect", expected_code=0)
        self.assertEqual(accepted["schema_version"], 8)

        case.manifest.write_text(
            V8_MANIFEST.replace("version = 8", 'version = 8\nproviders = "nope"'),
            encoding="utf-8",
        )
        provider_error = case.cli("inspect", expected_code=2)
        self.assertEqual(
            cast(dict[str, object], provider_error["error"])["code"],
            "providers",
        )
        self.assertNotIn("nope", json.dumps(provider_error))

        parser_marker = "fixture-parser-detail-must-not-escape"
        case.manifest.write_text(
            f'version = 8\n[broken\nmarker = "{parser_marker}"\n',
            encoding="utf-8",
        )
        parser_error = case.cli("inspect", expected_code=2)
        self.assertEqual(
            cast(dict[str, object], parser_error["error"])["code"],
            "manifest_toml",
        )
        self.assertNotIn(parser_marker, json.dumps(parser_error))

    def test_retired_manifest_versions_are_rejected_before_schema_checks(self) -> None:
        case = self._v8_case()
        for version in (1, 2, 3, 4, 5, 6, 7):
            case.manifest.write_text(
                f'version = {version}\n[[resources]]\nname = "legacy"\n',
                encoding="utf-8",
            )
            error = case.cli("inspect", expected_code=2)
            self.assertEqual(
                cast(dict[str, object], error["error"])["code"], "manifest_version"
            )
            self.assertEqual(
                cast(dict[str, object], error["error"])["message"],
                "manifest version must be 8",
            )

    def test_retired_commands_and_options_are_not_accepted(self) -> None:
        case = self._v8_case()
        for arguments in (
            ("record-inspect", "--record", "journal.json"),
            ("recover", "--record", "journal.json"),
            ("cache-inspect", "--cache", "cache.json"),
            ("platform-check",),
            ("apply", "--record", "journal.json", "--manifest", str(case.manifest)),
            ("inspect", "--allow-subprocess", "--manifest", str(case.manifest)),
        ):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
                main([*arguments, "--profile", "work"])
            self.assertEqual(raised.exception.code, 2)
