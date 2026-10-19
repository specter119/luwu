"""Writing live changes outside the declared fields back into a literal template."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.cli import main
from luwu.engine import EngineError, accept_baseline, apply_plan, reverse_undeclared
from luwu.rendering import template_syntax
from tests.support.profile_case import ProfileEngineCase

_FONT = ("ui", "font")

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
resources = [{names}]
[inputs.theme]
layers = [{{ file = "local/shared.json", path = ["theme"], format = "json" }}]
reverse_to = {{ file = "local/shared.json", path = ["theme"], format = "json" }}
"""

_APP = """
[resources.{name}]
source = "templates/{source}"
target = "{name}.json"
baseline = "baselines/{name}.json"
{extra}
[resources.{name}.fields.theme]
path = ["theme"]
owner = "merge"
input = "theme"
"""

_TEMPLATE = """{
  "theme": "{{ inputs.theme }}",
  "tag": "{{ inputs.theme }}-suffix",
  "ui": {
    "font": "mono"
  }
}
"""


class UndeclaredReverseTests(ProfileEngineCase):
    def setUp(self) -> None:
        super().setUp()
        self.template = self.source / "templates/app.json.j2"
        self.template.write_text(_TEMPLATE, encoding="utf-8")
        self.template.chmod(0o640)
        self.shared = self.inputs / "local/shared.json"
        self.shared.write_text('{"theme":"v1"}\n', encoding="utf-8")
        self.target = self.home / "app.json"
        self._configure("app")

    def _configure(
        self, *names: str, source: str = "app.json.j2", extra: str = ""
    ) -> None:
        body = _HEAD.format(names=", ".join(json.dumps(name) for name in names))
        for name in names:
            body += _APP.format(name=name, source=source, extra=extra)
        self.manifest_path.write_text(body, encoding="utf-8")

    def _deploy(self, *names: str) -> None:
        for name in names or ("app",):
            accept_baseline(
                self._plan(),
                resource=name,
                fields=("theme",),
                value_from="desired",
                confirm=True,
            )
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")

    def _live(self, edit) -> None:
        live = json.loads(self.target.read_text(encoding="utf-8"))
        edit(live)
        self.target.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")

    def _item(self, name: str = "app") -> dict:
        return next(
            entry
            for entry in self._plan().to_dict()["resources"]
            if entry["name"] == name
        )

    def _reverse(
        self,
        *paths: tuple[str, ...],
        confirm: bool = True,
        plan=None,
        name: str = "app",
    ):
        return reverse_undeclared(
            self._plan() if plan is None else plan,
            resource=name,
            paths=paths or (_FONT,),
            confirm=confirm,
        )

    def _refused(
        self,
        *paths: tuple[str, ...],
        code: str = "undeclared_unsafe",
        name: str = "app",
    ) -> None:
        before = self._snapshot()
        for confirm in (False, True):
            with self.assertRaises(EngineError) as raised:
                self._reverse(*paths, confirm=confirm, name=name)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(self._snapshot(), before)

    def test_a_changed_literal_is_written_to_the_source_only_after_confirmation(
        self,
    ) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(font="serif"))
        self.assertTrue(self._item()["undeclared_changed"])
        before = self._snapshot()

        preview = self._reverse(confirm=False)
        self.assertEqual(preview.outcome, "confirmation_required")
        self.assertEqual(
            [(a.kind, a.path, a.fields) for a in preview.actions],
            [("source_write", "templates/app.json.j2", ())],
        )
        self.assertEqual(self._snapshot(), before)

        result = self._reverse()
        self.assertEqual(result.outcome, "committed")
        self.assertEqual(result.verification_status, "verified")
        self.assertEqual(
            self.template.read_text(encoding="utf-8"),
            _TEMPLATE.replace('"mono"', '"serif"'),
        )
        self.assertEqual(stat.S_IMODE(self.template.stat().st_mode), 0o640)
        item = self._item()
        self.assertFalse(item["undeclared_changed"])
        self.assertEqual((item["status"], item["action"]), ("in_sync", "noop"))
        self.assertEqual(json.loads(self.target.read_text())["ui"]["font"], "serif")

    def test_new_keys_in_a_literal_table_and_new_tables_are_added(self) -> None:
        self._deploy()

        def edit(live: dict) -> None:
            live["ui"]["size"] = 12
            live["extras"] = {"enabled": True}

        self._live(edit)
        result = self._reverse(("ui", "size"), ("extras", "enabled"))
        self.assertEqual(result.outcome, "committed")
        written = json.loads(self.template.read_text(encoding="utf-8"))
        self.assertEqual(written["ui"], {"font": "mono", "size": 12})
        self.assertEqual(written["extras"], {"enabled": True})
        self.assertEqual(written["tag"], "{{ inputs.theme }}-suffix")
        self.assertFalse(self._item()["undeclared_changed"])

    def test_nothing_to_write_back_is_no_change(self) -> None:
        self._deploy()
        before = self._snapshot()
        result = self._reverse()
        self.assertEqual(result.outcome, "no_changes")
        self.assertEqual(self._snapshot(), before)

    def test_a_value_that_jinja_rewrites_is_refused(self) -> None:
        self._deploy()
        self._live(lambda live: live.update(tag="other"))
        self._refused(("tag",))

    def test_a_removed_key_is_refused(self) -> None:
        self._deploy()
        self._live(lambda live: live.pop("ui"))
        self._refused(_FONT)

    def test_arrays_are_refused(self) -> None:
        self._deploy()
        self._live(lambda live: live.update(items=[1, 2]))
        self._refused(("items",))

    def test_multiline_values_are_refused(self) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(font="a\nb"))
        self._refused()

    def test_template_syntax_in_a_live_value_is_refused(self) -> None:
        self._deploy()
        for value in ("{{ inputs.theme }}", "{% if x %}", "{# c #}"):
            with self.subTest(value=value):
                self._live(lambda live, value=value: live["ui"].update(font=value))
                self._refused()

    def test_a_value_that_renders_to_itself_is_still_refused(self) -> None:
        self.shared.write_text('{"theme":"{{ inputs.theme }}"}\n', encoding="utf-8")
        self._deploy()
        self._live(lambda live: live["ui"].update(font="{{ inputs.theme }}"))
        self._refused()

    def test_a_patcher_that_changes_other_content_is_caught_by_the_render(
        self,
    ) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(font="serif"))
        real = engine_module.patch_document
        for old, new in (
            (b'"serif"', b'"other"'),
            (b'"theme": "{{ inputs.theme }}"', b'"theme": "forced"'),
        ):
            with (
                self.subTest(old=old),
                patch.object(
                    engine_module,
                    "patch_document",
                    lambda doc, edits, old=old, new=new: real(doc, edits).replace(
                        old, new
                    ),
                ),
            ):
                self._refused()

    def test_one_unsafe_change_refuses_every_change(self) -> None:
        self._deploy()

        def edit(live: dict) -> None:
            live["ui"]["font"] = "serif"
            live["tag"] = "other"

        self._live(edit)
        self._refused(_FONT, ("tag",))

    def test_a_secret_looking_live_key_blocks_the_resource(self) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(api_key="x"))
        self.assertEqual(self._item()["status"], "blocked")
        self._refused(code="review_required")

    def test_block_tags_in_the_source_are_refused(self) -> None:
        self.template = self.source / "templates/app.jsonc.j2"
        self.template.write_text(
            "{\n  // {% if true %}\n"
            '  "theme": "{{ inputs.theme }}",\n  "ui": {"font": "mono"}\n'
            "  // {% endif %}\n}\n",
            encoding="utf-8",
        )
        self._configure("app", source="app.jsonc.j2")
        self._deploy()
        self.target.write_text(
            '{"theme":"v1","ui":{"font":"serif"}}\n', encoding="utf-8"
        )
        self._refused()

    def test_a_source_that_does_not_parse_is_refused(self) -> None:
        self.template.write_text(
            '{"theme":{{ inputs.theme | tojson }},"ui":{"font":"mono"}}\n',
            encoding="utf-8",
        )
        self._deploy()
        self._live(lambda live: live["ui"].update(font="serif"))
        self._refused()

    def test_a_resource_without_fields_is_not_eligible(self) -> None:
        (self.source / "plain.txt").write_text("x\n", encoding="utf-8")
        self.manifest_path.write_text(
            _HEAD.format(names='"plain"')
            + '[resources.plain]\nsource = "plain.txt"\ntarget = "plain.txt"\n',
            encoding="utf-8",
        )
        self._refused(code="operation_invalid", name="plain")

    def test_a_source_changed_after_the_plan_is_not_overwritten(self) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(font="serif"))
        plan = self._plan()
        self.template.write_text(_TEMPLATE + "\n", encoding="utf-8")
        before = self._snapshot()

        result = self._reverse(plan=plan)

        self.assertEqual(
            (result.outcome, result.error_code), ("not_attempted", "stale_plan")
        )
        self.assertEqual(self._snapshot(), before)

    def test_a_symlinked_source_directory_is_not_followed(self) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(font="serif"))
        plan = self._plan()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.source / "templates").rename(elsewhere / "templates")
        os.symlink(elsewhere / "templates", self.source / "templates")

        result = self._reverse(plan=plan)

        self.assertNotEqual(result.outcome, "committed")
        self.assertIn(
            "mono", (elsewhere / "templates/app.json.j2").read_text(encoding="utf-8")
        )

    def test_cli_requires_exactly_one_scope(self) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(font="serif"))
        base = [
            "reverse-sync",
            "--manifest",
            str(self.manifest_path),
            "--profile",
            "work",
            "--resource",
            "app",
            "--json",
        ]

        def run(*extra: str) -> tuple[int, dict]:
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                try:
                    code = main([*base, *extra])
                except SystemExit as exit_:
                    return int(exit_.code or 0), {}
            return code, json.loads(out.getvalue())

        self.assertEqual(run()[0], 2)
        path = '["ui", "font"]'
        self.assertEqual(run("--undeclared", path, "--field", "theme")[0], 2)
        self.assertEqual(run("--undeclared", "ui.font")[0], 2)
        self.assertEqual(run("--undeclared", '["ui", 1]')[0], 2)
        self.assertEqual(run("--undeclared", "[]")[0], 2)
        code, preview = run("--undeclared", path)
        self.assertEqual((code, preview["outcome"]), (2, "confirmation_required"))
        self.assertIn("mono", self.template.read_text(encoding="utf-8"))
        code, done = run("--undeclared", path, "--yes")
        self.assertEqual((code, done["outcome"]), (0, "committed"))
        self.assertIn("serif", self.template.read_text(encoding="utf-8"))

    def test_only_the_named_paths_are_written(self) -> None:
        self._deploy()

        def edit(live: dict) -> None:
            live["ui"]["font"] = "serif"
            live["ui"]["size"] = 12

        self._live(edit)

        result = self._reverse(_FONT)

        self.assertEqual(result.outcome, "committed")
        written = json.loads(self.template.read_text(encoding="utf-8"))
        self.assertEqual(written["ui"], {"font": "serif"})
        self.assertTrue(self._item()["undeclared_changed"])

    def test_a_path_that_does_not_differ_is_refused(self) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(font="serif"))
        for paths in ((("ui", "size"),), (_FONT, ("ui", "size")), ()):
            with self.subTest(paths=paths):
                before = self._snapshot()
                with self.assertRaises(EngineError) as raised:
                    reverse_undeclared(
                        self._plan(), resource="app", paths=paths, confirm=True
                    )
                self.assertEqual(raised.exception.code, "undeclared_unsafe")
                self.assertEqual(self._snapshot(), before)

    def test_a_secret_looking_value_is_written_only_for_a_named_path(self) -> None:
        self._deploy()

        def edit(live: dict) -> None:
            live["ui"]["font"] = "serif"
            live["ui"]["url"] = "https://user:pw@host"

        self._live(edit)
        self._reverse(_FONT)
        self.assertNotIn("pw@host", self.template.read_text(encoding="utf-8"))

    def test_a_patch_that_changes_the_template_syntax_is_refused(self) -> None:
        self._deploy()
        self._live(lambda live: live["ui"].update(font="serif"))
        real = engine_module.patch_document
        with patch.object(
            engine_module,
            "patch_document",
            lambda doc, edits: real(doc, edits).replace(
                b"{{ inputs.theme }}-suffix", b"{{ inputs.theme }}{{ '' }}-suffix"
            ),
        ):
            self._refused()

    def test_template_syntax_ignores_text_and_sees_expressions(self) -> None:
        base = 'a "{{ x }}" b'
        self.assertEqual(template_syntax(base), template_syntax('c "{{ x }}" d'))
        self.assertNotEqual(template_syntax(base), template_syntax('a "{{ y }}" b'))
        self.assertNotEqual(template_syntax(base), template_syntax("a b"))
        self.assertNotEqual(
            template_syntax("{{ 'a' }}"), template_syntax("{{ 'a' ~ 'b' }}")
        )
