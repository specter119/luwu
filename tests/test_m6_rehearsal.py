"""End-to-end checks for the isolated M6 rehearsal runner."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from experiments import m6_pilot
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
                (
                    "pi",
                    "json",
                    ["defaultModel", "defaultProvider", "lastChangelogVersion"],
                ),
                ("codex", "toml", ["model", "model_provider"]),
            ],
        )
        self.assertEqual(
            report["cli_execution"], "python -m luwu from the active environment"
        )
        self.assertEqual(
            report["application_invocation"],
            "none; application edits are synthetic file edits",
        )
        self.assertEqual(
            report["dotter_invocation"],
            "none; operator steps are simulated on fixture state",
        )
        self.assertFalse(report["dotter_handoff"]["real_dotter_called"])
        self.assertFalse(report["dotter_handoff"]["real_application_invoked"])
        for case in report["cases"]:
            self.assertTrue(case["normal_home_layout"])
            self.assertIn(
                case["target_relative_to_home"],
                {
                    ".pi/agent/settings.json",
                    ".codex/config.toml",
                },
            )
            self.assertEqual(case["initial_target"], "absent")
            self.assertEqual(case["baseline_outcome"], "committed")
            self.assertEqual(case["create_outcome"], "committed")
            self.assertTrue(case["cli_round_trip"])
            self.assertTrue(case["initial_noop_observed"])
            self.assertEqual(case["final_action"], "noop")
            self.assertEqual(len(case["cycles"]), 2)
            for cycle_number, cycle in enumerate(case["cycles"], start=1):
                self.assertEqual(cycle["cycle"], cycle_number)
                self.assertEqual(cycle["application_change"], "synthetic_file_edit")
                self.assertEqual(cycle["reverse_outcome"], "committed")
                self.assertEqual(
                    cycle["reverse_fields"],
                    [
                        "defaultProvider"
                        if case["resource"] == "pi"
                        else "model_provider"
                    ],
                )
                self.assertTrue(cycle["unselected_input_bytes_preserved"])
                self.assertTrue(cycle["other_inputs_unchanged"])
                self.assertTrue(cycle["baseline_unchanged_during_reverse"])
                self.assertEqual(
                    cycle["source_change_fields"],
                    ["defaultModel" if case["resource"] == "pi" else "model"],
                )
                self.assertEqual(cycle["forward_outcome"], "committed")
                self.assertTrue(cycle["retained_live_owned_field"])
                self.assertEqual(cycle["final_action"], "noop")

    def test_uncertain_write_is_metadata_only_and_recovery_is_read_only(self) -> None:
        recoveries = self.report["recovery"]
        self.assertEqual([item["resource"] for item in recoveries], ["pi", "codex"])
        expected = {
            "pi": ("defaultModel", ".pi/agent/settings.json"),
            "codex": ("model", ".codex/config.toml"),
        }
        for recovery in recoveries:
            resource = recovery["resource"]
            field, target = expected[resource]
            self.assertEqual(
                recovery["injected_boundary"], "post_replace_directory_sync"
            )
            self.assertEqual(recovery["result"]["outcome"], "committed_state_unknown")
            self.assertEqual(recovery["result"]["fields"], [field])
            self.assertEqual(recovery["result"]["committed"], [target])
            self.assertEqual(recovery["result"]["attempted"], [target])
            self.assertEqual(recovery["result"]["not_replaced"], [])
            self.assertEqual(recovery["result"]["uncertain"], [target])
            self.assertTrue(recovery["fresh_process_cli_observation"])
            self.assertFalse(recovery["automatic_replay"])
            self.assertFalse(recovery["baseline_advanced"])
            self.assertTrue(recovery["read_only_target_unchanged"])
            self.assertTrue(recovery["read_only_baseline_unchanged"])
            self.assertEqual(recovery["read_only_plan_action"], "noop")
            self.assertEqual(recovery["read_only_plan_status"], "noop")
            self.assertEqual(
                set(recovery["result"]),
                {
                    "outcome",
                    "operation",
                    "resource",
                    "fields",
                    "committed",
                    "attempted",
                    "not_replaced",
                    "uncertain",
                    "verification_status",
                },
            )

    def test_dotter_conflict_is_blocked_and_return_steps_are_recorded(self) -> None:
        handoff = self.report["dotter_handoff"]
        self.assertEqual(
            handoff["simulation"], "synthetic_operator_and_target_fixture_only"
        )
        self.assertEqual(handoff["conflicting_link"], "blocked")
        self.assertEqual(handoff["handoff_to_luwu_outcome"], "committed")
        self.assertTrue(handoff["luwu_managed_after_handoff"])
        self.assertFalse(handoff["real_home_touched"])
        returned = handoff["return_to_dotter"]
        self.assertTrue(returned["performed"])
        self.assertTrue(returned["fixture_declaration_restored"])
        self.assertTrue(returned["fixture_target_link_restored"])
        self.assertEqual(returned["dotter_manager_status"], "active")
        self.assertFalse(returned["luwu_resource_in_use"])
        self.assertEqual(returned["luwu_reobservation"], "blocked")
        self.assertIn(
            "restore_dotter_fixture_declaration_and_target_link", returned["steps"]
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
            "synthetic-pi-model-v1",
            "synthetic-pi-provider-v1",
            "synthetic-codex-model-v1",
            "synthetic-codex-provider-v1",
            "2026.10.0",
            "public-note",
            "synthetic-pi-app-1",
        ):
            self.assertNotIn(value, self.output)
        self.assertNotIn("secret", self.output.casefold())
        self.assertEqual(json.loads(rehearsal_path.read_text()), self.report)
        self.assertEqual(
            json.loads(handoff_path.read_text()), self.report["dotter_handoff"]
        )

    def test_runner_does_not_force_a_source_checkout_on_sys_path(self) -> None:
        source = Path(m6_pilot.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sys.path", source)
        self.assertNotIn('ROOT / "src"', source)

    def test_cli_absolutizes_existing_relative_pythonpath_before_chdir(self) -> None:
        caller_cwd = Path.cwd().resolve()
        expected = os.pathsep.join(
            (str((caller_cwd / "src").resolve()), str(caller_cwd))
        )
        with tempfile.TemporaryDirectory(prefix="luwu-m6-home-") as raw_home:
            home = Path(raw_home)
            manifest = home / "luwu.toml"
            with (
                mock.patch.dict(os.environ, {"PYTHONPATH": "src:."}),
                mock.patch("experiments.m6_pilot.subprocess.run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="{}", stderr=""
                )
                m6_pilot._cli(manifest, home, "inspect", "--json", expected_code=0)
            command = run.call_args.args[0]
            self.assertIn("--manifest", command)
            self.assertEqual(command[command.index("--manifest") + 1], str(manifest))
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"], expected)
            self.assertEqual(run.call_args.kwargs["cwd"], home)


if __name__ == "__main__":
    unittest.main()
