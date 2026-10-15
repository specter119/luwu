"""Tests for batch execution."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Self, cast
from unittest.mock import patch

from luwu import reconcile
from luwu.errors import ApplyError
from luwu.manifest import load_manifest
from luwu.reconcile import (
    Plan,
    ResourceObservation,
    apply_plan,
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
)
from tests.support.projects import ExecutionProject
from tests.support.snapshots import project_bytes


class _LegacyProject:
    def __init__(self, version: int) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.source = self.root / "templates/settings.json.j2"
        self.target = self.root / "live/settings.json"
        self.manifest_path = self.root / "luwu.toml"
        self.source.write_text('{"value": 1}\n')
        self.target.write_text('{"value": 1}\n')
        if version == 1:
            resource = """source = "templates/settings.json.j2"
target = "live/settings.json"
owner = "source"
scope = "whole-file"
"""
        elif version == 2:
            resource = """kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "source"
scope = "whole-file"
"""
        else:
            field_owner = "source" if version == 3 else "live"
            reverse_sync = (
                "\n[resources.settings.reverse_sync]\n"
                'format = "literal-json"\n\n'
                "[resources.settings.reverse_sync.fields]\n"
                'value = "value"\n'
                if version == 4
                else ""
            )
            resource = f'''kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
baseline = "baseline.json"

[resources.settings.fields]
value = "{field_owner}"
{reverse_sync}'''
            (self.root / "baseline.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "resource": "settings",
                        "source": "templates/settings.json.j2",
                        "target": "live/settings.json",
                        "owners": {"value": field_owner},
                        "values": {"value": 1},
                    }
                )
            )
        self.manifest_path.write_text(
            f"version = {version}\n\n[resources.settings]\n{resource}"
        )
        self.manifest = load_manifest(self.manifest_path)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.directory.cleanup()


class BatchExecutionV5Tests(unittest.TestCase):
    def test_preview_is_side_effect_free_and_metadata_only(self) -> None:
        with ExecutionProject(
            ("alpha", "zeta"),
            targets={"alpha": "alpha-old\n", "zeta": "zeta-desired\n"},
        ) as project:
            plan = build_plan(project.manifest)
            before = project_bytes(project)
            record_path = project.root / "journal.json"

            result = execute_execution_plan(plan, record_path, confirm=False)

            self.assertIsNone(result.record)
            self.assertEqual(result.record_state, None)
            self.assertEqual(
                [
                    resource["name"]
                    for resource in cast(
                        list[dict[str, object]], result.preview["resources"]
                    )
                ],
                ["alpha", "zeta"],
            )
            self.assertEqual(project_bytes(project), before)
            self.assertFalse(record_path.exists())

    def test_execution_uses_stable_order_and_records_committed_and_unchanged(
        self,
    ) -> None:
        with ExecutionProject(
            ("zeta", "alpha"),
            targets={"alpha": None, "zeta": "zeta-desired\n"},
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"

            with patch(
                "luwu.reconcile._write_observation",
                wraps=reconcile._write_observation,
            ) as writer:
                result = execute_execution_plan(plan, record_path, confirm=True)

            self.assertEqual(
                [call.args[1].resource.name for call in writer.call_args_list],
                ["alpha"],
            )
            self.assertEqual(result.changed_targets, ("targets/alpha.conf",))
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["zeta"].read_text(), "zeta-desired\n")

            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "committed")
            self.assertEqual(
                [
                    (resource["name"], resource["state"])
                    for resource in cast(list[dict[str, object]], journal["resources"])
                ],
                [("alpha", "committed"), ("zeta", "unchanged")],
            )
            self.assertEqual(
                [
                    (event["ordinal"], event["to_state"])
                    for event in cast(list[dict[str, object]], journal["events"])
                    if event["scope"] == "path"
                    and str(event["path"]).startswith("targets/")
                    and event["to_state"] in {"committed", "unchanged"}
                ],
                [(0, "committed"), (1, "unchanged")],
            )

    def test_failed_full_preflight_writes_no_target_or_journal(self) -> None:
        with ExecutionProject(
            ("alpha", "zeta"),
            targets={"alpha": "alpha-old\n", "zeta": "zeta-old\n"},
        ) as project:
            plan = build_plan(project.manifest)
            project.sources["zeta"].write_text("zeta-changed-after-plan\n")
            before = project_bytes(project)
            record_path = project.root / "journal.json"

            with self.assertRaises(ApplyError) as context:
                execute_execution_plan(plan, record_path, confirm=True)

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(project_bytes(project), before)
            self.assertFalse(record_path.exists())

    def test_replace_durability_failure_marks_unknown_and_stops_without_rollback(
        self,
    ) -> None:
        with ExecutionProject(
            ("zeta", "beta", "alpha"),
            targets={
                "alpha": "alpha-old\n",
                "beta": "beta-old\n",
                "zeta": "zeta-old\n",
            },
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"

            with (
                patch(
                    "luwu.reconcile.sync_directory",
                    side_effect=[None, OSError("directory fsync unavailable")],
                ),
                self.assertRaises(ApplyError) as context,
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            self.assertEqual(context.exception.code, "recovery_required")
            self.assertIn("rollback=never", str(context.exception))
            self.assertEqual(project.targets["alpha"].read_text(), "alpha-desired\n")
            self.assertEqual(project.targets["beta"].read_text(), "beta-desired\n")
            self.assertEqual(project.targets["zeta"].read_text(), "zeta-old\n")

            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "recovery_required")
            self.assertEqual(
                [
                    (resource["name"], resource["state"])
                    for resource in cast(list[dict[str, object]], journal["resources"])
                ],
                [
                    ("alpha", "committed"),
                    ("beta", "unknown"),
                    ("zeta", "not-attempted"),
                ],
            )

    def test_prior_committed_resource_upgrades_later_failure_to_recovery_required(
        self,
    ) -> None:
        with ExecutionProject(
            ("alpha", "beta", "zeta"),
            targets={
                "alpha": "alpha-old\n",
                "beta": "beta-old\n",
                "zeta": "zeta-old\n",
            },
        ) as project:
            plan = build_plan(project.manifest)
            record_path = project.root / "journal.json"
            failure = ApplyError(
                "writer failed before replacement",
                code="apply_failed",
                committed=False,
                target_name="targets/beta.conf",
            )

            calls = 0
            real_writer = reconcile._write_observation

            def write_with_failure(
                current_plan: Plan,
                observation: ResourceObservation,
                *,
                execution: bool = False,
            ) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_writer(current_plan, observation, execution=execution)
                raise failure

            with (
                patch(
                    "luwu.reconcile._write_observation",
                    side_effect=write_with_failure,
                ),
                self.assertRaises(ApplyError) as context,
            ):
                execute_execution_plan(plan, record_path, confirm=True)

            self.assertEqual(context.exception.code, "recovery_required")
            self.assertTrue(context.exception.committed)
            journal = inspect_execution_record(record_path)
            self.assertEqual(journal["state"], "recovery_required")
            self.assertEqual(
                [
                    (resource["name"], resource["state"])
                    for resource in cast(list[dict[str, object]], journal["resources"])
                ],
                [
                    ("alpha", "committed"),
                    ("beta", "unknown"),
                    ("zeta", "not-attempted"),
                ],
            )

    def test_symbolic_resource_creates_only_the_declared_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sources").mkdir()
            (root / "targets").mkdir()
            source = root / "sources/settings.conf"
            target = root / "targets/settings.conf"
            source.write_text("public symbolic input\n")
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                """version = 5

