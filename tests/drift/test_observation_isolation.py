"""Tests for observation isolation."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Self, cast
from unittest.mock import patch

from luwu.cli import main
from luwu.errors import ApplyError
from luwu.manifest import load_manifest
from luwu.reconcile import (
    Action,
    ApplyOutcome,
    Status,
    apply_plan,
    build_plan,
    plan_to_dict,
)
from luwu.semantic import ComparisonStatus


class _M2Project:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.manifest_path = self.root / "luwu.toml"
        self.alpha_target = self.root / "live/alpha.json"
        self.zeta_target = self.root / "live/zeta.json"
        self.settings_target = self.root / "live/settings.json"

    def _write_source(self, name: str, content: str) -> None:
        (self.root / f"templates/{name}.j2").write_text(
            content,
            encoding="utf-8",
        )

    def _write_manifest(self, content: str) -> None:
        self.manifest_path.write_text(content, encoding="utf-8")

    def _two_resource_manifest(self) -> str:
        return """version = 2

[resources.alpha]
source = "templates/alpha.j2"
target = "live/alpha.json"
owner = "source"
scope = "whole-file"

[resources.zeta]
source = "templates/zeta.j2"
target = "live/zeta.json"
owner = "source"
scope = "whole-file"
"""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()


def _create_m1_project(root: Path) -> tuple[Path, Path]:
    (root / "templates").mkdir()
    (root / "live").mkdir()
    (root / "templates/settings.conf.j2").write_text(
        'profile = "{{ profile }}"\n',
        encoding="utf-8",
    )
    manifest_path = root / "luwu.toml"
    manifest_path.write_text(
        """version = 1

[resources.settings]
source = "templates/settings.conf.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
variables_sensitivity = "public"

[resources.settings.variables]
profile = "developer"
""",
        encoding="utf-8",
    )
    return root / "live/settings.conf", manifest_path


def _create_m2_project(root: Path) -> tuple[Path, Path]:
    (root / "templates").mkdir()
    (root / "live").mkdir()
    (root / "templates/alpha.j2").write_text(
        '{{ "\\ud800" }}',
        encoding="utf-8",
    )
    (root / "templates/zeta.j2").write_text(
        "healthy-output-sentinel",
        encoding="utf-8",
    )
    manifest_path = root / "luwu.toml"
    manifest_path.write_text(
        """version = 2

[resources.alpha]
source = "templates/alpha.j2"
target = "live/alpha.conf"
owner = "source"
scope = "whole-file"

[resources.zeta]
source = "templates/zeta.j2"
target = "live/zeta.conf"
owner = "source"
scope = "whole-file"
""",
        encoding="utf-8",
    )
    return manifest_path, root / "live/zeta.conf"


def _snapshot_tree(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        relative = str(path.relative_to(root))
        common = (info.st_mode, info.st_ino, info.st_mtime_ns)
        if stat.S_ISLNK(info.st_mode):
            snapshot[relative] = ("symlink", os.readlink(path), *common)
        elif stat.S_ISREG(info.st_mode):
            snapshot[relative] = ("file", path.read_bytes(), *common)
        elif stat.S_ISDIR(info.st_mode):
            snapshot[relative] = ("directory", *common)
        else:
            snapshot[relative] = ("other", *common)
    return snapshot


class ObservationV2Tests(unittest.TestCase):
    def test_v2_observes_all_resources_in_stable_order_without_becoming_applyable(
        self,
    ) -> None:
        with _M2Project() as project:
            project._write_source("alpha", '{"name": "alpha"}\n')
            project._write_source("zeta", '{"name": "zeta"}\n')
            project._write_manifest(
                """version = 2

[resources.zeta]
source = "templates/zeta.j2"
target = "live/zeta.json"
owner = "source"
scope = "whole-file"

