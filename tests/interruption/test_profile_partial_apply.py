"""Tests for profile partial apply."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

from tests.support import profile_cli
from tests.support.profile_cli import (
    FAIL_PARENT_HOOK,
    FAIL_WRITE_HOOK,
    FUTURE_SOURCE_CHANGE_HOOK,
    NOOP_FINAL_CHANGE_HOOK,
    NOOP_PREFLIGHT_CHANGE_HOOK,
    ROOT,
)


class ProfilePartialApplyTests(profile_cli.ProfileScenarioCase):
    def test_tampered_plan_bytes_are_rejected_before_any_writer(self) -> None:
        case = self._case()
        source_package = ROOT / "src"
        if source_package.is_dir() and str(source_package) not in sys.path:
            sys.path.insert(0, str(source_package))
        engine = importlib.import_module("luwu.engine")
        manifest_module = importlib.import_module("luwu.manifest")

        def build_current_plan():
            manifest = manifest_module.load_manifest(case.manifest, profile="synthetic")
            return engine.build_plan(manifest)

        for resource in (
            "copy_profile",
            "assets:defaults.conf",
            "assets:nested/keymap.jsonc",
        ):
            result = engine.adopt_resource(
                build_current_plan(), resource=resource, confirm=True
            )
            self.assertEqual(result.outcome, "committed")
        for resource, fields in (
            ("settings", ("theme", "brand")),
            ("nested_toml", ("theme", "worker_name")),
        ):
            result = engine.accept_baseline(
                build_current_plan(),
                resource=resource,
                fields=fields,
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(result.outcome, "committed")

        plan = build_current_plan()
        settings = next(
            item for item in plan._resources if item.resource.name == "settings"
        )
        self.assertIsNotNone(settings.write_data)
        object.__setattr__(settings, "write_data", b"tampered-plan-output-v1\n")
        before = case.snapshot()
        with (
            patch.object(engine, "write_file", wraps=engine.write_file) as write_file,
            patch.object(
                engine, "create_parent", wraps=engine.create_parent
            ) as create_parent,
            self.assertRaises(engine.EngineError) as raised,
        ):
            engine.apply_plan(plan, confirm=True)
        self.assertEqual(raised.exception.code, "plan_invalid")
        self.assertEqual(write_file.call_count, 0)
        self.assertEqual(create_parent.call_count, 0)
        self._assert_snapshot_equal(case.snapshot(), before, "tampered plan apply")

    def test_future_resource_change_keeps_prior_independent_writes_partial(
        self,
    ) -> None:
        case = self._case()
        self._accept_finite_batch(case)
        self._assert_outcome(
            case.cli("apply", "--yes", profile="finite_batch", expected_code=0),
            "committed",
        )
        targets = self._finite_batch_targets(case)
        for resource, value in (
            ("batch_a", "finite-batch-a.initial.json"),
            ("batch_b", "finite-batch-b.initial.json"),
            ("batch_c", "finite-batch-c.initial.json"),
        ):
            self._assert_bytes_equal(
                targets[resource].read_bytes(), case.expected(value), resource
            )

        for resource in ("batch_a", "batch_b", "batch_c"):
            source = (
                case.input_root
                / "local"
                / f"finite-{resource.replace('batch_', 'batch-')}.json"
            )
            expected_name = (
                f"finite-{resource.replace('_', '-')}.after-source-update.json"
            )
            source.write_bytes(case.expected(expected_name))

        plan = case.cli("plan", profile="finite_batch", expected_code=0)
        self._assert_finite_batch_resource_set(plan, case)
        planned = {item.get("name"): item for item in self._resources(plan)}
        self.assertEqual(
            {name for name, item in planned.items() if item.get("status") == "drifted"},
            {"batch_a", "batch_b", "batch_c"},
        )
        before_preview = case.snapshot()
        preview = case.cli("apply", profile="finite_batch", expected_code=2)
        self._assert_outcome(preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_preview, "finite-batch preview"
        )

        control = case.process_home / "future-source-control.json"
        report = case.process_home / "future-source-report.json"
        future_input = case.input_root / "local/finite-batch-c.json"
        control.write_text(
            json.dumps(
                {
                    "trigger_targets": [str(targets["batch_a"])],
                    "mutation_path": str(future_input),
                    "replacement_hex": case.expected(
                        "finite-batch-c.after-preflight-race.json"
                    ).hex(),
                    "report": str(report),
                }
            ),
            encoding="utf-8",
        )

        # C changes after A returns; B is independent and remains eligible to write.
        result = case.cli_with_engine_hook(
            FUTURE_SOURCE_CHANGE_HOOK,
            control,
            "apply",
            "--yes",
            expected_code=2,
            profile="finite_batch",
        )
        self._assert_outcome(result, "partial")
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8")),
            {
                "mutated": True,
                "completed": [str(targets["batch_a"])],
            },
        )
        target_states = {
            action.get("resource"): action.get("state")
            for action in self._actions(result)
            if action.get("kind") == "target_write"
        }
        self.assertEqual(
            target_states,
            {
                "batch_a": "committed",
                "batch_b": "committed",
                "batch_c": "not_attempted",
            },
        )
        self._assert_bytes_equal(
            targets["batch_a"].read_bytes(),
            case.expected("finite-batch-a.after-source-update.json"),
            "batch A committed before future C changed",
        )
        self._assert_bytes_equal(
            targets["batch_b"].read_bytes(),
            case.expected("finite-batch-b.after-source-update.json"),
            "independent batch B remains eligible after C changed",
        )
        self._assert_bytes_equal(
            future_input.read_bytes(),
            case.expected("finite-batch-c.after-preflight-race.json"),
            "future C input changed after preflight",
        )
        self._assert_bytes_equal(
            targets["batch_c"].read_bytes(),
            case.expected("finite-batch-c.initial.json"),
            "C target remains untouched at its stale write boundary",
        )

    def test_preflight_checks_noop_target_before_first_write(self) -> None:
        case = self._case()
        self._accept_finite_batch(case)
        self._assert_outcome(
            case.cli("apply", "--yes", profile="finite_batch", expected_code=0),
            "committed",
        )
        targets = self._finite_batch_targets(case)
        (case.input_root / "local/finite-batch-a.json").write_bytes(
            case.expected("finite-batch-a.after-source-update.json")
        )

        plan = case.cli("plan", profile="finite_batch", expected_code=0)
        self._assert_finite_batch_resource_set(plan, case)
        planned = {item.get("name"): item for item in self._resources(plan)}
        self.assertEqual(planned["batch_a"].get("status"), "drifted")
        self.assertIn(planned["batch_c"].get("status"), {"in_sync", "converged"})

        control = case.process_home / "noop-preflight-control.json"
        report = case.process_home / "noop-preflight-report.json"
        control.write_text(
            json.dumps(
                {
                    "noop_target": str(targets["batch_c"]),
                    "replacement_hex": case.expected(
                        "finite-batch-c.noop-after-preflight.json"
                    ).hex(),
                    "report": str(report),
                }
            ),
            encoding="utf-8",
        )

        result = case.cli_with_engine_hook(
            NOOP_PREFLIGHT_CHANGE_HOOK,
            control,
            "apply",
            "--yes",
            expected_code=2,
            profile="finite_batch",
        )
        self.assertEqual(result.get("outcome"), "not_attempted")
        self.assertEqual(result.get("error_code"), "stale_plan")
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8")),
            {"mutated": True, "counts": {"create_parent": 0, "write_file": 0}},
        )
        self._assert_bytes_equal(
            targets["batch_a"].read_bytes(),
            case.expected("finite-batch-a.initial.json"),
            "preflight stale noop prevents first write",
        )
        self._assert_bytes_equal(
            targets["batch_c"].read_bytes(),
            case.expected("finite-batch-c.noop-after-preflight.json"),
            "externally changed noop remains visible",
        )

    def test_noop_target_change_fails_final_verification(self) -> None:
        case = self._case()
        self._accept_finite_batch(case)
        self._assert_outcome(
            case.cli("apply", "--yes", profile="finite_batch", expected_code=0),
            "committed",
        )
        targets = self._finite_batch_targets(case)
        (case.input_root / "local/finite-batch-a.json").write_bytes(
            case.expected("finite-batch-a.after-source-update.json")
        )

        plan = case.cli("plan", profile="finite_batch", expected_code=0)
        self._assert_finite_batch_resource_set(plan, case)
        planned = {item.get("name"): item for item in self._resources(plan)}
        self.assertEqual(planned["batch_a"].get("status"), "drifted")
        self.assertIn(planned["batch_c"].get("status"), {"in_sync", "converged"})

        control = case.process_home / "noop-final-control.json"
        report = case.process_home / "noop-final-report.json"
        control.write_text(
            json.dumps(
                {
                    "trigger_target": str(targets["batch_a"]),
                    "noop_target": str(targets["batch_c"]),
                    "replacement_hex": case.expected(
                        "finite-batch-c.noop-after-preflight.json"
                    ).hex(),
                    "report": str(report),
                }
            ),
            encoding="utf-8",
        )

        result = case.cli_with_engine_hook(
            NOOP_FINAL_CHANGE_HOOK,
            control,
            "apply",
            "--yes",
            expected_code=2,
            profile="finite_batch",
        )
        self._assert_outcome(result, "committed_but_verification_failed")
        self.assertNotEqual(result.get("verification_status"), "verified")
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8")), {"mutated": True}
        )
        target_states = {
            action.get("resource"): action.get("state")
            for action in self._actions(result)
            if action.get("kind") == "target_write"
        }
        self.assertEqual(target_states, {"batch_a": "committed"})
        self._assert_bytes_equal(
            targets["batch_a"].read_bytes(),
            case.expected("finite-batch-a.after-source-update.json"),
            "declared write before final verification",
        )
        self._assert_bytes_equal(
            targets["batch_c"].read_bytes(),
            case.expected("finite-batch-c.noop-after-preflight.json"),
            "externally changed noop target",
        )

    def test_partial_parent_creation_and_partial_target_write_are_reported(
        self,
    ) -> None:
        parent_case = self._case()
        self._accept_all(parent_case)
        self.assertEqual(
            parent_case.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        nested_target = (
            parent_case.target_root / ".config/demo/deep/profile/runtime.toml"
        )
        nested_target.unlink()
        (parent_case.target_root / ".config/demo/deep/profile").rmdir()
        (parent_case.target_root / ".config/demo/deep").rmdir()
        partial_parent = parent_case.cli_with_engine_hook(
            FAIL_PARENT_HOOK,
            Path("profile"),
            "apply",
            "--yes",
            expected_code=2,
        )
        self._assert_outcome(partial_parent, "partial")
        self.assertTrue((parent_case.target_root / ".config/demo/deep").is_dir())
        self.assertFalse(
            (parent_case.target_root / ".config/demo/deep/profile").exists()
        )
        self.assertFalse(nested_target.exists())
        mkdir_states = {
            action.get("path"): action.get("state")
            for action in self._actions(partial_parent)
            if action.get("kind") == "mkdir"
        }
        self.assertEqual(
            mkdir_states,
            {
                ".config/demo/deep": "committed",
                ".config/demo/deep/profile": "not_attempted",
            },
        )

        write_case = self._case()
        self._accept_all(write_case)
        self.assertEqual(
            write_case.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        (write_case.input_root / "global" / "settings.jsonc").write_bytes(
            write_case.expected("global-settings.after-brand-update.jsonc")
        )
        (write_case.input_root / "local" / "runtime.toml").write_bytes(
            write_case.expected("local-runtime.after-source-update.toml")
        )
        old_settings = (
            write_case.target_root / ".config/demo/settings.jsonc"
        ).read_bytes()
        partial_write = write_case.cli_with_engine_hook(
            FAIL_WRITE_HOOK,
            Path("settings.jsonc"),
            "apply",
            "--yes",
            expected_code=2,
        )
        self._assert_outcome(partial_write, "partial")
        self._assert_bytes_equal(
            (
                write_case.target_root / ".config/demo/deep/profile/runtime.toml"
            ).read_bytes(),
            write_case.expected("nested.after-worker-source-update.toml"),
            "committed first target",
        )
        self._assert_bytes_equal(
            (write_case.target_root / ".config/demo/settings.jsonc").read_bytes(),
            old_settings,
            "failed second target remains unchanged",
        )
        write_states = {
            action.get("resource"): action.get("state")
            for action in self._actions(partial_write)
            if action.get("kind") == "target_write"
        }
        self.assertEqual(
            write_states,
            {"nested_toml": "committed", "settings": "not_replaced"},
        )
