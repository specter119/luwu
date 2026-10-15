"""Tests for record durability."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from luwu.plan_record import PlanRecord, PlanRecordError


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


class RecordDurabilityTests(unittest.TestCase):
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

    def test_replace_then_raise_reports_journal_publication_uncertainty(self) -> None:
        record = _record()
        updated = record.transition("preflighted")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            record.write(path)
            real_replace = os.replace

            def replace_then_raise(
                source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                src_dir_fd: int | None = None,
                dst_dir_fd: int | None = None,
            ) -> None:
                real_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                raise OSError("publication boundary")

            with (
                patch("luwu.plan_record.os.replace", side_effect=replace_then_raise),
                patch("luwu.plan_record.ensure_supported"),
                self.assertRaises(PlanRecordError) as raised,
            ):
                updated.write(path, expected=record)

            self.assertEqual(raised.exception.code, "plan_record_durability_unknown")
            self.assertTrue(raised.exception.committed)
            self.assertFalse(raised.exception.durability_confirmed)
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
                patch("luwu.plan_record.ensure_supported"),
                self.assertRaises(PlanRecordError) as raised,
            ):
                _record().write(path)
            self.assertEqual(raised.exception.code, "plan_record_durability_unknown")
            self.assertTrue(raised.exception.committed)

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