[resources.settings]
kind = "symbolic"
source = "sources/settings.conf"
target = "targets/settings.conf"
owner = "source"
scope = "whole-file"
content_sensitivity = "public"
"""
            )
            record_path = root / "journal.json"

            result = execute_execution_plan(
                build_plan(load_manifest(manifest_path)), record_path, confirm=True
            )

            self.assertEqual(result.record_state, "committed")
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), source.resolve())
            self.assertEqual(
                inspect_execution_record(record_path)["state"], "committed"
            )

    def test_execution_rejects_manual_tampered_and_v1_to_v4_plans(self) -> None:
        with ExecutionProject(
            ("alpha", "zeta"), targets={"alpha": None, "zeta": None}
        ) as project:
            plan = build_plan(project.manifest)
            with self.assertRaises(ApplyError) as ordinary_apply:
                apply_plan(plan)
            self.assertEqual(ordinary_apply.exception.code, "execution_required")
            candidates = {
                "manual": Plan(manifest=plan.manifest, observations=plan.observations),
                "tampered": replace(
                    plan, observations=tuple(reversed(plan.observations))
                ),
            }
            for label, candidate in candidates.items():
                with self.subTest(plan=label):
                    record_path = project.root / f"{label}.json"
                    with self.assertRaises(ApplyError) as context:
                        execute_execution_plan(candidate, record_path, confirm=True)
                    self.assertEqual(context.exception.code, "invalid_plan")
                    self.assertFalse(record_path.exists())

        for version in range(1, 5):
            with self.subTest(version=version), _LegacyProject(version) as project:
                before = project.target.read_bytes()
                record_path = project.root / "journal.json"
                with self.assertRaises(ApplyError) as context:
                    execute_execution_plan(
                        build_plan(project.manifest), record_path, confirm=True
                    )
                self.assertEqual(context.exception.code, "invalid_plan")
                self.assertEqual(project.target.read_bytes(), before)
                self.assertFalse(record_path.exists())
