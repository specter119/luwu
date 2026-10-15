# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Run an isolated, sanitized pilot CLI rehearsal and Dotter handoff simulation.

The runner copies public synthetic fixtures beneath a temporary HOME-shaped
directory. It invokes ``python -m luwu`` for the forward/reverse CLI loop and
prints only outcomes, field labels, and sanitized artifact paths. It does not
launch Pi, Codex, or Dotter and does not touch the real HOME.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from unittest import mock

import luwu.m6_pilot as m6_pilot_module
from luwu.m6_formats import patch_m6_document
from luwu.m6_pilot import apply_m6_forward, build_m6_plan
from luwu.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]
_CASES: dict[str, dict[str, object]] = {
    "pi": {
        "format": "json",
        "resource": "pi",
        "fields": ("defaultModel", "defaultProvider", "lastChangelogVersion"),
        "source_field": "defaultModel",
        "live_field": "defaultProvider",
        "target": ".pi/agent/settings.json",
        "inputs": ("synthetic-pi-model-v1", "synthetic-pi-provider-v1", "2026.10.0"),
    },
    "codex": {
        "format": "toml",
        "resource": "codex",
        "fields": ("model", "model_provider"),
        "source_field": "model",
        "live_field": "model_provider",
        "target": ".codex/config.toml",
        "inputs": ("synthetic-codex-model-v1", "synthetic-codex-provider-v1"),
    },
}


def run_rehearsal(artifact_dir: Path) -> dict[str, object]:
    """Run both real-shape CLI loops, a post-write fault, and handoff steps."""

    artifact_dir = Path(artifact_dir).expanduser().absolute()
    _assert_outside_real_home(artifact_dir)
    artifact_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    work_values: list[str] = ["public-note"]
    with tempfile.TemporaryDirectory(prefix="luwu-m6-run-", dir=artifact_dir) as raw:
        work_root = Path(raw)
        cases = [
            _run_case(work_root / name, name=name, work_values=work_values)
            for name in ("pi", "codex")
        ]
        recovery = [
            _run_uncertain_write_observation(
                work_root / name, name=name, work_values=work_values
            )
            for name in ("pi", "codex")
        ]
        handoff = _run_dotter_handoff(work_root / "dotter-handoff")

    report: dict[str, object] = {
        "schema_version": 7,
        "isolated": True,
        "provider_access": "none",
        "network_access": "none",
        "cli_execution": "python -m luwu from the active environment",
        "application_invocation": "none; application edits are synthetic file edits",
        "dotter_invocation": "none; operator steps are simulated on fixture state",
        "cases": cases,
        "recovery": recovery,
        "dotter_handoff": handoff,
        "artifacts": {
            "rehearsal": str(artifact_dir / "rehearsal.json"),
            "dotter_handoff": str(artifact_dir / "dotter-handoff.json"),
        },
    }
    _assert_metadata_only(report, work_values)
    _write_json(artifact_dir / "dotter-handoff.json", handoff)
    _write_json(artifact_dir / "rehearsal.json", report)
    return report


