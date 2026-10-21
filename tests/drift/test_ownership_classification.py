"""Tests for ownership classification."""

from __future__ import annotations

import json
import unittest

from luwu.errors import LuwuError
from luwu.ownership import classify_fields


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

    def test_provider_field_compares_desired_with_live_without_a_baseline_side(
        self,
    ) -> None:
        owners = {"key": "provider", "theme": "source"}
        for desired, live, status, decision, reason in (
            (
                b'{"key":"vault-value","theme":1}',
                b'{"key":"vault-value","theme":1}',
                "unchanged",
                "none",
                "live matches provider value",
            ),
            (
                b'{"key":"vault-value","theme":1}',
                b'{"key":"stale","theme":1}',
                "source_changed",
                "forward_candidate",
                "provider value differs from live",
            ),
            (
                b'{"key":"vault-value","theme":1}',
                b'{"theme":1}',
                "source_changed",
                "forward_candidate",
                "provider value differs from live",
            ),
        ):
            with self.subTest(live=live):
                result = classify_fields(
                    desired,
                    live,
                    fields=owners,
                    baseline=_baseline({"theme": 1}, owners),
                    resource_name="resource",
                    source_name="source",
                    target_name="target",
                )
                by_name = {field.name: field for field in result.fields}
                field = by_name["key"]
                self.assertEqual(
                    (field.status, field.decision, field.reason),
                    (status, decision, reason),
                )
                self.assertEqual(by_name["theme"].status, "unchanged")

    def test_provider_field_is_never_reverse_candidate_or_conflict(self) -> None:
        owners = {"key": "provider"}
        for desired, live in (
            (b'{"key":"vault-value"}', b'{"key":"other"}'),
            (b'{"key":"vault-value"}', b"{}"),
            (b"{}", b'{"key":"other"}'),
        ):
            with self.subTest(desired=desired, live=live):
                for baseline in (None, _baseline({}, owners)):
                    result = classify_fields(
                        desired,
                        live,
                        fields=owners,
                        baseline=baseline,
                        resource_name="resource",
                        source_name="source",
                        target_name="target",
                    )
                    field = result.fields[0]
                    self.assertNotEqual(field.status, "unbased")
                    self.assertNotIn(field.decision, {"reverse_candidate", "review"})
                    self.assertNotEqual(field.status, "conflict")

    def test_provider_field_without_baseline_is_not_unbased(self) -> None:
        result = classify_fields(
            b'{"key":"vault-value","theme":1}',
            b'{"key":"vault-value","theme":1}',
            fields={"key": "provider", "theme": "source"},
            baseline=None,
            resource_name="resource",
            source_name="source",
            target_name="target",
        )
        by_name = {field.name: field for field in result.fields}
        self.assertEqual(by_name["key"].status, "unchanged")
        self.assertEqual(by_name["theme"].status, "unbased")

    def test_provider_baseline_keeps_owners_but_rejects_provider_values(self) -> None:
        owners = {"key": "provider", "theme": "source"}
        result = classify_fields(
            b'{"key":"vault-value","theme":1}',
            b'{"key":"vault-value","theme":1}',
            fields=owners,
            baseline=_baseline({"theme": 1}, owners),
            resource_name="resource",
            source_name="source",
            target_name="target",
        )
        self.assertEqual(result.baseline_status, "provided")

        with self.assertRaises(LuwuError) as raised:
            classify_fields(
                b'{"key":"vault-value","theme":1}',
                b'{"key":"vault-value","theme":1}',
                fields=owners,
                baseline=_baseline({"theme": 1, "key": "vault-value"}, owners),
                resource_name="resource",
                source_name="source",
                target_name="target",
            )
        self.assertEqual(raised.exception.code, "ownership_invalid_input")
        self.assertNotIn("vault-value", str(raised.exception))

        with self.assertRaises(LuwuError):
            classify_fields(
                b'{"key":"vault-value","theme":1}',
                b'{"key":"vault-value","theme":1}',
                fields=owners,
                baseline=_baseline({"theme": 1}, {"theme": "source"}),
                resource_name="resource",
                source_name="source",
                target_name="target",
            )

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
