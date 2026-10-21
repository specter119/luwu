"""Staging residue at the provider target-write boundary.

The provider value is staged into a ``.luwu-tmp-*`` entry before the atomic
publication. A normal in-process failure must remove that entry; a real
``SIGKILL`` between staging and publication cannot run cleanup, so one staged
entry may remain. That is the known limit asserted below, not repaired here:
the residue is created 0600 inside the declared target directory and holds
only the staged bytes, so it is owner-only and no wider than the target.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from luwu import filesystem
from luwu.engine import apply_plan, build_mutation_plan
from luwu.manifest import load_manifest
from luwu.providers import ProviderAuthority
from tests.support.fake_rbw import CANARY_BETA, FakeRbwCase

PROVIDER_FIELD_VALUE = CANARY_BETA

SIGKILL_AT_PUBLISH = """
import os
import signal
import sys
import luwu.filesystem as filesystem
from luwu.cli import main

target_name = sys.argv.pop(1)
original = filesystem.os.replace

def kill_at_publish(source, destination, **kwargs):
    if destination == target_name:
        os.kill(os.getpid(), signal.SIGKILL)
    return original(source, destination, **kwargs)

filesystem.os.replace = kill_at_publish
raise SystemExit(main())
"""


class ProviderWriteResidueTests(FakeRbwCase):
    """One provider-only resource whose target write can be interrupted."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.target_root = self.root / "target"
        for path in (
            self.source / "templates",
            self.inputs / "baselines",
            self.target_root,
        ):
            path.mkdir(parents=True)
        self.executable = self.install_rbw(
            document={
                "name": "fixture-item",
                "data": {"password": CANARY_BETA},
            }
        )
        self.bind_socket()
        environment = patch.dict(os.environ, self.environ())
        environment.start()
        self.addCleanup(environment.stop)
        (self.source / "templates" / "app.json.j2").write_text(
            '{"service":{"apiKey":"placeholder-residue"}}\n', encoding="utf-8"
        )
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text(
            """
version = 8

[roots]
source = "source"
inputs = "inputs"
target = "target"

[profiles.work]
packages = ["app"]
exclude = []

[packages.app]
dependencies = []
resources = ["app"]

[providers.db]
type = "rbw"
item = "fixture-item"

[resources.app]
source = "templates/app.json.j2"
target = ".config/app.json"
format = "json"
baseline = "baselines/app.json"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.app.fields.apiKey]
path = ["service", "apiKey"]
owner = "provider"
provider = "db"
""",
            encoding="utf-8",
        )

    def _target(self) -> Path:
        return self.target_root / ".config" / "app.json"

    def _plan(self):
        return build_mutation_plan(
            load_manifest(self.manifest_path, profile="work"),
            provider_authority=ProviderAuthority(rbw_executable=self.executable),
        )

    def _staging_entries(self, root: Path | None = None) -> list[Path]:
        base = self.target_root if root is None else root
        return sorted(path for path in base.rglob("*") if ".luwu-tmp-" in path.name)

    def _target_files(self) -> list[Path]:
        return sorted(
            path
            for path in self.target_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def _assert_canary_only_in_target(self, *, committed: bool) -> None:
        needle = PROVIDER_FIELD_VALUE.encode()
        holders = [path for path in self._target_files() if needle in path.read_bytes()]
        if committed:
            self.assertEqual(holders, [self._target()])
        else:
            self.assertEqual(holders, [])

    def test_publication_failure_removes_the_staged_file(self) -> None:
        plan = self._plan()

        with patch.object(
            filesystem.os, "replace", side_effect=OSError("injected publish failure")
        ):
            result = apply_plan(plan, confirm=True)

        # The writer fails closed at its publication boundary: once the
        # replace was attempted, the result is reported as uncertain rather
        # than claiming the namespace was left alone. The namespace and the
        # cleanup are still observable: no target, no staged entry.
        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.error_code, "write_unknown")
        states = {
            action.resource: action.state
            for action in result.actions
            if action.kind == "target_write"
        }
        self.assertEqual(states, {"app": "uncertain"})
        self.assertFalse(self._target().exists())
        self.assertEqual(self._staging_entries(), [])
        self._assert_canary_only_in_target(committed=False)

    def test_post_replace_verification_failure_leaves_no_staging_file(self) -> None:
        plan = self._plan()
        real_replace = filesystem.os.replace
        real_observe = filesystem._observe_entry
        state = {"published": False}

        def publishing_replace(
            source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            real_replace(
                source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd
            )
            state["published"] = True

        def corrupted_observe(parent: int, name: str):
            observation = real_observe(parent, name)
            if state["published"] and name == self._target().name:
                return replace(observation, data=b"corrupted-observation-v1\n")
            return observation

        with (
            patch.object(filesystem.os, "replace", publishing_replace),
            patch.object(filesystem, "_observe_entry", corrupted_observe),
        ):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.error_code, "write_unknown")
        target = self._target()
        self.assertTrue(target.is_file())
        self.assertIn(PROVIDER_FIELD_VALUE.encode(), target.read_bytes())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(self._staging_entries(), [])
        self._assert_canary_only_in_target(committed=True)

    def test_sigkill_after_staging_leaves_one_private_staging_entry(self) -> None:
        environment = {
            "HOME": str(self.root / "home"),
            "XDG_RUNTIME_DIR": str(self.runtime_root),
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                SIGKILL_AT_PUBLISH,
                self._target().name,
                "apply",
                "--manifest",
                str(self.manifest_path),
                "--profile",
                "work",
                "--provider-executable",
                f"rbw={self.executable}",
                "--yes",
                "--json",
            ],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(completed.returncode, -signal.SIGKILL)
        self.assertFalse(self._target().exists())
        staging = self._staging_entries()
        self.assertEqual(len(staging), 1, staging)
        staged = staging[0]
        self.assertEqual(staged.parent, self._target().parent)
        self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
        self.assertIn(PROVIDER_FIELD_VALUE.encode(), staged.read_bytes())


if __name__ == "__main__":
    import unittest

    unittest.main()