def _run_case(case: Path, *, name: str, work_values: list[str]) -> dict[str, object]:
    config = _CASES[name]
    fixture = ROOT / "tests" / "fixtures" / "pilot_v7" / name
    shutil.copytree(fixture, case)
    manifest_path = case / "luwu.toml"
    home = case / "home"
    target = home / str(config["target"])
    resource_name = str(config["resource"])
    fields = tuple(config["fields"])
    file_format = str(config["format"])
    source_field = str(config["source_field"])
    live_field = str(config["live_field"])
    input_values = tuple(str(value) for value in config["inputs"])
    work_values.extend(input_values)

    manifest = load_manifest(manifest_path)
    _require(manifest.roots is not None, "fixture roots must load")
    roots = manifest.roots
    normal_home_layout = (
        roots.target.path == home.resolve()
        and target.parent.is_dir()
        and roots.source.path == home.resolve() / "repo"
        and roots.inputs.path == home.resolve() / "inputs"
    )
    _require(
        normal_home_layout,
        "normal HOME must contain the target parent, repository, and inputs",
    )
    _require(
        not target.exists() and not target.is_symlink(), "fresh target starts absent"
    )

    initial = _plan(manifest_path, home)
    _require(initial["action"] == "review", "unbased fresh target requires acceptance")
    _require(initial["status"] == "unbased", "fresh target must report unbased fields")

    baseline_path = manifest.roots.inputs.path / "baselines" / f"{resource_name}.json"
    baseline_result = _accept_cli(
        manifest_path,
        home,
        resource_name=resource_name,
        fields=fields,
        value_from="desired",
        baseline_path=baseline_path,
    )

    create_preview = _cli(manifest_path, home, "apply", "--json", expected_code=2)
    _require(
        _resource(create_preview)["action"] == "create" and not target.exists(),
        "create preview must be read-only",
    )
    created = _cli(manifest_path, home, "apply", "--yes", "--json", expected_code=0)
    _require(created["outcome"] == "committed", "confirmed CLI create must commit")
    _require(
        tuple(created["fields"]) == tuple(sorted(fields)),
        "create result must name every declared field",
    )
    initial_noop = _plan(manifest_path, home)
    initial_noop_observed = initial_noop["action"] == "noop"
    _require(initial_noop_observed, "created target must reobserve as noop")

    cycles: list[dict[str, object]] = []
    for cycle in (1, 2):
        app_value = f"synthetic-{name}-app-{cycle}"
        work_values.append(app_value)
        _edit_document(target, file_format, live_field, app_value)
        changed_target = target.read_bytes()
        reverse_plan = _plan(manifest_path, home)
        _require(
            reverse_plan["action"] == "reverse_candidate"
            and reverse_plan["reverse_fields"] == [live_field],
            "application-side edit must be the live-owned reverse candidate",
        )

        input_path = manifest.resources[0].inputs[live_field]
        input_paths = set(manifest.resources[0].inputs.values())
        inputs_before = {path: path.read_bytes() for path in input_paths}
        input_before = inputs_before[input_path]
        baseline_before = baseline_path.read_bytes()
        reverse_preview = _cli(
            manifest_path,
            home,
            "reverse-sync",
            "--resource",
            resource_name,
            "--field",
            live_field,
            "--json",
            expected_code=2,
        )
        _require(
            reverse_preview["outcome"] == "confirmation_required"
            and reverse_preview["fields"] == [live_field],
            "reverse preview must identify only its selected field",
        )
        _require(
            all(path.read_bytes() == before for path, before in inputs_before.items())
            and baseline_path.read_bytes() == baseline_before
            and target.read_bytes() == changed_target,
            "reverse preview must leave inputs, baseline, and target unchanged",
        )
        reversed_result = _cli(
            manifest_path,
            home,
            "reverse-sync",
            "--resource",
            resource_name,
            "--field",
            live_field,
            "--yes",
            "--json",
            expected_code=0,
        )
        _require(
            reversed_result["outcome"] == "committed"
            and reversed_result["fields"] == [live_field],
            "confirmed CLI reverse sync must commit only the selected field",
        )
        _require(
            _read_document(input_path.read_bytes(), file_format)[live_field]
            == app_value,
            "reverse sync must update the declared input",
        )
        _require(
            input_path.read_bytes() != input_before,
            "confirmed reverse sync must change its selected input bytes",
        )
        unselected_input_bytes_preserved = _unselected_bytes_preserved(
            input_before,
            input_path.read_bytes(),
            file_format,
            live_field,
            app_value,
        )
        _require(
            unselected_input_bytes_preserved,
            "selected reverse patch must replace only its unique value token",
        )
        other_inputs_unchanged = all(
            path == input_path or path.read_bytes() == before
            for path, before in inputs_before.items()
        )
        _require(other_inputs_unchanged, "reverse sync must preserve other inputs")
        baseline_unchanged = baseline_path.read_bytes() == baseline_before
        _require(baseline_unchanged, "reverse sync must preserve the baseline")
        _accept_cli(
            manifest_path,
            home,
            resource_name=resource_name,
            fields=(live_field,),
            value_from="live",
            baseline_path=baseline_path,
        )
        _require(
            _plan(manifest_path, home)["action"] == "noop", "reverse loop converges"
        )

        source_input = manifest.resources[0].inputs[source_field]
        previous = _read_document(source_input.read_bytes(), file_format)[source_field]
        source_value = f"synthetic-{name}-source-cycle-{cycle}"
        work_values.append(source_value)
        _edit_document(source_input, file_format, source_field, source_value)
        forward_plan = _plan(manifest_path, home)
        _require(
            forward_plan["action"] == "forward"
            and forward_plan["forward_fields"] == [source_field],
            "source-only change must select only its declared source-owned field",
        )
        before_forward = target.read_bytes()
        forward_preview = _cli(manifest_path, home, "apply", "--json", expected_code=2)
        _require(
            _resource(forward_preview)["action"] == "forward"
            and target.read_bytes() == before_forward,
            "forward preview must preserve live bytes",
        )
        forward_result = _cli(
            manifest_path, home, "apply", "--yes", "--json", expected_code=0
        )
        _require(
            forward_result["outcome"] == "committed",
            "confirmed CLI forward patch must commit",
        )
        target_values = _read_document(target.read_bytes(), file_format)
        _require(
            target_values[source_field] == source_value
            and target_values[live_field] == app_value,
            "source-only redeploy must retain the live-owned application choice",
        )
        _accept_cli(
            manifest_path,
            home,
            resource_name=resource_name,
            fields=(source_field,),
            value_from="desired",
            baseline_path=baseline_path,
        )
        final_plan = _plan(manifest_path, home)
        _require(final_plan["action"] == "noop", "cycle must recalculate to noop")
        cycles.append(
            {
                "cycle": cycle,
                "application_change": "synthetic_file_edit",
                "reverse_outcome": reversed_result["outcome"],
                "reverse_fields": [live_field],
                "unselected_input_bytes_preserved": unselected_input_bytes_preserved,
                "other_inputs_unchanged": other_inputs_unchanged,
                "baseline_unchanged_during_reverse": baseline_unchanged,
                "source_change_fields": [source_field],
                "source_previous_value_changed": previous != source_value,
                "forward_outcome": forward_result["outcome"],
                "retained_live_owned_field": True,
                "final_action": final_plan["action"],
            }
        )

    final_plan = _plan(manifest_path, home)
    _require(final_plan["action"] == "noop", "both bidirectional cycles must converge")
    cli_round_trip = (
        initial["action"] == "review"
        and initial["status"] == "unbased"
        and baseline_result["outcome"] == "committed"
        and created["outcome"] == "committed"
        and initial_noop_observed
        and len(cycles) == 2
        and all(
            cycle["reverse_outcome"] == "committed"
            and cycle["forward_outcome"] == "committed"
            and cycle["final_action"] == "noop"
            for cycle in cycles
        )
        and final_plan["action"] == "noop"
    )
    _require(cli_round_trip, "real CLI steps must complete both bidirectional cycles")
    return {
        "resource": resource_name,
        "format": file_format,
        "target_relative_to_home": str(config["target"]),
        "declared_fields": list(fields),
        "normal_home_layout": normal_home_layout,
        "initial_target": "absent",
        "baseline_outcome": baseline_result["outcome"],
        "create_outcome": created["outcome"],
        "initial_noop_observed": initial_noop_observed,
        "cycles": cycles,
        "final_action": final_plan["action"],
        "cli_round_trip": cli_round_trip,
    }


