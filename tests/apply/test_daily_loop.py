"""The routine loop: baselines follow applies, and reviews stay local."""

from __future__ import annotations

import json
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.baseline import make_field_baseline
from luwu.engine import accept_baseline, adopt_resource, apply_plan
from luwu.filesystem import FilesystemError
from tests.support.profile_case import ProfileEngineCase

_HEAD = """
version = 8
[roots]
source = "source"
inputs = "inputs"
target = "home"
[profiles.work]
packages = ["app"]
exclude = []
[packages.app]
dependencies = []
resources = ["app"]
[inputs.theme]
layers = [{ file = "local/shared.json", path = ["theme"], format = "json" }]
reverse_to = { file = "local/shared.json", path = ["theme"], format = "json" }
"""

_APP = """
[resources.app]
source = "templates/app.json.j2"
target = "app.json"
baseline = "baselines/app.json"
{extra}
[resources.app.fields.theme]
path = ["theme"]
owner = "merge"
input = "theme"
"""

_NOTE = """
[resources.app]
source = "notes.txt"
target = "notes.txt"
baseline = "baselines/app.json"
deploy = "copy"
"""

_TEMPLATE = '{"theme":{{ inputs.theme | tojson }},"unmanaged":{"k":"v"}}\n'


class DailyLoopTests(ProfileEngineCase):
    def setUp(self) -> None:
        super().setUp()
        (self.source / "templates/app.json.j2").write_text(_TEMPLATE, encoding="utf-8")
        self.shared = self.inputs / "local/shared.json"
        self._set_theme("v1")
        self.target = self.home / "app.json"
        self.baseline = self.inputs / "baselines/app.json"

    def _set_theme(self, value: str) -> None:
        self.shared.write_text(json.dumps({"theme": value}) + "\n", encoding="utf-8")

    def _configure(self, resource: str = _APP, **fields: str) -> None:
        self.manifest_path.write_text(
            _HEAD + resource.format(**{"extra": "", **fields}), encoding="utf-8"
        )

    def _status(self) -> tuple[str, str]:
        (item,) = self._plan().to_dict()["resources"]
        return item["status"], item["action"]

    def _deploy(self) -> None:
        accept_baseline(
            self._plan(),
            resource="app",
            fields=("theme",),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")

    def _baseline_theme(self) -> object:
        return json.loads(self.baseline.read_text(encoding="utf-8"))["values"]["theme"]

    def test_apply_advances_the_baseline_so_the_next_change_is_not_a_conflict(
        self,
    ) -> None:
        self._configure()
        self._deploy()
        self._set_theme("v2")

        preview = apply_plan(self._plan(), confirm=False)
        self.assertEqual(
            [(a.kind, a.fields, a.state) for a in preview.actions],
            [
                ("target_write", ("theme",), "not_attempted"),
                ("baseline_write", ("theme",), "not_attempted"),
            ],
        )
        self.assertEqual(self._baseline_theme(), "v1")

        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(result.outcome, "committed")
        self.assertEqual(
            [(a.kind, a.state) for a in result.actions],
            [("target_write", "committed"), ("baseline_write", "committed")],
        )
        self.assertEqual(self._baseline_theme(), "v2")
        self.assertEqual(self._status(), ("in_sync", "noop"))

        self._set_theme("v3")
        self.assertEqual(self._status(), ("drifted", "forward"))
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        self.assertEqual(json.loads(self.target.read_text())["theme"], "v3")
        self.assertEqual(self._baseline_theme(), "v3")

    def test_whole_file_copy_baseline_follows_the_apply(self) -> None:
        self._configure(_NOTE)
        (self.source / "notes.txt").write_text("one\n", encoding="utf-8")
        adopt_resource(self._plan(), resource="app", confirm=True)
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        (self.source / "notes.txt").write_text("two\n", encoding="utf-8")
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        (self.source / "notes.txt").write_text("three\n", encoding="utf-8")
        self.assertEqual(self._status(), ("drifted", "forward"))

    def test_a_failed_baseline_write_is_reported_without_undoing_the_target(
        self,
    ) -> None:
        self._configure()
        self._deploy()
        self._set_theme("v2")

        with patch(
            "luwu.engine.write_baseline",
            side_effect=FilesystemError("no", code="write_failed"),
        ):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed_but_baseline_not_advanced")
        self.assertEqual(result.error_code, "write_failed")
        states = {a.kind: a.state for a in result.actions}
        self.assertEqual(states["target_write"], "committed")
        self.assertEqual(states["baseline_write"], "not_replaced")
        self.assertEqual(json.loads(self.target.read_text())["theme"], "v2")
        self.assertEqual(self._baseline_theme(), "v1")
        self.assertEqual(self._status(), ("converged", "noop"))

    def test_a_baseline_that_misses_the_live_value_fails_verification(self) -> None:
        self._configure()
        self._deploy()
        self._set_theme("v2")

        def wrong_baseline(manifest, item, fresh_item):
            return make_field_baseline(
                manifest,
                item.resource,
                current={"theme": "other"},
                selected_fields=("theme",),
                existing=fresh_item.baseline,
            )

        with patch.object(engine_module, "_advanced_baseline_data", wrong_baseline):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed_but_verification_failed")
        self.assertEqual(result.error_code, "stale_plan")

    def test_preserve_keeps_application_keys_while_declared_fields_apply(
        self,
    ) -> None:
        self._configure(extra='undeclared = "preserve"')
        self._deploy()
        live = json.loads(self.target.read_text())
        live["added"] = "by-the-application"
        self.target.write_text(json.dumps(live) + "\n", encoding="utf-8")

        (item,) = self._plan().to_dict()["resources"]
        self.assertTrue(item["undeclared_changed"])
        self.assertEqual((item["status"], item["action"]), ("in_sync", "noop"))

        self._set_theme("v2")
        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(result.outcome, "committed")
        written = json.loads(self.target.read_text())
        self.assertEqual(written["theme"], "v2")
        self.assertEqual(written["added"], "by-the-application")
        self.assertEqual(self._baseline_theme(), "v2")

    def test_default_policy_still_blocks_on_undeclared_changes(self) -> None:
        self._configure()
        self._deploy()
        live = json.loads(self.target.read_text())
        live["added"] = "by-the-application"
        self.target.write_text(json.dumps(live) + "\n", encoding="utf-8")

        self.assertEqual(self._status(), ("review", "blocked"))
        self._set_theme("v2")
        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(result.error_code, "review_required")
        self.assertEqual(json.loads(self.target.read_text())["theme"], "v1")

    def test_public_names_admit_a_key_that_looks_secret_but_is_not(self) -> None:
        (self.source / "templates/app.json.j2").write_text(
            '{"theme":{{ inputs.theme | tojson }},"env_key":"OPENAI_API_KEY"}\n',
            encoding="utf-8",
        )
        self._configure()
        (item,) = self._plan().to_dict()["resources"]
        self.assertEqual(item["status"], "blocked")

        self._configure(extra='public_names = ["env_key"]')
        self._deploy()
        self.assertEqual(
            json.loads(self.target.read_text())["env_key"], "OPENAI_API_KEY"
        )
        self._set_theme("v2")
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")
        self.assertEqual(self._status(), ("in_sync", "noop"))

    def test_adopt_preview_says_when_apply_will_replace_the_live_bytes(self) -> None:
        self._configure(_NOTE)
        (self.source / "notes.txt").write_text("source\n", encoding="utf-8")
        live = self.home / "notes.txt"
        live.write_text("live\n", encoding="utf-8")

        (item,) = self._plan().to_dict()["resources"]
        self.assertTrue(item["replaces_live_on_apply"])
        preview = adopt_resource(self._plan(), resource="app", confirm=False)
        (action,) = preview.actions
        self.assertTrue(action.replaces_live_on_apply)
        self.assertTrue(action.to_dict()["replaces_live_on_apply"])

        live.write_text("source\n", encoding="utf-8")
        (item,) = self._plan().to_dict()["resources"]
        self.assertFalse(item["replaces_live_on_apply"])
        preview = adopt_resource(self._plan(), resource="app", confirm=False)
        self.assertNotIn("replaces_live_on_apply", preview.actions[0].to_dict())
