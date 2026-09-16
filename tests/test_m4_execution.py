from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any, Self, cast

from luwu.errors import ApplyError
from luwu.manifest import load_manifest
from luwu.providers import ProviderAuthority
from luwu.reconcile import (
    Action,
    Status,
    build_plan,
    execute_execution_plan,
    inspect_execution_record,
    plan_to_dict,
    reobserve_execution_record,
)

SENTINEL = "m4-secret-sentinel"


class _Resolver:
    def __init__(self, value: str = SENTINEL) -> None:
        self.calls = 0
        self.value = value

    def resolve(self, reference: object, *, authority: ProviderAuthority) -> str:
        del reference, authority
        self.calls += 1
        return self.value


class M4ExecutionTests(unittest.TestCase):
    def test_missing_authority_blocks_without_starting_resolver(self) -> None:
        with _Project() as project:
            resolver = _Resolver()
            plan = build_plan(project.manifest, resolver=resolver)

            self.assertEqual(resolver.calls, 0)
            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            self.assertEqual(plan.observations[0].action, Action.BLOCK)
            serialized = json.dumps(plan_to_dict(plan, command="plan"))
            self.assertNotIn("database-prod", serialized)
            self.assertNotIn("password", serialized)
            self.assertNotIn(SENTINEL, serialized)

    def test_preview_is_side_effect_free_and_fetches_once(self) -> None:
        with _Project() as project:
            resolver = _Resolver()
            authority = ProviderAuthority(capabilities={"subprocess"})
            plan = build_plan(project.manifest, authority=authority, resolver=resolver)
            result = execute_execution_plan(
                plan,
                project.journal,
                confirm=False,
            )

            self.assertEqual(resolver.calls, 1)
            self.assertIsNone(result.record)
            self.assertFalse(project.target.exists())
            self.assertFalse(project.journal.exists())
            self.assertNotIn(SENTINEL, json.dumps(result.preview))
            self.assertNotIn("database-prod", json.dumps(result.preview))
            self.assertNotIn("password", json.dumps(result.preview))

    def test_confirmed_execution_writes_owner_only_target_without_refetch(self) -> None:
        with _Project() as project:
            resolver = _Resolver()
            authority = ProviderAuthority(capabilities={"subprocess"})
            plan = build_plan(project.manifest, authority=authority, resolver=resolver)
            result = execute_execution_plan(plan, project.journal, confirm=True)

            self.assertEqual(resolver.calls, 1)
            self.assertEqual(result.record_state, "committed")
            self.assertEqual(result.changed_targets, ("resource-0",))
            self.assertEqual(
                project.target.read_text(encoding="utf-8"), f"password={SENTINEL}\n"
            )
            self.assertEqual(stat.S_IMODE(project.target.stat().st_mode), 0o600)
            journal = cast(dict[str, Any], inspect_execution_record(project.journal))
            serialized = json.dumps(journal)
            self.assertNotIn(SENTINEL, serialized)
            self.assertNotIn("database-prod", serialized)
            self.assertNotIn("password", serialized)
            self.assertNotIn("digest", serialized)
            self.assertNotIn("size", serialized)
            self.assertEqual(journal["manifest"]["version"], 6)

    def test_execute_can_recalculate_a_blocked_plan_with_explicit_authority(
        self,
    ) -> None:
        with _Project() as project:
            resolver = _Resolver()
            blocked = build_plan(project.manifest, resolver=resolver)
            result = execute_execution_plan(
                blocked,
                project.journal,
                confirm=True,
                authority=ProviderAuthority(capabilities={"subprocess"}),
                resolver=resolver,
            )

            self.assertEqual(resolver.calls, 1)
            self.assertEqual(result.record_state, "committed")

    def test_record_inspection_and_recovery_without_authority_do_not_start_provider(
        self,
    ) -> None:
        with _Project() as project:
            resolver = _Resolver()
            authority = ProviderAuthority(capabilities={"subprocess"})
            execute_execution_plan(
                build_plan(project.manifest, authority=authority, resolver=resolver),
                project.journal,
                confirm=True,
            )
            calls_before = resolver.calls

            inspected = inspect_execution_record(project.journal)
            recovered = cast(
                dict[str, Any],
                reobserve_execution_record(project.journal, resolver=resolver),
            )

            self.assertEqual(resolver.calls, calls_before)
            self.assertEqual(inspected["state"], "committed")
            self.assertEqual(recovered["outcome"], "recovery_required")
            self.assertEqual(recovered["reason"], "capability_required")

    def test_recovery_with_authority_reports_current_convergence_not_history_proof(
        self,
    ) -> None:
        with _Project() as project:
            resolver = _Resolver()
            authority = ProviderAuthority(capabilities={"subprocess"})
            execute_execution_plan(
                build_plan(project.manifest, authority=authority, resolver=resolver),
                project.journal,
                confirm=True,
            )

            recovered = cast(
                dict[str, Any],
                reobserve_execution_record(
                    project.journal,
                    authority=authority,
                    resolver=resolver,
                ),
            )

            self.assertEqual(resolver.calls, 2)
            self.assertEqual(recovered["outcome"], "currently_converged")
            self.assertNotEqual(recovered["outcome"], "confirmed")
            self.assertEqual(
                recovered["resources"][0]["reobserved_state"],
                "currently_converged",
            )

    def test_existing_target_must_be_owner_only_and_single_link(self) -> None:
        for mode in (0o644, 0o1600):
            with self.subTest(mode=oct(mode)), _Project() as project:
                project.target.write_text("old\n", encoding="utf-8")
                os.chmod(project.target, mode)
                resolver = _Resolver()
                plan = build_plan(
                    project.manifest,
                    authority=ProviderAuthority(capabilities={"subprocess"}),
                    resolver=resolver,
                )
                self.assertEqual(resolver.calls, 0)
                self.assertEqual(plan.observations[0].status, Status.BLOCKED)

        with _Project() as project:
            hardlink = project.target.parent / "hardlink"
            project.target.write_text("old\n", encoding="utf-8")
            hardlink.hardlink_to(project.target)
            resolver = _Resolver()
            plan = build_plan(
                project.manifest,
                authority=ProviderAuthority(capabilities={"subprocess"}),
                resolver=resolver,
            )
            self.assertEqual(resolver.calls, 0)
            self.assertEqual(plan.observations[0].status, Status.BLOCKED)

    def test_target_symlink_and_missing_parent_are_blocked_before_provider(
        self,
    ) -> None:
        with _Project() as project:
            other = project.target.parent / "other"
            other.write_text("outside\n", encoding="utf-8")
            project.target.symlink_to(other)
            resolver = _Resolver()
            plan = build_plan(
                project.manifest,
                authority=ProviderAuthority(capabilities={"subprocess"}),
                resolver=resolver,
            )
            self.assertEqual(resolver.calls, 0)
            self.assertEqual(plan.observations[0].status, Status.BLOCKED)

        with _Project(missing_parent=True) as project:
            resolver = _Resolver()
            plan = build_plan(
                project.manifest,
                authority=ProviderAuthority(capabilities={"subprocess"}),
                resolver=resolver,
            )
            self.assertEqual(resolver.calls, 0)
            self.assertEqual(plan.observations[0].status, Status.BLOCKED)

    def test_manifest_declaration_change_is_stale_without_retrieving_again(
        self,
    ) -> None:
        with _Project() as project:
            resolver = _Resolver()
            authority = ProviderAuthority(capabilities={"subprocess"})
            plan = build_plan(project.manifest, authority=authority, resolver=resolver)
            project.manifest_path.write_text(
                project.manifest_path.read_text(encoding="utf-8").replace(
                    'field = "password"', 'field = "username"'
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ApplyError) as raised:
                execute_execution_plan(plan, project.journal, confirm=True)
            self.assertEqual(raised.exception.code, "stale_plan")
            self.assertEqual(resolver.calls, 1)
            self.assertFalse(project.target.exists())


class _Project:
    def __init__(self, *, missing_parent: bool = False) -> None:
        self._temporary_root: tempfile.TemporaryDirectory[str] | None = None
        self._missing_parent = missing_parent

    def __enter__(self) -> Self:
        self._temporary_root = tempfile.TemporaryDirectory()
        root = Path(self._temporary_root.name)
        self.root = root / "manifest"
        target_root = root / "secret-target"
        self.root.mkdir()
        target_root.mkdir()
        (self.root / "templates").mkdir()
        (self.root / "templates/database.conf.j2").write_text(
            "password={{ secrets.db_password }}\n",
            encoding="utf-8",
        )
        target_parent = target_root / "nested"
        if not self._missing_parent:
            target_parent.mkdir()
        self.target = target_parent / "database.conf"
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text(
            f'''version = 6
capabilities = ["subprocess"]

[resources.database]
kind = "template"
source = "templates/database.conf.j2"
target = "{self.target}"
owner = "source"
scope = "whole-file"
content_sensitivity = "secret"

[resources.database.providers.db_password]
type = "rbw"
item = "database-prod"
field = "password"
''',
            encoding="utf-8",
        )
        self.manifest = load_manifest(self.manifest_path)
        self.journal = self.root / "journal.json"
        return self

    def __exit__(self, *_exc_info: object) -> None:
        assert self._temporary_root is not None
        self._temporary_root.cleanup()


if __name__ == "__main__":
    unittest.main()
