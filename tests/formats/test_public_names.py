"""Declared public names let one secret-looking key through, exactly."""

from __future__ import annotations

import unittest

from luwu.errors import LuwuError
from luwu.formats import MISSING, get_leaf, parse_document, patch_document

_PUBLIC = frozenset({"env_key"})


class PublicNameTests(unittest.TestCase):
    def _rejected(self, data: bytes, fmt: str, public: frozenset[str]) -> bool:
        try:
            parse_document(data, format=fmt, public_names=public)
        except LuwuError as error:
            return getattr(error, "code", None) == "sensitive_input"
        return False

    def test_json_and_toml_reject_the_key_unless_it_is_declared(self) -> None:
        for fmt, data in (
            ("json", b'{"p":{"env_key":"OPENAI_API_KEY"}}'),
            ("jsonc", b'{"p":{"env_key":"OPENAI_API_KEY"} /* c */}'),
            ("toml", b'[p]\nenv_key = "OPENAI_API_KEY"\n'),
        ):
            with self.subTest(fmt=fmt):
                self.assertTrue(self._rejected(data, fmt, frozenset()))
                document = parse_document(data, format=fmt, public_names=_PUBLIC)
                self.assertEqual(get_leaf(document, ("p", "env_key")), "OPENAI_API_KEY")

    def test_only_the_exact_name_is_admitted(self) -> None:
        for fmt, data in (
            ("json", b'{"p":{"env_key":"x","api_key":"y"}}'),
            ("toml", b'[p]\nenv_key = "x"\napi_key = "y"\n'),
        ):
            with self.subTest(fmt=fmt):
                self.assertTrue(self._rejected(data, fmt, _PUBLIC))

    def test_patching_keeps_the_declaration_for_every_reparse(self) -> None:
        for fmt, data in (
            ("json", b'{"model":"a","p":{"env_key":"X"}}\n'),
            ("toml", b'model = "a"\n[p]\nenv_key = "X"\n'),
        ):
            with self.subTest(fmt=fmt):
                document = parse_document(data, format=fmt, public_names=_PUBLIC)
                patched = patch_document(document, {("model",): "b"})
                self.assertIn(b"env_key", patched)
                self.assertEqual(
                    get_leaf(
                        parse_document(patched, format=fmt, public_names=_PUBLIC),
                        ("model",),
                    ),
                    "b",
                )

    def test_a_patch_path_may_name_a_public_key_only_when_declared(self) -> None:
        document = parse_document(b'{"p":{}}', format="json", public_names=_PUBLIC)
        patched = patch_document(document, {("p", "env_key"): "X"})
        self.assertIn(b"env_key", patched)
        plain = parse_document(b'{"p":{}}', format="json")
        with self.assertRaises(LuwuError):
            patch_document(plain, {("p", "env_key"): "X"})
        self.assertIsNot(MISSING, None)


if __name__ == "__main__":
    unittest.main()
