"""Tests for profile authorization."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from luwu import engine as engine_module
from luwu import manifest as manifest_module
from luwu.engine import (
    accept_baseline,
    adopt_resource,
    apply_plan,
)
from luwu.filesystem import FileObservation
from tests.support import profile_case

_CheckInputs = Callable[[], None] | None


class ProfileAuthorizationTests(profile_case.ProfileEngineCase):
    def test_reauthorization_reuses_manifest_and_keeps_full_fresh_guards(self) -> None:
        plan = self._plan()
        before = self._snapshot()
        original_verify = engine_module.verify_manifest
        original_observe = engine_module._observe_all
        with (
            patch(
                "luwu.manifest.load_manifest",
                side_effect=AssertionError("reauthorization reloaded the manifest"),
            ),
            patch("luwu.engine.verify_manifest", wraps=original_verify) as verify,
            patch("luwu.engine._observe_all", wraps=original_observe) as observe,
        ):
            authorized = engine_module._require_plan(plan, operation="apply")

        self.assertIs(authorized.manifest, plan.manifest)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(self._snapshot(), before)

    def test_after_plan_target_hardlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        target = self._target()
        target.parent.mkdir(parents=True)
        alias = self.root / "target-hardlink"
        alias.write_bytes(b"{}\n")
        target.hardlink_to(alias)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_after_plan_input_hardlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        input_path = self.inputs / "local" / "settings.json"
        alias = self.root / "input-hardlink"
        alias.write_bytes(input_path.read_bytes())
        input_path.unlink()
        input_path.hardlink_to(alias)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_after_plan_baseline_hardlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        baseline = self.inputs / "baselines" / "settings.json"
        alias = self.root / "baseline-hardlink"
        alias.write_bytes(b"{}\n")
        baseline.hardlink_to(alias)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_after_plan_cross_role_hardlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        target = self._target()
        target.parent.mkdir(parents=True)
        input_path = self.inputs / "local" / "settings.json"
        target.hardlink_to(input_path)
        self.assertEqual(target.stat().st_ino, input_path.stat().st_ino)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_after_plan_parent_symlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / ".config").symlink_to(outside, target_is_directory=True)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_same_bytes_manifest_inode_replacement_blocks_before_any_write(
        self,
    ) -> None:
        plan = self._plan()
        original_identity = (
            self.manifest_path.stat().st_dev,
            self.manifest_path.stat().st_ino,
        )
        replacement = self.root / "replacement.toml"
        replacement.write_bytes(self.manifest_path.read_bytes())
        self.assertEqual(replacement.read_bytes(), self.manifest_path.read_bytes())
        self.assertNotEqual(
            (replacement.stat().st_dev, replacement.stat().st_ino), original_identity
        )
        replacement.replace(self.manifest_path)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_directory_membership_change_blocks_before_any_write(self) -> None:
        assets = self.source / "assets"
        assets.mkdir()
        (assets / "one.conf").write_text("one\n", encoding="utf-8")
        self.manifest_path.write_text(
            """
