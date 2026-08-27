"""Descriptor-relative filesystem primitives for the M1 safety boundary."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


class NotRegularFileError(OSError):
    """A path component was not a regular file."""


class FileChangedError(OSError):
    """A file changed between its descriptor-free and descriptor checks."""


def open_parent_directory(root: Path, path: Path) -> tuple[int, str]:
    """Open a target's parent without following any directory symlink."""

    relative = path.relative_to(root)
    if not relative.parts:
        raise ValueError("path must name an entry below the root")
    descriptor = _open_directory(root)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            previous = descriptor
            descriptor = child
            os.close(previous)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, relative.parts[-1]


def read_regular_file_at(
    parent_descriptor: int,
    name: str,
) -> tuple[bytes, os.stat_result]:
    """Read one regular file using a no-follow descriptor-relative open."""

    info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise NotRegularFileError(name)

    descriptor = os.open(name, _read_flags(), dir_fd=parent_descriptor)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != info.st_dev
            or current.st_ino != info.st_ino
        ):
            raise FileChangedError(name)
        return read_descriptor(descriptor), current
    finally:
        os.close(descriptor)


def read_descriptor(descriptor: int) -> bytes:
    """Read all bytes from an already-open descriptor."""

    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def create_temporary_file(parent_descriptor: int, prefix: str) -> tuple[int, str]:
    """Create a private temporary file in an already-open parent directory."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag()
    for _ in range(100):
        name = f"{prefix}{uuid.uuid4().hex}"
        try:
            return (
                os.open(name, flags, 0o600, dir_fd=parent_descriptor),
                name,
            )
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a temporary path")


def create_temporary_symlink(
    parent_descriptor: int,
    prefix: str,
    target: str,
) -> str:
    """Create a temporary symlink in an already-open parent directory."""

    for _ in range(100):
        name = f"{prefix}{uuid.uuid4().hex}"
        try:
            os.symlink(target, name, dir_fd=parent_descriptor)
            return name
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a temporary symlink")


def resolve_link_target(path: Path, link_text: str) -> Path:
    """Resolve a link target relative to the directory containing ``path``."""

    candidate = Path(link_text)
    if not candidate.is_absolute():
        candidate = path.parent / candidate
    return candidate.resolve(strict=False)


def sync_directory(descriptor: int) -> None:
    """Best-effort durability for an already-open directory descriptor."""

    try:
        os.fsync(descriptor)
    except OSError:
        pass


def _open_directory(path: Path) -> int:
    return os.open(path, _directory_flags())


def _directory_flags() -> int:
    return os.O_RDONLY | _required_flag("O_DIRECTORY") | _no_follow_flag()


def _read_flags() -> int:
    return os.O_RDONLY | _required_flag("O_NONBLOCK") | _no_follow_flag()


def _no_follow_flag() -> int:
    return _required_flag("O_NOFOLLOW")


def _required_flag(name: str) -> int:
    try:
        return getattr(os, name)
    except AttributeError as exc:
        raise NotImplementedError(f"filesystem flag {name} is unavailable") from exc
