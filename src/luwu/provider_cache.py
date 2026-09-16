"""Explicit, metadata-only diagnostics for the M4 provider cache.

The cache is deliberately not part of reconciliation.  It records only the
identity of the provider executable, fixed provider capability metadata, a
small status vocabulary, and timestamps.  In particular, this module has no
resource, item, field, value, payload, digest, or secret-shaped input.
"""

from __future__ import annotations

import errno
import json
import math
import os
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from . import filesystem
from .errors import LuwuError

CACHE_SCHEMA_VERSION = 1
CACHE_PROVIDER_TYPE = "rbw"
CACHE_CAPABILITIES = ("subprocess",)

# These are provider-result labels, not provider error details.  Keeping the
# vocabulary closed prevents a caller from turning the status field into a
# covert value or reference channel.
CACHE_ENTRY_STATUSES = frozenset(
    {
        "ok",
        "error",
        "unavailable",
        "timeout",
        "malformed",
        "capability_required",
        "identity_mismatch",
        "permission_denied",
    }
)
CACHE_INSPECTION_STATUSES = frozenset(
    {
        "fresh",
        "expired",
        "missing",
        "corrupt",
        "identity_mismatch",
        "permission_denied",
        "unsafe",
        "unreadable",
        "platform_unsupported",
    }
)

_CACHE_KEYS = frozenset(
    {
        "schema_version",
        "provider_type",
        "capabilities",
        "executable_identity",
        "status",
        "observed_at",
        "expires_at",
    }
)
_EXECUTABLE_IDENTITY_KEYS = frozenset({"device", "inode", "mode", "size", "mtime_ns"})
_OWNER_ONLY_MODE = 0o600
_MAX_CACHE_BYTES = 64 * 1024
_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


class _DuplicateCacheKey(ValueError):
    """A persisted cache contains a duplicate JSON object key."""


class _CacheTooLarge(ValueError):
    """A persisted cache exceeds the bounded metadata document size."""