version = 8
[roots]
source = "source"
inputs = "inputs"
target = "home"
[profiles.work]
packages = ["assets"]
exclude = []
[packages.assets]
dependencies = []
resources = ["asset_tree"]
[inputs]
[resources.asset_tree]
deploy = "copy"
source = "assets"
target = ".config/assets"
baseline = "baselines/assets"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
""",
            encoding="utf-8",
        )
        plan = self._plan()
        (assets / "added.conf").write_text("added\n", encoding="utf-8")
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_resigned_binding_and_layer_mutations_block_before_any_write(self) -> None:
        binding_plan = self._plan()
        binding = binding_plan.manifest.resources[0].fields["brand"]
        object.__setattr__(binding, "owner", "merge")
        object.__setattr__(
            binding_plan.manifest,
            "_signature",
            manifest_module._manifest_signature(binding_plan.manifest),
        )
        with self.subTest(mutation="field binding"):
            self._assert_invalid_reauthorization_is_readonly(binding_plan)

        layer_plan = self._plan()
        layer = layer_plan.manifest.inputs["brand"].layers[0]
        object.__setattr__(layer, "path", ("values", "theme"))
        object.__setattr__(
            layer_plan.manifest,
            "_signature",
            manifest_module._manifest_signature(layer_plan.manifest),
        )
        with self.subTest(mutation="input layer"):
            self._assert_invalid_reauthorization_is_readonly(layer_plan)

    def test_post_authorization_plan_mutation_keeps_authorized_write_bytes(
        self,
    ) -> None:
        self._accept_template_baseline()
        plan = self._plan()
        expected = plan._resources[0].write_data
        self.assertIsNotNone(expected)
        original_specs = engine_module._forward_specs

        def mutate_original_plan(resources, manifest):
            object.__setattr__(plan._resources[0], "write_data", b"forged\n")
            return original_specs(resources, manifest)

        with patch("luwu.engine._forward_specs", side_effect=mutate_original_plan):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(self._target().read_bytes(), expected)
        self.assertNotEqual(self._target().read_bytes(), b"forged\n")

    def test_forward_write_spec_is_rebuilt_from_fresh_observation(self) -> None:
        self._accept_template_baseline()
        local = self.inputs / "local" / "settings.json"
        inputs = json.loads(local.read_text(encoding="utf-8"))
        inputs["overrides"]["theme"] = "overlay-theme-v2"
        local.write_text(json.dumps(inputs) + "\n", encoding="utf-8")
        target = self._target()
        original_spec = engine_module._forward_specs_for_item
        calls = 0

        def poison_cached_spec(
            item: engine_module._ResourcePlan, manifest: engine_module.Manifest
        ) -> engine_module._WriteSpec | None:
            nonlocal calls
            spec = original_spec(item, manifest)
            calls += 1
            if calls == 1 and spec is not None:
                return replace(spec, data=b'{"settings":{"theme":"forged"}}\n')
            return spec

        with patch(
            "luwu.engine._forward_specs_for_item",
            side_effect=poison_cached_spec,
        ):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertGreaterEqual(calls, 2)
        written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(written["settings"]["theme"], "overlay-theme-v2")

    def test_review_in_any_resource_blocks_the_whole_forward_batch(self) -> None:
        review_target = self._add_review_resource()
        for resource in ("settings", "review"):
            result = accept_baseline(
                self._plan(),
                resource=resource,
                fields=("theme", "brand"),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(result.outcome, "committed")
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")

        settings_before = self._target().read_bytes()
        review = json.loads(review_target.read_text(encoding="utf-8"))
        review["unmanaged"]["source"] = "review-unknown-sentinel"
        review_target.write_text(json.dumps(review) + "\n", encoding="utf-8")
        local = self.inputs / "local" / "settings.json"
        inputs = json.loads(local.read_text(encoding="utf-8"))
        inputs["overrides"]["theme"] = "overlay-theme-v2"
        local.write_text(json.dumps(inputs) + "\n", encoding="utf-8")

        plan = self._plan()
        self.assertTrue(
            any(item["undeclared_changed"] for item in plan.to_dict()["resources"])
        )
        preview = apply_plan(plan, confirm=False)
        self.assertEqual(preview.outcome, "not_attempted")
        self.assertEqual(preview.error_code, "review_required")
        result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "review_required")
        self.assertTrue(
            all(action.state == "not_attempted" for action in result.actions)
        )
        self.assertEqual(self._target().read_bytes(), settings_before)
        self.assertIn("review-unknown-sentinel", review_target.read_text())

    def test_stale_noop_recheck_and_partial_batch_preserve_action_evidence(
        self,
    ) -> None:
        self._deploy_template()
        no_op_plan = self._plan()
        target = self._target()
        target.write_text(
            target.read_text().replace("unmanaged-source-v1", "external-v2"),
            encoding="utf-8",
        )
        stale = apply_plan(no_op_plan, confirm=True)
        self.assertEqual(stale.outcome, "not_attempted")
        self.assertEqual(stale.error_code, "stale_plan")

        (self.source / "a.txt").write_text("source-a-v2\n", encoding="utf-8")
        (self.source / "b.txt").write_text("source-b-v2\n", encoding="utf-8")
        (self.home / "a.txt").write_text("live-a-v1\n", encoding="utf-8")
        (self.home / "b.txt").write_text("live-b-v1\n", encoding="utf-8")
        self.manifest_path.write_text(
            """
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
resources = ["a", "b"]
[inputs]
[resources.a]
deploy = "copy"
source = "a.txt"
target = "a.txt"
baseline = "baselines/a.json"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
[resources.b]
deploy = "copy"
source = "b.txt"
target = "b.txt"
baseline = "baselines/b.json"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
""",
            encoding="utf-8",
        )
        adopt_resource(self._plan(), resource="a", confirm=True)
        adopt_resource(self._plan(), resource="b", confirm=True)
        original_write = engine_module.write_file
        target_b = self.home / "b.txt"

        def write_then_change_other_resource(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )
            if path == self.home / "a.txt":
                target_b.write_text("external-b-v3\n", encoding="utf-8")
            return identity

        with patch(
            "luwu.engine.write_file", side_effect=write_then_change_other_resource
        ):
            partial = apply_plan(self._plan(), confirm=True)
        self.assertEqual(partial.outcome, "partial")
        states = {
            (action.resource, action.kind): action.state for action in partial.actions
        }
        self.assertEqual(states[("a", "target_write")], "committed")
        self.assertEqual(states[("b", "target_write")], "not_attempted")
        self.assertEqual(
            (self.home / "a.txt").read_text(encoding="utf-8"), "source-a-v2\n"
        )
        self.assertEqual(target_b.read_text(encoding="utf-8"), "external-b-v3\n")

    def test_batch_defers_independent_future_resource_staleness(self) -> None:
        names = ("a", "b", "c")
        for name in names:
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v1\n", encoding="utf-8"
            )
            (self.home / f"{name}.txt").write_text(
                f"live-{name}-v1\n", encoding="utf-8"
            )
        self._configure_copy_batch(names)
        for name in names:
            self.assertEqual(
                adopt_resource(self._plan(), resource=name, confirm=True).outcome,
                "committed",
            )
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v2\n", encoding="utf-8"
            )

        original_write = engine_module.write_file
        target_c = self.home / "c.txt"

        def write_then_stale_future(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )
            if path == self.home / "a.txt":
                target_c.write_text("external-c-v3\n", encoding="utf-8")
            return identity

        with patch("luwu.engine.write_file", side_effect=write_then_stale_future):
            result = apply_plan(self._plan(), confirm=True)

        states = {
            (action.resource, action.kind): action.state for action in result.actions
        }
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.error_code, "stale_plan")
        self.assertEqual(states[("a", "target_write")], "committed")
        self.assertEqual(states[("b", "target_write")], "committed")
        self.assertEqual(states[("c", "target_write")], "not_attempted")
        self.assertEqual((self.home / "a.txt").read_text(), "source-a-v2\n")
        self.assertEqual((self.home / "b.txt").read_text(), "source-b-v2\n")
        self.assertEqual(target_c.read_text(), "external-c-v3\n")

    def test_final_batch_recheck_detects_changed_noop_after_write(self) -> None:
        names = ("write", "noop")
        for name in names:
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v1\n", encoding="utf-8"
            )
            (self.home / f"{name}.txt").write_text(
                f"source-{name}-v1\n" if name == "noop" else f"live-{name}-v1\n",
                encoding="utf-8",
            )
        self._configure_copy_batch(names)
        for name in names:
            self.assertEqual(
                adopt_resource(self._plan(), resource=name, confirm=True).outcome,
                "committed",
            )
        (self.source / "write.txt").write_text("source-write-v2\n", encoding="utf-8")
        plan = self._plan()
        original_write = engine_module.write_file
        noop_target = self.home / "noop.txt"

        def write_then_change_noop(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )
            noop_target.write_text("external-noop-v3\n", encoding="utf-8")
            return identity

        with patch("luwu.engine.write_file", side_effect=write_then_change_noop):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed_but_verification_failed")
        self.assertEqual(result.error_code, "stale_plan")
        self.assertEqual(
            [(action.resource, action.state) for action in result.actions],
            [("write", "committed")],
        )
        self.assertEqual((self.home / "write.txt").read_text(), "source-write-v2\n")
        self.assertEqual(noop_target.read_text(), "external-noop-v3\n")

    def test_future_plan_tamper_during_write_does_not_change_authorized_bytes(
        self,
    ) -> None:
        review_target = self._add_review_resource()
        for resource in ("settings", "review"):
            accepted = accept_baseline(
                self._plan(),
                resource=resource,
                fields=("theme", "brand"),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(accepted.outcome, "committed")
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")

        local = self.inputs / "local" / "settings.json"
        input_data = json.loads(local.read_text(encoding="utf-8"))
        input_data["overrides"]["theme"] = "overlay-theme-v2"
        local.write_text(json.dumps(input_data) + "\n", encoding="utf-8")
        plan = self._plan()
        specs = engine_module._forward_specs(plan._resources, plan.manifest)
        self.assertEqual(len(specs), 2)
        first = specs[0]
        future_resource_name = next(
            spec.resource.name
            for spec in specs
            if spec.resource.name != first.resource.name
        )
        future_item = next(
            item
            for item in plan._resources
            if item.resource.name == future_resource_name
        )
        future_target = (
            review_target if future_resource_name == "review" else self._target()
        )
        future_spec = next(
            spec for spec in specs if spec.resource.name == future_resource_name
        )
        expected_future_bytes = future_spec.data
        original_write = engine_module.write_file
        changed_future_plan = False

        def write_then_tamper_future(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            nonlocal changed_future_plan
            identity = original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )
            if path == first.path:
                object.__setattr__(future_item, "write_data", b"FORGED_FUTURE_WRITE")
                changed_future_plan = True
            return identity

        with patch("luwu.engine.write_file", side_effect=write_then_tamper_future):
            result = apply_plan(plan, confirm=True)

        self.assertTrue(changed_future_plan)
        self.assertEqual(result.outcome, "committed")
        states = {
            (action.resource, action.kind): action.state for action in result.actions
        }
        self.assertEqual(states[(first.resource.name, "target_write")], "committed")
        self.assertEqual(states[(future_resource_name, "target_write")], "committed")
        self.assertTrue(first.path.is_file())
        self.assertEqual(future_target.read_bytes(), expected_future_bytes)
        self.assertNotEqual(future_target.read_bytes(), b"FORGED_FUTURE_WRITE")
