"""Engine behavior for symlink resources."""

from __future__ import annotations

import os
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.engine import apply_plan, build_plan
from tests.support.profile_case import ProfileEngineCase

_LINK_MANIFEST = """
version = 8
[roots]
source = "source"
inputs = "inputs"
target = "home"
[profiles.work]
packages = ["notes"]
exclude = []
[packages.notes]
dependencies = []
resources = ["shellrc"]
[resources.shellrc]
source = "shellrc"
target = "{target}"
parents = "{parents}"
"""


class SymlinkResourceTests(ProfileEngineCase):
    def setUp(self) -> None:
        super().setUp()
        (self.source / "shellrc").write_text("export A=1\n", encoding="utf-8")
        self.link = self.home / ".shellrc"

    def _configure(self, *, target: str = ".shellrc", parents: str = "require") -> None:
        self.manifest_path.write_text(
            _LINK_MANIFEST.format(target=target, parents=parents), encoding="utf-8"
        )

    def _only(self, plan):
        (item,) = plan.to_dict()["resources"]
        return item

    def test_absent_target_is_planned_then_linked_only_after_confirmation(self) -> None:
        self._configure()
        item = self._only(self._plan())
        self.assertEqual((item["status"], item["action"]), ("missing", "create"))
        self.assertEqual(item["forward_fields"], ["link"])
        self.assertIsNone(item["effective_mode"])

        preview = apply_plan(self._plan(), confirm=False)
        self.assertEqual(preview.outcome, "confirmation_required")
        self.assertEqual([a.kind for a in preview.actions], ["link_create"])
        self.assertFalse(os.path.lexists(self.link))

        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(result.outcome, "committed")
        self.assertEqual(result.verification_status, "verified")
        self.assertEqual(os.readlink(self.link), str(self.source / "shellrc"))

    def test_a_link_swapped_after_creation_is_reported_as_unknown(self) -> None:
        self._configure()
        original = engine_module.create_link

        def create_then_swap(root, path, value, **kwargs):
            identity = original(root, path, value, **kwargs)
            path.unlink()
            os.symlink("/nonexistent-elsewhere", path)
            return identity

        with patch("luwu.engine.create_link", side_effect=create_then_swap):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(
            (result.outcome, result.error_code),
            ("committed_state_unknown", "write_unknown"),
        )

    def test_converged_link_is_in_sync_and_needs_no_baseline(self) -> None:
        self._configure()
        apply_plan(self._plan(), confirm=True)
        item = self._only(self._plan())
        self.assertEqual((item["status"], item["action"]), ("in_sync", "noop"))
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "no_changes")
        self.assertEqual(list((self.inputs / "baselines").iterdir()), [])

    def test_editing_the_source_never_makes_the_link_drift(self) -> None:
        self._configure()
        apply_plan(self._plan(), confirm=True)
        (self.source / "shellrc").write_text("export A=2\n", encoding="utf-8")
        self.assertEqual(self._only(self._plan())["status"], "in_sync")

    def test_foreign_targets_block_and_are_never_replaced(self) -> None:
        self._configure()
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("x", encoding="utf-8")
        cases = {
            "target exists and is not a symlink": lambda: self.link.write_text(
                "mine", encoding="utf-8"
            ),
            "target is a symlink to a different path": lambda: self.link.symlink_to(
                elsewhere
            ),
        }
        for reason, make in cases.items():
            with self.subTest(reason=reason):
                make()
                item = self._only(self._plan())
                self.assertEqual((item["status"], item["reason"]), ("blocked", reason))
                self.link.unlink()
        self.link.mkdir()
        item = self._only(self._plan())
        self.assertEqual(item["reason"], "target exists and is not a symlink")

    def test_blocked_link_is_not_attempted_and_left_untouched(self) -> None:
        self._configure()
        self.link.write_text("mine", encoding="utf-8")
        result = apply_plan(self._plan(), confirm=False)
        self.assertEqual(
            (result.outcome, result.error_code), ("not_attempted", "review_required")
        )
        self.assertEqual(self.link.read_text(encoding="utf-8"), "mine")
        self.assertFalse(self.link.is_symlink())

    def test_missing_parents_block_under_require_and_are_created_under_create(
        self,
    ) -> None:
        self._configure(target=".config/app/rc")
        item = self._only(self._plan())
        self.assertEqual(
            (item["status"], item["reason"]), ("blocked", "target parents are missing")
        )

        self._configure(target=".config/app/rc", parents="create")
        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(result.outcome, "committed")
        self.assertEqual(
            [a.kind for a in result.actions], ["mkdir", "mkdir", "link_create"]
        )
        self.assertEqual(
            os.readlink(self.home / ".config/app/rc"), str(self.source / "shellrc")
        )

    def test_target_appearing_after_planning_makes_the_plan_stale(self) -> None:
        self._configure()
        plan = self._plan()
        self.link.write_text("late", encoding="utf-8")
        result = apply_plan(plan, confirm=True)
        self.assertEqual(
            (result.outcome, result.error_code), ("not_attempted", "stale_plan")
        )
        self.assertEqual(self.link.read_text(encoding="utf-8"), "late")

    def test_target_appearing_during_the_symlink_call_is_kept(self) -> None:
        self._configure()
        plan = self._plan()

        def racing(link_value: str, name: str, *, dir_fd: int) -> None:
            (self.home / name).write_text("raced", encoding="utf-8")
            raise FileExistsError(name)

        with patch("luwu.filesystem.os.symlink", side_effect=racing):
            result = apply_plan(plan, confirm=True)
        self.assertNotEqual(result.outcome, "committed")
        self.assertEqual(self.link.read_text(encoding="utf-8"), "raced")

    def test_missing_source_blocks_the_resource(self) -> None:
        self._configure()
        manifest = self._load()
        (self.source / "shellrc").unlink()
        item = self._only(build_plan(manifest))
        self.assertEqual(
            (item["status"], item["error_code"]), ("blocked", "resource_blocked")
        )

    def test_link_resources_reject_every_field_based_operation(self) -> None:
        from luwu.engine import (
            EngineError,
            accept_baseline,
            adopt_resource,
            reverse_inputs,
        )

        self._configure()
        apply_plan(self._plan(), confirm=True)
        plan = self._plan()
        with self.assertRaises(EngineError):
            adopt_resource(plan, resource="shellrc", confirm=True)
        with self.assertRaises(EngineError):
            accept_baseline(
                plan,
                resource="shellrc",
                fields=("link",),
                value_from="desired",
                confirm=True,
            )
        with self.assertRaises(EngineError):
            reverse_inputs(plan, resource="shellrc", fields=("link",), confirm=True)