def _run_uncertain_write_observation(
    case: Path, *, name: str, work_values: list[str]
) -> dict[str, object]:
    manifest_path = case / "luwu.toml"
    manifest = load_manifest(manifest_path)
    resource = manifest.resources[0]
    config = _CASES[name]
    file_format = str(config["format"])
    source_field = str(config["source_field"])
    target = manifest.roots.target.path / str(config["target"])
    source_input = resource.inputs[source_field]
    previous = _read_document(source_input.read_bytes(), file_format)[source_field]
    uncertain_value = f"synthetic-{name}-durability-probe"
    work_values.append(uncertain_value)
    _edit_document(source_input, file_format, source_field, uncertain_value)
    plan = build_m6_plan(manifest)
    _require(
        plan.resource.action == "forward"
        and plan.resource.forward_fields == (source_field,),
        "durability probe must contain one source-owned candidate",
    )
    target_before = target.read_bytes()
    preview = _cli(
        manifest_path,
        manifest.roots.target.path,
        "apply",
        "--json",
        expected_code=2,
    )
    _require(
        _resource(preview)["action"] == "forward"
        and target.read_bytes() == target_before,
        "uncertain-write confirmation preview must remain read-only",
    )
    baseline_path = manifest.roots.inputs.path / resource.baseline_name
    baseline_before = baseline_path.read_bytes()
    baseline_signature_before = _file_signature(baseline_path)
    with mock.patch.object(
        m6_pilot_module,
        "sync_directory",
        side_effect=OSError("synthetic post-replace directory sync fault"),
    ):
        result = apply_m6_forward(plan, confirm=True)

    result_dict = result.to_dict()
    _assert_metadata_only(result_dict, work_values)
    _require(
        result.outcome == "committed_state_unknown",
        "post-replace durability failure must retain the current unknown outcome",
    )
    _require(
        tuple(result.uncertain) == (resource.target_name,)
        and result.verification_status == "unknown",
        "uncertain result must identify the attempted target and unknown verification",
    )
    target_after = target.read_bytes()
    target_signature_after = _file_signature(target)
    _require(
        target_after != target_before
        and _read_document(target_after, file_format)[source_field] == uncertain_value,
        "injected fault must occur after the target replacement",
    )
    baseline_advanced = (
        baseline_path.read_bytes() != baseline_before
        or baseline_signature_before != _file_signature(baseline_path)
    )
    _require(not baseline_advanced, "uncertain write must not advance the baseline")

    fresh = _plan(manifest_path, manifest.roots.target.path)
    target_after_observation = target.read_bytes()
    target_signature_after_observation = _file_signature(target)
    baseline_advanced_after_observation = baseline_path.read_bytes() != baseline_before
    baseline_advanced_after_observation = (
        baseline_advanced_after_observation
        or _file_signature(baseline_path) != baseline_signature_before
    )
    automatic_replay = (
        target_after_observation != target_after
        or target_signature_after_observation != target_signature_after
    )
    _require(
        fresh["action"] == "noop",
        "fresh CLI observation must show current convergence without replay",
    )
    _require(
        not automatic_replay and not baseline_advanced_after_observation,
        "fresh observation must not write target or baseline",
    )
    return {
        "resource": name,
        "injected_boundary": "post_replace_directory_sync",
        "result": {
            "outcome": result.outcome,
            "operation": result.operation,
            "resource": result.resource,
            "fields": list(result.fields),
            "committed": list(result.committed),
            "attempted": list(result.attempted),
            "not_replaced": list(result.not_replaced),
            "uncertain": list(result.uncertain),
            "verification_status": result.verification_status,
        },
        "fresh_process_cli_observation": fresh["action"] == "noop",
        "read_only_plan_action": fresh["action"],
        "read_only_plan_status": fresh["status"],
        "read_only_target_unchanged": not automatic_replay,
        "read_only_baseline_unchanged": not baseline_advanced_after_observation,
        "automatic_replay": automatic_replay,
        "baseline_advanced": (baseline_advanced or baseline_advanced_after_observation),
        "previous_source_value_was_replaced": previous != uncertain_value,
    }


