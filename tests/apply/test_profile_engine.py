"""Tests for profile engine."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import traceback
import unittest
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from luwu import m7_engine, m7_manifest
from luwu.errors import LuwuError
from luwu.m7_baseline import M7BaselineError, load_baseline
from luwu.m7_engine import (
    M7EngineError,
    Plan,
    accept_baseline,
    adopt_resource,
    apply_plan,
    build_plan,
    reverse_inputs,
)
from luwu.m7_filesystem import FileObservation
from luwu.m7_formats import M7FormatError, parse_document
from luwu.m7_inputs import M7InputError, load_inputs
from luwu.m7_manifest import Manifest, Resource, load_manifest

_CheckInputs = Callable[[], None] | None


class ProfileEngineV8Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="luwu-m7-engine-")
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
kind = "template"
source = "templates/settings.json.j2"
target = ".config/app/settings.json"
format = "json"
scope = "fields"
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
            patch("luwu.m7_engine.write_file", wraps=m7_engine.write_file) as writer,
            patch(
                "luwu.m7_engine.create_parent", wraps=m7_engine.create_parent
            ) as mkdir,
        ):
            result = apply_plan(plan, confirm=True)
        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "m7_stale_plan")
        self.assertTrue(
            all(action.state == "not_attempted" for action in result.actions)
        )
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(mkdir.call_count, 0)
        self.assertEqual(self._snapshot(), before)

    def _assert_invalid_reauthorization_is_readonly(self, plan: Plan) -> None:
        before = self._snapshot()
        with (
            patch("luwu.m7_engine.write_file", wraps=m7_engine.write_file) as writer,
            patch(
                "luwu.m7_engine.create_parent", wraps=m7_engine.create_parent
            ) as mkdir,
            self.assertRaises(M7EngineError) as raised,
        ):
            apply_plan(plan, confirm=True)
        self.assertEqual(raised.exception.code, "m7_plan_invalid")
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(mkdir.call_count, 0)
        self.assertEqual(self._snapshot(), before)

    def test_input_namespaces_use_root_relative_top_level_segment(self) -> None:
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
        self._write_template_case()

        manifest = self._load()
        context = load_inputs(manifest, manifest.resources[0])
        variables = context.template_variables()
        global_inputs = variables["global_inputs"]
        local_inputs = variables["local_inputs"]

        self.assertEqual(global_inputs["brand"], "brand-v1")
        self.assertEqual(local_inputs["brand"], "brand-local-v1")
        self.assertEqual(local_inputs["theme"], "overlay-theme-v1")

    def test_input_and_baseline_decode_errors_are_detached(self) -> None:
        manifest = self._load()
        resource = manifest.resources[0]
        marker = "M7_RAW_UTF8_MARKER"

        (self.inputs / "global" / "settings.json").write_bytes(
            b'{"values":{"theme":"' + marker.encode() + b'\xff"}}'
        )
        with self.assertRaises(M7InputError) as input_error:
            load_inputs(manifest, resource)
        self._assert_detached_error(input_error.exception, marker)

        (self.inputs / "baselines" / "settings.json").write_bytes(
            b'{"schema_version":2,"private_marker":"' + marker.encode() + b'\xff"}'
        )
        with self.assertRaises(M7BaselineError) as baseline_error:
            load_baseline(manifest, resource)
        self._assert_detached_error(baseline_error.exception, marker)

    def test_unknown_error_codes_are_not_serialized(self) -> None:
        marker = "m7_SYNTHETIC_MARKER"
        with patch(
            "luwu.m7_engine.load_inputs",
            side_effect=LuwuError("hidden", code=marker),
        ):
            plan = self._plan()

        resource = plan.to_dict()["resources"][0]
        assert isinstance(resource, dict)
        self.assertEqual(resource["error_code"], "m7_resource_blocked")
        public_output = json.dumps(plan.to_dict(), sort_keys=True) + repr(plan)
        self.assertNotIn(marker, public_output)
        self.assertNotIn("hidden", public_output)
        self.assertEqual(m7_engine._safe_code(123), "m7_resource_blocked")
        self.assertEqual(m7_engine._safe_code(marker), "m7_resource_blocked")
        self.assertEqual(m7_engine._safe_code("m7_input_invalid"), "m7_input_invalid")

    def test_plan_evidence_rechecks_complete_projection(self) -> None:
        plan = self._plan()
        evidence = m7_engine._PLAN_REGISTRY[plan].evidence
        self.assertTrue(
            m7_engine._matches_plan_evidence(m7_engine._plan_projection(plan), evidence)
        )

        plan._resources[0].desired_document._root.start = -1
        self.assertFalse(
            m7_engine._matches_plan_evidence(m7_engine._plan_projection(plan), evidence)
        )

        self.assertFalse(
            m7_engine._matches_plan_evidence(
                [True], m7_engine._capture_plan_evidence([1])
            )
        )
        self.assertFalse(
            m7_engine._matches_plan_evidence(
                [Decimal("1.00")], m7_engine._capture_plan_evidence([Decimal("1.0")])
            )
        )

        shared: list[object] = ["same", {"leaf": 1}]
        shared_graph = [shared, shared]
        duplicated_graph = [["same", {"leaf": 1}], ["same", {"leaf": 1}]]
        self.assertTrue(
            m7_engine._matches_plan_evidence(
                duplicated_graph, m7_engine._capture_plan_evidence(shared_graph)
            )
        )

    def test_plan_evidence_is_bounded_for_deep_parser_graphs(self) -> None:
        for depth in (8, 20, 32, 64, 128):
            with self.subTest(depth=depth):
                data = b'{"k":' * depth + b"1" + b"}" * depth
                document = parse_document(data, format="json")
                projection = m7_engine._document_value(document)
                evidence = m7_engine._capture_plan_evidence(projection)
                self.assertLess(len(evidence.records), depth * 20)
                self.assertTrue(m7_engine._matches_plan_evidence(projection, evidence))

                reparsed = parse_document(data, format="json")
                self.assertTrue(
                    m7_engine._matches_plan_evidence(
                        m7_engine._document_value(reparsed), evidence
                    )
                )
                document._root.start = -1
                self.assertFalse(
                    m7_engine._matches_plan_evidence(
                        m7_engine._document_value(document), evidence
                    )
                )

        deep_dag: object = {"leaf": 1}
        for _ in range(64):
            deep_dag = [deep_dag, deep_dag]
        evidence = m7_engine._capture_plan_evidence(deep_dag)
        self.assertLess(len(evidence.records), 500)
        self.assertTrue(m7_engine._matches_plan_evidence(deep_dag, evidence))

    def test_cyclic_plan_graph_is_rejected_before_writing(self) -> None:
        plan = self._plan()
        before = self._snapshot()
        document = plan._resources[0].desired_document
        assert document is not None
        desired_value = document.value
        desired_value["cycle"] = desired_value

        with self.assertRaises(M7EngineError) as raised:
            apply_plan(plan, confirm=True)

        self.assertEqual(raised.exception.code, "m7_plan_invalid")
        self.assertEqual(self._snapshot(), before)

    def test_plan_reauthorization_covers_jsonc_toml_and_shared_inputs(self) -> None:
        (self.inputs / "local" / "shared.json").write_text(
            '{"brand":"shared-v1"}\n', encoding="utf-8"
        )
        template_cases = {
            "jsonc": (
                "jsonc.jsonc.j2",
                '{"settings":{/* keep comment */"brand":{{ inputs.brand | tojson }}}}\n',
                ".config/m7-jsonc.jsonc",
                "jsonc",
            ),
            "toml_table": (
                "toml-table.toml.j2",
                "[settings]\nbrand = {{ inputs.brand | tojson }}\n",
                ".config/m7-table.toml",
                "toml",
            ),
            "toml_inline": (
                "toml-inline.toml.j2",
                "settings = { brand = {{ inputs.brand | tojson }} }\n",
                ".config/m7-inline.toml",
                "toml",
            ),
        }
        resource_sections: list[str] = []
        for name, (
            source_name,
            template,
            target,
            format_name,
        ) in template_cases.items():
            (self.source / "templates" / source_name).write_text(
                template, encoding="utf-8"
            )
            resource_sections.append(
                f"""
