from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Self
from unittest.mock import patch

from luwu import reconcile
from luwu.cli import main
from luwu.errors import MutationError
from luwu.manifest import load_manifest
from luwu.mutations import accept_baseline, reverse_sync
from luwu.reconcile import Status, build_plan, reobserve_execution_record
from tests.test_m3c_execution import _ExecutionProject


class M3ConflictRecoveryTests(unittest.TestCase):
    def test_reverse_sync_blocks_unselected_conflict_in_preview_and_confirm(
        self,
    ) -> None:
        with _FieldProject(
            source_setting=2,
            live_setting=3,
            source_runtime=1,
            live_runtime=2,
        ) as project:
            self._assert_conflict(project, conflict_status="conflict")

    def test_reverse_sync_blocks_unselected_wrong_direction_review(self) -> None:
        with _FieldProject(
            source_setting=1,
            live_setting=2,
            source_runtime=1,
            live_runtime=2,
        ) as project:
            self._assert_conflict(project, conflict_status="live_changed")

    def test_reverse_sync_allows_forward_and_reverse_candidates(self) -> None:
        with _FieldProject(
            source_setting=2,
            live_setting=1,
            source_runtime=1,
            live_runtime=2,
        ) as project:
            plan = build_plan(load_manifest(project.manifest))
            observation = plan.observations[0]
            self.assertEqual(observation.status, Status.DRIFTED)
            assert observation.ownership is not None
            self.assertEqual(
                {field.name: field.decision for field in observation.ownership.fields},
                {
                    "setting": "forward_candidate",
                    "runtime": "reverse_candidate",
                },
            )

            before = _snapshot(project.root)
            preview = reverse_sync(
                load_manifest(project.manifest),
                resource_name="settings",
                fields=("runtime",),
                confirm=False,
            )
            self.assertFalse(preview.applied)
            self.assertEqual(preview.outcome, "confirmation_required")
            self.assertEqual(_snapshot(project.root), before)

            result = reverse_sync(
                load_manifest(project.manifest),
                resource_name="settings",
                fields=("runtime",),
                confirm=True,
            )
            self.assertTrue(result.applied)
            self.assertEqual(result.outcome, "committed")
            self.assertEqual(
                json.loads(project.source.read_text(encoding="utf-8")),
                {"setting": 2, "runtime": 2},
            )

    def test_accept_remains_available_for_a_conflict(self) -> None:
        with _FieldProject(
            source_setting=2,
            live_setting=3,
            source_runtime=1,
            live_runtime=2,
        ) as project:
            before = _snapshot(project.root)
            preview = accept_baseline(
                load_manifest(project.manifest),
                resource_name="settings",
                value_from="desired",
                fields=("setting",),
                confirm=False,
            )
            self.assertFalse(preview.applied)
            self.assertEqual(preview.outcome, "confirmation_required")
            self.assertEqual(_snapshot(project.root), before)

            result = accept_baseline(
                load_manifest(project.manifest),
                resource_name="settings",
                value_from="desired",
                fields=("setting",),
                confirm=True,
            )
            self.assertTrue(result.applied)
            self.assertEqual(result.outcome, "committed")
            baseline = json.loads(project.baseline.read_text(encoding="utf-8"))
            self.assertEqual(baseline["values"], {"setting": 2, "runtime": 1})

    def test_reobserve_standalone_not_attempted_requires_recovery_and_writes_nothing(
        self,
    ) -> None:
        with _ExecutionProject(
            ("alpha",), targets={"alpha": "alpha-desired\n"}
        ) as project:
            plan = build_plan(project.manifest)
            self.assertEqual(plan.observations[0].status, Status.IN_SYNC)
            record = (
                reconcile._execution_record(plan)
                .transition("preflighted")
                .transition_path(0, "not-attempted")
                .transition("unknown")
            )
            record_path = project.root / "standalone-not-attempted.json"
            record.write(record_path)
            before = _snapshot(project.root)

            result = reobserve_execution_record(record_path)
            self.assertEqual(result["outcome"], "recovery_required")
            resources = result["resources"]
            assert isinstance(resources, list)
            resource = resources[0]
            assert isinstance(resource, dict)
            self.assertEqual(resource["reobserved_state"], "not-attempted")
            self.assertEqual(_snapshot(project.root), before)

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(["recover", "--record", str(record_path), "--json"])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["outcome"], "recovery_required")
            self.assertEqual(
                payload["resources"][0]["reobserved_state"], "not-attempted"
            )
            self.assertEqual(_snapshot(project.root), before)

    def _assert_conflict(self, project: _FieldProject, *, conflict_status: str) -> None:
        plan = build_plan(load_manifest(project.manifest))
        observation = plan.observations[0]
        self.assertEqual(observation.status, Status.CONFLICT)
        assert observation.ownership is not None
        fields = {field.name: field for field in observation.ownership.fields}
        self.assertEqual(fields["setting"].status, conflict_status)
        self.assertEqual(fields["runtime"].decision, "reverse_candidate")

        before = _snapshot(project.root)
        with (
            patch("luwu.mutations.build_source_patch") as build_patch,
            patch("luwu.mutations._write_source") as writer,
        ):
            for confirm in (False, True):
                with self.subTest(confirm=confirm):
                    with self.assertRaises(MutationError) as context:
                        reverse_sync(
                            load_manifest(project.manifest),
                            resource_name="settings",
                            fields=("runtime",),
                            confirm=confirm,
                        )
                    self.assertEqual(context.exception.code, "review_required")
        build_patch.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(_snapshot(project.root), before)


class _FieldProject:
    def __init__(
        self,
        *,
        source_setting: int,
        live_setting: int,
        source_runtime: int,
        live_runtime: int,
    ) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.source = self.root / "templates/settings.json.j2"
        self.target = self.root / "live/settings.json"
        self.baseline = self.root / "baseline.json"
        self.manifest = self.root / "luwu.toml"
        self.source.write_text(
            json.dumps({"setting": source_setting, "runtime": source_runtime}),
            encoding="utf-8",
        )
        self.target.write_text(
            json.dumps({"setting": live_setting, "runtime": live_runtime}),
            encoding="utf-8",
        )
        self.baseline.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "resource": "settings",
                    "source": "templates/settings.json.j2",
                    "target": "live/settings.json",
                    "owners": {"setting": "source", "runtime": "live"},
                    "values": {"setting": 1, "runtime": 1},
                }
            ),
            encoding="utf-8",
        )
        self.manifest.write_text(
            """version = 4

[resources.settings]
kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
baseline = "baseline.json"

[resources.settings.fields]
setting = "source"
runtime = "live"

[resources.settings.reverse_sync]
format = "literal-json"

[resources.settings.reverse_sync.fields]
runtime = "runtime"
""",
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.directory.cleanup()


def _snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        metadata = (
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ino,
        )
        relative = "." if path == root else str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            snapshot[relative] = ("symlink", os.readlink(path), *metadata)
        elif stat.S_ISDIR(info.st_mode):
            snapshot[relative] = ("directory", *metadata)
        elif stat.S_ISREG(info.st_mode):
            snapshot[relative] = ("file", path.read_bytes(), *metadata)
        else:
            snapshot[relative] = ("other", *metadata)
    return snapshot


if __name__ == "__main__":
    unittest.main()
