"""Tests for toml comment loop."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import luwu.m6_pilot as m6_pilot_module
from luwu.errors import LuwuError
from luwu.m6_pilot import build_m6_plan, reverse_m6_inputs
from luwu.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[2]


CODEX_FIXTURE = ROOT / "tests" / "fixtures" / "pilot_v7" / "codex"


GLOBAL_INPUT_INITIAL = (
    b"# Global public inputs; value comments are retained.\n"
    b'model = "synthetic-codex-model-v1" # global model note: keep bytes\n'
    b'retained = "public-note#literal" # unselected global note\n'
)


LOCAL_INPUT_INITIAL = (
    b"# Local public inputs; reverse updates only model_provider.\n"
    b'model_provider = "synthetic-codex-provider-v1" # local provider note\n'
    b'retained = "public-note#literal" # unselected local note\n'
)


LOCAL_INPUT_AFTER_REVERSE = (
    b"# Local public inputs; reverse updates only model_provider.\n"
    b'model_provider = "synthetic-codex-provider-live#v2" # local provider note\n'
    b'retained = "public-note#literal" # unselected local note\n'
)


LOCAL_INPUT_CHANGED_COMMENT = (
    b"# Local public inputs; reverse updates only model_provider.\n"
    b'model_provider = "synthetic-codex-provider-v1" # changed after plan\n'
    b'retained = "public-note#literal" # unselected local note\n'
)


GLOBAL_INPUT_AFTER_SOURCE_UPDATE = (
    b"# Global public inputs; value comments are retained.\n"
    b'model = "synthetic-codex-model-v2" # global model note: keep bytes\n'
    b'retained = "public-note#literal" # unselected global note\n'
)


TARGET_CREATED = (
    b'model = "synthetic-codex-model-v1"\n'
    b'model_provider = "synthetic-codex-provider-v1"\n'
)


TARGET_WITH_COMMENTS = (
    b"# Synthetic live target; keep this header.\n"
    b'model = "synthetic-codex-model-v1" # source model note\n'
    b'model_provider = "synthetic-codex-provider-v1" # live provider note\n'
)


TARGET_LIVE_PROVIDER_CHANGED = (
    b"# Synthetic live target; keep this header.\n"
    b'model = "synthetic-codex-model-v1" # source model note\n'
    b'model_provider = "synthetic-codex-provider-live#v2" # live provider note\n'
)


TARGET_AFTER_SOURCE_FORWARD = (
    b"# Synthetic live target; keep this header.\n"
    b'model = "synthetic-codex-model-v2" # source model note\n'
    b'model_provider = "synthetic-codex-provider-live#v2" # live provider note\n'
)


TARGET_AFTER_MODEL_DELETE = (
    b"# Synthetic live target; keep this header.\n"
    b'model_provider = "synthetic-codex-provider-v1" # live provider note\n'
)


TARGET_MODEL_CONFLICT = (
    b"# Synthetic live target; keep this header.\n"
    b'model = "synthetic-target-model-conflict" # source model note\n'
    b'model_provider = "synthetic-codex-provider-v1" # live provider note\n'
)


TARGET_WITH_UNDECLARED_FIELD = TARGET_WITH_COMMENTS + (
    b'unmanaged_public = "synthetic-undeclared" # preserve undeclared data\n'
)


LOCAL_INPUT_WITH_SENSITIVE_FIELD = LOCAL_INPUT_INITIAL + (
    b'api_key = "synthetic-test-marker-do-not-disclose" # rejected key\n'
)


INVALID_TOML_INPUTS = (
    (
        "trailing-token",
        (
            b"# Local public inputs; malformed assignment must fail closed.\n"
            b'model_provider = "synthetic-provider-v1" trailing # invalid token\n'
            b'retained = "public-note#literal" # unselected local note\n'
        ),
    ),
    (
        "non-scalar-token",
        (
            b"# Local public inputs; arrays are outside the bounded subset.\n"
            b'model_provider = ["synthetic-provider"] # unsupported value\n'
            b'retained = "public-note#literal" # unselected local note\n'
        ),
    ),
)


class _TomlLoopCase:
    def __init__(self, root: Path) -> None:
        shutil.copytree(CODEX_FIXTURE, root, dirs_exist_ok=True)
        self.root = root
        self.manifest = root / "luwu.toml"
        self.target = root / "home" / ".codex" / "config.toml"
        self.baseline = root / "home" / "inputs" / "baselines" / "codex.json"
        self.global_input = root / "home" / "inputs" / "global" / "codex.toml"
        self.local_input = root / "home" / "inputs" / "local" / "codex.toml"
        self.template = root / "home" / "repo" / "templates" / "codex-config.toml.j2"
        self.process_home = root / "isolated-process-home"
        self.process_home.mkdir()
        (root / "tmp").mkdir()
        self.global_input.write_bytes(GLOBAL_INPUT_INITIAL)
        self.local_input.write_bytes(LOCAL_INPUT_INITIAL)

    def cli(self, *arguments: str, expected_code: int) -> dict[str, object]:
        environment = {
            "HOME": str(self.process_home),
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(self.root / "tmp"),
            "XDG_CACHE_HOME": str(self.process_home / ".cache"),
            "XDG_CONFIG_HOME": str(self.process_home / ".config"),
            "XDG_DATA_HOME": str(self.process_home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.process_home / ".local" / "state"),
        }
        source_root = ROOT / "src"
        if source_root.exists():
            environment["PYTHONPATH"] = str(source_root)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "luwu",
                *arguments,
                "--manifest",
                str(self.manifest),
                "--json",
            ],
            cwd=self.process_home,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != expected_code:
            raise AssertionError(
                f"CLI {arguments[0]} returned {completed.returncode}, "
                f"expected {expected_code}; stdout={completed.stdout!r}; "
                f"stderr={completed.stderr!r}"
            )
        if completed.stderr:
            raise AssertionError(f"CLI unexpectedly wrote stderr: {completed.stderr!r}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise AssertionError("CLI did not emit one JSON object") from None
        if not isinstance(payload, dict):
            raise TypeError("CLI JSON result must be an object")
        return cast(dict[str, object], payload)


def _single_resource(payload: dict[str, object]) -> dict[str, object]:
    resources = payload.get("resources")
    if not isinstance(resources, list) or len(resources) != 1:
        raise AssertionError("M7 fixture must report exactly one resource")
    resource = resources[0]
    if not isinstance(resource, dict) or not all(
        isinstance(key, str) for key in resource
    ):
        raise AssertionError("CLI resource must be an object with string keys")
    return cast(dict[str, object], resource)


def _error_code(payload: dict[str, object]) -> str | None:
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


class TomlCommentLoopV7Tests(unittest.TestCase):
    def _case(self) -> _TomlLoopCase:
        temporary = tempfile.TemporaryDirectory(prefix="luwu-m7-comments-")
        self.addCleanup(temporary.cleanup)
        return _TomlLoopCase(Path(temporary.name))

    def _seed_cli(self, case: _TomlLoopCase) -> bytes:
        accepted_preview = case.cli(
            "accept",
            "--resource",
            "codex",
            "--from",
            "desired",
            "--field",
            "model",
            "--field",
            "model_provider",
            expected_code=2,
        )
        self.assertEqual(accepted_preview["outcome"], "confirmation_required")
        self.assertFalse(case.baseline.exists())

        accepted = case.cli(
            "accept",
            "--resource",
            "codex",
            "--from",
            "desired",
            "--field",
            "model",
            "--field",
            "model_provider",
            "--yes",
            expected_code=0,
        )
        self.assertEqual(accepted["outcome"], "committed")
        baseline_bytes = case.baseline.read_bytes()

        create_plan = case.cli("plan", expected_code=0)
        self.assertEqual(_single_resource(create_plan)["action"], "create")
        create_preview = case.cli("apply", expected_code=2)
        self.assertEqual(_single_resource(create_preview)["action"], "create")
        self.assertFalse(case.target.exists())

        created = case.cli("apply", "--yes", expected_code=0)
        self.assertEqual(created["outcome"], "committed")
        self.assertEqual(case.target.read_bytes(), TARGET_CREATED)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)
        return baseline_bytes

    def test_cli_bidirectional_loop_preserves_inline_comments(self) -> None:
        case = self._case()
        baseline_bytes = self._seed_cli(case)

        case.target.write_bytes(TARGET_WITH_COMMENTS)
        comment_only_plan = case.cli("plan", expected_code=0)
        self.assertEqual(_single_resource(comment_only_plan)["action"], "noop")
        self.assertEqual(case.target.read_bytes(), TARGET_WITH_COMMENTS)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

        case.target.write_bytes(TARGET_LIVE_PROVIDER_CHANGED)
        target_before_preview = case.target.read_bytes()
        local_before_preview = case.local_input.read_bytes()
        reverse_preview = case.cli(
            "reverse-sync",
            "--resource",
            "codex",
            "--field",
            "model_provider",
            expected_code=2,
        )
        self.assertEqual(reverse_preview["outcome"], "confirmation_required")
        self.assertEqual(case.target.read_bytes(), target_before_preview)
        self.assertEqual(case.local_input.read_bytes(), local_before_preview)

        reversed_result = case.cli(
            "reverse-sync",
            "--resource",
            "codex",
            "--field",
            "model_provider",
            "--yes",
            expected_code=0,
        )
        self.assertEqual(reversed_result["outcome"], "committed")
        self.assertEqual(reversed_result["fields"], ["model_provider"])
        self.assertEqual(case.local_input.read_bytes(), LOCAL_INPUT_AFTER_REVERSE)
        self.assertEqual(case.target.read_bytes(), TARGET_LIVE_PROVIDER_CHANGED)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

        case.global_input.write_bytes(GLOBAL_INPUT_AFTER_SOURCE_UPDATE)
        forward_plan = case.cli("plan", expected_code=0)
        planned_resource = _single_resource(forward_plan)
        self.assertEqual(planned_resource["action"], "forward")
        self.assertEqual(planned_resource["forward_fields"], ["model"])

        target_before_forward_preview = case.target.read_bytes()
        forward_preview = case.cli("apply", expected_code=2)
        self.assertEqual(_single_resource(forward_preview)["action"], "forward")
        self.assertEqual(case.target.read_bytes(), target_before_forward_preview)
        self.assertEqual(
            case.global_input.read_bytes(), GLOBAL_INPUT_AFTER_SOURCE_UPDATE
        )

        forward_result = case.cli("apply", "--yes", expected_code=0)
        self.assertEqual(forward_result["outcome"], "committed")
        self.assertEqual(forward_result["fields"], ["model"])
        self.assertEqual(case.target.read_bytes(), TARGET_AFTER_SOURCE_FORWARD)
        self.assertEqual(case.local_input.read_bytes(), LOCAL_INPUT_AFTER_REVERSE)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

        final_plan = case.cli("plan", expected_code=0)
        self.assertEqual(_single_resource(final_plan)["action"], "noop")
        self.assertEqual(case.target.read_bytes(), TARGET_AFTER_SOURCE_FORWARD)

        target_before_noop_apply = case.target.read_bytes()
        inputs_before_noop_apply = {
            case.global_input: case.global_input.read_bytes(),
            case.local_input: case.local_input.read_bytes(),
        }
        baseline_before_noop_apply = case.baseline.read_bytes()
        no_changes = case.cli("apply", "--yes", expected_code=0)
        self.assertEqual(no_changes["outcome"], "no_changes")
        self.assertEqual(case.target.read_bytes(), target_before_noop_apply)
        self.assertEqual(
            {path: path.read_bytes() for path in inputs_before_noop_apply},
            inputs_before_noop_apply,
        )
        self.assertEqual(case.baseline.read_bytes(), baseline_before_noop_apply)

    def test_source_owned_delete_removes_assignment_and_inline_comment(self) -> None:
        case = self._case()
        self._seed_cli(case)
        case.target.write_bytes(TARGET_WITH_COMMENTS)
        case.template.write_bytes(
            b"model_provider = {{ local_inputs.model_provider | tojson }}\n"
        )

        plan = case.cli("plan", expected_code=0)
        planned_resource = _single_resource(plan)
        self.assertEqual(planned_resource["action"], "forward")
        self.assertEqual(planned_resource["forward_fields"], ["model"])

        target_before = case.target.read_bytes()
        preview = case.cli("apply", expected_code=2)
        self.assertEqual(_single_resource(preview)["action"], "forward")
        self.assertEqual(case.target.read_bytes(), target_before)

        result = case.cli("apply", "--yes", expected_code=0)
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(result["fields"], ["model"])
        self.assertEqual(case.target.read_bytes(), TARGET_AFTER_MODEL_DELETE)

        fresh_plan = case.cli("plan", expected_code=0)
        self.assertEqual(_single_resource(fresh_plan)["action"], "noop")
        self.assertEqual(case.target.read_bytes(), TARGET_AFTER_MODEL_DELETE)

    def test_reverse_rejects_stale_plan_after_input_comment_change(self) -> None:
        case = self._case()
        baseline_bytes = self._seed_cli(case)
        case.target.write_bytes(TARGET_LIVE_PROVIDER_CHANGED)
        plan = build_m6_plan(load_manifest(case.manifest))
        self.assertIn("model_provider", plan.resource.reverse_fields)

        case.local_input.write_bytes(LOCAL_INPUT_CHANGED_COMMENT)
        stale_input = case.local_input.read_bytes()
        stale_target = case.target.read_bytes()
        with self.assertRaises(LuwuError) as raised:
            reverse_m6_inputs(plan, fields=("model_provider",), confirm=True)

        self.assertEqual(raised.exception.code, "m6_stale_plan")
        self.assertEqual(case.local_input.read_bytes(), stale_input)
        self.assertEqual(case.target.read_bytes(), stale_target)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

    def test_conflicting_source_and_live_model_fail_closed(self) -> None:
        case = self._case()
        baseline_bytes = self._seed_cli(case)
        case.global_input.write_bytes(GLOBAL_INPUT_AFTER_SOURCE_UPDATE)
        case.target.write_bytes(TARGET_MODEL_CONFLICT)
        input_before = case.global_input.read_bytes()
        target_before = case.target.read_bytes()

        planned = case.cli("plan", expected_code=0)
        planned_resource = _single_resource(planned)
        self.assertEqual(planned_resource["action"], "review")

        blocked = case.cli("apply", "--yes", expected_code=2)
        self.assertEqual(_error_code(blocked), "m6_review_required")
        self.assertEqual(case.global_input.read_bytes(), input_before)
        self.assertEqual(case.target.read_bytes(), target_before)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

    def test_undeclared_live_key_blocks_apply_without_rewriting_comments(self) -> None:
        case = self._case()
        baseline_bytes = self._seed_cli(case)
        case.target.write_bytes(TARGET_WITH_UNDECLARED_FIELD)
        target_before = case.target.read_bytes()
        local_before = case.local_input.read_bytes()

        planned = case.cli("plan", expected_code=0)
        self.assertEqual(_single_resource(planned)["action"], "review")
        blocked = case.cli("apply", "--yes", expected_code=2)

        self.assertEqual(_error_code(blocked), "m6_review_required")
        self.assertEqual(case.target.read_bytes(), target_before)
        self.assertEqual(case.local_input.read_bytes(), local_before)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

    def test_sensitive_key_is_rejected_without_disclosing_its_value(self) -> None:
        case = self._case()
        baseline_bytes = self._seed_cli(case)
        case.local_input.write_bytes(LOCAL_INPUT_WITH_SENSITIVE_FIELD)
        input_before = case.local_input.read_bytes()
        target_before = case.target.read_bytes()

        result = case.cli("plan", expected_code=0)
        resource = _single_resource(result)

        self.assertEqual(resource["status"], "blocked")
        self.assertEqual(resource["action"], "review")
        self.assertIn("m6_sensitive_input", str(resource["reason"]))
        self.assertNotIn("synthetic-test-marker-do-not-disclose", json.dumps(result))
        self.assertEqual(case.local_input.read_bytes(), input_before)
        self.assertEqual(case.target.read_bytes(), target_before)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

    def test_invalid_toml_tokens_fail_closed(self) -> None:
        for label, invalid_input in INVALID_TOML_INPUTS:
            with self.subTest(token=label):
                case = self._case()
                baseline_bytes = self._seed_cli(case)
                case.local_input.write_bytes(invalid_input)
                input_before = case.local_input.read_bytes()
                target_before = case.target.read_bytes()

                result = case.cli("plan", expected_code=0)
                resource = _single_resource(result)

                self.assertEqual(resource["status"], "blocked")
                self.assertEqual(resource["action"], "review")
                self.assertIn("m6_format", str(resource["reason"]))
                self.assertEqual(case.local_input.read_bytes(), input_before)
                self.assertEqual(case.target.read_bytes(), target_before)
                self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

    def test_post_replace_sync_uncertainty_keeps_commit_and_comment_evidence(
        self,
    ) -> None:
        case = self._case()
        baseline_bytes = self._seed_cli(case)
        case.target.write_bytes(TARGET_LIVE_PROVIDER_CHANGED)
        target_before = case.target.read_bytes()
        plan = build_m6_plan(load_manifest(case.manifest))

        with mock.patch.object(
            m6_pilot_module,
            "sync_directory",
            side_effect=OSError("synthetic directory sync failure"),
        ):
            result = reverse_m6_inputs(plan, fields=("model_provider",), confirm=True)

        self.assertEqual(result.outcome, "committed_state_unknown")
        self.assertEqual(result.committed, ("model_provider",))
        self.assertEqual(result.attempted, ("model_provider",))
        self.assertEqual(result.uncertain, ("model_provider",))
        self.assertEqual(result.verification_status, "unknown")
        self.assertEqual(case.local_input.read_bytes(), LOCAL_INPUT_AFTER_REVERSE)
        self.assertEqual(case.target.read_bytes(), target_before)
        self.assertEqual(case.baseline.read_bytes(), baseline_bytes)

        observation = case.cli("plan", expected_code=0)
        self.assertEqual(_single_resource(observation)["action"], "noop")
        self.assertEqual(case.local_input.read_bytes(), LOCAL_INPUT_AFTER_REVERSE)
