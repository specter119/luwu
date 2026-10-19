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


if __name__ == "__main__":
    unittest.main()
