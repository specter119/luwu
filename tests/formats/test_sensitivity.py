from __future__ import annotations

import unittest

from luwu.sensitivity import is_sensitive_name


class SensitiveNameTests(unittest.TestCase):
    def test_secret_bearing_names_are_sensitive(self) -> None:
        for name in (
            "api_key",
            "apiKey",
            "API-KEY",
            "client_secret",
            "db_password",
            "refreshToken",
            "service_credentials",
            "private_key",
            "AccessKey",
            "secrets",
            "\uff54oken",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_sensitive_name(name))

    def test_ordinary_names_and_dotted_keys_are_not_sensitive(self) -> None:
        for name in ("theme", "editor", "new.key", "literal.key", "keyboard"):
            with self.subTest(name=name):
                self.assertFalse(is_sensitive_name(name))

    def test_fragment_match_is_deliberately_conservative(self) -> None:
        self.assertTrue(is_sensitive_name("tokens_per_line"))

    def test_a_declared_public_name_passes_only_by_exact_match(self) -> None:
        public = frozenset({"env_key"})
        self.assertFalse(is_sensitive_name("env_key", public=public))
        for name in ("ENV_KEY", "env_key2", "api_key", "env_key_token"):
            with self.subTest(name=name):
                self.assertEqual(
                    is_sensitive_name(name, public=public), is_sensitive_name(name)
                )


if __name__ == "__main__":
    unittest.main()
