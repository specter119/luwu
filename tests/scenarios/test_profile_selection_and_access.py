"""Tests for profile selection and access."""

from __future__ import annotations

import json
import os

from tests.support import profile_cli
from tests.support.profile_cli import EXCLUDED_ACCESS_HOOK


class ProfileSelectionAndAccessTests(profile_cli.ProfileScenarioCase):
    def test_wsl_profile_excludes_inaccessible_payload_paths(self) -> None:
        case = self._case()
        before = case.snapshot()
        manifest_before = case.manifest.read_bytes()
        wsl_plan = case.cli("plan", profile="wsl", expected_code=0)
        self.assertEqual(wsl_plan.get("schema_version"), 8)
        self.assertEqual(
            wsl_plan.get("packages"), ["foundation", "formats", "payloads"]
        )
        self._assert_resource_set(wsl_plan, case)
        self.assertEqual(wsl_plan.get("excluded"), ["mihomo"])
        self._assert_snapshot_equal(case.snapshot(), before, "WSL profile plan")
        self.assertEqual(case.manifest.read_bytes(), manifest_before)
        self.assertTrue(
            (case.source_root / "templates" / "mihomo.jsonc.j2").is_symlink()
        )
        self.assertEqual(
            os.readlink(case.source_root / "templates" / "mihomo.jsonc.j2"),
            "excluded-source-must-not-be-read",
        )
        self.assertTrue(
            (
                case.input_root / "local" / "mihomo-exclusive" / "state.jsonc"
            ).is_symlink()
        )
        self.assertEqual(
            os.readlink(case.input_root / "local" / "mihomo-exclusive" / "state.jsonc"),
            "excluded-input-must-not-be-read",
        )
        excluded_target = case.target_root / ".config/mihomo/config.jsonc"
        self.assertTrue(excluded_target.is_symlink())
        self.assertEqual(
            os.readlink(excluded_target), "excluded-target-must-not-be-followed"
        )

    def test_dependency_cycles_unknown_packages_and_excluded_dependency_block(
        self,
    ) -> None:
        cycle = self._case()
        before_cycle = cycle.snapshot()
        cycle.replace_manifest(
            "[packages.foundation]\ndependencies = []",
            '[packages.foundation]\ndependencies = ["formats"]',
        )
        cycle.cli("plan", expected_code=2)
        self._assert_snapshot_equal(cycle.snapshot(), before_cycle, "cycle block")

        unknown = self._case()
        before_unknown = unknown.snapshot()
        unknown.replace_manifest(
            'packages = ["payloads", "formats"]',
            'packages = ["payloads", "missing_package"]',
        )
        unknown.cli("plan", expected_code=2)
        self._assert_snapshot_equal(
            unknown.snapshot(), before_unknown, "unknown package block"
        )

        excluded_dependency = self._case()
        before_excluded_dependency = excluded_dependency.snapshot()
        excluded_dependency.replace_manifest(
            'dependencies = ["foundation"]\nresources = ["settings", "nested_toml"]',
            'dependencies = ["foundation", "mihomo"]\nresources = ["settings", "nested_toml"]',
        )
        excluded_dependency.cli("plan", profile="wsl", expected_code=2)
        self._assert_snapshot_equal(
            excluded_dependency.snapshot(),
            before_excluded_dependency,
            "excluded dependency block",
        )

    def test_excluded_resource_and_exclusive_input_have_zero_path_access(self) -> None:
        case = self._case()
        report = case.process_home / "excluded-accesses.json"
        before = case.snapshot()
        result = case.cli_with_engine_hook(
            EXCLUDED_ACCESS_HOOK,
            report,
            "plan",
            profile="wsl",
            expected_code=(0, 86),
        )
        self.assertEqual(result.get("excluded"), ["mihomo"])
        counts = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(
            counts,
            dict.fromkeys(
                (
                    "access",
                    "builtins.open",
                    "chmod",
                    "chown",
                    "fchmod",
                    "fchown",
                    "io.open",
                    "link",
                    "listdir",
                    "lstat",
                    "mkdir",
                    "os.open",
                    "readlink",
                    "remove",
                    "rename",
                    "replace",
                    "rmdir",
                    "scandir",
                    "stat",
                    "symlink",
                    "unlink",
                ),
                0,
            ),
        )
        self._assert_snapshot_equal(case.snapshot(), before, "excluded path access")

    def test_wsl_apply_does_not_access_or_mutate_excluded_paths(self) -> None:
        case = self._case()
        self._accept_all(case, profile="wsl")
        before = case.snapshot()
        excluded_before = {
            path: value
            for path, value in before.items()
            if path.startswith(
                ("inputs/local/mihomo-exclusive/", "home/.config/mihomo/")
            )
        }
        report = case.process_home / "wsl-apply-excluded-accesses.json"
        result = case.cli_with_engine_hook(
            EXCLUDED_ACCESS_HOOK,
            report,
            "apply",
            "--yes",
            profile="wsl",
            expected_code=0,
        )
        self._assert_outcome(result, "committed")
        counts = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(set(counts.values()), {0})
        after = case.snapshot()
        excluded_after = {
            path: value
            for path, value in after.items()
            if path.startswith(
                ("inputs/local/mihomo-exclusive/", "home/.config/mihomo/")
            )
        }
        self._assert_snapshot_equal(
            excluded_after, excluded_before, "WSL excluded paths"
        )
