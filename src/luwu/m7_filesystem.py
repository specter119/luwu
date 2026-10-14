"""Bounded, descriptor-relative filesystem primitives for the M7 engine.

Directory locks coordinate cooperating Luwu writers only. These primitives do
not defend against a malicious same-UID process, a privileged actor, or a
malicious filesystem. ``create_parent`` returns the no-follow directory
identity observed and rechecked at the requested path; it is not proof that
the inode observed after ``mkdir`` came exclusively from that syscall.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from luwu.errors import LuwuError
from luwu.filesystem import (
    DirectoryBoundary,
    capture_directory_boundary,
    lock_directory,
    open_parent_directory,
    read_descriptor,
    sync_directory,
    unlock_directory,
    verify_directory_identity,
)


@dataclass(frozen=True, slots=True)
class FileObservation:
    """A stable snapshot of one regular file or an absent leaf."""

    data: bytes | None = field(repr=False)
    identity: tuple[int, int] | None
    digest: str | None = field(repr=False)
    mode: int | None
    existing_ancestors: tuple[tuple[Path, tuple[int, int]], ...] = ()
    missing_chain: tuple[Path, ...] = ()


class M7FilesystemError(LuwuError):
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
        raise _error("filesystem path is unsafe", "m7_unsafe_path") from None

    try:
        try:
            observation = _observe_entry(parent, name)
        except FileNotFoundError:
            observation = _ABSENT
        _verify_root_parent(boundary, parent, path.parent)
        _verify_same_ancestry(boundary, root, relative, existing, missing)
        return _with_ancestry(observation, existing, missing)
    except M7FilesystemError:
        raise
    except (OSError, NotImplementedError, ValueError):
        raise _error(
            "filesystem entry could not be observed", "m7_observe_failed"
        ) from None
    except Exception:  # noqa: BLE001 - keep unexpected filesystem errors metadata-only
        raise _error(
            "filesystem entry could not be observed", "m7_observe_failed"
        ) from None
    finally:
        try:
            os.close(parent)
        except (OSError, ValueError):
            raise _error(
                "filesystem observation cleanup failed", "m7_observe_failed"
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
        raise _error("filesystem parent is unsafe", "m7_unsafe_path") from None

    locked = False
    child_descriptor: int | None = None
    created = False
    create_attempted = False
    failure: M7FilesystemError | None = None
    identity: tuple[int, int] | None = None
    cleanup_state = _CLEANUP_NOT_NEEDED
    try:
        lock_directory(parent)
        locked = True
        _verify_root_parent(boundary, parent, path.parent)
        existing, missing = _capture_ancestry(boundary, root, relative)
        if missing:
            raise _error("filesystem parent is unsafe", "m7_unsafe_path")
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
            raise _error(
                "filesystem parent already exists", "m7_stale_inputs"
            ) from None
        created = True

        # Keep mkdir and the no-follow open adjacent: the descriptor is the
        # identity anchor for fchmod and subsequent checks.
        child_descriptor = os.open(name, _directory_flags(), dir_fd=parent)
        opened = os.fstat(child_descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise _error(
                "created filesystem parent is unsafe",
                "m7_parent_create_unknown",
                state=_UNCERTAIN,
                committed=True,
            )
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
                "m7_parent_create_unknown",
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
    except M7FilesystemError as exc:
        failure = _with_create_outcome(
            exc,
            created,
            None if exc.code == "m7_parent_create_unknown" else identity,
        )
    except (OSError, NotImplementedError, ValueError):
        failure = _error(
            "filesystem parent creation could not be confirmed",
            "m7_parent_create_unknown"
            if created or create_attempted
            else "m7_parent_create_failed",
            state=_UNCERTAIN if created or create_attempted else _NOT_REPLACED,
            committed=created,
            created_identity=identity if created else None,
        )
    except Exception:  # noqa: BLE001 - preserve creation uncertainty without raw details
        failure = _error(
            "filesystem parent creation could not be confirmed",
            "m7_parent_create_unknown"
            if created or create_attempted
            else "m7_parent_create_failed",
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
                "m7_parent_create_unknown"
                if cleanup_uncertain
                else "m7_cleanup_unknown",
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
        raise _error("filesystem content is invalid", "m7_invalid_content")
    if mode is not None:
        _validate_mode(mode)
    if not isinstance(expected, FileObservation):
        raise _error("filesystem observation is invalid", "m7_invalid_observation")
    _verify_expected_ancestry(boundary, root, relative, expected)

    try:
        parent, name = open_parent_directory(root, path)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem parent is unsafe", "m7_unsafe_path") from None

    temporary: str | None = None
    staged_identity: tuple[int, int] | None = None
    locked = False
    replace_attempted = False
    replaced = False
    failure: M7FilesystemError | None = None
    try:
        lock_directory(parent)
        locked = True
        _verify_root_parent(boundary, parent, path.parent)
        _verify_expected_ancestry(boundary, root, relative, expected)
        current = _observe_entry(parent, name)
        if not _same_file(current, expected):
            raise _error("filesystem inputs are stale", "m7_stale_inputs")

        final_mode = _write_mode(expected, mode)
        _validate_mode(final_mode)
        initial_mode = _initial_stage_mode(expected, mode)
        descriptor, temporary = _create_temporary_file(parent, initial_mode)
        try:
            staged_info = os.fstat(descriptor)
            if not stat.S_ISREG(staged_info.st_mode) or staged_info.st_nlink != 1:
                raise _error("staged filesystem entry is unsafe", "m7_unsafe_stage")
            staged_identity = _identity(staged_info)
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, data)
            os.fchmod(descriptor, final_mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        _verify_root_parent(boundary, parent, path.parent)
        _verify_staged_entry(
            parent, path.parent, temporary, staged_identity, final_mode, data
        )
        _run_check_inputs(check_inputs)

        _verify_root_parent(boundary, parent, path.parent)
        _verify_expected_ancestry(boundary, root, relative, expected)
        current = _observe_entry(parent, name)
        if not _same_file(current, expected):
            raise _error("filesystem inputs are stale", "m7_stale_inputs")
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
                "m7_write_unknown",
                state=_UNCERTAIN,
                committed=True,
            )
        _verify_expected_ancestry(boundary, root, relative, expected)
    except M7FilesystemError as exc:
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
            "m7_write_unknown" if committed or replace_attempted else "m7_write_failed",
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
            "m7_write_unknown" if committed or replace_attempted else "m7_write_failed",
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
                "m7_write_unknown"
                if replaced or replace_attempted
                else "m7_cleanup_unknown",
                state=_UNCERTAIN if replaced or replace_attempted else _NOT_REPLACED,
                committed=committed,
                cleanup_state=cleanup_state,
            )

    if failure is not None:
        raise failure from None
    assert staged_identity is not None
    return staged_identity


def _capture_paths(root: Path, path: Path) -> tuple[DirectoryBoundary, Path]:
    if (
        not root.is_absolute()
        or not path.is_absolute()
        or ".." in root.parts
        or ".." in path.parts
    ):
        raise _error("filesystem path is unsafe", "m7_unsafe_path")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise _error("filesystem path is outside its root", "m7_unsafe_path") from None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise _error("filesystem path is unsafe", "m7_unsafe_path")
    try:
        boundary = capture_directory_boundary(root)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem root is unsafe", "m7_unsafe_path") from None
    return boundary, relative


def _require_missing_entry(parent: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise _error(
            "filesystem parent could not be checked", "m7_unsafe_path"
        ) from None
    raise _error("filesystem parent already exists", "m7_stale_inputs")


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
        descriptor = os.open(root, _directory_flags())
        root_info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or _identity(root_info) != existing[0][1]
        ):
            raise _error("filesystem root changed", "m7_unsafe_path")
        _require_trusted_directory(root_info)

        current = root
        parents = relative.parts[:-1]
        for index, component in enumerate(parents):
            child_path = current / component
            try:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                missing.append(child_path)
                for remaining in parents[index + 1 :]:
                    missing.append(missing[-1] / remaining)
                break

            try:
                child_info = os.fstat(child)
                if not stat.S_ISDIR(child_info.st_mode):
                    raise _error("filesystem parent is unsafe", "m7_unsafe_path")
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
    except M7FilesystemError:
        raise
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem parent is unsafe", "m7_unsafe_path") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise _error(
                    "filesystem ancestry could not be verified", "m7_unsafe_path"
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
        raise _error("filesystem inputs are stale", "m7_stale_inputs")


def _verify_expected_ancestry(
    boundary: DirectoryBoundary,
    root: Path,
    relative: Path,
    expected: FileObservation,
) -> None:
    existing, missing = _capture_ancestry(boundary, root, relative)
    if existing != expected.existing_ancestors or missing != expected.missing_chain:
        raise _error("filesystem inputs are stale", "m7_stale_inputs")


def _with_ancestry(
    observation: FileObservation,
    existing: tuple[tuple[Path, tuple[int, int]], ...],
    missing: tuple[Path, ...],
) -> FileObservation:
    return FileObservation(
        data=observation.data,
        identity=observation.identity,
        digest=observation.digest,
        mode=observation.mode,
        existing_ancestors=existing,
        missing_chain=missing,
    )


def _same_file(left: FileObservation, right: FileObservation) -> bool:
    return (
        left.data == right.data
        and left.identity == right.identity
        and left.digest == right.digest
        and left.mode == right.mode
    )


def _observe_entry(parent: int, name: str) -> FileObservation:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return _ABSENT
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _error("filesystem entry is not a private regular file", "m7_unsafe_file")

    try:
        descriptor = os.open(name, _read_flags(), dir_fd=parent)
    except (OSError, NotImplementedError):
        raise _error(
            "filesystem entry changed during observation", "m7_unsafe_file"
        ) from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            raise _error(
                "filesystem entry changed during observation", "m7_unsafe_file"
            )
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
            raise _error(
                "filesystem entry changed during observation", "m7_unsafe_file"
            )
        return FileObservation(
            data=data,
            identity=_identity(after),
            digest=hashlib.sha256(data).hexdigest(),
            mode=stat.S_IMODE(after.st_mode),
        )
    finally:
        os.close(descriptor)


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
        raise _error("staged filesystem entry changed", "m7_unsafe_stage") from None
    if staged.identity != identity or staged.data != data or staged.mode != mode:
        raise _error("staged filesystem entry changed", "m7_unsafe_stage")


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
            "m7_parent_create_unknown",
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
            "m7_parent_create_unknown",
            state=_UNCERTAIN,
            committed=committed,
        )


def _verify_root(boundary: DirectoryBoundary) -> None:
    try:
        boundary.verify()
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem root changed", "m7_unsafe_path") from None


def _verify_root_parent(
    boundary: DirectoryBoundary,
    parent: int,
    parent_path: Path,
) -> None:
    try:
        boundary.verify()
        verify_directory_identity(parent, parent_path)
    except (OSError, NotImplementedError, ValueError):
        raise _error("filesystem root or parent changed", "m7_unsafe_path") from None


def _run_check_inputs(check_inputs: Callable[[], object] | None) -> None:
    if check_inputs is None:
        return
    try:
        check_inputs()
    except Exception:  # noqa: BLE001 - callbacks must fail closed without leaking details
        raise _error("filesystem inputs are stale", "m7_stale_inputs") from None


def _write_mode(expected: FileObservation, mode: int | None) -> int:
    if mode is not None:
        return mode
    if expected.mode is not None:
        return expected.mode
    return 0o644


def _initial_stage_mode(expected: FileObservation, mode: int | None) -> int:
    return 0o600


def _create_temporary_file(parent: int, initial_mode: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag()
    for _ in range(100):
        name = f".luwu-m7-{uuid.uuid4().hex}"
        try:
            return os.open(name, flags, initial_mode, dir_fd=parent), name
        except FileExistsError:
            continue
    raise _error("temporary filesystem entry could not be created", "m7_write_failed")


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]


def _read_flags() -> int:
    return os.O_RDONLY | _required_flag("O_NONBLOCK") | _nofollow_flag()


def _directory_flags() -> int:
    return os.O_RDONLY | _required_flag("O_DIRECTORY") | _nofollow_flag()


def _required_flag(name: str) -> int:
    try:
        return getattr(os, name)
    except AttributeError:
        raise _error(
            "filesystem platform is unsupported", "m7_platform_unsupported"
        ) from None


def _nofollow_flag() -> int:
    return _required_flag("O_NOFOLLOW")


def _validate_mode(mode: int) -> None:
    if type(mode) is not int or mode < 0 or mode > 0o777:
        raise _error("filesystem mode is invalid", "m7_invalid_mode")


def _require_trusted_directory(info: os.stat_result) -> None:
    if info.st_uid not in {0, os.getuid()} or stat.S_IMODE(info.st_mode) & 0o022:
        raise _error("filesystem directory is not trusted", "m7_unsafe_path")


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
    error: M7FilesystemError,
    created: bool,
    identity: tuple[int, int] | None,
) -> M7FilesystemError:
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
    error: M7FilesystemError,
    replaced: bool,
    replace_attempted: bool,
) -> M7FilesystemError:
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
) -> M7FilesystemError:
    return M7FilesystemError(
        message,
        code=code,
        state=state,
        committed=committed,
        created_identity=created_identity,
        cleanup_state=cleanup_state,
    )
