from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from luwu.plan_record import PlanRecord, PlanRecordError
from luwu.reconcile import inspect_execution_record


class PlanRecordTests(unittest.TestCase):
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

    def test_atomic_write_and_read(self) -> None:
        record = _record()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            record.write(path)
            self.assertEqual(PlanRecord.read(path).to_dict(), record.to_dict())
            self.assertEqual(
                list(Path(directory).glob(".*.luwu-*")),
                [Path(directory) / ".plan.json.luwu-lock"],
            )

    def test_create_is_exclusive_and_update_uses_expected_snapshot(self) -> None:
        record = _record()
        updated = record.transition("preflighted")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            record.write(path)

            with self.assertRaises(PlanRecordError) as conflict:
                _record().write(path)
            self.assertEqual(conflict.exception.code, "plan_record_conflict")
            self.assertEqual(PlanRecord.read(path).to_dict(), record.to_dict())

            updated.write(path, expected=record)
            with self.assertRaises(PlanRecordError) as stale:
                record.write(path, expected=record)
            self.assertEqual(stale.exception.code, "plan_record_cas")
            self.assertEqual(PlanRecord.read(path).to_dict(), updated.to_dict())

    def test_parent_rebind_after_replace_is_durability_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            with (
                patch(
                    "luwu.plan_record._parent_identity_matches",
                    side_effect=[True, True, False],
                ),
                self.assertRaises(PlanRecordError) as raised,
            ):
                _record().write(path)
            self.assertEqual(raised.exception.code, "plan_record_durability_unknown")
            self.assertTrue(raised.exception.committed)

    def test_temporary_cleanup_failure_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            with (
                patch("luwu.plan_record.os.unlink", side_effect=OSError("cleanup")),
                self.assertRaises(PlanRecordError) as raised,
            ):
                _record().write(path)
            self.assertEqual(raised.exception.code, "plan_record_durability_unknown")
            self.assertTrue(raised.exception.committed)

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

    def test_rejects_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            (root / "link").symlink_to(real, target_is_directory=True)
            with self.assertRaises(PlanRecordError):
                _record().write(root / "link" / "plan.json")

    def test_directory_fsync_failure_exposes_commit_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            with (
                patch(
                    "luwu.plan_record.os.fsync", side_effect=[None, OSError("fsync")]
                ),
                self.assertRaises(PlanRecordError) as raised,
            ):
                _record().write(path)
            self.assertTrue(raised.exception.committed)
            self.assertFalse(raised.exception.durability_confirmed)
            self.assertEqual(raised.exception.code, "plan_record_durability_unknown")

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


if __name__ == "__main__":
    unittest.main()
