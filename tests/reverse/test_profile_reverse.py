"""Tests for profile reverse."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.engine import (
    EngineError,
    accept_baseline,
    adopt_resource,
    apply_plan,
    reverse_inputs,
)
from luwu.filesystem import FileObservation
from luwu.manifest import Manifest, Resource
from tests.support import profile_case

_CheckInputs = Callable[[], None] | None


class ProfileReverseTests(profile_case.ProfileEngineCase):
    def test_declared_conflict_can_be_accepted_without_target_write(self) -> None:
        self._deploy_template()
        target = self._target()
        changed_live = json.loads(target.read_text(encoding="utf-8"))
        changed_live["settings"]["theme"] = "live-theme-v3"
        target.write_text(json.dumps(changed_live) + "\n", encoding="utf-8")
        local = self.inputs / "local" / "settings.json"
        changed_input = json.loads(local.read_text(encoding="utf-8"))
        changed_input["overrides"]["theme"] = "overlay-theme-v2"
        local.write_text(json.dumps(changed_input) + "\n", encoding="utf-8")
        plan = self._plan()
        self.assertEqual(plan.to_dict()["resources"][0]["status"], "conflict")
        self.assertFalse(plan.to_dict()["resources"][0]["undeclared_changed"])
        before_target = target.read_bytes()

        accepted = accept_baseline(
            plan,
            resource="settings",
            fields=("theme",),
            value_from="live",
            confirm=True,
        )

        self.assertEqual(accepted.outcome, "committed")
        self.assertEqual(accepted.verification_status, "verified")
        self.assertEqual(target.read_bytes(), before_target)
        baseline = json.loads(
            (self.inputs / "baselines" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(baseline["values"]["theme"], "live-theme-v3")

    def test_unknown_tree_changes_are_reported_separately_from_declared_fields(
        self,
    ) -> None:
        self._deploy_template()
        target = self._target()
        before = target.read_text(encoding="utf-8")
        target.write_text(
            before.replace("unmanaged-source-v1", "live-unknown-secret-sentinel"),
            encoding="utf-8",
        )

        plan = self._plan()
        resource = plan.to_dict()["resources"][0]
        self.assertTrue(resource["undeclared_changed"])
        self.assertEqual(resource["status"], "review")
        self.assertEqual(resource["action"], "blocked")
        self.assertEqual(resource["error_code"], "review_required")
        self.assertEqual(
            resource["reason"],
            "undeclared content changed; inspect declared scope or correct it manually, then re-plan",
        )
        ownership = resource["ownership"]
        assert isinstance(ownership, dict)
        self.assertTrue(ownership["fields"])
        encoded = json.dumps(plan.to_dict(), sort_keys=True)
        self.assertNotIn("live-unknown-secret-sentinel", encoded)
        self.assertNotIn("unmanaged", encoded)
        before = self._snapshot()
        with self.assertRaises(EngineError) as raised:
            accept_baseline(
                plan,
                resource="settings",
                fields=("theme",),
                value_from="desired",
                confirm=True,
            )
        self.assertEqual(raised.exception.code, "review_required")
        self.assertEqual(self._snapshot(), before)
        apply_result = apply_plan(plan, confirm=True)
        self.assertEqual(apply_result.outcome, "not_attempted")
        self.assertEqual(apply_result.error_code, "review_required")
        self.assertTrue(
            all(action.state == "not_attempted" for action in apply_result.actions)
        )
        self.assertEqual(self._snapshot(), before)

    def test_reverse_is_explicit_grouped_and_verified_without_target_write(
        self,
    ) -> None:
        # A checkout ancestor named "local" must not reclassify global inputs.
        self.root = self.root / "checkout" / "local" / "project"
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.home = self.root / "home"
        self.manifest_path = self.root / "luwu.toml"
        for path in (
            self.source / "templates",
            self.inputs / "global",
            self.inputs / "local",
            self.inputs / "baselines",
            self.home,
        ):
            path.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(self.home)
        self._write_template_case()

        self._deploy_template()
        target = self._target()
        before_target = target.read_bytes()
        changed = json.loads(before_target)
        changed["settings"]["theme"] = "app-theme-v2"
        target.write_text(json.dumps(changed) + "\n", encoding="utf-8")
        local = self.inputs / "local" / "settings.json"
        before_local = local.read_bytes()
        before_target = target.read_bytes()
        before = self._snapshot()

        preview = reverse_inputs(
            self._plan(),
            resource="settings",
            fields=("theme",),
            confirm=False,
        )
        self.assertEqual(preview.outcome, "confirmation_required")
        self.assertTrue(
            all(action.state == "not_attempted" for action in preview.actions)
        )
        self.assertEqual(self._snapshot(), before)

        result = reverse_inputs(
            self._plan(),
            resource="settings",
            fields=("theme",),
            confirm=True,
        )
        self.assertEqual(result.outcome, "committed")
        self.assertEqual(result.verification_status, "verified")
        self.assertEqual(target.read_bytes(), before_target)
        self.assertNotEqual(local.read_bytes(), before_local)
        self.assertEqual(
            json.loads(local.read_text(encoding="utf-8"))["overrides"]["theme"],
            "app-theme-v2",
        )
        self.assertNotIn("app-theme-v2", json.dumps(result.to_dict()))

    def test_same_resource_conflict_blocks_reverse_and_deletion_is_rejected(
        self,
    ) -> None:
        self._deploy_template()
        target = self._target()
        live = json.loads(target.read_text(encoding="utf-8"))
        live["settings"]["theme"] = "live-conflict-v2"
        target.write_text(json.dumps(live) + "\n", encoding="utf-8")
        local = self.inputs / "local" / "settings.json"
        source_input = json.loads(local.read_text(encoding="utf-8"))
        source_input["overrides"]["theme"] = "input-conflict-v3"
        local.write_text(json.dumps(source_input) + "\n", encoding="utf-8")
        before = self._snapshot()
        plan = self._plan()
        self.assertEqual(plan.to_dict()["resources"][0]["status"], "conflict")
        with self.assertRaises(EngineError) as raised:
            reverse_inputs(
                plan,
                resource="settings",
                fields=("theme",),
                confirm=True,
            )
        self.assertEqual(raised.exception.code, "review_required")
        self.assertEqual(self._snapshot(), before)

        target = self._target()
        live = json.loads(target.read_text(encoding="utf-8"))
        del live["settings"]["theme"]
        target.write_text(json.dumps(live) + "\n", encoding="utf-8")
        source_input = json.loads(local.read_text(encoding="utf-8"))
        source_input["overrides"]["theme"] = "overlay-theme-v1"
        local.write_text(json.dumps(source_input) + "\n", encoding="utf-8")
        before = self._snapshot()
        with self.assertRaises(EngineError) as raised:
            reverse_inputs(
                self._plan(),
                resource="settings",
                fields=("theme",),
                confirm=True,
            )
        self.assertEqual(raised.exception.code, "reverse_delete")
        self.assertEqual(self._snapshot(), before)

    def test_old_tampered_and_identity_stale_baselines_block(self) -> None:
        self._accept_template_baseline()
        baseline_path = self.inputs / "baselines" / "settings.json"
        valid = json.loads(baseline_path.read_text(encoding="utf-8"))
        valid["values"]["undeclared"] = "tampered-value"
        baseline_path.write_text(json.dumps(valid), encoding="utf-8")
        plan = self._plan()
        resource = plan.to_dict()["resources"][0]
        self.assertEqual(resource["status"], "blocked")
        self.assertEqual(resource["error_code"], "baseline_invalid")

        baseline_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "resource": "settings",
                    "source": "templates/settings.json.j2",
                    "target": ".config/app/settings.json",
                    "owners": {"theme": "merge", "brand": "source"},
                    "values": {"theme": "old"},
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            self._plan().to_dict()["resources"][0]["error_code"], "baseline_invalid"
        )

        baseline_path.unlink()
        self._accept_template_baseline()
        body = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(
            body.replace(
                'path = ["settings", "theme"]', 'path = ["settings", "renamed"]'
            ),
            encoding="utf-8",
        )
        stale = self._plan().to_dict()["resources"][0]
        self.assertEqual(stale["status"], "blocked")
        self.assertEqual(stale["error_code"], "baseline_stale")

    def test_copy_adoption_is_baseline_only_and_live_changes_conflict(self) -> None:
        (self.source / "note.txt").write_text("source-note-v2\n", encoding="utf-8")
        (self.home / "note.txt").write_text("live-note-v1\n", encoding="utf-8")
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
resources = ["note"]
[inputs]
[resources.note]
kind = "copy"
source = "note.txt"
target = "note.txt"
baseline = "baselines/note.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
""",
            encoding="utf-8",
        )
        target = self.home / "note.txt"
        before_target = target.read_bytes()
        before = self._snapshot()
        preview = adopt_resource(self._plan(), resource="note", confirm=False)
        self.assertEqual(preview.outcome, "confirmation_required")
        self.assertEqual(self._snapshot(), before)
        adopted = adopt_resource(self._plan(), resource="note", confirm=True)
        self.assertEqual(adopted.outcome, "committed")
        self.assertEqual(target.read_bytes(), before_target)
        baseline_path = self.inputs / "baselines" / "note.json"
        baseline = json.loads(baseline_path.read_text())
        self.assertEqual(set(baseline["values"]), {"whole_file", "mode"})
        self.assertEqual(set(baseline["values"]["whole_file"]), {"algorithm", "digest"})
        self.assertEqual(baseline["values"]["whole_file"]["algorithm"], "sha256")
        accepted_digest = baseline["values"]["whole_file"]["digest"]
        self.assertTrue(
            len(accepted_digest) == 64
            and all(character in "0123456789abcdef" for character in accepted_digest)
        )
        self.assertEqual(
            baseline["values"]["mode"], stat.S_IMODE(target.stat().st_mode)
        )
        for mutation in ("algorithm", "unknown_key"):
            tampered = json.loads(json.dumps(baseline))
            if mutation == "algorithm":
                tampered["values"]["whole_file"]["algorithm"] = "sha512"
            else:
                tampered["values"]["whole_file"][mutation] = "ignored"
            baseline_path.write_text(json.dumps(tampered), encoding="utf-8")
            blocked = self._plan().to_dict()["resources"][0]
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["error_code"], "baseline_invalid")
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        accepted_mode = stat.S_IMODE(target.stat().st_mode)
        target.chmod(0o600)
        self.assertEqual(self._plan().to_dict()["resources"][0]["status"], "conflict")
        target.chmod(accepted_mode)

        target.write_text("live-note-v3\n", encoding="utf-8")
        conflict_plan = self._plan()
        self.assertEqual(conflict_plan.to_dict()["resources"][0]["status"], "conflict")
        before = self._snapshot()
        with self.assertRaises(EngineError):
            apply_plan(conflict_plan, confirm=True)
        self.assertEqual(self._snapshot(), before)

        adopt_resource(self._plan(), resource="note", confirm=True)
        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(result.outcome, "committed")
        self.assertEqual(target.read_text(encoding="utf-8"), "source-note-v2\n")

    def test_directory_leaf_baselines_are_stable_across_leaf_content_updates(
        self,
    ) -> None:
        assets = self.source / "assets"
        (assets / "nested").mkdir(parents=True)
        defaults = assets / "defaults.conf"
        keymap = assets / "nested" / "keymap.jsonc"
        defaults.write_text("theme=one\n", encoding="utf-8")
        keymap.write_text('{"key":"value"}\n', encoding="utf-8")
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
kind = "directory"
source = "assets"
target = ".config/assets"
baseline = "baselines/assets"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
""",
            encoding="utf-8",
        )

        for resource_name in (
            "asset_tree:defaults.conf",
            "asset_tree:nested/keymap.jsonc",
        ):
            result = adopt_resource(self._plan(), resource=resource_name, confirm=True)
            self.assertEqual(result.outcome, "committed")

        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        defaults_target = self.home / ".config/assets/defaults.conf"
        keymap_target = self.home / ".config/assets/nested/keymap.jsonc"
        before_keymap = keymap_target.read_bytes()
        baseline_files = tuple((self.inputs / "baselines").glob("assets.leaf-*"))
        self.assertEqual(len(baseline_files), 2)
        for path in baseline_files:
            identity = json.loads(path.read_text(encoding="utf-8"))["identity"]
            self.assertNotIn("expansion", identity)
            self.assertNotIn("packages", identity)
            self.assertNotIn("excluded", identity)

        old_plan = self._plan()
        defaults.write_text("theme=two-and-updated\n", encoding="utf-8")
        stale = apply_plan(old_plan, confirm=True)
        self.assertEqual(stale.outcome, "not_attempted")
        self.assertEqual(stale.error_code, "stale_plan")
        self.assertEqual(defaults_target.read_text(encoding="utf-8"), "theme=one\n")

        fresh_plan = self._plan()
        resources = {item.resource.name: item for item in fresh_plan._resources}
        self.assertEqual(resources["asset_tree:defaults.conf"].status, "drifted")
        self.assertEqual(resources["asset_tree:nested/keymap.jsonc"].status, "in_sync")
        self.assertTrue(all(item.error_code is None for item in fresh_plan._resources))

        updated = apply_plan(fresh_plan, confirm=True)
        self.assertEqual(updated.outcome, "committed")
        self.assertEqual(
            defaults_target.read_text(encoding="utf-8"), "theme=two-and-updated\n"
        )
        self.assertEqual(keymap_target.read_bytes(), before_keymap)

    def test_shared_reverse_document_is_written_once(self) -> None:
        (self.source / "templates" / "shared.json.j2").write_text(
            '{"values":{"first":{{ inputs.first | tojson }},'
            '"second":{{ inputs.second | tojson }}}}\n',
            encoding="utf-8",
        )
        shared = self.inputs / "local" / "shared.json"
        shared.write_text('{"values":{"first":"first-v1","second":"second-v1"}}\n')
        self.manifest_path.write_text(
            """
