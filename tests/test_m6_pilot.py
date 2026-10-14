"""Focused tests for the bounded M6 bidirectional pilot loop."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from luwu.cli import main
from luwu.errors import LuwuError
from luwu.m6_pilot import (
    accept_m6_baseline,
    apply_m6_forward,
    build_m6_plan,
    reverse_m6_inputs,
)
from luwu.manifest import load_manifest


class M6PilotTests(unittest.TestCase):
    def test_preview_baseline_forward_and_reverse_form_one_closed_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            manifest = load_manifest(root / "luwu.toml")

            preview = accept_m6_baseline(
                manifest,
                resource_name="pi",
                fields=("model", "changelog"),
                value_from="desired",
                confirm=False,
            )
            self.assertEqual(preview.outcome, "confirmation_required")
            self.assertFalse((root / "inputs/baselines/pi.json").exists())

            accepted = accept_m6_baseline(
                manifest,
                resource_name="pi",
                fields=("model", "changelog"),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(accepted.outcome, "committed")
            accepted = accept_m6_baseline(
                manifest,
                resource_name="pi",
                fields=("provider",),
                value_from="live",
                confirm=True,
            )
            self.assertEqual(accepted.outcome, "committed")

            (root / "inputs/global/model.json").write_text(
                '{"model": "pi-5"}\n', encoding="utf-8"
            )
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            self.assertEqual(plan.resource.action, "forward")
            self.assertEqual(plan.resource.forward_fields, ("model",))

            before = (root / "home/pi.json").read_bytes()
            preview = apply_m6_forward(plan, confirm=False)
            self.assertEqual(preview.outcome, "confirmation_required")
            self.assertEqual((root / "home/pi.json").read_bytes(), before)

            applied = apply_m6_forward(plan, confirm=True)
            self.assertEqual(applied.outcome, "committed")
            target = json.loads((root / "home/pi.json").read_text(encoding="utf-8"))
            self.assertEqual(target["model"], "pi-5")
            self.assertEqual(target["provider"], "provider-1")

            target["provider"] = "provider-2"
            (root / "home/pi.json").write_text(
                json.dumps(target) + "\n", encoding="utf-8"
            )
            reverse_plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            self.assertEqual(reverse_plan.resource.action, "reverse_candidate")
            self.assertEqual(reverse_plan.resource.reverse_fields, ("provider",))
            reversed_result = reverse_m6_inputs(
                reverse_plan, fields=("provider",), confirm=True
            )
            self.assertEqual(reversed_result.outcome, "committed")
            self.assertEqual(
                json.loads((root / "inputs/local/provider.json").read_text()),
                {"provider": "provider-2"},
            )
            self.assertEqual(
                build_m6_plan(load_manifest(root / "luwu.toml")).resource.action, "noop"
            )

    def test_cli_routes_version_seven_without_an_execution_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            manifest = load_manifest(root / "luwu.toml")
            accept_m6_baseline(
                manifest,
                resource_name="pi",
                fields=("model", "changelog", "provider"),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(
                main(["plan", "--manifest", str(root / "luwu.toml"), "--json"]), 0
            )
            self.assertEqual(
                main(
                    ["apply", "--manifest", str(root / "luwu.toml"), "--yes", "--json"]
                ),
                0,
            )

    def test_undeclared_live_change_blocks_forward_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            manifest = load_manifest(root / "luwu.toml")
            for fields, source in (
                (("model", "changelog"), "desired"),
                (("provider",), "live"),
            ):
                accept_m6_baseline(
                    manifest,
                    resource_name="pi",
                    fields=fields,
                    value_from=source,
                    confirm=True,
                )
            target = json.loads((root / "home/pi.json").read_text())
            target["unmanaged"] = "keep"
            (root / "home/pi.json").write_text(json.dumps(target) + "\n")
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            self.assertEqual(plan.resource.action, "review")
            before = (root / "home/pi.json").read_bytes()
            with self.assertRaises(LuwuError):
                apply_m6_forward(plan, confirm=True)
            self.assertEqual((root / "home/pi.json").read_bytes(), before)

    def test_changed_input_after_preview_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            manifest = load_manifest(root / "luwu.toml")
            accept_m6_baseline(
                manifest,
                resource_name="pi",
                fields=("model", "changelog"),
                value_from="desired",
                confirm=True,
            )
            accept_m6_baseline(
                manifest,
                resource_name="pi",
                fields=("provider",),
                value_from="live",
                confirm=True,
            )
            (root / "inputs/global/model.json").write_text('{"model":"pi-5"}\n')
            plan = build_m6_plan(load_manifest(root / "luwu.toml"))
            (root / "inputs/global/model.json").write_text('{"model":"pi-6"}\n')
            current = build_m6_plan(load_manifest(root / "luwu.toml"))
            self.assertNotEqual(
                plan.resource.input_context.values["model"].digest,
                current.resource.input_context.values["model"].digest,
            )
            with self.assertRaises(LuwuError) as raised:
                apply_m6_forward(plan, confirm=True)
            self.assertEqual(raised.exception.code, "m6_stale_plan")

    def test_reverse_cli_rejects_a_resource_different_from_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            manifest = load_manifest(root / "luwu.toml")
            for fields, source in (
                (("model", "changelog"), "desired"),
                (("provider",), "live"),
            ):
                accept_m6_baseline(
                    manifest,
                    resource_name="pi",
                    fields=fields,
                    value_from=source,
                    confirm=True,
                )
            target = json.loads((root / "home/pi.json").read_text())
            target["provider"] = "provider-2"
            (root / "home/pi.json").write_text(json.dumps(target) + "\n")

            result = main(
                [
                    "reverse-sync",
                    "--manifest",
                    str(root / "luwu.toml"),
                    "--resource",
                    "wrong",
                    "--field",
                    "provider",
                    "--yes",
                    "--json",
                ]
            )

            self.assertEqual(result, 2)
            self.assertEqual(
                json.loads((root / "inputs/local/provider.json").read_text()),
                {"provider": "provider-1"},
            )

    def test_missing_target_requires_complete_baseline_and_declared_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_project(root)
            (root / "home/pi.json").unlink()
            manifest = load_manifest(root / "luwu.toml")

            unbased = build_m6_plan(manifest)
            self.assertEqual(unbased.resource.action, "review")
            self.assertEqual(unbased.resource.status, "unbased")
            accepted = accept_m6_baseline(
                manifest,
                resource_name="pi",
                fields=("model", "changelog", "provider"),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(accepted.outcome, "committed")
            create = build_m6_plan(load_manifest(root / "luwu.toml"))
            self.assertEqual(create.resource.action, "create")
            preview = apply_m6_forward(create, confirm=False)
            self.assertEqual(preview.outcome, "confirmation_required")
            self.assertFalse((root / "home/pi.json").exists())

            (root / "home/pi.json").write_text(
                '{"model":"pi-4","provider":"provider-1",'
                '"changelog":"release-1","client_secret":"opaque"}\n'
            )
            blocked = build_m6_plan(load_manifest(root / "luwu.toml"))
            self.assertEqual(blocked.resource.status, "blocked")
            before = (root / "home/pi.json").read_bytes()
            with self.assertRaises(LuwuError):
                apply_m6_forward(blocked, confirm=True)
            self.assertEqual((root / "home/pi.json").read_bytes(), before)

    def _make_project(self, root: Path) -> None:
        for path in (
            root / "repo",
            root / "inputs/global",
            root / "inputs/local",
            root / "inputs/baselines",
            root / "home",
        ):
            path.mkdir(parents=True)
        (root / "repo/pi.json.j2").write_text(
            '{"model":"{{ global_inputs.model }}",'
            '"provider":"{{ local_inputs.provider }}",'
            '"changelog":"{{ local_inputs.changelog }}"}\n',
            encoding="utf-8",
        )
        (root / "inputs/global/model.json").write_text(
            '{"model":"pi-4"}\n', encoding="utf-8"
        )
        (root / "inputs/local/provider.json").write_text(
            '{"provider":"provider-1"}\n', encoding="utf-8"
        )
        (root / "inputs/local/changelog.json").write_text(
            '{"changelog":"release-1"}\n', encoding="utf-8"
        )
        (root / "home/pi.json").write_text(
            '{"model":"pi-4","provider":"provider-1","changelog":"release-1"}\n',
            encoding="utf-8",
        )
        (root / "luwu.toml").write_text(
            """version = 7

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[resources.pi]
kind = "template"
source = "pi.json.j2"
target = "pi.json"
owner = "fields"
scope = "fields"
comparison = "json"
content_sensitivity = "public"
baseline = "baselines/pi.json"
inputs = { model = "global/model.json", provider = "local/provider.json", changelog = "local/changelog.json" }

[resources.pi.fields]
model = "source"
provider = "live"
changelog = "source"
""",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
