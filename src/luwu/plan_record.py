"""Closed, metadata-only durable records for the M3c and M4 executors."""

from __future__ import annotations

import copy
import errno
import json
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from .errors import LuwuError
from .platform_support import ensure_supported

fcntl: Any = None
try:
    import fcntl as _fcntl
except ImportError:
    pass
else:
    fcntl = _fcntl

_PLATFORM_OS = SimpleNamespace(
    **{
        name: getattr(os, name, None)
        for name in (
            "O_NOFOLLOW",
            "O_DIRECTORY",
            "O_NONBLOCK",
            "open",
            "close",
            "read",
            "stat",
            "lstat",
            "fstat",
            "fsync",
            "replace",
            "set_blocking",
            "killpg",
            "getuid",
            "unlink",
            "rename",
        )
    },
    supports_dir_fd=getattr(os, "supports_dir_fd", ()),
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "record_schema_version",
        "plan_id",
        "execution_contract",
        "mutation_contract",
        "manifest",
        "policy",
        "resources",
        "state",
        "next_ordinal",
        "events",
    }
)
_MANIFEST_KEYS = frozenset({"path", "root", "version", "digest"})
_POLICY_KEYS = frozenset({"on_failure", "rollback"})
_RESOURCE_KEYS = frozenset({"ordinal", "name", "operation", "paths", "state"})
_PATH_KEYS = frozenset(
    {"role", "path", "operation", "precondition", "postcondition", "state"}
)
_CONDITION_KEYS = frozenset({"type", "mode", "size", "mtime_ns", "file_id"})
_CONDITION_TYPES = frozenset({"missing", "unsafe", "regular", "symlink", "other"})
_EVENT_KEYS = frozenset({"scope", "ordinal", "path", "from_state", "to_state"})
_STATES = frozenset(
    {
        "planned",
        "preflighted",
        "commit_intent",
        "committed",
        "unchanged",
        "unknown",
        "recovery_required",
        "not-attempted",
    }
)
_TRANSITIONS = {
    "planned": {"preflighted", "unknown", "recovery_required", "not-attempted"},
    "preflighted": {
        "commit_intent",
        "unchanged",
        "unknown",
        "recovery_required",
        "not-attempted",
    },
    "commit_intent": {
        "committed",
        "unchanged",
        "unknown",
        "recovery_required",
    },
    "committed": set(),
    "unchanged": set(),
    "unknown": set(),
    "recovery_required": set(),
    "not-attempted": set(),
}


class _DuplicateRecordKey(ValueError):
    """A persisted record contains a duplicate JSON object key."""


