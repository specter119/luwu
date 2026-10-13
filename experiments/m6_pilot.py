# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Run a sanitized, isolated M6 bidirectional and Dotter handoff rehearsal.

All configuration fixtures are public synthetic values under a temporary
HOME. The printed report contains outcomes, declared field labels, and paths
to sanitized evidence files; it never prints fixture values or file contents.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

import luwu.m6_pilot as m6_pilot_module
from luwu.errors import LuwuError
from luwu.m6_pilot import (
    accept_m6_baseline,
    apply_m6_forward,
    build_m6_plan,
    reverse_m6_inputs,
)
from luwu.manifest import load_manifest

_PI_FIELDS = ("model", "provider", "changelog")
_CODEX_FIELDS = ("model", "provider")
_CHILD_PLAN = r"""
import json
import sys
from pathlib import Path
from luwu.m6_pilot import build_m6_plan
from luwu.manifest import load_manifest

plan = build_m6_plan(load_manifest(Path(sys.argv[1])))
item = plan.resource
print(json.dumps({
    "action": item.action,
    "status": item.status,
    "forward_fields": list(item.forward_fields),
    "reverse_fields": list(item.reverse_fields),
}, sort_keys=True))
"""


def run_rehearsal(artifact_dir: Path) -> dict[str, object]:
    """Run both format loops, uncertainty observation, and isolated handoff."""

    artifact_dir = Path(artifact_dir).expanduser().absolute()
    _assert_outside_real_home(artifact_dir)
    artifact_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    work_values = ["public-note"]
    with tempfile.TemporaryDirectory(
        prefix="luwu-m6-run-", dir=artifact_dir
    ) as temporary:
        work_root = Path(temporary)
        case_results = [
            _run_case(
                work_root,
                name="pi",
                file_format="json",
                fields=_PI_FIELDS,
                work_values=work_values,
            ),
            _run_case(
                work_root,
                name="codex",
                file_format="toml",
                fields=_CODEX_FIELDS,
                work_values=work_values,
            ),
        ]
        recovery = _run_uncertain_write_observation(
            work_root / "pi", work_values=work_values
        )
        handoff = _run_dotter_handoff(
            work_root / "dotter-handoff", work_values=work_values
        )

    report: dict[str, object] = {
        "schema_version": 7,
        "isolated": True,
        "provider_access": "none",
        "network_access": "none",
        "cases": case_results,
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


def _run_case(
    work_root: Path,
    *,
    name: str,
    file_format: str,
    fields: tuple[str, ...],
    work_values: list[str],
) -> dict[str, object]:
    case = work_root / name
    home = case / "home"
    repo = home / "repo"
    inputs = home / "inputs"
    for directory in (
        repo,
        inputs / "global",
        inputs / "local",
        inputs / "baselines",
    ):
        directory.mkdir(parents=True)

    extension = file_format
    input_names = {
        field: f"{'global' if field == 'model' else 'local'}/{field}.{extension}"
        for field in fields
    }
    values = {
        "model": f"{name}-model-v1",
        "provider": f"{name}-provider-v1",
        "changelog": f"{name}-changelog-v1",
    }
    work_values.extend(values[field] for field in fields)
    source_path = repo / f"{name}.{extension}.j2"
    source_path.write_text(
        _template_text(file_format, fields), encoding="utf-8", newline=""
    )

    input_paths: dict[str, Path] = {}
    for field in fields:
        relative = input_names[field]
        input_path = inputs / relative
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_bytes(_input_bytes(file_format, field, values[field]))
        input_paths[field] = input_path

    manifest_path = case / "luwu.toml"
    _write_manifest(
        manifest_path,
        home=home,
        resource_name=name,
        file_format=file_format,
        source_name=source_path.name,
        target_name=f"{name}.{extension}",
        baseline_name=f"baselines/{name}.{extension}",
        input_names=input_names,
        fields=fields,
    )
    manifest = load_manifest(manifest_path)
    assert manifest.roots is not None
    target_path = home / f"{name}.{extension}"
    _require(not target_path.exists(), "initial target must be absent")
    _require(manifest.roots.target.path == home, "target root must be isolated HOME")
    _require(
        manifest.roots.source.path.is_relative_to(home)
        and manifest.roots.inputs.path.is_relative_to(home),
        "source and inputs must be inside isolated HOME",
    )

    baseline_path = inputs / f"baselines/{name}.{extension}"
    accepted = _accept_fields(
        manifest,
        resource_name=name,
        fields=tuple(sorted(fields)),
        value_from="desired",
        baseline_path=baseline_path,
    )
    _require(accepted == "committed", "complete public baseline must be accepted")

    create_plan = build_m6_plan(load_manifest(manifest_path))
    _require(create_plan.resource.action == "create", "absent target must plan create")
    create_preview = apply_m6_forward(create_plan, confirm=False)
    _require(
        create_preview.outcome == "confirmation_required" and not target_path.exists(),
        "create preview must be read-only",
    )
    created = apply_m6_forward(create_plan, confirm=True)
    _require(created.outcome == "committed", "confirmed create must verify")
    _require(target_path.is_file() and not target_path.is_symlink(), "create target")
    _require(
        tuple(created.fields) == tuple(sorted(fields)),
        "create must identify every declared field",
    )
    _require(
        build_m6_plan(load_manifest(manifest_path)).resource.action == "noop",
        "created target must observe as noop",
    )

    cycle_results: list[dict[str, object]] = []
    for cycle in (1, 2):
        app_value = f"{name}-provider-app-{cycle}"
        work_values.append(app_value)
        _simulate_application_change(target_path, file_format, "provider", app_value)
        changed_target = target_path.read_bytes()
        reverse_plan = build_m6_plan(load_manifest(manifest_path))
        _require(
            reverse_plan.resource.action == "reverse_candidate"
            and reverse_plan.resource.reverse_fields == ("provider",),
            "application edit must select only live-owned provider",
        )
        provider_path = input_paths["provider"]
        provider_before = provider_path.read_bytes()
        provider_expected = _replace_input_value(
            provider_before,
            file_format,
            "provider",
            values["provider"] if cycle == 1 else f"{name}-provider-app-1",
            app_value,
        )
        reverse_preview = reverse_m6_inputs(
            reverse_plan, fields=("provider",), confirm=False
        )
        _require(
            reverse_preview.outcome == "confirmation_required"
            and tuple(reverse_preview.fields) == ("provider",),
            "reverse preview must identify only provider",
        )
        _require(
            provider_path.read_bytes() == provider_before
            and target_path.read_bytes() == changed_target,
            "reverse preview must not change files",
        )
        reversed_result = reverse_m6_inputs(
            reverse_plan, fields=("provider",), confirm=True
        )
        _require(
            reversed_result.outcome == "committed"
            and tuple(reversed_result.fields) == ("provider",),
            "confirmed reverse must commit only provider",
        )
        provider_after = provider_path.read_bytes()
        _require(
            provider_after == provider_expected,
            "reverse patch must preserve all unselected bytes",
        )
        _require(
            _read_input_value(provider_after, file_format, "provider") == app_value,
            "reverse patch must write the selected public value",
        )

        baseline_outcome = _accept_fields(
            load_manifest(manifest_path),
            resource_name=name,
            fields=("provider",),
            value_from="live",
            baseline_path=baseline_path,
        )
        _require(
            baseline_outcome == "committed",
            "provider baseline advancement must be explicit",
        )
        _require(
            build_m6_plan(load_manifest(manifest_path)).resource.action == "noop",
            "reverse and explicit acceptance must recompute to noop",
        )

        model_input = input_paths["model"]
        model_previous = _read_input_value(
            model_input.read_bytes(), file_format, "model"
        )
        model_next = f"{name}-model-source-cycle-{cycle}"
        work_values.append(model_next)
        model_before = model_input.read_bytes()
        model_expected = _replace_input_value(
            model_before, file_format, "model", model_previous, model_next
        )
        model_input.write_bytes(model_expected)
        forward_plan = build_m6_plan(load_manifest(manifest_path))
        _require(
            forward_plan.resource.action == "forward"
            and forward_plan.resource.forward_fields == ("model",),
            "source edit must select only model",
        )
        target_before_forward = target_path.read_bytes()
        forward_preview = apply_m6_forward(forward_plan, confirm=False)
        _require(
            forward_preview.outcome == "confirmation_required"
            and target_path.read_bytes() == target_before_forward,
            "forward preview must be read-only",
        )
        forward_result = apply_m6_forward(forward_plan, confirm=True)
        _require(forward_result.outcome == "committed", "forward redeploy must verify")
        target_values = _read_document(target_path.read_bytes(), file_format)
        _require(
            target_values.get("model") == model_next
            and target_values.get("provider") == app_value,
            "source redeploy must retain the reverse-synced provider",
        )
        model_baseline = _accept_fields(
            load_manifest(manifest_path),
            resource_name=name,
            fields=("model",),
            value_from="desired",
            baseline_path=baseline_path,
        )
        _require(
            model_baseline == "committed",
            "source baseline advancement must be explicit",
        )
        _require(
            build_m6_plan(load_manifest(manifest_path)).resource.action == "noop",
            "source redeploy and explicit acceptance must recompute to noop",
        )
        cycle_results.append(
            {
                "cycle": cycle,
                "application_change": "simulated",
                "reverse_outcome": reversed_result.outcome,
                "reverse_fields": list(reversed_result.fields),
                "unselected_input_bytes_preserved": True,
                "source_change_fields": ["model"],
                "forward_outcome": forward_result.outcome,
                "retained_live_owned_provider": True,
                "final_action": "noop",
            }
        )

    final_action = build_m6_plan(load_manifest(manifest_path)).resource.action
    _require(final_action == "noop", "both bidirectional cycles must converge")
    return {
        "resource": name,
        "format": file_format,
        "declared_fields": list(fields),
        "normal_home_layout": True,
        "initial_target": "absent",
        "baseline_outcome": accepted,
        "create_outcome": created.outcome,
        "create_fields": list(created.fields),
        "initial_noop_observed": True,
        "cycles": cycle_results,
        "final_action": final_action,
    }


def _run_uncertain_write_observation(
    case: Path, *, work_values: list[str]
) -> dict[str, object]:
    manifest_path = case / "luwu.toml"
    target_path = case / "home/pi.json"
    input_path = case / "home/inputs/global/model.json"
    baseline_path = case / "home/inputs/baselines/pi.json"
    previous_model = _read_input_value(input_path.read_bytes(), "json", "model")
    uncertain_model = "pi-model-durability-probe"
    work_values.append(uncertain_model)
    input_path.write_bytes(
        _replace_input_value(
            input_path.read_bytes(), "json", "model", previous_model, uncertain_model
        )
    )
    plan = build_m6_plan(load_manifest(manifest_path))
    _require(
        plan.resource.action == "forward"
        and plan.resource.forward_fields == ("model",),
        "durability probe must have one source-owned candidate",
    )
    preview = apply_m6_forward(plan, confirm=False)
    _require(preview.outcome == "confirmation_required", "fault preview")
    baseline_before = baseline_path.read_bytes()
    target_before = target_path.read_bytes()
    uncertain_result: dict[str, object]
    try:
        with mock.patch.object(
            m6_pilot_module,
            "sync_directory",
            side_effect=OSError("synthetic post-replace durability fault"),
        ):
            result = apply_m6_forward(plan, confirm=True)
    except LuwuError as error:
        code = getattr(error, "code", "")
        _require(
            "unknown" in str(code).casefold() or "uncertain" in str(code).casefold(),
            "post-replace failure must report uncertainty",
        )
        uncertain_result = {
            "outcome": "uncertain",
            "operation": "m6-apply",
            "resource": "pi",
            "fields": ["model"],
            "reason_code": str(code),
        }
    else:
        raw_result = result.to_dict()
        _assert_metadata_only(raw_result, work_values)
        raw_outcome = str(raw_result.get("outcome", ""))
        _require(
            "unknown" in raw_outcome.casefold()
            or "uncertain" in raw_outcome.casefold(),
            "post-replace result must report uncertainty",
        )
        uncertain_result = {
            "outcome": "uncertain",
            "operation": "m6-apply",
            "resource": "pi",
            "fields": ["model"],
            "reason_code": raw_outcome,
        }
    _assert_metadata_only(uncertain_result, work_values)
    target_after = target_path.read_bytes()
    _require(
        target_after != target_before
        and _read_document(target_after, "json").get("model") == uncertain_model,
        "injected failure must occur after replacement",
    )
    _require(
        baseline_path.read_bytes() == baseline_before,
        "uncertain deployment must not advance baseline",
    )

    child = _read_only_child_plan(manifest_path, isolated_home=case / "home")
    _require(
        child["action"] == "noop",
        "fresh read-only observation must not replay or require a new write",
    )
    _require(
        target_path.read_bytes() == target_after
        and baseline_path.read_bytes() == baseline_before,
        "fresh observation must not mutate target or baseline",
    )
    return {
        "injected_boundary": "post_replace_directory_sync",
        "result": uncertain_result,
        "fresh_process": True,
        "read_only_plan_action": child["action"],
        "read_only_plan_status": child["status"],
        "automatic_replay": False,
        "baseline_advanced": False,
    }


def _run_dotter_handoff(case: Path, *, work_values: list[str]) -> dict[str, object]:
    home = case / "home"
    repo = home / "repo"
    inputs = home / "inputs"
    (repo).mkdir(parents=True)
    (inputs / "global").mkdir(parents=True)
    (inputs / "local").mkdir(parents=True)
    (inputs / "baselines").mkdir(parents=True)
    field_value = "handoff-public-setting"
    work_values.append(field_value)
    source = repo / "handoff.json.j2"
    source.write_text('{"setting":"{{ global_inputs.setting }}"}\n', encoding="utf-8")
    (inputs / "global/setting.json").write_text(
        '{"setting": "handoff-public-setting"}\n', encoding="utf-8"
    )
    target = home / "handoff.json"
    target.symlink_to(source)
    manifest_path = case / "luwu.toml"
    _write_manifest(
        manifest_path,
        home=home,
        resource_name="handoff",
        file_format="json",
        source_name=source.name,
        target_name=target.name,
        baseline_name="baselines/handoff.json",
        input_names={"setting": "global/setting.json"},
        fields=("setting",),
        owners={"setting": "source"},
    )
    manager_state = case / "dotter-state.json"
    _write_json(
        manager_state,
        {
            "manager": "Dotter",
            "status": "active",
            "declared_targets": ["handoff.json"],
        },
    )
    conflict_plan = build_m6_plan(load_manifest(manifest_path))
    _require(
        conflict_plan.resource.action == "review"
        and conflict_plan.resource.status == "blocked",
        "conflicting Dotter link must block the Luwu plan",
    )
    blocked_apply = False
    try:
        result = apply_m6_forward(conflict_plan, confirm=True)
    except LuwuError:
        blocked_apply = True
    else:
        blocked_apply = result.outcome in {
            "not_attempted",
            "blocked",
            "review_required",
        }
    _require(
        blocked_apply and target.is_symlink(), "conflicting link must not be replaced"
    )

    # These state edits model the operator's isolated handoff workflow only.
    _write_json(
        manager_state,
        {
            "manager": "Dotter",
            "status": "stopped",
            "declared_targets": ["handoff.json"],
        },
    )
    _require(
        json.loads(manager_state.read_text(encoding="utf-8"))["status"] == "stopped",
        "competing manager must be stopped before transfer",
    )
    _write_json(
        manager_state,
        {
            "manager": "Dotter",
            "status": "stopped",
            "declared_targets": [],
        },
    )
    stopped_state = json.loads(manager_state.read_text(encoding="utf-8"))
    _require(
        stopped_state["status"] == "stopped"
        and stopped_state["declared_targets"] == [],
        "Dotter declaration must be removed after its manager stops",
    )
    target.unlink()
    _require(
        not target.exists() and not target.is_symlink(),
        "isolated conflict link removed",
    )

    manifest = load_manifest(manifest_path)
    accepted = accept_m6_baseline(
        manifest,
        resource_name="handoff",
        fields=("setting",),
        value_from="desired",
        confirm=True,
    )
    _require(accepted.outcome == "committed", "handoff baseline acceptance")
    create_plan = build_m6_plan(load_manifest(manifest_path))
    _require(create_plan.resource.action == "create", "handoff target should be absent")
    transferred = apply_m6_forward(create_plan, confirm=True)
    _require(transferred.outcome == "committed", "explicit isolated transfer to Luwu")

    return {
        "simulation": "isolated_fixture_only",
        "target_label": "handoff.json",
        "conflicting_link": "blocked",
        "competing_manager": "stopped_before_transfer",
        "dotter_declaration": "removed_in_fixture",
        "transfer": "explicit_dotter_to_luwu",
        "transfer_steps": [
            "detect_conflicting_dotter_link_and_block_luwu",
            "stop_dotter_manager",
            "remove_target_from_dotter_declaration",
            "manually_remove_conflicting_link_in_isolated_fixture",
            "confirm_luwu_baseline_and_create_target",
        ],
        "luwu_create_outcome": transferred.outcome,
        "real_home_touched": False,
        "real_dotter_configuration_touched": False,
        "manual_return": {
            "performed": False,
            "steps": [
                "stop_using_the_luwu_resource",
                "restore_the_dotter_target_declaration_through_the_normal_dotter_workflow",
                "verify_the_target_manager_before_the_next_deployment",
            ],
        },
    }


def _accept_fields(
    manifest: object,
    *,
    resource_name: str,
    fields: tuple[str, ...],
    value_from: str,
    baseline_path: Path,
) -> str:
    before = baseline_path.read_bytes() if baseline_path.exists() else None
    preview = accept_m6_baseline(
        manifest,
        resource_name=resource_name,
        fields=fields,
        value_from=value_from,
        confirm=False,
    )
    _require(
        preview.outcome == "confirmation_required"
        and (baseline_path.read_bytes() if baseline_path.exists() else None) == before,
        "baseline preview must be read-only",
    )
    accepted = accept_m6_baseline(
        manifest,
        resource_name=resource_name,
        fields=fields,
        value_from=value_from,
        confirm=True,
    )
    return accepted.outcome


def _write_manifest(
    path: Path,
    *,
    home: Path,
    resource_name: str,
    file_format: str,
    source_name: str,
    target_name: str,
    baseline_name: str,
    input_names: dict[str, str],
    fields: tuple[str, ...],
    owners: dict[str, str] | None = None,
) -> None:
    owners = owners or {
        field: ("live" if field == "provider" else "source") for field in fields
    }
    manifest_root = path.parent
    home_value = json.dumps(str(home))
    lines = [
        "version = 7",
        "",
        "[roots]",
        'source = "home/repo"',
        'inputs = "home/inputs"',
        f"target = {home_value}",
        "",
        f"[resources.{resource_name}]",
        'kind = "template"',
        f"source = {json.dumps(source_name)}",
        f"target = {json.dumps(target_name)}",
        'owner = "fields"',
        'scope = "fields"',
        f"comparison = {json.dumps(file_format)}",
        'content_sensitivity = "public"',
        f"baseline = {json.dumps(baseline_name)}",
        "inputs = { "
        + ", ".join(
            f"{field} = {json.dumps(relative)}"
            for field, relative in input_names.items()
        )
        + " }",
        "",
        f"[resources.{resource_name}.fields]",
    ]
    lines.extend(f"{field} = {json.dumps(owners[field])}" for field in fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    _require(manifest_root != home, "manifest must stay outside isolated target root")


def _template_text(file_format: str, fields: tuple[str, ...]) -> str:
    lines: list[str] = []
    for field in fields:
        namespace = "global" if field == "model" else "local"
        expression = f"{{{{ {namespace}_inputs.{field} }}}}"
        if file_format == "json":
            lines.append(f"  {json.dumps(field)}: {json.dumps(expression)}")
        else:
            lines.append(f"{field} = {json.dumps(expression)}")
    if file_format == "json":
        return "{\n" + ",\n".join(lines) + "\n}\n"
    return "\n".join(lines) + "\n"


def _input_bytes(file_format: str, field: str, value: str) -> bytes:
    if file_format == "json":
        return (
            "{ "
            + json.dumps(field)
            + " : "
            + json.dumps(value)
            + ', "retained" : "public-note" }\n'
        ).encode("utf-8")
    return (
        "# retained fixture comment\n"
        + f"{field} = {json.dumps(value)}\n"
        + 'retained = "public-note"\n'
    ).encode("utf-8")


def _replace_input_value(
    source: bytes, file_format: str, field: str, old_value: str, new_value: str
) -> bytes:
    if file_format == "json":
        old = (json.dumps(field) + " : " + json.dumps(old_value)).encode("utf-8")
        new = (json.dumps(field) + " : " + json.dumps(new_value)).encode("utf-8")
    else:
        old = (field + " = " + json.dumps(old_value)).encode("utf-8")
        new = (field + " = " + json.dumps(new_value)).encode("utf-8")
    _require(source.count(old) == 1, "selected input token must be unique")
    return source.replace(old, new, 1)


def _simulate_application_change(
    target: Path, file_format: str, field: str, value: str
) -> None:
    """Simulate an application edit; this is not evidence from a real app."""

    if file_format == "json":
        document = json.loads(target.read_text(encoding="utf-8"))
        document[field] = value
        target.write_text(
            json.dumps(document, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="",
        )
        return
    source = target.read_text(encoding="utf-8")
    old_lines = source.splitlines(keepends=True)
    matching = [
        index for index, line in enumerate(old_lines) if line.startswith(f"{field} = ")
    ]
    _require(len(matching) == 1, "simulated TOML field must be unique")
    index = matching[0]
    newline = "\r\n" if old_lines[index].endswith("\r\n") else "\n"
    old_lines[index] = f"{field} = {json.dumps(value)}{newline}"
    target.write_text("".join(old_lines), encoding="utf-8", newline="")


def _read_document(source: bytes, file_format: str) -> dict[str, object]:
    if file_format == "json":
        value = json.loads(source.decode("utf-8"))
    else:
        value = tomllib.loads(source.decode("utf-8"))
    _require(isinstance(value, dict), "rehearsal documents must be objects")
    return value


def _read_input_value(source: bytes, file_format: str, field: str) -> object:
    return _read_document(source, file_format)[field]


def _read_only_child_plan(
    manifest_path: Path, *, isolated_home: Path
) -> dict[str, object]:
    environment = {
        "HOME": str(isolated_home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD_PLAN, str(manifest_path)],
        cwd=isolated_home,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _require(completed.returncode == 0, "fresh-process plan must complete")
    _require(not completed.stderr, "fresh-process plan must not emit diagnostics")
    result = json.loads(completed.stdout)
    _require(
        set(result) == {"action", "status", "forward_fields", "reverse_fields"},
        "fresh-process plan output must be metadata-only",
    )
    return result


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


def _require(condition: bool, check: str) -> None:
    if not condition:
        raise AssertionError(f"M6 rehearsal check failed: {check}")


def main(argv: list[str] | None = None) -> None:
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


if __name__ == "__main__":
    main()
