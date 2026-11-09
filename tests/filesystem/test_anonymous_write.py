"""Anonymous (O_TMPFILE) staged writes leave no entry when interrupted."""

from __future__ import annotations

import errno
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from luwu import filesystem as filesystem_module
from luwu.filesystem import FilesystemError, observe_file, write_file

REPO = Path(__file__).resolve().parents[2]


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


_SIGKILL_DURING_WRITE = """
import os
import signal
import sys
from pathlib import Path

from luwu import filesystem

root = Path(sys.argv[1])

real_write_all = filesystem._write_all

def killing_write_all(descriptor, data):
    os.write(descriptor, b"partial")
    os.kill(os.getpid(), signal.SIGKILL)

filesystem._write_all = killing_write_all
expected = filesystem.observe_file(root, root / "app.conf")
filesystem.write_file(
    root, root / "app.conf", b"new-content", expected=expected, anonymous=True
)
"""


@unittest.skipUnless(
    sys.platform.startswith("linux") and hasattr(os, "O_TMPFILE"),
    "anonymous staging requires O_TMPFILE",
)
class AnonymousWriteTests(unittest.TestCase):
    def test_anonymous_replace_leaves_no_extra_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.conf"
            target.write_bytes(b"old")
            target.chmod(0o640)
            before = sorted(os.listdir(root))
            expected = observe_file(root, target)

            identity = write_file(
                root, target, b"new-content", expected=expected, anonymous=True
            )

            info = target.stat()
            self.assertEqual(identity, (info.st_dev, info.st_ino))
            self.assertEqual(target.read_bytes(), b"new-content")
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o640)
            self.assertEqual(sorted(os.listdir(root)), before)

    def test_anonymous_create_uses_the_requested_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "created.conf"
            expected = observe_file(root, target)
            self.assertIsNone(expected.identity)

            write_file(
                root,
                target,
                b"fresh",
                expected=expected,
                mode=0o600,
                anonymous=True,
            )

            self.assertEqual(target.read_bytes(), b"fresh")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(sorted(os.listdir(root)), ["created.conf"])

    def test_anonymous_write_without_tmpfile_support_refuses_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.conf"
            target.write_bytes(b"old")
            expected = observe_file(root, target)
            real_open = os.open

            def refusing_open(path, flags, *args, **kwargs):
                # O_TMPFILE includes the O_DIRECTORY bits, so compare fully.
                if flags & os.O_TMPFILE == os.O_TMPFILE:
                    raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
                return real_open(path, flags, *args, **kwargs)

            with (
                patch.object(filesystem_module.os, "open", refusing_open),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(
                    root, target, b"new-content", expected=expected, anonymous=True
                )

            self.assertEqual(raised.exception.code, "anonymous_staging_unsupported")
            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertFalse(raised.exception.committed)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(sorted(os.listdir(root)), ["app.conf"])

    def test_proc_probe_failure_refuses_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.conf"
            target.write_bytes(b"old")
            expected = observe_file(root, target)
            real_stat = os.stat

            def refusing_stat(path, *args, **kwargs):
                if str(path).startswith("/proc/self/fd/"):
                    raise FileNotFoundError(
                        errno.ENOENT, os.strerror(errno.ENOENT), str(path)
                    )
                return real_stat(path, *args, **kwargs)

            descriptors = _fd_count()
            with (
                patch.object(filesystem_module.os, "stat", refusing_stat),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(
                    root, target, b"new-content", expected=expected, anonymous=True
                )

            self.assertEqual(raised.exception.code, "anonymous_staging_unsupported")
            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(sorted(os.listdir(root)), ["app.conf"])
            self.assertEqual(_fd_count(), descriptors)

    def test_proc_link_failure_refuses_with_a_fixed_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.conf"
            target.write_bytes(b"old")
            expected = observe_file(root, target)

            def refusing_link(*args, **kwargs):
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))

            descriptors = _fd_count()
            with (
                patch.object(filesystem_module.os, "link", refusing_link),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(
                    root, target, b"new-content", expected=expected, anonymous=True
                )

            self.assertEqual(raised.exception.code, "anonymous_staging_unsupported")
            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(sorted(os.listdir(root)), ["app.conf"])
            self.assertEqual(_fd_count(), descriptors)

    def test_corrupted_staged_bytes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.conf"
            target.write_bytes(b"old")
            expected = observe_file(root, target)

            def corrupting_pread(descriptor, size, offset):
                return b"\x00" * size

            descriptors = _fd_count()
            with (
                patch.object(filesystem_module.os, "pread", corrupting_pread),
                self.assertRaises(FilesystemError) as raised,
            ):
                write_file(
                    root, target, b"new-content", expected=expected, anonymous=True
                )

            self.assertEqual(raised.exception.code, "unsafe_stage")
            self.assertEqual(raised.exception.state, "not_replaced")
            self.assertFalse(raised.exception.committed)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(sorted(os.listdir(root)), ["app.conf"])
            self.assertEqual(_fd_count(), descriptors)

    def test_sigkill_during_anonymous_write_leaves_no_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.conf"
            target.write_bytes(b"old")
            before = sorted(os.listdir(root))

            completed = subprocess.run(
                [sys.executable, "-B", "-c", _SIGKILL_DURING_WRITE, str(root)],
                env={
                    "PATH": os.defpath,
                    "PYTHONPATH": str(REPO / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )

            self.assertEqual(completed.returncode, -signal.SIGKILL, completed.stderr)
            self.assertEqual(sorted(os.listdir(root)), before)
            self.assertEqual(target.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
