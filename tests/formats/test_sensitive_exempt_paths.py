"""A declared provider path exempts one exact sensitive key, and only that key."""

from __future__ import annotations

import unittest

from luwu.errors import LuwuError
from luwu.formats import get_leaf, parse_document, patch_document

_EXEMPT = (("service", "password"),)


class SensitiveExemptPathTests(unittest.TestCase):
    def _rejected(
        self, data: bytes, fmt: str, exempt: tuple[tuple[str, ...], ...] = ()
    ) -> bool:
        try:
            parse_document(data, format=fmt, sensitive_exempt_paths=exempt)
        except LuwuError as error:
            return getattr(error, "code", None) == "sensitive_input"
        return False

    def test_exact_path_is_exempt_in_json_jsonc_and_toml(self) -> None:
        for fmt, data in (
            ("json", b'{"service":{"password":"x"}}'),
            ("jsonc", b'{/* c */"service":{"password":"x"}}'),
            ("toml", b'[service]\npassword = "x"\n'),
        ):
            with self.subTest(fmt=fmt):
                self.assertTrue(self._rejected(data, fmt))
                document = parse_document(
                    data, format=fmt, sensitive_exempt_paths=_EXEMPT
                )
                self.assertEqual(get_leaf(document, ("service", "password")), "x")

    def test_the_same_key_anywhere_else_still_blocks(self) -> None:
        for fmt, data in (
            ("json", b'{"service":{"password":"x"},"other":{"password":"y"}}'),
            ("json", b'{"password":"y","service":{"password":"x"}}'),
            ("toml", b'[service]\npassword = "x"\n[other]\npassword = "y"\n'),
        ):
            with self.subTest(fmt=fmt, data=data):
                self.assertTrue(self._rejected(data, fmt, _EXEMPT))

    def test_an_exemption_never_reaches_through_an_array(self) -> None:
        data = b'{"service":[{"password":"x"}]}'
        self.assertTrue(self._rejected(data, "json", _EXEMPT))
        self.assertTrue(self._rejected(data, "json", (("service", "0", "password"),)))

    def test_only_the_exempt_path_may_be_patched(self) -> None:
        plain = parse_document(b'{"service":{}}\n', format="json")
        with self.assertRaises(LuwuError):
            patch_document(plain, {("service", "password"): "s"})
        document = parse_document(
            b'{"service":{}}\n', format="json", sensitive_exempt_paths=_EXEMPT
        )
        with self.assertRaises(LuwuError):
            patch_document(document, {("service", "api_key"): "s"})
        patched = patch_document(document, {("service", "password"): "s"})
        reparsed = parse_document(
            patched, format="json", sensitive_exempt_paths=_EXEMPT
        )
        self.assertEqual(get_leaf(reparsed, ("service", "password")), "s")

    def test_toml_insert_at_the_exempt_path(self) -> None:
        document = parse_document(
            b'[service]\nuser = "a"\n', format="toml", sensitive_exempt_paths=_EXEMPT
        )
        patched = patch_document(document, {("service", "password"): "s"})
        reparsed = parse_document(
            patched, format="toml", sensitive_exempt_paths=_EXEMPT
        )
        self.assertEqual(get_leaf(reparsed, ("service", "password")), "s")

    def test_a_sensitive_value_reparse_keeps_the_exemption(self) -> None:
        document = parse_document(
            b'{"service":{"password":"x"}}\n',
            format="json",
            sensitive_exempt_paths=_EXEMPT,
        )
        patched = patch_document(document, {("service", "password"): "y"})
        self.assertIn(b'"password"', patched)


if __name__ == "__main__":
    unittest.main()
