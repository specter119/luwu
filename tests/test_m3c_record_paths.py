from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Self

from luwu.errors import ApplyError
from luwu.manifest import load_manifest
from luwu.plan_record import record_lock_path
from luwu.reconcile import build_plan, execute_execution_plan


class M3cRecordPathTests(unittest.TestCase):
    def test_record_lock_path_is_the_writer_sidecar(self) -> None:
        with _Project() as project:
            record_path = project.root / "journal.json"
            lock_path = record_lock_path(record_path)

            self.assertEqual(lock_path, project.root / ".journal.json.luwu-lock")
            execute_execution_plan(project.plan(), record_path, confirm=True)
            self.assertTrue(record_path.is_file())
            self.assertTrue(lock_path.is_file())

    def test_lock_sidecar_cannot_overlap_target_source_or_manifest(self) -> None:
        for declared_role in ("target", "source", "manifest"):
            with (
                self.subTest(declared_role=declared_role),
                _Project(
                    target_name=(
                        ".journal.json.luwu-lock"
                        if declared_role == "target"
                        else "targets/settings.conf"
                    ),
                    source_name=(
                        ".journal.json.luwu-lock"
                        if declared_role == "source"
                        else "sources/settings.conf.j2"
                    ),
                    manifest_name=(
                        ".journal.json.luwu-lock"
                        if declared_role == "manifest"
                        else "luwu.toml"
                    ),
                    kind="symbolic" if declared_role == "source" else "template",
                ) as project,
            ):
                self._assert_conflict(project, project.root / "journal.json")

    def test_exact_declared_paths_and_resolved_source_alias_are_conflicts(self) -> None:
        for role in ("target", "source", "manifest"):
            with self.subTest(role=role), _Project() as project:
                path = {
                    "target": project.target,
                    "source": project.source,
                    "manifest": project.manifest_path,
                }[role]
                self._assert_conflict(project, path)

        with _Project(source_name="sources/alias.conf.j2") as project:
            real_source = project.root / "sources" / "real.conf.j2"
            project.source.rename(real_source)
            project.source.symlink_to(real_source.name)
            project.reload_manifest()

            self._assert_conflict(project, real_source)

    def test_hardlinked_record_or_lock_alias_is_a_conflict(self) -> None:
        with _Project() as project:
            record_path = project.root / "journal.json"
            record_lock_path(record_path).hardlink_to(project.target)
            before = _snapshot(project.root)

            with self.assertRaises(ApplyError) as context:
                execute_execution_plan(project.plan(), record_path, confirm=True)

            self.assertEqual(context.exception.code, "record_path_conflict")
            self.assertEqual(_snapshot(project.root), before)

    def test_ancestor_and_descendant_record_paths_are_conflicts(self) -> None:
        for record_path in (self._ancestor_path, self._descendant_path):
            with self.subTest(record_path=record_path.__name__), _Project() as project:
                self._assert_conflict(project, record_path(project))

    def test_preview_has_no_record_or_lock_sidecar(self) -> None:
        with _Project() as project:
            record_path = project.root / "journal.json"
            before = _snapshot(project.root)

            result = execute_execution_plan(project.plan(), record_path, confirm=False)

            self.assertIsNone(result.record)
            self.assertEqual(_snapshot(project.root), before)
            self.assertFalse(record_path.exists())
            self.assertFalse(record_lock_path(record_path).exists())

    @staticmethod
    def _ancestor_path(project: _Project) -> Path:
        return project.root / "targets"

    @staticmethod
    def _descendant_path(project: _Project) -> Path:
        return project.root / "targets" / "settings.conf" / "journal.json"

    def _assert_conflict(self, project: _Project, record_path: Path) -> None:
        before = _snapshot(project.root)

        with self.assertRaises(ApplyError) as context:
            execute_execution_plan(project.plan(), record_path, confirm=True)

        self.assertEqual(context.exception.code, "record_path_conflict")
        self.assertEqual(_snapshot(project.root), before)


class _Project:
    def __init__(
        self,
        *,
        source_name: str = "sources/settings.conf.j2",
        target_name: str = "targets/settings.conf",
        manifest_name: str = "luwu.toml",
        kind: str = "template",
    ) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / source_name
        self.target = self.root / target_name
        self.manifest_path = self.root / manifest_name
        self.kind = kind
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_text("settings-desired\n", encoding="utf-8")
        self.target.write_text("settings-old\n", encoding="utf-8")
        self.reload_manifest()

    def reload_manifest(self) -> None:
        source_name = self.source.relative_to(self.manifest_path.parent)
        target_name = self.target.relative_to(self.manifest_path.parent)
        self.manifest_path.write_text(
            "version = 5\n\n"
            "[resources.settings]\n"
            f'kind = "{self.kind}"\n'
            f'source = "{source_name.as_posix()}"\n'
            f'target = "{target_name.as_posix()}"\n'
            'owner = "source"\n'
            'scope = "whole-file"\n'
            'content_sensitivity = "public"\n',
            encoding="utf-8",
        )
        self.manifest = load_manifest(self.manifest_path)

    def plan(self):
        return build_plan(self.manifest)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.directory.cleanup()


def _snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory",)
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        else:
            snapshot[relative] = ("other",)
    return snapshot


if __name__ == "__main__":
    unittest.main()
