"""Bounded, descriptor-relative filesystem primitives.

Directory locks coordinate cooperating Luwu writers only. These primitives do
not defend against a malicious same-UID process, a privileged actor, or a
malicious filesystem. ``create_parent`` returns the no-follow directory
identity observed and rechecked at the requested path; it is not proof that
the inode observed after ``mkdir`` came exclusively from that syscall.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from luwu.errors import LuwuError


class FileChangedError(OSError):
    """A file changed between its descriptor-free and descriptor checks."""


class RootChangedError(FileChangedError):
    """A captured directory boundary no longer names the same directory."""


class UnsafeEntryError(OSError):
    """A directory entry has the wrong type or link count for its role."""


@dataclass(frozen=True, slots=True)
class DirectoryBoundary:
    """A directory path captured without following any path component."""

    path: Path
    device: int
    inode: int

    def verify(self) -> None:
        """Reject a replaced root or any symlink/rebinding in its path."""

        os.close(open_boundary(self))


def capture_directory_boundary(path: Path) -> DirectoryBoundary:
    """Capture an existing absolute directory through no-follow opens."""

    descriptor = open_directory(path)
    try:
        current = os.fstat(descriptor)
        return DirectoryBoundary(path, current.st_dev, current.st_ino)
    finally:
        os.close(descriptor)


def open_parent_directory(root: Path, path: Path) -> tuple[int, str]:
    """Open a target's parent without following any directory symlink."""

    relative = path.relative_to(root)
    if not relative.parts:
        raise ValueError("path must name an entry below the root")
    return (
        _descend(open_directory(root), relative.parts[:-1]),
        relative.parts[-1],
    )


def read_descriptor(descriptor: int) -> bytes:
    """Read all bytes from an already-open descriptor."""

    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def verify_directory_identity(descriptor: int, path: Path) -> None:
    """Ensure an open directory is still reachable at its intended path."""

    expected_descriptor = -1
    try:
        expected_descriptor = open_directory(path)
        expected = os.fstat(expected_descriptor)
        current = os.fstat(descriptor)
    except (OSError, ValueError):
        raise FileChangedError(str(path)) from None
    finally:
        if expected_descriptor >= 0:
            os.close(expected_descriptor)
    if expected.st_dev != current.st_dev or expected.st_ino != current.st_ino:
        raise FileChangedError(str(path))


def sync_directory(descriptor: int) -> None:
    """Flush an already-open directory descriptor or report uncertainty."""

    os.fsync(descriptor)


def lock_directory(descriptor: int) -> None:
    """Take a non-blocking advisory lock for cooperating Luwu writers."""

    try:
        import fcntl
    except ImportError as exc:
        raise NotImplementedError("directory locking is unavailable") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise BlockingIOError("target directory is being changed") from exc


def unlock_directory(descriptor: int) -> None:
    """Release a lock previously acquired with :func:`lock_directory`."""

    try:
        import fcntl
    except ImportError as exc:
        raise NotImplementedError("directory locking is unavailable") from exc
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def open_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path must be absolute and contain no '..'")

    return _descend(os.open(path.anchor, directory_flags()), path.parts[1:])


def _descend(descriptor: int, parts: tuple[str, ...]) -> int:
    """Open each component below a descriptor, consuming the descriptor."""

    try:
        for component in parts:
            child = os.open(component, directory_flags(), dir_fd=descriptor)
            previous = descriptor
            descriptor = child
            os.close(previous)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_boundary(boundary: DirectoryBoundary) -> int:
    """Open a captured directory and require its recorded identity."""

    descriptor = open_directory(boundary.path)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (boundary.device, boundary.inode):
            raise RootChangedError(str(boundary.path))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def verify_directory_binding(
    boundary: DirectoryBoundary, relative: Path, descriptor: int
) -> None:
    """Require that ``descriptor`` is still the directory at boundary/relative."""

    current = _descend(open_boundary(boundary), relative.parts)
    try:
        expected = os.fstat(descriptor)
        actual = os.fstat(current)
    finally:
        os.close(current)
    if _identity(expected) != _identity(actual):
        raise FileChangedError(str(relative))


def list_directory(descriptor: int) -> list[str]:
    """Return the sorted entry names of an open directory."""

    return sorted(os.listdir(descriptor))


