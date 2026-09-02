from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Self, cast

from luwu.cli import main
from luwu.errors import ApplyError
from luwu.manifest import load_manifest
from luwu.reconcile import Action, Status, apply_plan, build_plan, plan_to_dict
from luwu.semantic import ComparisonStatus


class M2ReconcileTests(unittest.TestCase):
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


class M2CliTests(unittest.TestCase):
    def test_v2_human_plan_explains_read_only_capability(self) -> None:
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
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(["plan", "--manifest", str(project.manifest_path)])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("Capability: read-only (M2)", output)
            self.assertIn("Apply: blocked (m2_read_only)", output)
            self.assertIn("M2 writes nothing", output)

    def test_v2_confirmed_apply_explains_read_only_boundary(self) -> None:
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
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--yes",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(payload["applied"])
            self.assertEqual(payload["reason"], "m2_read_only")
            self.assertFalse(payload["applyable"])
            self.assertEqual(
                payload["resources"][0]["comparison"]["strategy"],
                "json",
            )
            self.assertEqual(
                payload["resources"][0]["comparison"]["status"],
                "not-compared",
            )
            self.assertFalse(project.settings_target.exists())


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


if __name__ == "__main__":
    unittest.main()