def _run_dotter_handoff(case: Path) -> dict[str, object]:
    fixture = ROOT / "tests" / "fixtures" / "pilot_v7" / "pi"
    shutil.copytree(fixture, case)
    manifest_path = case / "luwu.toml"
    manifest = load_manifest(manifest_path)
    resource = manifest.resources[0]
    home = manifest.roots.target.path
    source = manifest.roots.source.path / resource.source_name
    target = home / resource.target_name
    manager_state = case / "operator-state.json"
    _write_json(
        manager_state,
        {
            "dotter": {"status": "active", "declared_targets": [resource.target_name]},
            "luwu_resource_in_use": True,
        },
    )

    # A fixture symlink stands in for a conflicting manager-owned target.
    target.symlink_to(source)
    blocked = _plan(manifest_path, home)
    _require(
        blocked["action"] == "review" and blocked["status"] == "blocked",
        "fixture target link must block Luwu planning",
    )
    blocked_apply = _cli(
        manifest_path, home, "apply", "--yes", "--json", expected_code=2
    )
    _require(
        "error" in blocked_apply and target.is_symlink(),
        "confirmed Luwu apply must leave the conflicting fixture link intact",
    )

    operator_steps = ["observe_conflicting_target", "stop_dotter_fixture_manager"]
    _write_json(
        manager_state,
        {
            "dotter": {"status": "stopped", "declared_targets": [resource.target_name]},
            "luwu_resource_in_use": False,
        },
    )
    _write_json(
        manager_state,
        {
            "dotter": {"status": "stopped", "declared_targets": []},
            "luwu_resource_in_use": False,
        },
    )
    target.unlink()
    operator_steps.extend(
        ["remove_fixture_target_declaration", "remove_conflicting_fixture_link"]
    )

    baseline_path = manifest.roots.inputs.path / resource.baseline_name
    _accept_cli(
        manifest_path,
        home,
        resource_name=resource.name,
        fields=tuple(resource.fields),
        value_from="desired",
        baseline_path=baseline_path,
    )
    transferred = _cli(manifest_path, home, "apply", "--yes", "--json", expected_code=0)
    _require(
        transferred["outcome"] == "committed" and target.is_file(),
        "Luwu must create the isolated handoff target after explicit acceptance",
    )
    _write_json(
        manager_state,
        {
            "dotter": {"status": "stopped", "declared_targets": []},
            "luwu_resource_in_use": True,
        },
    )
    operator_steps.append("accept_baseline_and_create_with_luwu_cli")

    # Return to Dotter is performed against fixture state: stop using this Luwu
    # resource, restore a synthetic declaration, then restore a manager link.
    _write_json(
        manager_state,
        {
            "dotter": {"status": "stopped", "declared_targets": []},
            "luwu_resource_in_use": False,
        },
    )
    operator_steps.append("stop_using_the_luwu_resource")
    _write_json(
        manager_state,
        {
            "dotter": {"status": "active", "declared_targets": [resource.target_name]},
            "luwu_resource_in_use": False,
        },
    )
    target.unlink()
    target.symlink_to(source)
    operator_steps.append("restore_dotter_fixture_declaration_and_target_link")
    restored_state = json.loads(manager_state.read_text(encoding="utf-8"))
    _require(
        restored_state["dotter"]["status"] == "active"
        and restored_state["dotter"]["declared_targets"] == [resource.target_name]
        and not restored_state["luwu_resource_in_use"]
        and target.is_symlink()
        and target.resolve() == source.resolve(),
        "return workflow must actually restore fixture manager state and target link",
    )
    returned_plan = _plan(manifest_path, home)
    _require(
        returned_plan["status"] == "blocked" and returned_plan["action"] == "review",
        "returned fixture link must still be protected from Luwu replacement",
    )
    operator_steps.append("verify_returned_fixture_is_blocked_to_luwu")
    return {
        "simulation": "synthetic_operator_and_target_fixture_only",
        "target_relative_to_home": resource.target_name,
        "conflicting_link": "blocked",
        "handoff_to_luwu_outcome": transferred["outcome"],
        "luwu_managed_after_handoff": True,
        "return_to_dotter": {
            "performed": True,
            "fixture_declaration_restored": True,
            "fixture_target_link_restored": True,
            "dotter_manager_status": "active",
            "luwu_resource_in_use": False,
            "luwu_reobservation": "blocked",
            "steps": operator_steps,
        },
        "real_dotter_called": False,
        "real_application_invoked": False,
        "real_home_touched": False,
    }


