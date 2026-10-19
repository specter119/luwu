"""Tests for profile plan."""

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.baseline import BaselineError, load_baseline
from luwu.engine import (
    EngineError,
    Plan,
    accept_baseline,
    apply_plan,
    build_plan,
)
from luwu.errors import LuwuError
from luwu.filesystem import FileObservation
from luwu.formats import FormatError, parse_document
from luwu.inputs import InputError, load_inputs
from luwu.manifest import load_manifest
from tests.support import profile_case

_CheckInputs = Callable[[], None] | None


class ProfilePlanTests(profile_case.ProfileEngineCase):
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
        marker = "RAW_UTF8_MARKER"

        (self.inputs / "global" / "settings.json").write_bytes(
            b'{"values":{"theme":"' + marker.encode() + b'\xff"}}'
        )
        with self.assertRaises(InputError) as input_error:
            load_inputs(manifest, resource)
        self._assert_detached_error(input_error.exception, marker)

        (self.inputs / "baselines" / "settings.json").write_bytes(
            b'{"schema_version":2,"private_marker":"' + marker.encode() + b'\xff"}'
        )
        with self.assertRaises(BaselineError) as baseline_error:
            load_baseline(manifest, resource)
        self._assert_detached_error(baseline_error.exception, marker)

    def test_unknown_error_codes_are_not_serialized(self) -> None:
        marker = "SYNTHETIC_MARKER"
        with patch(
            "luwu.engine.load_inputs",
            side_effect=LuwuError("hidden", code=marker),
        ):
            plan = self._plan()

        resource = plan.to_dict()["resources"][0]
        assert isinstance(resource, dict)
        self.assertEqual(resource["error_code"], "resource_blocked")
        public_output = json.dumps(plan.to_dict(), sort_keys=True) + repr(plan)
        self.assertNotIn(marker, public_output)
        self.assertNotIn("hidden", public_output)
        self.assertEqual(engine_module._safe_code(123), "resource_blocked")
        self.assertEqual(engine_module._safe_code(marker), "resource_blocked")
        self.assertEqual(engine_module._safe_code("input_invalid"), "input_invalid")

    def test_plan_evidence_rechecks_complete_projection(self) -> None:
        plan = self._plan()
        evidence = engine_module._PLAN_REGISTRY[plan].evidence
        self.assertTrue(
            engine_module._matches_plan_evidence(
                engine_module._plan_projection(plan), evidence
            )
        )

        plan._resources[0].desired_document._root.start = -1
        self.assertFalse(
            engine_module._matches_plan_evidence(
                engine_module._plan_projection(plan), evidence
            )
        )

        self.assertFalse(
            engine_module._matches_plan_evidence(
                [True], engine_module._capture_plan_evidence([1])
            )
        )
        self.assertFalse(
            engine_module._matches_plan_evidence(
                [Decimal("1.00")],
                engine_module._capture_plan_evidence([Decimal("1.0")]),
            )
        )

        shared: list[object] = ["same", {"leaf": 1}]
        shared_graph = [shared, shared]
        duplicated_graph = [["same", {"leaf": 1}], ["same", {"leaf": 1}]]
        self.assertTrue(
            engine_module._matches_plan_evidence(
                duplicated_graph, engine_module._capture_plan_evidence(shared_graph)
            )
        )

    def test_plan_evidence_is_bounded_for_deep_parser_graphs(self) -> None:
        for depth in (8, 20, 32, 64, 128):
            with self.subTest(depth=depth):
                data = b'{"k":' * depth + b"1" + b"}" * depth
                document = parse_document(data, format="json")
                projection = engine_module._document_value(document)
                evidence = engine_module._capture_plan_evidence(projection)
                self.assertLess(len(evidence.records), depth * 20)
                self.assertTrue(
                    engine_module._matches_plan_evidence(projection, evidence)
                )

                reparsed = parse_document(data, format="json")
                self.assertTrue(
                    engine_module._matches_plan_evidence(
                        engine_module._document_value(reparsed), evidence
                    )
                )
                document._root.start = -1
                self.assertFalse(
                    engine_module._matches_plan_evidence(
                        engine_module._document_value(document), evidence
                    )
                )

        deep_dag: object = {"leaf": 1}
        for _ in range(64):
            deep_dag = [deep_dag, deep_dag]
        evidence = engine_module._capture_plan_evidence(deep_dag)
        self.assertLess(len(evidence.records), 500)
        self.assertTrue(engine_module._matches_plan_evidence(deep_dag, evidence))

    def test_cyclic_plan_graph_is_rejected_before_writing(self) -> None:
        plan = self._plan()
        before = self._snapshot()
        document = plan._resources[0].desired_document
        assert document is not None
        desired_value = document.value
        desired_value["cycle"] = desired_value

        with self.assertRaises(EngineError) as raised:
            apply_plan(plan, confirm=True)

        self.assertEqual(raised.exception.code, "plan_invalid")
        self.assertEqual(self._snapshot(), before)

    def test_plan_reauthorization_covers_jsonc_toml_and_shared_inputs(self) -> None:
        (self.inputs / "local" / "shared.json").write_text(
            '{"brand":"shared-v1"}\n', encoding="utf-8"
        )
        template_cases = {
            "jsonc": (
                "jsonc.jsonc.j2",
                '{"settings":{/* keep comment */"brand":{{ inputs.brand | tojson }}}}\n',
                ".config/demo-jsonc.jsonc",
                "jsonc",
            ),
            "toml_table": (
                "toml-table.toml.j2",
                "[settings]\nbrand = {{ inputs.brand | tojson }}\n",
                ".config/demo-table.toml",
                "toml",
            ),
            "toml_inline": (
                "toml-inline.toml.j2",
                "settings = { brand = {{ inputs.brand | tojson }} }\n",
                ".config/demo-inline.toml",
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
        authorized = engine_module._require_plan(plan, operation="apply")
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
        with self.assertRaises(EngineError) as stale:
            engine_module._require_plan(plan, operation="apply")
        self.assertEqual(stale.exception.code, "stale_plan")

        fresh_plan = build_plan(load_manifest(self.manifest_path, profile="work"))
        fresh_authorized = engine_module._require_plan(fresh_plan, operation="apply")
        self.assertEqual(len(fresh_authorized.resources), 3)

    def test_json_parser_depth_boundary_is_a_fixed_format_error(self) -> None:
        depth = 1500
        data = b'{"nested":' * depth + b"0" + b"}" * depth
        with self.assertRaises(FormatError) as raised:
            parse_document(data, format="json")
        self.assertEqual(raised.exception.code, "format")
        self.assertEqual(
            str(raised.exception), "document format or syntax is unsupported"
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
        original_observe = engine_module.observe_file

        def fail_target_observation(root: Path, path: Path) -> FileObservation:
            if path == target:
                raise engine_module.FilesystemError(
                    "private injected failure", code="unsafe_path"
                )
            return original_observe(root, path)

        with patch("luwu.engine.observe_file", side_effect=fail_target_observation):
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
                self.assertRaises(EngineError) as raised,
            ):
                apply_plan(candidate, confirm=True)
            self.assertEqual(raised.exception.code, "plan_invalid")
        self.assertEqual(self._snapshot(), before)
        plan._resources[0].desired_document.value["settings"]["theme"] = (
            "mutated-private-plan-value"
        )
        with self.assertRaises(EngineError) as raised:
            apply_plan(plan, confirm=True)
        self.assertEqual(raised.exception.code, "plan_invalid")
        self.assertEqual(self._snapshot(), before)

        plan = self._plan()
        object.__setattr__(plan._resources[0], "write_data", b'{"x":999}')
        with self.assertRaises(EngineError) as raised:
            apply_plan(plan, confirm=True)
        self.assertEqual(raised.exception.code, "plan_invalid")
        self.assertEqual(self._snapshot(), before)
