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
from luwu.platform_support import PlatformStatus

SENTINEL = "m4-secret-sentinel"


class M4CliTests(unittest.TestCase):
    def test_v6_requires_explicit_runtime_authority(self) -> None:
        with _Project() as project:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--record",
                        str(project.journal),
                        "--yes",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(project.target.exists())
            self.assertEqual(payload["reason"], "plan_blocked")
            self.assertNotIn(SENTINEL, stdout.getvalue())

    def test_v6_cli_apply_uses_explicit_fake_rbw_and_safe_projection(self) -> None:
        with _Project() as project:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--allow-subprocess",
                        "--rbw-executable",
                        str(project.executable),
                        "--record",
                        str(project.journal),
                        "--yes",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["state"], "committed")
            self.assertEqual(payload["target_names"], ["resource-0"])
            self.assertEqual(
                project.target.read_text(encoding="utf-8"), f"password={SENTINEL}\n"
            )
            self.assertEqual(project.target.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(SENTINEL, stdout.getvalue())
            self.assertNotIn("database-prod", stdout.getvalue())
            self.assertNotIn("password", stdout.getvalue())

    def test_v6_recover_flags_are_explicit_and_recover_never_writes(self) -> None:
        with _Project() as project:
            self.assertEqual(
                main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--allow-subprocess",
                        "--rbw-executable",
                        str(project.executable),
                        "--record",
                        str(project.journal),
                        "--yes",
                    ]
                ),
                0,
            )
            record_before = project.journal.read_bytes()
            target_before = project.target.read_bytes()
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    ["recover", "--record", str(project.journal), "--json"]
                )
            denied = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertEqual(denied["reason"], "capability_required")

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "recover",
                        "--record",
                        str(project.journal),
                        "--allow-subprocess",
                        "--rbw-executable",
                        str(project.executable),
                        "--json",
                    ]
                )
            allowed = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(allowed["outcome"], "currently_converged")
            self.assertEqual(project.journal.read_bytes(), record_before)
            self.assertEqual(project.target.read_bytes(), target_before)
            self.assertNotIn(SENTINEL, stdout.getvalue())

    def test_cache_refresh_and_inspect_are_explicit_diagnostics(self) -> None:
        with _Project() as project:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                refresh_code = main(
                    [
                        "cache-refresh",
                        "--cache",
                        str(project.cache),
                        "--rbw-executable",
                        str(project.executable),
                        "--json",
                    ]
                )
            self.assertEqual(refresh_code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "written")

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                inspect_code = main(
                    [
                        "cache-inspect",
                        "--cache",
                        str(project.cache),
                        "--rbw-executable",
                        str(project.executable),
                        "--json",
                    ]
                )
            self.assertEqual(inspect_code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "fresh")
            self.assertNotIn(SENTINEL, stdout.getvalue())

    def test_platform_check_is_diagnostic_only(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = main(["platform-check", "--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["supported"])

    def test_platform_check_reports_unsupported_without_authorizing_execution(
        self,
    ) -> None:
        unsupported = PlatformStatus(
            supported=False,
            system="Darwin",
            machine="x86_64",
            python_version=(3, 14, 0),
            missing=("linux",),
        )
        stdout = io.StringIO()
        with (
            patch("luwu.cli.probe_platform", return_value=unsupported),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(["platform-check", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(payload["supported"])
        self.assertEqual(payload["missing"], ["linux"])


class _Project:
    def __enter__(self) -> Self:
        self._temporary_root = tempfile.TemporaryDirectory(dir=Path.cwd())
        root = Path(self._temporary_root.name)
        self.manifest_root = root / "manifest"
        target_root = root / "secret-target"
        self.manifest_root.mkdir()
        target_root.mkdir()
        (self.manifest_root / "templates").mkdir()
        (self.manifest_root / "templates/database.conf.j2").write_text(
            "password={{ secrets.db_password }}\n",
            encoding="utf-8",
        )
        target_parent = target_root / "nested"
        target_parent.mkdir()
        self.target = target_parent / "database.conf"
        self.executable = root / "rbw"
        self.executable.write_text(
            "#!/bin/sh\nprintf 'm4-secret-sentinel\\n'\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        self.manifest_path = self.manifest_root / "luwu.toml"
        self.manifest_path.write_text(
            f'''version = 6
capabilities = ["subprocess"]

[resources.database]
kind = "template"
source = "templates/database.conf.j2"
target = "{self.target}"
owner = "source"
scope = "whole-file"
content_sensitivity = "secret"

[resources.database.providers.db_password]
type = "rbw"
item = "database-prod"
field = "password"
''',
            encoding="utf-8",
        )
        self.journal = self.manifest_root / "journal.json"
        self.cache = self.manifest_root / "provider-cache.json"
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._temporary_root.cleanup()


if __name__ == "__main__":
    unittest.main()