[resources.alpha]
source = "templates/alpha.j2"
target = "live/alpha.json"
owner = "source"
scope = "whole-file"
"""
            )
            plan = build_plan(load_manifest(project.manifest_path))

            self.assertEqual(
                tuple(observation.resource.name for observation in plan.observations),
                ("alpha", "zeta"),
            )
            self.assertEqual(
                tuple(observation.status for observation in plan.observations),
                (Status.MISSING, Status.MISSING),
            )
            self.assertFalse(plan.can_apply)
            self.assertEqual(plan.apply_block_reason, "m2_read_only")
            self.assertFalse(project.alpha_target.exists())
            self.assertFalse(project.zeta_target.exists())

    def test_v2_apply_refuses_before_writing_any_resource(self) -> None:
        with _M2Project() as project:
            project._write_source("alpha", '{"name": "alpha"}\n')
            project._write_source("zeta", '{"name": "zeta"}\n')
            project._write_manifest(project._two_resource_manifest())
            plan = build_plan(load_manifest(project.manifest_path))

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "m2_read_only")
            self.assertFalse(project.alpha_target.exists())
            self.assertFalse(project.zeta_target.exists())

    def test_v2_read_only_capability_survives_manifest_object_tampering(self) -> None:
        with _M2Project() as project:
            project._write_source("settings", '{"profile": "developer"}\n')
            project._write_manifest(
                """version = 2

[resources.settings]
source = "templates/settings.j2"
target = "live/settings.json"
owner = "source"
scope = "whole-file"
"""
            )
            manifest = load_manifest(project.manifest_path)
            plan = build_plan(manifest)
            alternate_manifest_path = project.root / "alternate.toml"
            alternate_manifest_path.write_text(
                """version = 1

[resources.settings]
source = "templates/settings.j2"
target = "live/settings.json"
owner = "source"
scope = "whole-file"
""",
                encoding="utf-8",
            )
            alternate_manifest = load_manifest(alternate_manifest_path)
            object.__setattr__(manifest, "version", 1)
            object.__setattr__(manifest, "path", alternate_manifest.path)
            object.__setattr__(
                manifest, "content_digest", alternate_manifest.content_digest
            )
            object.__setattr__(plan, "manifest_version", 1)

            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "m2_read_only")
            self.assertFalse(project.settings_target.exists())

    def test_v2_copy_resource_is_observed_without_implicit_template_rendering(
        self,
    ) -> None:
        with _M2Project() as project:
            source = project.root / "files/settings.conf"
            source.parent.mkdir()
            source.write_bytes(b"literal {{ not_a_variable }}\n")
            project._write_manifest(
                """version = 2

[resources.settings]
kind = "copy"
source = "files/settings.conf"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
"""
            )

            plan = build_plan(load_manifest(project.manifest_path))

            self.assertEqual(plan.observations[0].resource.kind, "copy")
            self.assertEqual(plan.observations[0].status, Status.MISSING)
            self.assertEqual(plan.observations[0].action, Action.CREATE)
            self.assertFalse(project.root.joinpath("live/settings.conf").exists())

    def test_json_comparison_reports_formatting_and_semantic_drift_without_replace(
        self,
    ) -> None:
        with _M2Project() as project:
            project._write_source("settings", '{"profile": "developer"}\n')
            project._write_manifest(
                """version = 2

[resources.settings]
source = "templates/settings.j2"
target = "live/settings.json"
owner = "source"
scope = "whole-file"
comparison = "json"
"""
            )
            project.settings_target.write_text(
                '{\n  "profile": "developer"\n}',
                encoding="utf-8",
            )

            formatted_plan = build_plan(load_manifest(project.manifest_path))
            formatted_observation = formatted_plan.observations[0]
            self.assertEqual(formatted_observation.status, Status.FORMATTING)
            self.assertEqual(formatted_observation.action, Action.NOOP)
            formatted_comparison = formatted_observation.comparison
            assert formatted_comparison is not None
            self.assertEqual(
                formatted_comparison.status,
                ComparisonStatus.EQUIVALENT_BUT_REFORMATTED,
            )

            project.settings_target.write_text(
                '{"profile": "other"}',
                encoding="utf-8",
            )
            drifted_plan = build_plan(load_manifest(project.manifest_path))
            drifted_observation = drifted_plan.observations[0]
            self.assertEqual(drifted_observation.status, Status.DRIFTED)
            self.assertEqual(drifted_observation.action, Action.REPORT)
            drifted_comparison = drifted_observation.comparison
            assert drifted_comparison is not None
            self.assertEqual(
                drifted_comparison.status,
                ComparisonStatus.DIFFERENT,
            )

    def test_invalid_live_json_is_blocked_without_replace(self) -> None:
        with _M2Project() as project:
            project._write_source("settings", '{"profile": "developer"}\n')
            project._write_manifest(
                """version = 2

