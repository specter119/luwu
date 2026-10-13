"""End-to-end checks for the isolated M6 rehearsal runner."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from experiments.m6_pilot import main


class M6RehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="luwu-m6-rehearsal-")
        cls.addClassCleanup(cls._temporary.cleanup)
        cls.root = Path(cls._temporary.name)
        cls.artifact_dir = cls.root / "artifacts"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["--artifact-dir", str(cls.artifact_dir)])
        cls.output = output.getvalue()
        cls.report = json.loads(cls.output)

    def test_pi_json_and_codex_toml_complete_two_bidirectional_cycles(self) -> None:
        report = self.report
        self.assertEqual(report["schema_version"], 7)
        self.assertTrue(report["isolated"])
        self.assertEqual(report["provider_access"], "none")
        self.assertEqual(report["network_access"], "none")
        self.assertEqual(
            [
                (case["resource"], case["format"], case["declared_fields"])
                for case in report["cases"]
            ],
            [
                ("pi", "json", ["model", "provider", "changelog"]),
                ("codex", "toml", ["model", "provider"]),
            ],
        )
        for case in report["cases"]:
            self.assertTrue(case["normal_home_layout"])
            self.assertEqual(case["initial_target"], "absent")
            self.assertEqual(case["baseline_outcome"], "committed")
            self.assertEqual(case["create_outcome"], "committed")
            self.assertTrue(case["initial_noop_observed"])
            self.assertEqual(case["final_action"], "noop")
            self.assertEqual(len(case["cycles"]), 2)
            for cycle_number, cycle in enumerate(case["cycles"], start=1):
                self.assertEqual(cycle["cycle"], cycle_number)
                self.assertEqual(cycle["application_change"], "simulated")
                self.assertEqual(cycle["reverse_outcome"], "committed")
                self.assertEqual(cycle["reverse_fields"], ["provider"])
                self.assertTrue(cycle["unselected_input_bytes_preserved"])
                self.assertEqual(cycle["source_change_fields"], ["model"])
                self.assertEqual(cycle["forward_outcome"], "committed")
                self.assertTrue(cycle["retained_live_owned_provider"])
                self.assertEqual(cycle["final_action"], "noop")

    def test_uncertain_write_is_metadata_only_and_recovery_is_read_only(self) -> None:
        recovery = self.report["recovery"]
        self.assertEqual(recovery["injected_boundary"], "post_replace_directory_sync")
        self.assertEqual(recovery["result"]["outcome"], "uncertain")
        self.assertEqual(recovery["result"]["fields"], ["model"])
        self.assertTrue(recovery["fresh_process"])
        self.assertFalse(recovery["automatic_replay"])
        self.assertFalse(recovery["baseline_advanced"])
        self.assertEqual(recovery["read_only_plan_action"], "noop")
        self.assertEqual(recovery["read_only_plan_status"], "noop")
        self.assertEqual(
            set(recovery["result"]),
            {"outcome", "operation", "resource", "fields", "reason_code"},
        )

    def test_dotter_conflict_is_blocked_and_return_steps_are_recorded(self) -> None:
        handoff = self.report["dotter_handoff"]
        self.assertEqual(handoff["simulation"], "isolated_fixture_only")
        self.assertEqual(handoff["conflicting_link"], "blocked")
        self.assertEqual(handoff["competing_manager"], "stopped_before_transfer")
        self.assertEqual(handoff["dotter_declaration"], "removed_in_fixture")
        self.assertEqual(handoff["transfer"], "explicit_dotter_to_luwu")
        self.assertEqual(handoff["luwu_create_outcome"], "committed")
        self.assertFalse(handoff["real_home_touched"])
        self.assertFalse(handoff["real_dotter_configuration_touched"])
        self.assertEqual(
            handoff["transfer_steps"],
            [
                "detect_conflicting_dotter_link_and_block_luwu",
                "stop_dotter_manager",
                "remove_target_from_dotter_declaration",
                "manually_remove_conflicting_link_in_isolated_fixture",
                "confirm_luwu_baseline_and_create_target",
            ],
        )
        self.assertFalse(handoff["manual_return"]["performed"])
        self.assertEqual(
            handoff["manual_return"]["steps"],
            [
                "stop_using_the_luwu_resource",
                "restore_the_dotter_target_declaration_through_the_normal_dotter_workflow",
                "verify_the_target_manager_before_the_next_deployment",
            ],
        )

    def test_stdout_and_artifacts_contain_only_sanitized_metadata(self) -> None:
        paths = self.report["artifacts"]
        self.assertEqual(set(paths), {"rehearsal", "dotter_handoff"})
        rehearsal_path = Path(paths["rehearsal"])
        handoff_path = Path(paths["dotter_handoff"])
        self.assertTrue(rehearsal_path.is_file())
        self.assertTrue(handoff_path.is_file())
        self.assertFalse(rehearsal_path.is_relative_to(Path.home()))
        for value in (
            "pi-model-v1",
            "pi-provider-v1",
            "pi-changelog-v1",
            "codex-model-v1",
            "codex-provider-v1",
            "public-note",
            "handoff-public-setting",
        ):
            self.assertNotIn(value, self.output)
        self.assertNotIn("secret", self.output.casefold())
        self.assertEqual(json.loads(rehearsal_path.read_text()), self.report)
        self.assertEqual(
            json.loads(handoff_path.read_text()), self.report["dotter_handoff"]
        )


if __name__ == "__main__":
    unittest.main()
