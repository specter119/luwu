"""Tests for observe and write."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luwu import filesystem as filesystem_module
from luwu.filesystem import (
    FileObservation,
    FilesystemError,
    create_parent,
    missing_parents,
    observe_file,
    write_file,
)


def _absent() -> FileObservation:
    return FileObservation(data=None, identity=None, digest=None, mode=None)


class FilesystemV8Tests(unittest.TestCase):
    def test_observe_regular_file_and_absent_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "existing"
            parent.mkdir()
            path = parent / "settings.json"
            path.write_bytes(b'{"enabled":true}')
            path.chmod(0o640)

            observed = observe_file(root, path)

            self.assertEqual(observed.data, b'{"enabled":true}')
            self.assertEqual(
                observed.identity, (path.stat().st_dev, path.stat().st_ino)
            )
            assert observed.data is not None
            assert observed.digest is not None
            self.assertEqual(observed.digest, hashlib.sha256(observed.data).hexdigest())
            self.assertEqual(observed.mode, 0o640)
            self.assertEqual(
                observed.existing_ancestors,
                (
                    (root, (root.stat().st_dev, root.stat().st_ino)),
                    (parent, (parent.stat().st_dev, parent.stat().st_ino)),
                ),
            )
            self.assertEqual(observed.missing_chain, ())
            self.assertNotIn("enabled", repr(observed))
            self.assertNotIn(observed.digest, repr(observed))

            absent = observe_file(root, root / "missing" / "deeper" / "file")
            self.assertIsNone(absent.data)
            self.assertEqual(
                absent.existing_ancestors,
                ((root, (root.stat().st_dev, root.stat().st_ino)),),
            )
            self.assertEqual(
                absent.missing_chain,
                (root / "missing", root / "missing" / "deeper"),
            )

    def test_observe_rejects_symlink_and_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"public")
            symlink = root / "symlink"
            symlink.symlink_to(source.name)

            with self.assertRaises(FilesystemError) as raised:
                observe_file(root, symlink)
            self.assertEqual(raised.exception.code, "unsafe_file")

            alias = root / "alias"
            os.link(source, alias)
            with self.assertRaises(FilesystemError) as raised:
                observe_file(root, source)
            self.assertEqual(raised.exception.code, "unsafe_file")

    def test_observe_rechecks_ancestors_after_parent_open_reports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            moved = root / "parent-moved"
            (root / "real").mkdir()

            def replace_parent_then_report_missing(
                _root: Path, _path: Path
            ) -> tuple[int, str]:
                parent.rename(moved)
                parent.symlink_to("real", target_is_directory=True)
                raise FileNotFoundError

            with (
                patch.object(
                    filesystem_module,
                    "open_parent_directory",
                    side_effect=replace_parent_then_report_missing,
                ),
                self.assertRaises(FilesystemError) as raised,
            ):
                observe_file(root, parent / "file")

            self.assertEqual(raised.exception.code, "unsafe_path")
            self.assertTrue(parent.is_symlink())

    def test_missing_ancestor_snapshot_rejects_symlink_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "missing" / "nested" / "file"
            expected = observe_file(root, target)
            (root / "real").mkdir()
            (root / "missing").symlink_to("real", target_is_directory=True)

            with self.assertRaises(FilesystemError) as raised:
                write_file(root, target, b"after", expected=expected)

            self.assertEqual(raised.exception.code, "unsafe_path")
            self.assertFalse((root / "real" / "nested" / "file").exists())

    def test_missing_parents_are_ordered_and_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "one" / "two" / "file"

            self.assertEqual(
                missing_parents(root, target),
                (root / "one", root / "one" / "two"),
            )
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(FilesystemError) as raised:
                missing_parents(root, root / "link" / "child" / "file")
            self.assertEqual(raised.exception.code, "unsafe_path")

    def test_create_parent_sets_requested_mode_after_umask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            previous_umask = os.umask(0o077)
            try:
                identity = create_parent(root, first, 0o755)
            finally:
                os.umask(previous_umask)

            info = first.stat()
            self.assertEqual(identity, (info.st_dev, info.st_ino))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o755)
            self.assertEqual(missing_parents(root, first / "file"), ())

            with self.assertRaises(FilesystemError) as raised:
                create_parent(root, first, 0o700)
            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertTrue(first.is_dir())

            locked = root / "locked"
            previous_umask = os.umask(0o077)
            try:
                create_parent(root, locked, 0)
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(locked.stat().st_mode), 0)

    def test_create_parent_reports_post_create_fsync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(filesystem_module, "sync_directory", side_effect=OSError),
                self.assertRaises(FilesystemError) as raised,
            ):
                create_parent(root, root / "created", 0o700)

            self.assertEqual(raised.exception.state, "uncertain")
            self.assertTrue(raised.exception.committed)
            self.assertTrue((root / "created").is_dir())
            info = (root / "created").stat()
            self.assertEqual(
                raised.exception.created_identity, (info.st_dev, info.st_ino)
            )

    def test_create_parent_keeps_commit_status_for_fsync_file_exists_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(
                    filesystem_module,
                    "sync_directory",
                    side_effect=FileExistsError("private injected detail"),
                ),
                self.assertRaises(FilesystemError) as raised,
            ):
                create_parent(root, root / "created", 0o700)

            info = (root / "created").stat()
            self.assertEqual(raised.exception.state, "uncertain")
            self.assertTrue(raised.exception.committed)
            self.assertEqual(
                raised.exception.created_identity, (info.st_dev, info.st_ino)
            )

    def test_create_parent_reports_post_create_unlock_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(
                    filesystem_module, "unlock_directory", side_effect=OSError
                ),
                self.assertRaises(FilesystemError) as raised,
            ):
                create_parent(root, root / "created", 0o700)

            info = (root / "created").stat()
            self.assertEqual(raised.exception.state, "uncertain")
            self.assertTrue(raised.exception.committed)
            self.assertEqual(
                raised.exception.created_identity, (info.st_dev, info.st_ino)
            )
            self.assertEqual(raised.exception.cleanup_state, "failed")

    def test_create_parent_reports_ambiguous_mkdir_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_mkdir = os.mkdir

            def mkdir_then_raise(
                name: str, mode: int = 0o777, *, dir_fd: int | None = None
            ) -> None:
                original_mkdir(name, mode, dir_fd=dir_fd)
                raise OSError("private filesystem detail")

            with (
                patch.object(
                    filesystem_module.os, "mkdir", side_effect=mkdir_then_raise
                ),
                self.assertRaises(FilesystemError) as raised,
            ):
                create_parent(root, root / "created", 0o700)

            self.assertEqual(raised.exception.state, "uncertain")
            self.assertFalse(raised.exception.committed)
            self.assertTrue((root / "created").is_dir())
            self.assertIsNone(raised.exception.created_identity)
            self.assertEqual(raised.exception.cleanup_state, "not_needed")
            self.assertNotIn("private", str(raised.exception))

    def test_create_parent_rechecks_every_ancestor_after_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outer = root / "outer"
            parent = outer / "inner"
            parent.mkdir(parents=True)
            moved = root / "outer-moved"

            def rebind_intermediate_ancestor() -> None:
                outer.rename(moved)
                outer.mkdir()
                (moved / "inner").rename(outer / "inner")

            with self.assertRaises(FilesystemError) as raised:
                create_parent(
                    root,
                    parent / "created",
                    0o700,
                    check_inputs=rebind_intermediate_ancestor,
                )

            self.assertEqual(raised.exception.code, "stale_inputs")
            self.assertFalse((parent / "created").exists())
            self.assertEqual(list(root.glob(".luwu-tmp-dir-*")), [])

    def test_create_parent_never_replaces_a_racing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "created"
            original_mkdir = os.mkdir
            captured: dict[str, tuple[int, int]] = {}

            def race_target_into_place(
                name: str, mode: int = 0o777, *, dir_fd: int | None = None
            ) -> None:
                original_mkdir(name, mode, dir_fd=dir_fd)
                foreign = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                captured["foreign_target"] = (foreign.st_dev, foreign.st_ino)
                original_mkdir(name, mode, dir_fd=dir_fd)

            with (
                patch.object(
                    filesystem_module.os, "mkdir", side_effect=race_target_into_place
                ),
                self.assertRaises(FilesystemError) as raised,
            ):
                create_parent(root, target, 0o755)

            info = target.stat()
            self.assertEqual(raised.exception.code, "stale_inputs")
            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertFalse(raised.exception.committed)
            self.assertEqual(raised.exception.cleanup_state, "not_needed")
            self.assertEqual((info.st_dev, info.st_ino), captured["foreign_target"])

    def test_create_parent_reports_detected_post_mkdir_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "created"
            original_sync = filesystem_module.sync_directory
            original_mkdir = os.mkdir
            calls = 0
            captured: dict[str, tuple[int, int]] = {}

            def replace_after_first_sync(descriptor: int) -> None:
                nonlocal calls
                original_sync(descriptor)
                calls += 1
                if calls == 1:
                    os.rmdir(target)
                    original_mkdir(target, 0o700)
                    foreign = target.stat()
                    captured["foreign_target"] = (foreign.st_dev, foreign.st_ino)

            with (
                patch.object(
                    filesystem_module,
                    "sync_directory",
                    side_effect=replace_after_first_sync,
                ),
                self.assertRaises(FilesystemError) as raised,
            ):
                create_parent(root, target, 0o755)

            info = target.stat()
            self.assertEqual(raised.exception.state, "uncertain")
            self.assertTrue(raised.exception.committed)
            self.assertIsNone(raised.exception.created_identity)
            self.assertEqual((info.st_dev, info.st_ino), captured["foreign_target"])

    def test_create_parent_identity_is_observed_not_source_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "created"
            original_open = os.open
            original_mkdir = os.mkdir
            captured: dict[str, tuple[int, int]] = {}
            replaced = False

            def replace_before_open(
                name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal replaced
                if name == target.name and not replaced:
                    assert dir_fd is not None
                    os.rmdir(target.name, dir_fd=dir_fd)
                    original_mkdir(target.name, 0o700, dir_fd=dir_fd)
                    foreign = os.stat(target.name, dir_fd=dir_fd, follow_symlinks=False)
                    captured["foreign_target"] = (foreign.st_dev, foreign.st_ino)
                    replaced = True
                return original_open(name, flags, mode, dir_fd=dir_fd)

            with patch.object(
                filesystem_module.os, "open", side_effect=replace_before_open
            ):
                identity = create_parent(root, target, 0o755)

            info = target.stat()
            self.assertTrue(replaced)
            self.assertEqual(identity, (info.st_dev, info.st_ino))
            self.assertEqual((info.st_dev, info.st_ino), captured["foreign_target"])
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o755)

    def test_create_parent_rejects_untrusted_root_and_parent_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            parent.chmod(0o770)
            with self.assertRaises(FilesystemError) as raised:
                create_parent(root, parent / "created", 0o700)
            self.assertEqual(raised.exception.code, "unsafe_path")
            self.assertFalse((parent / "created").exists())

            parent.chmod(0o700)
            root.chmod(0o777)
            try:
                with self.assertRaises(FilesystemError) as raised:
                    observe_file(root, root / "file")
                self.assertEqual(raised.exception.code, "unsafe_path")
            finally:
                root.chmod(0o700)

    def test_write_rejects_a_parent_that_became_group_writable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            target = parent / "file"
            target.write_bytes(b"before")
            expected = observe_file(root, target)
            parent.chmod(0o770)

            with self.assertRaises(FilesystemError) as raised:
                write_file(root, target, b"after", expected=expected)

            self.assertEqual(raised.exception.code, "unsafe_path")
            self.assertEqual(target.read_bytes(), b"before")

    def test_private_root_under_sticky_tmp_ancestor_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir())))
            observed = observe_file(root, root / "missing")
            self.assertIsNone(observed.data)

    def test_create_parent_callback_failure_has_no_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def stale() -> None:
                raise RuntimeError("sensitive callback details")

            with self.assertRaises(FilesystemError) as raised:
                create_parent(root, root / "created", 0o700, check_inputs=stale)

            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertFalse(raised.exception.committed)
            self.assertNotIn("sensitive", str(raised.exception))
            self.assertFalse((root / "created").exists())

    def test_write_new_file_uses_fixed_default_and_exact_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "created"
            previous_umask = os.umask(0)
            try:
                written = write_file(
                    root, path, b"new", expected=observe_file(root, path)
                )
            finally:
                os.umask(previous_umask)

            info = path.stat()
            self.assertEqual(written, (info.st_dev, info.st_ino))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o644)
            self.assertEqual(path.read_bytes(), b"new")

            expected = observe_file(root, path)
            previous_umask = os.umask(0o077)
            try:
                write_file(root, path, b"new", expected=expected, mode=0o640)
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_write_preserves_mode_and_supports_mode_only_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "target"
            path.write_bytes(b"same bytes")
            path.chmod(0o644)
            expected = observe_file(root, path)

            new_identity = write_file(
                root, path, b"same bytes", expected=expected, mode=0o600
            )

            self.assertNotEqual(new_identity, expected.identity)
            self.assertEqual(path.read_bytes(), b"same bytes")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            expected = observe_file(root, path)
            write_file(root, path, b"replacement", expected=expected)
            self.assertEqual(path.read_bytes(), b"replacement")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_staged_file_fsync_failure_keeps_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "target"
            path.write_bytes(b"before")
            expected = observe_file(root, path)

            with (
                patch.object(filesystem_module.os, "fsync", side_effect=OSError),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(root, path, b"after", expected=expected)

            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertFalse(raised.exception.committed)
            self.assertEqual(path.read_bytes(), b"before")
            self.assertEqual(list(root.glob(".luwu-tmp-*")), [])

    def test_write_rejects_hardlinks_and_stale_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "target"
            path.write_bytes(b"before")
            expected = observe_file(root, path)
            path.write_bytes(b"changed")

            with self.assertRaises(FilesystemError) as raised:
                write_file(root, path, b"after", expected=expected)
            self.assertEqual(raised.exception.code, "stale_inputs")
            self.assertEqual(path.read_bytes(), b"changed")

            alias = root / "alias"
            os.link(path, alias)
            with self.assertRaises(FilesystemError) as raised:
                write_file(root, path, b"after", expected=expected)
            self.assertEqual(raised.exception.code, "unsafe_file")

    def test_write_stale_callback_does_not_replace_target_or_leak_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "target"
            path.write_bytes(b"before")
            expected = observe_file(root, path)

            def stale() -> None:
                raise RuntimeError("private source contents")

            with self.assertRaises(FilesystemError) as raised:
                write_file(root, path, b"after", expected=expected, check_inputs=stale)

            self.assertEqual(raised.exception.code, "stale_inputs")
            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(path.read_bytes(), b"before")
            self.assertEqual(list(root.glob(".luwu-tmp-*")), [])

    def test_write_rechecks_parent_after_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            target = parent / "file"
            target.write_bytes(b"before")
            expected = observe_file(root, target)
            moved = root / "parent-moved"
            original_fsync = os.fsync
            calls = 0

            def rebind_after_stage(descriptor: int) -> None:
                nonlocal calls
                original_fsync(descriptor)
                calls += 1
                if calls == 1:
                    parent.rename(moved)
                    parent.mkdir()
                    (parent / target.name).write_bytes(b"before")

            with (
                patch.object(
                    filesystem_module.os, "fsync", side_effect=rebind_after_stage
                ),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(root, target, b"after", expected=expected)

            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertEqual(target.read_bytes(), b"before")
            self.assertEqual((moved / target.name).read_bytes(), b"before")
            self.assertEqual(list(moved.glob(".luwu-tmp-*")), [])

    def test_write_rejects_changed_ancestor_snapshot_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            target = parent / "file"
            target.write_bytes(b"before")
            expected = observe_file(root, target)
            moved = root / "parent-moved"
            parent.rename(moved)
            parent.mkdir()
            (parent / target.name).write_bytes(b"before")

            with self.assertRaises(FilesystemError) as raised:
                write_file(root, target, b"after", expected=expected)

            self.assertEqual(raised.exception.code, "stale_inputs")
            self.assertEqual(target.read_bytes(), b"before")
            self.assertEqual((moved / target.name).read_bytes(), b"before")
            self.assertEqual(list(root.glob(".luwu-tmp-*")), [])

    def test_write_rejects_ancestor_creation_after_missing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "parent" / "file"
            expected = observe_file(root, target)
            (root / "parent").mkdir()

            with self.assertRaises(FilesystemError) as raised:
                write_file(root, target, b"after", expected=expected)

            self.assertEqual(raised.exception.code, "stale_inputs")
            self.assertFalse(target.exists())

    def test_write_reports_post_replace_and_replace_exception_honestly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "target"
            path.write_bytes(b"before")
            expected = observe_file(root, path)

            with (
                patch.object(filesystem_module, "sync_directory", side_effect=OSError),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(root, path, b"after", expected=expected)
            self.assertEqual(raised.exception.state, "uncertain")
            self.assertTrue(raised.exception.committed)
            self.assertEqual(path.read_bytes(), b"after")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "target"
            path.write_bytes(b"before")
            expected = observe_file(root, path)
            original_replace = os.replace

            def replace_then_raise(
                source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                src_dir_fd: int | None = None,
                dst_dir_fd: int | None = None,
            ) -> None:
                original_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                raise OSError("private filesystem detail")

            with (
                patch.object(
                    filesystem_module.os, "replace", side_effect=replace_then_raise
                ),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(root, path, b"after", expected=expected)
            self.assertEqual(raised.exception.state, "uncertain")
            self.assertTrue(raised.exception.committed)
            self.assertEqual(path.read_bytes(), b"after")
            self.assertNotIn("private", str(raised.exception))

    def test_write_rejects_substituted_staging_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "target"
            path.write_bytes(b"before")
            expected = observe_file(root, path)
            original_create = filesystem_module._create_temporary_file
            original_fsync = os.fsync
            staged: dict[str, object] = {}

            def remember(parent: int, mode: int) -> tuple[int, str]:
                descriptor, name = original_create(parent, mode)
                staged.update(parent=parent, name=name)
                return descriptor, name

            def substitute(descriptor: int) -> None:
                original_fsync(descriptor)
                parent_fd = staged["parent"]
                name = staged["name"]
                assert isinstance(parent_fd, int) and isinstance(name, str)
                os.unlink(name, dir_fd=parent_fd)
                os.symlink(path.name, name, dir_fd=parent_fd)

            with (
                patch.object(
                    filesystem_module, "_create_temporary_file", side_effect=remember
                ),
                patch.object(filesystem_module.os, "fsync", side_effect=substitute),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(root, path, b"after", expected=expected)

            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertEqual(path.read_bytes(), b"before")
            staged_entries = list(root.glob(".luwu-tmp-*"))
            self.assertEqual(len(staged_entries), 1)
            self.assertTrue(staged_entries[0].is_symlink())
            self.assertEqual(raised.exception.cleanup_state, "foreign_entry_retained")

    def test_write_rechecks_staged_bytes_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "target"
            path.write_bytes(b"before")
            expected = observe_file(root, path)
            original_create = filesystem_module._create_temporary_file
            original_fsync = os.fsync
            staged: dict[str, object] = {}

            def remember(parent: int, mode: int) -> tuple[int, str]:
                descriptor, name = original_create(parent, mode)
                staged.update(parent=parent, name=name)
                return descriptor, name

            def corrupt(descriptor: int) -> None:
                original_fsync(descriptor)
                parent_fd = staged["parent"]
                name = staged["name"]
                assert isinstance(parent_fd, int) and isinstance(name, str)
                staged_fd = os.open(name, os.O_WRONLY, dir_fd=parent_fd)
                try:
                    os.ftruncate(staged_fd, 0)
                    os.write(staged_fd, b"corrupt")
                    original_fsync(staged_fd)
                finally:
                    os.close(staged_fd)

            with (
                patch.object(
                    filesystem_module, "_create_temporary_file", side_effect=remember
                ),
                patch.object(filesystem_module.os, "fsync", side_effect=corrupt),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(root, path, b"after", expected=expected)

            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertEqual(path.read_bytes(), b"before")
            self.assertEqual(list(root.glob(".luwu-tmp-*")), [])

    def test_path_and_mode_validation_are_fixed_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (root.parent / "outside", root / ".." / "outside"):
                with self.subTest(path=path), self.assertRaises(FilesystemError):
                    observe_file(root, path)
            with self.assertRaises(FilesystemError) as raised:
                write_file(
                    root, root / "target", b"data", expected=_absent(), mode=True
                )  # type: ignore[arg-type]
            self.assertEqual(raised.exception.code, "invalid_mode")

            for mode in (0o1000, 0o2000, 0o4000, 0o4755, 0o7777):
                with (
                    self.subTest(mode=oct(mode)),
                    self.assertRaises(FilesystemError) as raised,
                ):
                    write_file(
                        root,
                        root / "target",
                        b"data",
                        expected=observe_file(root, root / "target"),
                        mode=mode,
                    )
                self.assertEqual(raised.exception.code, "invalid_mode")
                with self.assertRaises(FilesystemError) as raised:
                    create_parent(root, root / f"directory-{mode:o}", mode)
                self.assertEqual(raised.exception.code, "invalid_mode")

            world_writable = root / "world-writable"
            create_parent(root, world_writable, 0o777)
            self.assertEqual(stat.S_IMODE(world_writable.stat().st_mode), 0o777)
