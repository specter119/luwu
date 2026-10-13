# /// script
# requires-python = ">=3.12"
# dependencies = ["Jinja2>=3.1,<4"]
# ///
"""Run the isolated Pi and Codex shaped M6 version-7 pilot rehearsal.

The rehearsal creates all roots and files below a temporary directory. It uses
sanitized public values, proves preview/confirmation boundaries, and prints
only metadata about the two closed loops.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from luwu.m6_pilot import (
    accept_m6_baseline,
    apply_m6_forward,
    build_m6_plan,
    reverse_m6_inputs,
)
from luwu.manifest import load_manifest


def _run_case(root: Path, *, name: str, fields: tuple[str, ...]) -> dict[str, object]:
    case = root / name
    for relative in (
        "repo",
        "inputs/global",
        "inputs/local",
        "inputs/baselines",
        "home",
    ):
        (case / relative).mkdir(parents=True)

    input_names = {
        field: f"{'global' if field == 'model' else 'local'}/{field}.json"
        for field in fields
    }
    source_fields = [
        f'"{field}":"{{{{ {"global" if input_names[field].startswith("global/") else "local"}_inputs.{field} }}}}"'
        for field in fields
    ]
    desired = {field: f"{field}-old" for field in fields}
    source = "{" + ",".join(source_fields) + "}\n"
    (case / f"repo/{name}.json.j2").write_text(source, encoding="utf-8")
    for field, relative in input_names.items():
        path = case / "inputs" / relative
        path.write_text(json.dumps({field: desired[field]}) + "\n", encoding="utf-8")
    (case / f"home/{name}.json").write_text(
        json.dumps(desired) + "\n", encoding="utf-8"
    )
    manifest_text = [
        "version = 7",
        "",
        "[roots]",
        'source = "repo"',
        'inputs = "inputs"',
        'target = "home"',
        "",
        f"[resources.{name}]",
        'kind = "template"',
        f'source = "{name}.json.j2"',
        f'target = "{name}.json"',
        'owner = "fields"',
        'scope = "fields"',
        'comparison = "json"',
        'content_sensitivity = "public"',
        f'baseline = "baselines/{name}.json"',
        "inputs = { "
        + ", ".join(
            f'{field} = "{relative}"' for field, relative in input_names.items()
        )
        + " }",
        "",
        f"[resources.{name}.fields]",
    ]
    for field in fields:
        owner = "live" if field == "provider" else "source"
        manifest_text.append(f'{field} = "{owner}"')
    manifest_path = case / "luwu.toml"
    manifest_path.write_text("\n".join(manifest_text) + "\n", encoding="utf-8")

    manifest = load_manifest(manifest_path)
    accepted = accept_m6_baseline(
        manifest,
        resource_name=name,
        fields=fields,
        value_from="desired",
        confirm=True,
    )
    assert accepted.outcome == "committed"

    changed_field = fields[0]
    changed_input = case / "inputs" / input_names[changed_field]
    changed_input.write_text(
        json.dumps({changed_field: f"{changed_field}-new"}) + "\n",
        encoding="utf-8",
    )
    forward_plan = build_m6_plan(load_manifest(manifest_path))
    assert forward_plan.resource.action == "forward"
    forward_preview = apply_m6_forward(forward_plan, confirm=False)
    assert forward_preview.outcome == "confirmation_required"
    forward_result = apply_m6_forward(forward_plan, confirm=True)
    assert forward_result.outcome == "committed"

    target_path = case / f"home/{name}.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    assert target[changed_field] == f"{changed_field}-new"
    if "provider" in fields:
        target["provider"] = "provider-live-change"
        target_path.write_text(json.dumps(target) + "\n", encoding="utf-8")
        reverse_plan = build_m6_plan(load_manifest(manifest_path))
        assert reverse_plan.resource.action == "reverse_candidate"
        reverse_preview = reverse_m6_inputs(
            reverse_plan, fields=("provider",), confirm=False
        )
        assert reverse_preview.outcome == "confirmation_required"
        reverse_result = reverse_m6_inputs(
            reverse_plan, fields=("provider",), confirm=True
        )
        assert reverse_result.outcome == "committed"
    else:
        reverse_result = None

    final_plan = build_m6_plan(load_manifest(manifest_path))
    assert final_plan.resource.action == "noop"
    return {
        "case": name,
        "fields": list(fields),
        "forward": forward_result.outcome,
        "reverse": None if reverse_result is None else reverse_result.outcome,
        "final_action": final_plan.resource.action,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="luwu-m6-pilot-") as directory:
        root = Path(directory)
        results = [
            _run_case(
                root,
                name="pi",
                fields=("model", "provider", "changelog"),
            ),
            _run_case(root, name="codex", fields=("model", "provider")),
        ]
    print(json.dumps({"schema_version": 7, "cases": results}, indent=2))


if __name__ == "__main__":
    main()
