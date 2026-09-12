from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from luwu.plan_record import PlanRecord, PlanRecordError


class PlanRecordTests(unittest.TestCase):
    def test_record_is_closed_metadata_only_and_transitionable(self) -> None:
        record = _record()
        payload = record.to_dict()
        self.assertEqual(payload["policy"]["rollback"], "never")
        self.assertNotIn("values", json.dumps(payload))
        committed = record.transition("locked").transition("preflighted")
        self.assertEqual(committed.to_dict()["state"], "preflighted")

    def test_unknown_schema_sensitive_content_and_invalid_transition_are_rejected(
        self,
    ) -> None:
        payload = _record().to_dict()
        payload["unknown"] = True
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

        payload = _record().to_dict()
        payload["events"] = [{"values": {"secret": "x"}}]
        with self.assertRaises(PlanRecordError):
            PlanRecord.from_dict(payload)

        with self.assertRaises(PlanRecordError):
            _record().transition("committed")

    def test_path_state_transition_and_duplicate_paths_are_checked(self) -> None:
        record = _record()
        updated = record.transition_path(0, "prepared")
        self.assertEqual(
            updated.to_dict()["resources"][0]["paths"][0]["state"], "prepared"
        )
        with self.assertRaises(PlanRecordError):
            updated.transition_path(0, "pending")

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
            self.assertEqual(list(Path(directory).glob(".*.luwu-*")), [])

    def test_corrupt_record_does_not_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(PlanRecordError):
                PlanRecord.read(path)


def _record() -> PlanRecord:
    return PlanRecord.create(
        plan_id=str(uuid4()),
        execution_contract="execution-contract-1",
        mutation_contract="mutation-contract-1",
        manifest={
            "path": "luwu.toml",
            "root": ".",
            "version": 4,
            "digest": "sha256:public-fingerprint",
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
                        "precondition": {"type": "regular", "fingerprint": "public"},
                        "postcondition": {"type": "regular", "fingerprint": "public"},
                        "state": "pending",
                    }
                ],
                "state": "pending",
            }
        ],
    )


if __name__ == "__main__":
    unittest.main()
