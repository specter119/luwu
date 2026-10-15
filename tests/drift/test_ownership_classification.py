"""Tests for ownership classification."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Self, cast

from luwu.cli import main
from luwu.errors import ApplyError, LuwuError
from luwu.manifest import load_manifest
from luwu.ownership import classify_fields
from luwu.reconcile import Action, Status, apply_plan, build_plan, plan_to_dict


def _baseline(values: dict[str, object], owners: dict[str, str] | None = None) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "resource": "resource",
            "source": "source",
            "target": "target",
            "owners": owners
            or {"source_field": "source", "live_field": "live", "ignored": "ignore"},
            "values": values,
        }
    ).encode()


def _baseline_v3(*, owners: dict[str, str], values: dict[str, object]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "resource": "settings",
            "source": "templates/settings.json.j2",
            "target": "live/settings.json",
            "owners": owners,
            "values": values,
        }
    )


class _M3Project:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "templates").mkdir()
        (self.root / "live").mkdir()
        self.source = self.root / "templates/settings.json.j2"
        self.target = self.root / "live/settings.json"
        self.baseline = self.root / "baseline.json"
        self.manifest_path = self.root / "luwu.toml"
        self.write_manifest()

    def write_source(self, content: str) -> None:
        self.source.write_text(content, encoding="utf-8")

    def write_target(self, content: str) -> None:
        self.target.write_text(content, encoding="utf-8")

    def write_baseline(self, content: str) -> None:
        self.baseline.write_text(content, encoding="utf-8")

    def write_manifest(
        self,
        *,
        baseline: str | None = "baseline.json",
        fields: dict[str, str] | None = None,
    ) -> None:
        baseline_line = f'baseline = "{baseline}"\n' if baseline is not None else ""
        selected_fields = fields or {
            "source_value": "source",
            "live_value": "live",
            "setting": "source",
            "ignored": "ignore",
        }
        fields_toml = "\n".join(
            f'{json.dumps(name)} = "{owner}"' for name, owner in selected_fields.items()
        )
        self.manifest_path.write_text(
            f"""version = 3

