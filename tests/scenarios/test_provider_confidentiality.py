"""A provider value reaches exactly one file: the declared target.

Every command below runs through the real CLI entry with a fake rbw whose
output carries the shared canary constants. After each command the test walks
the whole case tree and asserts that the selected canary bytes and their
sha256 appear only in the fake's own vault store and, once an apply commits,
in the single declared target file. No other file, filename, leftover staging
entry, stdout, stderr, or JSON document may carry them, and values the fake
returned but no field selected must never surface at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from luwu.cli import main
from tests.support.fake_rbw import (
    CANARY_ALPHA,
    CANARY_BETA,
    SENTINEL_NOTES,
    SENTINEL_USERNAME,
    FakeRbwCase,
)

# The provider field selects this canary; the other values below are decoys
# that only the fake rbw's output contains and no field ever selects.
PROVIDER_FIELD_VALUE = CANARY_BETA
UNSELECTED_CANARY = CANARY_ALPHA
UNSELECTED_SENTINELS = (SENTINEL_USERNAME, SENTINEL_NOTES, "tenant-fixture")
PLACEHOLDER = "placeholder-provider-confidentiality"


class ProviderConfidentialityTests(FakeRbwCase):
    """One structured template with a public field and one provider field."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.target_root = self.root / "target"
        self.home = self.root / "home"
        for path in (
            self.source / "templates",
            self.inputs / "baselines",
            self.target_root,
            self.home,
        ):
            path.mkdir(parents=True)
        self.document = {
            "name": "fixture-item",
            "data": {
                "password": UNSELECTED_CANARY,
                "username": SENTINEL_USERNAME,
            },
            "notes": SENTINEL_NOTES,
            "fields": [
                {"name": "apiKey", "value": PROVIDER_FIELD_VALUE},
                {"name": "tenant", "value": "tenant-fixture"},
            ],
        }
        self.executable = self.install_rbw(document=self.document)
        self.bind_socket()
        environment = patch.dict(os.environ, self.environ())
        environment.start()
        self.addCleanup(environment.stop)
        (self.source / "templates" / "app.json.j2").write_text(
            '{"service":{"apiKey":"' + PLACEHOLDER + '"}}\n',
            encoding="utf-8",
        )
        (self.source / "notes.txt").write_text("public-note\n", encoding="utf-8")
        self.manifest_path = self.root / "luwu.toml"
        self.manifest_path.write_text(
            """
version = 8

[roots]
source = "source"
inputs = "inputs"
target = "target"

[profiles.work]
packages = ["app"]
exclude = []

[packages.app]
dependencies = []
resources = ["app", "notes"]

[providers.db]
type = "rbw"
item = "fixture-item"
field = "apiKey"

[resources.app]
source = "templates/app.json.j2"
target = ".config/app.json"
format = "json"
baseline = "baselines/app.json"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.app.fields.apiKey]
path = ["service", "apiKey"]
owner = "provider"
provider = "db"

[resources.notes]
source = "notes.txt"
target = "notes.txt"
""",
            encoding="utf-8",
        )

    def _target(self) -> Path:
        return self.target_root / ".config" / "app.json"

    def _vault_store(self) -> Path:
        return self.payload_dir / "get.stdout"

    def _run(self, *argv: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def _base(self, command: str) -> tuple[str, ...]:
        return (command, "--manifest", str(self.manifest_path), "--profile", "work")

    def _authorized(self, command: str) -> tuple[str, ...]:
        return (
            *self._base(command),
            "--provider-executable",
            f"rbw={self.executable}",
            "--json",
        )

    def _resource(self, payload: dict[str, object], name: str) -> dict[str, Any]:
        resources = payload["resources"]
        assert isinstance(resources, list)
        for item in resources:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        raise AssertionError(f"resource {name} missing from plan")

    def _get_calls(self) -> int:
        return sum(
            1 for argv, _env in self.recorded_calls() if argv and argv[0] == "get"
        )

    def _staging_entries(self) -> list[Path]:
        return sorted(
            path for path in self.root.rglob("*") if ".luwu-tmp-" in path.name
        )

    def _tree_files(self) -> list[Path]:
        return sorted(
            path for path in self.root.rglob("*") if path.is_file() or path.is_symlink()
        )

    def _assert_output_clean(self, *streams: str) -> None:
        forbidden = (
            PROVIDER_FIELD_VALUE,
            UNSELECTED_CANARY,
            *UNSELECTED_SENTINELS,
            hashlib.sha256(PROVIDER_FIELD_VALUE.encode()).hexdigest(),
            hashlib.sha256(UNSELECTED_CANARY.encode()).hexdigest(),
        )
        for stream in streams:
            for value in forbidden:
                self.assertNotIn(value, stream)

    def _assert_value_confined(self, *, target: Path | None) -> None:
        """The selected canary lives only in the vault store and the target.

        The unselected canary may live only in the vault store: no command may
        copy a value no declared field selected.
        """

        selected = {
            PROVIDER_FIELD_VALUE.encode(),
            hashlib.sha256(PROVIDER_FIELD_VALUE.encode()).hexdigest().encode(),
        }
        unselected = {
            UNSELECTED_CANARY.encode(),
            hashlib.sha256(UNSELECTED_CANARY.encode()).hexdigest().encode(),
        }
        allowed_selected = {self._vault_store()}
        if target is not None:
            self.assertTrue(target.is_file(), f"expected committed target {target}")
            allowed_selected.add(target)
        for path in self._tree_files():
            relative = path.relative_to(self.root)
            for value in (
                PROVIDER_FIELD_VALUE,
                UNSELECTED_CANARY,
                hashlib.sha256(PROVIDER_FIELD_VALUE.encode()).hexdigest(),
            ):
                self.assertNotIn(value, str(relative))
            if path.is_symlink():
                link_target = os.readlink(path)
                self.assertNotIn(PROVIDER_FIELD_VALUE, link_target)
                self.assertNotIn(UNSELECTED_CANARY, link_target)
                continue
            data = path.read_bytes()
            for needle in selected:
                if needle in data:
                    self.assertIn(
                        path,
                        allowed_selected,
                        f"{relative} holds the selected provider value",
                    )
            for needle in unselected:
                if needle in data:
                    self.assertEqual(
                        path,
                        self._vault_store(),
                        f"{relative} holds a value no field selected",
                    )
        self.assertEqual(self._staging_entries(), [], "staging entry was left behind")

    def test_plan_preview_and_apply_confine_the_provider_value(self) -> None:
        code, stdout, stderr = self._run(*self._authorized("plan"))

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        app = self._resource(payload, "app")
        self.assertEqual(app["status"], "missing")
        self.assertEqual(
            app["providers"],
            [
                {
                    "provider": "db",
                    "item": "fixture-item",
                    "field": "apiKey",
                    "state": "ok",
                }
            ],
        )
        self.assertFalse(self._target().exists())
        self.assertEqual(self._get_calls(), 1)
        self._assert_output_clean(stdout, stderr)
        self._assert_value_confined(target=None)

        code, stdout, stderr = self._run(*self._authorized("apply"))

        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["outcome"], "confirmation_required")
        self.assertFalse(self._target().exists())
        self.assertEqual(self._get_calls(), 2)
        self._assert_output_clean(stdout, stderr)
        self._assert_value_confined(target=None)

        code, stdout, stderr = self._run(*self._authorized("apply"), "--yes")

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["outcome"], "committed")
        target = self._target()
        document = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(document["service"]["apiKey"], PROVIDER_FIELD_VALUE)
        self.assertNotIn(PLACEHOLDER, target.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        # One fetch covers the plan, its re-observations, and verification.
        self.assertEqual(self._get_calls(), 3)
        self._assert_output_clean(stdout, stderr)
        self._assert_value_confined(target=target)

    def test_locked_vault_blocks_only_the_provider_resource(self) -> None:
        self.install_rbw(
            document=self.document,
            unlocked_exit=1,
            unlocked_stderr=b"rbw: error: agent is locked\n",
        )

        code, stdout, stderr = self._run(*self._authorized("plan"))

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        app = self._resource(payload, "app")
        self.assertEqual(app["status"], "blocked")
        self.assertEqual(app["error_code"], "provider_locked")
        self.assertEqual(app["providers"][0]["state"], "provider_locked")
        # The public resource is still planned; the lock is not profile-wide.
        notes = self._resource(payload, "notes")
        self.assertEqual(notes["action"], "create")
        self.assertNotEqual(notes["status"], "blocked")
        self.assertEqual(
            [argv[0] for argv, _env in self.recorded_calls()], ["unlocked"]
        )
        self.assertEqual(self._get_calls(), 0)
        self._assert_output_clean(stdout, stderr)
        self._assert_value_confined(target=None)

        code, stdout, stderr = self._run(*self._authorized("apply"), "--yes")

        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["outcome"], "partial")
        self.assertEqual(payload["error_code"], "provider_locked")
        self.assertFalse(self._target().exists())
        self.assertTrue((self.target_root / "notes.txt").is_symlink())
        self._assert_output_clean(stdout, stderr)
        self._assert_value_confined(target=None)

    def test_refused_mutations_never_fetch_or_expose(self) -> None:
        commands = (
            (
                (
                    "accept",
                    "--resource",
                    "app",
                    "--from",
                    "desired",
                    "--field",
                    "apiKey",
                    "--yes",
                ),
                "provider_field_refused",
            ),
            (
                (
                    "reverse-sync",
                    "--resource",
                    "app",
                    "--field",
                    "apiKey",
                    "--yes",
                ),
                "provider_field_refused",
            ),
            (
                (
                    "reverse-sync",
                    "--resource",
                    "app",
                    "--undeclared",
                    '["service", "apiKey"]',
                    "--yes",
                ),
                "undeclared_unsafe",
            ),
        )
        for argv, error_code in commands:
            with self.subTest(command=argv[0], option=argv[-2]):
                code, stdout, stderr = self._run(
                    *self._base(argv[0]), *argv[1:], "--json"
                )
                self.assertEqual(code, 2)
                payload = json.loads(stdout)
                self.assertEqual(payload["error"]["code"], error_code)
                self._assert_output_clean(stdout, stderr)

        self.assertEqual(self.recorded_calls(), [])
        self.assertFalse(self._target().exists())
        self.assertFalse((self.inputs / "baselines" / "app.json").exists())
        self._assert_value_confined(target=None)


if __name__ == "__main__":
    import unittest

    unittest.main()
