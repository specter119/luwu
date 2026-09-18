from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from luwu.errors import PlatformError
from luwu.plan_record import SecretPlanRecord, SecretPlanRecordError


def _record() -> SecretPlanRecord:
    return SecretPlanRecord.create(
        plan_id=str(uuid4()),
        execution_contract="provider-secret-whole-file-v6",
        mutation_contract="atomic-single-secret-target",
        manifest={
            "path": "/tmp/manifest/luwu.toml",
            "root": "/tmp/manifest",
            "version": 6,
        },
        resources=[
            {
                "ordinal": 0,
                "label": "resource-0",
                "operation": "create",
                "target_state": "missing",
                "state": "planned",
            }
        ],
    )


class M4PlanRecordTests(unittest.TestCase):
    def test_record_is_closed_and_contains_no_content_condition(self) -> None:
        record = _record()
        document = record.to_dict()
        self.assertNotIn("digest", json.dumps(document))
        self.assertNotIn("size", json.dumps(document))
        self.assertNotIn("provider_reference", json.dumps(document))
        self.assertNotIn("item", json.dumps(document))
        self.assertNotIn("field", json.dumps(document))
        self.assertNotIn("path", document["resources"][0])

        changed = record.transition("preflighted").transition_path(0, "preflighted")
        changed = changed.transition("commit_intent").transition_path(
            0, "commit_intent"
        )
        changed = changed.transition_path(0, "committed").transition("committed")
        self.assertEqual(changed.to_dict()["state"], "committed")

    def test_record_writes_owner_only_and_supports_compare_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            record = _record()
            record.write(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = SecretPlanRecord.read(path)
            self.assertEqual(loaded.to_dict(), record.to_dict())

            updated = record.transition("preflighted")
            updated.write(path, expected=record)
            self.assertEqual(SecretPlanRecord.read(path).to_dict(), updated.to_dict())
            with self.assertRaises(SecretPlanRecordError) as raised:
                record.write(path, expected=record)
            self.assertEqual(raised.exception.code, "plan_record_cas")

    def test_create_does_not_retain_mutable_input_objects(self) -> None:
        manifest = {
            "path": "/tmp/manifest/luwu.toml",
            "root": "/tmp/manifest",
            "version": 6,
        }
        resources: list[dict[str, Any]] = [
            {
                "ordinal": 0,
                "label": "resource-0",
                "operation": "create",
                "target_state": "missing",
                "state": "planned",
            }
        ]
        record = SecretPlanRecord.create(
            plan_id=str(uuid4()),
            execution_contract="provider-secret-whole-file-v6",
            mutation_contract="atomic-single-secret-target",
            manifest=manifest,
            resources=cast(list[Mapping[str, Any]], resources),
        )

        manifest["path"] = "/tmp/changed/luwu.toml"
        resources[0]["label"] = "changed"

        document = record.to_dict()
        self.assertEqual(document["manifest"]["path"], "/tmp/manifest/luwu.toml")
        self.assertEqual(document["resources"][0]["label"], "resource-0")

    def test_non_owner_only_record_is_not_read_or_updated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            record = _record()
            record.write(path)
            os.chmod(path, 0o644)
            with self.assertRaises(SecretPlanRecordError):
                SecretPlanRecord.read(path)
            with self.assertRaises(SecretPlanRecordError):
                record.write(path)

    def test_record_library_fails_closed_when_platform_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            record = _record()
            with patch(
                "luwu.plan_record.ensure_supported",
                side_effect=PlatformError(),
            ):
                with self.assertRaises(PlatformError) as read_error:
                    SecretPlanRecord.read(path)
                with self.assertRaises(PlatformError) as write_error:
                    record.write(path)

            self.assertEqual(read_error.exception.code, "platform_unsupported")
            self.assertEqual(write_error.exception.code, "platform_unsupported")
            self.assertFalse(path.exists())

    def test_unknown_fields_and_content_metadata_are_rejected(self) -> None:
        document = _record().to_dict()
        document["unexpected"] = "sentinel"
        with self.assertRaises(SecretPlanRecordError):
            SecretPlanRecord.from_dict(document)

        document = _record().to_dict()
        document["resources"][0]["size"] = 12
        with self.assertRaises(SecretPlanRecordError):
            SecretPlanRecord.from_dict(document)

    def test_target_condition_update_is_a_non_persisting_compatibility_noop(
        self,
    ) -> None:
        record = _record()
        updated = record.update_path_condition(
            0,
            "/possibly-sensitive/target",
            postcondition={"size": 123, "value": "secret"},
        )
        self.assertEqual(updated.to_dict(), record.to_dict())


if __name__ == "__main__":
    unittest.main()
