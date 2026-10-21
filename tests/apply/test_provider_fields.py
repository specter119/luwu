"""Provider-owned fields resolve through rbw and never leave the target file."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from luwu import engine as engine_module
from luwu.engine import (
    EngineError,
    accept_baseline,
    adopt_resource,
    adopt_resources,
    apply_plan,
    build_mutation_plan,
    build_plan,
    reverse_inputs,
    reverse_undeclared,
)
from luwu.filesystem import FileObservation, FilesystemError
from luwu.manifest import load_manifest
from luwu.providers import ProviderAuthority
from tests.support.fake_rbw import FakeRbwCase


class ProviderFieldCase(FakeRbwCase):
    """One profile whose ``app`` template takes a password from rbw."""

    secret = "canary-one"
    rotated = "canary-two"

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
        self.executable = self.install_rbw(document=self._document(self.secret))
        self.bind_socket()
        environment = patch.dict(os.environ, self.environ())
        environment.start()
        self.addCleanup(environment.stop)
        self.authority = ProviderAuthority(rbw_executable=self.executable)
        self.manifest_path = self.root / "luwu.toml"
        (self.source / "notes.txt").write_text("note-v1\n", encoding="utf-8")

    @staticmethod
    def _document(password: str) -> dict[str, object]:
        return {
            "name": "fixture-item",
            "data": {"password": password, "username": "user-fixture"},
            "notes": "notes-fixture",
        }

    def _rotate(self, password: str) -> None:
        (self.payload_dir / "get.stdout").write_bytes(
            json.dumps(self._document(password)).encode("utf-8")
        )

    def _write_manifest(
        self,
        *,
        user_field: bool = False,
        theme_field: bool = False,
        undeclared: str | None = None,
        notes: bool = False,
    ) -> None:
        names = ["app"] + (["notes"] if notes else [])
        resource_list = ", ".join(json.dumps(name) for name in names)
        undeclared_line = "" if undeclared is None else f'undeclared = "{undeclared}"\n'
        fields = """
[resources.app.fields.password]
path = ["service", "password"]
owner = "provider"
provider = "db"
"""
        if user_field:
            fields += """
[resources.app.fields.user]
path = ["service", "user"]
owner = "source"
"""
        if theme_field:
            fields += """
[resources.app.fields.theme]
path = ["theme"]
owner = "merge"
input = "theme"
"""
        inputs_block = ""
        if theme_field:
            (self.inputs / "local" / "theme.json").write_text(
                '{"theme":"t1"}\n', encoding="utf-8"
            )
            inputs_block = """
[inputs.theme]
layers = [
  { file = "local/theme.json", path = ["theme"], format = "json" },
]
reverse_to = { file = "local/theme.json", path = ["theme"], format = "json" }
"""
        notes_block = ""
        if notes:
            notes_block = """
[resources.notes]
source = "notes.txt"
target = "notes.txt"
"""
        self.manifest_path.write_text(
            f"""
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
resources = [{resource_list}]
{inputs_block}
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
{undeclared_line}{fields}{notes_block}""",
            encoding="utf-8",
        )

    def _write_template(self, *, theme: bool = False) -> None:
        theme_part = ',"theme":{{ inputs.theme | tojson }}' if theme else ""
        (self.source / "templates" / "app.json.j2").write_text(
            '{"service":{"password":"placeholder","user":"app-user"}'
            + theme_part
            + "}\n",
            encoding="utf-8",
        )

    def _load(self):
        return load_manifest(self.manifest_path, profile="work")

    def _target(self) -> Path:
        return self.target_root / ".config" / "app.json"

    def _get_calls(self) -> int:
        return sum(
            1 for argv, _env in self.recorded_calls() if argv and argv[0] == "get"
        )

    def _resource(self, plan, name: str = "app") -> dict[str, Any]:
        for item in plan.to_dict()["resources"]:
            if item["name"] == name:
                return item
        raise AssertionError(f"resource {name} missing from plan")

    def _deploy(self) -> None:
        result = apply_plan(
            build_mutation_plan(
                self._load(), provider_authority=self.authority, fetch_providers=True
            ),
            confirm=True,
        )
        self.assertEqual(result.outcome, "committed")

    def _rewrite_target(self, **changes: object) -> None:
        document = json.loads(self._target().read_text(encoding="utf-8"))
        for dotted, value in changes.items():
            cursor = document
            parts = dotted.split(".")
            for part in parts[:-1]:
                cursor = cursor[part]
            if value is None:
                del cursor[parts[-1]]
            else:
                cursor[parts[-1]] = value
        self._target().write_text(json.dumps(document) + "\n", encoding="utf-8")

    def _b_target(self) -> Path:
        return self.target_root / ".config" / "b.json"

    def _rewrite_b_target(self, theme: str) -> None:
        self._b_target().write_text(
            json.dumps({"theme": theme}) + "\n", encoding="utf-8"
        )

    def _theme_input(self) -> dict[str, object]:
        return json.loads(
            (self.inputs / "local" / "theme.json").read_text(encoding="utf-8")
        )

    def _write_shared_theme_manifest(self, *, app_declares_theme: bool) -> None:
        """Resources app and b render the same theme input; b always declares it."""
        (self.inputs / "local" / "theme.json").write_text(
            '{"theme":"t1"}\n', encoding="utf-8"
        )
        theme_render = (
            ',"theme":{{ inputs.theme | tojson }}' if app_declares_theme else ""
        )
        (self.source / "templates" / "app.json.j2").write_text(
            '{"service":{"password":"placeholder","user":"app-user"}'
            + theme_render
            + "}\n",
            encoding="utf-8",
        )
        (self.source / "templates" / "b.json.j2").write_text(
            '{"theme":{{ inputs.theme | tojson }}}\n', encoding="utf-8"
        )
        app_theme_field = (
            """
