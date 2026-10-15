#!/usr/bin/env python3
"""Refresh uv.lock and make it point at public PyPI.

The index configured on this machine (for example a company mirror) is used
only to resolve. The committed lock must not depend on it.

    python3 scripts/uv_lock_pypi.py [uv lock arguments]
    python3 scripts/uv_lock_pypi.py --rewrite-only
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PYPI_SIMPLE = "https://pypi.org/simple"
PYPI_FILES = "https://files.pythonhosted.org/packages"

# PyPI file paths end in <2 hex>/<2 hex>/<60 hex>/<filename>. Mirrors keep that
# tail but vary the prefix, and some drop the "packages" segment, so the
# prefix cannot be replaced textually.
_FILE_URL = re.compile(
    r'url = "https?://[^"]*?/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{60})/([^/"]+)"'
)
_REGISTRY = re.compile(r'registry = "https?://[^"]+"')
_ANY_URL = re.compile(r'(?:url|registry) = "(https?://[^"]+)"')


def rewrite(text: str) -> str:
    text = _REGISTRY.sub(f'registry = "{PYPI_SIMPLE}"', text)
    return _FILE_URL.sub(rf'url = "{PYPI_FILES}/\1/\2/\3/\4"', text)


def foreign_urls(text: str) -> list[str]:
    allowed = (PYPI_SIMPLE, PYPI_FILES + "/")
    return [url for url in _ANY_URL.findall(text) if not url.startswith(allowed)]


def main(argv: list[str]) -> int:
    lock = Path("uv.lock")
    rewrite_only = argv[:1] == ["--rewrite-only"]
    if not rewrite_only:
        status = subprocess.run(["uv", "lock", *argv], check=False).returncode
        if status:
            return status
    text = lock.read_text(encoding="utf-8")
    rewritten = rewrite(text)
    leftover = foreign_urls(rewritten)
    if leftover:
        print("uv.lock still has non-PyPI URLs; not keeping it:", file=sys.stderr)
        for url in leftover[:5]:
            print(f"  {url}", file=sys.stderr)
        return 1
    if rewritten != text:
        lock.write_text(rewritten, encoding="utf-8")
    return subprocess.run(
        ["uv", "lock", "--check", "--offline", "--default-index", PYPI_SIMPLE],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