[resources.settings]
source = "templates/settings.j2"
target = "live/settings.json"
owner = "source"
scope = "whole-file"
comparison = "json"
"""
            )
            project.settings_target.write_text("NaN", encoding="utf-8")

            plan = build_plan(load_manifest(project.manifest_path))
            observation = plan.observations[0]

            self.assertEqual(observation.status, Status.BLOCKED)
            self.assertEqual(observation.action, Action.BLOCK)
            comparison = observation.comparison
            assert comparison is not None
            self.assertEqual(comparison.status, ComparisonStatus.UNSUPPORTED)
            self.assertEqual(comparison.code, "non_finite_number")

    def test_json_parse_failure_is_blocked_while_other_resources_are_observed(
        self,
    ) -> None:
        with _M2Project() as project:
            project._write_source("broken", '{"profile":}\n')
            project._write_source("healthy", '{"profile": "developer"}\n')
            project._write_manifest(
                """version = 2

[resources.broken]
source = "templates/broken.j2"
target = "live/broken.json"
owner = "source"
scope = "whole-file"
comparison = "json"

[resources.healthy]
source = "templates/healthy.j2"
target = "live/healthy.json"
owner = "source"
scope = "whole-file"
comparison = "json"
"""
            )
            (project.root / "live/broken.json").write_text(
                '{"profile": "current"}',
                encoding="utf-8",
            )

            plan = build_plan(load_manifest(project.manifest_path))

            self.assertEqual(
                tuple(observation.resource.name for observation in plan.observations),
                ("broken", "healthy"),
            )
            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            self.assertEqual(plan.observations[0].action, Action.BLOCK)
            self.assertEqual(plan.observations[1].status, Status.MISSING)
            self.assertEqual(plan.apply_block_reason, "plan_blocked")
            payload = plan_to_dict(plan, command="plan")
            resources = cast(list[dict[str, object]], payload["resources"])
            comparison = cast(dict[str, object], resources[1]["comparison"])
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["manifest_version"], 2)
            self.assertFalse(payload["applyable"])
            self.assertEqual(
                comparison["status"],
                "not-compared",
            )
            self.assertNotIn("profile", json.dumps(payload))

    def test_json_nesting_limit_blocks_one_resource_without_hiding_another(
        self,
    ) -> None:
        with _M2Project() as project:
            project._write_source("deep", "[" * 129 + "0" + "]" * 129)
            project._write_source("healthy", '{"profile": "developer"}\n')
            project._write_manifest(
                """version = 2

[resources.deep]
source = "templates/deep.j2"
target = "live/deep.json"
owner = "source"
scope = "whole-file"
comparison = "json"

[resources.healthy]
source = "templates/healthy.j2"
target = "live/healthy.json"
owner = "source"
scope = "whole-file"
comparison = "json"
"""
            )
            (project.root / "live/deep.json").write_text("{}", encoding="utf-8")

            plan = build_plan(load_manifest(project.manifest_path))

            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            comparison = plan.observations[0].comparison
            assert comparison is not None
            self.assertEqual(comparison.code, "json_nesting_limit")
            self.assertEqual(plan.observations[1].status, Status.MISSING)

    def test_v2_render_error_does_not_hide_other_resource(self) -> None:
        with _M2Project() as project:
            project._write_source("broken", "{{ missing }}\n")
            project._write_source("healthy", "healthy\n")
            project._write_manifest(
                """version = 2