def child_is_directory(parent: int, name: str) -> bool:
    """Tell whether a child entry is a directory, without following links."""

    return stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)


def open_child_directory(
    parent: int, name: str, *, observed: os.stat_result | None = None
) -> tuple[int, os.stat_result]:
    """Open a child directory without following links and bind its identity.

    ``observed`` is an earlier no-follow stat of the entry; without it the
    entry is statted here. Raises ``FileNotFoundError`` for an absent entry,
    ``UnsafeEntryError`` for a non-directory, and ``FileChangedError`` when the
    opened directory is not the one observed.
    """

    info = observed or os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeEntryError(name)
    child = os.open(name, directory_flags(), dir_fd=parent)
    try:
        opened = os.fstat(child)
        if _identity(opened) != _identity(info):
            raise FileChangedError(name)
    except BaseException:
        os.close(child)
        raise
    return child, opened


def open_child_file(
    parent: int, name: str, *, observed: os.stat_result | None = None
) -> tuple[int, os.stat_result]:
    """Open a private regular child file without following links.

    Raises ``FileNotFoundError`` for an absent entry, ``UnsafeEntryError`` unless
    the entry is a single-linked regular file, and ``FileChangedError`` when the
    opened file is not the entry that was observed.
    """

    info = observed or os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafeEntryError(name)
    try:
        descriptor = os.open(name, read_flags(), dir_fd=parent)
    except OSError as error:
        if error.errno in _ENTRY_RACE_ERRNOS:
            raise FileChangedError(name) from None
        raise
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(info)
        ):
            raise FileChangedError(name)
        latest = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(latest) != _identity(info):
            raise FileChangedError(name)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def read_stable_file(parent: int, name: str) -> tuple[bytes, os.stat_result]:
    """Read a private regular file and prove it did not change while read."""

    descriptor, opened = open_child_file(parent, name)
    try:
        data = read_descriptor(descriptor)
        after = os.fstat(descriptor)
        latest = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or _identity(after) != _identity(opened)
            or _identity(latest) != _identity(after)
            or not stat.S_ISREG(latest.st_mode)
            or latest.st_nlink != 1
            or _file_signature(after) != _file_signature(opened)
        ):
            raise FileChangedError(name)
        return data, after
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class FileObservation:
    """A stable snapshot of one regular file, symlink, or absent leaf.

    A symlink observation (``observe_link``) carries ``link_target`` and no
    content. ``foreign`` marks an entry that is neither absent nor a symlink.
    """

    data: bytes | None = field(repr=False)
    identity: tuple[int, int] | None
    digest: str | None = field(repr=False)
    mode: int | None
    existing_ancestors: tuple[tuple[Path, tuple[int, int]], ...] = ()
    missing_chain: tuple[Path, ...] = ()
    link_target: str | None = None
    foreign: bool = False


