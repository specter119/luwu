"""Focused M6 public input and bounded format tests."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from luwu.errors import MutationError
from luwu.m6_formats import load_m6_inputs, parse_m6_document, patch_m6_document
from luwu.m6_pilot import accept_m6_baseline, apply_m6_forward, build_m6_plan
from luwu.manifest import load_manifest
from luwu.rendering import render_template


class M6InputsAndFormatsTests(unittest.TestCase):
    def test_input_provenance_and_renderer_context_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("repo", "inputs/global", "inputs/local", "home"):
                (root / name).mkdir(parents=True)
            (root / "repo/pi.json.j2").write_text(
                '{"model":"{{ global_inputs.model }}","provider":"{{ local_inputs.provider }}"}\n',
                encoding="utf-8",
            )
            (root / "inputs/global/pi.json").write_text(
                '{"model":"pi-4"}\n', encoding="utf-8"
            )
            (root / "inputs/local/pi.json").write_text(
                '{"provider":"local-provider"}\n', encoding="utf-8"
            )
            manifest_path = root / "luwu.toml"
            manifest_path.write_text(self._manifest(), encoding="utf-8")

            manifest = load_manifest(manifest_path)
            roots = manifest.roots
            assert roots is not None
            context = load_m6_inputs(manifest.resources[0], roots)
            rendered = render_template(
                manifest.resources[0],
                root=roots.source.path,
                m6_inputs=context,
            )

            self.assertEqual(context.values["model"].provenance, "global")
            self.assertEqual(context.values["provider"].provenance, "local")
            self.assertEqual(
                rendered.data, b'{"model":"pi-4","provider":"local-provider"}\n'
            )
            self.assertNotIn("pi-4", repr(context.values["model"]))

    def test_json_patch_preserves_unselected_bytes_and_absent_null(self) -> None:
        source = b'{"model": "old", "nullable": null, "keep": 1}\n'

        changed = patch_m6_document(source, format="json", updates={"model": "new"})
        deleted = patch_m6_document(source, format="json", deletes=("nullable",))

        self.assertEqual(changed, b'{"model": "new", "nullable": null, "keep": 1}\n')
        self.assertEqual(deleted, b'{"model": "old", "keep": 1}\n')

    def test_json_duplicate_or_invalid_input_fails_closed(self) -> None:
        with self.assertRaises(MutationError):
            patch_m6_document(
                b'{"model": 1, "model": 2}', format="json", updates={"model": 3}
            )
        with self.assertRaises(MutationError):
            patch_m6_document(
                b'{"model": "\xff"}', format="json", updates={"model": "x"}
            )

    def test_toml_patch_preserves_comments_and_rejects_tables(self) -> None:
        source = b'# keep\nmodel = "old"\nprovider = "x"\n'

        changed = patch_m6_document(source, format="toml", updates={"model": "new"})
        added = patch_m6_document(source, format="toml", updates={"changelog": "on"})
        deleted = patch_m6_document(source, format="toml", deletes=("provider",))

        self.assertEqual(changed, b'# keep\nmodel = "new"\nprovider = "x"\n')
        self.assertIn(b"# keep\n", added)
        self.assertIn(b'changelog = "on"\n', added)
        self.assertEqual(deleted, b'# keep\nmodel = "old"\n')
        with self.assertRaises(MutationError):
            patch_m6_document(
                b'[provider]\nmodel = "x"\n', format="toml", updates={"model": "y"}
            )

    def test_json_numbers_keep_decimal_precision_and_types(self) -> None:
        source = b'{"precise": 0.10000000000000001, "whole": 1, "flag": true}\n'

        parsed = parse_m6_document(source, format="json")
        changed = patch_m6_document(
            source,
            format="json",
            updates={"precise": Decimal("1234567890.1234567890123456789")},
        )

        self.assertEqual(parsed["precise"], Decimal("0.10000000000000001"))
        self.assertEqual(type(parsed["whole"]), Decimal)
        self.assertIs(parsed["flag"], True)
        self.assertIn(b"1234567890.1234567890123456789", changed)

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

    def test_toml_requires_standard_syntax_before_parsing_or_patching(self) -> None:
        cases = (
            b"model = 01\n",
            b"model = 1__0\n",
            b'model = "\\/"\n',
            b'model = "\\uD800"\n',
        )
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(MutationError) as parse_error:
                    parse_m6_document(source, format="toml")
                self.assertEqual(parse_error.exception.code, "m6_format")
                self.assertNotIn("model =", str(parse_error.exception))

                with self.assertRaises(MutationError) as patch_error:
                    patch_m6_document(source, format="toml", updates={"model": "new"})
                self.assertEqual(patch_error.exception.code, "m6_format")
                self.assertNotIn("model =", str(patch_error.exception))

    def test_toml_uses_toml_unicode_escapes_and_keeps_decimal_precision(self) -> None:
        source = (
            b'# keep\r\nmodel = "\\U0001F642"\r\n'
            b"precise = 0.1000000000000000000000000001"
        )

        parsed = parse_m6_document(source, format="toml")
        changed = patch_m6_document(source, format="toml", updates={"model": "updated"})

        self.assertEqual(parsed["model"], "🙂")
        self.assertEqual(parsed["precise"], Decimal("0.1000000000000000000000000001"))
        self.assertIs(type(parsed["precise"]), Decimal)
        self.assertEqual(
            changed,
            b'# keep\r\nmodel = "updated"\r\nprecise = 0.1000000000000000000000000001',
        )

    def test_toml_hash_inside_string_is_not_an_inline_comment(self) -> None:
        source = b'# keep\r\nmodel = "old#value"\r\nprovider = "x"'

        changed = patch_m6_document(
            source, format="toml", updates={"model": "new#value"}
        )

        self.assertEqual(
            changed,
            b'# keep\r\nmodel = "new#value"\r\nprovider = "x"',
        )

    def test_toml_string_token_consumes_escaped_quotes_and_unicode(self) -> None:
        source = b'model = "say \\"hi\\" # \\U0001F642"\nprovider = "old"\n'

        parsed = parse_m6_document(source, format="toml")
        changed = patch_m6_document(source, format="toml", updates={"provider": "new"})

        self.assertEqual(parsed["model"], 'say "hi" # 🙂')
        self.assertEqual(
            changed,
            b'model = "say \\"hi\\" # \\U0001F642"\nprovider = "new"\n',
        )

    def test_toml_inline_comments_preserve_exact_non_value_bytes(self) -> None:
        source = (
            b'# header\r\nmodel = "say \\"hi\\" # inside" \t# "keep"\r\n'
            b"provider = 'old#value' # literal comment"
        )

        parsed = parse_m6_document(source, format="toml")
        changed = patch_m6_document(
            source,
            format="toml",
            updates={"model": "updated", "provider": "next"},
        )

        self.assertEqual(parsed["model"], 'say "hi" # inside')
        self.assertEqual(parsed["provider"], "old#value")
        self.assertEqual(
            changed,
            b'# header\r\nmodel = "updated" \t# "keep"\r\n'
            b'provider = "next" # literal comment',
        )

    def test_toml_inline_comments_preserve_bool_int_decimal_bytes(self) -> None:
        cases = (
            (
                "bool",
                "enabled",
                "enabled = true# 开关注释  ".encode(),
                True,
                False,
                "enabled = false# 开关注释  ".encode(),
            ),
            (
                "int",
                "count",
                "count = 7  # 计数注释\t  ".encode(),
                7,
                12,
                "count = 12  # 计数注释\t  ".encode(),
            ),
            (
                "Decimal",
                "ratio",
                "ratio = 0.25# 小数注释  ".encode(),
                Decimal("0.25"),
                Decimal("1.125"),
                "ratio = 1.125# 小数注释  ".encode(),
            ),
        )

        for label, field, source, original, updated, expected in cases:
            with self.subTest(value_type=label):
                parsed = parse_m6_document(source, format="toml")
                changed = patch_m6_document(
                    source, format="toml", updates={field: updated}
                )

                self.assertEqual(parsed[field], original)
                self.assertIs(type(parsed[field]), type(original))
                self.assertEqual(changed, expected)

    def test_toml_unsupported_lexical_forms_fail_closed(self) -> None:
        cases = (
            b'"model" = "x"\n',
            b'model = """x\ny"""\n',
            b'model = "x" trailing\n',
            b'model = "x"\nnot-an-assignment\n',
            b'model = ["x"]\n',
            b"model = 1\nmodel = 2\n",
            b"model = inf\n",
            b"model = nan\n",
        )
        for source in cases:
            with self.subTest(source=source), self.assertRaises(MutationError):
                parse_m6_document(source, format="toml")

    def test_toml_patch_updates_and_deletes_without_touching_unselected_bytes(
        self,
    ) -> None:
        source = (
            b'# keep\r\nmodel = "old"\r\n'
            b'remove = "gone" # deleted with its assignment\r\n'
            b"keep = 12345678901234567890"
        )

        changed = patch_m6_document(
            source,
            format="toml",
            updates={"model": "new"},
            deletes=("remove",),
        )

        self.assertEqual(
            changed,
            b'# keep\r\nmodel = "new"\r\nkeep = 12345678901234567890',
        )

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


if __name__ == "__main__":
    unittest.main()
