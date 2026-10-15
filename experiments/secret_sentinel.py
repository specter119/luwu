# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise the real version-6 secret output boundaries with a sentinel."""

from __future__ import annotations

import io
import json
import stat
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from luwu import provider_cache
from luwu.cli import main
from luwu.providers import inspect_executable_identity

SENTINEL = "m4-secret-sentinel"


def _call(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    output = stdout.getvalue(), stderr.getvalue()
    assert SENTINEL not in "".join(output), arguments
    return code, *output


def _write_manifest(root: Path, target: Path) -> Path:
    (root / "templates").mkdir(parents=True)
    (root / "templates/database.conf.j2").write_text(
        "password={{ secrets.db_password }}\n", encoding="utf-8"
    )
    manifest = root / "luwu.toml"
    manifest.write_text(
        f'''version = 6
capabilities = ["subprocess"]

[resources.database]
kind = "template"
source = "templates/database.conf.j2"
target = "{target}"
owner = "source"
scope = "whole-file"
content_sensitivity = "secret"

[resources.database.providers.db_password]
type = "rbw"
item = "database-prod"
field = "password"
''',
        encoding="utf-8",
    )
    return manifest


def _write_provider(path: Path, *, succeeds: bool) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "prefix='m4-'\n"
        "suffix='secret-sentinel'\n"
        + (
            'printf \'%s%s\\n\' "$prefix" "$suffix"\n'
            if succeeds
            else 'printf \'%s%s\\n\' "$prefix" "$suffix" >&2\nexit 7\n'
        ),
        encoding="utf-8",
    )
    path.chmod(0o700)


def _assert_no_staging(*roots: Path) -> None:
    for root in roots:
        unexpected = [
            entry
            for entry in root.rglob("*")
            if entry.name.startswith(".")
            and ".luwu-" in entry.name
            and not entry.name.endswith(".luwu-lock")
        ]
        assert not unexpected, unexpected


def main_experiment() -> None:
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        root = Path(directory)
        manifest_root = root / "manifest"
        target_parent = root / "secret-target"
        target_parent.mkdir()
        target = target_parent / "database.conf"
        executable = root / "rbw"
        _write_provider(executable, succeeds=True)
        manifest = _write_manifest(manifest_root, target)
        journal = manifest_root / "journal.json"

        denied_code, denied_stdout, denied_stderr = _call(
            ["plan", "--manifest", str(manifest), "--json"]
        )
        assert denied_code == 0
        assert "plan_blocked" in denied_stdout
        assert not denied_stderr
        assert not target.exists()

        common = [
            "--manifest",
            str(manifest),
            "--allow-subprocess",
            "--rbw-executable",
            str(executable),
        ]
        apply_code, apply_stdout, apply_stderr = _call(
            ["apply", *common, "--record", str(journal), "--yes", "--json"]
        )
        assert apply_code == 0
        assert json.loads(apply_stdout)["state"] == "committed"
        assert not apply_stderr
        assert target.read_text(encoding="utf-8") == SENTINEL.join(("password=", "\n"))
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

        journal_bytes = journal.read_bytes()
        assert SENTINEL.encode() not in journal_bytes
        assert b"database-prod" not in journal_bytes
        assert b"password" not in journal_bytes

        _call(["record-inspect", "--record", str(journal), "--json"])
        _call(["recover", "--record", str(journal), "--json"])
        _call(
            [
                "recover",
                "--record",
                str(journal),
                "--allow-subprocess",
                "--rbw-executable",
                str(executable),
                "--json",
            ]
        )

        cache = manifest_root / "provider-cache.json"
        identity = inspect_executable_identity(executable)
        provider_cache.refresh_cache(
            cache,
            provider_type="rbw",
            capabilities=("subprocess",),
            executable_identity=provider_cache.ExecutableIdentity(
                device=identity.device,
                inode=identity.inode,
                mode=identity.mode,
                size=identity.size,
                mtime_ns=identity.mtime_ns,
            ),
            status="ok",
            ttl_seconds=300,
        )
        assert SENTINEL.encode() not in cache.read_bytes()
        _call(["cache-inspect", "--cache", str(cache), "--json"])
        _assert_no_staging(manifest_root, target_parent)

        error_root = root / "error-manifest"
        error_target_parent = root / "error-target"
        error_target_parent.mkdir()
        error_executable = root / "rbw-error"
        _write_provider(error_executable, succeeds=False)
        error_manifest = _write_manifest(
            error_root, error_target_parent / "database.conf"
        )
        error_code, error_stdout, error_stderr = _call(
            [
                "inspect",
                "--manifest",
                str(error_manifest),
                "--allow-subprocess",
                "--rbw-executable",
                str(error_executable),
                "--json",
            ]
        )
        assert error_code == 0
        assert "provider value is unavailable" in error_stdout
        assert not error_stderr
        assert not (error_target_parent / "database.conf").exists()
        _assert_no_staging(error_root, error_target_parent)

    print("success/error/output/journal/cache/staging secret sentinel checks passed")


if __name__ == "__main__":
    main_experiment()