class PlanRecordError(LuwuError):
    """A durable plan record is invalid or cannot be safely updated."""

    default_code = "plan_record_invalid"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        committed: bool = False,
        durability_confirmed: bool = False,
    ) -> None:
        super().__init__(message, code=code)
        self.committed = committed
        self.durability_confirmed = durability_confirmed


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """An immutable, validated metadata record; transitions return a copy."""

    _document: dict[str, Any]

    def __post_init__(self) -> None:
        document = copy.deepcopy(self._document)
        _validate_document(document)
        object.__setattr__(self, "_document", document)

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        execution_contract: str,
        mutation_contract: str,
        manifest: Mapping[str, Any],
        resources: list[Mapping[str, Any]],
    ) -> PlanRecord:
        if not isinstance(resources, list) or not resources:
            raise PlanRecordError(
                "plan record resources are invalid", code="plan_record_resource"
            )
        if not all(isinstance(resource, Mapping) for resource in resources):
            raise PlanRecordError(
                "plan record resources are invalid", code="plan_record_resource"
            )
        document = {
            "record_schema_version": 1,
            "plan_id": plan_id,
            "execution_contract": execution_contract,
            "mutation_contract": mutation_contract,
            "manifest": copy.deepcopy(dict(manifest)),
            "policy": {"on_failure": "stop", "rollback": "never"},
            "resources": [copy.deepcopy(dict(resource)) for resource in resources],
            "state": "planned",
            "next_ordinal": len(resources),
            "events": [],
        }
        return cls._validated(document)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> PlanRecord:
        if not isinstance(document, dict):
            raise PlanRecordError(
                "plan record must be an object", code="plan_record_schema"
            )
        return cls._validated(copy.deepcopy(document))

    @classmethod
    def read(cls, path: Path) -> PlanRecord:
        ensure_supported(os_module=_PLATFORM_OS, fcntl_module=fcntl)
        parent, leaf = _open_parent_directory(path)
        try:
            return cls._read_at(parent, leaf)
        finally:
            os.close(parent)

    @classmethod
    def _read_at(cls, parent: int, leaf: str) -> PlanRecord:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | _required_flag("O_NOFOLLOW"),
                dir_fd=parent,
            )
            if not _is_regular(os.fstat(descriptor).st_mode):
                raise PlanRecordError(
                    "plan record cannot be read safely", code="plan_record_corrupt"
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                try:
                    document = json.loads(
                        handle.read().decode("utf-8"),
                        object_pairs_hook=_record_object_without_duplicates,
                    )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    _DuplicateRecordKey,
                ) as exc:
                    raise PlanRecordError(
                        "plan record cannot be read safely", code="plan_record_corrupt"
                    ) from exc
            return cls.from_dict(document)
        except PlanRecordError:
            raise
        except (OSError, TypeError):
            raise PlanRecordError(
                "plan record cannot be read safely", code="plan_record_corrupt"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._document)

    def transition(self, state: str) -> PlanRecord:
        current = self._document["state"]
        _check_transition(current, state)
        document = self.to_dict()
        document["state"] = state
        document["events"].append(_event("plan", None, None, current, state))
        return self._validated(document)

    def transition_path(self, ordinal: int, state: str) -> PlanRecord:
        if state not in _STATES:
            raise PlanRecordError(
                "path state is invalid", code="plan_record_transition"
            )
        document = self.to_dict()
        matches = [
            resource
            for resource in document["resources"]
            if resource["ordinal"] == ordinal
        ]
        if len(matches) != 1:
            raise PlanRecordError(
                "resource ordinal is invalid", code="plan_record_resource"
            )
        resource = matches[0]
        _check_transition(resource["state"], state)
        resource["state"] = state
        for path in resource["paths"]:
            previous = path["state"]
            _check_transition(previous, state)
            path["state"] = state
            document["events"].append(
                _event("path", ordinal, path["path"], previous, state)
            )
        return self._validated(document)

    def update_path_condition(
        self,
        ordinal: int,
        path_name: str,
        *,
        precondition: Mapping[str, Any] | None = None,
        postcondition: Mapping[str, Any] | None = None,
    ) -> PlanRecord:
        """Return a copy with metadata conditions refreshed for one path."""

        document = self.to_dict()
        matches = [
            resource
            for resource in document["resources"]
            if resource["ordinal"] == ordinal
        ]
        if len(matches) != 1:
            raise PlanRecordError(
                "resource ordinal is invalid", code="plan_record_resource"
            )
        paths = [path for path in matches[0]["paths"] if path["path"] == path_name]
        if len(paths) != 1:
            raise PlanRecordError(
                "plan record path is invalid", code="plan_record_path"
            )
        path = paths[0]
        if precondition is not None:
            path["precondition"] = dict(precondition)
        if postcondition is not None:
            path["postcondition"] = dict(postcondition)
        return self._validated(document)

    def write(self, path: Path, *, expected: PlanRecord | None = None) -> None:
        """Atomically create or compare-and-swap a record.

        Without ``expected`` this is a create-only operation.  An existing
        journal must be updated with the exact previous record, which keeps
        the optimistic-concurrency contract explicit to callers.
        """

        ensure_supported(os_module=_PLATFORM_OS, fcntl_module=fcntl)
        _validate_document(self._document)
        if expected is not None and not isinstance(expected, PlanRecord):
            raise PlanRecordError(
                "expected plan record is invalid", code="plan_record_cas"
            )
        if (
            expected is not None
            and self._document["plan_id"] != expected._document["plan_id"]
        ):
            raise PlanRecordError(
                "plan record identity does not match expected record",
                code="plan_record_cas",
            )
        data = (
            json.dumps(
                self._document, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            + b"\n"
        )
        parent, leaf = _open_parent_directory(path)
        temporary: str | None = None
        lock: int | None = None
        published = False
        error: PlanRecordError | None = None
        cleanup_error: PlanRecordError | None = None
        try:
            parent_identity = _directory_identity(parent)
            lock = _open_record_lock(parent, leaf)
            _require_parent_identity(path, parent, parent_identity)
            _check_expected_record(parent, leaf, expected)
            for _ in range(32):
                candidate = f".{leaf}.luwu-{secrets.token_hex(12)}"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | _required_flag("O_NOFOLLOW"),
                        0o600,
                        dir_fd=parent,
                    )
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError(errno.EEXIST, "temporary record name collision")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                os.fsync(handle.fileno())

            _require_parent_identity(path, parent, parent_identity)
            _check_expected_record(parent, leaf, expected)
            if expected is None:
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                published = True
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError as exc:
                    raise PlanRecordError(
                        "plan record temporary cleanup is uncertain",
                        code="plan_record_durability_unknown",
                        committed=True,
                        durability_confirmed=False,
                    ) from exc
                temporary = None
            else:
                old_record_identity = _record_entry_identity_at(parent, leaf)
                staged_identity = _record_entry_identity_at(parent, temporary)
                try:
                    os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                except (OSError, NotImplementedError) as exc:
                    state, temporary_present = _classify_record_replace_failure(
                        parent,
                        leaf,
                        temporary,
                        old_record_identity,
                        staged_identity,
                    )
                    if temporary_present is False:
                        temporary = None
                    if state == "replaced":
                        published = True
                        raise PlanRecordError(
                            "plan record replacement occurred but durability is unknown",
                            code="plan_record_durability_unknown",
                            committed=True,
                            durability_confirmed=False,
                        ) from exc
                    if state == "indeterminate":
                        raise PlanRecordError(
                            "plan record replacement state is unknown",
                            code="plan_record_durability_unknown",
                            committed=False,
                            durability_confirmed=False,
                        ) from exc
                    raise PlanRecordError(
                        "plan record replacement did not occur",
                        code="plan_record_write",
                        committed=False,
                    ) from exc
                published = True
                temporary = None
            _require_parent_identity(path, parent, parent_identity, committed=True)
            os.fsync(parent)
        except PlanRecordError as exc:
            error = _with_publication_status(exc, published)
        except OSError as exc:
            error = PlanRecordError(
                "plan record write could not be confirmed",
                code=(
                    "plan_record_conflict"
                    if not published and expected is None and exc.errno == errno.EEXIST
                    else (
                        "plan_record_durability_unknown"
                        if published
                        else "plan_record_write"
                    )
                ),
                committed=published,
                durability_confirmed=False,
            )
            error.__cause__ = exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError as exc:
                    cleanup_error = PlanRecordError(
                        "plan record temporary cleanup could not be confirmed",
                        code=(
                            "plan_record_durability_unknown"
                            if published
                            else "plan_record_write"
                        ),
                        committed=published,
                        durability_confirmed=False,
                    )
                    cleanup_error.__cause__ = exc
            if lock is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                finally:
                    os.close(lock)
            os.close(parent)
        if cleanup_error is not None:
            if error is not None:
                raise cleanup_error from error
            raise cleanup_error
        if error is not None:
            raise error

    @classmethod
    def _validated(cls, document: dict[str, Any]) -> PlanRecord:
        _validate_document(document)
        return cls(document)


