"""Per-format acceptance matrix for the M6 Pi and Codex pilot fixtures."""

from __future__ import annotations

import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import TypedDict, cast
from unittest import mock

import luwu.m6_pilot as m6_pilot_module
from experiments.pilot_rehearsal import _cli, _unselected_bytes_preserved
from luwu.errors import LuwuError
from luwu.m6_formats import M6FormatError, parse_m6_document, patch_m6_document
from luwu.m6_pilot import (
    M6Plan,
    accept_m6_baseline,
    apply_m6_forward,
    build_m6_plan,
    reverse_m6_inputs,
)
from luwu.manifest import Manifest, Resource, load_manifest

ROOT = Path(__file__).resolve().parents[1]
FORMAT_CASES = (
    ("pi", "json", "defaultModel", "defaultProvider", "lastChangelogVersion"),
    ("codex", "toml", "model", "model_provider", None),
)


class _MaterializedCaseData(TypedDict):
    manifest_path: Path
    manifest: Manifest
    resource: Resource
    target: Path
    baseline: Path
    plan: M6Plan


def _single_cli_resource(result: dict[str, object]) -> dict[str, object]:
    raw_resources = result.get("resources")
    if not isinstance(raw_resources, list):
        raise TypeError("CLI result resources must be a list")
    resources: list[dict[str, object]] = []
    for raw_resource in raw_resources:
        if not isinstance(raw_resource, dict) or not all(
            isinstance(key, str) for key in raw_resource
        ):
            raise TypeError("CLI resources must be objects with string keys")
        resources.append(cast(dict[str, object], raw_resource))
    if len(resources) != 1:
        raise AssertionError("M6 acceptance fixture must have one CLI resource")
    return resources[0]


def _expected_unique_string_replacement(
    before: bytes,
    *,
    file_format: str,
    field: str,
    value: str,
) -> bytes:
    if file_format == "json":
        document = json.loads(before.decode("utf-8"))
    else:
        document = tomllib.loads(before.decode("utf-8"))
    old_value = document.get(field)
    if not isinstance(old_value, str):
        raise TypeError("fixture selected field must be a public string")
    if file_format == "json":
        old_token = json.dumps(old_value, ensure_ascii=False).encode("utf-8")
        new_token = json.dumps(value, ensure_ascii=False).encode("utf-8")
    else:
        if not _is_simple_toml_fixture_string(
            old_value
        ) or not _is_simple_toml_fixture_string(value):
            raise AssertionError("TOML byte oracle expects simple fixture strings")
        old_token = f'"{old_value}"'.encode("ascii")
        new_token = f'"{value}"'.encode("ascii")
    if before.count(old_token) != 1:
        raise AssertionError("selected value token must occur exactly once")
    return before.replace(old_token, new_token, 1)


def _is_simple_toml_fixture_string(value: str) -> bool:
    return value.isascii() and all(
        character.isalnum() or character in "._-" for character in value
    )