def _accept_cli(
    manifest: Path,
    home: Path,
    *,
    resource_name: str,
    fields: tuple[str, ...],
    value_from: str,
    baseline_path: Path,
) -> dict[str, object]:
    before = baseline_path.read_bytes() if baseline_path.exists() else None
    args = [
        "accept",
        "--manifest",
        str(manifest),
        "--resource",
        resource_name,
        "--from",
        value_from,
    ]
    for field in fields:
        args.extend(("--field", field))
    preview = _cli(manifest, home, *args, "--json", expected_code=2)
    _require(
        preview["outcome"] == "confirmation_required"
        and (baseline_path.read_bytes() if baseline_path.exists() else None) == before,
        "baseline acceptance preview must be read-only",
    )
    confirmed = _cli(manifest, home, *args, "--yes", "--json", expected_code=0)
    _require(confirmed["outcome"] == "committed", "confirmed baseline acceptance")
    return confirmed


def _plan(manifest: Path, home: Path) -> dict[str, object]:
    result = _cli(
        manifest,
        home,
        "plan",
        "--manifest",
        str(manifest),
        "--json",
        expected_code=0,
    )
    return _resource(result)


def _resource(result: dict[str, object]) -> dict[str, object]:
    resources = result.get("resources")
    _require(
        isinstance(resources, list) and len(resources) == 1, "one-resource fixture"
    )
    return resources[0]


