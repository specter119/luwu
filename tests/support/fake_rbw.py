"""Fake rbw executables and agent sockets for provider tests.

The real provider boundary refuses executables below group- or world-writable
ancestors, so the fake lives under a private 0700 tree whose sticky, root-owned
/tmp ancestor acts as the trusted boundary.
"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

CANARY_ALPHA = "canary-alpha-7f3a91"
SENTINEL_USERNAME = "user-sentinel-2d05e1"
SENTINEL_NOTES = "notes-sentinel-83aa4f"
CANARY_BETA = "canary-beta-c20e64"

DEFAULT_DOCUMENT = {
    "name": "fixture-item",
    "data": {"password": CANARY_ALPHA, "username": SENTINEL_USERNAME},
    "notes": SENTINEL_NOTES,
    "fields": [
        {"name": "token", "value": CANARY_BETA},
        {"name": "tenant", "value": "tenant-fixture"},
    ],
}

_SCRIPT = """#!/bin/sh
record={record}
payload={payload}
{{
  printf '==call==\\n'
  for word in "$@"; do
    printf '%s\\n' "$word"
  done
  printf '==env==\\n'
  env | LC_ALL=C sort
  printf '==end==\\n'
}} >> "$record"
case "${{1-}}" in
  unlocked)
    cat "$payload/unlocked.stderr" >&2
    exit {unlocked_exit}
    ;;
  get)
    {get_command}
    exit {get_exit}
    ;;
esac
exit 9
"""


def default_stdout(document: Mapping[str, object] | None = None) -> bytes:
    return json.dumps(document or DEFAULT_DOCUMENT).encode("utf-8")


def parse_record(data: str) -> list[tuple[list[str], dict[str, str]]]:
    """Parse the argv/environment pairs the fake rbw appended to its record."""

    calls: list[tuple[list[str], dict[str, str]]] = []
    argv: list[str] = []
    env: dict[str, str] = {}
    section: str | None = None
    for line in data.splitlines():
        if line == "==call==":
            argv = []
            section = "argv"
        elif line == "==env==":
            env = {}
            section = "env"
        elif line == "==end==":
            calls.append((argv, env))
            section = None
        elif section == "argv":
            argv.append(line)
        elif section == "env":
            key, _, value = line.partition("=")
            env[key] = value
    return calls


def _sh_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


class FakeRbwCase(unittest.TestCase):
    """Base case with a private tree, a fake rbw, and an agent socket."""

    def setUp(self) -> None:
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory(prefix="luwu-test-providers-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin_dir = self.root / "bin"
        self.payload_dir = self.root / "payload"
        self.runtime_root = self.root / "runtime"
        for directory in (self.bin_dir, self.payload_dir, self.runtime_root):
            directory.mkdir(mode=0o700)
        self.record = self.root / "record.log"

    def bind_socket(self, profile: str | None = None) -> Path:
        name = "rbw" if profile is None else f"rbw-{profile}"
        directory = self.runtime_root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "socket"
        agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(agent.close)
        agent.bind(str(path))
        return path

    def install_rbw(
        self,
        *,
        unlocked_exit: int = 0,
        unlocked_stderr: bytes = b"",
        document: Mapping[str, object] | None = None,
        get_stdout: bytes | None = None,
        get_exit: int = 0,
        get_command: str | None = None,
        name: str = "rbw",
    ) -> Path:
        (self.payload_dir / "unlocked.stderr").write_bytes(unlocked_stderr)
        if get_stdout is None:
            get_stdout = default_stdout(document)
        (self.payload_dir / "get.stdout").write_bytes(get_stdout)
        command = (
            get_command if get_command is not None else 'cat "$payload/get.stdout"'
        )
        executable = self.bin_dir / name
        executable.write_text(
            _SCRIPT.format(
                record=_sh_quote(self.record),
                payload=_sh_quote(self.payload_dir),
                unlocked_exit=unlocked_exit,
                get_command=command,
                get_exit=get_exit,
            ),
            encoding="utf-8",
        )
        executable.chmod(0o700)
        return executable

    def environ(self, **extra: str) -> dict[str, str]:
        environ = {
            "HOME": str(self.root / "home"),
            "XDG_RUNTIME_DIR": str(self.runtime_root),
        }
        environ.update(extra)
        return environ

    def recorded_calls(self) -> list[tuple[list[str], dict[str, str]]]:
        if not self.record.exists():
            return []
        return parse_record(self.record.read_text(encoding="utf-8"))
