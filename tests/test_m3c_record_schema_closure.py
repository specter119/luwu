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
from tests.test_m3c_execution import _ExecutionProject


class M3cRecordSchemaClosureTests(unittest.TestCase):
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

        with _ExecutionProject(("alpha",), targets={"alpha": "alpha-old\n"}) as project:
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


if __name__ == "__main__":
    unittest.main()
