"""Tests for sensitive keys."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from luwu.errors import MutationError
from luwu.m6_formats import parse_m6_document
from luwu.m6_pilot import accept_m6_baseline, apply_m6_forward, build_m6_plan
from luwu.manifest import load_manifest


class SensitiveKeysV7Tests(unittest.TestCase):
    def test_sensitive_top_level_key_blocks_complete_document(self) -> None:
        with self.assertRaisesRegex(MutationError, "sensitive"):
            parse_m6_document(
                b'{"provider": "public", "client_secret": "opaque"}',
                format="json",
            )

    def test_camel_case_and_hyphen_sensitive_keys_are_rejected_in_json_and_toml(
        self,
    ) -> None:
        sensitive_keys = (
            "privateKey",
            "privatekey",
            "PRIVATEKEY",
            "apiKey",
            "private-key",
            "api-key",
            "clientSecret",
            "client-credential",
            "access-token",
        )

        for key in sensitive_keys:
            sources = (
                ("json", f'{{"model":"public","{key}":"opaque"}}'.encode()),
                ("toml", f'model = "public"\n{key} = "opaque"\n'.encode()),
            )
            for format, source in sources:
                with self.subTest(format=format, key=key):
                    with self.assertRaises(MutationError) as raised:
                        parse_m6_document(source, format=format)

                    self.assertEqual(raised.exception.code, "m6_sensitive_input")
                    self.assertNotIn(key, str(raised.exception))
                    self.assertNotIn("opaque", str(raised.exception))

    def test_complete_desired_live_and_input_documents_check_all_top_level_keys(
        self,
    ) -> None:
        safe_template = (
            '{"model":"{{ global_inputs.model }}",'
            '"provider":"{{ local_inputs.provider }}"}\n'
        )
        safe_live = '{"model":"pi-4","provider":"local-provider"}\n'
        safe_global_input = '{"model":"pi-4"}\n'
        cases = (
            (
                "desired",
                safe_template[:-2] + ',"privateKey":"opaque"}\n',
                safe_live,
                safe_global_input,
            ),
            (
                "live",
                safe_template,
                ('{"model":"pi-4","provider":"local-provider","apiKey":"opaque"}\n'),
                safe_global_input,
            ),
            (
                "input",
                safe_template,
                safe_live,
                '{"model":"pi-4","private-key":"opaque"}\n',
            ),
        )

        for side, template, live, global_input in cases:
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("repo", "inputs/global", "inputs/local", "home"):
                    (root / name).mkdir(parents=True)
                (root / "repo/pi.json.j2").write_text(template, encoding="utf-8")
                (root / "inputs/global/pi.json").write_text(
                    global_input, encoding="utf-8"
                )
                (root / "inputs/local/pi.json").write_text(
                    '{"provider":"local-provider"}\n', encoding="utf-8"
                )
                (root / "home/pi.json").write_text(live, encoding="utf-8")
                manifest_path = root / "luwu.toml"
                manifest_path.write_text(self._manifest(), encoding="utf-8")

                plan = build_m6_plan(load_manifest(manifest_path))

                self.assertEqual(plan.resource.status, "blocked")
                self.assertNotIn("opaque", plan.resource.reason)

    def test_apply_blocks_unselected_privatekey_shared_by_desired_and_live(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "repo",
                "inputs/global",
                "inputs/local",
                "inputs/baselines",
                "home",
            ):
                (root / name).mkdir(parents=True)
            (root / "repo/pi.json.j2").write_text(
                '{"model":"{{ global_inputs.model }}",'
                '"provider":"{{ local_inputs.provider }}"}\n',
                encoding="utf-8",
            )
            (root / "inputs/global/pi.json").write_text(
                '{"model":"pi-4"}\n', encoding="utf-8"
            )
            (root / "inputs/local/pi.json").write_text(
                '{"provider":"provider-1"}\n', encoding="utf-8"
            )
            (root / "home/pi.json").write_text(
                '{"model":"pi-4","provider":"provider-1"}\n',
                encoding="utf-8",
            )
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(
                self._manifest()
                .replace(
                    'comparison = "json"\n',
                    'comparison = "json"\nbaseline = "baselines/pi.json"\n',
                )
                .replace(
                    'provider = "live"\n',
                    'provider = "live"\nprivatekey = "ignore"\n',
                ),
                encoding="utf-8",
            )
            manifest = load_manifest(manifest_path)
            accepted = accept_m6_baseline(
                manifest,
                resource_name="pi",
                fields=("model", "provider"),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(accepted.outcome, "committed")

            sentinel = "fake-sentinel"
            (root / "inputs/global/pi.json").write_text(
                '{"model":"pi-5"}\n', encoding="utf-8"
            )
            (root / "repo/pi.json.j2").write_text(
                '{"model":"{{ global_inputs.model }}",'
                '"provider":"{{ local_inputs.provider }}",'
                f'"privatekey":"{sentinel}"}}\n',
                encoding="utf-8",
            )
            live = (
                '{"model":"pi-4","provider":"provider-1",'
                f'"privatekey":"{sentinel}"}}\n'
            ).encode()
            (root / "home/pi.json").write_bytes(live)

            plan = build_m6_plan(load_manifest(manifest_path))

            self.assertEqual(plan.resource.status, "blocked")
            with self.assertRaises(MutationError) as raised:
                apply_m6_forward(plan, confirm=True)
            self.assertNotIn(sentinel, str(raised.exception))
            self.assertEqual((root / "home/pi.json").read_bytes(), live)

    def _manifest(self) -> str:
        return """version = 7

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
inputs = { model = "global/pi.json", provider = "local/pi.json" }

[resources.pi.fields]
model = "source"
provider = "live"
"""
