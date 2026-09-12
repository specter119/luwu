from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from luwu.cli import main
from luwu.errors import ApplyError
from luwu.manifest import load_manifest
from luwu.reconcile import ApplyOutcome, apply_plan, build_plan


class M1M2BoundaryTests(unittest.TestCase):
    def test_m1_target_symlink_is_blocked_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target, manifest_path = _create_m1_project(root)
            protected = root / "protected.conf"
            protected.write_bytes(b"protected content\n")
            target.symlink_to(protected)

            plan = build_plan(load_manifest(manifest_path))

            self.assertEqual(plan.observations[0].action.value, "block")
            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "plan_blocked")
            self.assertTrue(target.is_symlink())
            self.assertEqual(protected.read_bytes(), b"protected content\n")

    def test_m1_existing_target_mode_is_preserved_after_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target, manifest_path = _create_m1_project(root)
            target.write_bytes(b"old target\n")
            target.chmod(0o600)

            result = apply_plan(build_plan(load_manifest(manifest_path)))

            self.assertEqual(result.outcome, ApplyOutcome.COMMITTED)
            self.assertEqual(target.read_bytes(), b'profile = "developer"\n')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_m1_successful_apply_cleans_temporary_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target, manifest_path = _create_m1_project(root)
            target.write_bytes(b"old target\n")

            result = apply_plan(build_plan(load_manifest(manifest_path)))

            self.assertEqual(result.outcome, ApplyOutcome.COMMITTED)
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.luwu-*")),
                [],
            )

    def test_m1_failed_write_preserves_old_target_and_cleans_temporary_entry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target, manifest_path = _create_m1_project(root)
            target.write_bytes(b"old target\n")
            target.chmod(0o640)
            before = target.stat()
            plan = build_plan(load_manifest(manifest_path))

            with (
                patch(
                    "luwu.reconcile.os.fchmod",
                    side_effect=OSError("injected permission failure"),
                ),
                self.assertRaises(ApplyError) as context,
            ):
                apply_plan(plan)

            self.assertEqual(context.exception.code, "write_failed")
            self.assertFalse(context.exception.committed)
            self.assertEqual(target.read_bytes(), b"old target\n")
            self.assertEqual(target.stat().st_ino, before.st_ino)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.luwu-*")),
                [],
            )

    def test_m2_rendered_encoding_block_keeps_other_resource_visible_and_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path, healthy_target = _create_m2_project(root)
            healthy_target.write_bytes(b"old healthy target\n")
            before = _snapshot_tree(root)

            for command, extra_arguments in (
                ("inspect", ()),
                ("plan", ()),
                ("apply", ("--yes",)),
            ):
                with self.subTest(command=command):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = main(
                            [
                                command,
                                "--manifest",
                                str(manifest_path),
                                "--json",
                                *extra_arguments,
                            ]
                        )

                    payload = json.loads(stdout.getvalue())
                    resources = {
                        resource["name"]: resource for resource in payload["resources"]
                    }
                    self.assertEqual(exit_code, 2 if command == "apply" else 0)
                    self.assertEqual(resources["alpha"]["status"], "blocked")
                    self.assertEqual(
                        resources["alpha"]["comparison"]["code"],
                        "rendered_encoding",
                    )
                    self.assertEqual(resources["zeta"]["status"], "drifted")
                    self.assertEqual(resources["zeta"]["action"], "replace")
                    self.assertNotIn("healthy-output-sentinel", stdout.getvalue())
                    self.assertNotIn("\\ud800", stdout.getvalue())
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(_snapshot_tree(root), before)


def _create_m1_project(root: Path) -> tuple[Path, Path]:
    (root / "templates").mkdir()
    (root / "live").mkdir()
    (root / "templates/settings.conf.j2").write_text(
        'profile = "{{ profile }}"\n',
        encoding="utf-8",
    )
    manifest_path = root / "luwu.toml"
    manifest_path.write_text(
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
    return root / "live/settings.conf", manifest_path


def _create_m2_project(root: Path) -> tuple[Path, Path]:
    (root / "templates").mkdir()
    (root / "live").mkdir()
    (root / "templates/alpha.j2").write_text(
        '{{ "\\ud800" }}',
        encoding="utf-8",
    )
    (root / "templates/zeta.j2").write_text(
        "healthy-output-sentinel",
        encoding="utf-8",
    )
    manifest_path = root / "luwu.toml"
    manifest_path.write_text(
        """version = 2

[resources.alpha]
source = "templates/alpha.j2"
target = "live/alpha.conf"
owner = "source"
scope = "whole-file"

[resources.zeta]
source = "templates/zeta.j2"
target = "live/zeta.conf"
owner = "source"
scope = "whole-file"
""",
        encoding="utf-8",
    )
    return manifest_path, root / "live/zeta.conf"


def _snapshot_tree(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        relative = str(path.relative_to(root))
        common = (info.st_mode, info.st_ino, info.st_mtime_ns)
        if stat.S_ISLNK(info.st_mode):
            snapshot[relative] = ("symlink", os.readlink(path), *common)
        elif stat.S_ISREG(info.st_mode):
            snapshot[relative] = ("file", path.read_bytes(), *common)
        elif stat.S_ISDIR(info.st_mode):
            snapshot[relative] = ("directory", *common)
        else:
            snapshot[relative] = ("other", *common)
    return snapshot


if __name__ == "__main__":
    unittest.main()