class FilesystemError(LuwuError):
    """A fixed-message filesystem error with an honest mutation outcome."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        state: str = "not_replaced",
        committed: bool = False,
        created_identity: tuple[int, int] | None = None,
        cleanup_state: str = "not_needed",
    ) -> None:
        super().__init__(message, code=code)
        self.state = state
        self.committed = committed
        self.created_identity = created_identity
        self.cleanup_state = cleanup_state


# An open that fails this way means the entry was swapped or removed after the
# no-follow stat. Other errors (permissions, descriptor limits) are not races.
_ENTRY_RACE_ERRNOS = frozenset({errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EMLINK})
_ABSENT = FileObservation(None, None, None, None)
_NOT_REPLACED = "not_replaced"
_UNCERTAIN = "uncertain"
_CLEANUP_NOT_NEEDED = "not_needed"
_CLEANUP_REMOVED = "removed"
_CLEANUP_MISSING = "already_missing"
_CLEANUP_FOREIGN = "foreign_entry_retained"
_CLEANUP_UNBOUND = "unbound_entry_retained"
_CLEANUP_FAILED = "failed"


def observe_file(root: Path, path: Path) -> FileObservation:
    """Observe a no-follow regular file beneath ``root`` or return absent."""

    return _observe_leaf(root, path, _observe_entry)


def observe_link(root: Path, path: Path) -> FileObservation:
    """Observe a leaf as an absent entry, a symlink, or a foreign entry."""

    return _observe_leaf(root, path, _observe_link_entry)


def _observe_leaf(
    root: Path, path: Path, observe_entry: Callable[[int, str], FileObservation]
) -> FileObservation:
    boundary, relative = _capture_paths(root, path)
    existing, missing = _capture_ancestry(boundary, root, relative)
    if missing:
        _verify_same_ancestry(boundary, root, relative, existing, missing)
        return _with_ancestry(_ABSENT, existing, missing)
    try:
        parent, name = open_parent_directory(root, path)
    except FileNotFoundError:
        _verify_root(boundary)
        _verify_same_ancestry(boundary, root, relative, existing, missing)
        return _with_ancestry(_ABSENT, existing, missing)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem path is unsafe", "unsafe_path") from None

    try:
        try:
            observation = observe_entry(parent, name)
        except FileNotFoundError:
            observation = _ABSENT
        _verify_root_parent(boundary, parent, path.parent)
        _verify_same_ancestry(boundary, root, relative, existing, missing)
        return _with_ancestry(observation, existing, missing)
    except FilesystemError:
        raise
    except (OSError, NotImplementedError, ValueError):
        raise _error(
            "filesystem entry could not be observed", "observe_failed"
        ) from None
    except Exception:  # noqa: BLE001 - keep unexpected filesystem errors metadata-only
        raise _error(
            "filesystem entry could not be observed", "observe_failed"
        ) from None
    finally:
        try:
            os.close(parent)
        except (OSError, ValueError):
            raise _error(
                "filesystem observation cleanup failed", "observe_failed"
            ) from None


def missing_parents(root: Path, path: Path) -> tuple[Path, ...]:
    """Return missing parent directories in root-first order."""

    boundary, relative = _capture_paths(root, path)
    existing, missing = _capture_ancestry(boundary, root, relative)
    _verify_same_ancestry(boundary, root, relative, existing, missing)
    return missing


def create_parent(
    root: Path,
    path: Path,
    mode: int,
    *,
    check_inputs: Callable[[], object] | None = None,
) -> tuple[int, int]:
    """Create one directory and return its observed, rechecked identity.

    The returned inode identity binds to the no-follow descriptor and named
    entry observed during this call. It does not prove exclusive creation
    provenance against a same-UID actor racing ``mkdir`` and ``open``.
    """

    boundary, relative = _capture_paths(root, path)
    _validate_mode(mode)
    try:
        parent, name = open_parent_directory(root, path)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem parent is unsafe", "unsafe_path") from None

    locked = False
    child_descriptor: int | None = None
    created = False
    create_attempted = False
    failure: FilesystemError | None = None
    identity: tuple[int, int] | None = None
    cleanup_state = _CLEANUP_NOT_NEEDED
    try:
        lock_directory(parent)
        locked = True
        _verify_root_parent(boundary, parent, path.parent)
        existing, missing = _capture_ancestry(boundary, root, relative)
        if missing:
            raise _error("filesystem parent is unsafe", "unsafe_path")
        _require_missing_entry(parent, name)

        _run_check_inputs(check_inputs)
        _verify_same_ancestry(boundary, root, relative, existing, missing)
        _verify_root_parent(boundary, parent, path.parent)
        _require_missing_entry(parent, name)

        create_attempted = True
        try:
            # The restrictive initial mode limits exposure before the held
            # descriptor receives the exact requested permissions.
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            create_attempted = False
            raise _error("filesystem parent already exists", "stale_inputs") from None
        created = True

        # Keep mkdir and the no-follow open adjacent: the descriptor is the
        # identity anchor for fchmod and subsequent checks.
        child_descriptor = os.open(name, directory_flags(), dir_fd=parent)
        opened = os.fstat(child_descriptor)
        identity = _identity(opened)
        os.fchmod(child_descriptor, mode)
        opened = os.fstat(child_descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _identity(opened) != identity
            or stat.S_IMODE(opened.st_mode) != mode
        ):
            raise _error(
                "created filesystem parent changed",
                "parent_create_unknown",
                state=_UNCERTAIN,
                committed=True,
            )
        _verify_directory_entry(parent, name, identity, mode)

        _verify_same_ancestry(boundary, root, relative, existing, missing)
        _verify_root_parent(boundary, parent, path.parent)

        sync_directory(child_descriptor)
        sync_directory(parent)
        _verify_root_parent(boundary, parent, path.parent)
        _verify_directory_entry(parent, name, identity, mode)
        _verify_same_ancestry(boundary, root, relative, existing, missing)
    except FilesystemError as exc:
        failure = _with_create_outcome(
            exc,
            created,
            None if exc.code == "parent_create_unknown" else identity,
        )
    except (OSError, NotImplementedError, ValueError):
        failure = _error(
            "filesystem parent creation could not be confirmed",
            "parent_create_unknown"
            if created or create_attempted
            else "parent_create_failed",
            state=_UNCERTAIN if created or create_attempted else _NOT_REPLACED,
            committed=created,
            created_identity=identity if created else None,
        )
    except Exception:  # noqa: BLE001 - preserve creation uncertainty without raw details
        failure = _error(
            "filesystem parent creation could not be confirmed",
            "parent_create_unknown"
            if created or create_attempted
            else "parent_create_failed",
            state=_UNCERTAIN if created or create_attempted else _NOT_REPLACED,
            committed=created,
            created_identity=identity if created else None,
        )
    finally:
        cleanup_failed = False
        if child_descriptor is not None:
            try:
                os.close(child_descriptor)
            except (OSError, ValueError):
                cleanup_failed = True
                cleanup_state = _CLEANUP_FAILED
        if locked:
            try:
                unlock_directory(parent)
            except (OSError, NotImplementedError, ValueError):
                cleanup_failed = True
                cleanup_state = _CLEANUP_FAILED
        try:
            os.close(parent)
        except (OSError, ValueError):
            cleanup_failed = True
            if cleanup_state == _CLEANUP_NOT_NEEDED:
                cleanup_state = _CLEANUP_FAILED
        if (
            cleanup_state == _CLEANUP_NOT_NEEDED
            and failure is not None
            and failure.cleanup_state != _CLEANUP_NOT_NEEDED
        ):
            cleanup_state = failure.cleanup_state
        if failure is not None:
            failure.cleanup_state = cleanup_state
        if cleanup_failed:
            cleanup_uncertain = created or (
                failure is not None and failure.state == _UNCERTAIN
            )
            failure = _error(
                "filesystem parent creation cleanup is uncertain",
                "parent_create_unknown" if cleanup_uncertain else "cleanup_unknown",
                state=_UNCERTAIN if cleanup_uncertain else _NOT_REPLACED,
                committed=created
                or (failure.committed if failure is not None else False),
                created_identity=(
                    failure.created_identity if failure is not None else identity
                ),
                cleanup_state=cleanup_state,
            )

    if failure is not None:
        raise failure from None
    assert identity is not None
    return identity


def write_file(
    root: Path,
    path: Path,
    data: bytes,
    *,
    expected: FileObservation,
    mode: int | None = None,
    check_inputs: Callable[[], object] | None = None,
) -> tuple[int, int]:
    """Atomically replace one regular file after rechecking its observation."""

    boundary, relative = _capture_paths(root, path)
    if type(data) is not bytes:
        raise _error("filesystem content is invalid", "invalid_content")
    if mode is not None:
        _validate_mode(mode)
    if not isinstance(expected, FileObservation):
        raise _error("filesystem observation is invalid", "invalid_observation")

    try:
        parent, name = open_parent_directory(root, path)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem parent is unsafe", "unsafe_path") from None

    temporary: str | None = None
    staged_identity: tuple[int, int] | None = None
    locked = False
    replace_attempted = False
    replaced = False
    failure: FilesystemError | None = None
    try:
        lock_directory(parent)
        locked = True
        _verify_root_parent(boundary, parent, path.parent)
        _verify_expected_ancestry(boundary, root, relative, expected)
        current = _observe_entry(parent, name)
        if not _same_file(current, expected):
            raise _error("filesystem inputs are stale", "stale_inputs")

        final_mode = _write_mode(expected, mode)
        _validate_mode(final_mode)
        descriptor, temporary = _create_temporary_file(parent)
        try:
            staged_identity = _identity(os.fstat(descriptor))
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, data)
            os.fchmod(descriptor, final_mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        _run_check_inputs(check_inputs)

        _verify_root_parent(boundary, parent, path.parent)
        _verify_expected_ancestry(boundary, root, relative, expected)
        current = _observe_entry(parent, name)
        if not _same_file(current, expected):
            raise _error("filesystem inputs are stale", "stale_inputs")
        _verify_staged_entry(
            parent, path.parent, temporary, staged_identity, final_mode, data
        )

        replace_attempted = True
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        replaced = True
        sync_directory(parent)
        _verify_root_parent(boundary, parent, path.parent)
        written = _observe_entry(parent, name)
        if (
            written.identity != staged_identity
            or written.data != data
            or written.mode != final_mode
        ):
            raise _error(
                "filesystem write result is uncertain",
                "write_unknown",
                state=_UNCERTAIN,
                committed=True,
            )
        _verify_expected_ancestry(boundary, root, relative, expected)
    except FilesystemError as exc:
        committed = replaced or (
            replace_attempted and _replacement_is_staged(parent, name, staged_identity)
        )
        failure = _with_write_outcome(exc, committed, replace_attempted)
    except (OSError, NotImplementedError, ValueError):
        committed = replaced or (
            replace_attempted and _replacement_is_staged(parent, name, staged_identity)
        )
        failure = _error(
            "filesystem write outcome is uncertain"
            if committed or replace_attempted
            else "filesystem write was not replaced",
            "write_unknown" if committed or replace_attempted else "write_failed",
            state=_UNCERTAIN if committed or replace_attempted else _NOT_REPLACED,
            committed=committed,
        )
    except Exception:  # noqa: BLE001 - classify unexpected failures at the replace boundary
        committed = replaced or (
            replace_attempted and _replacement_is_staged(parent, name, staged_identity)
        )
        failure = _error(
            "filesystem write outcome is uncertain"
            if committed or replace_attempted
            else "filesystem write was not replaced",
            "write_unknown" if committed or replace_attempted else "write_failed",
            state=_UNCERTAIN if committed or replace_attempted else _NOT_REPLACED,
            committed=committed,
        )
    finally:
        committed = replaced or (
            replace_attempted and _replacement_is_staged(parent, name, staged_identity)
        )
        cleanup_failed = False
        cleanup_state = _CLEANUP_NOT_NEEDED
        if temporary is not None:
            cleanup_state = _cleanup_stage_entry(
                parent, temporary, staged_identity, directory=False
            )
            cleanup_failed = cleanup_state not in {
                _CLEANUP_REMOVED,
                _CLEANUP_MISSING,
            }
        if locked:
            try:
                unlock_directory(parent)
            except (OSError, NotImplementedError):
                cleanup_failed = True
                if cleanup_state == _CLEANUP_NOT_NEEDED:
                    cleanup_state = _CLEANUP_FAILED
        try:
            os.close(parent)
        except (OSError, ValueError):
            cleanup_failed = True
            if cleanup_state == _CLEANUP_NOT_NEEDED:
                cleanup_state = _CLEANUP_FAILED
        if failure is not None:
            failure.cleanup_state = cleanup_state
        if cleanup_failed:
            failure = _error(
                "filesystem write cleanup could not be confirmed",
                "write_unknown" if replaced or replace_attempted else "cleanup_unknown",
                state=_UNCERTAIN if replaced or replace_attempted else _NOT_REPLACED,
                committed=committed,
                cleanup_state=cleanup_state,
            )

    if failure is not None:
        raise failure from None
    assert staged_identity is not None
    return staged_identity


def create_link(
    root: Path,
    path: Path,
    link_value: str,
    *,
    expected: FileObservation,
    check_inputs: Callable[[], object] | None = None,
) -> tuple[int, int]:
    """Create one symlink where nothing exists and return its identity.

    ``os.symlink`` fails with ``EEXIST`` instead of replacing an entry, so the
    leaf is never overwritten even when it appears after the recheck.
    """

    boundary, relative = _capture_paths(root, path)
    if (
        type(link_value) is not str
        or not link_value.startswith("/")
        or "\x00" in link_value
    ):
        raise _error("filesystem content is invalid", "invalid_content")
    if (
        not isinstance(expected, FileObservation)
        or expected.identity is not None
        or expected.link_target is not None
    ):
        raise _error("filesystem observation is invalid", "invalid_observation")
    try:
        parent, name = open_parent_directory(root, path)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem parent is unsafe", "unsafe_path") from None

    locked = False
    attempted = False
    created = False
    failure: FilesystemError | None = None
    identity: tuple[int, int] | None = None
    try:
        lock_directory(parent)
        locked = True
        _verify_root_parent(boundary, parent, path.parent)
        _verify_expected_ancestry(boundary, root, relative, expected)
        _require_missing_entry(parent, name)

        _run_check_inputs(check_inputs)
        _verify_root_parent(boundary, parent, path.parent)
        _verify_expected_ancestry(boundary, root, relative, expected)

        attempted = True
        try:
            os.symlink(link_value, name, dir_fd=parent)
        except FileExistsError:
            attempted = False
            raise _error("filesystem inputs are stale", "stale_inputs") from None
        created = True
        sync_directory(parent)
        written = _observe_link_entry(parent, name)
        if written.link_target != link_value or written.identity is None:
            raise _error(
                "filesystem link result is uncertain",
                "write_unknown",
                state=_UNCERTAIN,
                committed=True,
            )
        identity = written.identity
        _verify_root_parent(boundary, parent, path.parent)
        _verify_expected_ancestry(boundary, root, relative, expected)
    except FilesystemError as exc:
        failure = (
            _error(str(exc), exc.code, state=_UNCERTAIN, committed=True)
            if created
            else exc
        )
    except Exception:  # noqa: BLE001 - classify unexpected failures at the symlink boundary
        failure = _error(
            "filesystem link outcome is uncertain"
            if attempted
            else "filesystem link was not created",
            "write_unknown" if attempted else "write_failed",
            state=_UNCERTAIN if attempted else _NOT_REPLACED,
            committed=created,
        )
    finally:
        cleanup_failed = False
        if locked:
            try:
                unlock_directory(parent)
            except (OSError, NotImplementedError):
                cleanup_failed = True
        try:
            os.close(parent)
        except (OSError, ValueError):
            cleanup_failed = True
        if cleanup_failed:
            failure = _error(
                "filesystem link cleanup could not be confirmed",
                "write_unknown" if attempted else "cleanup_unknown",
                state=_UNCERTAIN if attempted else _NOT_REPLACED,
                committed=created,
                cleanup_state=_CLEANUP_FAILED,
            )

    if failure is not None:
        raise failure from None
    assert identity is not None
    return identity


def _capture_paths(root: Path, path: Path) -> tuple[DirectoryBoundary, Path]:
    if (
        not root.is_absolute()
        or not path.is_absolute()
        or ".." in root.parts
        or ".." in path.parts
    ):
        raise _error("filesystem path is unsafe", "unsafe_path")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise _error("filesystem path is outside its root", "unsafe_path") from None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise _error("filesystem path is unsafe", "unsafe_path")
    try:
        boundary = capture_directory_boundary(root)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem root is unsafe", "unsafe_path") from None
    return boundary, relative


def _require_missing_entry(parent: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise _error("filesystem parent could not be checked", "unsafe_path") from None
    raise _error("filesystem parent already exists", "stale_inputs")


def _cleanup_stage_entry(
    parent: int,
    name: str,
    identity: tuple[int, int] | None,
    *,
    directory: bool,
) -> str:
    """Remove only a stage entry whose current no-follow inode still matches."""

    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return _CLEANUP_MISSING
    except (OSError, ValueError):
        return _CLEANUP_FAILED

    if identity is None:
        return _CLEANUP_UNBOUND
    if _identity(info) != identity:
        return _CLEANUP_FOREIGN
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            return _CLEANUP_FOREIGN
        remove = os.rmdir
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return _CLEANUP_FOREIGN
        remove = os.unlink
    try:
        remove(name, dir_fd=parent)
    except FileNotFoundError:
        return _CLEANUP_MISSING
    except (OSError, ValueError):
        return _CLEANUP_FAILED
    return _CLEANUP_REMOVED


def _capture_ancestry(
    boundary: DirectoryBoundary,
    root: Path,
    relative: Path,
) -> tuple[tuple[tuple[Path, tuple[int, int]], ...], tuple[Path, ...]]:
    """Capture no-follow identities for existing parents and any missing chain."""

    existing: list[tuple[Path, tuple[int, int]]] = [
        (root, (boundary.device, boundary.inode))
    ]
    missing: list[Path] = []
    descriptor = -1
    try:
        descriptor = os.open(root, directory_flags())
        root_info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or _identity(root_info) != existing[0][1]
        ):
            raise _error("filesystem root changed", "unsafe_path")
        _require_trusted_directory(root_info)

        current = root
        parents = relative.parts[:-1]
        for index, component in enumerate(parents):
            child_path = current / component
            try:
                child = os.open(component, directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                missing.append(child_path)
                for remaining in parents[index + 1 :]:
                    missing.append(missing[-1] / remaining)
                break

            try:
                child_info = os.fstat(child)
                if not stat.S_ISDIR(child_info.st_mode):
                    raise _error("filesystem parent is unsafe", "unsafe_path")
                _require_trusted_directory(child_info)
                child_identity = _identity(child_info)
                verify_directory_identity(child, child_path)
            except BaseException:
                os.close(child)
                raise

            previous = descriptor
            descriptor = child
            os.close(previous)
            current = child_path
            existing.append((current, child_identity))

        boundary.verify()
        verify_directory_identity(descriptor, current)
    except FilesystemError:
        raise
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem parent is unsafe", "unsafe_path") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise _error(
                    "filesystem ancestry could not be verified", "unsafe_path"
                ) from None

    return tuple(existing), tuple(missing)


def _verify_same_ancestry(
    boundary: DirectoryBoundary,
    root: Path,
    relative: Path,
    existing: tuple[tuple[Path, tuple[int, int]], ...],
    missing: tuple[Path, ...],
) -> None:
    current_existing, current_missing = _capture_ancestry(boundary, root, relative)
    if current_existing != existing or current_missing != missing:
        raise _error("filesystem inputs are stale", "stale_inputs")


def _verify_expected_ancestry(
    boundary: DirectoryBoundary,
    root: Path,
    relative: Path,
    expected: FileObservation,
) -> None:
    existing, missing = _capture_ancestry(boundary, root, relative)
    if existing != expected.existing_ancestors or missing != expected.missing_chain:
        raise _error("filesystem inputs are stale", "stale_inputs")


def _with_ancestry(
    observation: FileObservation,
    existing: tuple[tuple[Path, tuple[int, int]], ...],
    missing: tuple[Path, ...],
) -> FileObservation:
    return replace(observation, existing_ancestors=existing, missing_chain=missing)


def _same_file(left: FileObservation, right: FileObservation) -> bool:
    return (
        left.data == right.data
        and left.identity == right.identity
        and left.digest == right.digest
        and left.mode == right.mode
    )


def _observe_entry(parent: int, name: str) -> FileObservation:
    try:
        data, info = read_stable_file(parent, name)
    except FileNotFoundError:
        return _ABSENT
    except UnsafeEntryError:
        raise _error(
            "filesystem entry is not a private regular file", "unsafe_file"
        ) from None
    except (FileChangedError, PermissionError):
        raise _error(
            "filesystem entry changed during observation", "unsafe_file"
        ) from None
    return FileObservation(
        data=data,
        identity=_identity(info),
        digest=hashlib.sha256(data).hexdigest(),
        mode=stat.S_IMODE(info.st_mode),
    )


def _observe_link_entry(parent: int, name: str) -> FileObservation:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISLNK(info.st_mode):
            return FileObservation(None, _identity(info), None, None, foreign=True)
        link_target = os.readlink(name, dir_fd=parent)
        latest = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return _ABSENT
    if _identity(latest) != _identity(info) or not stat.S_ISLNK(latest.st_mode):
        raise _error("filesystem entry changed during observation", "unsafe_file")
    return FileObservation(None, _identity(info), None, None, link_target=link_target)


def _verify_staged_entry(
    parent: int,
    parent_path: Path,
    temporary: str,
    identity: tuple[int, int],
    mode: int,
    data: bytes,
) -> None:
    try:
        verify_directory_identity(parent, parent_path)
        staged = _observe_entry(parent, temporary)
        verify_directory_identity(parent, parent_path)
    except (OSError, NotImplementedError, ValueError):
        raise _error("staged filesystem entry changed", "unsafe_stage") from None
    if staged.identity != identity or staged.data != data or staged.mode != mode:
        raise _error("staged filesystem entry changed", "unsafe_stage")


def _verify_directory_entry(
    parent: int,
    name: str,
    identity: tuple[int, int] | None,
    mode: int,
    *,
    committed: bool = True,
) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        raise _error(
            "created filesystem parent changed",
            "parent_create_unknown",
            state=_UNCERTAIN,
            committed=committed,
        ) from None
    if (
        identity is None
        or not stat.S_ISDIR(info.st_mode)
        or _identity(info) != identity
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise _error(
            "created filesystem parent changed",
            "parent_create_unknown",
            state=_UNCERTAIN,
            committed=committed,
        )


def _verify_root(boundary: DirectoryBoundary) -> None:
    try:
        boundary.verify()
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem root changed", "unsafe_path") from None


def _verify_root_parent(
    boundary: DirectoryBoundary,
    parent: int,
    parent_path: Path,
) -> None:
    try:
        boundary.verify()
        verify_directory_identity(parent, parent_path)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem root or parent changed", "unsafe_path") from None


def _run_check_inputs(check_inputs: Callable[[], object] | None) -> None:
    if check_inputs is None:
        return
    try:
        check_inputs()
    except Exception:  # noqa: BLE001 - callbacks must fail closed without leaking details
        raise _error("filesystem inputs are stale", "stale_inputs") from None


def _write_mode(expected: FileObservation, mode: int | None) -> int:
    if mode is not None:
        return mode
    if expected.mode is not None:
        return expected.mode
    return 0o644


def _create_temporary_file(parent: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag()
    for _ in range(100):
        name = f".luwu-tmp-{uuid.uuid4().hex}"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent), name
        except FileExistsError:
            continue
    raise _error("temporary filesystem entry could not be created", "write_failed")


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]


def read_flags() -> int:
    return os.O_RDONLY | _required_flag("O_NONBLOCK") | _nofollow_flag()


def directory_flags() -> int:
    return os.O_RDONLY | _required_flag("O_DIRECTORY") | _nofollow_flag()


def _required_flag(name: str) -> int:
    try:
        return getattr(os, name)
    except AttributeError:
        raise _error(
            "filesystem platform is unsupported", "platform_unsupported"
        ) from None


def _nofollow_flag() -> int:
    return _required_flag("O_NOFOLLOW")


def _validate_mode(mode: int) -> None:
    if type(mode) is not int or mode < 0 or mode > 0o777:
        raise _error("filesystem mode is invalid", "invalid_mode")


def _require_trusted_directory(info: os.stat_result) -> None:
    if info.st_uid not in {0, os.getuid()} or stat.S_IMODE(info.st_mode) & 0o022:
        raise _error("filesystem directory is not trusted", "unsafe_path")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_size,
        info.st_mode,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _with_create_outcome(
    error: FilesystemError,
    created: bool,
    identity: tuple[int, int] | None,
) -> FilesystemError:
    if not created:
        return error
    return _error(
        str(error),
        error.code,
        state=_UNCERTAIN,
        committed=True,
        created_identity=identity,
    )


def _with_write_outcome(
    error: FilesystemError,
    replaced: bool,
    replace_attempted: bool,
) -> FilesystemError:
    if error.state == _UNCERTAIN:
        return error
    if replaced:
        return _error(str(error), error.code, state=_UNCERTAIN, committed=True)
    if replace_attempted:
        return _error(str(error), error.code, state=_UNCERTAIN, committed=False)
    return error


def _replacement_is_staged(
    parent: int,
    name: str,
    identity: tuple[int, int] | None,
) -> bool:
    if identity is None:
        return False
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(current.st_mode) and _identity(current) == identity


def _error(
    message: str,
    code: str,
    *,
    state: str = _NOT_REPLACED,
    committed: bool = False,
    created_identity: tuple[int, int] | None = None,
    cleanup_state: str = _CLEANUP_NOT_NEEDED,
) -> FilesystemError:
    return FilesystemError(
        message,
        code=code,
        state=state,
        committed=committed,
        created_identity=created_identity,
        cleanup_state=cleanup_state,
    )