[resources.settings]
kind = "template"
source = "templates/settings.json.j2"
target = "live/settings.json"
comparison = "json"
owner = "fields"
scope = "fields"
content_sensitivity = "public"
{baseline_line}
[resources.settings.fields]
{fields_toml}
""",
            encoding="utf-8",
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.temporary_directory.cleanup()


class OwnershipTests(unittest.TestCase):
    def test_classifies_three_way_states_and_decisions(self) -> None:
        desired = b'{"unchanged":1,"converged":2,"source_field":3,"live_field":4,"conflict":5,"ignored":9,"extra":1}'
        live = b'{"unchanged":1,"converged":2,"source_field":1,"live_field":8,"conflict":6,"ignored":10,"extra":2}'
        baseline = _baseline(
            {
                "unchanged": 1,
                "converged": 0,
                "source_field": 1,
                "live_field": 4,
                "conflict": 1,
            },
            {
                "unchanged": "source",
                "converged": "source",
                "source_field": "source",
                "live_field": "live",
                "conflict": "merge",
                "ignored": "ignore",
            },
        )
        result = classify_fields(
            desired,
            live,
            fields={
                "unchanged": "source",
                "converged": "source",
                "source_field": "source",
                "live_field": "live",
                "conflict": "merge",
                "ignored": "ignore",
            },
            baseline=baseline,
            resource_name="resource",
            source_name="source",
            target_name="target",
        )
        by_name = {field["name"]: field for field in result.to_dict()["fields"]}
        self.assertEqual(by_name["unchanged"]["status"], "unchanged")
        self.assertEqual(by_name["converged"]["status"], "converged")
        self.assertEqual(by_name["source_field"]["decision"], "forward_candidate")
        self.assertEqual(by_name["live_field"]["decision"], "reverse_candidate")
        self.assertEqual(by_name["conflict"]["decision"], "review")
        self.assertEqual(by_name["ignored"]["status"], "ignored")
        self.assertTrue(result.undeclared_changed)

    def test_unbased_has_no_candidate_and_distinguishes_missing_from_null(self) -> None:
        result = classify_fields(
            b'{"missing":null}',
            b"{}",
            fields={"missing": "source"},
            baseline=None,
            resource_name="resource",
            source_name="source",
            target_name="target",
        )
        field = result.to_dict()["fields"][0]
        self.assertEqual(field["status"], "unbased")
        self.assertEqual(field["decision"], "none")
        self.assertEqual(result.baseline_status, "absent")

    def test_equal_undeclared_content_is_not_reported_changed(self) -> None:
        result = classify_fields(
            b'{"declared":1,"extra":2}',
            b'{"declared":1,"extra":2}',
            fields={"declared": "source"},
            baseline=_baseline({"declared": 1}, {"declared": "source"}),
            resource_name="resource",
            source_name="source",
            target_name="target",
        )

        self.assertFalse(result.undeclared_changed)

    def test_boolean_and_number_are_not_equivalent_json_values(self) -> None:
        result = classify_fields(
            b'{"declared":true}',
            b'{"declared":1}',
            fields={"declared": "source"},
            baseline=_baseline({"declared": False}, {"declared": "source"}),
            resource_name="resource",
            source_name="source",
            target_name="target",
        )

        self.assertEqual(result.fields[0].status, "conflict")

    def test_undeclared_boolean_and_number_are_not_equivalent(self) -> None:
        result = classify_fields(
            b'{"declared":1,"extra":true}',
            b'{"declared":1,"extra":1}',
            fields={"declared": "source"},
            baseline=_baseline({"declared": 1}, {"declared": "source"}),
            resource_name="resource",
            source_name="source",
            target_name="target",
        )

        self.assertTrue(result.undeclared_changed)

    def test_baseline_envelope_is_closed_and_bound_to_declaration(self) -> None:
        for baseline in (
            b'{"schema_version":1,"resource":"resource","source":"source","target":"target","owners":{"source_field":"source"},"values":{"unknown":1}}',
            b'{"schema_version":true,"resource":"resource","source":"source","target":"target","owners":{"source_field":"source"},"values":{}}',
            b'{"schema_version":1,"resource":"other","source":"source","target":"target","owners":{"source_field":"source"},"values":{}}',
        ):
            with self.assertRaises(LuwuError) as raised:
                classify_fields(
                    b'{"source_field":1}',
                    b'{"source_field":1}',
                    fields={"source_field": "source"},
                    baseline=baseline,
                    resource_name="resource",
                    source_name="source",
                    target_name="target",
                )
            self.assertEqual(str(raised.exception), "ownership input is invalid")
            self.assertNotIn("unknown", str(raised.exception))
            self.assertNotIn("other", str(raised.exception))

    def test_rejects_non_object_or_non_strict_json_without_echoing_content(
        self,
    ) -> None:
        for desired in (b"[]", b'{"secret":NaN}', b'{"secret":1,"secret":2}'):
            with self.assertRaises(LuwuError) as raised:
                classify_fields(
                    desired,
                    b"{}",
                    fields={"secret": "source"},
                    baseline=None,
                    resource_name="resource",
                    source_name="source",
                    target_name="target",
                )
            self.assertEqual(raised.exception.code, "ownership_invalid_input")
            self.assertNotIn("secret", str(raised.exception))


class OwnershipObservationV3Tests(unittest.TestCase):
    def test_v3_reports_field_ownership_without_values_or_writes(self) -> None:
        with _M3Project() as project:
            project.write_manifest(
                fields={
                    "source_value": "source",
                    "live_value": "live",
                    "setting": "source",
                    "ignored": "ignore",
                }
            )
            project.write_source(
                '{"source_value": 2, "live_value": 4, "runtime": "desired"}'
            )
            project.write_target(
                '{"source_value": 2, "live_value": 3, "runtime": "live"}'
            )
            project.write_baseline(
                _baseline_v3(
                    owners={
                        "source_value": "source",
                        "live_value": "live",
                        "setting": "source",
                        "ignored": "ignore",
                    },
                    values={"source_value": 1, "live_value": 3},
                )
            )

            plan = build_plan(load_manifest(project.manifest_path))
            observation = plan.observations[0]
            payload = cast(dict[str, Any], plan_to_dict(plan, command="plan"))

            self.assertEqual(observation.status, Status.CONFLICT)
            self.assertEqual(observation.action, Action.REPORT)
            self.assertFalse(plan.can_apply)
            self.assertEqual(plan.apply_block_reason, "m3_read_only")
            ownership = payload["resources"][0]["ownership"]
            self.assertTrue(ownership["undeclared_changed"])
            fields = {item["name"]: item for item in ownership["fields"]}
            self.assertEqual(fields["source_value"]["status"], "converged")
            self.assertEqual(fields["live_value"]["decision"], "review")
            serialized = json.dumps(payload)
            self.assertNotIn("runtime", serialized)
            self.assertEqual(
                project.target.read_text(encoding="utf-8"),
                '{"source_value": 2, "live_value": 3, "runtime": "live"}',
            )

    def test_v3_without_baseline_is_unbased_and_never_applyable(self) -> None:
        with _M3Project() as project:
            project.write_manifest(fields={"setting": "source"}, baseline=None)
            project.write_source('{"setting": 1}')
            project.write_target('{"setting": 1}')

            plan = build_plan(load_manifest(project.manifest_path))
            observation = plan.observations[0]

            self.assertEqual(observation.status, Status.UNBASED)
            self.assertEqual(observation.action, Action.REPORT)
            with self.assertRaises(ApplyError) as raised:
                apply_plan(plan)
            self.assertEqual(raised.exception.code, "m3_read_only")
            self.assertEqual(
                project.target.read_text(encoding="utf-8"), '{"setting": 1}'
            )

    def test_baseline_symlink_is_blocked_without_following_it(self) -> None:
        with _M3Project() as project:
            project.write_manifest(fields={"setting": "source"})
            project.write_source('{"setting": 1}')
            project.write_target('{"setting": 1}')
            project.write_baseline(
                _baseline_v3(owners={"setting": "source"}, values={"setting": 1})
            )
            real = project.root / "real-baseline.json"
            real.write_text(
                project.baseline.read_text(encoding="utf-8"), encoding="utf-8"
            )
            project.baseline.unlink()
            project.baseline.symlink_to(real)

            observation = build_plan(load_manifest(project.manifest_path)).observations[
                0
            ]

            self.assertEqual(observation.status, Status.BLOCKED)
            self.assertEqual(observation.action, Action.BLOCK)
            self.assertEqual(observation.reason, "baseline is not a regular file")

    def test_invalid_baseline_and_root_json_are_blocked_without_echoing_values(
        self,
    ) -> None:
        with _M3Project() as project:
            project.write_manifest(fields={"setting": "source"})
            project.write_source('["source-secret-sentinel"]')
            project.write_target('{"setting": 1}')
            project.write_baseline(
                json.dumps(
                    {
                        "schema_version": 1,
                        "resource": "settings",
                        "source": "templates/settings.json.j2",
                        "target": "live/settings.json",
                        "owners": {"setting": "source"},
                        "values": {},
                        "unexpected": "baseline-secret-sentinel",
                    }
                )
            )

            payload = cast(
                dict[str, Any],
                plan_to_dict(
                    build_plan(load_manifest(project.manifest_path)), command="plan"
                ),
            )
            self.assertEqual(payload["resources"][0]["status"], "blocked")
            serialized = json.dumps(payload)
            self.assertNotIn("source-secret-sentinel", serialized)
            self.assertNotIn("baseline-secret-sentinel", serialized)

    def test_v3_cli_apply_is_read_only_and_does_not_touch_target(self) -> None:
        with _M3Project() as project:
            project.write_manifest(fields={"setting": "source"})
            project.write_source('{"setting": 2}')
            project.write_target('{"setting": 1}')
            project.write_baseline(
                _baseline_v3(owners={"setting": "source"}, values={"setting": 1})
            )
            before = project.target.read_bytes()
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "apply",
                        "--manifest",
                        str(project.manifest_path),
                        "--yes",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(payload["applied"])
            self.assertEqual(payload["reason"], "m3_read_only")
            self.assertEqual(project.target.read_bytes(), before)
