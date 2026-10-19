"""Tests for profile reverse redeploy."""

from __future__ import annotations

import json
import stat

from tests.support import profile_cli
from tests.support.profile_cli import (
    COMMENT_RACE_HOOK,
    EXPECTED_RESOURCES,
    MUTATION_COUNT_HOOK,
    UNKNOWN_SENTINEL,
)


class ProfileReverseRedeployTests(profile_cli.ProfileScenarioCase):
    def test_profiled_multi_resource_reverse_and_redeploy_loop(self) -> None:
        case = self._case()
        initial = case.cli("plan", expected_code=0)
        self.assertEqual(initial.get("schema_version"), 8)
        self.assertEqual(initial.get("packages"), ["foundation", "formats", "payloads"])
        self.assertEqual(initial.get("excluded"), [])
        self._assert_resource_set(initial, case)
        self.assertEqual(
            tuple(item.get("name") for item in self._resources(initial)),
            EXPECTED_RESOURCES,
        )
        self._accept_all(case)

        before_apply_preview = case.snapshot()
        preview = case.cli("apply", expected_code=2)
        self._assert_outcome(preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_apply_preview, "apply preview"
        )
        applied = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(applied, "committed")
        self._assert_deployed_fixture_bytes(case)
        self.assertEqual(
            stat.S_IMODE((case.target_root / ".config/demo/deep").stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(
                (case.target_root / ".config/demo/deep/profile").stat().st_mode
            ),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(
                (case.target_root / ".config/demo/settings.jsonc").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(
                (case.target_root / ".config/demo/deep/profile/runtime.toml")
                .stat()
                .st_mode
            ),
            0o600,
        )

        local_settings_theme = case.input_root / "local" / "settings-theme.jsonc"
        self._assert_bytes_equal(
            local_settings_theme.read_bytes(),
            case.expected("local-settings-theme.initial.jsonc"),
            "independent settings theme input before reverse",
        )
        shared_theme_input = case.input_root / "local" / "settings.jsonc"
        shared_theme_before_reverse = shared_theme_input.read_bytes()
        settings_target = case.target_root / ".config/demo/settings.jsonc"
        live_before = settings_target.read_bytes()
        old_theme = b'"synthetic-theme-overlay-v1"'
        new_theme = b'"synthetic-theme-app-v2"'
        self.assertEqual(live_before.count(old_theme), 1)
        live_after_app_edit = live_before.replace(old_theme, new_theme, 1)
        settings_target.write_bytes(live_after_app_edit)
        local_before_reverse = local_settings_theme.read_bytes()

        reverse_preview = case.cli(
            "reverse-sync",
            "--resource",
            "settings",
            "--field",
            "theme",
            expected_code=2,
        )
        self._assert_outcome(reverse_preview, "confirmation_required")
        self._assert_bytes_equal(
            local_settings_theme.read_bytes(),
            local_before_reverse,
            "reverse preview input",
        )
        self._assert_bytes_equal(
            shared_theme_input.read_bytes(),
            shared_theme_before_reverse,
            "unselected shared input during reverse preview",
        )
        self._assert_bytes_equal(
            settings_target.read_bytes(), live_after_app_edit, "reverse preview target"
        )

        reverse = case.cli(
            "reverse-sync",
            "--resource",
            "settings",
            "--field",
            "theme",
            "--yes",
            expected_code=0,
        )
        self._assert_outcome(reverse, "committed")
        self._assert_bytes_equal(
            local_settings_theme.read_bytes(),
            case.expected("local-settings-theme.after-reverse.jsonc"),
            "reverse-updated JSONC input",
        )
        self._assert_bytes_equal(
            shared_theme_input.read_bytes(),
            shared_theme_before_reverse,
            "unselected shared input after reverse",
        )
        self._assert_bytes_equal(
            settings_target.read_bytes(), live_after_app_edit, "reverse target"
        )

        recalculated = case.cli("plan", expected_code=0)
        recalculated_resources = {
            item.get("name"): item for item in self._resources(recalculated)
        }
        self.assertEqual(recalculated_resources["settings"].get("status"), "converged")
        self.assertEqual(recalculated_resources["nested_toml"].get("status"), "in_sync")
        before_confirmed_noop = case.snapshot()
        redeployed = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(redeployed, "no_changes")
        self._assert_snapshot_equal(
            case.snapshot(), before_confirmed_noop, "post-reverse confirmed no-op"
        )
        self._assert_bytes_equal(
            settings_target.read_bytes(),
            case.expected("settings.after-app.jsonc"),
            "application-edited JSONC target",
        )
        nested_target = case.target_root / ".config/demo/deep/profile/runtime.toml"
        self._assert_bytes_equal(
            nested_target.read_bytes(),
            case.expected("nested.after-reverse.toml"),
            "independent TOML input consumer",
        )
        no_op = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(no_op, "no_changes")

        nested_target = case.target_root / ".config/demo/deep/profile/runtime.toml"
        nested_before_app = nested_target.read_bytes()
        old_worker = b'"synthetic-worker-overlay-v1"'
        new_worker = b'"synthetic-worker-app-v2"'
        self.assertEqual(nested_before_app.count(old_worker), 1)
        nested_after_app = nested_before_app.replace(old_worker, new_worker, 1)
        nested_target.write_bytes(nested_after_app)
        local_runtime = case.input_root / "local" / "runtime.toml"
        local_runtime_before = local_runtime.read_bytes()

        nested_preview = case.cli(
            "reverse-sync",
            "--resource",
            "nested_toml",
            "--field",
            "worker_name",
            expected_code=2,
        )
        self._assert_outcome(nested_preview, "confirmation_required")
        self._assert_bytes_equal(
            local_runtime.read_bytes(),
            local_runtime_before,
            "nested reverse preview input",
        )
        self._assert_bytes_equal(
            nested_target.read_bytes(),
            nested_after_app,
            "nested reverse preview target",
        )
        nested_reverse = case.cli(
            "reverse-sync",
            "--resource",
            "nested_toml",
            "--field",
            "worker_name",
            "--yes",
            expected_code=0,
        )
        self._assert_outcome(nested_reverse, "committed")
        self._assert_bytes_equal(
            local_runtime.read_bytes(),
            case.expected("local-runtime.after-reverse.toml"),
            "reverse-updated nested TOML input",
        )
        self._assert_bytes_equal(
            nested_target.read_bytes(), nested_after_app, "nested reverse target"
        )
        self._assert_bytes_equal(
            nested_target.read_bytes(),
            case.expected("nested.after-worker-app.toml"),
            "nested application-edited target",
        )
        self.assertEqual(
            case.cli("apply", "--yes", expected_code=0).get("outcome"),
            "no_changes",
        )

    def test_copy_and_directory_source_updates_are_independent(self) -> None:
        case = self._case()
        self._accept_all(case)
        self._assert_outcome(case.cli("apply", "--yes", expected_code=0), "committed")

        (case.source_root / "copies" / "profile.ini").write_bytes(
            case.expected("copy-profile.after-source-update.ini")
        )
        before_copy_preview = case.snapshot()
        copy_preview = case.cli("apply", expected_code=2)
        self._assert_outcome(copy_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_copy_preview, "copy source preview"
        )
        copy_apply = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(copy_apply, "committed")
        self._assert_bytes_equal(
            (case.target_root / ".config/demo/profile.ini").read_bytes(),
            case.expected("copy-profile.after-source-update.ini"),
            "whole-file copy update",
        )

        defaults_source = case.source_root / "assets" / "defaults.conf"
        defaults_source.write_bytes(
            case.expected("assets-defaults.after-source-update.conf")
        )
        plan = case.cli("plan", expected_code=0)
        resources = {item.get("name"): item for item in self._resources(plan)}
        self.assertEqual(resources["assets:defaults.conf"].get("status"), "drifted")
        self.assertEqual(
            resources["assets:nested/keymap.jsonc"].get("status"), "in_sync"
        )
        before_directory_preview = case.snapshot()
        directory_preview = case.cli("apply", expected_code=2)
        self._assert_outcome(directory_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_directory_preview, "directory source preview"
        )
        directory_apply = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(directory_apply, "committed")
        self._assert_bytes_equal(
            (case.target_root / ".config/demo/assets/defaults.conf").read_bytes(),
            case.expected("assets-defaults.after-source-update.conf"),
            "directory leaf update",
        )
        self._assert_bytes_equal(
            (case.target_root / ".config/demo/assets/nested/keymap.jsonc").read_bytes(),
            case.expected("assets-nested-keymap.jsonc"),
            "unchanged directory leaf",
        )

    def test_source_owned_jsonc_input_update_requires_confirmation(self) -> None:
        case = self._case()
        self._accept_all(case)
        self._assert_outcome(case.cli("apply", "--yes", expected_code=0), "committed")
        global_settings = case.input_root / "global" / "settings.jsonc"
        global_settings.write_bytes(
            case.expected("global-settings.after-brand-update.jsonc")
        )
        before_preview = case.snapshot()
        preview = case.cli("apply", expected_code=2)
        self._assert_outcome(preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_preview, "input update preview"
        )
        applied = case.cli("apply", "--yes", expected_code=0)
        self._assert_outcome(applied, "committed")
        self._assert_bytes_equal(
            (case.target_root / ".config/demo/settings.jsonc").read_bytes(),
            case.expected("settings.after-brand-update.jsonc"),
            "source-owned JSONC update",
        )

    def test_shared_consumer_divergence_blocks_reverse_before_any_write(self) -> None:
        case = self._case()
        self._adopt(case, "copy_profile", profile="shared_consumer")
        self._accept(
            case,
            "settings_shared",
            ("theme", "brand"),
            profile="shared_consumer",
        )
        self._accept(
            case,
            "nested_toml_consumer",
            ("theme",),
            profile="shared_consumer",
        )
        self._assert_outcome(
            case.cli("apply", "--yes", profile="shared_consumer", expected_code=0),
            "committed",
        )
        plan = case.cli("plan", profile="shared_consumer", expected_code=0)
        resources = self._resources(plan)
        expected_names = tuple(
            line
            for line in case.expected("shared-consumer-resources.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        expected_targets = {
            line
            for line in case.expected("shared-consumer-targets.txt")
            .decode("utf-8")
            .splitlines()
            if line
        }
        self.assertEqual(
            tuple(sorted(str(item.get("name")) for item in resources)),
            expected_names,
        )
        self.assertEqual(
            {str(item.get("target")) for item in resources}, expected_targets
        )
        shared_target = case.target_root / ".config/demo/shared/runtime.toml"
        self._assert_bytes_equal(
            shared_target.read_bytes(),
            case.expected("nested-toml-consumer.initial.toml"),
            "unselected nested TOML consumer before reverse",
        )
        shared_settings_target = case.target_root / ".config/demo/shared/settings.jsonc"
        shared_settings_target.write_bytes(case.expected("settings.after-app.jsonc"))
        before = case.snapshot()

        preview = case.cli(
            "reverse-sync",
            "--resource",
            "settings_shared",
            "--field",
            "theme",
            expected_code=2,
            profile="shared_consumer",
        )
        self.assertEqual(self._error_code(preview), "reverse_consumer_change")
        self._assert_snapshot_equal(
            case.snapshot(), before, "shared-consumer reverse preview"
        )

        report = case.process_home / "shared-consumer-write-counts.json"
        confirmed = case.cli_with_engine_hook(
            MUTATION_COUNT_HOOK,
            report,
            "reverse-sync",
            "--resource",
            "settings_shared",
            "--field",
            "theme",
            "--yes",
            expected_code=2,
            profile="shared_consumer",
        )
        self.assertEqual(self._error_code(confirmed), "reverse_consumer_change")
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8")),
            {"create_parent": 0, "write_file": 0},
        )
        self._assert_snapshot_equal(
            case.snapshot(), before, "shared-consumer reverse confirmation"
        )

    def test_unknown_empty_containers_remain_observable_and_cannot_be_accepted(
        self,
    ) -> None:
        case = self._case()
        self._accept_all(case)
        self._assert_outcome(case.cli("apply", "--yes", expected_code=0), "committed")
        settings_target = case.target_root / ".config/demo/settings.jsonc"
        settings_target.write_bytes(case.expected("settings.unknown-container.jsonc"))
        before = case.snapshot()

        plan = case.cli("plan", expected_code=0)
        self._assert_snapshot_equal(case.snapshot(), before, "unknown-container plan")
        settings = next(
            item for item in self._resources(plan) if item.get("name") == "settings"
        )
        self.assertTrue(settings.get("undeclared_changed"))
        self.assertEqual(settings.get("status"), "review")
        self.assertEqual(settings.get("action"), "blocked")
        self.assertEqual(settings.get("error_code"), "review_required")
        self.assertNotIn(UNKNOWN_SENTINEL, json.dumps(plan, sort_keys=True))

        accepted = case.cli(
            "accept",
            "--resource",
            "settings",
            "--from",
            "desired",
            "--field",
            "theme",
            "--yes",
            expected_code=2,
        )
        self.assertEqual(self._error_code(accepted), "review_required")
        self._assert_snapshot_equal(case.snapshot(), before, "unknown-container accept")

        blocked = case.cli("apply", "--yes", expected_code=2)
        self.assertEqual(blocked.get("outcome"), "not_attempted")
        self.assertEqual(blocked.get("error_code"), "review_required")
        self._assert_snapshot_equal(case.snapshot(), before, "unknown-container apply")

    def test_duplicate_leaf_conflict_unknown_input_and_selected_alias_block(
        self,
    ) -> None:
        same_leaf = self._case()
        before_same_leaf = same_leaf.snapshot()
        same_leaf.replace_manifest(
            '[resources.settings.fields.brand]\npath = ["ui", "brand"]',
            '[resources.settings.fields.brand]\npath = ["ui", "theme"]',
        )
        same_leaf.cli("plan", expected_code=2)
        self._assert_snapshot_equal(
            same_leaf.snapshot(), before_same_leaf, "same-leaf block"
        )

        conflict = self._case()
        self._accept_all(conflict)
        self.assertEqual(
            conflict.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        settings_target = conflict.target_root / ".config/demo/settings.jsonc"
        live_bytes = settings_target.read_bytes()
        old_value = b'"synthetic-theme-overlay-v1"'
        self.assertEqual(live_bytes.count(old_value), 1)
        settings_target.write_bytes(
            live_bytes.replace(old_value, b'"synthetic-theme-live-v2"', 1)
        )
        local_settings = conflict.input_root / "local" / "settings-theme.jsonc"
        local_bytes = local_settings.read_bytes()
        self.assertEqual(local_bytes.count(old_value), 1)
        local_settings.write_bytes(
            local_bytes.replace(old_value, b'"synthetic-theme-source-v2"', 1)
        )
        before_conflict_mutations = conflict.snapshot()
        conflict.cli(
            "reverse-sync",
            "--resource",
            "settings",
            "--field",
            "theme",
            "--yes",
            expected_code=2,
        )
        conflict.cli("apply", "--yes", expected_code=2)
        self._assert_snapshot_equal(
            conflict.snapshot(), before_conflict_mutations, "conflict block"
        )

        unknown = self._case()
        self._accept_all(unknown)
        self.assertEqual(
            unknown.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        bad_input = unknown.input_root / "local" / "settings-theme.jsonc"
        bad_input.write_bytes(b"\xff")
        before_unknown_apply = unknown.snapshot()
        unknown.cli("apply", "--yes", expected_code=2)
        self._assert_snapshot_equal(
            unknown.snapshot(), before_unknown_apply, "unknown input block"
        )

        alias = self._case()
        before_alias = alias.snapshot()
        alias.cli("plan", profile="alias", expected_code=2)
        self._assert_snapshot_equal(
            alias.snapshot(), before_alias, "selected alias block"
        )

    def test_comment_only_stale_input_blocks_confirm_and_ablation_is_caught(
        self,
    ) -> None:
        case = self._case()
        self._accept_all(case)
        self.assertEqual(
            case.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        settings_target = case.target_root / ".config/demo/settings.jsonc"
        target_before = settings_target.read_bytes()
        global_settings = case.input_root / "global" / "settings.jsonc"
        global_settings.write_bytes(
            case.expected("global-settings.after-brand-update.jsonc")
        )
        before_preview = case.snapshot()
        case.cli("apply", expected_code=2)
        self._assert_snapshot_equal(
            case.snapshot(), before_preview, "comment-race preview"
        )

        blocked = case.cli_with_engine_hook(
            COMMENT_RACE_HOOK.replace("ABLATEME", ""),
            global_settings,
            "apply",
            "--yes",
            expected_code=2,
        )
        self.assertNotEqual(blocked.get("outcome"), "committed")
        self._assert_bytes_equal(
            settings_target.read_bytes(), target_before, "stale comment race target"
        )
        self._assert_bytes_equal(
            global_settings.read_bytes(),
            case.expected("global-settings.comment-race.jsonc"),
            "raced comment-only input",
        )

        ablated = self._case()
        self._accept_all(ablated)
        self.assertEqual(
            ablated.cli("apply", "--yes", expected_code=0).get("outcome"),
            "committed",
        )
        ablated_target = ablated.target_root / ".config/demo/settings.jsonc"
        ablated_global = ablated.input_root / "global" / "settings.jsonc"
        ablated_global.write_bytes(
            ablated.expected("global-settings.after-brand-update.jsonc")
        )
        ablated_program = COMMENT_RACE_HOOK.replace(
            "ABLATEME",
            "engine._assert_fresh = lambda record, **_kwargs: record.resources",
        )
        ablated.cli_with_engine_hook(
            ablated_program,
            ablated_global,
            "apply",
            "--yes",
            expected_code=2,
        )
        # This separate control proves the fixture byte oracle catches a disabled
        # freshness guard: the raced command changes the target before failing.
        self._assert_bytes_equal(
            ablated_target.read_bytes(),
            ablated.expected("settings.after-brand-update.jsonc"),
            "freshness ablation control target",
        )

    def test_official_application_shapes_reverse_and_redeploy_from_fixtures(
        self,
    ) -> None:
        case = self._case()
        profile = "official_shapes"
        codex_hash_initial = (
            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        )
        codex_hash_after_app = (
            "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        )
        planned = case.cli("plan", expected_code=0, profile=profile)
        self.assertNotIn(codex_hash_initial, json.dumps(planned))
        expected_resources = tuple(
            line
            for line in case.expected("official-shapes-resources.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        expected_targets = {
            line
            for line in case.expected("official-shapes-targets.txt")
            .decode("utf-8")
            .splitlines()
            if line
        }
        selected = self._resources(planned)
        selected_names = tuple(item.get("name") for item in selected)
        self.assertEqual(selected_names, expected_resources)
        self.assertEqual(len(selected_names), len(set(selected_names)))
        selected_targets = tuple(item.get("target") for item in selected)
        self.assertEqual(set(selected_targets), expected_targets)
        self.assertEqual(len(selected_targets), len(expected_targets))
        self.assertEqual(planned.get("packages"), ["official_apps"])
        self.assertEqual(planned.get("excluded"), [])

        codex_accept = self._accept(
            case,
            "codex",
            ("project_trust", "hook_enabled", "hook_trusted_hash"),
            profile=profile,
        )
        self.assertNotIn(codex_hash_initial, json.dumps(codex_accept))
        self._accept(
            case,
            "opencode",
            ("schema", "model", "autoupdate", "port", "hostname"),
            profile=profile,
        )
        self._accept(
            case,
            "droid",
            ("interaction_mode", "autonomy_level"),
            profile=profile,
        )

        before_apply = case.snapshot()
        apply_preview = case.cli("apply", expected_code=2, profile=profile)
        self._assert_outcome(apply_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_apply, "official apply preview"
        )
        applied = case.cli("apply", "--yes", expected_code=0, profile=profile)
        self._assert_outcome(applied, "committed")
        self.assertNotIn(codex_hash_initial, json.dumps(applied))

        codex_target = case.target_root / ".codex/config.toml"
        opencode_target = case.target_root / ".config/opencode/opencode.jsonc"
        self._assert_bytes_equal(
            codex_target.read_bytes(), case.expected("codex.initial.toml"), "Codex TOML"
        )
        self._assert_bytes_equal(
            opencode_target.read_bytes(),
            case.expected("opencode.initial.jsonc"),
            "OpenCode JSONC",
        )
        droid_target = case.target_root / ".factory/settings.json"
        self._assert_bytes_equal(
            droid_target.read_bytes(),
            case.expected("droid-settings.initial.json"),
            "Droid settings JSON",
        )
        hook_id = "/synthetic/luwu-demo/hooks.json:pre_tool_use:0:0"
        initial_codex = codex_target.read_text(encoding="utf-8")
        self.assertEqual(initial_codex.count(f'[hooks.state."{hook_id}"]'), 1)
        self.assertEqual(initial_codex.count(codex_hash_initial), 1)
        self.assertIn('[projects."/synthetic/luwu-demo/project.a"]', initial_codex)
        self.assertIn('trust_level = "untrusted"', initial_codex)
        self.assertIn("enabled = false", initial_codex)

        # These fixture byte replacements model an application editing its own files.
        codex_target.write_bytes(case.expected("codex.after-app.toml"))
        opencode_target.write_bytes(case.expected("opencode.after-app.jsonc"))
        droid_target.write_bytes(case.expected("droid-settings.after-app.json"))
        codex_local = case.input_root / "local/codex-runtime.toml"
        opencode_local = case.input_root / "local/opencode.jsonc"
        droid_local = case.input_root / "local/droid-settings.json"
        opencode_local_before = opencode_local.read_bytes()
        droid_local_before = droid_local.read_bytes()
        codex_live_after_app = codex_target.read_bytes()
        opencode_live_after_app = opencode_target.read_bytes()
        droid_live_after_app = droid_target.read_bytes()
        before_reverse = case.snapshot()

        codex_preview = case.cli(
            "reverse-sync",
            "--resource",
            "codex",
            *self._field_arguments(
                ("project_trust", "hook_enabled", "hook_trusted_hash")
            ),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(codex_preview, "confirmation_required")
        self.assertNotIn(codex_hash_after_app, json.dumps(codex_preview))
        self._assert_snapshot_equal(
            case.snapshot(), before_reverse, "Codex reverse preview"
        )

        codex_reverse = case.cli(
            "reverse-sync",
            "--resource",
            "codex",
            *self._field_arguments(
                ("project_trust", "hook_enabled", "hook_trusted_hash")
            ),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(codex_reverse, "committed")
        self.assertNotIn(codex_hash_after_app, json.dumps(codex_reverse))
        self._assert_bytes_equal(
            codex_local.read_bytes(),
            case.expected("codex-local.after-reverse.toml"),
            "Codex reverse input",
        )
        self._assert_bytes_equal(
            codex_target.read_bytes(), codex_live_after_app, "Codex reverse target"
        )
        self.assertEqual(
            codex_local.read_text(encoding="utf-8").count(codex_hash_after_app), 1
        )

        before_opencode_reverse = case.snapshot()
        opencode_preview = case.cli(
            "reverse-sync",
            "--resource",
            "opencode",
            *self._field_arguments(("model", "autoupdate", "port", "hostname")),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(opencode_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_opencode_reverse, "OpenCode reverse preview"
        )
        self._assert_bytes_equal(
            opencode_local.read_bytes(), opencode_local_before, "OpenCode preview input"
        )
        self._assert_bytes_equal(
            opencode_target.read_bytes(),
            opencode_live_after_app,
            "OpenCode preview target",
        )

        opencode_reverse = case.cli(
            "reverse-sync",
            "--resource",
            "opencode",
            *self._field_arguments(("model", "autoupdate", "port", "hostname")),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(opencode_reverse, "committed")
        self._assert_bytes_equal(
            opencode_local.read_bytes(),
            case.expected("opencode-local.after-reverse.jsonc"),
            "OpenCode reverse input",
        )
        self._assert_bytes_equal(
            opencode_target.read_bytes(),
            opencode_live_after_app,
            "OpenCode reverse target",
        )

        before_droid_reverse = case.snapshot()
        droid_preview = case.cli(
            "reverse-sync",
            "--resource",
            "droid",
            *self._field_arguments(("interaction_mode", "autonomy_level")),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(droid_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_droid_reverse, "Droid reverse preview"
        )
        self._assert_bytes_equal(
            droid_local.read_bytes(), droid_local_before, "Droid preview input"
        )
        self._assert_bytes_equal(
            droid_target.read_bytes(), droid_live_after_app, "Droid preview target"
        )

        droid_reverse = case.cli(
            "reverse-sync",
            "--resource",
            "droid",
            *self._field_arguments(("interaction_mode", "autonomy_level")),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(droid_reverse, "committed")
        self._assert_bytes_equal(
            droid_local.read_bytes(),
            case.expected("droid-settings.local.after-reverse.json"),
            "Droid reverse input",
        )
        self._assert_bytes_equal(
            droid_target.read_bytes(), droid_live_after_app, "Droid reverse target"
        )

        recalculated = case.cli("plan", expected_code=0, profile=profile)
        self.assertNotIn(codex_hash_after_app, json.dumps(recalculated))
        recalculated_by_name = {
            item.get("name"): item for item in self._resources(recalculated)
        }
        self.assertEqual(set(recalculated_by_name), set(expected_resources))
        self.assertEqual(recalculated_by_name["codex"].get("status"), "converged")
        self.assertEqual(recalculated_by_name["opencode"].get("status"), "converged")
        self.assertEqual(recalculated_by_name["droid"].get("status"), "converged")
        before_confirmed_noop = case.snapshot()
        redeployed = case.cli("apply", "--yes", expected_code=0, profile=profile)
        self._assert_outcome(redeployed, "no_changes")
        self.assertNotIn(codex_hash_after_app, json.dumps(redeployed))
        self._assert_snapshot_equal(
            case.snapshot(), before_confirmed_noop, "official-shape confirmed no-op"
        )
        self._assert_bytes_equal(
            codex_target.read_bytes(),
            case.expected("codex.after-app.toml"),
            "Codex app edit",
        )
        self._assert_bytes_equal(
            opencode_target.read_bytes(),
            case.expected("opencode.after-app.jsonc"),
            "OpenCode app edit",
        )
        self._assert_bytes_equal(
            droid_target.read_bytes(),
            case.expected("droid-settings.after-app.json"),
            "Droid settings app edit",
        )

    def test_droid_settings_bidirectional_fixture_excludes_workspace_trust(
        self,
    ) -> None:
        case = self._case()
        profile = "droid_settings"
        planned = case.cli("plan", expected_code=0, profile=profile)
        expected_resources = tuple(
            line
            for line in case.expected("droid-settings-resources.txt")
            .decode("utf-8")
            .splitlines()
            if line
        )
        expected_targets = {
            line
            for line in case.expected("droid-settings-targets.txt")
            .decode("utf-8")
            .splitlines()
            if line
        }
        resources = self._resources(planned)
        self.assertEqual(
            tuple(item.get("name") for item in resources), expected_resources
        )
        self.assertEqual({item.get("target") for item in resources}, expected_targets)
        self.assertEqual(planned.get("packages"), ["official_droid"])
        self.assertEqual(planned.get("excluded"), [])
        self.assertEqual(expected_targets, {".factory/settings.json"})

        fields = ("interaction_mode", "autonomy_level")
        self._accept(case, "droid", fields, profile=profile)
        before_apply = case.snapshot()
        apply_preview = case.cli("apply", expected_code=2, profile=profile)
        self._assert_outcome(apply_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_apply, "Droid settings apply preview"
        )
        applied = case.cli("apply", "--yes", expected_code=0, profile=profile)
        self._assert_outcome(applied, "committed")

        target = case.target_root / ".factory/settings.json"
        local = case.input_root / "local/droid-settings.json"
        self._assert_bytes_equal(
            target.read_bytes(),
            case.expected("droid-settings.initial.json"),
            "Droid settings initial target",
        )
        local_before = local.read_bytes()
        target.write_bytes(case.expected("droid-settings.after-app.json"))
        app_edited_target = target.read_bytes()
        before_reverse = case.snapshot()

        reverse_preview = case.cli(
            "reverse-sync",
            "--resource",
            "droid",
            *self._field_arguments(fields),
            expected_code=2,
            profile=profile,
        )
        self._assert_outcome(reverse_preview, "confirmation_required")
        self._assert_snapshot_equal(
            case.snapshot(), before_reverse, "Droid settings reverse preview"
        )
        self._assert_bytes_equal(
            local.read_bytes(), local_before, "Droid settings preview input"
        )

        reverse = case.cli(
            "reverse-sync",
            "--resource",
            "droid",
            *self._field_arguments(fields),
            "--yes",
            expected_code=0,
            profile=profile,
        )
        self._assert_outcome(reverse, "committed")
        self._assert_bytes_equal(
            local.read_bytes(),
            case.expected("droid-settings.local.after-reverse.json"),
            "Droid settings reverse input",
        )
        self._assert_bytes_equal(
            target.read_bytes(), app_edited_target, "Droid settings reverse target"
        )

        recalculated = case.cli("plan", expected_code=0, profile=profile)
        selected_after_reverse = self._resources(recalculated)
        self.assertEqual(len(selected_after_reverse), 1)
        self.assertEqual(selected_after_reverse[0].get("name"), "droid")
        self.assertEqual(selected_after_reverse[0].get("status"), "converged")
        before_noop = case.snapshot()
        redeployed = case.cli("apply", "--yes", expected_code=0, profile=profile)
        self._assert_outcome(redeployed, "no_changes")
        self._assert_snapshot_equal(
            case.snapshot(), before_noop, "Droid settings confirmed no-op"
        )
        self._assert_bytes_equal(
            target.read_bytes(),
            case.expected("droid-settings.after-app.json"),
            "Droid settings after redeploy",
        )
