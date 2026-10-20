"""Shared fixture for the version 8 profile engine tests."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import traceback
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.engine import (
    EngineError,
    Plan,
    accept_baseline,
    apply_plan,
    build_plan,
)
from luwu.manifest import load_manifest

_CheckInputs = Callable[[], None] | None


class ProfileEngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="luwu-test-engine-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.home = self.root / "home"
        for path in (
            self.source / "templates",
            self.inputs / "global",
            self.inputs / "local",
            self.inputs / "baselines",
            self.home,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "luwu.toml"
        self._isolate_home()
        self._write_template_case()

    def _isolate_home(self) -> None:
        environment = patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def _write_template_case(self) -> None:
        (self.source / "templates" / "settings.json.j2").write_text(
            '{"settings":{"theme":{{ inputs.theme | tojson }},'
            '"brand":{{ inputs.brand | tojson }},'
            '"globalBrand":{{ global_inputs.brand | tojson }},'
            '"localBrand":{{ local_inputs.brand | tojson }}},'
            '"unmanaged":{"source":"unmanaged-source-v1"}}\n',
            encoding="utf-8",
        )
        (self.inputs / "global" / "settings.json").write_text(
            '{"values":{"theme":"global-theme-secret-sentinel","brand":"brand-v1"}}\n',
            encoding="utf-8",
        )
        (self.inputs / "local" / "settings.json").write_text(
            '{"overrides":{"theme":"overlay-theme-v1"},'
            '"branding":"brand-local-v1",'
            '"retained":"local-retained-v1"}\n',
            encoding="utf-8",
        )
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
resources = ["settings"]

[inputs.theme]
layers = [
  { file = "global/settings.json", path = ["values", "theme"], format = "json" },
  { file = "local/settings.json", path = ["overrides", "theme"], format = "json" },
]
reverse_to = { file = "local/settings.json", path = ["overrides", "theme"], format = "json" }

[inputs.brand]
layers = [
  { file = "global/settings.json", path = ["values", "brand"], format = "json" },
  { file = "local/settings.json", path = ["branding"], format = "json" },
]

[resources.settings]
source = "templates/settings.json.j2"
target = ".config/app/settings.json"
format = "json"
baseline = "baselines/settings.json"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.settings.fields.theme]
path = ["settings", "theme"]
owner = "merge"
input = "theme"

[resources.settings.fields.brand]
path = ["settings", "brand"]
owner = "source"
input = "brand"
""",
            encoding="utf-8",
        )

    def _load(self):
        return load_manifest(self.manifest_path, profile="work")

    def _plan(self):
        return build_plan(self._load())

    def _target(self) -> Path:
        return self.home / ".config" / "app" / "settings.json"

    def _snapshot(self) -> dict[str, tuple[object, ...]]:
        state: dict[str, tuple[object, ...]] = {}
        for name, base in (
            ("source", self.source),
            ("inputs", self.inputs),
            ("home", self.home),
        ):
            for path in sorted(base.rglob("*")):
                info = path.lstat()
                relative = f"{name}/{path.relative_to(base).as_posix()}"
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISLNK(info.st_mode):
                    state[relative] = ("symlink", os.readlink(path), mode)
                elif stat.S_ISDIR(info.st_mode):
                    state[relative] = ("directory", mode)
                elif stat.S_ISREG(info.st_mode):
                    state[relative] = ("file", path.read_bytes(), mode)
                else:
                    state[relative] = ("other", mode)
        return state

    def _assert_stale_reauthorization_is_readonly(self, plan: Plan) -> None:
        before = self._snapshot()
        with (
            patch("luwu.engine.write_file", wraps=engine_module.write_file) as writer,
            patch(
                "luwu.engine.create_parent", wraps=engine_module.create_parent
            ) as mkdir,
        ):
            result = apply_plan(plan, confirm=True)
        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "stale_plan")
        self.assertTrue(
            all(action.state == "not_attempted" for action in result.actions)
        )
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(mkdir.call_count, 0)
        self.assertEqual(self._snapshot(), before)

    def _assert_invalid_reauthorization_is_readonly(self, plan: Plan) -> None:
        before = self._snapshot()
        with (
            patch("luwu.engine.write_file", wraps=engine_module.write_file) as writer,
            patch(
                "luwu.engine.create_parent", wraps=engine_module.create_parent
            ) as mkdir,
            self.assertRaises(EngineError) as raised,
        ):
            apply_plan(plan, confirm=True)
        self.assertEqual(raised.exception.code, "plan_invalid")
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(mkdir.call_count, 0)
        self.assertEqual(self._snapshot(), before)

    def _assert_detached_error(self, error: BaseException, marker: str) -> None:
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertNotIn(marker, str(error))
        self.assertNotIn(marker, repr(error))
        self.assertNotIn(marker, "".join(traceback.format_exception(error)))

    def _accept_template_baseline(self) -> None:
        result = accept_baseline(
            self._plan(),
            resource="settings",
            fields=("theme", "brand"),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(result.outcome, "committed")

    def _deploy_template(self) -> None:
        self._accept_template_baseline()
        result = apply_plan(self._plan(), confirm=True)
        self.assertEqual(result.outcome, "committed")

    def _add_review_resource(self) -> Path:
        review_source = self.source / "templates" / "review.json.j2"
        review_source.write_bytes(
            (self.source / "templates" / "settings.json.j2").read_bytes()
        )
        manifest = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(
            manifest.replace(
                'resources = ["settings"]',
                'resources = ["settings", "review"]',
            )
            + """

[resources.review]
source = "templates/review.json.j2"
target = ".config/review.json"
format = "json"
baseline = "baselines/review.json"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.review.fields.theme]
path = ["settings", "theme"]
owner = "merge"
input = "theme"

[resources.review.fields.brand]
path = ["settings", "brand"]
owner = "source"
input = "brand"
""",
            encoding="utf-8",
        )
        return self.home / ".config" / "review.json"

    def _configure_copy_batch(self, names: tuple[str, ...]) -> None:
        resource_list = ", ".join(json.dumps(name) for name in names)
        sections = "\n".join(
            f"""
[resources.{name}]
deploy = "copy"
source = "{name}.txt"
target = "{name}.txt"
baseline = "baselines/{name}.json"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
"""
            for name in names
        )
        self.manifest_path.write_text(
            f"""
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
resources = [{resource_list}]
[inputs]
{sections}
""",
            encoding="utf-8",
        )
