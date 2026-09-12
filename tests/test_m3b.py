from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Self, cast
from unittest.mock import patch

from luwu import mutations
from luwu.baseline import encode_public_json
from luwu.baseline import write_baseline as write_baseline_impl
from luwu.cli import main
from luwu.errors import ManifestError, MutationError, RenderError
from luwu.manifest import load_manifest
from luwu.mutations import accept_baseline, reverse_sync
from luwu.reconcile import _read_target as reconcile_read_target
from luwu.rendering import render_template as render_template_impl
from luwu.reverse_sync import build_source_patch


class M3bMutationTests(unittest.TestCase):
    def test_public_json_writer_rejects_unsafe_numeric_and_key_inputs(self) -> None:
        with self.assertRaises(MutationError):
            encode_public_json({"value": Decimal("NaN")})
        with self.assertRaises(MutationError):
            encode_public_json({1: "not-a-json-key"})

    def test_v4_manifest_is_explicit_and_reverse_mapping_is_frozen(self) -> None:
        with _Project() as project:
            resource = load_manifest(project.manifest).resources[0]
            self.assertEqual(resource.reverse_sync["runtime"], "runtime")
            with self.assertRaises(TypeError):
                cast(Any, resource.reverse_sync)["new"] = "new"

    def test_manifest_rejects_reverse_mapping_to_another_declared_field(self) -> None:
        with _Project() as project:
            project.manifest.write_text(
                project.manifest.read_text(encoding="utf-8").replace(
                    'runtime = "runtime"', 'runtime = "setting"'
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(project.manifest)

            self.assertEqual(context.exception.code, "reverse_sync_owner")

    def test_manifest_rejects_reverse_mapping_to_undeclared_source_key(self) -> None:
        with _Project() as project:
            project.manifest.write_text(
                project.manifest.read_text(encoding="utf-8").replace(
                    'runtime = "runtime"', 'runtime = "new_undeclared_key"'
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError) as context:
                load_manifest(project.manifest)

            self.assertEqual(context.exception.code, "reverse_sync_owner")

    def test_literal_reverse_sync_rejects_non_identity_destination(self) -> None:
        source = b'{"setting": 1, "runtime": 2, "undeclared": "source"}'
        live = b'{"setting": 1, "runtime": 3, "undeclared": "live"}'
        fields = {"setting": "source", "runtime": "live", "undeclared": "ignore"}
        for destination in ("setting", "undeclared"):
            with self.subTest(destination=destination):
                with self.assertRaises(MutationError) as context:
                    build_source_patch(
                        source,
                        live,
                        fields=fields,
                        owners=fields,
                        reverse_sync={"runtime": destination},
                        selected_fields=("runtime",),
                    )
                self.assertEqual(context.exception.code, "reverse_sync_mapping")

    def test_accept_preview_does_not_create_baseline_and_confirmed_accept_writes_it(
        self,
    ) -> None:
        with _Project() as project:
            before = project.baseline.exists()
            preview = _invoke(
                [
                    "accept",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--from",
                    "desired",
                    "--field",
                    "setting",
                    "--json",
                ]
            )
            self.assertEqual(preview[0], 2)
            self.assertFalse(preview[1]["applied"])
            self.assertFalse(before or project.baseline.exists())

            applied, payload = _invoke(
                [
                    "accept",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--from",
                    "desired",
                    "--field",
                    "setting",
                    "--yes",
                    "--json",
                ]
            )
            self.assertEqual(applied, 0)
            self.assertTrue(payload["applied"])
            baseline = json.loads(project.baseline.read_text(encoding="utf-8"))
            self.assertEqual(baseline["values"], {"setting": 1})
            self.assertNotIn("runtime-value", json.dumps(payload))

    def test_reverse_sync_changes_only_selected_source_key_and_recalculates(
        self,
    ) -> None:
        with _Project() as project:
            project.write_baseline(
                {
                    "setting": 1,
                    "runtime": 1,
                }
            )
            before = project.source.read_bytes()
            preview, payload = _invoke(
                [
                    "reverse-sync",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--field",
                    "runtime",
                    "--json",
                ]
            )
            self.assertEqual(preview, 2)
            self.assertFalse(payload["applied"])
            self.assertEqual(project.source.read_bytes(), before)

            applied, payload = _invoke(
                [
                    "reverse-sync",
                    "--manifest",
                    str(project.manifest),
                    "--resource",
                    "settings",
                    "--field",
                    "runtime",
                    "--yes",
                    "--json",
                ]
            )
            self.assertEqual(applied, 0)
            self.assertEqual(
                json.loads(project.source.read_text(encoding="utf-8")),
                {"setting": 1, "runtime": 2, "undeclared": "source"},
            )
            self.assertEqual(
                cast(dict[str, Any], payload["verification"])["resources"][0]["status"],
                "drifted",
            )

    def test_reverse_sync_blocks_strictly_changed_undeclared_content(self) -> None:
        with _Project() as project:
            project.source.write_text(
                '{"setting": 1, "runtime": 1, "undeclared": "source", "outside": true}',
                encoding="utf-8",
            )
            project.target.write_text(
                '{"setting": 1, "runtime": 2, "undeclared": "live", "outside": 1}',
                encoding="utf-8",
            )
            project.write_baseline({"setting": 1, "runtime": 1})
            before = project.source.read_bytes()

            with self.assertRaises(MutationError) as context:
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "undeclared_changed")
            self.assertEqual(project.source.read_bytes(), before)

    def test_v4_apply_remains_read_only(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            code, payload = _invoke(
                [
                    "apply",
                    "--manifest",
                    str(project.manifest),
                    "--yes",
                    "--json",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["reason"], "m3_read_only")

    def test_mutations_reject_multi_resource_manifest_before_planning(self) -> None:
        with _Project() as project:
            loaded = load_manifest(project.manifest)
            manifest = replace(
                loaded,
                resources=loaded.resources * 2,
            )
            with self.assertRaises(MutationError) as context:
                accept_baseline(
                    manifest,
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )
            self.assertEqual(context.exception.code, "resource_count")
            self.assertFalse(project.baseline.exists())

    def test_field_selection_is_unique_and_declared_at_the_public_boundary(
        self,
    ) -> None:
        with _Project() as project:
            for fields in (("setting", "setting"), ("missing",)):
                with self.subTest(fields=fields):
                    with self.assertRaises(MutationError) as context:
                        accept_baseline(
                            load_manifest(project.manifest),
                            resource_name="settings",
                            value_from="desired",
                            fields=fields,
                            confirm=False,
                        )
                    self.assertEqual(
                        context.exception.code,
                        "field_duplicate"
                        if len(set(fields)) != len(fields)
                        else "field_not_declared",
                    )

    def test_post_write_verification_failure_reports_known_commit(self) -> None:
        with _Project() as project:
            with patch(
                "luwu.mutations._verify",
                side_effect=RuntimeError("verification unavailable"),
            ):
                result = accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )
            self.assertTrue(result.applied)
            self.assertEqual(result.outcome, "committed_but_verification_failed")
            self.assertTrue(project.baseline.exists())
            self.assertEqual(
                result.verification,
                {"error": "post_write_verification_failed"},
            )

    def test_accept_rechecks_the_baseline_snapshot_under_the_writer_lock(self) -> None:
        with _Project() as project:

            def write_with_race(
                root: Path,
                resource: Any,
                data: bytes,
                *,
                expected_data: bytes | None,
                check_inputs: Any = None,
            ) -> None:
                project.baseline.write_text("racing-baseline", encoding="utf-8")
                write_baseline_impl(
                    root,
                    resource,
                    data,
                    expected_data=expected_data,
                    check_inputs=check_inputs,
                )

            with (
                patch("luwu.mutations.write_baseline", side_effect=write_with_race),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "stale_plan")
            self.assertEqual(
                project.baseline.read_text(encoding="utf-8"), "racing-baseline"
            )

    def test_stale_manifest_source_and_live_are_rejected(self) -> None:
        with _Project() as project:

            def render_with_race(resource: Any, *, root: Path) -> Any:
                project.source.write_text(
                    '{"setting": 9, "runtime": 1, "undeclared": "source"}',
                    encoding="utf-8",
                )
                return render_template_impl(resource, root=root)

            with (
                patch("luwu.mutations.render_template", side_effect=render_with_race),
                self.assertRaises(MutationError) as source_context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )
            self.assertEqual(source_context.exception.code, "stale_plan")
            self.assertFalse(project.baseline.exists())

        with _Project() as project:
            with (
                patch(
                    "luwu.mutations._check_manifest_fresh",
                    side_effect=MutationError("manifest changed", code="stale_plan"),
                ),
                self.assertRaises(MutationError) as manifest_context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )
            self.assertEqual(manifest_context.exception.code, "stale_plan")
            self.assertFalse(project.baseline.exists())

        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            calls = 0

            def read_target_with_race(target: Path, *, root: Path) -> Any:
                nonlocal calls
                calls += 1
                state = reconcile_read_target(target, root=root)
                if calls == 1:
                    project.target.write_text(
                        '{"setting": 1, "runtime": 3, "undeclared": "live"}',
                        encoding="utf-8",
                    )
                return state

            with (
                patch("luwu.mutations._read_target", side_effect=read_target_with_race),
                self.assertRaises(MutationError) as live_context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )
            self.assertEqual(live_context.exception.code, "stale_plan")

    def test_reverse_sync_reports_unknown_after_final_live_check_race(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            real_replace = os.replace

            def replace_with_live_race(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                project.target.write_text(
                    '{"setting": 1, "runtime": 9, "undeclared": "live"}',
                    encoding="utf-8",
                )
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                patch("luwu.mutations.os.replace", side_effect=replace_with_live_race),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "source_state_unknown")
            self.assertTrue(context.exception.committed)
            self.assertEqual(
                json.loads(project.source.read_text(encoding="utf-8"))["runtime"],
                2,
            )

    def test_reverse_sync_reports_verification_failure_after_directory_sync_race(
        self,
    ) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            real_sync = mutations.sync_directory

            def sync_with_live_race(parent: int) -> None:
                project.target.write_text(
                    '{"setting": 1, "runtime": 9, "undeclared": "live"}',
                    encoding="utf-8",
                )
                real_sync(parent)

            with patch(
                "luwu.mutations.sync_directory", side_effect=sync_with_live_race
            ):
                result = reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(result.outcome, "committed_but_verification_failed")
            self.assertTrue(result.applied)

    def test_accept_reports_unknown_after_final_source_check_race(self) -> None:
        with _Project() as project:
            real_replace = os.replace

            def replace_with_source_race(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                project.source.write_text(
                    '{"setting": 9, "runtime": 1, "undeclared": "source"}',
                    encoding="utf-8",
                )
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                patch("luwu.baseline.os.replace", side_effect=replace_with_source_race),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "baseline_state_unknown")
            self.assertTrue(context.exception.committed)
            self.assertTrue(project.baseline.exists())

    def test_accept_converts_post_replace_input_error_to_unknown(self) -> None:
        with _Project() as project:
            calls = 0

            def check_inputs(*args: Any, **kwargs: Any) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RenderError("render became unavailable")

            with (
                patch("luwu.mutations._check_accept_inputs", side_effect=check_inputs),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "baseline_state_unknown")
            self.assertTrue(context.exception.committed)
            self.assertEqual(context.exception.outcome, "committed_state_unknown")
            self.assertTrue(project.baseline.exists())

    def test_baseline_cleanup_failure_is_reported(self) -> None:
        with _Project() as project:
            resource = load_manifest(project.manifest).resources[0]

            def fail_before_replace() -> None:
                raise MutationError("input changed", code="stale_plan")

            with (
                patch("luwu.baseline.os.unlink", side_effect=OSError("cleanup")),
                self.assertRaises(MutationError) as context,
            ):
                write_baseline_impl(
                    project.root,
                    resource,
                    b"{}\n",
                    expected_data=None,
                    check_inputs=fail_before_replace,
                )

            self.assertEqual(context.exception.code, "cleanup_failed")
            self.assertFalse(context.exception.committed)

    def test_source_cleanup_failure_is_reported(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            with (
                patch(
                    "luwu.mutations._check_source_current",
                    side_effect=MutationError("source changed", code="stale_plan"),
                ),
                patch("luwu.mutations.os.unlink", side_effect=OSError("cleanup")),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "cleanup_failed")
            self.assertFalse(context.exception.committed)

    def test_source_parent_rebind_after_final_check_is_unknown(self) -> None:
        with _Project() as project:
            project.write_baseline({"setting": 1, "runtime": 1})
            original_parent = project.source.parent
            detached_parent = project.root / "templates-detached"
            real_replace = os.replace

            def replace_after_parent_rebind(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                original_parent.rename(detached_parent)
                original_parent.mkdir()
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                patch(
                    "luwu.mutations.os.replace",
                    side_effect=replace_after_parent_rebind,
                ),
                self.assertRaises(MutationError) as context,
            ):
                reverse_sync(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    fields=("runtime",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "source_state_unknown")
            self.assertTrue(context.exception.committed)
            self.assertFalse(project.source.exists())
            self.assertTrue((detached_parent / project.source.name).exists())

    def test_baseline_parent_rebind_after_final_check_is_unknown(self) -> None:
        with _Project() as project:
            state = project.root / "state"
            state.mkdir()
            project.manifest.write_text(
                project.manifest.read_text(encoding="utf-8").replace(
                    'baseline = "baseline.json"',
                    'baseline = "state/baseline.json"',
                ),
                encoding="utf-8",
            )
            original_parent = state
            detached_parent = project.root / "state-detached"
            real_replace = os.replace

            def replace_after_parent_rebind(
                source_name: str,
                target_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                original_parent.rename(detached_parent)
                original_parent.mkdir()
                real_replace(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                patch(
                    "luwu.baseline.os.replace",
                    side_effect=replace_after_parent_rebind,
                ),
                self.assertRaises(MutationError) as context,
            ):
                accept_baseline(
                    load_manifest(project.manifest),
                    resource_name="settings",
                    value_from="desired",
                    fields=("setting",),
                    confirm=True,
                )

            self.assertEqual(context.exception.code, "baseline_state_unknown")
            self.assertTrue(context.exception.committed)
            self.assertFalse((state / "baseline.json").exists())
            self.assertTrue((detached_parent / "baseline.json").exists())


def _invoke(arguments: list[str]) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
        code = main(arguments)
    return code, json.loads(stdout.getvalue())


class _Project:
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.source = self.root / "templates/settings.json.j2"
        self.target = self.root / "live/settings.json"
        self.baseline = self.root / "baseline.json"
        self.manifest = self.root / "luwu.toml"
        self.source.write_text(
            '{"setting": 1, "runtime": 1, "undeclared": "source"}',
            encoding="utf-8",
        )
        self.target.write_text(
            '{"setting": 1, "runtime": 2, "undeclared": "live"}',
            encoding="utf-8",
        )
        self.manifest.write_text(
            """version = 4

[resources.settings]
kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
baseline = "baseline.json"

[resources.settings.fields]
setting = "source"
runtime = "live"
undeclared = "ignore"

[resources.settings.reverse_sync]
format = "literal-json"

[resources.settings.reverse_sync.fields]
runtime = "runtime"
""",
            encoding="utf-8",
        )

    def write_baseline(self, values: dict[str, object]) -> None:
        self.baseline.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "resource": "settings",
                    "source": "templates/settings.json.j2",
                    "target": "live/settings.json",
                    "owners": {
                        "setting": "source",
                        "runtime": "live",
                        "undeclared": "ignore",
                    },
                    "values": values,
                }
            ),
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.directory.cleanup()


if __name__ == "__main__":
    unittest.main()