[resources.{name}]
kind = "template"
source = "templates/{source_name}"
target = "{target}"
format = "{format_name}"
scope = "fields"
baseline = "baselines/{name}.json"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.{name}.fields.brand]
path = ["settings", "brand"]
owner = "merge"
input = "brand"
"""
            )

        self.manifest_path.write_text(
            """
version = 8

[roots]
source = "source"
inputs = "inputs"
target = "home"

[profiles.work]
packages = ["formats"]
exclude = []

[packages.formats]
dependencies = []
resources = ["jsonc", "toml_table", "toml_inline"]

[inputs.brand]
layers = [
  { file = "local/shared.json", path = ["brand"], format = "json" },
]
"""
            + "\n".join(resource_sections),
            encoding="utf-8",
        )

        plan = build_plan(load_manifest(self.manifest_path, profile="work"))
        authorized = m7_engine._require_plan(plan, operation="apply")
        self.assertEqual(
            {
                item.resource.name: item.desired_document._format
                for item in authorized.resources
                if item.desired_document is not None
            },
            {"jsonc": "jsonc", "toml_table": "toml", "toml_inline": "toml"},
        )
        self.assertEqual(
            sum(item.desired_document is not None for item in authorized.resources), 3
        )
        self.assertFalse(
            any(
                (self.home / target).exists()
                for _, _, target, _ in template_cases.values()
            )
        )

        (self.inputs / "local" / "shared.json").write_text(
            '{"brand":"shared-v2"}\n', encoding="utf-8"
        )
        with self.assertRaises(M7EngineError) as stale:
            m7_engine._require_plan(plan, operation="apply")
        self.assertEqual(stale.exception.code, "m7_stale_plan")

        fresh_plan = build_plan(load_manifest(self.manifest_path, profile="work"))
        fresh_authorized = m7_engine._require_plan(fresh_plan, operation="apply")
        self.assertEqual(len(fresh_authorized.resources), 3)

    def test_json_parser_depth_boundary_is_a_fixed_format_error(self) -> None:
        depth = 1500
        data = b'{"nested":' * depth + b"0" + b"}" * depth
        with self.assertRaises(M7FormatError) as raised:
            parse_document(data, format="json")
        self.assertEqual(raised.exception.code, "m7_format")
        self.assertEqual(
            str(raised.exception), "document format or syntax is unsupported"
        )

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
kind = "template"
source = "templates/review.json.j2"
target = ".config/review.json"
format = "json"
scope = "fields"
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
kind = "copy"
source = "{name}.txt"
target = "{name}.txt"
baseline = "baselines/{name}.json"
scope = "whole-file"
owner = "source"
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

    def test_plan_hides_values_and_confirmation_gates_parent_creation(self) -> None:
        plan = self._plan()
        payload = plan.to_dict()
        encoded = json.dumps(payload, sort_keys=True)
        self.assertEqual(payload["schema_version"], 8)
        self.assertNotIn("global-theme-secret-sentinel", encoded)
        self.assertNotIn("brand-v1", encoded)
        self.assertNotIn("unmanaged-source-v1", encoded)
        self.assertNotIn("digest", repr(plan).casefold())

        resource = payload["resources"][0]
        self.assertEqual(resource["status"], "unbased")
        self.assertEqual(resource["mode"], "0o0600")
        self.assertEqual(resource["effective_mode"], "0o0600")
        provenance = resource["input_provenance"]
        theme = next(item for item in provenance if item["binding"] == "theme")
        self.assertEqual(theme["winner"]["file"], "local/settings.json")
        self.assertEqual(theme["winner"]["layer"], 1)
        self.assertEqual(
            [entry["file"] for entry in theme["present_layers"]],
            ["global/settings.json", "local/settings.json"],
        )
        self.assertEqual(
            [entry["status"] for entry in theme["layers"]],
            ["present", "present"],
        )

        before = self._snapshot()
        preview = accept_baseline(
            plan,
            resource="settings",
            fields=("theme", "brand"),
            value_from="desired",
            confirm=False,
        )
        self.assertEqual(preview.outcome, "confirmation_required")
        self.assertTrue(
            all(action.state == "not_attempted" for action in preview.actions)
        )
        self.assertEqual(self._snapshot(), before)

        accepted = accept_baseline(
            plan,
            resource="settings",
            fields=("theme", "brand"),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(accepted.outcome, "committed")
        baseline = json.loads((self.inputs / "baselines" / "settings.json").read_text())
        self.assertEqual(set(baseline), {"schema_version", "identity", "values"})
        self.assertEqual(baseline["schema_version"], 2)
        self.assertEqual(set(baseline["values"]), {"theme", "brand"})
        identity = baseline["identity"]
        self.assertEqual(identity["profile"], "work")
        self.assertEqual(
            identity["roots"],
            {
                "source": {"path": str(self.source.resolve())},
                "inputs": {"path": str(self.inputs.resolve())},
                "target": {"path": str(self.home.resolve())},
            },
        )
        self.assertEqual(
            identity["mapping"],
            {
                "brand": {
                    "path": ["settings", "brand"],
                    "owner": "source",
                    "input": "brand",
                },
                "theme": {
                    "path": ["settings", "theme"],
                    "owner": "merge",
                    "input": "theme",
                },
            },
        )
        self.assertEqual(
            identity["inputs"]["theme"],
            {
                "layers": [
                    {
                        "file": "global/settings.json",
                        "path": ["values", "theme"],
                        "format": "json",
                        "optional": False,
                    },
                    {
                        "file": "local/settings.json",
                        "path": ["overrides", "theme"],
                        "format": "json",
                        "optional": False,
                    },
                ],
                "reverse_to": {
                    "file": "local/settings.json",
                    "path": ["overrides", "theme"],
                    "format": "json",
                    "optional": False,
                },
            },
        )
        self.assertEqual(
            identity["inputs"]["brand"],
            {
                "layers": [
                    {
                        "file": "global/settings.json",
                        "path": ["values", "brand"],
                        "format": "json",
                        "optional": False,
                    },
                    {
                        "file": "local/settings.json",
                        "path": ["branding"],
                        "format": "json",
                        "optional": False,
                    },
                ],
                "reverse_to": None,
            },
        )
        self.assertTrue(
            all(
                set(root_identity) == {"path"}
                for root_identity in baseline["identity"]["roots"].values()
            )
        )

        after_accept = self._snapshot()
        apply_plan_preview = apply_plan(self._plan(), confirm=False)
        self.assertEqual(apply_plan_preview.outcome, "confirmation_required")
        self.assertEqual(self._snapshot(), after_accept)
        parent_actions = self._plan().to_dict()["resources"][0]["parent_actions"][
            "target"
        ]
        self.assertEqual(
            [item["path"] for item in parent_actions],
            [".config", ".config/app"],
        )
        self.assertTrue(all(item["mode"] == "0o0700" for item in parent_actions))

        applied = apply_plan(self._plan(), confirm=True)
        self.assertEqual(applied.outcome, "committed")
        self.assertTrue(
            any(
                action.kind == "mkdir" and action.state == "committed"
                for action in applied.actions
            )
        )
        self.assertEqual(stat.S_IMODE((self.home / ".config").stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.home / ".config" / "app").stat().st_mode), 0o700
        )
        self.assertEqual(stat.S_IMODE(self._target().stat().st_mode), 0o600)
        deployed = json.loads(self._target().read_text(encoding="utf-8"))
        self.assertEqual(deployed["settings"]["brand"], "brand-local-v1")
        self.assertEqual(deployed["settings"]["globalBrand"], "brand-v1")
        self.assertEqual(deployed["settings"]["localBrand"], "brand-local-v1")
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "no_changes")

    def test_plan_reports_effective_mode_for_new_and_existing_targets(self) -> None:
        manifest = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(
            manifest.replace('mode = "0600"\n', ""), encoding="utf-8"
        )

        missing = self._plan().to_dict()["resources"][0]
        self.assertIsNone(missing["mode"])
        self.assertEqual(missing["effective_mode"], "0o0644")
        self.assertEqual(self._plan().to_dict()["schema_version"], 8)

        target = self._target()
        original_observe = m7_engine.observe_file

        def fail_target_observation(root: Path, path: Path) -> FileObservation:
            if path == target:
                raise m7_engine.M7FilesystemError(
                    "private injected failure", code="m7_unsafe_path"
                )
            return original_observe(root, path)

        with patch("luwu.m7_engine.observe_file", side_effect=fail_target_observation):
            unavailable = self._plan().to_dict()["resources"][0]
        self.assertIsNone(unavailable["effective_mode"])

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"settings":{"theme":"live","brand":"live"}}\n')
        target.chmod(0o640)
        existing = self._plan().to_dict()["resources"][0]
        self.assertIsNone(existing["mode"])
        self.assertEqual(existing["effective_mode"], "0o0640")

    def test_hand_built_or_replaced_plan_is_not_an_apply_capability(self) -> None:
        plan = self._plan()
        before = self._snapshot()
        for candidate in (replace(plan), Plan(plan.manifest, ())):
            with (
                self.subTest(candidate=repr(candidate)),
                self.assertRaises(M7EngineError) as raised,
            ):
                apply_plan(candidate, confirm=True)
            self.assertEqual(raised.exception.code, "m7_plan_invalid")
        self.assertEqual(self._snapshot(), before)
        plan._resources[0].desired_document.value["settings"]["theme"] = (
            "mutated-private-plan-value"
        )
        with self.assertRaises(M7EngineError) as raised:
            apply_plan(plan, confirm=True)
        self.assertEqual(raised.exception.code, "m7_plan_invalid")
        self.assertEqual(self._snapshot(), before)

        plan = self._plan()
        object.__setattr__(plan._resources[0], "write_data", b'{"x":999}')
        with self.assertRaises(M7EngineError) as raised:
            apply_plan(plan, confirm=True)
        self.assertEqual(raised.exception.code, "m7_plan_invalid")
        self.assertEqual(self._snapshot(), before)

    def test_reauthorization_reuses_manifest_and_keeps_full_fresh_guards(self) -> None:
        plan = self._plan()
        before = self._snapshot()
        original_verify = m7_engine.verify_manifest
        original_observe = m7_engine._observe_all
        with (
            patch(
                "luwu.m7_manifest._load_manifest",
                side_effect=AssertionError("reauthorization reloaded the manifest"),
            ),
            patch("luwu.m7_engine.verify_manifest", wraps=original_verify) as verify,
            patch("luwu.m7_engine._observe_all", wraps=original_observe) as observe,
        ):
            authorized = m7_engine._require_plan(plan, operation="apply")

        self.assertIs(authorized.manifest, plan.manifest)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(self._snapshot(), before)

    def test_after_plan_target_hardlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        target = self._target()
        target.parent.mkdir(parents=True)
        alias = self.root / "target-hardlink"
        alias.write_bytes(b"{}\n")
        target.hardlink_to(alias)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_after_plan_input_hardlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        input_path = self.inputs / "local" / "settings.json"
        alias = self.root / "input-hardlink"
        alias.write_bytes(input_path.read_bytes())
        input_path.unlink()
        input_path.hardlink_to(alias)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_after_plan_baseline_hardlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        baseline = self.inputs / "baselines" / "settings.json"
        alias = self.root / "baseline-hardlink"
        alias.write_bytes(b"{}\n")
        baseline.hardlink_to(alias)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_after_plan_cross_role_hardlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        target = self._target()
        target.parent.mkdir(parents=True)
        input_path = self.inputs / "local" / "settings.json"
        target.hardlink_to(input_path)
        self.assertEqual(target.stat().st_ino, input_path.stat().st_ino)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_after_plan_parent_symlink_blocks_before_any_write(self) -> None:
        plan = self._plan()
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / ".config").symlink_to(outside, target_is_directory=True)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_same_bytes_manifest_inode_replacement_blocks_before_any_write(
        self,
    ) -> None:
        plan = self._plan()
        original_identity = (
            self.manifest_path.stat().st_dev,
            self.manifest_path.stat().st_ino,
        )
        replacement = self.root / "replacement.toml"
        replacement.write_bytes(self.manifest_path.read_bytes())
        self.assertEqual(replacement.read_bytes(), self.manifest_path.read_bytes())
        self.assertNotEqual(
            (replacement.stat().st_dev, replacement.stat().st_ino), original_identity
        )
        replacement.replace(self.manifest_path)
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_directory_membership_change_blocks_before_any_write(self) -> None:
        assets = self.source / "assets"
        assets.mkdir()
        (assets / "one.conf").write_text("one\n", encoding="utf-8")
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
        plan = self._plan()
        (assets / "added.conf").write_text("added\n", encoding="utf-8")
        self._assert_stale_reauthorization_is_readonly(plan)

    def test_resigned_binding_and_layer_mutations_block_before_any_write(self) -> None:
        binding_plan = self._plan()
        binding = binding_plan.manifest.resources[0].fields["brand"]
        object.__setattr__(binding, "owner", "merge")
        object.__setattr__(
            binding_plan.manifest,
            "_signature",
            m7_manifest._manifest_signature(binding_plan.manifest),
        )
        with self.subTest(mutation="field binding"):
            self._assert_invalid_reauthorization_is_readonly(binding_plan)

        layer_plan = self._plan()
        layer = layer_plan.manifest.inputs["brand"].layers[0]
        object.__setattr__(layer, "path", ("values", "theme"))
        object.__setattr__(
            layer_plan.manifest,
            "_signature",
            m7_manifest._manifest_signature(layer_plan.manifest),
        )
        with self.subTest(mutation="input layer"):
            self._assert_invalid_reauthorization_is_readonly(layer_plan)

    def test_post_authorization_plan_mutation_keeps_authorized_write_bytes(
        self,
    ) -> None:
        self._accept_template_baseline()
        plan = self._plan()
        expected = plan._resources[0].write_data
        self.assertIsNotNone(expected)
        original_specs = m7_engine._forward_specs

        def mutate_original_plan(resources, manifest):
            object.__setattr__(plan._resources[0], "write_data", b"forged\n")
            return original_specs(resources, manifest)

        with patch("luwu.m7_engine._forward_specs", side_effect=mutate_original_plan):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(self._target().read_bytes(), expected)
        self.assertNotEqual(self._target().read_bytes(), b"forged\n")

    def test_forward_write_spec_is_rebuilt_from_fresh_observation(self) -> None:
        self._accept_template_baseline()
        local = self.inputs / "local" / "settings.json"
        inputs = json.loads(local.read_text(encoding="utf-8"))
        inputs["overrides"]["theme"] = "overlay-theme-v2"
        local.write_text(json.dumps(inputs) + "\n", encoding="utf-8")
        target = self._target()
        original_spec = m7_engine._forward_specs_for_item
        calls = 0

        def poison_cached_spec(
            item: m7_engine._ResourcePlan, manifest: m7_engine.Manifest
        ) -> m7_engine._WriteSpec | None:
            nonlocal calls
            spec = original_spec(item, manifest)
            calls += 1
            if calls == 1 and spec is not None:
                return replace(spec, data=b'{"settings":{"theme":"forged"}}\n')
            return spec

        with patch(
            "luwu.m7_engine._forward_specs_for_item",
            side_effect=poison_cached_spec,
        ):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertGreaterEqual(calls, 2)
        written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(written["settings"]["theme"], "overlay-theme-v2")

    def test_review_in_any_resource_blocks_the_whole_forward_batch(self) -> None:
        review_target = self._add_review_resource()
        for resource in ("settings", "review"):
            result = accept_baseline(
                self._plan(),
                resource=resource,
                fields=("theme", "brand"),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(result.outcome, "committed")
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")

        settings_before = self._target().read_bytes()
        review = json.loads(review_target.read_text(encoding="utf-8"))
        review["unmanaged"]["source"] = "review-unknown-sentinel"
        review_target.write_text(json.dumps(review) + "\n", encoding="utf-8")
        local = self.inputs / "local" / "settings.json"
        inputs = json.loads(local.read_text(encoding="utf-8"))
        inputs["overrides"]["theme"] = "overlay-theme-v2"
        local.write_text(json.dumps(inputs) + "\n", encoding="utf-8")

        plan = self._plan()
        self.assertTrue(
            any(item["undeclared_changed"] for item in plan.to_dict()["resources"])
        )
        preview = apply_plan(plan, confirm=False)
        self.assertEqual(preview.outcome, "not_attempted")
        self.assertEqual(preview.error_code, "m7_review_required")
        result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "m7_review_required")
        self.assertTrue(
            all(action.state == "not_attempted" for action in result.actions)
        )
        self.assertEqual(self._target().read_bytes(), settings_before)
        self.assertIn("review-unknown-sentinel", review_target.read_text())

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
        self.assertEqual(resource["error_code"], "m7_review_required")
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
        with self.assertRaises(M7EngineError) as raised:
            accept_baseline(
                plan,
                resource="settings",
                fields=("theme",),
                value_from="desired",
                confirm=True,
            )
        self.assertEqual(raised.exception.code, "m7_review_required")
        self.assertEqual(self._snapshot(), before)
        apply_result = apply_plan(plan, confirm=True)
        self.assertEqual(apply_result.outcome, "not_attempted")
        self.assertEqual(apply_result.error_code, "m7_review_required")
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
        with self.assertRaises(M7EngineError) as raised:
            reverse_inputs(
                plan,
                resource="settings",
                fields=("theme",),
                confirm=True,
            )
        self.assertEqual(raised.exception.code, "m7_review_required")
        self.assertEqual(self._snapshot(), before)

        target = self._target()
        live = json.loads(target.read_text(encoding="utf-8"))
        del live["settings"]["theme"]
        target.write_text(json.dumps(live) + "\n", encoding="utf-8")
        source_input = json.loads(local.read_text(encoding="utf-8"))
        source_input["overrides"]["theme"] = "overlay-theme-v1"
        local.write_text(json.dumps(source_input) + "\n", encoding="utf-8")
        before = self._snapshot()
        with self.assertRaises(M7EngineError) as raised:
            reverse_inputs(
                self._plan(),
                resource="settings",
                fields=("theme",),
                confirm=True,
            )
        self.assertEqual(raised.exception.code, "m7_reverse_delete")
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
        self.assertEqual(resource["error_code"], "m7_baseline_invalid")

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
            self._plan().to_dict()["resources"][0]["error_code"], "m7_baseline_invalid"
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
        self.assertEqual(stale["error_code"], "m7_baseline_stale")

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
            self.assertEqual(blocked["error_code"], "m7_baseline_invalid")
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        accepted_mode = stat.S_IMODE(target.stat().st_mode)
        target.chmod(0o600)
        self.assertEqual(self._plan().to_dict()["resources"][0]["status"], "conflict")
        target.chmod(accepted_mode)

        target.write_text("live-note-v3\n", encoding="utf-8")
        conflict_plan = self._plan()
        self.assertEqual(conflict_plan.to_dict()["resources"][0]["status"], "conflict")
        before = self._snapshot()
        with self.assertRaises(M7EngineError):
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
        baseline_files = tuple((self.inputs / "baselines").glob("assets.m7-*"))
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
        self.assertEqual(stale.error_code, "m7_stale_plan")
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

    def test_stale_noop_recheck_and_partial_batch_preserve_action_evidence(
        self,
    ) -> None:
        self._deploy_template()
        no_op_plan = self._plan()
        target = self._target()
        target.write_text(
            target.read_text().replace("unmanaged-source-v1", "external-v2"),
            encoding="utf-8",
        )
        stale = apply_plan(no_op_plan, confirm=True)
        self.assertEqual(stale.outcome, "not_attempted")
        self.assertEqual(stale.error_code, "m7_stale_plan")

        (self.source / "a.txt").write_text("source-a-v2\n", encoding="utf-8")
        (self.source / "b.txt").write_text("source-b-v2\n", encoding="utf-8")
        (self.home / "a.txt").write_text("live-a-v1\n", encoding="utf-8")
        (self.home / "b.txt").write_text("live-b-v1\n", encoding="utf-8")
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
resources = ["a", "b"]
[inputs]
[resources.a]
kind = "copy"
source = "a.txt"
target = "a.txt"
baseline = "baselines/a.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
[resources.b]
kind = "copy"
source = "b.txt"
target = "b.txt"
baseline = "baselines/b.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
""",
            encoding="utf-8",
        )
        adopt_resource(self._plan(), resource="a", confirm=True)
        adopt_resource(self._plan(), resource="b", confirm=True)
        original_write = m7_engine.write_file
        target_b = self.home / "b.txt"

        def write_then_change_other_resource(
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
            if path == self.home / "a.txt":
                target_b.write_text("external-b-v3\n", encoding="utf-8")
            return identity

        with patch(
            "luwu.m7_engine.write_file", side_effect=write_then_change_other_resource
        ):
            partial = apply_plan(self._plan(), confirm=True)
        self.assertEqual(partial.outcome, "partial")
        states = {
            (action.resource, action.kind): action.state for action in partial.actions
        }
        self.assertEqual(states[("a", "target_write")], "committed")
        self.assertEqual(states[("b", "target_write")], "not_attempted")
        self.assertEqual(
            (self.home / "a.txt").read_text(encoding="utf-8"), "source-a-v2\n"
        )
        self.assertEqual(target_b.read_text(encoding="utf-8"), "external-b-v3\n")

    def test_batch_defers_independent_future_resource_staleness(self) -> None:
        names = ("a", "b", "c")
        for name in names:
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v1\n", encoding="utf-8"
            )
            (self.home / f"{name}.txt").write_text(
                f"live-{name}-v1\n", encoding="utf-8"
            )
        self._configure_copy_batch(names)
        for name in names:
            self.assertEqual(
                adopt_resource(self._plan(), resource=name, confirm=True).outcome,
                "committed",
            )
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v2\n", encoding="utf-8"
            )

        original_write = m7_engine.write_file
        target_c = self.home / "c.txt"

        def write_then_stale_future(
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
            if path == self.home / "a.txt":
                target_c.write_text("external-c-v3\n", encoding="utf-8")
            return identity

        with patch("luwu.m7_engine.write_file", side_effect=write_then_stale_future):
            result = apply_plan(self._plan(), confirm=True)

        states = {
            (action.resource, action.kind): action.state for action in result.actions
        }
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.error_code, "m7_stale_plan")
        self.assertEqual(states[("a", "target_write")], "committed")
        self.assertEqual(states[("b", "target_write")], "committed")
        self.assertEqual(states[("c", "target_write")], "not_attempted")
        self.assertEqual((self.home / "a.txt").read_text(), "source-a-v2\n")
        self.assertEqual((self.home / "b.txt").read_text(), "source-b-v2\n")
        self.assertEqual(target_c.read_text(), "external-c-v3\n")

    def test_final_batch_recheck_detects_changed_noop_after_write(self) -> None:
        names = ("write", "noop")
        for name in names:
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v1\n", encoding="utf-8"
            )
            (self.home / f"{name}.txt").write_text(
                f"source-{name}-v1\n" if name == "noop" else f"live-{name}-v1\n",
                encoding="utf-8",
            )
        self._configure_copy_batch(names)
        for name in names:
            self.assertEqual(
                adopt_resource(self._plan(), resource=name, confirm=True).outcome,
                "committed",
            )
        (self.source / "write.txt").write_text("source-write-v2\n", encoding="utf-8")
        plan = self._plan()
        original_write = m7_engine.write_file
        noop_target = self.home / "noop.txt"

        def write_then_change_noop(
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
            noop_target.write_text("external-noop-v3\n", encoding="utf-8")
            return identity

        with patch("luwu.m7_engine.write_file", side_effect=write_then_change_noop):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed_but_verification_failed")
        self.assertEqual(result.error_code, "m7_stale_plan")
        self.assertEqual(
            [(action.resource, action.state) for action in result.actions],
            [("write", "committed")],
        )
        self.assertEqual((self.home / "write.txt").read_text(), "source-write-v2\n")
        self.assertEqual(noop_target.read_text(), "external-noop-v3\n")

    def test_future_plan_tamper_during_write_does_not_change_authorized_bytes(
        self,
    ) -> None:
        review_target = self._add_review_resource()
        for resource in ("settings", "review"):
            accepted = accept_baseline(
                self._plan(),
                resource=resource,
                fields=("theme", "brand"),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(accepted.outcome, "committed")
        self.assertEqual(apply_plan(self._plan(), confirm=True).outcome, "committed")

        local = self.inputs / "local" / "settings.json"
        input_data = json.loads(local.read_text(encoding="utf-8"))
        input_data["overrides"]["theme"] = "overlay-theme-v2"
        local.write_text(json.dumps(input_data) + "\n", encoding="utf-8")
        plan = self._plan()
        specs = m7_engine._forward_specs(plan._resources, plan.manifest)
        self.assertEqual(len(specs), 2)
        first = specs[0]
        future_resource_name = next(
            spec.resource.name
            for spec in specs
            if spec.resource.name != first.resource.name
        )
        future_item = next(
            item
            for item in plan._resources
            if item.resource.name == future_resource_name
        )
        future_target = (
            review_target if future_resource_name == "review" else self._target()
        )
        future_spec = next(
            spec for spec in specs if spec.resource.name == future_resource_name
        )
        expected_future_bytes = future_spec.data
        original_write = m7_engine.write_file
        changed_future_plan = False

        def write_then_tamper_future(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            nonlocal changed_future_plan
            identity = original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )
            if path == first.path:
                object.__setattr__(future_item, "write_data", b"M7_FORGED_FUTURE_WRITE")
                changed_future_plan = True
            return identity

        with patch("luwu.m7_engine.write_file", side_effect=write_then_tamper_future):
            result = apply_plan(plan, confirm=True)

        self.assertTrue(changed_future_plan)
        self.assertEqual(result.outcome, "committed")
        states = {
            (action.resource, action.kind): action.state for action in result.actions
        }
        self.assertEqual(states[(first.resource.name, "target_write")], "committed")
        self.assertEqual(states[(future_resource_name, "target_write")], "committed")
        self.assertTrue(first.path.is_file())
        self.assertEqual(future_target.read_bytes(), expected_future_bytes)
        self.assertNotEqual(future_target.read_bytes(), b"M7_FORGED_FUTURE_WRITE")

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

        original_write = m7_engine.write_file
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

        with patch("luwu.m7_engine.write_file", side_effect=count_write):
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

    def test_external_mode_change_to_created_parent_is_not_waived(self) -> None:
        self._accept_template_baseline()
        original_create = m7_engine.create_parent

        def create_then_change_mode(
            root: Path,
            path: Path,
            mode: int,
            *,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_create(root, path, mode, check_inputs=check_inputs)
            if path == self.home / ".config":
                path.chmod(0o600)
            return identity

        with patch("luwu.m7_engine.create_parent", side_effect=create_then_change_mode):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.error_code, "m7_parent_create_unknown")
        states = {(action.kind, action.path): action.state for action in result.actions}
        self.assertEqual(states[("mkdir", ".config")], "uncertain")
        self.assertEqual(states[("mkdir", ".config/app")], "not_attempted")
        self.assertEqual(
            states[("target_write", ".config/app/settings.json")], "not_attempted"
        )
        self.assertEqual(stat.S_IMODE((self.home / ".config").stat().st_mode), 0o600)

    def test_created_parent_identity_is_not_replaced_by_a_matching_foreign_dir(
        self,
    ) -> None:
        self._accept_template_baseline()
        original_create = m7_engine.create_parent
        first_parent = self.home / ".config"

        def create_then_replace(
            root: Path,
            path: Path,
            mode: int,
            *,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            identity = original_create(root, path, mode, check_inputs=check_inputs)
            if path == first_parent:
                moved = self.home / ".config-original"
                path.rename(moved)
                path.mkdir(mode=mode)
                path.chmod(mode)
            return identity

        with patch("luwu.m7_engine.create_parent", side_effect=create_then_replace):
            result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.error_code, "m7_parent_create_unknown")
        states = {(action.kind, action.path): action.state for action in result.actions}
        self.assertEqual(states[("mkdir", ".config")], "uncertain")
        self.assertEqual(states[("mkdir", ".config/app")], "not_attempted")
        self.assertEqual(
            states[("target_write", ".config/app/settings.json")], "not_attempted"
        )
        self.assertTrue((self.home / ".config-original").is_dir())

    def test_successful_write_does_not_adopt_a_foreign_mode_change(self) -> None:
        self._accept_template_baseline()
        plan = self._plan()
        original_write = m7_engine.write_file
        target = self._target()

        def write_then_change_mode(
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
            if path == target:
                path.chmod(0o644)
            return identity

        with patch("luwu.m7_engine.write_file", side_effect=write_then_change_mode):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.error_code, "m7_write_unknown")
        target_action = next(
            action for action in result.actions if action.kind == "target_write"
        )
        self.assertEqual(target_action.state, "committed")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_observation_failure_after_successful_write_preserves_commit(self) -> None:
        self._accept_template_baseline()
        plan = self._plan()
        original_write = m7_engine.write_file
        original_observe = m7_engine.observe_file
        target = self._target()
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
            if path == target:
                write_returned = True
            return identity

        def observe_after_write(root: Path, path: Path):
            if write_returned and path == target:
                raise m7_engine.M7FilesystemError(
                    "private injected failure", code="m7_observe_failed"
                )
            return original_observe(root, path)

        with (
            patch("luwu.m7_engine.write_file", side_effect=write_then_arm),
            patch("luwu.m7_engine.observe_file", side_effect=observe_after_write),
        ):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "committed_but_verification_failed")
        self.assertEqual(result.error_code, "m7_observe_failed")
        target_action = next(
            action for action in result.actions if action.kind == "target_write"
        )
        self.assertEqual(target_action.state, "committed")
        self.assertIn('"theme":"overlay-theme-v1"', target.read_text(encoding="utf-8"))

    def test_writer_errors_distinguish_not_replaced_and_committed(self) -> None:
        self._accept_template_baseline()
        target = self._target()
        target.parent.mkdir(parents=True, exist_ok=True)
        with patch(
            "luwu.m7_engine.write_file",
            side_effect=m7_engine.M7FilesystemError(
                "private injected failure",
                code="m7_write_failed",
                state="not_replaced",
                committed=False,
            ),
        ):
            not_replaced = apply_plan(self._plan(), confirm=True)
        not_replaced_action = next(
            action for action in not_replaced.actions if action.kind == "target_write"
        )
        self.assertEqual(not_replaced.outcome, "not_replaced")
        self.assertEqual(not_replaced_action.state, "not_replaced")
        self.assertFalse(target.exists())

        original_write = m7_engine.write_file

        def write_then_raise(
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
            if path == target:
                raise m7_engine.M7FilesystemError(
                    "private injected failure",
                    code="m7_write_unknown",
                    state="uncertain",
                    committed=True,
                )
            return identity

        with patch("luwu.m7_engine.write_file", side_effect=write_then_raise):
            committed = apply_plan(self._plan(), confirm=True)
        committed_action = next(
            action for action in committed.actions if action.kind == "target_write"
        )
        self.assertEqual(committed.outcome, "committed_state_unknown")
        self.assertEqual(committed_action.state, "committed")
        self.assertIn('"theme":"overlay-theme-v1"', target.read_text(encoding="utf-8"))

    def test_middle_writer_not_replaced_preserves_partial_batch_state(self) -> None:
        for name in ("first", "second"):
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v1\n", encoding="utf-8"
            )
            (self.home / f"{name}.txt").write_text(
                f"live-{name}-v1\n", encoding="utf-8"
            )
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
resources = ["first", "second"]
[inputs]
[resources.first]
kind = "copy"
source = "first.txt"
target = "first.txt"
baseline = "baselines/first.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
[resources.second]
kind = "copy"
source = "second.txt"
target = "second.txt"
baseline = "baselines/second.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "require"
parent_mode = "0700"
""",
            encoding="utf-8",
        )
        for name in ("first", "second"):
            self.assertEqual(
                adopt_resource(self._plan(), resource=name, confirm=True).outcome,
                "committed",
            )
            (self.source / f"{name}.txt").write_text(
                f"source-{name}-v2\n", encoding="utf-8"
            )

        original_write = m7_engine.write_file
        second_target = self.home / "second.txt"

        def fail_second_write(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: _CheckInputs = None,
        ) -> tuple[int, int]:
            if path == second_target:
                raise m7_engine.M7FilesystemError(
                    "private injected failure",
                    code="m7_write_failed",
                    state="not_replaced",
                    committed=False,
                )
            return original_write(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
            )

        with patch("luwu.m7_engine.write_file", side_effect=fail_second_write):
            result = apply_plan(self._plan(), confirm=True)

        states = {
            (action.resource, action.kind): action.state for action in result.actions
        }
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(states[("first", "target_write")], "committed")
        self.assertEqual(states[("second", "target_write")], "not_replaced")
        self.assertEqual((self.home / "first.txt").read_text(), "source-first-v2\n")
        self.assertEqual(second_target.read_text(), "live-second-v1\n")

    def test_accept_and_adopt_writer_callbacks_keep_authorized_baseline_bytes(
        self,
    ) -> None:
        plan = self._plan()
        original_write = m7_engine.write_baseline
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
            object.__setattr__(plan._resources[0], "write_data", b"M7_FORGED_BASELINE")
            return identity

        with patch(
            "luwu.m7_engine.write_baseline", side_effect=write_then_poison_caller_plan
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
        self.assertNotIn(b"M7_FORGED_BASELINE", accepted_path.read_bytes())

        (self.source / "note.txt").write_text("source-note-v1\n", encoding="utf-8")
        (self.home / "note.txt").write_text("live-note-v1\n", encoding="utf-8")
        self._configure_copy_batch(("note",))
        plan = self._plan()
        with patch(
            "luwu.m7_engine.write_baseline", side_effect=write_then_poison_caller_plan
        ):
            adopted = adopt_resource(plan, resource="note", confirm=True)

        adopted_path = self.inputs / "baselines" / "note.json"
        self.assertEqual(adopted.outcome, "committed")
        self.assertEqual(adopted_path.read_bytes(), written_data[1])
        self.assertNotIn(b"M7_FORGED_BASELINE", adopted_path.read_bytes())
        self.assertEqual((self.home / "note.txt").read_bytes(), b"live-note-v1\n")

    def test_reverse_writer_callback_keeps_authorized_input_bytes(self) -> None:
        self._deploy_template()
        target = self._target()
        live = json.loads(target.read_text(encoding="utf-8"))
        live["settings"]["theme"] = "live-theme-v2"
        target.write_text(json.dumps(live) + "\n", encoding="utf-8")

        plan = self._plan()
        input_path = self.inputs / "local" / "settings.json"
        original_write = m7_engine.write_file
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
                object.__setattr__(plan._resources[0], "write_data", b"M7_FORGED_INPUT")
            return identity

        with patch(
            "luwu.m7_engine.write_file", side_effect=write_then_poison_caller_plan
        ):
            result = reverse_inputs(
                plan,
                resource="settings",
                fields=("theme",),
                confirm=True,
            )

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(result.verification_status, "verified")
        self.assertEqual(input_path.read_bytes(), written_data[0])
        self.assertNotIn(b"M7_FORGED_INPUT", input_path.read_bytes())
        self.assertEqual(
            json.loads(input_path.read_text(encoding="utf-8"))["overrides"]["theme"],
            "live-theme-v2",
        )

    def test_accept_and_adopt_preserve_commit_when_postwrite_observation_fails(
        self,
    ) -> None:
        original_write = m7_engine.write_baseline
        original_observe = m7_engine.observe_file
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
                raise m7_engine.M7FilesystemError(
                    "private injected failure", code="m7_observe_failed"
                )
            return original_observe(root, path)

        with (
            patch(
                "luwu.m7_engine.write_baseline",
                side_effect=write_baseline_then_arm,
            ),
            patch("luwu.m7_engine.observe_file", side_effect=observe_accept_write),
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
                raise m7_engine.M7FilesystemError(
                    "private injected failure", code="m7_observe_failed"
                )
            return original_observe(root, path)

        with (
            patch(
                "luwu.m7_engine.write_baseline",
                side_effect=write_baseline_then_arm,
            ),
            patch("luwu.m7_engine.observe_file", side_effect=observe_adopt_write),
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
        original_write = m7_engine.write_file
        original_observe = m7_engine.observe_file
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
                raise m7_engine.M7FilesystemError(
                    "private injected failure", code="m7_observe_failed"
                )
            return original_observe(root, path)

        with (
            patch("luwu.m7_engine.write_file", side_effect=write_then_arm),
            patch("luwu.m7_engine.observe_file", side_effect=observe_after_write),
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
        with self.assertRaises(M7EngineError) as preview_error:
            reverse_inputs(
                plan,
                resource="first",
                fields=("theme",),
                confirm=False,
            )
        self.assertEqual(preview_error.exception.code, "m7_reverse_consumer_change")
        self.assertEqual(self._snapshot(), before)
        with self.assertRaises(M7EngineError) as raised:
            reverse_inputs(
                plan,
                resource="first",
                fields=("theme",),
                confirm=True,
            )
        self.assertEqual(raised.exception.code, "m7_reverse_consumer_change")
        self.assertEqual(self._snapshot(), before)

    def test_apply_blocks_conflicting_shared_parent_modes_before_writes(self) -> None:
        for name in ("first", "second"):
            (self.source / f"{name}.txt").write_text(
                f"{name}-source\n", encoding="utf-8"
            )
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
resources = ["first", "second"]
[inputs]
[resources.first]
kind = "copy"
source = "first.txt"
target = "nested/first.txt"
baseline = "baselines/first.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
[resources.second]
kind = "copy"
source = "second.txt"
target = "nested/second.txt"
baseline = "baselines/second.json"
scope = "whole-file"
owner = "source"
content_sensitivity = "public"
parents = "create"
parent_mode = "0750"
""",
            encoding="utf-8",
        )
        for resource in ("first", "second"):
            self.assertEqual(
                adopt_resource(self._plan(), resource=resource, confirm=True).outcome,
                "committed",
            )
        before = self._snapshot()
        plan = self._plan()
        payload = plan.to_dict()
        planned_modes = {
            resource["parent_actions"]["target"][0]["mode"]
            for resource in payload["resources"]
        }
        self.assertEqual(planned_modes, {"0o0700", "0o0750"})
        result = apply_plan(plan, confirm=True)
        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "m7_parent_mode_conflict")
        self.assertTrue(
            all(action.state == "not_attempted" for action in result.actions)
        )
        self.assertEqual(self._snapshot(), before)

    def test_apply_blocks_unsafe_missing_parent_mode_before_any_write(self) -> None:
        manifest = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(
            manifest.replace('parent_mode = "0700"', 'parent_mode = "0777"'),
            encoding="utf-8",
        )
        accepted = accept_baseline(
            self._plan(),
            resource="settings",
            fields=("theme", "brand"),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(accepted.outcome, "committed")

        before = self._snapshot()
        result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "m7_parent_mode_unsafe")
        self.assertTrue(
            all(action.state == "not_attempted" for action in result.actions)
        )
        self.assertFalse((self.home / ".config").exists())
        self.assertEqual(self._snapshot(), before)

    def test_public_file_mode_0666_remains_allowed(self) -> None:
        (self.home / ".config" / "app").mkdir(parents=True)
        manifest = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(
            manifest.replace('mode = "0600"', 'mode = "0666"').replace(
                'parent_mode = "0700"', 'parent_mode = "0777"'
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            accept_baseline(
                self._plan(),
                resource="settings",
                fields=("theme", "brand"),
                value_from="desired",
                confirm=True,
            ).outcome,
            "committed",
        )

        result = apply_plan(self._plan(), confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(stat.S_IMODE(self._target().stat().st_mode), 0o666)
