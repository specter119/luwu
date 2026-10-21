"""The manual rbw acceptance script stays importable and redaction-bound.

These tests never execute rbw: they import the experiment file by path and
exercise its local preconditions and redaction helper only.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

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


if __name__ == "__main__":
    import unittest

    unittest.main()
