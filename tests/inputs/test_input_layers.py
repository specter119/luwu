"""Tests for input layers."""

from __future__ import annotations

import tempfile
import traceback
import unittest
from pathlib import Path

from luwu.inputs import InputError, absolute_input_path, load_inputs
from luwu.manifest import Manifest, Resource, load_manifest


class InputLayersV8Tests(unittest.TestCase):
    def _load_case(
        self,
        *,
        input_format: str,
        global_data: bytes,
        local_data: bytes,
    ) -> tuple[Manifest, Resource]:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        source_root = root / "source"
        inputs_root = root / "inputs"
        target_root = root / "target"
        for directory in (
            source_root,
            inputs_root / "global",
            inputs_root / "local",
            inputs_root / "baselines",
            target_root / ".config",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        (source_root / "settings.json.j2").write_text("{}\n", encoding="utf-8")
        extension = "json" if input_format == "json" else "toml"
        (inputs_root / "global" / f"model.{extension}").write_bytes(global_data)
        (inputs_root / "local" / f"model.{extension}").write_bytes(local_data)
        manifest_path = root / "luwu.toml"
        manifest_path.write_text(
            f'''version = 8

[roots]
source = "source"
inputs = "inputs"
target = "target"

[profiles.dev]
packages = ["settings"]
exclude = []

[packages.settings]
dependencies = []
resources = ["settings"]

[inputs.model]
layers = [
  {{ file = "global/model.{extension}", path = ["value"], format = "{input_format}" }},
  {{ file = "local/model.{extension}", path = ["value"], format = "{input_format}", optional = true }},
]
reverse_to = {{ file = "local/model.{extension}", path = ["value"], format = "{input_format}" }}

[resources.settings]
kind = "template"
source = "settings.json.j2"
target = ".config/settings.json"
format = "json"
baseline = "baselines/settings.json"
content_sensitivity = "public"
scope = "fields"
parents = "require"
parent_mode = "0700"

[resources.settings.fields.value]
path = ["value"]
owner = "merge"
input = "model"
''',
            encoding="utf-8",
        )

        manifest = load_manifest(manifest_path, profile="dev")
        return manifest, manifest.resources[0]

    def _assert_error_chain_is_safe(self, error: BaseException, marker: str) -> None:
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertNotIn(marker, str(error))
        self.assertNotIn(marker, repr(error))
        self.assertNotIn(marker, "".join(traceback.format_exception(error)))

        pending: list[BaseException] = [error]
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            self.assertNotIn(marker, str(current))
            self.assertNotIn(marker, repr(current))
            self.assertNotIn(marker, "".join(traceback.format_exception(current)))
            if current.__context__ is not None:
                pending.append(current.__context__)
            if current.__cause__ is not None:
                pending.append(current.__cause__)

    def test_parse_errors_do_not_expose_input_content_or_parser_context(self) -> None:
        valid_local_values = {
            "json": b'{"value":"local"}\n',
            "toml": b'value = "local"\n',
        }
        cases = (
            (
                "UTF8_INPUT_MARKER",
                "json",
                b'{"value":"UTF8_INPUT_MARKER\xff"}',
            ),
            (
                "JSON_INPUT_MARKER",
                "json",
                b'{"value":"JSON_INPUT_MARKER", invalid}',
            ),
            (
                "TOML_INPUT_MARKER",
                "toml",
                b'value = "TOML_INPUT_MARKER',
            ),
        )
        for marker, input_format, data in cases:
            with self.subTest(input_format=input_format, marker=marker):
                manifest, resource = self._load_case(
                    input_format=input_format,
                    global_data=data,
                    local_data=valid_local_values[input_format],
                )

                with self.assertRaises(InputError) as raised:
                    load_inputs(manifest, resource)

                self.assertEqual(raised.exception.code, "input_invalid")
                self._assert_error_chain_is_safe(raised.exception, marker)

    def test_valid_json_and_toml_overlays_keep_order_and_winner(self) -> None:
        cases = (
            ("json", b'{"value":"global"}\n', b'{"value":"local"}\n'),
            ("toml", b'value = "global"\n', b'value = "local"\n'),
        )
        for input_format, global_data, local_data in cases:
            with self.subTest(input_format=input_format):
                manifest, resource = self._load_case(
                    input_format=input_format,
                    global_data=global_data,
                    local_data=local_data,
                )

                context = load_inputs(manifest, resource)
                resolved = context.values["model"]

                self.assertEqual(resolved.value, "local")
                self.assertEqual(
                    tuple(layer.value for layer in resolved.layers),
                    ("global", "local"),
                )
                self.assertEqual(
                    tuple(layer.category for layer in resolved.layers),
                    ("global", "local"),
                )
                self.assertEqual(
                    context.template_variables()["inputs"]["model"], "local"
                )
                reverse_to = manifest.inputs["model"].reverse_to
                if reverse_to is None:
                    self.fail("expected the fixture to declare a reverse destination")
                self.assertEqual(reverse_to.path, ("value",))
                self.assertEqual(reverse_to.file.parent.name, "local")

    def test_unsafe_path_error_does_not_retain_path_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "inputs"
            outside = Path(temporary_directory) / "SYNTHETIC_PATH_MARKER.json"
            with self.assertRaises(InputError) as raised:
                absolute_input_path(root, outside)

        self.assertEqual(raised.exception.code, "unsafe_path")
        self._assert_error_chain_is_safe(raised.exception, "SYNTHETIC_PATH_MARKER")

    def test_non_finite_toml_numbers_never_reach_resolved_inputs(self) -> None:
        for literal in (b"value = inf\n", b"value = nan\n"):
            with self.subTest(literal=literal):
                manifest, resource = self._load_case(
                    input_format="toml",
                    global_data=literal,
                    local_data=b'value = "local"\n',
                )

                with self.assertRaises(InputError) as raised:
                    load_inputs(manifest, resource)

                self.assertEqual(raised.exception.code, "input_invalid")
