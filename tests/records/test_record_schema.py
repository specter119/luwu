"""Tests for record schema."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from luwu.plan_record import PlanRecord, PlanRecordError
from luwu.reconcile import build_plan, execute_execution_plan, inspect_execution_record
from tests.support.projects import ExecutionProject


def _record() -> PlanRecord:
    return PlanRecord.create(
        plan_id=str(uuid4()),
        execution_contract="execution-contract-1",
        mutation_contract="mutation-contract-1",
        manifest={
            "path": "luwu.toml",
            "root": ".",
            "version": 4,
            "digest": "sha256:" + "0" * 64,
        },
        resources=[
            {
                "ordinal": 0,
                "name": "settings",
                "operation": "reverse-sync",
                "paths": [
                    {
                        "role": "source",
                        "path": "templates/settings.json.j2",
                        "operation": "write_source_input",
                        "precondition": {
                            "type": "regular",
                            "mode": 420,
                            "size": 0,
                            "mtime_ns": 0,
                            "file_id": 0,
                        },
                        "postcondition": {
                            "type": "regular",
                            "mode": 420,
                            "size": 0,
                            "mtime_ns": 0,
                            "file_id": 0,
                        },
                        "state": "planned",
                    }
                ],
                "state": "planned",
            }
        ],
    )


def _create_record(
    resources: list[dict[str, Any]], *, manifest_version: int = 5
) -> PlanRecord:
    return PlanRecord.create(
        plan_id=str(uuid4()),
        execution_contract="execution-contract-1",
        mutation_contract="atomic-single-file",
        manifest={
            "path": "luwu.toml",
            "root": ".",
            "version": manifest_version,
            "digest": "sha256:" + "0" * 64,
        },
        resources=cast(list[Mapping[str, Any]], resources),
    )


def _resource(
    ordinal: int,
    name: str,
    *,
    condition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    condition = condition or _condition()
    return {
        "ordinal": ordinal,
        "name": name,
        "operation": "replace",
        "paths": [
            {
                "role": "source",
                "path": f"sources/{name}.conf",
                "operation": "observe",
                "precondition": copy.deepcopy(condition),
                "postcondition": copy.deepcopy(condition),
                "state": "planned",
            },
            {
                "role": "target",
                "path": f"targets/{name}.conf",
                "operation": "atomic_replace",
                "precondition": copy.deepcopy(condition),
                "postcondition": copy.deepcopy(condition),
                "state": "planned",
            },
        ],
        "state": "planned",
    }


def _condition(
    *,
    type: Any = "regular",
    mode: Any = 0o644,
    size: Any = 0,
    mtime_ns: Any = 0,
    file_id: Any = 0,
) -> dict[str, Any]:
    return {
        "type": type,
        "mode": mode,
        "size": size,
        "mtime_ns": mtime_ns,
        "file_id": file_id,
    }


def _invalid_documents() -> dict[str, dict[str, Any]]:
    base = _create_record([_resource(0, "settings")]).to_dict()

    empty = copy.deepcopy(base)
    empty["resources"] = []
    empty["next_ordinal"] = 0

    gap = copy.deepcopy(base)
    gap["resources"][0]["ordinal"] = 1
    gap["next_ordinal"] = 2

    duplicate = copy.deepcopy(base)
    second = copy.deepcopy(duplicate["resources"][0])
    second["name"] = "other"
    second["paths"][0]["path"] = "sources/other.conf"
    second["paths"][1]["path"] = "targets/other.conf"
    duplicate["resources"].append(second)
    duplicate["next_ordinal"] = 2

    wrong_next = copy.deepcopy(base)
    wrong_next["next_ordinal"] = 2

    unknown_condition = copy.deepcopy(base)
    unknown_condition["resources"][0]["paths"][0]["precondition"]["type"] = "directory"

    unhashable_condition = copy.deepcopy(base)
    unhashable_condition["resources"][0]["paths"][0]["precondition"]["type"] = {
        "name": "regular"
    }

    return {
        "empty resources": empty,
        "ordinal gap": gap,
        "duplicate ordinal": duplicate,
        "wrong next ordinal": wrong_next,
        "unknown condition type": unknown_condition,
        "unhashable condition type": unhashable_condition,
    }


def _invalid_execution_mutations() -> dict[str, Callable[[dict[str, Any]], None]]:
    def empty(document: dict[str, Any]) -> None:
        document["resources"] = []
        document["next_ordinal"] = 0

    def gap(document: dict[str, Any]) -> None:
        document["resources"][0]["ordinal"] = 1
        document["next_ordinal"] = 2

    def wrong_next(document: dict[str, Any]) -> None:
        document["next_ordinal"] = 2

    def unknown_condition(document: dict[str, Any]) -> None:
        document["resources"][0]["paths"][0]["precondition"]["type"] = "directory"

    def unhashable_condition(document: dict[str, Any]) -> None:
        document["resources"][0]["paths"][0]["precondition"]["type"] = ["regular"]

    def boolean_mode(document: dict[str, Any]) -> None:
        document["resources"][0]["paths"][0]["precondition"]["mode"] = True

    def nonzero_missing_condition(document: dict[str, Any]) -> None:
        document["resources"][0]["paths"][0]["precondition"].update(
            {"type": "missing", "size": 1}
        )

    return {
        "empty resources": empty,
        "ordinal gap": gap,
        "wrong next ordinal": wrong_next,
        "unknown condition type": unknown_condition,
        "unhashable condition type": unhashable_condition,
        "boolean mode": boolean_mode,
        "nonzero missing condition": nonzero_missing_condition,
    }


class RecordSchemaV5Tests(unittest.TestCase):
    def test_record_is_closed_metadata_only_and_transitionable(self) -> None:
        record = _record()
        payload = record.to_dict()
        self.assertEqual(payload["policy"]["rollback"], "never")
        self.assertNotIn("values", json.dumps(payload))
        committed = (
            record.transition("preflighted")
            .transition_path(0, "preflighted")
            .transition("commit_intent")
            .transition_path(0, "commit_intent")
            .transition_path(0, "committed")
            .transition("committed")
        )
        self.assertEqual(committed.to_dict()["state"], "committed")

    def test_unknown_schema_sensitive_content_and_invalid_transition_are_rejected(
        self,
    ) -> None:
        with self.assertRaises(PlanRecordError):
            PlanRecord({"secret": "value"})

        payload = _record().to_dict()
        payload["unknown"] = True
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

        payload = _record().to_dict()
        payload["events"] = [
            {
                "scope": "plan",
                "ordinal": None,
                "path": None,
                "from_state": "planned",
                "to_state": "preflighted",
                "values": {"secret": "x"},
            }
        ]
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

        with self.assertRaises(PlanRecordError):
            _record().transition("committed")

    def test_state_machine_exposes_unknown_recovery_and_not_attempted(self) -> None:
        record = _record()
        unknown = (
            record.transition("preflighted")
            .transition_path(0, "preflighted")
            .transition_path(0, "unknown")
            .transition("unknown")
        )
        self.assertEqual(unknown.to_dict()["state"], "unknown")
        self.assertEqual(
            record.transition("preflighted")
            .transition_path(0, "preflighted")
            .transition_path(0, "unknown")
            .transition("recovery_required")
            .to_dict()["state"],
            "recovery_required",
        )
        self.assertEqual(
            record.transition("preflighted")
            .transition_path(0, "preflighted")
            .transition_path(0, "not-attempted")
            .transition("unknown")
            .to_dict()["resources"][0]["state"],
            "not-attempted",
        )
        self.assertEqual(
            record.transition("preflighted")
            .transition_path(0, "preflighted")
            .transition("commit_intent")
            .transition_path(0, "commit_intent")
            .transition_path(0, "unchanged")
            .transition("unchanged")
            .to_dict()["state"],
            "unchanged",
        )
        with self.assertRaises(PlanRecordError):
            record.transition("preflighted").transition("committed")

    def test_path_state_transition_and_duplicate_paths_are_checked(self) -> None:
        record = _record()
        updated = record.transition("preflighted").transition_path(0, "preflighted")
        self.assertEqual(
            updated.to_dict()["resources"][0]["paths"][0]["state"], "preflighted"
        )
        with self.assertRaises(PlanRecordError):
            updated.transition_path(0, "planned")

        payload = _record().to_dict()
        payload["resources"][0]["paths"].append(
            payload["resources"][0]["paths"][0].copy()
        )
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

    def test_create_does_not_retain_mutable_input_objects(self) -> None:
        manifest = {
            "path": "luwu.toml",
            "root": ".",
            "version": 4,
            "digest": "sha256:" + "0" * 64,
        }
        resources: list[dict[str, Any]] = [
            {
                "ordinal": 0,
                "name": "settings",
                "operation": "reverse-sync",
                "paths": [
                    {
                        "role": "source",
                        "path": "templates/settings.json.j2",
                        "operation": "write_source_input",
                        "precondition": {
                            "type": "regular",
                            "mode": 420,
                            "size": 0,
                            "mtime_ns": 0,
                            "file_id": 0,
                        },
                        "postcondition": {
                            "type": "regular",
                            "mode": 420,
                            "size": 0,
                            "mtime_ns": 0,
                            "file_id": 0,
                        },
                        "state": "planned",
                    }
                ],
                "state": "planned",
            }
        ]
        record = PlanRecord.create(
            plan_id=str(uuid4()),
            execution_contract="execution-contract-1",
            mutation_contract="mutation-contract-1",
            manifest=manifest,
            resources=cast(list[Mapping[str, Any]], resources),
        )

        manifest["digest"] = "sha256:" + "1" * 64
        resources[0]["paths"][0]["precondition"]["size"] = 99

        document = record.to_dict()
        self.assertEqual(document["manifest"]["digest"], "sha256:" + "0" * 64)
        self.assertEqual(
            document["resources"][0]["paths"][0]["precondition"]["size"], 0
        )

    def test_rejects_committed_plan_with_unknown_resource(self) -> None:
        payload = _record().to_dict()
        payload["state"] = "committed"
        payload["resources"][0]["state"] = "unknown"
        payload["resources"][0]["paths"][0]["state"] = "unknown"
        payload["events"] = [
            {
                "scope": "plan",
                "ordinal": None,
                "path": None,
                "from_state": "planned",
                "to_state": "preflighted",
            },
            {
                "scope": "plan",
                "ordinal": None,
                "path": None,
                "from_state": "preflighted",
                "to_state": "commit_intent",
            },
            {
                "scope": "plan",
                "ordinal": None,
                "path": None,
                "from_state": "commit_intent",
                "to_state": "committed",
            },
            {
                "scope": "path",
                "ordinal": 0,
                "path": "templates/settings.json.j2",
                "from_state": "planned",
                "to_state": "preflighted",
            },
            {
                "scope": "path",
                "ordinal": 0,
                "path": "templates/settings.json.j2",
                "from_state": "preflighted",
                "to_state": "commit_intent",
            },
            {
                "scope": "path",
                "ordinal": 0,
                "path": "templates/settings.json.j2",
                "from_state": "commit_intent",
                "to_state": "unknown",
            },
        ]
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

    def test_rejects_metadata_schema_escape_without_keyword_blacklist(self) -> None:
        payload = _record().to_dict()
        payload["resources"][0]["paths"][0]["precondition"]["extra"] = "rendered"
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

        payload = _record().to_dict()
        payload["manifest"]["digest"] = "not-a-digest"
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

    def test_rejects_state_that_cannot_be_derived_from_event_history(self) -> None:
        payload = _record().to_dict()
        payload["state"] = "committed"
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

        payload = _record().to_dict()
        payload["events"] = [
            {
                "scope": "plan",
                "ordinal": None,
                "path": None,
                "from_state": "planned",
                "to_state": "preflighted",
            }
        ]
        payload["state"] = "committed"
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

    def test_corrupt_record_does_not_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(PlanRecordError):
                PlanRecord.read(path)

    def test_duplicate_json_keys_do_not_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            raw = json.dumps(_record().to_dict(), sort_keys=True, indent=2)
            raw = raw.replace(
                '\n  "state": "planned"\n',
                '\n  "state": "planned",\n  "state": "planned"\n',
                1,
            )
            path.write_text(raw, encoding="utf-8")

            with self.assertRaises(PlanRecordError) as context:
                PlanRecord.read(path)

            self.assertEqual(context.exception.code, "plan_record_corrupt")

    def test_execution_inspection_rejects_non_v5_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            _record().write(path)

            with self.assertRaises(PlanRecordError) as context:
                inspect_execution_record(path)

            self.assertEqual(context.exception.code, "execution_record_contract")


class RecordSchemaClosureV5Tests(unittest.TestCase):
    def test_create_rejects_empty_and_non_contiguous_resources(self) -> None:
        with self.assertRaises(PlanRecordError):
            _create_record([])

        gap = [_resource(1, "settings")]
        with self.assertRaises(PlanRecordError):
            _create_record(gap)

        duplicate = [_resource(0, "settings"), _resource(0, "other")]
        with self.assertRaises(PlanRecordError):
            _create_record(duplicate)

        record = _create_record([_resource(0, "settings"), _resource(1, "other")])
        self.assertEqual(record.to_dict()["next_ordinal"], 2)

    def test_condition_domain_accepts_signed_mtime_and_zero_wildcards(self) -> None:
        regular = _condition(type="regular", mtime_ns=-17, file_id=23)
        symlink = _condition(type="symlink", mtime_ns=0, file_id=0)

        record = _create_record([_resource(0, "regular", condition=regular)])
        updated = record.update_path_condition(
            0,
            "sources/regular.conf",
            postcondition=symlink,
        )

        self.assertEqual(
            updated.to_dict()["resources"][0]["paths"][0]["postcondition"], symlink
        )

    def test_condition_domain_rejects_unknown_types_ranges_and_nonzero_missing_values(
        self,
    ) -> None:
        invalid_conditions = (
            ("unknown type", _condition(type="directory")),
            ("non-string type", _condition(type=[])),
            ("unhashable mapping type", _condition(type={"name": "regular"})),
            ("boolean mode", _condition(mode=True)),
            ("mode outside permission bits", _condition(mode=0o1000)),
            ("boolean size", _condition(size=False)),
            ("negative size", _condition(size=-1)),
            ("boolean mtime", _condition(mtime_ns=True)),
            ("negative file id", _condition(file_id=-1)),
            ("nonzero missing", _condition(type="missing", size=1)),
            ("nonzero unsafe", _condition(type="unsafe", mtime_ns=-1)),
        )

        for label, condition in invalid_conditions:
            with self.subTest(label=label):
                with self.assertRaises(PlanRecordError):
                    _create_record([_resource(0, "settings", condition=condition)])

                record = _create_record([_resource(0, "settings")])
                with self.assertRaises(PlanRecordError):
                    record.update_path_condition(
                        0,
                        "sources/settings.conf",
                        precondition=condition,
                    )

    def test_missing_and_unsafe_conditions_require_all_numeric_fields_to_be_zero(
        self,
    ) -> None:
        numeric_fields = ("mode", "size", "mtime_ns", "file_id")
        for condition_type in ("missing", "unsafe"):
            with self.subTest(condition_type=condition_type):
                zeroed = _condition(type=condition_type, mode=0)
                _create_record([_resource(0, "settings", condition=zeroed)])
                for field in numeric_fields:
                    with self.subTest(field=field):
                        invalid = dict(zeroed)
                        invalid[field] = 1
                        with self.assertRaises(PlanRecordError):
                            _create_record(
                                [_resource(0, "settings", condition=invalid)]
                            )

    def test_all_record_integer_fields_reject_boolean_values(self) -> None:
        base = _create_record([_resource(0, "settings")]).to_dict()
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "record schema version": lambda document: document.__setitem__(
                "record_schema_version", True
            ),
            "manifest version": lambda document: document["manifest"].__setitem__(
                "version", True
            ),
            "resource ordinal": lambda document: document["resources"][0].__setitem__(
                "ordinal", True
            ),
            "next ordinal": lambda document: document.__setitem__("next_ordinal", True),
            "condition mode": lambda document: document["resources"][0]["paths"][0][
                "precondition"
            ].__setitem__("mode", True),
            "condition size": lambda document: document["resources"][0]["paths"][0][
                "precondition"
            ].__setitem__("size", True),
            "condition mtime": lambda document: document["resources"][0]["paths"][0][
                "precondition"
            ].__setitem__("mtime_ns", True),
            "condition file id": lambda document: document["resources"][0]["paths"][0][
                "precondition"
            ].__setitem__("file_id", True),
        }

        for label, mutate in mutations.items():
            with self.subTest(label=label):
                payload = copy.deepcopy(base)
                mutate(payload)
                with self.assertRaises(PlanRecordError):
                    PlanRecord.from_dict(payload)

    def test_from_dict_read_and_execution_inspection_share_schema_rejection(
        self,
    ) -> None:
        invalid_documents = _invalid_documents()

        for label, payload in invalid_documents.items():
            with (
                self.subTest(api="from_dict", case=label),
                self.assertRaises(PlanRecordError),
            ):
                PlanRecord.from_dict(copy.deepcopy(payload))

            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "record.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with (
                    self.subTest(api="read", case=label),
                    self.assertRaises(PlanRecordError),
                ):
                    PlanRecord.read(path)

        with ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
            record_path = project.root / "journal.json"
            execute_execution_plan(
                build_plan(project.manifest), record_path, confirm=True
            )
            valid_v5 = json.loads(record_path.read_text(encoding="utf-8"))
            inspected = inspect_execution_record(record_path)
            self.assertEqual(inspected, valid_v5)

            for label, mutate in _invalid_execution_mutations().items():
                payload = copy.deepcopy(valid_v5)
                mutate(payload)
                record_path.write_text(json.dumps(payload), encoding="utf-8")
                with (
                    self.subTest(api="inspect_execution_record", case=label),
                    self.assertRaises(PlanRecordError),
                ):
                    inspect_execution_record(record_path)

    def test_v1_to_v4_records_remain_readable_but_are_not_v5_execution_journals(
        self,
    ) -> None:
        for version in range(1, 5):
            with self.subTest(version=version):
                record = _create_record(
                    [_resource(0, "settings")], manifest_version=version
                )
                self.assertEqual(
                    PlanRecord.from_dict(record.to_dict()).to_dict(), record.to_dict()
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "legacy.json"
                    record.write(path)
                    with self.assertRaises(PlanRecordError) as context:
                        inspect_execution_record(path)
                    self.assertEqual(
                        context.exception.code, "execution_record_contract"
                    )