def _matches_unique_string_replacement(
    before: bytes,
    after: bytes,
    *,
    file_format: str,
    field: str,
    value: str,
) -> bool:
    try:
        expected = _expected_unique_string_replacement(
            before, file_format=file_format, field=field, value=value
        )
    except (
        AssertionError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        return False
    return after == expected


class M6AcceptanceTests(unittest.TestCase):
    def test_fresh_baseline_create_and_noop_for_each_format(self) -> None:
        for name, file_format, source_field, live_field, third_field in FORMAT_CASES:
            with self.subTest(resource=name), self._case(name) as case:
                fields = tuple(case["resource"].fields)
                target = case["target"]
                baseline = case["baseline"]
                self.assertFalse(target.exists())
                self.assertEqual(case["plan"].resource.action, "review")
                self.assertEqual(case["plan"].resource.status, "unbased")

                preview = accept_m6_baseline(
                    case["manifest"],
                    resource_name=name,
                    fields=fields,
                    value_from="desired",
                    confirm=False,
                )
                self.assertEqual(preview.outcome, "confirmation_required")
                self.assertFalse(baseline.exists())
                accepted = accept_m6_baseline(
                    case["manifest"],
                    resource_name=name,
                    fields=fields,
                    value_from="desired",
                    confirm=True,
                )
                self.assertEqual(accepted.outcome, "committed")

                create_plan = build_m6_plan(load_manifest(case["manifest_path"]))
                self.assertEqual(create_plan.resource.action, "create")
                preview = apply_m6_forward(create_plan, confirm=False)
                self.assertEqual(preview.outcome, "confirmation_required")
                self.assertFalse(target.exists())
                created = apply_m6_forward(create_plan, confirm=True)
                self.assertEqual(created.outcome, "committed")
                self.assertEqual(set(created.fields), set(fields))
                self.assertEqual(
                    build_m6_plan(load_manifest(case["manifest_path"])).resource.action,
                    "noop",
                )
                self.assertEqual(
                    set(parse_m6_document(target.read_bytes(), format=file_format)),
                    set(fields),
                )
                self.assertIn(source_field, fields)
                self.assertIn(live_field, fields)
                if third_field is not None:
                    self.assertIn(third_field, fields)

    def test_templates_serialize_escaped_public_strings_for_each_format(self) -> None:
        for name, file_format, source_field, _live, _third in FORMAT_CASES:
            with self.subTest(resource=name), self._case(name) as case:
                value = f'synthetic "{name}" model \\ path'
                self._edit(
                    case["resource"].inputs[source_field],
                    file_format,
                    source_field,
                    value,
                )
                self._seed(case)
                rendered = parse_m6_document(
                    case["target"].read_bytes(), format=file_format
                )
                self.assertEqual(rendered[source_field], value)

    def test_source_only_change_preserves_live_owned_value_for_each_format(
        self,
    ) -> None:
        for name, file_format, source_field, live_field, _third in FORMAT_CASES:
            with self.subTest(resource=name), self._case(name) as case:
                self._seed(case)
                original = parse_m6_document(
                    case["target"].read_bytes(), format=file_format
                )
                new_value = f"synthetic-{name}-source-only"
                self._edit(
                    case["resource"].inputs[source_field],
                    file_format,
                    source_field,
                    new_value,
                )

                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                self.assertEqual(plan.resource.action, "forward")
                self.assertEqual(plan.resource.forward_fields, (source_field,))
                target_before = case["target"].read_bytes()
                preview = apply_m6_forward(plan, confirm=False)
                self.assertEqual(preview.outcome, "confirmation_required")
                self.assertEqual(case["target"].read_bytes(), target_before)
                result = apply_m6_forward(plan, confirm=True)
                self.assertEqual(result.outcome, "committed")
                changed = parse_m6_document(
                    case["target"].read_bytes(), format=file_format
                )
                self.assertEqual(changed[source_field], new_value)
                self.assertEqual(changed[live_field], original[live_field])
                self.assertEqual(
                    build_m6_plan(load_manifest(case["manifest_path"])).resource.action,
                    "noop",
                )

    def test_source_owned_template_field_deletion_uses_cli_and_reobserves_noop(
        self,
    ) -> None:
        for name, file_format, source_field, live_field, _third in FORMAT_CASES:
            with self.subTest(resource=name), self._case(name) as case:
                self._seed(case)
                manifest = case["manifest"]
                assert manifest.roots is not None
                source_path = manifest.roots.source.path / case["resource"].source_name
                template_before = source_path.read_bytes()
                field_marker = (
                    f'"{source_field}"'.encode()
                    if file_format == "json"
                    else f"{source_field} =".encode()
                )
                template_lines = template_before.splitlines(keepends=True)
                retained_lines = [
                    line for line in template_lines if field_marker not in line
                ]
                self.assertEqual(len(template_lines) - len(retained_lines), 1)
                source_path.write_bytes(b"".join(retained_lines))

                manifest_path = case["manifest_path"]
                home = manifest.roots.target.path
                planned = _cli(
                    manifest_path,
                    home,
                    "plan",
                    "--manifest",
                    str(manifest_path),
                    "--json",
                    expected_code=0,
                )
                planned_resource = _single_cli_resource(planned)
                self.assertEqual(planned_resource["action"], "forward")
                self.assertEqual(planned_resource["forward_fields"], [source_field])

                target_before = case["target"].read_bytes()
                baseline_before = case["baseline"].read_bytes()
                preview = _cli(
                    manifest_path,
                    home,
                    "apply",
                    "--manifest",
                    str(manifest_path),
                    "--json",
                    expected_code=2,
                )
                self.assertEqual(_single_cli_resource(preview)["action"], "forward")
                self.assertEqual(case["target"].read_bytes(), target_before)

                result = _cli(
                    manifest_path,
                    home,
                    "apply",
                    "--manifest",
                    str(manifest_path),
                    "--yes",
                    "--json",
                    expected_code=0,
                )
                self.assertEqual(result["outcome"], "committed")
                self.assertEqual(result["fields"], [source_field])
                changed_target = case["target"].read_bytes()
                self.assertEqual(case["baseline"].read_bytes(), baseline_before)
                changed_values = parse_m6_document(changed_target, format=file_format)
                self.assertNotIn(source_field, changed_values)
                self.assertIn(live_field, changed_values)

                fresh = _single_cli_resource(
                    _cli(
                        manifest_path,
                        home,
                        "plan",
                        "--manifest",
                        str(manifest_path),
                        "--json",
                        expected_code=0,
                    )
                )
                self.assertEqual(fresh["action"], "noop")
                self.assertEqual(fresh["status"], "noop")
                self.assertEqual(case["target"].read_bytes(), changed_target)
                self.assertEqual(case["baseline"].read_bytes(), baseline_before)

    def test_both_side_conflict_stops_for_review_for_each_format(self) -> None:
        for name, file_format, source_field, _live, _third in FORMAT_CASES:
            with self.subTest(resource=name), self._case(name) as case:
                self._seed(case)
                self._edit(
                    case["resource"].inputs[source_field],
                    file_format,
                    source_field,
                    f"synthetic-{name}-desired-change",
                )
                self._edit(
                    case["target"],
                    file_format,
                    source_field,
                    f"synthetic-{name}-different-live-change",
                )
                before = case["target"].read_bytes()
                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                self.assertEqual(plan.resource.action, "review")
                with self.assertRaises(LuwuError):
                    apply_m6_forward(plan, confirm=True)
                self.assertEqual(case["target"].read_bytes(), before)

    def test_stale_baseline_and_stale_input_stop_reverse_sync_for_each_format(
        self,
    ) -> None:
        for name, file_format, _source, live_field, _third in FORMAT_CASES:
            with (
                self.subTest(resource=name, stale="baseline"),
                self._case(name) as case,
            ):
                self._seed(case)
                self._edit(
                    case["target"], file_format, live_field, f"synthetic-{name}-live"
                )
                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                self.assertIn(live_field, plan.resource.reverse_fields)
                baseline_bytes = case["baseline"].read_bytes()
                case["baseline"].write_bytes(baseline_bytes + b"\n")
                input_before = case["resource"].inputs[live_field].read_bytes()
                with self.assertRaises(LuwuError) as raised:
                    reverse_m6_inputs(plan, fields=(live_field,), confirm=True)
                self.assertEqual(raised.exception.code, "m6_stale_plan")
                self.assertEqual(
                    case["resource"].inputs[live_field].read_bytes(), input_before
                )

            with self.subTest(resource=name, stale="input"), self._case(name) as case:
                self._seed(case)
                self._edit(
                    case["target"], file_format, live_field, f"synthetic-{name}-live"
                )
                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                input_path = case["resource"].inputs[live_field]
                self._edit(
                    input_path,
                    file_format,
                    live_field,
                    f"synthetic-{name}-concurrent-input",
                )
                before = input_path.read_bytes()
                with self.assertRaises(LuwuError) as raised:
                    reverse_m6_inputs(plan, fields=(live_field,), confirm=True)
                self.assertEqual(raised.exception.code, "m6_stale_plan")
                self.assertEqual(input_path.read_bytes(), before)

    def test_wrong_owner_and_selected_with_unselected_conflict_for_each_format(
        self,
    ) -> None:
        for name, file_format, source_field, live_field, _third in FORMAT_CASES:
            with (
                self.subTest(resource=name, case="wrong_owner"),
                self._case(name) as case,
            ):
                self._seed(case)
                self._edit(
                    case["target"],
                    file_format,
                    source_field,
                    f"synthetic-{name}-wrong-owner",
                )
                before = case["target"].read_bytes()
                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                self.assertEqual(plan.resource.action, "review")
                self.assertNotIn(source_field, plan.resource.reverse_fields)
                with self.assertRaises(LuwuError):
                    reverse_m6_inputs(plan, fields=(source_field,), confirm=True)
                self.assertEqual(case["target"].read_bytes(), before)

            with (
                self.subTest(resource=name, case="unselected_conflict"),
                self._case(name) as case,
            ):
                self._seed(case)
                self._edit(
                    case["target"],
                    file_format,
                    live_field,
                    f"synthetic-{name}-live-choice",
                )
                self._edit(
                    case["resource"].inputs[source_field],
                    file_format,
                    source_field,
                    f"synthetic-{name}-source-choice",
                )
                self._edit(
                    case["target"],
                    file_format,
                    source_field,
                    f"synthetic-{name}-conflicting-live-choice",
                )
                input_before = case["resource"].inputs[live_field].read_bytes()
                target_before = case["target"].read_bytes()
                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                self.assertEqual(plan.resource.action, "review")
                with self.assertRaises(LuwuError):
                    reverse_m6_inputs(plan, fields=(live_field,), confirm=False)
                with self.assertRaises(LuwuError):
                    reverse_m6_inputs(plan, fields=(live_field,), confirm=True)
                self.assertEqual(
                    case["resource"].inputs[live_field].read_bytes(), input_before
                )
                self.assertEqual(case["target"].read_bytes(), target_before)

    def test_undeclared_live_key_is_reviewed_and_not_written_for_each_format(
        self,
    ) -> None:
        for name, file_format, _source, _live, _third in FORMAT_CASES:
            with self.subTest(resource=name), self._case(name) as case:
                self._seed(case)
                self._edit(
                    case["target"], file_format, "unmanagedPublicField", "keep-me"
                )
                target_before = case["target"].read_bytes()
                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                self.assertEqual(plan.resource.action, "review")
                with self.assertRaises(LuwuError):
                    apply_m6_forward(plan, confirm=True)
                self.assertEqual(case["target"].read_bytes(), target_before)

    def test_selected_patch_preserves_unselected_input_bytes_for_each_format(
        self,
    ) -> None:
        for name, file_format, _source, live_field, _third in FORMAT_CASES:
            with self.subTest(resource=name), self._case(name) as case:
                self._seed(case)
                self._edit(
                    case["target"],
                    file_format,
                    live_field,
                    f"synthetic-{name}-selected-live",
                )
                target_after_edit = case["target"].read_bytes()
                input_path = case["resource"].inputs[live_field]
                input_before = input_path.read_bytes()
                all_input_bytes = {
                    path: path.read_bytes()
                    for path in set(case["resource"].inputs.values())
                }
                baseline_before = case["baseline"].read_bytes()
                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                preview = reverse_m6_inputs(plan, fields=(live_field,), confirm=False)
                self.assertEqual(preview.outcome, "confirmation_required")
                self.assertEqual(input_path.read_bytes(), input_before)
                result = reverse_m6_inputs(plan, fields=(live_field,), confirm=True)
                self.assertEqual(result.outcome, "committed")
                expected = _expected_unique_string_replacement(
                    input_before,
                    file_format=file_format,
                    field=live_field,
                    value=f"synthetic-{name}-selected-live",
                )
                self.assertEqual(input_path.read_bytes(), expected)
                for other_path, before in all_input_bytes.items():
                    if other_path != input_path:
                        self.assertEqual(other_path.read_bytes(), before)
                if file_format == "toml":
                    self.assertIn(b"# Local public inputs.", input_path.read_bytes())
                self.assertEqual(case["baseline"].read_bytes(), baseline_before)
                self.assertEqual(case["target"].read_bytes(), target_after_edit)
                self.assertEqual(
                    parse_m6_document(case["target"].read_bytes(), format=file_format)[
                        live_field
                    ],
                    f"synthetic-{name}-selected-live",
                )

    def test_unselected_field_corruption_is_caught_by_independent_byte_oracle(
        self,
    ) -> None:
        for name, file_format, _source, live_field, _third in FORMAT_CASES:
            with self.subTest(resource=name), self._case(name) as case:
                self._seed(case)
                selected_value = f"synthetic-{name}-selected-live"
                self._edit(case["target"], file_format, live_field, selected_value)
                input_path = case["resource"].inputs[live_field]
                input_before = input_path.read_bytes()
                plan = build_m6_plan(load_manifest(case["manifest_path"]))
                production_patch = m6_pilot_module.patch_m6_document

                def corrupt_unselected(
                    raw: bytes,
                    *,
                    format: str,
                    updates: dict[str, object],
                    deletes=(),
                    original_patch=production_patch,
                    selected_field=live_field,
                    selected_format=file_format,
                ) -> bytes:
                    patched = original_patch(
                        raw, format=format, updates=updates, deletes=deletes
                    )
                    if selected_field not in updates:
                        return patched
                    if selected_format == "json":
                        original = b'"retained" : "public-note"'
                        corrupted = b'"retained" : "public-note-corrupted"'
                    else:
                        original = b'retained = "public-note"'
                        corrupted = b'retained = "public-note-corrupted"'
                    self.assertEqual(patched.count(original), 1)
                    return patched.replace(original, corrupted, 1)

                with mock.patch.object(
                    m6_pilot_module,
                    "patch_m6_document",
                    side_effect=corrupt_unselected,
                ):
                    result = reverse_m6_inputs(plan, fields=(live_field,), confirm=True)

                self.assertEqual(result.outcome, "committed")
                input_after = input_path.read_bytes()
                expected = _expected_unique_string_replacement(
                    input_before,
                    file_format=file_format,
                    field=live_field,
                    value=selected_value,
                )
                self.assertNotEqual(input_after, expected)
                self.assertFalse(
                    _matches_unique_string_replacement(
                        input_before,
                        input_after,
                        file_format=file_format,
                        field=live_field,
                        value=selected_value,
                    )
                )
                self.assertFalse(
                    _unselected_bytes_preserved(
                        input_before,
                        input_after,
                        file_format,
                        live_field,
                        selected_value,
                    )
                )
                self.assertIn(b"public-note-corrupted", input_after)

    def test_comments_and_unsupported_syntax_follow_each_format_boundary(self) -> None:
        with self.subTest(resource="pi-comments"), self._case("pi") as case:
            self._seed(case)
            target = case["target"]
            target_before = (
                target.read_bytes() + b"\n// JSON comments are unsupported\n"
            )
            target.write_bytes(target_before)
            input_before = case["resource"].inputs["defaultProvider"].read_bytes()
            plan = build_m6_plan(load_manifest(case["manifest_path"]))
            self.assertEqual(plan.resource.status, "blocked")
            with self.assertRaises(LuwuError):
                apply_m6_forward(plan, confirm=True)
            self.assertEqual(target.read_bytes(), target_before)
            self.assertEqual(
                case["resource"].inputs["defaultProvider"].read_bytes(), input_before
            )

        with (
            self.subTest(resource="codex-comments-and-syntax"),
            self._case("codex") as case,
        ):
            self._seed(case)
            target = case["target"]
            target.write_bytes(b"# retained target comment\n" + target.read_bytes())
            self._edit(
                target, "toml", "model_provider", "synthetic-codex-comment-probe"
            )
            plan = build_m6_plan(load_manifest(case["manifest_path"]))
            self.assertEqual(plan.resource.action, "reverse_candidate")
            input_path = case["resource"].inputs["model_provider"]
            input_before = input_path.read_bytes()
            expected = _expected_unique_string_replacement(
                input_before,
                file_format="toml",
                field="model_provider",
                value="synthetic-codex-comment-probe",
            )
            reversed_result = reverse_m6_inputs(
                plan, fields=("model_provider",), confirm=True
            )
            self.assertEqual(reversed_result.outcome, "committed")
            self.assertEqual(input_path.read_bytes(), expected)
            self.assertIn(b"# Local public inputs.", input_path.read_bytes())
            self.assertIn(b"# retained target comment", target.read_bytes())

            unsupported = target.read_bytes() + b"\nunsupported = { nested = true }\n"
            target.write_bytes(unsupported)
            input_after = input_path.read_bytes()
            blocked = build_m6_plan(load_manifest(case["manifest_path"]))
            self.assertEqual(blocked.resource.status, "blocked")
            with self.assertRaises(LuwuError):
                apply_m6_forward(blocked, confirm=True)
            self.assertEqual(target.read_bytes(), unsupported)
            self.assertEqual(input_path.read_bytes(), input_after)

    def test_json_null_is_distinct_from_absence_and_toml_rejects_null(self) -> None:
        with self.subTest(format="json-null-and-deletion"), self._case("pi") as case:
            self._seed(case)
            target = case["target"]
            input_path = case["resource"].inputs["defaultProvider"]
            self._edit(target, "json", "defaultProvider", None)
            null_plan = build_m6_plan(load_manifest(case["manifest_path"]))
            self.assertEqual(null_plan.resource.action, "reverse_candidate")
            null_result = reverse_m6_inputs(
                null_plan, fields=("defaultProvider",), confirm=True
            )
            self.assertEqual(null_result.outcome, "committed")
            self.assertIsNone(
                parse_m6_document(input_path.read_bytes(), format="json")[
                    "defaultProvider"
                ]
            )
            accept_m6_baseline(
                load_manifest(case["manifest_path"]),
                resource_name="pi",
                fields=("defaultProvider",),
                value_from="live",
                confirm=True,
            )
            self.assertEqual(
                build_m6_plan(load_manifest(case["manifest_path"])).resource.action,
                "noop",
            )

            document = parse_m6_document(target.read_bytes(), format="json")
            del document["defaultProvider"]
            target.write_bytes(
                json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
                + b"\n"
            )
            deleted_plan = build_m6_plan(load_manifest(case["manifest_path"]))
            self.assertIn("defaultProvider", deleted_plan.resource.reverse_fields)
            before = input_path.read_bytes()
            with self.assertRaises(LuwuError) as raised:
                reverse_m6_inputs(
                    deleted_plan, fields=("defaultProvider",), confirm=True
                )
            self.assertEqual(raised.exception.code, "m6_reverse_delete")
            self.assertEqual(input_path.read_bytes(), before)

        with self.subTest(format="toml-null-and-deletion"), self._case("codex") as case:
            self._seed(case)
            with self.assertRaises(M6FormatError):
                patch_m6_document(
                    case["resource"].inputs["model_provider"].read_bytes(),
                    format="toml",
                    updates={"model_provider": None},
                )
            target = case["target"]
            document = parse_m6_document(target.read_bytes(), format="toml")
            del document["model_provider"]
            target.write_bytes((f'model = "{document["model"]}"\n').encode())
            deleted_plan = build_m6_plan(load_manifest(case["manifest_path"]))
            self.assertIn("model_provider", deleted_plan.resource.reverse_fields)
            before = case["resource"].inputs["model_provider"].read_bytes()
            with self.assertRaises(LuwuError) as raised:
                reverse_m6_inputs(
                    deleted_plan, fields=("model_provider",), confirm=True
                )
            self.assertEqual(raised.exception.code, "m6_reverse_delete")
            self.assertEqual(
                case["resource"].inputs["model_provider"].read_bytes(), before
            )

    def _case(self, name: str) -> _MaterializedCase:
        return _MaterializedCase(name)

    def _seed(self, case: _MaterializedCaseData) -> None:
        fields = tuple(case["resource"].fields)
        accepted = accept_m6_baseline(
            case["manifest"],
            resource_name=case["resource"].name,
            fields=fields,
            value_from="desired",
            confirm=True,
        )
        self.assertEqual(accepted.outcome, "committed")
        plan = build_m6_plan(load_manifest(case["manifest_path"]))
        self.assertEqual(plan.resource.action, "create")
        created = apply_m6_forward(plan, confirm=True)
        self.assertEqual(created.outcome, "committed")

    def _edit(self, path: Path, file_format: str, field: str, value: object) -> None:
        path.write_bytes(
            patch_m6_document(
                path.read_bytes(), format=file_format, updates={field: value}
            )
        )


class _MaterializedCase:
    def __init__(self, name: str) -> None:
        self.name = name
        self.temporary = tempfile.TemporaryDirectory(prefix=f"luwu-m6-{name}-")
        root = Path(self.temporary.name)
        shutil.copytree(
            ROOT / "tests" / "fixtures" / "m6" / name, root, dirs_exist_ok=True
        )
        self.manifest_path = root / "luwu.toml"
        self.manifest = load_manifest(self.manifest_path)
        roots = self.manifest.roots
        if roots is None:
            raise AssertionError("M6 fixture must declare roots")
        self.resource = self.manifest.resources[0]
        baseline_name = self.resource.baseline_name
        if baseline_name is None:
            raise AssertionError("M6 fixture resource must declare its baseline")
        self.target = roots.target.path / self.resource.target_name
        self.baseline = roots.inputs.path / baseline_name
        self.plan = build_m6_plan(self.manifest)

    def __enter__(self) -> _MaterializedCaseData:
        return {
            "manifest_path": self.manifest_path,
            "manifest": self.manifest,
            "resource": self.resource,
            "target": self.target,
            "baseline": self.baseline,
            "plan": self.plan,
        }

    def __exit__(self, *_args: object) -> None:
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