_SECRET_RECORD_TOP_LEVEL_KEYS = frozenset(
    {
        "record_schema_version",
        "plan_id",
        "execution_contract",
        "mutation_contract",
        "manifest",
        "policy",
        "resources",
        "state",
        "events",
    }
)
_SECRET_RECORD_MANIFEST_KEYS = frozenset({"path", "root", "version"})
_SECRET_RECORD_POLICY = {"on_failure": "stop", "rollback": "never"}
_SECRET_RECORD_RESOURCE_KEYS = frozenset(
    {"ordinal", "label", "operation", "target_state", "state"}
)
_SECRET_RECORD_EVENT_KEYS = frozenset({"scope", "ordinal", "from_state", "to_state"})
_SECRET_RECORD_TARGET_STATES = frozenset({"missing", "regular", "unsafe"})
_SECRET_RECORD_OPERATIONS = frozenset({"noop", "create", "replace", "block", "report"})


class SecretPlanRecordError(PlanRecordError):
    """A version-6 secret-safe journal is invalid or unsafe to update."""


@dataclass(frozen=True, slots=True)
class SecretPlanRecord:
    """Independent v6 record with no resource paths, digests, sizes, or content facts."""

    _document: dict[str, Any]

    def __post_init__(self) -> None:
        document = copy.deepcopy(self._document)
        _validate_secret_record(document)
        object.__setattr__(self, "_document", document)

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        execution_contract: str,
        mutation_contract: str,
        manifest: Mapping[str, Any],
        resources: list[Mapping[str, Any]],
    ) -> SecretPlanRecord:
        if not isinstance(resources, list) or not resources:
            raise SecretPlanRecordError(
                "secret execution resources are invalid",
                code="plan_record_resource",
            )
        document = {
            "record_schema_version": 1,
            "plan_id": plan_id,
            "execution_contract": execution_contract,
            "mutation_contract": mutation_contract,
            "manifest": copy.deepcopy(dict(manifest)),
            "policy": dict(_SECRET_RECORD_POLICY),
            "resources": [copy.deepcopy(dict(resource)) for resource in resources],
            "state": "planned",
            "events": [],
        }
        return cls._validated(document)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> SecretPlanRecord:
        if not isinstance(document, dict):
            raise SecretPlanRecordError(
                "secret execution record must be an object",
                code="plan_record_schema",
            )
        return cls._validated(copy.deepcopy(document))

    @classmethod
    def read(cls, path: Path) -> SecretPlanRecord:
        ensure_supported(os_module=_PLATFORM_OS, fcntl_module=fcntl)
        parent, leaf = _open_parent_directory(path)
        try:
            return cls._read_at(parent, leaf)
        finally:
            os.close(parent)

    @classmethod
    def _read_at(cls, parent: int, leaf: str) -> SecretPlanRecord:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | _required_flag("O_NOFOLLOW"),
                dir_fd=parent,
            )
            info = os.fstat(descriptor)
            if (
                not _is_regular(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_mode & 0o7000
            ):
                raise SecretPlanRecordError(
                    "secret execution record cannot be read safely",
                    code="plan_record_corrupt",
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                try:
                    document = json.loads(
                        handle.read().decode("utf-8"),
                        object_pairs_hook=_record_object_without_duplicates,
                    )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    _DuplicateRecordKey,
                ) as exc:
                    raise SecretPlanRecordError(
                        "secret execution record cannot be read safely",
                        code="plan_record_corrupt",
                    ) from exc
            return cls.from_dict(document)
        except SecretPlanRecordError:
            raise
        except (OSError, TypeError):
            raise SecretPlanRecordError(
                "secret execution record cannot be read safely",
                code="plan_record_corrupt",
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._document)

    def transition(self, state: str) -> SecretPlanRecord:
        current = self._document["state"]
        _check_transition(current, state)
        document = self.to_dict()
        document["state"] = state
        document["events"].append(_secret_event(None, current, state))
        return self._validated(document)

    def transition_path(self, ordinal: int, state: str) -> SecretPlanRecord:
        """Transition one resource; the name matches the v5 execution seam."""

        if state not in _STATES:
            raise SecretPlanRecordError(
                "secret execution resource state is invalid",
                code="plan_record_transition",
            )
        document = self.to_dict()
        matches = [
            resource
            for resource in document["resources"]
            if resource["ordinal"] == ordinal
        ]
        if len(matches) != 1:
            raise SecretPlanRecordError(
                "secret execution resource ordinal is invalid",
                code="plan_record_resource",
            )
        resource = matches[0]
        previous = resource["state"]
        _check_transition(previous, state)
        resource["state"] = state
        document["events"].append(_secret_event(ordinal, previous, state))
        return self._validated(document)

    def update_path_condition(
        self,
        ordinal: int,
        path_name: str,
        *,
        precondition: Mapping[str, Any] | None = None,
        postcondition: Mapping[str, Any] | None = None,
    ) -> SecretPlanRecord:
        """Keep the v5 writer seam without persisting a secret target condition."""

        del path_name, precondition, postcondition
        if not any(
            resource["ordinal"] == ordinal for resource in self._document["resources"]
        ):
            raise SecretPlanRecordError(
                "secret execution resource ordinal is invalid",
                code="plan_record_resource",
            )
        return self

    def write(
        self,
        path: Path,
        *,
        expected: SecretPlanRecord | None = None,
    ) -> None:
        """Atomically create or compare-and-swap an owner-only v6 record."""

        ensure_supported(os_module=_PLATFORM_OS, fcntl_module=fcntl)
        _validate_secret_record(self._document)
        if expected is not None and not isinstance(expected, SecretPlanRecord):
            raise SecretPlanRecordError(
                "expected secret execution record is invalid",
                code="plan_record_cas",
            )
        if (
            expected is not None
            and self._document["plan_id"] != expected._document["plan_id"]
        ):
            raise SecretPlanRecordError(
                "secret execution record identity does not match expected record",
                code="plan_record_cas",
            )
        data = (
            json.dumps(
                self._document, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            + b"\n"
        )
        parent, leaf = _open_parent_directory(path)
        temporary: str | None = None
        lock: int | None = None
        published = False
        error: SecretPlanRecordError | None = None
        cleanup_error: SecretPlanRecordError | None = None
        try:
            parent_identity = _directory_identity(parent)
            lock = _open_secret_record_lock(parent, leaf)
            _require_parent_identity(path, parent, parent_identity)
            _check_expected_secret_record(parent, leaf, expected)
            for _ in range(32):
                candidate = f".{leaf}.luwu-{secrets.token_hex(12)}"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | _required_flag("O_NOFOLLOW"),
                        0o600,
                        dir_fd=parent,
                    )
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError(errno.EEXIST, "temporary record name collision")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                os.fsync(handle.fileno())

            _require_parent_identity(path, parent, parent_identity)
            _check_expected_secret_record(parent, leaf, expected)
            if expected is None:
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                published = True
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError as exc:
                    raise SecretPlanRecordError(
                        "secret execution record cleanup is uncertain",
                        code="plan_record_durability_unknown",
                        committed=True,
                        durability_confirmed=False,
                    ) from exc
                temporary = None
            else:
                old_record_identity = _record_entry_identity_at(parent, leaf)
                staged_identity = _record_entry_identity_at(parent, temporary)
                try:
                    os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                except (OSError, NotImplementedError) as exc:
                    state, temporary_present = _classify_record_replace_failure(
                        parent,
                        leaf,
                        temporary,
                        old_record_identity,
                        staged_identity,
                    )
                    if temporary_present is False:
                        temporary = None
                    if state == "replaced":
                        published = True
                        raise SecretPlanRecordError(
                            "secret execution record replacement occurred but durability is unknown",
                            code="plan_record_durability_unknown",
                            committed=True,
                            durability_confirmed=False,
                        ) from exc
                    if state == "indeterminate":
                        raise SecretPlanRecordError(
                            "secret execution record replacement state is unknown",
                            code="plan_record_durability_unknown",
                            committed=False,
                            durability_confirmed=False,
                        ) from exc
                    raise SecretPlanRecordError(
                        "secret execution record replacement did not occur",
                        code="plan_record_write",
                        committed=False,
                    ) from exc
                published = True
                temporary = None
            _require_parent_identity(path, parent, parent_identity, committed=True)
            os.fsync(parent)
        except SecretPlanRecordError as exc:
            error = _with_secret_publication_status(exc, published)
        except PlanRecordError as exc:
            error = SecretPlanRecordError(
                "secret execution record write could not be confirmed",
                code="plan_record_durability_unknown"
                if published
                else "plan_record_write",
                committed=published or exc.committed,
                durability_confirmed=False,
            )
            error.__cause__ = exc
        except OSError as exc:
            error = SecretPlanRecordError(
                "secret execution record write could not be confirmed",
                code=(
                    "plan_record_conflict"
                    if not published and expected is None and exc.errno == errno.EEXIST
                    else (
                        "plan_record_durability_unknown"
                        if published
                        else "plan_record_write"
                    )
                ),
                committed=published,
                durability_confirmed=False,
            )
            error.__cause__ = exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError as exc:
                    cleanup_error = SecretPlanRecordError(
                        "secret execution record cleanup could not be confirmed",
                        code=(
                            "plan_record_durability_unknown"
                            if published
                            else "plan_record_write"
                        ),
                        committed=published,
                        durability_confirmed=False,
                    )
                    cleanup_error.__cause__ = exc
            if lock is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                finally:
                    os.close(lock)
            os.close(parent)
        if cleanup_error is not None:
            if error is not None:
                raise cleanup_error from error
            raise cleanup_error
        if error is not None:
            raise error

    @classmethod
    def _validated(cls, document: dict[str, Any]) -> SecretPlanRecord:
        _validate_secret_record(document)
        return cls(document)


def _secret_event(ordinal: int | None, before: str, after: str) -> dict[str, Any]:
    return {
        "scope": "plan" if ordinal is None else "resource",
        "ordinal": ordinal,
        "from_state": before,
        "to_state": after,
    }


def _validate_secret_record(document: Mapping[str, Any]) -> None:
    if not isinstance(document, dict) or set(document) != _SECRET_RECORD_TOP_LEVEL_KEYS:
        raise SecretPlanRecordError(
            "secret execution record schema is not closed",
            code="plan_record_schema",
        )
    _int(
        document["record_schema_version"],
        "secret execution record schema is unsupported",
        "plan_record_schema",
        exact=1,
    )
    _uuid(document["plan_id"])
    _string(document["execution_contract"], "plan_record_contract")
    _string(document["mutation_contract"], "plan_record_contract")

    manifest = document["manifest"]
    if not isinstance(manifest, dict) or set(manifest) != _SECRET_RECORD_MANIFEST_KEYS:
        raise SecretPlanRecordError(
            "secret execution record manifest is not closed",
            code="plan_record_manifest",
        )
    for key in ("path", "root"):
        value = manifest[key]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise SecretPlanRecordError(
                "secret execution record manifest is invalid",
                code="plan_record_manifest",
            )
    if Path(manifest["path"]).parent != Path(manifest["root"]):
        raise SecretPlanRecordError(
            "secret execution record manifest paths are invalid",
            code="plan_record_manifest",
        )
    _int(
        manifest["version"],
        "secret execution record manifest is invalid",
        "plan_record_manifest",
        exact=6,
    )

    if document["policy"] != _SECRET_RECORD_POLICY:
        raise SecretPlanRecordError(
            "secret execution record policy is unsupported",
            code="plan_record_policy",
        )
    resources = document["resources"]
    if not isinstance(resources, list) or not resources:
        raise SecretPlanRecordError(
            "secret execution record resources are invalid",
            code="plan_record_resource",
        )
    ordinals: set[int] = set()
    labels: set[str] = set()
    for resource in resources:
        if (
            not isinstance(resource, dict)
            or set(resource) != _SECRET_RECORD_RESOURCE_KEYS
        ):
            raise SecretPlanRecordError(
                "secret execution record resource is not closed",
                code="plan_record_resource",
            )
        _int(
            resource["ordinal"],
            "secret execution resource ordinal is invalid",
            "plan_record_resource",
        )
        ordinal = resource["ordinal"]
        if ordinal < 0 or ordinal in ordinals:
            raise SecretPlanRecordError(
                "secret execution resource ordinal is invalid",
                code="plan_record_resource",
            )
        ordinals.add(ordinal)
        label = resource["label"]
        _string(label, "plan_record_resource")
        if label in labels or label != f"resource-{ordinal}":
            raise SecretPlanRecordError(
                "secret execution resource label is invalid",
                code="plan_record_resource",
            )
        labels.add(label)
        if resource["operation"] not in _SECRET_RECORD_OPERATIONS:
            raise SecretPlanRecordError(
                "secret execution resource operation is invalid",
                code="plan_record_resource",
            )
        if resource["target_state"] not in _SECRET_RECORD_TARGET_STATES:
            raise SecretPlanRecordError(
                "secret execution target state is invalid",
                code="plan_record_resource",
            )
        _state(resource["state"], "secret execution resource state is invalid")
    if ordinals != set(range(len(resources))):
        raise SecretPlanRecordError(
            "secret execution resource ordinals must be contiguous",
            code="plan_record_resource",
        )

    _state(document["state"], "secret execution record state is invalid")
    events = document["events"]
    if not isinstance(events, list):
        raise SecretPlanRecordError(
            "secret execution record events are invalid",
            code="plan_record_event",
        )
    for event in events:
        if not isinstance(event, dict) or set(event) != _SECRET_RECORD_EVENT_KEYS:
            raise SecretPlanRecordError(
                "secret execution record event is not closed",
                code="plan_record_event",
            )
        scope = event["scope"]
        if scope == "plan":
            if event["ordinal"] is not None:
                raise SecretPlanRecordError(
                    "secret execution record event is invalid",
                    code="plan_record_event",
                )
        elif scope == "resource":
            _int(
                event["ordinal"],
                "secret execution record event is invalid",
                "plan_record_event",
            )
            if event["ordinal"] not in ordinals:
                raise SecretPlanRecordError(
                    "secret execution record event provenance is invalid",
                    code="plan_record_event",
                )
        else:
            raise SecretPlanRecordError(
                "secret execution record event is invalid",
                code="plan_record_event",
            )
        _state(event["from_state"], "secret execution record event is invalid")
        _state(event["to_state"], "secret execution record event is invalid")
        if event["to_state"] not in _TRANSITIONS[event["from_state"]]:
            raise SecretPlanRecordError(
                "secret execution record event transition is invalid",
                code="plan_record_event",
            )
    _validate_secret_event_history(document, resources)


def _validate_secret_event_history(
    document: Mapping[str, Any], resources: list[Mapping[str, Any]]
) -> None:
    plan_state = "planned"
    resource_states = {resource["ordinal"]: "planned" for resource in resources}
    for event in document["events"]:
        if event["scope"] == "plan":
            if event["from_state"] != plan_state:
                raise SecretPlanRecordError(
                    "secret execution record event history is inconsistent",
                    code="plan_record_event",
                )
            _check_transition(plan_state, event["to_state"])
            plan_state = event["to_state"]
        else:
            ordinal = event["ordinal"]
            current = resource_states[ordinal]
            if event["from_state"] != current:
                raise SecretPlanRecordError(
                    "secret execution record event history is inconsistent",
                    code="plan_record_event",
                )
            _check_transition(current, event["to_state"])
            resource_states[ordinal] = event["to_state"]
    if plan_state != document["state"]:
        raise SecretPlanRecordError(
            "secret execution record state does not match its event history",
            code="plan_record_event",
        )
    for resource in resources:
        if resource_states[resource["ordinal"]] != resource["state"]:
            raise SecretPlanRecordError(
                "secret execution resource state does not match its event history",
                code="plan_record_event",
            )
    allowed: dict[str, set[str]] = {
        "planned": {"planned"},
        "preflighted": {
            "planned",
            "preflighted",
            "unknown",
            "recovery_required",
            "not-attempted",
        },
        "commit_intent": {
            "preflighted",
            "commit_intent",
            "committed",
            "unchanged",
            "unknown",
            "recovery_required",
            "not-attempted",
        },
        "committed": {"committed", "unchanged"},
        "unchanged": {"unchanged"},
        "unknown": {"unknown", "not-attempted"},
        "recovery_required": {"committed", "unchanged", "unknown", "not-attempted"},
        "not-attempted": {"not-attempted"},
    }
    states = {resource["state"] for resource in resources}
    if not states.issubset(allowed[document["state"]]):
        raise SecretPlanRecordError(
            "secret execution record and resource states are inconsistent",
            code="plan_record_event",
        )
    if document["state"] in {
        "unknown",
        "recovery_required",
    } and not states.intersection({"unknown", "not-attempted"}):
        raise SecretPlanRecordError(
            "secret execution record does not expose a recovery boundary",
            code="plan_record_event",
        )


def _open_secret_record_lock(parent: int, leaf: str) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            _record_lock_name(leaf),
            os.O_RDWR | os.O_CREAT | _required_flag("O_NOFOLLOW"),
            0o600,
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        if (
            not _is_regular(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_mode & 0o7000
        ):
            raise SecretPlanRecordError(
                "secret execution record lock path is unsafe",
                code="plan_record_write",
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except SecretPlanRecordError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise SecretPlanRecordError(
            "secret execution record lock could not be acquired",
            code="plan_record_write",
        ) from None


def _check_expected_secret_record(
    parent: int, leaf: str, expected: SecretPlanRecord | None
) -> None:
    try:
        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if expected is not None:
            raise SecretPlanRecordError(
                "expected secret execution record is missing",
                code="plan_record_cas",
            )
        return
    except OSError:
        raise SecretPlanRecordError(
            "secret execution record path cannot be checked",
            code="plan_record_write",
        ) from None
    if (
        not _is_regular(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_mode & 0o7000
    ):
        raise SecretPlanRecordError(
            "secret execution record path is unsafe",
            code="plan_record_write",
        )
    if expected is None:
        raise SecretPlanRecordError(
            "existing secret execution record requires an expected record",
            code="plan_record_conflict",
        )
    current = SecretPlanRecord._read_at(parent, leaf)
    if current.to_dict() != expected.to_dict():
        raise SecretPlanRecordError(
            "secret execution record changed since the expected snapshot",
            code="plan_record_cas",
        )


def _with_secret_publication_status(
    error: SecretPlanRecordError, published: bool
) -> SecretPlanRecordError:
    if not published or error.committed:
        return error
    return SecretPlanRecordError(
        str(error),
        code="plan_record_durability_unknown",
        committed=True,
        durability_confirmed=False,
    )


def _record_entry_identity_at(parent: int, name: str) -> tuple[int, int, int] | None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _classify_record_replace_failure(
    parent: int,
    leaf: str,
    temporary: str,
    old_record_identity: tuple[int, int, int] | None,
    staged_identity: tuple[int, int, int] | None,
) -> tuple[str, bool | None]:
    """Classify a journal replace exception from no-follow entry identities."""

    try:
        record_identity = _record_entry_identity_at(parent, leaf)
        temporary_identity = _record_entry_identity_at(parent, temporary)
    except (OSError, NotImplementedError):
        return "indeterminate", None
    if record_identity == staged_identity and temporary_identity is None:
        return "replaced", False
    if record_identity == old_record_identity and temporary_identity == staged_identity:
        return "not_replaced", True
    return "indeterminate", temporary_identity is not None


def _event(
    scope: str, ordinal: int | None, path: str | None, before: str, after: str
) -> dict[str, Any]:
    return {
        "scope": scope,
        "ordinal": ordinal,
        "path": path,
        "from_state": before,
        "to_state": after,
    }


def _record_object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise _DuplicateRecordKey(key)
        values[key] = value
    return values


def _validate_document(document: Mapping[str, Any]) -> None:
    if not isinstance(document, dict) or set(document) != _TOP_LEVEL_KEYS:
        raise PlanRecordError(
            "plan record schema is not closed", code="plan_record_schema"
        )
    _int(
        document["record_schema_version"],
        "plan record schema version is unsupported",
        "plan_record_schema",
        exact=1,
    )
    _uuid(document["plan_id"])
    _string(document["execution_contract"], "plan_record_contract")
    _string(document["mutation_contract"], "plan_record_contract")
    _validate_manifest(document["manifest"])
    _closed_mapping(document["policy"], _POLICY_KEYS, "plan_record_policy")
    if document["policy"] != {"on_failure": "stop", "rollback": "never"}:
        raise PlanRecordError(
            "plan record policy is unsupported", code="plan_record_policy"
        )
    resources = document["resources"]
    if not isinstance(resources, list) or not resources:
        raise PlanRecordError(
            "plan record resources are invalid", code="plan_record_resource"
        )
    ordinals: set[int] = set()
    names: set[str] = set()
    paths: set[tuple[int, str]] = set()
    for resource in resources:
        _validate_resource(resource, ordinals, names, paths)
    if ordinals != set(range(len(resources))):
        raise PlanRecordError(
            "resource ordinals must be contiguous", code="plan_record_resource"
        )
    _state(document["state"], "plan record state is invalid")
    _int(
        document["next_ordinal"],
        "plan record ordinal is invalid",
        "plan_record_resource",
    )
    if document["next_ordinal"] != len(resources):
        raise PlanRecordError(
            "plan record ordinal is invalid", code="plan_record_resource"
        )
    if not isinstance(document["events"], list):
        raise PlanRecordError(
            "plan record events are invalid", code="plan_record_event"
        )
    for event in document["events"]:
        _closed_mapping(event, _EVENT_KEYS, "plan_record_event")
        if event["scope"] not in {"plan", "path"}:
            raise PlanRecordError(
                "plan record event is invalid", code="plan_record_event"
            )
        if event["scope"] == "plan":
            if event["ordinal"] is not None or event["path"] is not None:
                raise PlanRecordError(
                    "plan record event is invalid", code="plan_record_event"
                )
        else:
            _int(event["ordinal"], "plan record event is invalid", "plan_record_event")
            _relative_path(event["path"])
            if not any(
                resource["ordinal"] == event["ordinal"]
                and any(path["path"] == event["path"] for path in resource["paths"])
                for resource in resources
            ):
                raise PlanRecordError(
                    "plan record event provenance is invalid", code="plan_record_event"
                )
        _state(event["from_state"], "plan record event is invalid")
        _state(event["to_state"], "plan record event is invalid")
        if event["to_state"] not in _TRANSITIONS[event["from_state"]]:
            raise PlanRecordError(
                "plan record event transition is invalid", code="plan_record_event"
            )
    _validate_event_history(document, resources)


def _validate_manifest(value: Any) -> None:
    _closed_mapping(value, _MANIFEST_KEYS, "plan_record_manifest")
    for key in ("path", "root"):
        _string(value[key], "plan_record_manifest")
    digest = value["digest"]
    _string(digest, "plan_record_manifest")
    if (
        len(digest) != len("sha256:") + 64
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise PlanRecordError(
            "plan record manifest digest is invalid", code="plan_record_manifest"
        )
    _int(value["version"], "plan record manifest is invalid", "plan_record_manifest")


def _validate_resource(
    value: Any, ordinals: set[int], names: set[str], paths: set[tuple[int, str]]
) -> None:
    _closed_mapping(value, _RESOURCE_KEYS, "plan_record_resource")
    _int(value["ordinal"], "resource ordinal is invalid", "plan_record_resource")
    if value["ordinal"] < 0:
        raise PlanRecordError(
            "resource ordinal is invalid", code="plan_record_resource"
        )
    if value["ordinal"] in ordinals:
        raise PlanRecordError(
            "resource ordinal is invalid", code="plan_record_resource"
        )
    ordinals.add(value["ordinal"])
    _string(value["name"], "plan_record_resource")
    if value["name"] in names:
        raise PlanRecordError(
            "resource name is not unique", code="plan_record_resource"
        )
    names.add(value["name"])
    _string(value["operation"], "plan_record_resource")
    _state(value["state"], "resource state is invalid")
    if not isinstance(value["paths"], list) or not value["paths"]:
        raise PlanRecordError("resource paths are invalid", code="plan_record_path")
    for path in value["paths"]:
        _closed_mapping(path, _PATH_KEYS, "plan_record_path")
        _string(path["role"], "plan_record_path")
        _relative_path(path["path"])
        key = (value["ordinal"], path["path"])
        if key in paths:
            raise PlanRecordError(
                "plan record paths must be unique", code="plan_record_path"
            )
        paths.add(key)
        _string(path["operation"], "plan_record_path")
        _validate_condition(path["precondition"])
        _validate_condition(path["postcondition"])
        _state(path["state"], "path state is invalid")


def _validate_condition(value: Any) -> None:
    _closed_mapping(value, _CONDITION_KEYS, "plan_record_path")
    condition_type = value["type"]
    if not isinstance(condition_type, str) or condition_type not in _CONDITION_TYPES:
        raise PlanRecordError(
            "plan record condition type is invalid", code="plan_record_path"
        )
    for key in ("mode", "size", "mtime_ns", "file_id"):
        _int(value[key], "plan record condition is invalid", "plan_record_path")
    if not 0 <= value["mode"] <= 0o777:
        raise PlanRecordError(
            "plan record condition mode is invalid", code="plan_record_path"
        )
    if value["size"] < 0 or value["file_id"] < 0:
        raise PlanRecordError(
            "plan record condition size or file id is invalid",
            code="plan_record_path",
        )
    if condition_type in {"missing", "unsafe"} and any(
        value[key] != 0 for key in ("mode", "size", "mtime_ns", "file_id")
    ):
        raise PlanRecordError(
            "missing and unsafe conditions must be zeroed",
            code="plan_record_path",
        )


def _validate_event_history(
    document: Mapping[str, Any], resources: list[Mapping[str, Any]]
) -> None:
    """Ensure persisted states are the result of the persisted transitions."""

    plan_state = "planned"
    path_states = {
        (resource["ordinal"], path["path"]): "planned"
        for resource in resources
        for path in resource["paths"]
    }
    for event in document["events"]:
        before = event["from_state"]
        after = event["to_state"]
        if event["scope"] == "plan":
            if before != plan_state:
                raise PlanRecordError(
                    "plan record event history is inconsistent",
                    code="plan_record_event",
                )
            _check_transition(plan_state, after)
            plan_state = after
            continue

        key = (event["ordinal"], event["path"])
        current = path_states.get(key)
        if current is None or before != current:
            raise PlanRecordError(
                "plan record event history is inconsistent",
                code="plan_record_event",
            )
        _check_transition(current, after)
        path_states[key] = after

    if plan_state != document["state"]:
        raise PlanRecordError(
            "plan record state does not match its event history",
            code="plan_record_event",
        )
    for resource in resources:
        states = {
            path_states[(resource["ordinal"], path["path"])]
            for path in resource["paths"]
        }
        if states != {resource["state"]}:
            raise PlanRecordError(
                "resource state does not match its event history",
                code="plan_record_event",
            )
    _validate_cross_state(document["state"], resources)


def _validate_cross_state(plan_state: str, resources: list[Mapping[str, Any]]) -> None:
    resource_states = {resource["state"] for resource in resources}
    allowed: dict[str, set[str]] = {
        "planned": {"planned"},
        "preflighted": {
            "planned",
            "preflighted",
            "unknown",
            "recovery_required",
            "not-attempted",
        },
        "commit_intent": {
            "preflighted",
            "commit_intent",
            "committed",
            "unchanged",
            "unknown",
            "recovery_required",
            "not-attempted",
        },
        "committed": {"committed", "unchanged"},
        "unchanged": {"unchanged"},
        "unknown": {"unknown", "not-attempted"},
        "recovery_required": {
            "committed",
            "unchanged",
            "unknown",
            "not-attempted",
        },
        "not-attempted": {"not-attempted"},
    }
    if not resource_states.issubset(allowed[plan_state]):
        raise PlanRecordError(
            "plan and resource states are inconsistent", code="plan_record_event"
        )
    if plan_state in {
        "unknown",
        "recovery_required",
    } and not resource_states.intersection({"unknown", "not-attempted"}):
        raise PlanRecordError(
            "plan state does not expose a recovery boundary",
            code="plan_record_event",
        )


def _closed_mapping(value: Any, keys: frozenset[str], code: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise PlanRecordError("plan record object is not closed", code=code)


def _state(value: Any, message: str) -> None:
    if not isinstance(value, str) or value not in _STATES:
        raise PlanRecordError(message, code="plan_record_transition")


def _check_transition(current: str, state: str) -> None:
    if state not in _STATES or state not in _TRANSITIONS[current]:
        raise PlanRecordError(
            "plan record state transition is invalid", code="plan_record_transition"
        )


def _string(value: Any, code: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) < 0x20 for char in value)
    ):
        raise PlanRecordError("plan record string is invalid", code=code)


def _int(value: Any, message: str, code: str, *, exact: int | None = None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or (exact is not None and value != exact)
    ):
        raise PlanRecordError(message, code=code)


def _uuid(value: Any) -> None:
    if not isinstance(value, str):
        raise PlanRecordError("plan record id is invalid", code="plan_record_id")
    try:
        UUID(value)
    except ValueError:
        raise PlanRecordError(
            "plan record id is invalid", code="plan_record_id"
        ) from None


def _relative_path(value: Any) -> None:
    _string(value, "plan_record_path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PlanRecordError(
            "plan record paths must be relative", code="plan_record_path"
        )


def _is_regular(mode: int) -> bool:
    return (mode & 0o170000) == 0o100000


def _directory_identity(descriptor: int) -> tuple[int, int]:
    stat_result = os.fstat(descriptor)
    return stat_result.st_dev, stat_result.st_ino


def _parent_identity_matches(
    path: Path, parent: int, expected: tuple[int, int]
) -> bool:
    try:
        current, _ = _open_parent_directory(path)
    except PlanRecordError:
        return False
    try:
        return _directory_identity(parent) == expected == _directory_identity(current)
    finally:
        os.close(current)


def _require_parent_identity(
    path: Path,
    parent: int,
    expected: tuple[int, int],
    *,
    committed: bool = False,
) -> None:
    if not _parent_identity_matches(path, parent, expected):
        raise PlanRecordError(
            "plan record parent path changed during write",
            code="plan_record_durability_unknown" if committed else "plan_record_write",
            committed=committed,
            durability_confirmed=False,
        )


def _open_record_lock(parent: int, leaf: str) -> int:
    lock_name = _record_lock_name(leaf)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | _required_flag("O_NOFOLLOW"),
            0o600,
            dir_fd=parent,
        )
        if not _is_regular(os.fstat(descriptor).st_mode):
            raise PlanRecordError(
                "plan record lock path is unsafe", code="plan_record_write"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except PlanRecordError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise PlanRecordError(
            "plan record lock could not be acquired", code="plan_record_write"
        ) from exc


def record_lock_path(record_path: Path) -> Path:
    """Return the deterministic sidecar lock path for a plan record."""

    return record_path.parent / _record_lock_name(record_path.name)


def _record_lock_name(leaf: str) -> str:
    return f".{leaf}.luwu-lock"


def _check_expected_record(parent: int, leaf: str, expected: PlanRecord | None) -> None:
    try:
        stat_result = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if expected is not None:
            raise PlanRecordError(
                "expected plan record is missing", code="plan_record_cas"
            )
        return
    except OSError as exc:
        raise PlanRecordError(
            "plan record path cannot be checked", code="plan_record_write"
        ) from exc
    if not _is_regular(stat_result.st_mode):
        raise PlanRecordError("plan record path is unsafe", code="plan_record_write")
    if expected is None:
        raise PlanRecordError(
            "existing plan record requires an expected record",
            code="plan_record_conflict",
        )
    current = PlanRecord._read_at(parent, leaf)
    if current.to_dict() != expected.to_dict():
        raise PlanRecordError(
            "plan record changed since the expected snapshot",
            code="plan_record_cas",
        )


def _with_publication_status(
    error: PlanRecordError, published: bool
) -> PlanRecordError:
    if not published or error.committed:
        return error
    return PlanRecordError(
        str(error),
        code="plan_record_durability_unknown",
        committed=True,
        durability_confirmed=False,
    )


def _open_parent_directory(path: Path) -> tuple[int, str]:
    raw = os.fspath(path)
    if not raw or raw.endswith(os.sep):
        raise PlanRecordError("plan record path is unsafe", code="plan_record_write")
    absolute = os.path.isabs(raw)
    parts = [part for part in raw.split(os.sep) if part]
    if not parts or parts[-1] in {".", ".."}:
        raise PlanRecordError("plan record path is unsafe", code="plan_record_write")
    leaf = parts.pop()
    if any(part in {".", ".."} for part in parts):
        raise PlanRecordError("plan record path is unsafe", code="plan_record_write")
    flags = os.O_RDONLY | _required_flag("O_DIRECTORY") | _required_flag("O_NOFOLLOW")
    descriptor: int | None = None
    try:
        descriptor = os.open(os.sep if absolute else ".", flags)
        for part in parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, leaf
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise PlanRecordError(
            "plan record path is unsafe", code="plan_record_write"
        ) from exc


def _required_flag(name: str) -> int:
    try:
        return getattr(os, name)
    except AttributeError as exc:
        raise PlanRecordError(
            "required filesystem safety primitive is unavailable",
            code="platform_unsupported",
        ) from exc
