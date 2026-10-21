"""Tests for baseline envelope."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import traceback
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from luwu.baseline import (
    BaselineError,
    _parse_schema2_json,
    baseline_field_names,
    load_baseline,
    make_copy_baseline,
    make_field_baseline,
)
from luwu.engine import EngineError, accept_baseline, build_plan
from luwu.formats import FormatError, parse_document
from luwu.manifest import load_manifest


class BaselineEnvelopeV8Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="luwu-test-baseline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.home = self.root / "home"
        for path in (
            self.source / "templates",
            self.inputs / "global",
            self.inputs / "baselines",
            self.home,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "luwu.toml"
        environment = patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    @property
    def baseline_path(self) -> Path:
        return self.inputs / "baselines" / "settings.json"

    def _write_template_case(self, *, alias: str, binding: str):
        template = (
            '{"settings":{"brand":{{ inputs['
            + json.dumps(binding)
            + "] | tojson }}"
            + "}}\n"
        )
        (self.source / "templates" / "settings.json.j2").write_text(
            template, encoding="utf-8"
        )
        (self.inputs / "global" / "settings.json").write_text(
            '{"brand":"brand-value"}\n', encoding="utf-8"
        )
        self.manifest_path.write_text(
            f"""
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

[inputs.{binding}]
layers = [
  {{ file = "global/settings.json", path = ["brand"], format = "json" }},
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

[resources.settings.fields.{alias}]
path = ["settings", "brand"]
owner = "source"
input = "{binding}"
""",
            encoding="utf-8",
        )
        return load_manifest(self.manifest_path, profile="work")

    def test_sensitive_metadata_aliases_and_bindings_round_trip_and_replan(
        self,
    ) -> None:
        for alias, binding in (
            ("client_secret", "brand"),
            ("brand", "client_secret"),
            ("client_secret", "client_secret"),
        ):
            with self.subTest(alias=alias, binding=binding):
                manifest = self._write_template_case(alias=alias, binding=binding)
                resource = manifest.resources[0]

                accepted = accept_baseline(
                    build_plan(manifest),
                    resource="settings",
                    fields=(alias,),
                    value_from="desired",
                    confirm=True,
                )

                self.assertEqual(accepted.outcome, "committed")
                self.assertTrue(self.baseline_path.is_file())
                _, baseline = load_baseline(manifest, resource)
                self.assertIsNotNone(baseline)
                assert baseline is not None
                self.assertEqual(dict(baseline.values), {alias: "brand-value"})
                fresh_plan = build_plan(manifest).to_dict()
                resources = fresh_plan["resources"]
                assert isinstance(resources, list)
                fresh_resource = resources[0]
                assert isinstance(fresh_resource, dict)
                self.assertIsNone(fresh_resource["error_code"])
                self.assertNotEqual(fresh_resource["status"], "blocked")

                self.baseline_path.unlink()

    def test_schema2_decoder_keeps_integer_decimal_and_boolean_types(self) -> None:
        value = _parse_schema2_json(
            b'{"schema_version":2,"integer":7,"fraction":1.25,"flag":true}'
        )
        assert isinstance(value, dict)
        self.assertIs(type(value["schema_version"]), int)
        self.assertIs(type(value["integer"]), int)
        self.assertEqual(value["fraction"], Decimal("1.25"))
        self.assertIs(type(value["flag"]), bool)

    def test_malicious_baseline_envelopes_are_rejected_without_content_leaks(
        self,
    ) -> None:
        manifest = self._write_template_case(alias="brand", binding="brand")
        resource = manifest.resources[0]
        valid = make_field_baseline(
            manifest,
            resource,
            current={"brand": "accepted"},
            selected_fields=("brand",),
            existing=None,
        )
        valid_model = json.loads(valid)
        marker = "baseline-private-marker"

        with_nested_duplicate = valid.replace(
            b'"identity":{"resource":"settings",',
            b'"identity":{"resource":"settings","resource":"settings",',
            1,
        )
        malformed = [
            (
                "duplicate top-level key",
                valid.replace(
                    b'"schema_version":2,',
                    b'"schema_version":2,"schema_version":2,',
                    1,
                ),
                "baseline_invalid",
            ),
            ("duplicate nested key", with_nested_duplicate, "baseline_invalid"),
            (
                "boolean schema version",
                valid.replace(b'"schema_version":2,', b'"schema_version":true,', 1),
                "baseline_invalid",
            ),
            (
                "fractional schema version",
                valid.replace(b'"schema_version":2,', b'"schema_version":2.0,', 1),
                "baseline_invalid",
            ),
            (
                "nonfinite value",
                valid.replace(
                    b'"values":{"brand":"accepted"}',
                    b'"values":{"brand":NaN}',
                    1,
                ),
                "baseline_invalid",
            ),
            (
                "lone surrogate",
                valid.replace(
                    b'"values":{"brand":"accepted"}',
                    b'"values":{"brand":"\\ud800"}',
                    1,
                ),
                "baseline_invalid",
            ),
            (
                "invalid UTF-8",
                valid.rstrip(b"\n") + b"\xff\n",
                "baseline_invalid",
            ),
            (
                "comment",
                valid.rstrip(b"\n") + b" /* " + marker.encode() + b" */\n",
                "baseline_invalid",
            ),
            (
                "trailing comma",
                valid.rstrip(b"\n")[:-1] + b",}\n",
                "baseline_invalid",
            ),
            (
                "undeclared value alias",
                self._json_bytes(
                    {
                        **valid_model,
                        "values": {"brand": "accepted", "undeclared": marker},
                    }
                ),
                "baseline_invalid",
            ),
            (
                "container value",
                self._json_bytes(
                    {
                        **valid_model,
                        "values": {"brand": {"nested": {"client_secret": marker}}},
                    }
                ),
                "baseline_invalid",
            ),
            (
                "unknown envelope member",
                self._json_bytes({**valid_model, "extra": marker}),
                "baseline_invalid",
            ),
            (
                "identity mismatch",
                self._json_bytes(
                    {
                        **valid_model,
                        "identity": {**valid_model["identity"], "extra": marker},
                    }
                ),
                "baseline_stale",
            ),
            (
                "malformed JSON",
                b'{"private_marker":"' + marker.encode() + b'"',
                "baseline_invalid",
            ),
        ]

        self.baseline_path.parent.mkdir(exist_ok=True)
        for name, data, code in malformed:
            with self.subTest(envelope=name):
                self.baseline_path.write_bytes(data)
                with self.assertRaises(BaselineError) as raised:
                    load_baseline(manifest, resource)
                error = raised.exception
                self.assertEqual(error.code, code)
                self.assertIsNone(error.__context__)
                self.assertIsNone(error.__cause__)
                rendered = "".join(traceback.format_exception(error))
                self.assertNotIn(marker, str(error))
                self.assertNotIn(marker, repr(error))
                self.assertNotIn(marker, rendered)

    def test_generated_envelope_is_validated_before_any_write(self) -> None:
        manifest = self._write_template_case(alias="brand", binding="brand")
        baseline_directory = self.baseline_path.parent
        self.assertEqual(tuple(baseline_directory.iterdir()), ())

        with (
            patch("luwu.baseline.encode_public_json", return_value=b"{}"),
            patch("luwu.engine.write_baseline") as write_baseline,
            self.assertRaises(EngineError) as raised,
        ):
            accept_baseline(
                build_plan(manifest),
                resource="settings",
                fields=("brand",),
                value_from="desired",
                confirm=True,
            )

        self.assertEqual(raised.exception.code, "field_invalid")
        write_baseline.assert_not_called()
        self.assertEqual(tuple(baseline_directory.iterdir()), ())
        self.assertFalse(self.baseline_path.exists())

    def test_generated_surrogate_error_is_detached_and_does_not_write(self) -> None:
        alias = "declared_alias"
        manifest = self._write_template_case(alias=alias, binding="brand")
        resource = manifest.resources[0]
        marker = "SYNTHETIC_MARKER"
        baseline_directory = self.baseline_path.parent
        self.assertEqual(tuple(baseline_directory.iterdir()), ())

        with (
            patch("luwu.baseline.write_baseline") as write_baseline,
            self.assertRaises(BaselineError) as raised,
        ):
            make_field_baseline(
                manifest,
                resource,
                current={alias: marker + "\ud800"},
                selected_fields=(alias,),
                existing=None,
            )

        error = raised.exception
        self.assertEqual(error.code, "baseline_invalid")
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        rendered = "".join(traceback.format_exception(error))
        self.assertNotIn(marker, str(error))
        self.assertNotIn(marker, repr(error))
        self.assertNotIn(marker, rendered)
        write_baseline.assert_not_called()
        self.assertEqual(tuple(baseline_directory.iterdir()), ())
        self.assertFalse(self.baseline_path.exists())

    def test_copy_baseline_keeps_algorithm_digest_and_mode(self) -> None:
        (self.source / "note.txt").write_text("source note\n", encoding="utf-8")
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
deploy = "copy"
source = "note.txt"
target = "note.txt"
baseline = "baselines/note.json"
content_sensitivity = "public"
parents = "create"
parent_mode = "0700"
""",
            encoding="utf-8",
        )
        manifest = load_manifest(self.manifest_path, profile="work")
        resource = manifest.resources[0]
        accepted_data = b"accepted note bytes\n"
        accepted_mode = 0o640

        data = make_copy_baseline(
            manifest,
            resource,
            accepted_data=accepted_data,
            accepted_mode=accepted_mode,
        )
        path = self.inputs / resource.baseline_path()
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        _, baseline = load_baseline(manifest, resource)

        self.assertIsNotNone(baseline)
        assert baseline is not None
        self.assertEqual(
            baseline.whole_file_digest, hashlib.sha256(accepted_data).hexdigest()
        )
        self.assertEqual(baseline.whole_file_mode, accepted_mode)

    def test_actual_configuration_sensitive_key_still_uses_format_rejection(
        self,
    ) -> None:
        with self.assertRaises(FormatError):
            parse_document(
                b'{"settings":{"nested":{"client_secret":"sentinel"}}}',
                format="json",
            )

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