[resources.broken]
source = "templates/broken.j2"
target = "live/broken.conf"
owner = "source"
scope = "whole-file"

[resources.healthy]
source = "templates/healthy.j2"
target = "live/healthy.conf"
owner = "source"
scope = "whole-file"
"""
            )

            plan = build_plan(load_manifest(project.manifest_path))

            self.assertEqual(plan.observations[0].status, Status.BLOCKED)
            self.assertIn("broken", plan.observations[0].reason)
            self.assertEqual(plan.observations[1].status, Status.MISSING)


class ObservationBoundaryV1V2Tests(unittest.TestCase):
    def test_m1_target_symlink_is_blocked_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target, manifest_path = _create_m1_project(root)
            protected = root / "protected.conf"
            protected.write_bytes(b"protected content\n")
            target.symlink_to(protected)

            plan = build_plan(load_manifest(manifest_path))

            self.assertEqual(plan.observations[0].action.value, "block")
            with self.assertRaises(ApplyError) as context:
                apply_plan(plan)

            self.assertEqual(context.exception.code, "plan_blocked")
            self.assertTrue(target.is_symlink())
            self.assertEqual(protected.read_bytes(), b"protected content\n")

    def test_m1_existing_target_mode_is_preserved_after_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target, manifest_path = _create_m1_project(root)
            target.write_bytes(b"old target\n")
            target.chmod(0o600)

            result = apply_plan(build_plan(load_manifest(manifest_path)))

            self.assertEqual(result.outcome, ApplyOutcome.COMMITTED)
            self.assertEqual(target.read_bytes(), b'profile = "developer"\n')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_m1_successful_apply_cleans_temporary_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target, manifest_path = _create_m1_project(root)
            target.write_bytes(b"old target\n")

            result = apply_plan(build_plan(load_manifest(manifest_path)))

            self.assertEqual(result.outcome, ApplyOutcome.COMMITTED)
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.luwu-*")),
                [],
            )

    def test_m1_failed_write_preserves_old_target_and_cleans_temporary_entry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target, manifest_path = _create_m1_project(root)
            target.write_bytes(b"old target\n")
            target.chmod(0o640)
            before = target.stat()
            plan = build_plan(load_manifest(manifest_path))

            with (
                patch(
                    "luwu.reconcile.os.fchmod",
                    side_effect=OSError("injected permission failure"),
                ),
                self.assertRaises(ApplyError) as context,
            ):
                apply_plan(plan)

            self.assertEqual(context.exception.code, "write_failed")
            self.assertFalse(context.exception.committed)
            self.assertEqual(target.read_bytes(), b"old target\n")
            self.assertEqual(target.stat().st_ino, before.st_ino)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.luwu-*")),
                [],
            )

    def test_m2_rendered_encoding_block_keeps_other_resource_visible_and_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path, healthy_target = _create_m2_project(root)
            healthy_target.write_bytes(b"old healthy target\n")
            before = _snapshot_tree(root)

            for command, extra_arguments in (
                ("inspect", ()),
                ("plan", ()),
                ("apply", ("--yes",)),
            ):
                with self.subTest(command=command):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = main(
                            [
                                command,
                                "--manifest",
                                str(manifest_path),
                                "--json",
                                *extra_arguments,
                            ]
                        )

                    payload = json.loads(stdout.getvalue())
                    resources = {
                        resource["name"]: resource for resource in payload["resources"]
                    }
                    self.assertEqual(exit_code, 2 if command == "apply" else 0)
                    self.assertEqual(resources["alpha"]["status"], "blocked")
                    self.assertEqual(
                        resources["alpha"]["comparison"]["code"],
                        "rendered_encoding",
                    )
                    self.assertEqual(resources["zeta"]["status"], "drifted")
                    self.assertEqual(resources["zeta"]["action"], "replace")
                    self.assertNotIn("healthy-output-sentinel", stdout.getvalue())
                    self.assertNotIn("\\ud800", stdout.getvalue())
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(_snapshot_tree(root), before)