[resources.app.fields.theme]
path = ["theme"]
owner = "merge"
input = "theme"
"""
            if app_declares_theme
            else ""
        )
        self.manifest_path.write_text(
            f"""
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
resources = ["app", "b"]

[inputs.theme]
layers = [
  {{ file = "local/theme.json", path = ["theme"], format = "json" }},
]
reverse_to = {{ file = "local/theme.json", path = ["theme"], format = "json" }}

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
{app_theme_field}
[resources.b]
source = "templates/b.json.j2"
target = ".config/b.json"
format = "json"
baseline = "baselines/b.json"
parents = "create"

[resources.b.fields.theme]
path = ["theme"]
owner = "merge"
input = "theme"
""",
            encoding="utf-8",
        )

    def _prepare_shared_theme(self, *, app_declares_theme: bool) -> None:
        self._write_shared_theme_manifest(app_declares_theme=app_declares_theme)
        names = ("app", "b") if app_declares_theme else ("b",)
        for name in names:
            accepted = accept_baseline(
                build_mutation_plan(self._load()),
                resource=name,
                fields=("theme",),
                value_from="desired",
                confirm=True,
            )
            self.assertEqual(accepted.outcome, "committed")
        self._deploy()


class ProviderPlanTests(ProviderFieldCase):
    def test_plan_fetches_once_and_reports_metadata_only(self) -> None:
        self._write_manifest()
        self._write_template()

        plan = build_plan(self._load(), provider_authority=self.authority)

        resource = self._resource(plan)
        self.assertEqual(resource["status"], "missing")
        self.assertEqual(resource["action"], "create")
        self.assertEqual(
            resource["providers"],
            [
                {
                    "provider": "db",
                    "item": "fixture-item",
                    "field": None,
                    "state": "ok",
                }
            ],
        )
        self.assertEqual(resource["forward_fields"], ["password"])
        payload = json.dumps(plan.to_dict())
        self.assertNotIn(self.secret, payload)
        self.assertNotIn(hashlib.sha256(self.secret.encode()).hexdigest(), payload)
        self.assertNotIn("placeholder", payload)
        self.assertEqual(self._get_calls(), 1)

    def test_apply_writes_the_secret_with_the_declared_mode(self) -> None:
        self._write_manifest()
        self._write_template()

        self._deploy()

        content = self._target().read_bytes()
        self.assertIn(self.secret.encode(), content)
        self.assertNotIn(b"placeholder", content)
        self.assertEqual(stat.S_IMODE(self._target().stat().st_mode), 0o600)
        self.assertEqual(
            [p for p in self.target_root.rglob("*") if ".luwu-tmp" in p.name], []
        )
        # One fetch covers the plan, its re-observations, and verification.
        self.assertEqual(self._get_calls(), 1)

        settled = build_plan(self._load(), provider_authority=self.authority)
        self.assertEqual(self._resource(settled)["status"], "in_sync")

    def test_provider_only_resource_needs_no_baseline(self) -> None:
        self._write_manifest()
        self._write_template()

        self._deploy()

        self.assertFalse((self.inputs / "baselines" / "app.json").exists())

    def test_rotation_forwards_only_the_provider_field(self) -> None:
        self._write_manifest(user_field=True)
        self._write_template()
        plan = build_mutation_plan(self._load())
        accepted = accept_baseline(
            plan, resource="app", fields=("user",), value_from="desired", confirm=True
        )
        self.assertEqual(accepted.outcome, "committed")
        self._deploy()
        self._rotate(self.rotated)

        plan = build_plan(self._load(), provider_authority=self.authority)
        resource = self._resource(plan)
        self.assertEqual(resource["status"], "drifted")
        self.assertEqual(resource["forward_fields"], ["password"])

        self._deploy()

        document = json.loads(self._target().read_text(encoding="utf-8"))
        self.assertEqual(document["service"]["password"], self.rotated)
        self.assertEqual(document["service"]["user"], "app-user")

    def test_baseline_bytes_hold_no_secret_or_digest(self) -> None:
        self._write_manifest(user_field=True)
        self._write_template()
        plan = build_mutation_plan(self._load())
        accepted = accept_baseline(
            plan, resource="app", fields=("user",), value_from="desired", confirm=True
        )
        self.assertEqual(accepted.outcome, "committed")
        self._deploy()

        baseline = (self.inputs / "baselines" / "app.json").read_bytes()
        self.assertIn(b"app-user", baseline)
        # The provider field appears only as name/path metadata, never a value.
        values = json.loads(baseline)["values"]
        self.assertEqual(sorted(values), ["user"])
        self.assertNotIn(self.secret.encode(), baseline)
        self.assertNotIn(
            hashlib.sha256(self.secret.encode()).hexdigest().encode(), baseline
        )

    def test_plan_evidence_binds_provider_bytes(self) -> None:
        from luwu import engine as engine_module

        self._write_manifest()
        self._write_template()
        self._deploy()

        plan = build_mutation_plan(
            self._load(), provider_authority=self.authority, fetch_providers=True
        )
        # Changing the live bytes of a provider resource stales the plan.
        self._rewrite_target(**{"service.password": "tampered"})
        result = apply_plan(plan, confirm=True)
        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "stale_plan")

        # Evidence lives in process memory only; no repr or str leaks it.
        plan = build_plan(self._load(), provider_authority=self.authority)
        record = engine_module._PLAN_REGISTRY.get(plan)
        self.assertIsNotNone(record)
        assert record is not None
        assert record.provider_state is not None
        for obj in (plan, record, record.resources[0], record.provider_state):
            self.assertNotIn(self.secret, repr(obj))
            self.assertNotIn(self.secret, str(obj))
        # The evidence itself binds the provider-bearing values unmasked.
        projection = engine_module._plan_projection(plan)
        entries = [
            value
            for value in projection
            if isinstance(value, dict) and "desired_values" in value
        ]
        self.assertEqual(entries[0]["desired_values"]["password"], self.secret)

    def test_wide_mode_with_synced_value_plans_a_mode_only_write(self) -> None:
        self._write_manifest(undeclared="preserve")
        self._write_template()
        self._deploy()
        # Live drift: a wide mode and an undeclared key the plan must keep.
        self._rewrite_target(extra="keep")
        self._target().chmod(0o644)

        plan = build_plan(self._load(), provider_authority=self.authority)
        resource = self._resource(plan)
        self.assertEqual(resource["status"], "drifted")
        self.assertEqual(resource["action"], "forward")
        self.assertEqual(resource["forward_fields"], ["mode"])

        result = apply_plan(
            build_mutation_plan(
                self._load(), provider_authority=self.authority, fetch_providers=True
            ),
            confirm=True,
        )

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(stat.S_IMODE(self._target().stat().st_mode), 0o600)
        document = json.loads(self._target().read_text(encoding="utf-8"))
        self.assertEqual(document["service"]["password"], self.secret)
        self.assertEqual(document["extra"], "keep")
        settled = build_plan(self._load(), provider_authority=self.authority)
        self.assertEqual(self._resource(settled)["status"], "in_sync")

    def test_the_declared_mode_stays_in_sync(self) -> None:
        self._write_manifest()
        self._write_template()
        self._deploy()

        plan = build_plan(self._load(), provider_authority=self.authority)

        resource = self._resource(plan)
        self.assertEqual(resource["status"], "in_sync")
        self.assertEqual(resource["action"], "noop")

    def test_rotation_with_a_wide_mode_writes_once_with_the_declared_mode(
        self,
    ) -> None:
        self._write_manifest(undeclared="preserve")
        self._write_template()
        self._deploy()
        self._rewrite_target(extra="keep")
        self._target().chmod(0o644)
        self._rotate(self.rotated)

        plan = build_plan(self._load(), provider_authority=self.authority)
        self.assertEqual(self._resource(plan)["forward_fields"], ["password"])
        result = apply_plan(
            build_mutation_plan(
                self._load(), provider_authority=self.authority, fetch_providers=True
            ),
            confirm=True,
        )

        self.assertEqual(result.outcome, "committed")
        writes = [
            action.to_dict()
            for action in result.actions
            if action.resource == "app" and action.kind != "resource_skip"
        ]
        self.assertEqual(len(writes), 1)
        self.assertEqual(stat.S_IMODE(self._target().stat().st_mode), 0o600)
        document = json.loads(self._target().read_text(encoding="utf-8"))
        self.assertEqual(document["service"]["password"], self.rotated)
        self.assertEqual(document["extra"], "keep")

    def test_a_plain_template_keeps_a_wide_live_mode(self) -> None:
        # The mode rule applies only to resources with provider fields.
        (self.source / "templates" / "plain.json.j2").write_text(
            '{"model":"model-v1"}\n', encoding="utf-8"
        )
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
resources = ["plain"]

[resources.plain]
source = "templates/plain.json.j2"
target = ".config/plain.json"
format = "json"
baseline = "baselines/plain.json"
parents = "create"
parent_mode = "0700"
mode = "0600"

[resources.plain.fields.model]
path = ["model"]
owner = "source"
""",
            encoding="utf-8",
        )
        plan = build_plan(self._load())
        accepted = accept_baseline(
            plan,
            resource="plain",
            fields=("model",),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(accepted.outcome, "committed")
        result = apply_plan(
            build_mutation_plan(self._load(), fetch_providers=True), confirm=True
        )
        self.assertEqual(result.outcome, "committed")
        target = self.target_root / ".config" / "plain.json"
        target.chmod(0o644)

        plan = build_plan(self._load())

        resource = self._resource(plan, "plain")
        self.assertEqual(resource["status"], "in_sync")
        self.assertEqual(resource["action"], "noop")

    def test_missing_authority_blocks_without_fetching(self) -> None:
        self._write_manifest()
        self._write_template()

        plan = build_plan(self._load())

        resource = self._resource(plan)
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
        self.assertFalse(self._target().exists())

    def test_a_provider_failure_blocks_only_its_resource(self) -> None:
        self._write_manifest(notes=True)
        self._write_template()
        (self.payload_dir / "get.stdout").write_bytes(b"not json")

        plan = build_plan(self._load(), provider_authority=self.authority)
        app = self._resource(plan)
        self.assertEqual(app["status"], "blocked")
        self.assertEqual(app["error_code"], "provider_output_invalid")
        self.assertEqual(app["providers"][0]["state"], "provider_output_invalid")
        notes = self._resource(plan, "notes")
        self.assertEqual(notes["action"], "create")
        # Resources without provider fields keep the empty projection.
        self.assertEqual(notes["providers"], [])

        result = apply_plan(
            build_mutation_plan(
                self._load(), provider_authority=self.authority, fetch_providers=True
            ),
            confirm=True,
        )

        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.error_code, "provider_output_invalid")
        skips = [
            action
            for action in result.actions
            if action.kind == "resource_skip" and action.resource == "app"
        ]
        self.assertEqual(len(skips), 1)
        self.assertFalse(self._target().exists())
        self.assertTrue((self.target_root / "notes.txt").is_symlink())

    def test_a_locked_provider_blocks_only_its_resource(self) -> None:
        self._write_manifest(notes=True)
        self._write_template()
        self.install_rbw(
            document=self._document(self.secret),
            unlocked_exit=1,
            unlocked_stderr=b"rbw: error: agent is locked\n",
        )

        plan = build_plan(self._load(), provider_authority=self.authority)

        app = self._resource(plan)
        self.assertEqual(app["status"], "blocked")
        self.assertEqual(app["error_code"], "provider_locked")
        self.assertEqual(app["providers"][0]["state"], "provider_locked")
        notes = self._resource(plan, "notes")
        self.assertEqual(notes["action"], "create")
        self.assertNotEqual(notes["status"], "blocked")
        # The lock stops the fetch; the public resource is still planned.
        self.assertEqual(
            [argv[0] for argv, _env in self.recorded_calls()], ["unlocked"]
        )
        self.assertEqual(self._get_calls(), 0)
        self.assertFalse(self._target().exists())

    def test_an_unexpected_provider_failure_uses_the_fixed_code(self) -> None:
        self._write_manifest()
        self._write_template()

        def broken_run(*args: object, **kwargs: object) -> object:
            raise OSError("rbw said nope")

        with patch("luwu.providers.SubprocessRunner.run", broken_run):
            plan = build_plan(self._load(), provider_authority=self.authority)

        resource = self._resource(plan)
        self.assertEqual(resource["status"], "blocked")
        self.assertEqual(resource["error_code"], "provider_command_failed")
        self.assertEqual(resource["providers"][0]["state"], "provider_command_failed")

    def test_an_uninjectable_provider_value_blocks_the_resource(self) -> None:
        self._write_manifest()
        # The provider path sits below an array, so the format layer cannot
        # place the value.
        (self.source / "templates" / "app.json.j2").write_text(
            '{"service": []}\n', encoding="utf-8"
        )

        plan = build_plan(self._load(), provider_authority=self.authority)

        resource = self._resource(plan)
        self.assertEqual(resource["status"], "blocked")
        self.assertEqual(resource["error_code"], "provider_inject_failed")
        # The provider itself resolved; the format layer refused the value.
        self.assertEqual(resource["providers"][0]["state"], "ok")

    def test_a_non_string_placeholder_refuses_injection(self) -> None:
        self._write_manifest()
        (self.source / "templates" / "app.json.j2").write_text(
            '{"service":{"password":0}}\n', encoding="utf-8"
        )

        plan = build_plan(self._load(), provider_authority=self.authority)

        resource = self._resource(plan)
        self.assertEqual(resource["status"], "blocked")
        self.assertEqual(resource["error_code"], "provider_inject_failed")
        self.assertEqual(resource["providers"][0]["state"], "ok")
        self.assertFalse(self._target().exists())

    def test_apply_stages_a_provider_target_write_anonymously(self) -> None:
        self._write_manifest()
        self._write_template()
        calls: list[tuple[str, bool]] = []
        original = engine_module.write_file

        def recording_write_file(
            root: Path,
            path: Path,
            data: bytes,
            *,
            expected: FileObservation,
            mode: int | None = None,
            check_inputs: Callable[[], object] | None = None,
            anonymous: bool = False,
        ) -> tuple[int, int]:
            calls.append((Path(path).name, anonymous))
            return original(
                root,
                path,
                data,
                expected=expected,
                mode=mode,
                check_inputs=check_inputs,
                anonymous=anonymous,
            )

        with patch("luwu.engine.write_file", recording_write_file):
            self._deploy()

        self.assertEqual(calls, [("app.json", True)])

    def test_an_unsupported_anonymous_stage_maps_to_a_blocked_result(self) -> None:
        self._write_manifest()
        self._write_template()
        self._deploy()
        before = self._target().read_bytes()
        self._rotate(self.rotated)

        plan = build_mutation_plan(
            self._load(), provider_authority=self.authority, fetch_providers=True
        )
        with patch(
            "luwu.engine.write_file",
            side_effect=FilesystemError(
                "anonymous filesystem staging is unsupported",
                code="anonymous_staging_unsupported",
            ),
        ):
            result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "not_replaced")
        self.assertEqual(result.error_code, "anonymous_staging_unsupported")
        states = {
            action.resource: action.state
            for action in result.actions
            if action.kind == "target_write"
        }
        self.assertEqual(states, {"app": "not_replaced"})
        self.assertEqual(self._target().read_bytes(), before)
        self.assertEqual(
            [p for p in self.target_root.rglob("*") if ".luwu-tmp-" in p.name], []
        )

    def test_a_live_provider_edit_is_rewritten_and_keeps_reversed_choices(
        self,
    ) -> None:
        self._write_manifest(theme_field=True)
        self._write_template(theme=True)
        accepted = accept_baseline(
            build_mutation_plan(self._load()),
            resource="app",
            fields=("theme",),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(accepted.outcome, "committed")
        self._deploy()
        # A live local choice is reverse-synced into the declared input.
        self._rewrite_target(theme="t2")
        reversed_ = reverse_inputs(
            build_mutation_plan(self._load()),
            resource="app",
            fields=("theme",),
            confirm=True,
        )
        self.assertEqual(reversed_.outcome, "committed")
        # A manual live edit of the provider field is drift to overwrite.
        self._rewrite_target(**{"service.password": "manual-edit"})

        plan = build_plan(self._load(), provider_authority=self.authority)
        resource = self._resource(plan)
        self.assertEqual(resource["status"], "drifted")
        self.assertEqual(resource["forward_fields"], ["password"])

        result = apply_plan(
            build_mutation_plan(
                self._load(), provider_authority=self.authority, fetch_providers=True
            ),
            confirm=True,
        )

        self.assertEqual(result.outcome, "committed")
        content = self._target().read_bytes()
        self.assertIn(self.secret.encode(), content)
        self.assertNotIn(b"manual-edit", content)
        self.assertEqual(
            json.loads(content),
            {"service": {"password": self.secret, "user": "app-user"}, "theme": "t2"},
        )

    def test_a_reverse_candidate_keeps_a_wide_mode_until_fields_settle(self) -> None:
        self._write_manifest(theme_field=True)
        self._write_template(theme=True)
        accepted = accept_baseline(
            build_mutation_plan(self._load()),
            resource="app",
            fields=("theme",),
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(accepted.outcome, "committed")
        self._deploy()
        self._rewrite_target(theme="t2")
        self._target().chmod(0o644)

        plan = build_plan(self._load(), provider_authority=self.authority)

        resource = self._resource(plan)
        self.assertEqual(resource["status"], "reverse_candidate")
        self.assertEqual(resource["forward_fields"], [])
        self.assertEqual(resource["reverse_fields"], ["theme"])

        result = apply_plan(
            build_mutation_plan(
                self._load(), provider_authority=self.authority, fetch_providers=True
            ),
            confirm=True,
        )

        self.assertEqual(result.outcome, "no_changes")
        self.assertEqual(stat.S_IMODE(self._target().stat().st_mode), 0o644)

    def test_reverse_from_a_shared_input_names_the_real_consumer_conflict(
        self,
    ) -> None:
        # app carries a provider field and declares theme from the shared
        # input; b declares the same field. Reversing theme from b would
        # change app's unselected theme, so the block names app's theme: the
        # provider path renders the same placeholder on both sides of the
        # consumer comparison and takes no part in the conflict.
        self._prepare_shared_theme(app_declares_theme=True)
        self._rewrite_target(theme="t2")
        self._rewrite_b_target("t2")
        fetches = self._get_calls()

        with self.assertRaises(EngineError) as caught:
            reverse_inputs(
                build_mutation_plan(self._load()),
                resource="b",
                fields=("theme",),
                confirm=True,
            )

        self.assertEqual(caught.exception.code, "reverse_consumer_change")
        self.assertEqual(caught.exception.resource, "app")
        self.assertEqual(caught.exception.fields, ("theme",))
        self.assertEqual(self._get_calls(), fetches)
        self.assertEqual(self._theme_input(), {"theme": "t1"})

    def test_reverse_succeeds_when_the_provider_resource_does_not_share_the_field(
        self,
    ) -> None:
        # Same profile, but app does not declare theme: reversing theme from
        # b updates the shared input and app is not a consumer of the change.
        self._prepare_shared_theme(app_declares_theme=False)
        self._rewrite_b_target("t2")
        fetches = self._get_calls()

        result = reverse_inputs(
            build_mutation_plan(self._load()),
            resource="b",
            fields=("theme",),
            confirm=True,
        )

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(self._theme_input(), {"theme": "t2"})
        self.assertEqual(self._get_calls(), fetches)
        self.assertEqual(
            json.loads(self._target().read_text(encoding="utf-8"))["service"][
                "password"
            ],
            self.secret,
        )


class ProviderRefusalTests(ProviderFieldCase):
    def test_accept_and_reverse_refuse_provider_fields_without_fetching(self) -> None:
        self._write_manifest()
        self._write_template()
        plan = build_mutation_plan(self._load())

        with self.assertRaises(EngineError) as accepted:
            accept_baseline(
                plan,
                resource="app",
                fields=("password",),
                value_from="desired",
                confirm=True,
            )
        self.assertEqual(accepted.exception.code, "provider_field_refused")
        with self.assertRaises(EngineError) as reversed_:
            reverse_inputs(plan, resource="app", fields=("password",), confirm=True)
        self.assertEqual(reversed_.exception.code, "provider_field_refused")
        with self.assertRaises(EngineError) as undeclared:
            reverse_undeclared(
                plan,
                resource="app",
                paths=(("service", "password"),),
                confirm=True,
            )
        self.assertEqual(undeclared.exception.code, "undeclared_unsafe")

        # The refusals are local checks; nothing contacted a provider.
        self.assertEqual(self.recorded_calls(), [])

    def test_mutations_refuse_a_plan_that_contacted_providers(self) -> None:
        self._write_manifest(user_field=True, theme_field=True, notes=True)
        self._write_template(theme=True)
        plan = build_mutation_plan(
            self._load(), provider_authority=self.authority, fetch_providers=True
        )
        fetches = self._get_calls()
        template_before = (self.source / "templates" / "app.json.j2").read_bytes()

        with self.assertRaises(EngineError) as accepted:
            accept_baseline(
                plan,
                resource="app",
                fields=("user",),
                value_from="desired",
                confirm=True,
            )
        self.assertEqual(accepted.exception.code, "plan_invalid")
        with self.assertRaises(EngineError) as reversed_:
            reverse_inputs(plan, resource="app", fields=("theme",), confirm=True)
        self.assertEqual(reversed_.exception.code, "plan_invalid")
        with self.assertRaises(EngineError) as undeclared:
            reverse_undeclared(
                plan,
                resource="app",
                paths=(("service", "user"),),
                confirm=True,
            )
        self.assertEqual(undeclared.exception.code, "plan_invalid")
        with self.assertRaises(EngineError) as adopted:
            adopt_resource(plan, resource="app", confirm=True)
        self.assertEqual(adopted.exception.code, "plan_invalid")
        with self.assertRaises(EngineError) as adopted_batch:
            adopt_resources(plan, resources=("notes", "app"), confirm=True)
        self.assertEqual(adopted_batch.exception.code, "plan_invalid")

        # Every entry refused before doing any work: no new fetch, no write.
        self.assertEqual(self._get_calls(), fetches)
        self.assertFalse((self.inputs / "baselines" / "app.json").exists())
        self.assertFalse((self.inputs / "baselines" / "notes.json").exists())
        self.assertEqual(
            (self.source / "templates" / "app.json.j2").read_bytes(), template_before
        )
        self.assertEqual(self._theme_input(), {"theme": "t1"})

    def test_reverse_sync_of_a_sibling_field_needs_no_fetch(self) -> None:
        self._write_manifest(theme_field=True)
        self._write_template(theme=True)
        plan = build_mutation_plan(self._load())
        accepted = accept_baseline(
            plan, resource="app", fields=("theme",), value_from="desired", confirm=True
        )
        self.assertEqual(accepted.outcome, "committed")
        self._deploy()
        self._rewrite_target(theme="t2")

        plan = build_mutation_plan(self._load())
        self.assertEqual(self._resource(plan)["status"], "reverse_candidate")
        fetches = self._get_calls()
        result = reverse_inputs(plan, resource="app", fields=("theme",), confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(self._get_calls(), fetches)
        document = json.loads(
            (self.inputs / "local" / "theme.json").read_text(encoding="utf-8")
        )
        self.assertEqual(document["theme"], "t2")
        # The provider value never left the target file.
        self.assertEqual(
            json.loads(self._target().read_text(encoding="utf-8"))["service"][
                "password"
            ],
            self.secret,
        )

    def test_undeclared_preserve_keeps_unmanaged_content(self) -> None:
        self._write_manifest(undeclared="preserve")
        self._write_template()
        self._deploy()
        self._rewrite_target(extra="keep-me")
        self._rotate(self.rotated)

        self._deploy()

        document = json.loads(self._target().read_text(encoding="utf-8"))
        self.assertEqual(document["extra"], "keep-me")
        self.assertEqual(document["service"]["password"], self.rotated)

    def test_undeclared_block_skips_a_provider_resource(self) -> None:
        self._write_manifest()
        self._write_template()
        self._deploy()
        self._rewrite_target(extra="keep-me")
        self._rotate(self.rotated)

        plan = build_mutation_plan(
            self._load(), provider_authority=self.authority, fetch_providers=True
        )
        resource = self._resource(plan)
        self.assertEqual(resource["status"], "review")
        self.assertTrue(resource["undeclared_changed"])
        self.assertEqual(resource["providers"][0]["state"], "ok")
        fetches = self._get_calls()

        result = apply_plan(plan, confirm=True)

        self.assertEqual(result.outcome, "not_attempted")
        self.assertEqual(result.error_code, "review_required")
        self.assertEqual(self._get_calls(), fetches)
        document = json.loads(self._target().read_text(encoding="utf-8"))
        self.assertEqual(document["extra"], "keep-me")
        self.assertEqual(document["service"]["password"], self.secret)


class NonFetchingPlanTests(ProviderFieldCase):
    """Accept, adopt, and reverse-sync observe without contacting rbw."""

    def test_non_fetching_plan_neither_blocks_nor_fetches(self) -> None:
        self._write_manifest(user_field=True)
        self._write_template()

        plan = build_mutation_plan(self._load())

        resource = self._resource(plan)
        # Unbased like any fielded template without a baseline, not blocked.
        self.assertEqual(resource["status"], "unbased")
        self.assertIsNone(resource["error_code"])
        self.assertEqual(resource["providers"][0]["state"], "not_fetched")
        self.assertEqual(self.recorded_calls(), [])

    def test_non_fetching_classification_treats_provider_fields_as_unchanged(
        self,
    ) -> None:
        self._write_manifest()
        self._write_template()
        self._deploy()
        fetches = self._get_calls()

        plan = build_mutation_plan(self._load())

        resource = self._resource(plan)
        self.assertEqual(resource["status"], "in_sync")
        self.assertEqual(resource["forward_fields"], [])
        self.assertEqual(resource["reverse_fields"], [])
        self.assertEqual(self._get_calls(), fetches)

    def test_apply_refuses_a_non_fetching_plan(self) -> None:
        self._write_manifest()
        self._write_template()
        plan = build_mutation_plan(self._load(), fetch_providers=False)

        with self.assertRaises(EngineError) as raised:
            apply_plan(plan, confirm=True)

        self.assertEqual(raised.exception.code, "plan_invalid")
        self.assertFalse(self._target().exists())
        self.assertEqual(self.recorded_calls(), [])

    def test_accept_of_a_public_field_needs_no_provider(self) -> None:
        self._write_manifest(user_field=True)
        self._write_template()
        plan = build_mutation_plan(self._load())

        result = accept_baseline(
            plan, resource="app", fields=("user",), value_from="desired", confirm=True
        )

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(self.recorded_calls(), [])
        baseline = json.loads(
            (self.inputs / "baselines" / "app.json").read_text(encoding="utf-8")
        )
        self.assertEqual(baseline["values"], {"user": "app-user"})

        # A fetching apply still writes the provider value and keeps the
        # accepted public choice.
        self._deploy()
        document = json.loads(self._target().read_text(encoding="utf-8"))
        self.assertEqual(document["service"]["password"], self.secret)
        self.assertEqual(document["service"]["user"], "app-user")

    def test_reverse_sync_of_a_public_field_needs_no_provider(self) -> None:
        self._write_manifest(theme_field=True)
        self._write_template(theme=True)
        plan = build_mutation_plan(self._load())
        accepted = accept_baseline(
            plan, resource="app", fields=("theme",), value_from="desired", confirm=True
        )
        self.assertEqual(accepted.outcome, "committed")
        self._deploy()
        self._rewrite_target(theme="t2")
        fetches = self._get_calls()

        plan = build_mutation_plan(self._load(), fetch_providers=False)
        self.assertEqual(self._resource(plan)["status"], "reverse_candidate")
        result = reverse_inputs(plan, resource="app", fields=("theme",), confirm=True)

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(self._get_calls(), fetches)
        document = json.loads(
            (self.inputs / "local" / "theme.json").read_text(encoding="utf-8")
        )
        self.assertEqual(document["theme"], "t2")

        # The fetching loop afterwards agrees on live and desired; the
        # baseline still holds the pre-reverse value until the next apply.
        settled = build_plan(self._load(), provider_authority=self.authority)
        self.assertEqual(self._resource(settled)["status"], "converged")


if __name__ == "__main__":
    import unittest

    unittest.main()
