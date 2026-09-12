"""Metadata-only durable records for the future M3c executor."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .errors import LuwuError

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
_STATES = frozenset(
    {
        "planned",
        "locked",
        "preflighted",
        "staged",
        "committing",
        "committed",
        "partial_commit",
        "recovery_required",
        "stale",
        "pending",
        "prepared",
        "commit_intent",
        "failed",
        "unknown",
    }
)
_TRANSITIONS = {
    "planned": {"locked", "stale"},
    "locked": {"preflighted", "stale", "recovery_required"},
    "preflighted": {"staged", "stale", "recovery_required"},
    "staged": {"committing", "recovery_required"},
    "committing": {"committed", "partial_commit", "recovery_required", "unknown"},
    "partial_commit": {"recovery_required", "committed", "stale"},
    "recovery_required": {"preflighted", "stale", "unknown"},
    "committed": set(),
    "stale": set(),
    "pending": {"prepared", "failed", "unknown"},
    "prepared": {"commit_intent", "failed", "unknown"},
    "commit_intent": {"committed", "unknown"},
    "failed": set(),
    "unknown": set(),
}
_FORBIDDEN_KEYS = frozenset(
    {"value", "values", "bytes", "content", "diff", "rendered", "secret", "provider"}
)


class PlanRecordError(LuwuError):
    """A durable plan record is invalid or cannot be safely updated."""

    default_code = "plan_record_invalid"


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """An immutable validated record; transitions return a new record."""

    _document: dict[str, Any]

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
        try:
            UUID(plan_id)
        except (ValueError, AttributeError):
            raise PlanRecordError(
                "plan_id must be a UUID", code="plan_record_id"
            ) from None
        document = {
            "record_schema_version": 1,
            "plan_id": plan_id,
            "execution_contract": execution_contract,
            "mutation_contract": mutation_contract,
            "manifest": dict(manifest),
            "policy": {"on_failure": "stop", "rollback": "never"},
            "resources": [dict(resource) for resource in resources],
            "state": "planned",
            "next_ordinal": 0,
            "events": [],
        }
        return cls._validated(document)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> PlanRecord:
        return cls._validated(copy.deepcopy(dict(document)))

    @classmethod
    def read(cls, path: Path) -> PlanRecord:
        if path.is_symlink():
            raise PlanRecordError(
                "plan record path is unsafe", code="plan_record_corrupt"
            )
        try:
            data = path.read_bytes()
            document = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise PlanRecordError(
                "plan record cannot be read safely", code="plan_record_corrupt"
            ) from None
        if not isinstance(document, dict):
            raise PlanRecordError(
                "plan record must be an object", code="plan_record_corrupt"
            )
        return cls.from_dict(document)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._document)

    def transition(self, state: str) -> PlanRecord:
        current = self._document["state"]
        if state not in _STATES or state not in _TRANSITIONS[current]:
            raise PlanRecordError(
                "plan record state transition is invalid", code="plan_record_transition"
            )
        document = self.to_dict()
        document["state"] = state
        document["events"].append({"state": state})
        return self._validated(document)

    def transition_path(self, ordinal: int, state: str) -> PlanRecord:
        if state not in _STATES:
            raise PlanRecordError(
                "path state is invalid", code="plan_record_transition"
            )
        document = self.to_dict()
        resources = document["resources"]
        matches = [resource for resource in resources if resource["ordinal"] == ordinal]
        if len(matches) != 1:
            raise PlanRecordError(
                "resource ordinal is invalid", code="plan_record_resource"
            )
        paths = matches[0]["paths"]
        for path in paths:
            if path["state"] == state:
                continue
            allowed = _TRANSITIONS[path["state"]]
            if state not in allowed:
                raise PlanRecordError(
                    "path state transition is invalid", code="plan_record_transition"
                )
            path["state"] = state
        return self._validated(document)

    def write(self, path: Path) -> None:
        data = (
            json.dumps(
                self._document, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            + b"\n"
        )
        parent = path.parent
        if not parent.is_dir() or path.is_symlink():
            raise PlanRecordError(
                "plan record path is unsafe", code="plan_record_write"
            )
        temporary: str | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.luwu-", dir=parent
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            directory_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except (OSError, NotImplementedError) as exc:
            raise PlanRecordError(
                "plan record write could not be confirmed", code="plan_record_write"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @classmethod
    def _validated(cls, document: dict[str, Any]) -> PlanRecord:
        _validate_document(document)
        return cls(document)


def _validate_document(document: Mapping[str, Any]) -> None:
    if set(document) != _TOP_LEVEL_KEYS:
        raise PlanRecordError(
            "plan record schema is not closed", code="plan_record_schema"
        )
    if document["record_schema_version"] != 1 or isinstance(
        document["record_schema_version"], bool
    ):
        raise PlanRecordError(
            "plan record schema version is unsupported", code="plan_record_schema"
        )
    if not isinstance(document["plan_id"], str):
        raise PlanRecordError("plan record id is invalid", code="plan_record_id")
    try:
        UUID(document["plan_id"])
    except (ValueError, AttributeError):
        raise PlanRecordError(
            "plan record id is invalid", code="plan_record_id"
        ) from None
    if not isinstance(document["execution_contract"], str) or not isinstance(
        document["mutation_contract"], str
    ):
        raise PlanRecordError(
            "plan record contract is invalid", code="plan_record_contract"
        )
    _closed_mapping(document["manifest"], _MANIFEST_KEYS, "plan_record_manifest")
    policy = document["policy"]
    _closed_mapping(policy, _POLICY_KEYS, "plan_record_policy")
    if policy.get("on_failure") != "stop" or policy.get("rollback") != "never":
        raise PlanRecordError(
            "plan record policy is unsupported", code="plan_record_policy"
        )
    if not isinstance(document["resources"], list):
        raise PlanRecordError(
            "plan record resources are invalid", code="plan_record_resource"
        )
    ordinals: set[int] = set()
    paths: set[str] = set()
    for resource in document["resources"]:
        _closed_mapping(resource, _RESOURCE_KEYS, "plan_record_resource")
        ordinal = resource["ordinal"]
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal in ordinals
        ):
            raise PlanRecordError(
                "resource ordinal is invalid", code="plan_record_resource"
            )
        ordinals.add(ordinal)
        if resource["state"] not in _STATES:
            raise PlanRecordError(
                "resource state is invalid", code="plan_record_transition"
            )
        if not isinstance(resource["paths"], list):
            raise PlanRecordError("resource paths are invalid", code="plan_record_path")
        for path in resource["paths"]:
            _closed_mapping(path, _PATH_KEYS, "plan_record_path")
            if not isinstance(path["path"], str) or Path(path["path"]).is_absolute():
                raise PlanRecordError(
                    "plan record paths must be relative", code="plan_record_path"
                )
            if path["path"] in paths:
                raise PlanRecordError(
                    "plan record paths must be unique", code="plan_record_path"
                )
            paths.add(path["path"])
            if path["state"] not in _STATES:
                raise PlanRecordError(
                    "path state is invalid", code="plan_record_transition"
                )
            for condition in (path["precondition"], path["postcondition"]):
                if not isinstance(condition, dict):
                    raise PlanRecordError(
                        "plan record preconditions are invalid", code="plan_record_path"
                    )
    if document["state"] not in _STATES:
        raise PlanRecordError(
            "plan record state is invalid", code="plan_record_transition"
        )
    if isinstance(document["next_ordinal"], bool) or not isinstance(
        document["next_ordinal"], int
    ):
        raise PlanRecordError(
            "plan record ordinal is invalid", code="plan_record_resource"
        )
    if not isinstance(document["events"], list):
        raise PlanRecordError(
            "plan record events are invalid", code="plan_record_event"
        )
    _reject_sensitive_keys(document)


def _closed_mapping(value: Any, keys: frozenset[str], code: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise PlanRecordError("plan record object is not closed", code=code)


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, dict):
        if any(str(key).casefold() in _FORBIDDEN_KEYS for key in value):
            raise PlanRecordError(
                "plan record contains configuration content", code="plan_record_secret"
            )
        for item in value.values():
            _reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)