class ProviderFieldBaselineTests(unittest.TestCase):
    """Provider-owned fields never persist values but stay in the identity."""

    SENTINEL = "PROVIDER-SENTINEL-7f3a9c"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="luwu-test-provider-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.home = self.root / "home"
        for path in (
            self.source / "templates",
            self.inputs / "global",
            self.inputs / "baselines",
            self.home,
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.source / "templates" / "settings.json.j2").write_text(
            "{}\n", encoding="utf-8"
        )
        self.manifest_path = self.root / "luwu.toml"

    @property
    def baseline_path(self) -> Path:
        return self.inputs / "baselines" / "settings.json"

    def _manifest_text(self, key_binding: str, *, providers: str = "") -> str:
        return (
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

[inputs.brand]
layers = [
  { file = "global/settings.json", path = ["brand"], format = "json" },
]

"""
            + providers
            + """
[resources.settings]
source = "templates/settings.json.j2"
target = ".config/app/settings.json"
format = "json"
baseline = "baselines/settings.json"
parents = "create"
parent_mode = "0700"
mode = 0o600

[resources.settings.fields.brand]
path = ["settings", "brand"]
owner = "source"
input = "brand"

[resources.settings.fields.key]
path = ["settings", "key"]
"""
            + key_binding
        )

    def _provider_manifest(self):
        self.manifest_path.write_text(
            self._manifest_text(
                'owner = "provider"\nprovider = "openai"\n',
                providers='[providers.openai]\ntype = "rbw"\nitem = "openai"\n',
            ),
            encoding="utf-8",
        )
        return load_manifest(self.manifest_path, profile="work")

    def test_field_baseline_excludes_provider_values(self) -> None:
        manifest = self._provider_manifest()
        resource = manifest.resources[0]
        self.assertEqual(resource.provider_fields, ("key",))
        self.assertEqual(baseline_field_names(resource), ("brand",))

        data = make_field_baseline(
            manifest,
            resource,
            current={"brand": "brand-value", "key": self.SENTINEL},
            selected_fields=("brand",),
            existing=None,
        )
        self.assertNotIn(self.SENTINEL.encode(), data)
        envelope = json.loads(data)
        self.assertEqual(envelope["values"], {"brand": "brand-value"})
        mapping = envelope["identity"]["mapping"]
        self.assertEqual(
            mapping["key"],
            {
                "path": ["settings", "key"],
                "owner": "provider",
                "input": None,
            },
        )
        self.assertNotIn("provider", mapping["brand"])
        self.assertNotIn("provider", mapping["key"])

        self.baseline_path.write_bytes(data)
        _, baseline = load_baseline(manifest, resource)
        self.assertIsNotNone(baseline)
        assert baseline is not None
        self.assertEqual(dict(baseline.values), {"brand": "brand-value"})

    def test_provider_field_is_not_selectable_for_a_baseline(self) -> None:
        manifest = self._provider_manifest()
        resource = manifest.resources[0]
        with self.assertRaises(BaselineError) as raised:
            make_field_baseline(
                manifest,
                resource,
                current={"key": self.SENTINEL},
                selected_fields=("key",),
                existing=None,
            )
        self.assertEqual(raised.exception.code, "field_invalid")
        self.assertNotIn(self.SENTINEL, str(raised.exception))

    def test_stored_provider_value_fails_closed(self) -> None:
        manifest = self._provider_manifest()
        resource = manifest.resources[0]
        valid = make_field_baseline(
            manifest,
            resource,
            current={"brand": "brand-value"},
            selected_fields=("brand",),
            existing=None,
        )
        envelope = json.loads(valid)
        envelope["values"]["key"] = self.SENTINEL
        self.baseline_path.write_bytes(
            (json.dumps(envelope, separators=(",", ":")) + "\n").encode("utf-8")
        )
        with self.assertRaises(BaselineError) as raised:
            load_baseline(manifest, resource)
        error = raised.exception
        self.assertEqual(error.code, "baseline_invalid")
        rendered = "".join(traceback.format_exception(error))
        self.assertNotIn(self.SENTINEL, str(error))
        self.assertNotIn(self.SENTINEL, repr(error))
        self.assertNotIn(self.SENTINEL, rendered)

    def test_identity_binds_owner_but_not_the_provider_binding_name(self) -> None:
        self.manifest_path.write_text(
            self._manifest_text('owner = "source"\ninput = "brand"\n'),
            encoding="utf-8",
        )
        source_manifest = load_manifest(self.manifest_path, profile="work")
        data = make_field_baseline(
            source_manifest,
            source_manifest.resources[0],
            current={"brand": "brand-value", "key": "plain-value"},
            selected_fields=("brand", "key"),
            existing=None,
        )
        self.baseline_path.write_bytes(data)

        provider_manifest = self._provider_manifest()
        with self.assertRaises(BaselineError) as raised:
            load_baseline(provider_manifest, provider_manifest.resources[0])
        self.assertEqual(raised.exception.code, "baseline_stale")

        provider_data = make_field_baseline(
            provider_manifest,
            provider_manifest.resources[0],
            current={"brand": "brand-value"},
            selected_fields=("brand",),
            existing=None,
        )
        self.baseline_path.write_bytes(provider_data)

        # Renaming the provider declaration keeps the same ownership identity,
        # so the baseline stays valid.
        self.manifest_path.write_text(
            self._manifest_text(
                'owner = "provider"\nprovider = "other"\n',
                providers='[providers.other]\ntype = "rbw"\nitem = "openai"\n',
            ),
            encoding="utf-8",
        )
        renamed = load_manifest(self.manifest_path, profile="work")
        _, baseline = load_baseline(renamed, renamed.resources[0])
        self.assertIsNotNone(baseline)
        assert baseline is not None
        self.assertEqual(dict(baseline.values), {"brand": "brand-value"})
