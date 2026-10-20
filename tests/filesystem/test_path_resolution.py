"""Directory path resolution behaves the same with and without openat2."""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from luwu import filesystem as filesystem_module
from luwu.filesystem import (
    FileChangedError,
    capture_directory_boundary,
    observe_file,
    open_directory,
    open_parent_directory,
    verify_directory_binding,
    verify_directory_identity,
)

_NATIVE = filesystem_module._load_openat2()
_Resolver = Callable[[int, bytes, int], int]

if TYPE_CHECKING:
    _ContractBase = unittest.TestCase
else:
    _ContractBase = object


class _ResolutionContract(_ContractBase):
    """Shared expectations; each subclass picks the resolver under test."""

    expected_open_limit: int

    def resolver_under_test(self) -> _Resolver | None:
        raise NotImplementedError

    def setUp(self) -> None:
        self.resolver = self.resolver_under_test()
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        patcher = patch.object(filesystem_module, "_openat2", lambda: self.resolver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _open_descriptors(self) -> int:
        return len(os.listdir("/proc/self/fd"))

    def test_opens_the_directory_at_the_named_path(self) -> None:
        (self.root / "a" / "b").mkdir(parents=True)
        descriptor = open_directory(self.root / "a" / "b")
        self.addCleanup(os.close, descriptor)
        self.assertEqual(
            os.fstat(descriptor).st_ino, (self.root / "a" / "b").stat().st_ino
        )
        self.assertFalse(os.get_inheritable(descriptor))

    def test_refuses_a_symlink_in_any_position(self) -> None:
        (self.root / "real" / "inner").mkdir(parents=True)
        (self.root / "link").symlink_to(self.root / "real")
        (self.root / "real" / "tail").symlink_to(self.root / "real" / "inner")
        for path in (
            self.root / "link",
            self.root / "link" / "inner",
            self.root / "real" / "tail",
        ):
            with (
                self.subTest(path=path.relative_to(self.root)),
                self.assertRaises(OSError),
            ):
                open_directory(path)

    def test_reports_absent_and_non_directory_entries_distinctly(self) -> None:
        (self.root / "file").write_bytes(b"x")
        with self.assertRaises(FileNotFoundError):
            open_directory(self.root / "absent" / "deeper")
        with self.assertRaises(OSError) as raised:
            open_directory(self.root / "file")
        self.assertNotIsInstance(raised.exception, FileNotFoundError)

    def test_rejects_parent_references_before_touching_the_filesystem(self) -> None:
        with self.assertRaises(ValueError):
            open_directory(self.root / ".." / self.root.name)

    def test_parent_directory_walk_stays_beneath_its_root(self) -> None:
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "escape").symlink_to(self.root / "a")
        descriptor, name = open_parent_directory(self.root, self.root / "a" / "b" / "f")
        self.addCleanup(os.close, descriptor)
        self.assertEqual(name, "f")
        self.assertEqual(
            os.fstat(descriptor).st_ino, (self.root / "a" / "b").stat().st_ino
        )
        with self.assertRaises(OSError):
            open_parent_directory(self.root, self.root / "escape" / "f")

    def test_relative_walk_cannot_climb_out_of_its_start_directory(self) -> None:
        (self.root / "a").mkdir()
        start = open_directory(self.root / "a")
        with self.assertRaises(ValueError):
            filesystem_module._descend(start, ("..",))
        with self.assertRaises(OSError):
            os.fstat(start)

    def test_identity_check_detects_a_directory_swapped_at_its_path(self) -> None:
        (self.root / "a").mkdir()
        descriptor = open_directory(self.root / "a")
        self.addCleanup(os.close, descriptor)
        verify_directory_identity(descriptor, self.root / "a")
        (self.root / "a").rename(self.root / "moved")
        (self.root / "a").mkdir()
        with self.assertRaises(FileChangedError):
            verify_directory_identity(descriptor, self.root / "a")

    def test_binding_check_detects_a_replaced_intermediate_directory(self) -> None:
        (self.root / "a" / "b").mkdir(parents=True)
        boundary = capture_directory_boundary(self.root)
        descriptor = open_directory(self.root / "a" / "b")
        self.addCleanup(os.close, descriptor)
        verify_directory_binding(boundary, Path("a/b"), descriptor)
        (self.root / "a").rename(self.root / "moved")
        (self.root / "a" / "b").mkdir(parents=True)
        with self.assertRaises(FileChangedError):
            verify_directory_binding(boundary, Path("a/b"), descriptor)

    def test_failed_and_successful_walks_leak_no_descriptors(self) -> None:
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "link").symlink_to(self.root / "a")
        before = self._open_descriptors()
        for _ in range(3):
            os.close(open_directory(self.root / "a" / "b"))
            for path in (self.root / "link" / "b", self.root / "absent"):
                with self.assertRaises(OSError):
                    open_directory(path)
            observe_file(self.root, self.root / "a" / "b" / "f")
        self.assertEqual(self._open_descriptors(), before)

    def test_one_observation_costs_a_bounded_number_of_opens(self) -> None:
        deep = self.root.joinpath(*(f"d{index}" for index in range(8)))
        deep.mkdir(parents=True)
        (deep / "f").write_bytes(b"x")
        real_open = os.open
        opens = {"count": 0}

        def counting(
            path_name: str | Path, flags: int, *, dir_fd: int | None = None
        ) -> int:
            opens["count"] += 1
            return real_open(path_name, flags, dir_fd=dir_fd)

        def counting_resolver(dirfd: int, path: bytes, flags: int) -> int:
            opens["count"] += 1
            assert self.resolver is not None
            return self.resolver(dirfd, path, flags)

        resolver = counting_resolver if self.resolver is not None else None
        with (
            patch.object(filesystem_module, "_openat2", lambda: resolver),
            patch.object(filesystem_module.os, "open", counting),
        ):
            observe_file(self.root, deep / "f")
        self.assertLessEqual(opens["count"], self.expected_open_limit)


class FallbackResolutionTests(_ResolutionContract, unittest.TestCase):
    expected_open_limit = 120

    def resolver_under_test(self) -> _Resolver | None:
        return None


@unittest.skipIf(_NATIVE is None, "openat2 is unavailable on this system")
class Openat2ResolutionTests(_ResolutionContract, unittest.TestCase):
    expected_open_limit = 45

    def resolver_under_test(self) -> _Resolver | None:
        return _NATIVE


class Openat2ProbeTests(unittest.TestCase):
    def test_a_refused_probe_keeps_the_portable_walk(self) -> None:
        with (
            patch.object(filesystem_module.sys, "platform", "linux"),
            patch("ctypes.get_errno", return_value=errno.EPERM),
            patch("ctypes.CDLL") as library,
        ):
            library.return_value.syscall.return_value = -1
            self.assertIsNone(filesystem_module._load_openat2())

    def test_a_non_linux_platform_never_probes(self) -> None:
        with (
            patch.object(filesystem_module.sys, "platform", "darwin"),
            patch("ctypes.CDLL") as library,
        ):
            self.assertIsNone(filesystem_module._load_openat2())
        library.assert_not_called()

    def test_the_probe_result_is_computed_once(self) -> None:
        with (
            patch.object(
                filesystem_module, "_openat2_resolver", filesystem_module._UNPROBED
            ),
            patch.object(filesystem_module, "_load_openat2", return_value=None) as load,
        ):
            filesystem_module._openat2()
            filesystem_module._openat2()
        load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
