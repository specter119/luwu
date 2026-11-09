"""The manual rbw acceptance script stays importable and redaction-bound.

These tests never execute a real rbw or a real luwu: they import the
experiment file by path and exercise its local preconditions, redaction
helper, step list, the manifest it builds, and one step against temporary fake
commands.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments" / "rbw_acceptance.py"


def _load_script() -> ModuleType:
    specification = importlib.util.spec_from_file_location("rbw_acceptance", SCRIPT)
    if specification is None or specification.loader is None:
        raise AssertionError("the acceptance script could not be loaded")
    module = importlib.util.module_from_spec(specification)
    # Dataclasses resolve their module through sys.modules while the class
    # body runs, so register the module before executing it.
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
    except Exception:
        sys.modules.pop(specification.name, None)
        raise
    return module


class RbwAcceptanceToolingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_script()

    def test_relative_or_missing_rbw_is_refused(self) -> None:
        # An existing, executable relative path is still refused: the guard is
        # the absolute-path rule, not a missing file.
        with tempfile.TemporaryDirectory(prefix="luwu-test-rbw-path-") as directory:
            root = Path(directory)
            relative = root / "bin" / "rbw"
            relative.parent.mkdir()
            relative.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            relative.chmod(0o700)
            original = Path.cwd()
            os.chdir(root)
            try:
                with self.assertRaises(self.module.AcceptanceError) as raised:
                    self.module.validate_rbw(Path("bin/rbw"))
            finally:
                os.chdir(original)
            self.assertIn("absolute", str(raised.exception))

        with self.assertRaises(self.module.AcceptanceError):
            self.module.validate_rbw(Path("/nonexistent/luwu-rbw"))
        self.assertEqual(
            self.module.validate_rbw(Path(sys.executable)), Path(sys.executable)
        )

    def test_workdir_refuses_a_non_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luwu-test-rbw-workdir-") as directory:
            root = Path(directory)
            (root / "occupied.txt").write_text("not empty\n", encoding="utf-8")
            with self.assertRaises(self.module.AcceptanceError):
                self.module.prepare_workdir(root)

            fresh = root / "fresh"
            prepared = self.module.prepare_workdir(fresh)
            self.assertEqual(prepared, fresh)
            self.assertEqual(stat.S_IMODE(prepared.stat().st_mode), 0o700)

    def test_redaction_refuses_the_secret_and_long_strings(self) -> None:
        redact = self.module.redact
        self.assertEqual(redact("plan"), "plan")
        self.assertEqual(redact("x" * 64), "x" * 64)
        marker = self.module.REDACTION_MARKER
        self.assertEqual(redact("x" * 65), marker)
        self.assertEqual(
            redact("value provider-canary here", "provider-canary"), marker
        )
        self.assertEqual(redact(None), marker)

    def test_write_profile_manifest_loads(self) -> None:
        from luwu import manifest as manifest_module

        with tempfile.TemporaryDirectory(prefix="luwu-test-rbw-manifest-") as directory:
            workdir = self.module.prepare_workdir(Path(directory) / "workdir")
            manifest_path = self.module.write_profile(
                workdir, item="acceptance-item", field_name="apiKey"
            )
            manifest = manifest_module.load_manifest(
                manifest_path, profile=self.module.PROFILE
            )
        reference = manifest.providers["vault"]
        self.assertEqual(reference.item, "acceptance-item")
        self.assertEqual(reference.field, "apiKey")

    def test_steps_are_the_three_external_fact_steps(self) -> None:
        names = {function.__name__ for function in self.module.STEPS}
        self.assertEqual(
            names,
            {
                "step_locked_vault",
                "step_agent_stopped",
                "step_unlocked_apply_verify",
            },
        )

    def test_open_facts_exclude_what_the_walk_cannot_confirm(self) -> None:
        confirmed = "\n".join(self.module.OPEN_FACTS)
        not_confirmed = "\n".join(self.module.NOT_CONFIRMED_FACTS)
        self.assertNotIn("pinentry", confirmed)
        self.assertNotIn("core_pattern", confirmed)
        self.assertIn("pinentry", not_confirmed)
        self.assertIn("core_pattern", not_confirmed)

    def test_run_cli_refuses_output_carrying_the_secret(self) -> None:
        secret = "acceptance-canary-51be"
        with tempfile.TemporaryDirectory(prefix="luwu-test-rbw-scan-") as directory:
            root = Path(directory)
            workdir = self.module.prepare_workdir(root / "workdir")
            for stream in ("1", "2"):
                leaky = root / f"leaky-luwu-{stream}"
                leaky.write_text(
                    f"#!/bin/sh\nprintf '%s\\n' '{secret}' >&{stream}\n",
                    encoding="utf-8",
                )
                leaky.chmod(0o700)
                context = self.module.Context(
                    workdir=workdir,
                    manifest=root / "luwu.toml",
                    rbw=Path(sys.executable),
                    luwu=[str(leaky)],
                    secret=secret,
                )
                with self.assertRaises(self.module.AcceptanceError):
                    self.module.run_cli(context, ["plan"])
                # The scan is bound to a known value: the same output passes
                # while the secret is still unknown.
                context.secret = None
                self.module.run_cli(context, ["plan"])

    def _agent_stopped_context(self, root: Path, *, error_code: str) -> ModuleType:
        workdir = self.module.prepare_workdir(root / "workdir")
        manifest = self.module.write_profile(workdir, item="item", field_name=None)
        fake_rbw = root / "rbw"
        fake_rbw.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        fake_rbw.chmod(0o700)
        payload = json.dumps(
            {
                "resources": [
                    {
                        "name": "app",
                        "status": "blocked",
                        "error_code": error_code,
                        "providers": [{"state": error_code}],
                    }
                ]
            }
        )
        fake_luwu = root / "luwu"
        fake_luwu.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(payload)}\n", encoding="utf-8"
        )
        fake_luwu.chmod(0o700)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        return self.module.Context(
            workdir=workdir,
            manifest=manifest,
            rbw=fake_rbw,
            luwu=[str(fake_luwu)],
            environ={"HOME": str(root / "home"), "XDG_RUNTIME_DIR": str(runtime)},
        )

    def test_agent_stopped_accepts_the_stopped_agent_codes(self) -> None:
        self.assertEqual(
            set(self.module.AGENT_STOPPED_CODES),
            {"provider_agent_unavailable", "provider_command_failed"},
        )
        with tempfile.TemporaryDirectory(prefix="luwu-test-rbw-stopped-") as directory:
            root = Path(directory)
            context = self._agent_stopped_context(
                root, error_code="provider_command_failed"
            )
            with mock.patch.object(self.module, "prompt"):
                name, checks = self.module.step_agent_stopped(context)
        self.assertEqual(name, "agent_stopped")
        self.assertEqual(checks["observed_error_code"], "provider_command_failed")
        self.assertTrue(checks["plan_blocked"])
        self.assertTrue(checks["code_is_agent_stopped"])
        self.assertTrue(checks["provider_state_matches_code"])
        self.assertTrue(checks["probe_nonzero"])
        self.assertEqual(checks["probe_exit_code"], 3)
        self.assertFalse(checks["stale_socket_left_behind"])
        if checks["agent_count_checked"]:
            self.assertTrue(checks["no_rbw_agent_started"])
        else:
            self.assertTrue(checks["agent_count_unavailable"])

    def test_agent_stopped_rejects_another_blocked_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luwu-test-rbw-stopped-") as directory:
            root = Path(directory)
            context = self._agent_stopped_context(root, error_code="provider_locked")
            with (
                mock.patch.object(self.module, "prompt"),
                self.assertRaises(self.module.AcceptanceError) as raised,
            ):
                self.module.step_agent_stopped(context)
        self.assertEqual(
            raised.exception.checks["observed_error_code"], "provider_locked"
        )

    def test_agent_count_checks_assert_only_when_countable(self) -> None:
        stable = self.module.agent_count_checks("step", 1, 1)
        self.assertTrue(stable["agent_count_checked"])
        self.assertTrue(stable["no_rbw_agent_started"])
        with self.assertRaises(self.module.AcceptanceError):
            self.module.agent_count_checks("step", 1, 2)
        unavailable = self.module.agent_count_checks("step", None, None)
        self.assertFalse(unavailable["agent_count_checked"])
        self.assertTrue(unavailable["agent_count_unavailable"])
        self.assertIsNone(unavailable["no_rbw_agent_started"])


if __name__ == "__main__":
    import unittest

    unittest.main()
