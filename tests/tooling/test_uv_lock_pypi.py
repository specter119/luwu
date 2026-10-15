"""Tests for the uv.lock PyPI rewrite."""

from __future__ import annotations

import unittest

from scripts.uv_lock_pypi import PYPI_FILES, PYPI_SIMPLE, foreign_urls, rewrite

_TAIL = "df/bf/f7da0350254c0ed7c72f3e33cef02e048281fec7ecec5f032d4aac52226b"
_HASH = (
    'hash = "sha256:0137fb05990d35f1275a587e9aee6d56da821fc83491a0fb838183be43f66d6d"'
)


def _lock(registry: str, sdist_prefix: str, wheel_prefix: str) -> str:
    return (
        f'source = {{ registry = "{registry}" }}\n'
        f'sdist = {{ url = "{sdist_prefix}/{_TAIL}/jinja2-3.1.6.tar.gz", {_HASH} }}\n'
        f'wheels = [\n    {{ url = "{wheel_prefix}/{_TAIL}/jinja2-3.1.6-py3-none-any.whl", {_HASH} }},\n]\n'
    )


class UvLockPypiTests(unittest.TestCase):
    def _expected(self) -> str:
        return _lock(PYPI_SIMPLE, PYPI_FILES, PYPI_FILES)

    def test_mirror_layout_with_packages_prefix_is_rewritten(self) -> None:
        text = _lock(
            "https://mirrors.example.org/pypi/simple",
            "https://mirrors.example.org/pypi/packages",
            "https://mirrors.example.org/pypi/packages",
        )

        self.assertEqual(rewrite(text), self._expected())

    def test_artifactory_layout_without_a_packages_segment_is_rewritten(self) -> None:
        text = _lock(
            "https://repo.example.org/api/pypi/virtual/simple",
            "https://repo.example.org/api/pypi/virtual/packages/packages",
            "https://repo.example.org/api/pypi/virtual",
        )

        self.assertEqual(rewrite(text), self._expected())
        self.assertEqual(foreign_urls(rewrite(text)), [])

    def test_rewrite_is_idempotent_and_keeps_hashes(self) -> None:
        once = rewrite(
            _lock(
                "https://repo.example.org/simple",
                "https://repo.example.org/packages",
                "https://repo.example.org/packages",
            )
        )

        self.assertEqual(rewrite(once), once)
        self.assertEqual(once.count(_HASH), 2)

    def test_unrecognized_file_url_is_reported_instead_of_kept(self) -> None:
        text = (
            f'source = {{ registry = "{PYPI_SIMPLE}" }}\n'
            'sdist = { url = "https://repo.example.org/flat/jinja2-3.1.6.tar.gz" }\n'
        )

        self.assertEqual(
            foreign_urls(rewrite(text)),
            ["https://repo.example.org/flat/jinja2-3.1.6.tar.gz"],
        )


if __name__ == "__main__":
    unittest.main()
