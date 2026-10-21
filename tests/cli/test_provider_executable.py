"""The --provider-executable option authorizes rbw for observing commands only."""

from __future__ import annotations

import json
import os
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from luwu.cli import main
from tests.support.fake_rbw import FakeRbwCase


class ProviderExecutableCliTests(FakeRbwCase):
    secret = "w3-cli-secret-c3"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.target_root = self.root / "target"
        for path in (
            self.source / "templates",
            self.inputs / "baselines",
            self.target_root,
        ):
            path.mkdir(parents=True)
        self.executable = self.install_rbw(
            document={
                "name": "fixture-item",
                "data": {"password": self.secret, "username": "user-fixture"},
                "notes": "notes-fixture",
            }
        )
        self.bind_socket()
        environment = patch.dict(os.environ, self.environ())
        environment.start()
        self.addCleanup(environment.stop)
        (self.source / "templates" / "app.json.j2").write_text(
            '{"service":{"password":"placeholder"}}\n', encoding="utf-8"
        )
        self.manifest = self.root / "luwu.toml"
        self.manifest.write_text(
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
resources = ["app"]

[providers.db]
type = "rbw"
item = "fixture-item"

[resources.app]
source = "templates/app.json.j2"
target = ".config/app.json"
format = "json"
baseline = "baselines/app.json"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.app.fields.password]
path = ["service", "password"]
owner = "provider"
provider = "db"
""",
            encoding="utf-8",
        )

    def _run(self, *argv: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def _base(self, command: str) -> tuple[str, ...]:
        return (command, "--manifest", str(self.manifest), "--profile", "work")

    def test_inspect_without_authority_blocks_without_fetching(self) -> None:
        code, stdout, stderr = self._run(*self._base("inspect"), "--json")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        resource = json.loads(stdout)["resources"][0]
        self.assertEqual(resource["status"], "blocked")
        self.assertEqual(resource["error_code"], "provider_authority_required")
        self.assertEqual(
            resource["providers"],
            [
                {
                    "provider": "db",
                    "item": "fixture-item",
                    "field": None,
                    "state": "provider_authority_required",
                }
            ],
        )
        self.assertEqual(self.recorded_calls(), [])

    def test_inspect_with_authority_reports_metadata_without_the_secret(self) -> None:
        code, stdout, stderr = self._run(
            *self._base("plan"), "--provider-executable", f"rbw={self.executable}"
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn('"status"', stdout)
        # Text mode renders the same per-resource provider metadata as JSON.
        self.assertIn(
            '"providers":[{"provider":"db","item":"fixture-item","field":null,'
            '"state":"ok"}]',
            stdout,
        )
        self.assertNotIn(self.secret, stdout)
        self.assertNotIn("placeholder", stdout)

    def test_apply_with_authority_writes_only_the_target(self) -> None:
        code, stdout, stderr = self._run(
            *self._base("apply"),
            "--provider-executable",
            f"rbw={self.executable}",
            "--yes",
        )

        self.assertEqual(code, 0, stderr)
        target = self.target_root / ".config" / "app.json"
        self.assertIn(self.secret, target.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn(self.secret, stdout)
        self.assertNotIn(self.secret, stderr)
        gets = sum(
            1 for argv, _env in self.recorded_calls() if argv and argv[0] == "get"
        )
        self.assertEqual(gets, 1)

    def test_other_provider_keys_are_a_fixed_usage_error(self) -> None:
        for value in ("bw=/usr/bin/bw", "rbw=relative/rbw", "rbw="):
            with self.subTest(value=value):
                with (
                    redirect_stderr(StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main(
                        [
                            *self._base("inspect"),
                            "--provider-executable",
                            value,
                        ]
                    )
                self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.recorded_calls(), [])

    def test_mutation_commands_do_not_accept_the_option(self) -> None:
        for argv in (
            (
                "accept",
                "--resource",
                "app",
                "--from",
                "desired",
                "--field",
                "password",
            ),
            ("adopt", "--resource", "app"),
            ("reverse-sync", "--resource", "app", "--field", "password"),
        ):
            with self.subTest(argv=argv):
                with (
                    redirect_stderr(StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main(
                        [
                            *argv,
                            "--provider-executable",
                            f"rbw={self.executable}",
                            "--manifest",
                            str(self.manifest),
                            "--profile",
                            "work",
                        ]
                    )
                self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.recorded_calls(), [])

    def test_accept_of_a_provider_field_is_refused_without_fetching(self) -> None:
        code, stdout, stderr = self._run(
            *self._base("accept"),
            "--resource",
            "app",
            "--from",
            "desired",
            "--field",
            "password",
            "--yes",
        )

        self.assertEqual(code, 2)
        self.assertIn("provider_field_refused", stderr)
        self.assertNotIn(self.secret, stdout + stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_reverse_undeclared_under_a_provider_path_is_refused(self) -> None:
        code, stdout, stderr = self._run(
            *self._base("reverse-sync"),
            "--resource",
            "app",
            "--undeclared",
            '["service", "password"]',
            "--yes",
        )

        self.assertEqual(code, 2)
        self.assertIn("undeclared_unsafe", stderr)
        self.assertNotIn(self.secret, stdout + stderr)
        self.assertEqual(self.recorded_calls(), [])


class ProviderDailyLoopCliTests(FakeRbwCase):
    """Accept and reverse-sync work on mixed resources without rbw."""

    secret = "w3-cli-secret-daily"

    def setUp(self) -> None:
        super().setUp()
        self.source = self.root / "source"
        self.inputs = self.root / "inputs"
        self.target_root = self.root / "target"
        for path in (
            self.source / "templates",
            self.inputs / "baselines",
            self.inputs / "local",
            self.target_root,
        ):
            path.mkdir(parents=True)
        self.executable = self.install_rbw(
            document={
                "name": "fixture-item",
                "data": {"password": self.secret, "username": "user-fixture"},
            }
        )
        self.bind_socket()
        environment = patch.dict(os.environ, self.environ())
        environment.start()
        self.addCleanup(environment.stop)
        self.manifest = self.root / "luwu.toml"
        (self.inputs / "local" / "model.json").write_text(
            '{"model":"model-v1"}\n', encoding="utf-8"
        )
        (self.source / "templates" / "app.json.j2").write_text(
            '{"service":{"password":"placeholder"},'
            '"model":{{ inputs.model | tojson }}}\n',
            encoding="utf-8",
        )
        self.manifest.write_text(
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
resources = ["app"]

[inputs.model]
layers = [
  { file = "local/model.json", path = ["model"], format = "json" },
]
reverse_to = { file = "local/model.json", path = ["model"], format = "json" }

[providers.db]
type = "rbw"
item = "fixture-item"

[resources.app]
source = "templates/app.json.j2"
target = ".config/app.json"
format = "json"
baseline = "baselines/app.json"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.app.fields.password]
path = ["service", "password"]
owner = "provider"
provider = "db"

[resources.app.fields.model]
path = ["model"]
owner = "merge"
input = "model"
""",
            encoding="utf-8",
        )

    def _run(self, *argv: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def _base(self, command: str) -> tuple[str, ...]:
        return (command, "--manifest", str(self.manifest), "--profile", "work")

    def _target(self) -> Path:
        return self.target_root / ".config" / "app.json"

    def test_accept_and_reverse_sync_of_a_public_field_never_fetch(self) -> None:
        code, _stdout, stderr = self._run(
            *self._base("accept"),
            "--resource",
            "app",
            "--from",
            "desired",
            "--field",
            "model",
            "--yes",
        )
        self.assertEqual(code, 0, stderr)
        baseline = json.loads(
            (self.inputs / "baselines" / "app.json").read_text(encoding="utf-8")
        )
        self.assertEqual(baseline["values"], {"model": "model-v1"})
        self.assertEqual(self.recorded_calls(), [])

        # The fetching loop writes the provider value and keeps the choice.
        code, stdout, _stderr = self._run(
            *self._base("apply"),
            "--provider-executable",
            f"rbw={self.executable}",
            "--yes",
        )
        self.assertEqual(code, 0)
        document = json.loads(self._target().read_text(encoding="utf-8"))
        self.assertEqual(document["service"]["password"], self.secret)
        self.assertEqual(document["model"], "model-v1")
        self.assertNotIn(self.secret, stdout)
        gets = sum(
            1 for argv, _env in self.recorded_calls() if argv and argv[0] == "get"
        )

        # A live edit to the public field reverse-syncs with no fetch at all.
        document["model"] = "model-v2"
        self._target().write_text(json.dumps(document) + "\n", encoding="utf-8")
        code, _stdout, stderr = self._run(
            *self._base("reverse-sync"),
            "--resource",
            "app",
            "--field",
            "model",
            "--yes",
        )
        self.assertEqual(code, 0, stderr)
        updated = json.loads(
            (self.inputs / "local" / "model.json").read_text(encoding="utf-8")
        )
        self.assertEqual(updated["model"], "model-v2")
        self.assertEqual(
            sum(1 for argv, _env in self.recorded_calls() if argv and argv[0] == "get"),
            gets,
        )

        # The fetching loop then sees agreement, with provider metadata ok.
        code, stdout, _stderr = self._run(
            *self._base("plan"),
            "--provider-executable",
            f"rbw={self.executable}",
            "--json",
        )
        self.assertEqual(code, 0)
        resource = json.loads(stdout)["resources"][0]
        self.assertIn(resource["status"], {"in_sync", "converged"})
        self.assertEqual(resource["providers"][0]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