def _cli(
    manifest: Path,
    home: Path,
    *arguments: str,
    expected_code: int,
) -> dict[str, object]:
    cli_arguments = list(arguments)
    if "--manifest" not in cli_arguments:
        cli_arguments.extend(("--manifest", str(manifest)))
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    if "PYTHONPATH" in environment:
        caller_cwd = Path.cwd()
        entries: list[str] = []
        for entry in environment["PYTHONPATH"].split(os.pathsep):
            if not entry:
                entries.append(str(caller_cwd))
            elif Path(entry).is_absolute():
                entries.append(entry)
            else:
                entries.append(str((caller_cwd / entry).resolve()))
        environment["PYTHONPATH"] = os.pathsep.join(entries)
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "luwu", *cli_arguments],
        cwd=home,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _require(
        completed.returncode == expected_code,
        f"CLI {cli_arguments[0]} returned {completed.returncode}, expected {expected_code}",
    )
    _require(not completed.stderr, "CLI JSON command must not emit stderr")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise AssertionError("CLI command must emit one JSON document") from None
    _require(isinstance(payload, dict), "CLI JSON result must be an object")
    return payload


def _edit_document(path: Path, file_format: str, field: str, value: object) -> None:
    raw = path.read_bytes()
    path.write_bytes(patch_m6_document(raw, format=file_format, updates={field: value}))


def _read_document(raw: bytes, file_format: str) -> dict[str, object]:
    if file_format == "json":
        value = json.loads(raw.decode("utf-8"))
    else:
        value = tomllib.loads(raw.decode("utf-8"))
    _require(isinstance(value, dict), "fixture document must be an object")
    return value


def _unselected_bytes_preserved(
    before: bytes, after: bytes, file_format: str, field: str, value: object
) -> bool:
    document = _read_document(before, file_format)
    old_value = document.get(field)
    if not isinstance(old_value, str) or not isinstance(value, str):
        return False
    old_token = _serialized_string_token(old_value, file_format)
    new_token = _serialized_string_token(value, file_format)
    if old_token is None or new_token is None or before.count(old_token) != 1:
        return False
    expected = before.replace(old_token, new_token, 1)
    return after == expected


def _serialized_string_token(value: str, file_format: str) -> bytes | None:
    if file_format == "json":
        return json.dumps(value, ensure_ascii=False).encode("utf-8")
    if (
        file_format == "toml"
        and value.isascii()
        and all(character.isalnum() or character in "._-" for character in value)
    ):
        return f'"{value}"'.encode("ascii")
    return None


def _assert_metadata_only(value: object, forbidden_values: list[str]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for forbidden in forbidden_values:
        _require(
            forbidden not in encoded, "report must not contain configuration values"
        )


def _assert_outside_real_home(path: Path) -> None:
    resolved = path.resolve(strict=False)
    real_home = Path.home().resolve()
    _require(
        not resolved.is_relative_to(real_home),
        "rehearsal artifacts must stay outside the real HOME",
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="",
    )


def _file_signature(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _require(condition: bool, check: str) -> None:
    if not condition:
        raise AssertionError(f"pilot rehearsal check failed: {check}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="new directory outside the real HOME for sanitized evidence files",
    )
    args = parser.parse_args(argv)
    artifact_dir = args.artifact_dir
    if artifact_dir is None:
        artifact_dir = (
            Path(tempfile.gettempdir()) / f"luwu-m6-pilot-artifacts-{uuid.uuid4().hex}"
        )
    report = run_rehearsal(artifact_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