version = 8
[roots]
source = "source"
inputs = "inputs"
target = "home"
[profiles.work]
packages = ["editor"]
exclude = []
[packages.editor]
dependencies = []
resources = ["combined"]
[inputs.first]
layers = [{ file = "local/shared.json", path = ["values", "first"], format = "json" }]
reverse_to = { file = "local/shared.json", path = ["values", "first"], format = "json" }
[inputs.second]
layers = [{ file = "local/shared.json", path = ["values", "second"], format = "json" }]
reverse_to = { file = "local/shared.json", path = ["values", "second"], format = "json" }
[resources.combined]
kind = "template"
source = "templates/shared.json.j2"
target = "combined.json"
format = "json"
scope = "fields"
baseline = "baselines/combined.json"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
[resources.combined.fields.first]
path = ["values", "first"]
owner = "merge"
input = "first"
[resources.combined.fields.second]
path = ["values", "second"]
owner = "merge"
input = "second"
""",
            encoding="utf-8",
        )
        accepted = accept_baseline(
            self._plan(),
            resource="combined",
            fields=("first", "second"),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(accepted.outcome, "committed")
        apply_plan(self._plan(), confirm=True)
        target = self.home / "combined.json"
        live = json.loads(target.read_text(encoding="utf-8"))
        live["values"].update(first="app-first-v2", second="app-second-v2")
        target.write_text(json.dumps(live) + "\n", encoding="utf-8")

        original_write = engine_module.write_file
        write_paths: list[Path] = []

        def count_write(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            write_paths.append(path)
            return original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )

        with patch("luwu.engine.write_file", side_effect=count_write):
            result = reverse_inputs(
                self._plan(),
                resource="combined",
                fields=("first", "second"),
                confirm=True,
            )
        self.assertEqual(result.outcome, "committed")
        self.assertEqual(result.verification_status, "verified")
        self.assertEqual(write_paths, [shared])
        after = json.loads(shared.read_text(encoding="utf-8"))
        self.assertEqual(after["values"]["first"], "app-first-v2")
        self.assertEqual(after["values"]["second"], "app-second-v2")

    def test_accept_and_adopt_writer_callbacks_keep_authorized_baseline_bytes(
        self,
    ) -> None:
        plan = self._plan()
        original_write = engine_module.write_baseline
        written_data: list[bytes] = []

        def write_then_poison_caller_plan(
            manifest: Manifest,
            resource: Resource,
            *,
            data: bytes,
            expected: FileObservation,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_write(
                manifest,
                resource,
                data=data,
                expected=expected,
                check_inputs=check_inputs,
            )
            written_data.append(data)
            object.__setattr__(plan._resources[0], "write_data", b"FORGED_BASELINE")
            return identity

        with patch(
            "luwu.engine.write_baseline", side_effect=write_then_poison_caller_plan
        ):
            accepted = accept_baseline(
                plan,
                resource="settings",
                fields=("theme",),
                value_from="desired",
                confirm=True,
            )

        accepted_path = self.inputs / "baselines" / "settings.json"
        self.assertEqual(accepted.outcome, "committed")
        self.assertEqual(accepted_path.read_bytes(), written_data[0])
        self.assertNotIn(b"FORGED_BASELINE", accepted_path.read_bytes())

        (self.source / "note.txt").write_text("source-note-v1\n", encoding="utf-8")
        (self.home / "note.txt").write_text("live-note-v1\n", encoding="utf-8")
        self._configure_copy_batch(("note",))
        plan = self._plan()
        with patch(
            "luwu.engine.write_baseline", side_effect=write_then_poison_caller_plan
        ):
            adopted = adopt_resource(plan, resource="note", confirm=True)

        adopted_path = self.inputs / "baselines" / "note.json"
        self.assertEqual(adopted.outcome, "committed")
        self.assertEqual(adopted_path.read_bytes(), written_data[1])
        self.assertNotIn(b"FORGED_BASELINE", adopted_path.read_bytes())
        self.assertEqual((self.home / "note.txt").read_bytes(), b"live-note-v1\n")

    def test_reverse_writer_callback_keeps_authorized_input_bytes(self) -> None:
        self._deploy_template()
        target = self._target()
        live = json.loads(target.read_text(encoding="utf-8"))
        live["settings"]["theme"] = "live-theme-v2"
        target.write_text(json.dumps(live) + "\n", encoding="utf-8")

        plan = self._plan()
        input_path = self.inputs / "local" / "settings.json"
        original_write = engine_module.write_file
        written_data: list[bytes] = []

        def write_then_poison_caller_plan(
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
            if path == input_path:
                written_data.append(data)
                object.__setattr__(plan._resources[0], "write_data", b"FORGED_INPUT")
            return identity

        with patch("luwu.engine.write_file", side_effect=write_then_poison_caller_plan):
            result = reverse_inputs(
                plan,
                resource="settings",
                fields=("theme",),
                confirm=True,
            )

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(result.verification_status, "verified")
        self.assertEqual(input_path.read_bytes(), written_data[0])
        self.assertNotIn(b"FORGED_INPUT", input_path.read_bytes())
        self.assertEqual(
            json.loads(input_path.read_text(encoding="utf-8"))["overrides"]["theme"],
            "live-theme-v2",
        )

    def test_accept_and_adopt_preserve_commit_when_postwrite_observation_fails(
        self,
    ) -> None:
        original_write = engine_module.write_baseline
        original_observe = engine_module.observe_file
        baseline_path = self.inputs / "baselines" / "settings.json"
        write_returned = False

        def write_baseline_then_arm(
            manifest: Manifest,
            resource: Resource,
            *,
            data: bytes,
            expected: FileObservation,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            nonlocal write_returned
            identity = original_write(
                manifest,
                resource,
                data=data,
                expected=expected,
                check_inputs=check_inputs,
            )
            write_returned = True
            return identity

        def observe_accept_write(root: Path, path: Path):
            if write_returned and path == baseline_path:
                raise engine_module.FilesystemError(
                    "private injected failure", code="observe_failed"
                )
            return original_observe(root, path)

        with (
            patch(
                "luwu.engine.write_baseline",
                side_effect=write_baseline_then_arm,
            ),
            patch("luwu.engine.observe_file", side_effect=observe_accept_write),
        ):
            accepted = accept_baseline(
                self._plan(),
                resource="settings",
                fields=("theme",),
                value_from="desired",
                confirm=True,
            )
        accept_action = next(
            action for action in accepted.actions if action.kind == "baseline_write"
        )
        self.assertEqual(accepted.outcome, "committed_but_verification_failed")
        self.assertEqual(accept_action.state, "committed")
        self.assertTrue(baseline_path.exists())

        (self.source / "note.txt").write_text("source-v1\n", encoding="utf-8")
        target = self.home / "note.txt"
        target.write_text("live-v1\n", encoding="utf-8")
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
resources = ["note"]
[inputs]
[resources.note]
kind = "copy"
source = "note.txt"
target = "note.txt"
baseline = "baselines/note.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
""",
            encoding="utf-8",
        )
        adopt_path = self.inputs / "baselines" / "note.json"
        write_returned = False

        def observe_adopt_write(root: Path, path: Path):
            if write_returned and path == adopt_path:
                raise engine_module.FilesystemError(
                    "private injected failure", code="observe_failed"
                )
            return original_observe(root, path)

        with (
            patch(
                "luwu.engine.write_baseline",
                side_effect=write_baseline_then_arm,
            ),
            patch("luwu.engine.observe_file", side_effect=observe_adopt_write),
        ):
            adopted = adopt_resource(self._plan(), resource="note", confirm=True)
        adopt_action = next(
            action for action in adopted.actions if action.kind == "baseline_write"
        )
        self.assertEqual(adopted.outcome, "committed_but_verification_failed")
        self.assertEqual(adopt_action.state, "committed")
        self.assertTrue(adopt_path.exists())

    def test_reverse_preserves_commit_when_postwrite_observation_fails(self) -> None:
        self._deploy_template()
        target = self._target()
        live = json.loads(target.read_text(encoding="utf-8"))
        live["settings"]["theme"] = "live-theme-v2"
        target.write_text(json.dumps(live) + "\n", encoding="utf-8")
        input_path = self.inputs / "local" / "settings.json"
        original_write = engine_module.write_file
        original_observe = engine_module.observe_file
        write_returned = False

        def write_then_arm(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            nonlocal write_returned
            identity = original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )
            if path == input_path:
                write_returned = True
            return identity

        def observe_after_write(root: Path, path: Path):
            if write_returned and path == input_path:
                raise engine_module.FilesystemError(
                    "private injected failure", code="observe_failed"
                )
            return original_observe(root, path)

        with (
            patch("luwu.engine.write_file", side_effect=write_then_arm),
            patch("luwu.engine.observe_file", side_effect=observe_after_write),
        ):
            result = reverse_inputs(
                self._plan(),
                resource="settings",
                fields=("theme",),
                confirm=True,
            )
        action = next(item for item in result.actions if item.kind == "input_write")
        self.assertEqual(result.outcome, "committed_but_verification_failed")
        self.assertEqual(action.state, "committed")
        self.assertEqual(
            json.loads(input_path.read_text(encoding="utf-8"))["overrides"]["theme"],
            "live-theme-v2",
        )

    def test_reverse_preflight_rejects_divergent_shared_consumers(self) -> None:
        (self.source / "templates" / "first.json.j2").write_text(
            '{"theme":{{ inputs.theme | tojson }}}\n', encoding="utf-8"
        )
        (self.source / "templates" / "second.json.j2").write_text(
            '{"theme":{{ inputs.theme | tojson }}}\n', encoding="utf-8"
        )
        shared = self.inputs / "local" / "shared.json"
        shared.write_text('{"theme":"theme-v1"}\n', encoding="utf-8")
        self.manifest_path.write_text(
            """
version = 8
[roots]
source = "source"
inputs = "inputs"
target = "home"
[profiles.work]
packages = ["editor"]
exclude = []
[packages.editor]
dependencies = []
resources = ["first", "second"]
[inputs.theme]
layers = [{ file = "local/shared.json", path = ["theme"], format = "json" }]
reverse_to = { file = "local/shared.json", path = ["theme"], format = "json" }
[resources.first]
kind = "template"
source = "templates/first.json.j2"
target = "first.json"
format = "json"
scope = "fields"
baseline = "baselines/first.json"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
[resources.first.fields.theme]
path = ["theme"]
owner = "merge"
input = "theme"
[resources.second]
kind = "template"
source = "templates/second.json.j2"
target = "second.json"
format = "json"
scope = "fields"
baseline = "baselines/second.json"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
[resources.second.fields.theme]
path = ["theme"]
owner = "merge"
input = "theme"
""",
            encoding="utf-8",
        )
        for resource in ("first", "second"):
            result = accept_baseline(
                self._plan(),
                resource=resource,
                fields=("theme",),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(result.outcome, "committed")
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")

        first_target = self.home / "first.json"
        first_target.write_text('{"theme":"theme-v2"}\n', encoding="utf-8")
        before = self._snapshot()
        plan = self._plan()
        self.assertEqual(plan.to_dict()["resources"][0]["status"], "reverse_candidate")
        with self.assertRaises(EngineError) as preview_error:
            reverse_inputs(
                plan,
                resource="first",
                fields=("theme",),
                confirm=False,
            )
        self.assertEqual(preview_error.exception.code, "reverse_consumer_change")
        self.assertEqual(self._snapshot(), before)
        with self.assertRaises(EngineError) as raised:
            reverse_inputs(
                plan,
                resource="first",
                fields=("theme",),
                confirm=True,
            )
        self.assertEqual(raised.exception.code, "reverse_consumer_change")
        self.assertEqual(self._snapshot(), before)