class ProviderCacheError(LuwuError):
    """A cache input or explicit cache refresh cannot be safely completed."""

    default_code = "provider_cache_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        published: bool = False,
        durability_confirmed: bool = False,
        cleanup_confirmed: bool = True,
        publication_uncertain: bool = False,
    ) -> None:
        super().__init__(message, code=code)
        self.published = published
        self.durability_confirmed = durability_confirmed
        self.cleanup_confirmed = cleanup_confirmed
        self.publication_uncertain = publication_uncertain

    def metadata(self) -> dict[str, object]:
        """Return fixed, safe metadata for a future CLI or JSON boundary."""

        return {
            "code": self.code,
            "published": self.published,
            "durability_confirmed": self.durability_confirmed,
            "cleanup_confirmed": self.cleanup_confirmed,
            "publication_uncertain": self.publication_uncertain,
        }


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    """Non-content identity and mode metadata for the provider executable."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        _validate_executable_identity_values(
            self.device,
            self.inode,
            self.mode,
            self.size,
            self.mtime_ns,
        )

    @classmethod
    def from_stat(cls, info: os.stat_result) -> ExecutableIdentity:
        """Build an identity from an already-checked executable stat result."""

        try:
            return cls(
                device=info.st_dev,
                inode=info.st_ino,
                mode=stat.S_IMODE(info.st_mode),
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
            )
        except (AttributeError, TypeError, ValueError):
            raise ProviderCacheError(
                "provider executable identity is invalid", code="cache_schema"
            ) from None

    @classmethod
    def from_dict(cls, document: object) -> ExecutableIdentity:
        """Validate the closed persisted identity object."""

        _closed_mapping(
            document,
            _EXECUTABLE_IDENTITY_KEYS,
            "provider executable identity is invalid",
        )
        assert isinstance(document, dict)
        return cls(
            device=document["device"],
            inode=document["inode"],
            mode=document["mode"],
            size=document["size"],
            mtime_ns=document["mtime_ns"],
        )

    def to_dict(self) -> dict[str, int]:
        """Return a fresh metadata-only mapping."""

        return {
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True, slots=True)
class ProviderCacheEntry:
    """One closed, metadata-only cache document."""

    provider_type: str
    capabilities: tuple[str, ...]
    executable_identity: ExecutableIdentity
    status: str
    observed_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if self.provider_type != CACHE_PROVIDER_TYPE:
            raise ProviderCacheError(
                "provider cache provider type is unsupported", code="cache_schema"
            )
        if self.capabilities != CACHE_CAPABILITIES:
            raise ProviderCacheError(
                "provider cache capabilities are unsupported", code="cache_schema"
            )
        if not isinstance(self.executable_identity, ExecutableIdentity):
            raise ProviderCacheError(
                "provider cache executable identity is invalid", code="cache_schema"
            )
        _validate_status(self.status)
        _validate_timestamp(self.observed_at, "provider cache timestamp is invalid")
        _validate_timestamp(self.expires_at, "provider cache expiry is invalid")
        if self.expires_at < self.observed_at:
            raise ProviderCacheError(
                "provider cache expiry is invalid", code="cache_schema"
            )

    @classmethod
    def from_dict(cls, document: object) -> ProviderCacheEntry:
        """Validate one persisted cache document against the closed schema."""

        _closed_mapping(document, _CACHE_KEYS, "provider cache schema is not closed")
        assert isinstance(document, dict)
        version = document["schema_version"]
        if isinstance(version, bool) or version != CACHE_SCHEMA_VERSION:
            raise ProviderCacheError(
                "provider cache schema version is unsupported", code="cache_schema"
            )
        capabilities = document["capabilities"]
        if not isinstance(capabilities, list) or any(
            not isinstance(capability, str) for capability in capabilities
        ):
            raise ProviderCacheError(
                "provider cache capabilities are invalid", code="cache_schema"
            )
        return cls(
            provider_type=document["provider_type"],
            capabilities=tuple(capabilities),
            executable_identity=ExecutableIdentity.from_dict(
                document["executable_identity"]
            ),
            status=document["status"],
            observed_at=document["observed_at"],
            expires_at=document["expires_at"],
        )

    def to_dict(self) -> dict[str, object]:
        """Return the exact persisted schema without any caller metadata."""

        return {
            "schema_version": CACHE_SCHEMA_VERSION,
            "provider_type": self.provider_type,
            "capabilities": list(self.capabilities),
            "executable_identity": self.executable_identity.to_dict(),
            "status": self.status,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class CacheInspection:
    """The read-only diagnostic result for :func:`inspect_cache`."""

    status: str
    entry: ProviderCacheEntry | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, str)
            or self.status not in CACHE_INSPECTION_STATUSES
        ):
            raise ProviderCacheError(
                "provider cache inspection status is unsupported",
                code="cache_schema",
            )
        if self.entry is not None and not isinstance(self.entry, ProviderCacheEntry):
            raise ProviderCacheError(
                "provider cache inspection entry is invalid", code="cache_schema"
            )
        requires_entry = {"fresh", "expired", "identity_mismatch"}
        if (self.status in requires_entry) != (self.entry is not None):
            raise ProviderCacheError(
                "provider cache inspection result is invalid", code="cache_schema"
            )

    def to_dict(self) -> dict[str, object]:
        """Return safe metadata for a future presentation boundary."""

        return {
            "status": self.status,
            "entry": None if self.entry is None else self.entry.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CacheWriteResult:
    """Confirmed outcome of an explicit cache refresh."""

    entry: ProviderCacheEntry
    published: bool = True
    durability_confirmed: bool = True
    cleanup_confirmed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.entry, ProviderCacheEntry):
            raise ProviderCacheError(
                "provider cache write result is invalid", code="cache_schema"
            )

    def to_dict(self) -> dict[str, object]:
        """Return fixed safe write metadata and the safe cache entry."""

        return {
            "status": "written",
            "published": self.published,
            "durability_confirmed": self.durability_confirmed,
            "cleanup_confirmed": self.cleanup_confirmed,
            "entry": self.entry.to_dict(),
        }


def inspect_cache(
    path: Path,
    *,
    executable_identity: ExecutableIdentity | None = None,
    now: float | None = None,
) -> CacheInspection:
    """Read a cache document and return diagnostics without changing it.

    ``executable_identity`` is optional so a caller can inspect the document
    without an executable observation.  When supplied, it is compared only
    with the persisted non-content identity.  Expiry, corruption, permission,
    and identity mismatch never trigger repair or deletion.
    """

    cache_path = _validate_cache_path(path)
    expected_identity = _validate_optional_identity(executable_identity)
    current_time = _validate_optional_now(now)
    try:
        _require_platform()
    except ProviderCacheError:
        return CacheInspection("platform_unsupported")

    try:
        parent, leaf = _open_cache_parent(cache_path)
    except FileNotFoundError:
        return CacheInspection("missing")
    except PermissionError:
        return CacheInspection("permission_denied")
    except NotImplementedError:
        return CacheInspection("platform_unsupported")
    except OSError as exc:
        return CacheInspection(_read_os_error_status(exc))

    read_status = "unreadable"
    data: bytes | None = None
    try:
        read_status, data = _read_cache_file(parent, leaf)
    finally:
        try:
            os.close(parent)
        except OSError:
            if read_status == "ok":
                read_status, data = "unreadable", None
    if read_status != "ok" or data is None:
        return CacheInspection(read_status)

    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_cache_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
        entry = ProviderCacheEntry.from_dict(document)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return CacheInspection("corrupt")
    except ProviderCacheError:
        return CacheInspection("corrupt")

    if expected_identity is not None and entry.executable_identity != expected_identity:
        return CacheInspection("identity_mismatch", entry)
    if current_time >= entry.expires_at:
        return CacheInspection("expired", entry)
    return CacheInspection("fresh", entry)


def refresh_cache(
    path: Path,
    *,
    provider_type: str,
    capabilities: Sequence[str],
    executable_identity: ExecutableIdentity,
    status: str,
    ttl_seconds: float,
    now: float | None = None,
) -> CacheWriteResult:
    """Explicitly atomically replace one owner-only metadata cache document.

    The API accepts only the fixed provider metadata listed in the M4d
    contract.  It deliberately has no key, resource, path, item, field,
    value, payload, digest, or reference argument.
    """

    cache_path = _validate_cache_path(path)
    entry = _new_entry(
        provider_type=provider_type,
        capabilities=capabilities,
        executable_identity=executable_identity,
        status=status,
        ttl_seconds=ttl_seconds,
        now=now,
    )
    data = _encode_entry(entry)
    _require_platform()

    try:
        parent, leaf = _open_cache_parent(cache_path)
    except ProviderCacheError:
        raise
    except FileNotFoundError:
        raise ProviderCacheError(
            "provider cache parent is unavailable", code="cache_parent_missing"
        ) from None
    except PermissionError:
        raise ProviderCacheError(
            "provider cache parent is not writable", code="cache_permission_denied"
        ) from None
    except NotImplementedError:
        raise ProviderCacheError(
            "provider cache platform is unsupported", code="platform_unsupported"
        ) from None
    except OSError:
        raise ProviderCacheError(
            "provider cache parent cannot be opened safely", code="cache_write_failed"
        ) from None

    temporary: str | None = None
    locked = False
    parent_closed = False
    published = False
    durability_confirmed = False
    publication_uncertain = False
    replace_attempted = False
    pending_error: ProviderCacheError | None = None
    cleanup_error: ProviderCacheError | None = None
    try:
        parent_identity = _directory_identity(parent)
        _verify_parent(cache_path, parent, parent_identity)
        try:
            filesystem.lock_directory(parent)
        except BlockingIOError:
            raise ProviderCacheError(
                "provider cache directory is busy", code="cache_busy"
            ) from None
        except NotImplementedError:
            raise ProviderCacheError(
                "provider cache platform is unsupported", code="platform_unsupported"
            ) from None
        locked = True

        _verify_parent(cache_path, parent, parent_identity)
        old_identity = _existing_target_identity(parent, leaf)

        descriptor, temporary = filesystem.create_temporary_file(
            parent, prefix=f".{leaf}.luwu-cache-"
        )
        _write_temporary(descriptor, data)
        staged_identity = _entry_identity_at(parent, temporary)
        if staged_identity is None:
            raise ProviderCacheError(
                "provider cache temporary entry disappeared", code="cache_write_failed"
            )

        _verify_parent(cache_path, parent, parent_identity)
        if _entry_identity_at(parent, leaf) != old_identity:
            raise ProviderCacheError(
                "provider cache changed during refresh", code="cache_conflict"
            )

        replace_attempted = True
        try:
            os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError as replace_error:
            state, temporary_present = _classify_replace_failure(
                cache_path,
                parent,
                leaf,
                temporary,
                old_identity,
                staged_identity,
            )
            if temporary_present is False:
                temporary = None
            if state == "replaced":
                published = True
                publication_uncertain = True
                raise ProviderCacheError(
                    "provider cache replacement outcome is not fully confirmed",
                    code="cache_durability_unknown",
                    published=True,
                    publication_uncertain=True,
                ) from None
            if state == "indeterminate":
                publication_uncertain = True
                raise ProviderCacheError(
                    "provider cache replacement outcome is unknown",
                    code="cache_durability_unknown",
                    publication_uncertain=True,
                ) from None
            code = (
                "cache_permission_denied"
                if isinstance(replace_error, PermissionError)
                or replace_error.errno in _PERMISSION_ERRNOS
                else "cache_write_failed"
            )
            raise ProviderCacheError(
                "provider cache replacement did not occur", code=code
            ) from None

        published = True
        temporary = None
        try:
            _verify_parent(cache_path, parent, parent_identity)
            _verify_published_target(parent, leaf, staged_identity)
        except ProviderCacheError:
            publication_uncertain = True
            raise ProviderCacheError(
                "provider cache replacement outcome is not fully confirmed",
                code="cache_durability_unknown",
                published=True,
                publication_uncertain=True,
            ) from None
        filesystem.sync_directory(parent)
        durability_confirmed = True
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        pending_error = _as_write_error(
            exc,
            published=published,
            durability_confirmed=durability_confirmed,
            publication_uncertain=publication_uncertain,
            replace_attempted=replace_attempted,
        )
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                temporary = None
            except OSError:
                cleanup_error = ProviderCacheError(
                    "provider cache temporary cleanup could not be confirmed",
                    code="cache_cleanup_failed",
                    published=published,
                    durability_confirmed=durability_confirmed,
                    cleanup_confirmed=False,
                    publication_uncertain=publication_uncertain,
                )
        if locked:
            try:
                filesystem.unlock_directory(parent)
            except (OSError, NotImplementedError):
                if cleanup_error is None:
                    cleanup_error = ProviderCacheError(
                        "provider cache directory cleanup could not be confirmed",
                        code="cache_cleanup_failed",
                        published=published,
                        durability_confirmed=durability_confirmed,
                        cleanup_confirmed=False,
                        publication_uncertain=publication_uncertain,
                    )
        try:
            os.close(parent)
            parent_closed = True
        except OSError:
            if cleanup_error is None:
                cleanup_error = ProviderCacheError(
                    "provider cache parent cleanup could not be confirmed",
                    code="cache_cleanup_failed",
                    published=published,
                    durability_confirmed=durability_confirmed,
                    cleanup_confirmed=False,
                    publication_uncertain=publication_uncertain,
                )

    if cleanup_error is not None:
        raise cleanup_error
    if pending_error is not None:
        raise pending_error
    if not parent_closed:
        raise ProviderCacheError(
            "provider cache parent cleanup could not be confirmed",
            code="cache_cleanup_failed",
            published=published,
            durability_confirmed=durability_confirmed,
            cleanup_confirmed=False,
            publication_uncertain=publication_uncertain,
        )
    return CacheWriteResult(
        entry=entry,
        published=published,
        durability_confirmed=durability_confirmed,
        cleanup_confirmed=True,
    )


def _new_entry(
    *,
    provider_type: str,
    capabilities: Sequence[str],
    executable_identity: ExecutableIdentity,
    status: str,
    ttl_seconds: float,
    now: float | None,
) -> ProviderCacheEntry:
    if provider_type != CACHE_PROVIDER_TYPE:
        raise ProviderCacheError(
            "provider cache provider type is unsupported", code="cache_schema"
        )
    if isinstance(capabilities, (str, bytes)) or not isinstance(
        capabilities, (list, tuple)
    ):
        raise ProviderCacheError(
            "provider cache capabilities are invalid", code="cache_schema"
        )
    if tuple(capabilities) != CACHE_CAPABILITIES:
        raise ProviderCacheError(
            "provider cache capabilities are unsupported", code="cache_schema"
        )
    if not isinstance(executable_identity, ExecutableIdentity):
        raise ProviderCacheError(
            "provider cache executable identity is invalid", code="cache_schema"
        )
    _validate_status(status)
    current_time = _validate_optional_now(now)
    ttl = _validate_ttl(ttl_seconds)
    expiry = current_time + ttl
    if not math.isfinite(expiry):
        raise ProviderCacheError(
            "provider cache expiry is invalid", code="cache_schema"
        )
    return ProviderCacheEntry(
        provider_type=provider_type,
        capabilities=CACHE_CAPABILITIES,
        executable_identity=executable_identity,
        status=status,
        observed_at=current_time,
        expires_at=expiry,
    )


def _encode_entry(entry: ProviderCacheEntry) -> bytes:
    try:
        return (
            json.dumps(
                entry.to_dict(),
                ensure_ascii=True,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, OverflowError):
        raise ProviderCacheError(
            "provider cache schema cannot be encoded", code="cache_schema"
        ) from None


def _validate_cache_path(path: Path) -> Path:
    try:
        raw = os.fspath(path)
    except TypeError:
        raise ProviderCacheError(
            "provider cache path is invalid", code="cache_schema"
        ) from None
    if isinstance(raw, bytes):
        raise ProviderCacheError("provider cache path is invalid", code="cache_schema")
    if not raw or "\x00" in raw or raw.endswith(os.sep):
        raise ProviderCacheError("provider cache path is invalid", code="cache_schema")
    raw_parts = raw.split(os.sep)
    if os.path.isabs(raw):
        raw_parts = raw_parts[1:]
    if not raw_parts or raw_parts[-1] in {"", ".", ".."}:
        raise ProviderCacheError("provider cache path is invalid", code="cache_schema")
    if any(part in {"", ".", ".."} for part in raw_parts[:-1]):
        raise ProviderCacheError("provider cache path is invalid", code="cache_schema")
    return Path(raw)


def _open_cache_parent(path: Path) -> tuple[int, str]:
    root = Path(path.anchor) if path.is_absolute() else Path(".")
    return filesystem.open_parent_directory(root, path)


def _require_platform() -> None:
    if os.name != "posix":
        raise ProviderCacheError(
            "provider cache platform is unsupported", code="platform_unsupported"
        )
    required = (
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_NONBLOCK",
        "fchmod",
        "fsync",
        "getuid",
        "replace",
        "rename",
        "stat",
        "supports_dir_fd",
    )
    if any(not hasattr(os, name) for name in required):
        raise ProviderCacheError(
            "provider cache platform is unsupported", code="platform_unsupported"
        )
    if os.stat not in os.supports_dir_fd or os.rename not in os.supports_dir_fd:
        raise ProviderCacheError(
            "provider cache platform is unsupported", code="platform_unsupported"
        )
    try:
        import fcntl  # noqa: F401 - the existing filesystem lock needs it
    except ImportError:
        raise ProviderCacheError(
            "provider cache platform is unsupported", code="platform_unsupported"
        ) from None


def _validate_optional_identity(
    value: ExecutableIdentity | None,
) -> ExecutableIdentity | None:
    if value is not None and not isinstance(value, ExecutableIdentity):
        raise ProviderCacheError(
            "provider cache executable identity is invalid", code="cache_schema"
        )
    return value


def _validate_optional_now(value: float | None) -> float:
    if value is None:
        value = time.time()
    _validate_timestamp(value, "provider cache timestamp is invalid")
    return float(value)


def _validate_ttl(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderCacheError("provider cache TTL is invalid", code="cache_schema")
    if not math.isfinite(value) or value < 0:
        raise ProviderCacheError("provider cache TTL is invalid", code="cache_schema")
    return float(value)


def _validate_timestamp(value: object, message: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderCacheError(message, code="cache_schema")
    if not math.isfinite(value):
        raise ProviderCacheError(message, code="cache_schema")


def _validate_status(value: object) -> None:
    if not isinstance(value, str) or value not in CACHE_ENTRY_STATUSES:
        raise ProviderCacheError(
            "provider cache status is unsupported", code="cache_schema"
        )


def _validate_executable_identity_values(
    device: object,
    inode: object,
    mode: object,
    size: object,
    mtime_ns: object,
) -> None:
    for value in (device, inode, mode, size, mtime_ns):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProviderCacheError(
                "provider executable identity is invalid", code="cache_schema"
            )
    device_value = cast(int, device)
    inode_value = cast(int, inode)
    size_value = cast(int, size)
    mode_value = cast(int, mode)
    if (
        device_value < 0
        or inode_value < 0
        or size_value < 0
        or mode_value < 0
        or mode_value > 0o7777
    ):
        raise ProviderCacheError(
            "provider executable identity is invalid", code="cache_schema"
        )


def _closed_mapping(value: object, keys: frozenset[str], message: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProviderCacheError(message, code="cache_schema")


def _cache_object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise _DuplicateCacheKey(key)
        values[key] = value
    return values


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _read_cache_file(parent: int, leaf: str) -> tuple[str, bytes | None]:
    descriptor: int | None = None
    status = "ok"
    data: bytes | None = None
    try:
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return "unsafe", None
        if not _is_owner_only(info):
            return "permission_denied", None
        data = _read_limited(descriptor)
    except FileNotFoundError:
        status = "missing"
    except PermissionError:
        status = "permission_denied"
    except OSError as exc:
        status = _read_os_error_status(exc)
    except _CacheTooLarge:
        status = "corrupt"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                status, data = "unreadable", None
    return status, data


def _read_limited(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(16 * 1024, _MAX_CACHE_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_CACHE_BYTES:
            raise _CacheTooLarge


def _read_os_error_status(error: OSError) -> str:
    if error.errno in _PERMISSION_ERRNOS:
        return "permission_denied"
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return "unsafe"
    return "unreadable"


def _write_temporary(descriptor: int, data: bytes) -> None:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fchmod(handle.fileno(), _OWNER_ONLY_MODE)
        if not _is_owner_only(os.fstat(handle.fileno())):
            raise ProviderCacheError(
                "provider cache temporary permissions are unsafe",
                code="cache_permission_denied",
            )
        os.fsync(handle.fileno())


def _is_owner_only(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == _OWNER_ONLY_MODE


def _existing_target_identity(parent: int, leaf: str) -> tuple[object, ...] | None:
    try:
        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except PermissionError:
        raise ProviderCacheError(
            "provider cache target cannot be checked safely",
            code="cache_permission_denied",
        ) from None
    except OSError:
        raise ProviderCacheError(
            "provider cache target cannot be checked safely", code="cache_write_failed"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise ProviderCacheError(
            "provider cache target is unsafe", code="cache_unsafe_path"
        )
    if info.st_uid != os.getuid():
        raise ProviderCacheError(
            "provider cache target is not owner-only", code="cache_permission_denied"
        )
    return _entry_identity(info)


def _verify_parent(path: Path, parent: int, expected: tuple[int, int]) -> None:
    try:
        filesystem.verify_directory_identity(parent, path.parent)
    except (OSError, NotImplementedError):
        raise ProviderCacheError(
            "provider cache parent changed during refresh", code="cache_conflict"
        ) from None
    if _directory_identity(parent) != expected:
        raise ProviderCacheError(
            "provider cache parent changed during refresh", code="cache_conflict"
        )


def _verify_published_target(
    parent: int, leaf: str, staged_identity: tuple[object, ...]
) -> None:
    try:
        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except OSError:
        raise ProviderCacheError(
            "provider cache replacement cannot be confirmed",
            code="cache_durability_unknown",
        ) from None
    if (
        not stat.S_ISREG(info.st_mode)
        or not _is_owner_only(info)
        or _entry_identity(info) != staged_identity
    ):
        raise ProviderCacheError(
            "provider cache replacement permissions cannot be confirmed",
            code="cache_durability_unknown",
        )


def _directory_identity(descriptor: int) -> tuple[int, int]:
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino


def _entry_identity(info: os.stat_result) -> tuple[object, ...]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_uid,
    )


def _entry_identity_at(parent: int, leaf: str) -> tuple[object, ...] | None:
    try:
        return _entry_identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _classify_replace_failure(
    path: Path,
    parent: int,
    leaf: str,
    temporary: str,
    old_identity: tuple[object, ...] | None,
    staged_identity: tuple[object, ...],
) -> tuple[str, bool | None]:
    try:
        _verify_parent(path, parent, _directory_identity(parent))
        target_identity = _entry_identity_at(parent, leaf)
        temporary_identity = _entry_identity_at(parent, temporary)
    except (ProviderCacheError, OSError):
        return "indeterminate", None
    if target_identity == staged_identity and temporary_identity is None:
        return "replaced", False
    if target_identity == old_identity and temporary_identity == staged_identity:
        return "not_replaced", True
    return "indeterminate", temporary_identity is not None


def _as_write_error(
    error: BaseException,
    *,
    published: bool,
    durability_confirmed: bool,
    publication_uncertain: bool,
    replace_attempted: bool,
) -> ProviderCacheError:
    if isinstance(error, ProviderCacheError):
        code = error.code
        message = str(error)
        publication_uncertain = publication_uncertain or error.publication_uncertain
    elif published or publication_uncertain or replace_attempted and not published:
        code = "cache_durability_unknown"
        message = "provider cache write outcome is not fully confirmed"
        publication_uncertain = True
    elif isinstance(error, NotImplementedError):
        code = "platform_unsupported"
        message = "provider cache platform is unsupported"
    elif isinstance(error, BlockingIOError):
        code = "cache_busy"
        message = "provider cache directory is busy"
    elif isinstance(error, PermissionError) or (
        isinstance(error, OSError) and error.errno in _PERMISSION_ERRNOS
    ):
        code = "cache_permission_denied"
        message = "provider cache cannot be written with owner-only permissions"
    else:
        code = "cache_write_failed"
        message = "provider cache write could not be confirmed"
    return ProviderCacheError(
        message,
        code=code,
        published=published,
        durability_confirmed=durability_confirmed,
        cleanup_confirmed=True,
        publication_uncertain=publication_uncertain,
    )


__all__ = [
    "CACHE_CAPABILITIES",
    "CACHE_ENTRY_STATUSES",
    "CACHE_INSPECTION_STATUSES",
    "CACHE_PROVIDER_TYPE",
    "CACHE_SCHEMA_VERSION",
    "CacheInspection",
    "CacheWriteResult",
    "ExecutableIdentity",
    "ProviderCacheEntry",
    "ProviderCacheError",
    "inspect_cache",
    "refresh_cache",
]
