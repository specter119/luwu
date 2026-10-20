"""Tests for the shared no-follow descriptor walk primitives."""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luwu import filesystem as filesystem_module
from luwu.filesystem import (
    DirectoryBoundary,
    FileChangedError,
    FilesystemError,
    RootChangedError,
    UnsafeEntryError,
    capture_directory_boundary,
    list_directory,
    observe_file,
    open_boundary,
    open_child_directory,
    open_child_file,
    open_directory,
    read_stable_file,
    verify_directory_binding,
)


class DescriptorWalkTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.parent = open_directory(self.root)
        self.addCleanup(os.close, self.parent)

    def _stat(self, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=self.parent, follow_symlinks=False)

    def test_child_directory_requires_a_real_directory(self) -> None:
        (self.root / "real").mkdir()
        (self.root / "file").write_bytes(b"x")
        (self.root / "link").symlink_to(self.root / "real")

        descriptor, info = open_child_directory(self.parent, "real")
        os.close(descriptor)
        self.assertEqual(info.st_ino, (self.root / "real").stat().st_ino)
        for name in ("file", "link"):
            with self.subTest(name=name), self.assertRaises(UnsafeEntryError):
                open_child_directory(self.parent, name)
        with self.assertRaises(FileNotFoundError):
            open_child_directory(self.parent, "absent")

    def test_child_directory_rejects_an_entry_that_is_not_the_observed_one(
        self,
    ) -> None:
        (self.root / "slot").mkdir()
        observed = self._stat("slot")
        forged = os.stat_result((observed.st_mode, observed.st_ino + 1, *observed[2:]))

        with self.assertRaises(FileChangedError):
            open_child_directory(self.parent, "slot", observed=forged)

    def test_child_file_requires_a_single_linked_regular_file(self) -> None:
        (self.root / "plain").write_bytes(b"data")
        (self.root / "dir").mkdir()
        (self.root / "link").symlink_to(self.root / "plain")
        os.link(self.root / "plain", self.root / "twin")
        (self.root / "solo").write_bytes(b"data")

        descriptor, info = open_child_file(self.parent, "solo")
        os.close(descriptor)
        self.assertEqual(info.st_size, 4)
        for name in ("dir", "link", "plain", "twin"):
            with self.subTest(name=name), self.assertRaises(UnsafeEntryError):
                open_child_file(self.parent, name)
        with self.assertRaises(FileNotFoundError):
            open_child_file(self.parent, "absent")

    def test_child_file_rejects_an_entry_that_is_not_the_observed_one(self) -> None:
        (self.root / "slot").write_bytes(b"data")
        observed = self._stat("slot")
        forged = os.stat_result((observed.st_mode, observed.st_ino + 1, *observed[2:]))

        with self.assertRaises(FileChangedError):
            open_child_file(self.parent, "slot", observed=forged)

    def test_stable_read_returns_bytes_and_detects_a_change_during_the_read(
        self,
    ) -> None:
        path = self.root / "doc"
        path.write_bytes(b"before")
        data, info = read_stable_file(self.parent, "doc")
        self.assertEqual((data, info.st_size), (b"before", 6))

        original = filesystem_module.read_descriptor

        def read_then_modify(descriptor: int) -> bytes:
            content = original(descriptor)
            path.write_bytes(b"after-longer")
            return content

        with (
            patch.object(filesystem_module, "read_descriptor", read_then_modify),
            self.assertRaises(FileChangedError),
        ):
            read_stable_file(self.parent, "doc")

    def test_stable_read_detects_replacement_by_another_file(self) -> None:
        path = self.root / "doc"
        path.write_bytes(b"before")
        original = filesystem_module.read_descriptor

        def read_then_replace(descriptor: int) -> bytes:
            content = original(descriptor)
            # Keep the opened inode linked so only the name-to-inode check can notice.
            path.rename(self.root / "kept")
            path.write_bytes(content)
            return content

        with (
            patch.object(filesystem_module, "read_descriptor", read_then_replace),
            self.assertRaises(FileChangedError),
        ):
            read_stable_file(self.parent, "doc")

    def test_child_file_detects_a_swap_between_open_and_recheck(self) -> None:
        (self.root / "slot").write_bytes(b"data")
        real_open = os.open

        def open_then_swap(path: str, flags: int, *, dir_fd: int | None = None) -> int:
            descriptor = real_open(path, flags, dir_fd=dir_fd)
            if path == "slot":
                (self.root / "slot").rename(self.root / "kept")
                (self.root / "slot").write_bytes(b"data")
            return descriptor

        with (
            patch.object(filesystem_module.os, "open", open_then_swap),
            self.assertRaises(FileChangedError),
        ):
            open_child_file(self.parent, "slot")

    def test_child_file_open_failures_split_races_from_other_errors(self) -> None:
        (self.root / "slot").write_bytes(b"data")
        real_open = os.open

        def failing(error: OSError):
            def open_fails(path: str, flags: int, *, dir_fd: int | None = None) -> int:
                if path == "slot":
                    raise error
                return real_open(path, flags, dir_fd=dir_fd)

            return open_fails

        for error, expected in (
            (OSError(errno.ELOOP, "loop"), FileChangedError),
            (FileNotFoundError(errno.ENOENT, "gone"), FileChangedError),
            (PermissionError(errno.EACCES, "denied"), PermissionError),
            (OSError(errno.EMFILE, "limit"), OSError),
        ):
            with (
                self.subTest(error=error),
                patch.object(filesystem_module.os, "open", failing(error)),
                self.assertRaises(expected) as raised,
            ):
                open_child_file(self.parent, "slot")
            if expected is OSError:
                self.assertNotIsInstance(raised.exception, FileChangedError)

    def test_observation_keeps_read_errors_distinct_from_unsafe_files(self) -> None:
        path = self.root / "doc"
        path.write_bytes(b"data")

        def read_fails(descriptor: int) -> bytes:
            raise OSError(errno.EIO, "io")

        with (
            patch.object(filesystem_module, "read_descriptor", read_fails),
            self.assertRaises(FilesystemError) as raised,
        ):
            observe_file(self.root, path)
        self.assertEqual(raised.exception.code, "observe_failed")

        real_open = os.open

        def denied(path_name: str, flags: int, *, dir_fd: int | None = None) -> int:
            if path_name == "doc":
                raise PermissionError(errno.EACCES, "denied")
            return real_open(path_name, flags, dir_fd=dir_fd)

        with (
            patch.object(filesystem_module.os, "open", denied),
            self.assertRaises(FilesystemError) as raised,
        ):
            observe_file(self.root, path)
        self.assertEqual(raised.exception.code, "unsafe_file")

    def test_boundary_open_detects_a_replaced_root(self) -> None:
        directory = self.root / "boundary"
        directory.mkdir()
        boundary = capture_directory_boundary(directory)
        os.close(open_boundary(boundary))
        forged = DirectoryBoundary(boundary.path, boundary.device, boundary.inode + 1)

        with self.assertRaises(RootChangedError):
            open_boundary(forged)
        with self.assertRaises(RootChangedError):
            forged.verify()

    def test_directory_binding_detects_a_directory_swapped_at_its_path(self) -> None:
        boundary = capture_directory_boundary(self.root)
        (self.root / "a").mkdir()
        descriptor = open_directory(self.root / "a")
        self.addCleanup(os.close, descriptor)
        verify_directory_binding(boundary, Path("a"), descriptor)

        (self.root / "a").rename(self.root / "moved")
        (self.root / "a").mkdir()

        with self.assertRaises(FileChangedError):
            verify_directory_binding(boundary, Path("a"), descriptor)

    def test_ancestry_capture_detects_an_intermediate_directory_swapped_mid_walk(
        self,
    ) -> None:
        (self.root / "a" / "b").mkdir(parents=True)
        boundary = capture_directory_boundary(self.root)
        original_verify = DirectoryBoundary.verify

        def swap_then_verify(instance: DirectoryBoundary) -> None:
            (self.root / "a").rename(self.root / "moved")
            (self.root / "a" / "b").mkdir(parents=True)
            original_verify(instance)

        with (
            patch.object(DirectoryBoundary, "verify", swap_then_verify),
            self.assertRaises(FilesystemError) as raised,
        ):
            filesystem_module._capture_ancestry(boundary, self.root, Path("a/b/file"))
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_ancestry_capture_opens_grow_linearly_with_depth(self) -> None:
        real_open = os.open
        opens = {"count": 0}

        def counting(
            path_name: str | Path, flags: int, *, dir_fd: int | None = None
        ) -> int:
            opens["count"] += 1
            return real_open(path_name, flags, dir_fd=dir_fd)

        def cost(depth: int) -> int:
            parts = [f"d{index}" for index in range(depth)]
            (self.root.joinpath(*parts)).mkdir(parents=True)
            boundary = capture_directory_boundary(self.root)
            opens["count"] = 0
            with patch.object(filesystem_module.os, "open", counting):
                filesystem_module._capture_ancestry(
                    boundary, self.root, Path(*parts, "leaf")
                )
            return opens["count"]

        shallow = cost(6)
        (self.root / "d0").rename(self.root / "old")
        deep = cost(12)
        self.assertLessEqual(deep - shallow, 4 * 6)

    def test_directory_binding_rejects_a_symlinked_component(self) -> None:
        boundary = capture_directory_boundary(self.root)
        (self.root / "real").mkdir()
        (self.root / "link").symlink_to(self.root / "real")
        descriptor = open_directory(self.root / "real")
        self.addCleanup(os.close, descriptor)

        with self.assertRaises(OSError):
            verify_directory_binding(boundary, Path("link"), descriptor)

    def test_list_directory_is_sorted(self) -> None:
        for name in ("b", "a", "c"):
            (self.root / name).write_bytes(b"")

        self.assertEqual(list_directory(self.parent), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
